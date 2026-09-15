"""rosbag2 extraction, tested on real MCAP bags written without a ROS installation."""
import os
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("rosbags")

from rosbags.rosbag2 import StoragePlugin, Writer
from rosbags.typesys import Stores, get_typestore

from safegate.core.model import ConcreteRun, ExecutionTier, Pinning
from safegate.execution.adapter import ReplayRunner
from safegate.execution.rosbag import BagMapping, extract, resample
from safegate.sim import SilRunner

ROOT = Path(__file__).resolve().parents[1]
MAPPING = ROOT / "src" / "safegate" / "ros" / "safegate_state_mapping.yaml"
TS = get_typestore(Stores.ROS2_JAZZY)
JointState = TS.types["sensor_msgs/msg/JointState"]
Header = TS.types["std_msgs/msg/Header"]
Time = TS.types["builtin_interfaces/msg/Time"]
Float64 = TS.types["std_msgs/msg/Float64"]


def run_for(scenario, assignment):
    return ConcreteRun(test_case_ref="TC", scenario=scenario, assignment=assignment,
                       tier=ExecutionTier.SIL,
                       pinning=Pinning(scenario_hash="s", sut_build_hash="b", backend_hash="k",
                                       config_hash="c", seed=1))


def write_state_bag(path, trace, storage=StoragePlugin.MCAP, jitter_ns=0):
    """What the ROS 2 plant node publishes: one JointState per control period."""
    names = sorted(k for k in trace.signals if not k.startswith("__"))
    with Writer(path, version=9, storage_plugin=storage) as w:
        conn = w.add_connection("/safegate/state", JointState.__msgtype__, typestore=TS)
        for i, t in enumerate(trace.time):
            ns = round(float(t) * 1e9)
            msg = JointState(
                header=Header(stamp=Time(sec=ns // 1_000_000_000, nanosec=ns % 1_000_000_000),
                              frame_id="sim"),
                name=names,
                position=np.array([trace.signals[k][i] for k in names], dtype=np.float64),
                velocity=np.array([], dtype=np.float64),
                effort=np.array([], dtype=np.float64),
            )
            # bag receive time is wall clock with jitter; the header stamp is sim time
            w.write(conn, 1_700_000_000_000_000_000 + ns + (i % 7) * jitter_ns,
                    TS.serialize_cdr(msg, JointState.__msgtype__))


def test_extracted_bag_reproduces_the_simulator_trace(tmp_path):
    sut = ROOT / "examples" / "amr_project_revb" / "sut_config.yaml"
    run = run_for("scenarios/person_crossing.osc", {"truck_speed": 1.0, "entry_gap": 1.0,
                                                    "warning_field": 0.0, "floor_mu": 0.1})
    ref = SilRunner(sut).execute(run, tmp_path).trace
    write_state_bag(tmp_path / "bag", ref, jitter_ns=3_000_000)
    npz = extract(tmp_path / "bag", BagMapping.from_yaml(MAPPING), tmp_path / "signals.npz")
    d = np.load(npz)
    assert d["time"].size == ref.n
    assert np.allclose(d["time"], ref.time, atol=1e-9)
    for name in ("speed", "min_distance_to_person", "safe_stop", "protective_field_length"):
        assert np.array_equal(d[name], ref.signals[name]), name


def test_replay_runner_reads_rosbag2_directly(tmp_path):
    sut = ROOT / "examples" / "amr_project" / "sut_config.yaml"
    run = run_for("scenarios/muting_pick_station.osc", {"pick_duration_s": 3.0})
    ref = SilRunner(sut).execute(run, tmp_path).trace
    bags = tmp_path / "bags"
    bags.mkdir()
    np.savez(bags / "pick-001.signals.npz", time=ref.time, speed=ref.signals["speed"])
    write_state_bag(bags / "pick-042", ref, storage=StoragePlugin.SQLITE3)
    replay = ReplayRunner(bags, mapping=MAPPING)
    assert replay.recordings() == ["pick-001", "pick-042"]
    out = replay.execute(run_for("", {"bag_index": 1.0}), tmp_path / "w")
    assert out.ok, out.message
    assert np.array_equal(out.trace.signals["protective_device_muted"],
                          ref.signals["protective_device_muted"])


def test_zero_order_hold_aligns_topics_at_different_rates():
    fast = (np.arange(0, 1.0001, 0.01), np.arange(101, dtype=float))
    slow = (np.arange(0.05, 1.0001, 0.1), np.arange(10, dtype=float) * 10)
    t, s = resample({"a": fast, "b": slow}, 0.01)
    assert t[0] == 0.0 and s["a"][0] == 5.0          # grid starts when both exist (0.05 s)
    assert s["b"][9] == 0.0 and s["b"][10] == 10.0    # holds the slow value until its next sample


def test_float_topic_uses_bag_time_when_no_stamp(tmp_path):
    with Writer(tmp_path / "bag", version=9, storage_plugin=StoragePlugin.MCAP) as w:
        c = w.add_connection("/speed", Float64.__msgtype__, typestore=TS)
        for i in range(11):
            w.write(c, 5_000_000_000 + i * 10_000_000, TS.serialize_cdr(Float64(data=i * 0.1),
                                                                        Float64.__msgtype__))
    mapping = tmp_path / "m.yaml"
    mapping.write_text("dt: 0.01\nsignals:\n  speed: {topic: /speed, field: data}\n")
    d = np.load(extract(tmp_path / "bag", BagMapping.from_yaml(mapping), tmp_path / "s.npz"))
    assert d["time"].size == 11 and d["speed"][-1] == pytest.approx(1.0)


def test_mapping_errors_are_explicit(tmp_path):
    bad = tmp_path / "m.yaml"
    bad.write_text("signals:\n  speed: {topic: /odom}\n")
    with pytest.raises(ValueError, match="field, a name"):
        BagMapping.from_yaml(bad)


ROS2 = os.environ.get("SAFEGATE_TEST_ROS2")


@pytest.mark.skipif(not ROS2, reason="set SAFEGATE_TEST_ROS2=1 with the safegate-ros:jazzy image built")
def test_ros2_runner_records_and_extracts_a_real_bag(tmp_path):
    from safegate.execution.adapter import Ros2Runner
    sut = ROOT / "examples" / "amr_project_revb" / "sut_config.yaml"
    run = run_for("scenarios/occluded_emergence.osc", {"truck_speed": 1.0, "entry_gap": 1.5,
                                                       "rack_clearance": 0.3, "warning_field": 0.0})
    out = Ros2Runner(sut).execute(run, tmp_path / "ros")
    assert out.ok, out.message
    ref = SilRunner(sut).execute(run, tmp_path).trace
    assert out.trace.n == ref.n
    for name in ("speed", "min_distance_to_person", "detection", "safe_stop"):
        assert np.array_equal(out.trace.signals[name], ref.signals[name]), name
