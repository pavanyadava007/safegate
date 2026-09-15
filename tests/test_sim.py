"""SIL world: the physics and controller behaviour the campaign relies on."""
import json
import subprocess
import sys

import numpy as np
import pytest

from safegate.sim import FieldSet, Pedestrian, Rect, SpeedZone, SutConfig, World, build, run_scripts
from safegate.sim.core import segment_hits_rect, signed_distance_rect_circle

LONG = (FieldSet(0.5, 1.5), FieldSet(1.0, 3.0), FieldSet(2.0, 6.0))


def cfg(**kw):
    base = dict(front_field_sets=LONG, rear_field_sets=(FieldSet(1.0, 2.0),),
                warning_field_enabled=False)
    base.update(kw)
    return SutConfig(**base)


def test_signed_distance_is_negative_inside():
    r = Rect(0, 2, -0.5, 0.5)
    assert signed_distance_rect_circle(r, 3.0, 0.0, 0.25) == pytest.approx(0.75)
    assert signed_distance_rect_circle(r, 1.9, 0.0, 0.25) == pytest.approx(-0.35)


def test_segment_rect_intersection():
    r = Rect(1, 2, -1, 1)
    assert segment_hits_rect(0, 0, 3, 0, r)
    assert not segment_hits_rect(0, 2, 3, 2, r)
    assert not segment_hits_rect(0, 0, 0.9, 0, r)


def _stopping_distance(mu, v0, decel=1.5):
    w = World(cfg(safety_brake_decel=decel), floor_mu=mu, truck_v0=v0)
    w.drive_to(100.0, v0)
    w.safe_stop = True  # command a stop directly
    x0 = w.truck.x
    while w.truck.v > 0.0:
        w.step()
    return w.truck.x - x0


def test_braking_distance_matches_v2_over_2a():
    assert _stopping_distance(0.6, 1.5) == pytest.approx(1.5**2 / (2 * 1.5), abs=0.02)


def test_floor_friction_caps_deceleration():
    a = 0.10 * 9.81
    assert _stopping_distance(0.10, 1.0) == pytest.approx(1.0 / (2 * a), abs=0.02)


def test_stop_onset_equals_response_plus_reaction_plus_bus_latency():
    c = cfg(scanner_response_s=0.08, controller_reaction_s=0.06, comm_latency_s=0.03)
    ped = Pedestrian("p", 3.0, 0.0)
    w = World(c, pedestrians=[ped], truck_x0=-1.0, truck_v0=0.5)
    w.drive_to(100.0, 0.5)
    run_scripts(w, [], 3.0)
    t, s = w.arrays()
    assert s["person_in_field"].max() == 1.0
    first_intrusion = t[np.argmax(s["person_in_field"] > 0)]
    first_stop = t[np.argmax(s["safe_stop"] > 0)]
    assert first_stop - first_intrusion == pytest.approx(0.17, abs=0.011)


def test_rack_blocks_line_of_sight():
    c = cfg()
    ped = Pedestrian("p", 2.0, 0.7)
    open_world = World(c, pedestrians=[Pedestrian("p", 2.0, 0.7)], truck_v0=0.5)
    blocked = World(c, pedestrians=[ped], truck_v0=0.5,
                    racks=[Rect(1.5, 1.8, 0.2, 2.0)])
    for w in (open_world, blocked):
        w.drive_to(100.0, 0.5)
        run_scripts(w, [], 0.2)
    assert open_world.arrays()[1]["person_in_field"].max() == 1.0
    assert blocked.arrays()[1]["person_in_field"].max() == 0.0


def test_field_set_follows_measured_speed_and_trips_above_the_table():
    c = cfg(front_field_sets=(FieldSet(0.5, 1.0), FieldSet(1.0, 2.0)))
    w = World(c, truck_v0=0.3)
    w.drive_to(100.0, 1.5)  # navigation asks for more than the table allows
    run_scripts(w, [], 5.0)
    _, s = w.arrays()
    assert set(np.unique(s["protective_field_length"])) == {1.0, 2.0}
    assert s["safe_stop"].max() == 1.0
    assert np.abs(s["speed"]).max() < 1.0 + 0.05 + 0.1


@pytest.mark.parametrize("timeout, expect_max", [(None, 4.0), (1.8, 1.8)])
def test_mute_timeout(timeout, expect_max):
    c = cfg(mute_timeout_s=timeout)
    spec = build("muting_pick_station", {"pick_duration_s": 3.0, "approach_speed": 0.3}, c)
    w = run_scripts(spec.world, spec.scripts, spec.horizon_s)
    t, s = w.arrays()
    muted = s["protective_device_muted"] > 0.5
    longest, start = 0.0, None
    for i in range(t.size):
        if muted[i] and start is None:
            start = t[i]
        if start is not None and (not muted[i] or i == t.size - 1):
            longest = max(longest, t[i] - start)
            start = None
    if timeout is None:
        assert longest > 3.0
    else:
        assert longest == pytest.approx(expect_max, abs=0.02)


def test_ramp_monitoring_keeps_the_zone_limit_when_navigation_brakes_late():
    params = {"truck_speed": 1.8, "zone_speed_limit": 0.6, "nav_position_error": -0.6,
              "floor_mu": 0.3}

    def overspeed(ramp):
        # The ramp design also trips below the limit, so the reaction-time
        # overshoot stays inside the permitted 0.05 m/s.
        c = cfg(zone_ramp_monitoring=ramp, zone_trip_offset=-0.06 if ramp else 0.05)
        spec = build("speed_zone_entry", params, c)
        w = run_scripts(spec.world, spec.scripts, spec.horizon_s)
        _, s = w.arrays()
        return float(np.max(s["speed"] - s["zone_speed_limit"]))

    assert overspeed(False) > 0.2
    assert overspeed(True) <= 0.05


def test_unknown_config_key_is_rejected():
    with pytest.raises(ValueError, match="unknown SUT config keys"):
        SutConfig.from_dict({"front_field_set": []})


def test_zone_lookup_takes_the_slowest_overlapping_zone():
    c = cfg(speed_zones=(SpeedZone(0, 10, 1.0), SpeedZone(5, 20, 0.3)))
    w = World(c)
    assert w.zone_limit_at(2.0) == 1.0
    assert w.zone_limit_at(6.0) == 0.3


def test_every_family_runs():
    from safegate.sim.scenarios import FAMILIES
    for name in FAMILIES:
        spec = build(name, {}, cfg())
        w = run_scripts(spec.world, spec.scripts, spec.horizon_s)
        t, _ = w.arrays()
        assert t.size > 100 and np.all(np.diff(t) > 0)


_CHILD = """
import json, sys
from safegate.sim import SutConfig, build, run_scripts
spec = build("occluded_emergence", {"truck_speed": 1.4, "entry_gap": 2.0,
             "rack_clearance": 0.4, "pedestrian_speed": 1.6}, SutConfig())
w = run_scripts(spec.world, spec.scripts, spec.horizon_s)
t, s = w.arrays()
print(json.dumps({"x": s["truck_x"].tolist(), "d": s["min_distance_to_person"].tolist()}))
"""


def test_bit_identical_across_processes():
    outs = [
        json.loads(subprocess.run([sys.executable, "-c", _CHILD], check=True,
                                  capture_output=True, text=True,
                                  env={"PYTHONHASHSEED": str(seed), "PATH": ""}).stdout)
        for seed in (1, 2)
    ]
    assert outs[0] == outs[1]
