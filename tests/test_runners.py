"""Execution adapters: OSC2 parity, the HIL contract, NullRunner geometry."""
from pathlib import Path

import numpy as np
import pytest

from safegate.core.model import ConcreteRun, ExecutionTier, Pinning
from safegate.execution.adapter import HilRunner, NullRunner, ScenarioExecutionRunner
from safegate.sim import SilRunner, SutConfig

ROOT = Path(__file__).resolve().parents[1]
SCEN = ROOT / "examples" / "amr_project"
SUT = SCEN / "sut_config.yaml"

CASES = {
    "person_crossing": {"truck_speed": 1.6, "entry_gap": 2.0, "warning_field": 0.0},
    "occluded_emergence": {"truck_speed": 1.2, "entry_gap": 1.5, "rack_clearance": 0.4},
    "speed_zone_entry": {"truck_speed": 1.8, "zone_speed_limit": 0.6, "nav_position_error": -0.4},
    "muting_pick_station": {"pick_duration_s": 2.5, "approach_speed": 0.25},
    "field_switching": {"truck_speed": 1.9, "goal_distance": 12.0},
    "reversing_at_dock": {"reverse_speed": 0.5, "person_gap": 2.0, "pedestrian_speed": 1.6},
}


def run_for(scenario, assignment, tc="TC"):
    return ConcreteRun(
        test_case_ref=tc, scenario=scenario, assignment=assignment, tier=ExecutionTier.SIL,
        pinning=Pinning(scenario_hash="s", sut_build_hash="b", backend_hash="k",
                        config_hash="c", seed=1),
    )


@pytest.mark.parametrize("family", sorted(CASES))
def test_osc2_through_scenario_execution_matches_in_process(family, tmp_path):
    pytest.importorskip("scenario_execution")
    scenario = f"scenarios/{family}.osc"
    run = run_for(scenario, CASES[family])
    osc = ScenarioExecutionRunner(SCEN, sut_config=SUT).execute(run, tmp_path)
    assert osc.ok, osc.message
    ref = SilRunner(SUT).execute(run, tmp_path)
    assert osc.trace.n == ref.trace.n
    for name, sig in ref.trace.signals.items():
        assert np.array_equal(osc.trace.signals[name], sig), name


def test_scenario_execution_reports_undeclared_parameter(tmp_path):
    pytest.importorskip("scenario_execution")
    run = run_for("scenarios/field_switching.osc", {"not_a_parameter": 1.0})
    out = ScenarioExecutionRunner(SCEN, sut_config=SUT).execute(run, tmp_path)
    assert not out.ok


def test_scenario_execution_missing_file_is_an_error(tmp_path):
    run = run_for("scenarios/nope.osc", {})
    out = ScenarioExecutionRunner(SCEN, sut_config=SUT).execute(run, tmp_path)
    assert not out.ok and "not found" in out.message


def test_reset_signature_lists_every_scenario_parameter():
    pytest.importorskip("scenario_execution")
    import inspect

    from safegate.sim.osc_bridge import SafeGateSimulation
    from safegate.sim.scenarios import PARAMETERS
    names = set(inspect.signature(SafeGateSimulation.reset).parameters) - {"self", "family"}
    assert names == set(PARAMETERS)
    src = (ROOT / "src/safegate/sim/scenarios.py").read_text()
    import re
    used = set(re.findall(r'_p\(\w+, "(\w+)"', src)) | {"warning_field"}
    assert used == set(PARAMETERS)


# ---- HIL contract ---------------------------------------------------------


@pytest.fixture()
def rig():
    from safegate.execution.hil_emulator import EmulatedRig, serve_in_thread
    r = EmulatedRig(SutConfig.from_yaml(SUT))
    server, url = serve_in_thread(r)
    yield r, url
    server.shutdown()


def test_emulated_rig_is_refused_by_default(rig):
    _, url = rig
    with pytest.raises(RuntimeError, match="physical: false"):
        HilRunner(url)


def test_emulated_rig_never_yields_hil_tier(rig, tmp_path):
    _, url = rig
    runner = HilRunner(url, allow_emulated=True)
    assert runner.tier is ExecutionTier.SIL and runner.name == "hil-emulated"
    out = runner.execute(run_for("scenarios/field_switching.osc", CASES["field_switching"]), tmp_path)
    assert out.ok, out.message
    ref = SilRunner(SUT).execute(run_for("scenarios/field_switching.osc", CASES["field_switching"]), tmp_path)
    assert np.array_equal(out.trace.signals["speed"], ref.trace.signals["speed"])


def test_scanner_checksum_mismatch_is_refused(rig):
    _, url = rig
    with pytest.raises(RuntimeError, match="scanner configuration checksum"):
        HilRunner(url, expected_scanner_config_checksum="0000", allow_emulated=True)


def test_rig_changing_mid_campaign_discards_the_run(rig, tmp_path):
    r, url = rig
    runner = HilRunner(url, allow_emulated=True)
    r.firmware_digest = "flashed-during-campaign"
    out = runner.execute(run_for("scenarios/field_switching.osc", {}), tmp_path)
    assert not out.ok and "changed during the campaign" in out.message


# ---- NullRunner regression ------------------------------------------------


def test_null_runner_does_not_count_a_person_behind_the_truck(tmp_path):
    """The shipped example's headline counterexample was this artefact."""
    a = {"brake_decel": 0.8, "detection_range": 2.5, "floor_friction": 0.55, "latency": 0.1,
         "pedestrian_lateral_offset": 6.0, "pedestrian_speed": 0.5, "truck_speed": 2.0}
    out = NullRunner().execute(run_for("", a), tmp_path)
    s = out.trace.signals
    assert s["min_distance_to_person"].min() > 1.0
    assert s["speed"].min() == pytest.approx(2.0)  # nothing ahead, so no braking
