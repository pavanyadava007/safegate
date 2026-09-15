"""
safegate.sim.scenarios
======================

Scenario families for the SIL world. Each family turns a parameter
assignment (a point in a test case's ODD box) into a world layout plus
scripts that sequence the actors.

A family returns a ScenarioSpec: the world (static layout and initial
state), the behaviour scripts, and the geometry numbers those scripts use.
The in-process SilRunner advances the scripts. The OSC2 path builds the same
world inside `SafeGateSimulation.reset()`, ignores the Python scripts, and
expresses the behaviour as an OpenSCENARIO 2 behaviour tree whose actions
call the same `Actions` methods with the same geometry, so both runners
share one definition of what "the pedestrian starts walking" means.

Units: metres, seconds, m/s. The aisle runs along +x at y = 0.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field

from .core import PEDESTRIAN_RADIUS, Pedestrian, Rect, Script, SpeedZone, SutConfig, World

LEAD_IN_M = 3.0  # distance the truck cruises before the scenario trigger


def _p(params: Mapping[str, float], key: str, default: float) -> float:
    return float(params.get(key, default))


def _warning(params: Mapping[str, float]) -> bool | None:
    """`warning_field` parameter: 0 off, 1 on, absent or negative = config."""
    if "warning_field" not in params or float(params["warning_field"]) < 0:
        return None
    return float(params["warning_field"]) >= 0.5


# --------------------------------------------------------------------------
# Actions shared by the Python scripts and the OSC2 behaviour tree
# --------------------------------------------------------------------------


class Actions:
    """Conditions and commands over a World. All return immediately."""

    def __init__(self, world: World) -> None:
        self.w = world

    # conditions
    def gap_to_line(self, x_line: float) -> float:
        tr = self.w.truck
        return (x_line - tr.leading_x) * tr.direction

    def truck_stopped(self) -> bool:
        return abs(self.w.truck.v) == 0.0

    # commands
    def walk(self, name: str, x: float, y: float, speed: float) -> None:
        self.w.pedestrian(name).walk_to(x, y, speed)

    def mute(self, on: bool) -> None:
        self.w.request_muting(on)

    def drive_to(self, goal_x: float, speed: float) -> None:
        self.w.drive_to(goal_x, speed)


def wait_until(cond: Callable[[], bool]) -> Iterator[None]:
    while not cond():
        yield


def wait_elapsed(world: World, seconds: float) -> Iterator[None]:
    start = world.t
    while world.t - start < seconds - 1e-9:
        yield


# --------------------------------------------------------------------------
# Families
# --------------------------------------------------------------------------


@dataclass
class ScenarioSpec:
    world: World
    scripts: list[Script]
    horizon_s: float
    # Numbers the behaviour needs (trigger gap, walk target, ...). The OSC2
    # actions read them from here instead of re-deriving them.
    geometry: dict[str, float] = field(default_factory=dict)


def _crossing_geometry(params: Mapping[str, float], cfg: SutConfig) -> dict[str, float]:
    v = max(0.05, _p(params, "truck_speed", 1.0))
    ped_v = max(0.05, _p(params, "pedestrian_speed", 1.4))
    offset = max(0.0, _p(params, "pedestrian_start_offset", 2.0))
    gap = _p(params, "entry_gap", 4.0)
    side = 1.0 if _p(params, "approach_side", 1.0) >= 0 else -1.0
    y_edge = cfg.truck_width / 2.0 + PEDESTRIAN_RADIUS
    t_walk = offset / ped_v
    trigger = gap + v * t_walk
    lead0 = -(trigger + LEAD_IN_M)
    return {
        "v": v, "ped_v": ped_v, "offset": offset, "gap": gap, "side": side,
        "y_edge": y_edge, "trigger": trigger, "lead0": lead0,
    }


def person_crossing(params: Mapping[str, float], cfg: SutConfig, dt: float = 0.01) -> ScenarioSpec:
    """A person steps into an open aisle in front of a cruising truck.

    `entry_gap` is the distance between the truck's leading edge and the
    crossing line at the moment the person reaches the truck's corridor,
    had the truck kept cruising. It is the ODD assumption that matters: no
    field can protect someone who steps in closer than the stopping distance.
    """
    g = _crossing_geometry(params, cfg)
    ped = Pedestrian("p1", 0.0, g["side"] * (g["y_edge"] + g["offset"]))
    world = World(
        cfg,
        dt=dt,
        floor_mu=_p(params, "floor_mu", 0.5),
        pedestrians=[ped],
        truck_x0=g["lead0"] - cfg.truck_length / 2.0,
        truck_v0=g["v"],
        warning_field=_warning(params),
    )
    world.drive_to(60.0, g["v"])
    geom = {"x_line": 0.0, "trigger": g["trigger"], "walk_x": 0.0,
            "walk_y": -g["side"] * (g["y_edge"] + 3.0), "walk_speed": g["ped_v"]}
    horizon = LEAD_IN_M / g["v"] + (g["offset"] + 2 * g["y_edge"] + 3.0) / g["ped_v"] + 4.0
    return ScenarioSpec(
        world, [crossing_script(world, geom)], min(60.0, max(6.0, horizon)), geom
    )


def crossing_script(world: World, geom: Mapping[str, float]) -> Iterator[None]:
    act = Actions(world)
    yield from wait_until(lambda: act.gap_to_line(geom["x_line"]) <= geom["trigger"])
    act.walk("p1", geom["walk_x"], geom["walk_y"], geom["walk_speed"])


def occluded_emergence(
    params: Mapping[str, float], cfg: SutConfig, dt: float = 0.01
) -> ScenarioSpec:
    """A person walks out of a cross aisle between two rack blocks.

    The racks hide the person from both scanner fields until they are close
    to the main aisle, so the non-safety warning field gets little or no
    chance to slow the truck. `rack_clearance` is the gap between the truck
    body and the rack face.
    """
    clearance = max(0.05, _p(params, "rack_clearance", 0.6))
    y_rack = cfg.truck_width / 2.0 + clearance
    y_start = y_rack + 1.0
    offset = y_start - (cfg.truck_width / 2.0 + PEDESTRIAN_RADIUS)
    merged = dict(params)
    merged["pedestrian_start_offset"] = offset
    g = _crossing_geometry(merged, cfg)
    s = g["side"]
    half_aisle = 0.9  # cross aisle 1.8 m wide
    lo, hi = sorted((s * y_rack, s * (y_rack + 4.0)))
    racks = [
        Rect(-40.0, -half_aisle, lo, hi),
        Rect(half_aisle, 40.0, lo, hi),
    ]
    ped = Pedestrian("p1", 0.0, s * y_start)
    world = World(
        cfg,
        dt=dt,
        floor_mu=_p(params, "floor_mu", 0.5),
        racks=racks,
        pedestrians=[ped],
        truck_x0=g["lead0"] - cfg.truck_length / 2.0,
        truck_v0=g["v"],
        warning_field=_warning(params),
    )
    world.drive_to(60.0, g["v"])
    geom = {"x_line": 0.0, "trigger": g["trigger"], "walk_x": 0.0,
            "walk_y": -s * (g["y_edge"] + 3.0), "walk_speed": g["ped_v"]}
    horizon = LEAD_IN_M / g["v"] + (offset + 2 * g["y_edge"] + 3.0) / g["ped_v"] + 4.0
    return ScenarioSpec(
        world, [crossing_script(world, geom)], min(60.0, max(6.0, horizon)), geom
    )


def speed_zone_entry(params: Mapping[str, float], cfg: SutConfig, dt: float = 0.01) -> ScenarioSpec:
    """A cruising truck enters a slower zone that starts at x = 0.

    `nav_position_error` shifts where navigation believes the truck is;
    negative values make it brake late. The safety controller measures
    position correctly, so this probes whether the speed supervision, not
    navigation, keeps the limit.
    """
    v = max(0.05, _p(params, "truck_speed", 1.2))
    limit = _p(params, "zone_speed_limit", 0.6)
    zcfg = cfg.with_zones((SpeedZone(0.0, 200.0, limit),))
    approach = v * v / (2.0 * cfg.nav_decel) + 4.0
    world = World(
        zcfg,
        dt=dt,
        floor_mu=_p(params, "floor_mu", 0.5),
        nav_position_error=_p(params, "nav_position_error", 0.0),
        truck_x0=-approach - cfg.truck_length / 2.0,
        truck_v0=v,
    )
    world.drive_to(150.0, v)
    horizon = approach / max(0.1, (v + min(v, limit)) / 2.0) + 4.0
    return ScenarioSpec(world, [], min(60.0, max(6.0, horizon)))


def muting_pick_station(
    params: Mapping[str, float], cfg: SutConfig, dt: float = 0.01
) -> ScenarioSpec:
    """The truck docks at a pick station and navigation asks for muting.

    Muting is requested for the last `mute_start_gap` metres of the approach
    and held for `pick_duration_s` after standstill. Whether muting lasts
    longer than the requirement allows is decided by the safety controller's
    timeout, which is exactly what is under test.
    """
    v = max(0.05, _p(params, "approach_speed", 0.3))
    pick = max(0.0, _p(params, "pick_duration_s", 2.0))
    start_gap = _p(params, "mute_start_gap", 0.3)
    station_x = 0.0
    dock_lead = station_x - 0.05
    world = World(
        cfg,
        dt=dt,
        truck_x0=dock_lead - 5.0 - cfg.truck_length / 2.0,
        truck_v0=v,
    )
    world.drive_to(dock_lead - cfg.truck_length / 2.0, v)
    geom = {"x_line": station_x, "mute_start_gap": start_gap, "pick_duration_s": pick}
    horizon = 5.0 / v + pick + 6.0
    return ScenarioSpec(world, [muting_script(world, geom)], min(60.0, horizon), geom)


def muting_script(world: World, geom: Mapping[str, float]) -> Iterator[None]:
    act = Actions(world)
    yield from wait_until(lambda: act.gap_to_line(geom["x_line"]) <= geom["mute_start_gap"])
    act.mute(True)
    yield from wait_until(act.truck_stopped)
    yield from wait_elapsed(world, geom["pick_duration_s"])
    act.mute(False)


def field_switching(params: Mapping[str, float], cfg: SutConfig, dt: float = 0.01) -> ScenarioSpec:
    """Accelerate from rest to cruise and stop at a goal, in an empty aisle.

    Exercises every field-set switch in both directions of the speed ramp;
    the criterion compares the active field with the stopping budget.
    """
    v = max(0.05, _p(params, "truck_speed", 1.2))
    dist = max(1.0, _p(params, "goal_distance", 20.0))
    world = World(cfg, dt=dt, floor_mu=_p(params, "floor_mu", 0.5))
    world.drive_to(dist, v)
    horizon = v / cfg.nav_accel + dist / v + v / cfg.nav_decel + 3.0
    return ScenarioSpec(world, [], min(90.0, horizon))


def reversing_at_dock(
    params: Mapping[str, float], cfg: SutConfig, dt: float = 0.01
) -> ScenarioSpec:
    """The truck starts reversing toward a dock while a person walks toward it.

    The truck starts at rest, as it would after loading. A person already
    inside the rear field therefore prevents the start; a person outside it
    walking toward the truck is the ISO 13855 approach case the K term in
    the stopping budget is meant to cover. `pedestrian_speed = 0` is the
    stationary test piece.
    """
    v = max(0.05, _p(params, "reverse_speed", 0.3))
    gap = max(0.3, _p(params, "person_gap", 3.0))
    ped_v = max(0.0, _p(params, "pedestrian_speed", 1.0))
    x0 = cfg.truck_length / 2.0
    rear = x0 - cfg.truck_length / 2.0
    ped_x = rear - gap - PEDESTRIAN_RADIUS
    ped = Pedestrian("p1", ped_x, _p(params, "lateral_offset", 0.0))
    world = World(
        cfg,
        dt=dt,
        floor_mu=_p(params, "floor_mu", 0.5),
        pedestrians=[ped],
        truck_x0=x0,
        truck_v0=0.0,
        truck_direction0=-1,
        warning_field=_warning(params),
    )
    world.drive_to(ped_x - 10.0, v)
    geom = {"walk_x": ped_x + 30.0, "walk_y": ped.y, "walk_speed": ped_v}
    return ScenarioSpec(world, [approach_script(world, geom)], 12.0, geom)


def approach_script(world: World, geom: Mapping[str, float]) -> Iterator[None]:
    Actions(world).walk("p1", geom["walk_x"], geom["walk_y"], geom["walk_speed"])
    yield


FAMILIES: dict[str, Callable[..., ScenarioSpec]] = {
    "person_crossing": person_crossing,
    "occluded_emergence": occluded_emergence,
    "speed_zone_entry": speed_zone_entry,
    "muting_pick_station": muting_pick_station,
    "field_switching": field_switching,
    "reversing_at_dock": reversing_at_dock,
}


def family_for(scenario: str) -> str:
    """Scenario family from a template reference such as scenarios/x.osc."""
    stem = scenario.replace("\\", "/").rsplit("/", 1)[-1]
    stem = stem.split(".", 1)[0]
    if stem not in FAMILIES:
        raise KeyError(f"no SIL scenario family {stem!r}; known: {sorted(FAMILIES)}")
    return stem


def build(scenario: str, params: Mapping[str, float], cfg: SutConfig, dt: float = 0.01) -> ScenarioSpec:
    return FAMILIES[family_for(scenario)](params, cfg, dt)


# Every scenario parameter any family reads. The OSC2 bridge exposes exactly
# these as reset() arguments; a test keeps the two lists in sync.
PARAMETERS = (
    "approach_side",
    "approach_speed",
    "entry_gap",
    "floor_mu",
    "goal_distance",
    "lateral_offset",
    "mute_start_gap",
    "nav_position_error",
    "pedestrian_speed",
    "pedestrian_start_offset",
    "person_gap",
    "pick_duration_s",
    "rack_clearance",
    "reverse_speed",
    "truck_speed",
    "warning_field",
    "zone_speed_limit",
)

__all__ = [
    "FAMILIES",
    "PARAMETERS",
    "Actions",
    "ScenarioSpec",
    "approach_script",
    "build",
    "crossing_script",
    "family_for",
    "muting_script",
    "wait_elapsed",
    "wait_until",
]
