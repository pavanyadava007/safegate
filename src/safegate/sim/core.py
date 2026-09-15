"""
safegate.sim.core
=================

A deterministic, closed-loop 2D software-in-the-loop model of a driverless
truck in an aisle, built to be the system under test for the campaign.

What is modelled, and why each piece is there:

  Truck plant        rectangular footprint on a straight aisle (y = 0),
                     forward or reverse travel, acceleration limited by the
                     navigation controller and deceleration limited by floor
                     friction (a <= mu * g). Friction is the term stopping
                     budgets most often get wrong, so it is a scenario
                     parameter, not a constant.
  Safety scanner     one at each end. Protective field = rectangle ahead of
                     the leading edge. Detection needs line of sight
                     (ray-cast against racks), is evaluated on a field
                     shortened by the measurement tolerance, and reaches the
                     controller only after the scanner response time.
  Safety controller  emulates the safety PLC parameter set in `SutConfig`:
                     speed-dependent field-set switching on the measured
                     (latency-delayed) speed, safe stop after reaction plus
                     bus latency, overspeed trip, zone speed supervision
                     (in-zone only, or ramp monitoring ahead of a slower
                     zone), and muting with an optional timeout.
  Navigation         non-safety: cruise, stop at goal, slow for speed zones
                     using its own (possibly wrong) position estimate, and
                     slow when a wide warning field sees a person. It is
                     deliberately fallible; the safety functions exist
                     because navigation is not trusted.

What is not modelled: tyre slip dynamics beyond the friction cap, steering,
sensor noise and reflectivity, pedestrians reacting to the truck. The
simulator is a SIL backend, and the technical file says that simulated
evidence does not discharge physical verification (policy R-EXEC-002).

Determinism: no randomness and no wall clock. Identical inputs give a
bit-identical trace, which the tests assert across processes.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from ..core.ids import digest

G = 9.81
PEDESTRIAN_RADIUS = 0.25
NO_ZONE_LIMIT = 99.0  # m/s; stands for "no speed zone at this position"


# --------------------------------------------------------------------------
# SUT parameter set
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldSet:
    """A protective field valid up to `v_max` (m/s), `length` m long."""

    v_max: float
    length: float


@dataclass(frozen=True)
class SpeedZone:
    x0: float
    x1: float
    limit: float  # m/s


@dataclass(frozen=True)
class SutConfig:
    """Safety-controller and navigation parameters of the truck under test.

    This is the "configuration" in the run pinning: its digest is the
    config hash, so a changed field table is a different system under test.
    """

    name: str = "unnamed"
    truck_length: float = 2.0
    truck_width: float = 1.0
    # safety scanner and controller
    front_field_sets: tuple[FieldSet, ...] = (FieldSet(2.0, 2.6),)
    rear_field_sets: tuple[FieldSet, ...] = (FieldSet(0.6, 0.8),)
    field_side_margin: float = 0.10
    field_selection_speed_margin: float = 0.0
    scanner_response_s: float = 0.08
    scanner_tolerance_m: float = 0.09
    controller_reaction_s: float = 0.06
    comm_latency_s: float = 0.03
    encoder_latency_s: float = 0.02
    safety_brake_decel: float = 1.5
    # Trip thresholds relative to a speed limit. They are separate because
    # they protect different things: the field table must not trip inside the
    # truck's rated speed range, while a zone supervision may need to trip
    # below the zone limit (negative offset) so that the reaction-time
    # overshoot stays inside the permitted maximum.
    field_overspeed_trip_offset: float = 0.05  # above the largest field set's v_max
    zone_trip_offset: float = 0.05  # relative to a speed-zone limit
    zone_ramp_monitoring: bool = False
    zone_ramp_decel: float = 1.2
    mute_timeout_s: float | None = None
    restart_delay_s: float = 1.0
    # navigation (not safety-rated)
    nav_accel: float = 0.5
    nav_decel: float = 0.8
    warning_field_enabled: bool = True
    warning_field_length: float = 4.0
    warning_field_side: float = 1.5
    warning_speed: float = 0.5
    speed_zones: tuple[SpeedZone, ...] = ()

    @staticmethod
    def from_dict(d: dict[str, Any]) -> SutConfig:
        known = {f.name for f in fields(SutConfig)}
        unknown = set(d) - known
        if unknown:
            raise ValueError(f"unknown SUT config keys: {sorted(unknown)}")
        kw = dict(d)
        for key in ("front_field_sets", "rear_field_sets"):
            if key in kw:
                kw[key] = tuple(
                    FieldSet(float(fs["v_max"]), float(fs["length"])) for fs in kw[key]
                )
        if "speed_zones" in kw:
            kw["speed_zones"] = tuple(
                SpeedZone(float(z["x0"]), float(z["x1"]), float(z["limit"]))
                for z in kw["speed_zones"]
            )
        cfg = SutConfig(**kw)
        cfg.check()
        return cfg

    @staticmethod
    def from_yaml(path: str | Path) -> SutConfig:
        with open(path, encoding="utf-8") as fh:
            return SutConfig.from_dict(yaml.safe_load(fh) or {})

    def check(self) -> None:
        for key in ("front_field_sets", "rear_field_sets"):
            sets = getattr(self, key)
            if not sets:
                raise ValueError(f"{key} is empty")
            vs = [fs.v_max for fs in sets]
            if vs != sorted(vs):
                raise ValueError(f"{key} must be sorted by v_max")

    def digest(self) -> str:
        return digest(_as_plain(self))

    def with_zones(self, zones: tuple[SpeedZone, ...]) -> SutConfig:
        """Scenario zones are added to the configured site zones."""
        kw = {f.name: getattr(self, f.name) for f in fields(self)}
        kw["speed_zones"] = tuple(self.speed_zones) + tuple(zones)
        return SutConfig(**kw)


def _as_plain(obj: Any) -> Any:
    if hasattr(obj, "__dataclass_fields__"):
        return {f.name: _as_plain(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [_as_plain(v) for v in obj]
    return obj


# --------------------------------------------------------------------------
# Geometry
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Rect:
    x0: float
    x1: float
    y0: float
    y1: float

    def contains(self, x: float, y: float) -> bool:
        return self.x0 <= x <= self.x1 and self.y0 <= y <= self.y1


def signed_distance_rect_circle(r: Rect, cx: float, cy: float, radius: float) -> float:
    """Distance from a rectangle to a circle; negative is penetration depth."""
    dx = max(r.x0 - cx, 0.0, cx - r.x1)
    dy = max(r.y0 - cy, 0.0, cy - r.y1)
    if dx == 0.0 and dy == 0.0:
        inside = min(cx - r.x0, r.x1 - cx, cy - r.y0, r.y1 - cy)
        return -inside - radius
    return math.hypot(dx, dy) - radius


def _overlap(a: Rect, b: Rect) -> bool:
    return a.x0 < b.x1 and b.x0 < a.x1 and a.y0 < b.y1 and b.y0 < a.y1


def segment_hits_rect(ax: float, ay: float, bx: float, by: float, r: Rect) -> bool:
    """Liang-Barsky clip of segment AB against an axis-aligned rectangle."""
    t0, t1 = 0.0, 1.0
    dx, dy = bx - ax, by - ay
    for p, q in (
        (-dx, ax - r.x0),
        (dx, r.x1 - ax),
        (-dy, ay - r.y0),
        (dy, r.y1 - ay),
    ):
        if p == 0.0:
            if q < 0.0:
                return False
        else:
            t = q / p
            if p < 0.0:
                if t > t1:
                    return False
                t0 = max(t0, t)
            else:
                if t < t0:
                    return False
                t1 = min(t1, t)
    return t0 <= t1


# --------------------------------------------------------------------------
# Actors
# --------------------------------------------------------------------------


@dataclass
class Pedestrian:
    name: str
    x: float
    y: float
    radius: float = PEDESTRIAN_RADIUS
    target: tuple[float, float] | None = None
    speed: float = 0.0

    def walk_to(self, x: float, y: float, speed: float) -> None:
        self.target = (x, y)
        self.speed = speed

    def step(self, dt: float) -> None:
        if self.target is None or self.speed <= 0.0:
            return
        tx, ty = self.target
        dx, dy = tx - self.x, ty - self.y
        dist = math.hypot(dx, dy)
        step = self.speed * dt
        if dist <= step:
            self.x, self.y = tx, ty
            self.target = None
        else:
            self.x += dx / dist * step
            self.y += dy / dist * step

    @property
    def walking(self) -> bool:
        return self.target is not None


@dataclass
class Truck:
    """Centre position `x` on the aisle axis; body is axis-aligned."""

    length: float
    width: float
    x: float = 0.0
    v: float = 0.0
    direction: int = 1  # +1 forward, -1 reverse; last commanded direction

    @property
    def body(self) -> Rect:
        h = self.length / 2.0
        w = self.width / 2.0
        return Rect(self.x - h, self.x + h, -w, w)

    @property
    def leading_x(self) -> float:
        return self.x + self.direction * self.length / 2.0


# --------------------------------------------------------------------------
# Delay line
# --------------------------------------------------------------------------


class Delay:
    """Fixed-length transport delay of `steps` samples."""

    def __init__(self, steps: int, initial: Any) -> None:
        self._q: deque[Any] = deque([initial] * max(0, steps))
        self._steps = max(0, steps)

    def push(self, value: Any) -> Any:
        if self._steps == 0:
            return value
        self._q.append(value)
        return self._q.popleft()


def _steps(seconds: float, dt: float) -> int:
    """Whole control periods covering `seconds`, rounded up.

    Rounding to nearest would model some latencies shorter than configured,
    which is the non-conservative direction for a safety simulation.
    """
    return max(0, math.ceil(seconds / dt - 1e-6))


# --------------------------------------------------------------------------
# World and closed loop
# --------------------------------------------------------------------------


SIGNALS = (
    "speed",
    "speed_commanded",
    "truck_x",
    "leading_edge_x",
    "min_distance_to_person",
    "person_in_field",
    "detection",
    "safe_stop",
    "protective_field_length",
    "protective_device_muted",
    "mute_requested",
    "zone_speed_limit",
    "warning_active",
    "floor_decel_limit",
)


@dataclass
class World:
    cfg: SutConfig
    dt: float = 0.01
    floor_mu: float = 0.6
    racks: list[Rect] = field(default_factory=list)  # block line of sight only
    fixtures: list[Rect] = field(default_factory=list)  # detected by the fields
    pedestrians: list[Pedestrian] = field(default_factory=list)
    nav_position_error: float = 0.0
    truck_x0: float = 0.0
    truck_v0: float = 0.0
    truck_direction0: int = 1  # +1 forward, -1 reverse, for a truck starting at rest
    # Override of cfg.warning_field_enabled. Verification of a safety
    # function must not take credit for non-safety layers, so test cases
    # sweep this off as well as on.
    warning_field: bool | None = None

    def __post_init__(self) -> None:
        c = self.cfg
        self.truck = Truck(c.truck_length, c.truck_width, x=self.truck_x0, v=self.truck_v0)
        self.truck.direction = -1 if (self.truck_v0 < 0 or self.truck_direction0 < 0) else 1
        self.t = 0.0
        self.step_index = 0
        # navigation command state
        self.cruise_speed = 0.0
        self.goal_x: float | None = None
        self._pending_direction: int | None = None
        self.mute_request = False
        # safety controller state
        self._v_hist = Delay(_steps(c.encoder_latency_s, self.dt), self.truck_v0)
        self._detect_delay = Delay(_steps(c.scanner_response_s, self.dt), False)
        self._stop_delay = Delay(
            _steps(c.controller_reaction_s + c.comm_latency_s, self.dt), False
        )
        self.safe_stop = False
        self._clear_since: int | None = None  # step index
        self._muting = False
        self._mute_started: int | None = None  # step index
        self._mute_expired = False
        self.active_field_length = self._select_field(self.truck_v0)[0]
        if self.truck_v0:
            self.cruise_speed = abs(self.truck_v0)
        self.recorded: dict[str, list[float]] = {k: [] for k in ("time", *SIGNALS)}
        self._last: dict[str, float] = {}
        self._record(
            self.cruise_speed if self.truck_v0 else 0.0,
            False,
            False,
            False,
            self.floor_mu * G,
            self.zone_limit_at(self.truck.leading_x),
            False,
        )

    # ---- commands (called by scenario scripts) -------------------------

    def drive_to(self, goal_x: float, cruise_speed: float) -> None:
        """Command a goal. A reversal of direction waits for standstill."""
        self.goal_x = goal_x
        self.cruise_speed = abs(cruise_speed)
        direction = 1 if goal_x >= self.truck.x else -1
        if self.truck.v == 0.0 or direction == self.truck.direction:
            self.truck.direction = direction
            self._pending_direction = None
        else:
            self._pending_direction = direction

    def request_muting(self, on: bool) -> None:
        self.mute_request = on

    def pedestrian(self, name: str) -> Pedestrian:
        for p in self.pedestrians:
            if p.name == name:
                return p
        raise KeyError(f"no pedestrian {name!r}")

    # ---- helpers --------------------------------------------------------

    def zone_limit_at(self, x: float) -> float:
        lim = NO_ZONE_LIMIT
        for z in self.cfg.speed_zones:
            if z.x0 <= x <= z.x1:
                lim = min(lim, z.limit)
        return lim

    def _field_rect(self, length: float, side: float, shrink: float) -> Rect:
        tr = self.truck
        w = tr.width / 2.0 + side
        eff = max(0.0, length - shrink)
        lead = tr.leading_x
        if tr.direction >= 0:
            return Rect(lead, lead + eff, -w, w)
        return Rect(lead - eff, lead, -w, w)

    def _visible_in(self, region: Rect) -> bool:
        sx, sy = self.truck.leading_x, 0.0
        for p in self.pedestrians:
            dx, dy = p.x - sx, p.y - sy
            d = math.hypot(dx, dy) or 1.0
            ux, uy = dx / d, dy / d
            samples = (
                (p.x, p.y),
                (p.x - ux * p.radius, p.y - uy * p.radius),
                (p.x - uy * p.radius, p.y + ux * p.radius),
                (p.x + uy * p.radius, p.y - ux * p.radius),
            )
            for qx, qy in samples:
                if not region.contains(qx, qy):
                    continue
                if not any(segment_hits_rect(sx, sy, qx, qy, r) for r in self.racks):
                    return True
        return False

    def _select_field(self, v_meas: float) -> tuple[float, bool]:
        c = self.cfg
        sets = c.front_field_sets if self.truck.direction >= 0 else c.rear_field_sets
        v_sel = abs(v_meas) + c.field_selection_speed_margin
        for fs in sets:
            if v_sel <= fs.v_max:
                return fs.length, False
        # faster than the largest field set: overspeed trip, largest field
        return sets[-1].length, abs(v_meas) > sets[-1].v_max + c.field_overspeed_trip_offset

    def _next_slower_zone(self, lead: float, direction: int) -> tuple[float, float] | None:
        """(distance to boundary, limit) of the nearest slower zone ahead."""
        here = self.zone_limit_at(lead)
        best: tuple[float, float] | None = None
        for z in self.cfg.speed_zones:
            if z.limit >= here:
                continue
            boundary = z.x0 if direction >= 0 else z.x1
            d = (boundary - lead) * direction
            if d < 0:
                continue
            if best is None or d < best[0]:
                best = (d, z.limit)
        return best

    # ---- one control period --------------------------------------------

    def step(self) -> None:
        c, tr, dt = self.cfg, self.truck, self.dt
        self.t = (self.step_index + 1) * dt
        self.step_index += 1

        # Actors move first; the truck then reacts to what it perceives. A
        # person does not walk into the truck body: a step that would
        # deepen contact is not taken. The truck can still drive into a
        # person, and that penetration is what the separation signal shows.
        body = tr.body
        for p in self.pedestrians:
            x_prev, y_prev = p.x, p.y
            before = signed_distance_rect_circle(body, p.x, p.y, p.radius)
            p.step(dt)
            after = signed_distance_rect_circle(body, p.x, p.y, p.radius)
            if after < 0.0 and after < before:
                p.x, p.y = x_prev, y_prev

        # --- safety controller (safety-rated inputs, delayed) ---
        v_meas = self._v_hist.push(tr.v)
        field_len, overspeed = self._select_field(v_meas)
        self.active_field_length = field_len

        # muting with optional timeout; re-arms only when the request drops
        if self.mute_request and not self._mute_expired:
            if self._mute_started is None:
                self._mute_started = self.step_index
            if (
                c.mute_timeout_s is not None
                and self.step_index - self._mute_started >= _steps(c.mute_timeout_s, self.dt)
            ):
                self._mute_expired = True
        if not self.mute_request:
            self._mute_started = None
            self._mute_expired = False
        self._muting = self.mute_request and not self._mute_expired

        pf = self._field_rect(field_len, c.field_side_margin, c.scanner_tolerance_m)
        intrusion = (not self._muting) and (
            self._visible_in(pf) or any(_overlap(pf, f) for f in self.fixtures)
        )
        detection = self._detect_delay.push(intrusion)

        lead = tr.leading_x
        zone_lim = self.zone_limit_at(lead)
        over_zone = abs(v_meas) > zone_lim + c.zone_trip_offset
        if c.zone_ramp_monitoring:
            nxt = self._next_slower_zone(lead, tr.direction)
            if nxt is not None:
                d, lim = nxt
                latency = c.encoder_latency_s + c.controller_reaction_s + c.comm_latency_s
                d_eff = max(0.0, d - abs(v_meas) * latency)
                # Same trip offset as inside the zone, so a trip right at the
                # boundary still leaves the reaction-time overshoot below
                # the limit.
                target = max(0.0, lim + c.zone_trip_offset)
                allowed = math.sqrt(target * target + 2.0 * c.zone_ramp_decel * d_eff)
                over_zone = over_zone or abs(v_meas) > allowed
        cause = self._stop_delay.push(detection or overspeed or over_zone)

        if cause:
            self.safe_stop = True
            self._clear_since = None
        elif self.safe_stop:
            if abs(tr.v) == 0.0:
                if self._clear_since is None:
                    self._clear_since = self.step_index
                if self.step_index - self._clear_since >= _steps(c.restart_delay_s, dt):
                    self.safe_stop = False
                    self._clear_since = None

        # --- navigation (not safety-rated) ---
        warn_on = c.warning_field_enabled if self.warning_field is None else self.warning_field
        warning = warn_on and self._visible_in(
            self._field_rect(c.warning_field_length, c.warning_field_side, 0.0)
        )
        v_cmd = self._nav_speed(warning)

        # --- plant ---
        a_floor = self.floor_mu * G
        if self.safe_stop:
            a = min(c.safety_brake_decel, a_floor)
            speed = max(0.0, abs(tr.v) - a * dt)
        else:
            cur = abs(tr.v)
            if v_cmd > cur:
                speed = min(v_cmd, cur + min(c.nav_accel, a_floor) * dt)
            else:
                speed = max(v_cmd, cur - min(c.nav_decel, a_floor) * dt)
        tr.v = tr.direction * speed
        tr.x += tr.v * dt
        if self._pending_direction is not None and speed == 0.0:
            tr.direction = self._pending_direction
            self._pending_direction = None

        self._record(v_cmd, intrusion, detection, warning, a_floor, zone_lim, self._muting)

    def _nav_speed(self, warning: bool) -> float:
        c, tr = self.cfg, self.truck
        if self.goal_x is None or self._pending_direction is not None:
            return 0.0  # no goal, or stopping before reversing direction
        direction = tr.direction
        believed_x = tr.x + self.nav_position_error * direction
        remaining = (self.goal_x - believed_x) * direction
        if remaining <= 0.0:
            self.goal_x = None
            return 0.0
        v = min(self.cruise_speed, math.sqrt(2.0 * c.nav_decel * remaining))
        believed_lead = believed_x + direction * c.truck_length / 2.0
        v = min(v, self.zone_limit_at(believed_lead))
        for z in c.speed_zones:
            boundary = z.x0 if direction >= 0 else z.x1
            d = (boundary - believed_lead) * direction
            if d > 0:
                v = min(v, math.sqrt(z.limit * z.limit + 2.0 * c.nav_decel * d))
        if warning:
            v = min(v, c.warning_speed)
        return v

    # ---- recording ------------------------------------------------------

    def _record(
        self,
        v_cmd: float,
        intrusion: bool,
        detection: bool,
        warning: bool,
        a_floor: float,
        zone_lim: float,
        muted: bool,
    ) -> None:
        tr = self.truck
        body = tr.body
        min_d = min(
            (signed_distance_rect_circle(body, p.x, p.y, p.radius) for p in self.pedestrians),
            default=NO_ZONE_LIMIT,
        )
        row = {
            "time": self.t,
            "speed": tr.v,
            "speed_commanded": v_cmd * tr.direction,
            "truck_x": tr.x,
            "leading_edge_x": tr.leading_x,
            "min_distance_to_person": min_d,
            "person_in_field": float(intrusion),
            "detection": float(detection),
            "safe_stop": float(self.safe_stop),
            "protective_field_length": self.active_field_length,
            "protective_device_muted": float(muted),
            "mute_requested": float(self.mute_request),
            "zone_speed_limit": zone_lim,
            "warning_active": float(warning),
            "floor_decel_limit": a_floor,
        }
        for k, v in row.items():
            self.recorded[k].append(v)

    def arrays(self) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        t = np.asarray(self.recorded["time"], dtype=float)
        sig = {k: np.asarray(v, dtype=float) for k, v in self.recorded.items() if k != "time"}
        return t, sig


# --------------------------------------------------------------------------
# Scripts: tiny coroutines advanced once per control period
# --------------------------------------------------------------------------


Script = Iterator[None]


def run_scripts(world: World, scripts: list[Script], horizon_s: float) -> World:
    """Lock-step loop: physics step, then advance every script one tick.

    This mirrors scenario_execution's step-based loop (simulation.step()
    followed by a behaviour-tree tick), so the in-process runner and the
    OSC2 runner see actions take effect on the same control period.
    """
    active = list(scripts)
    n = _steps(horizon_s, world.dt)
    for _ in range(n):
        world.step()
        still: list[Script] = []
        for s in active:
            try:
                next(s)
                still.append(s)
            except StopIteration:
                pass
        active = still
    return world


__all__ = [
    "NO_ZONE_LIMIT",
    "PEDESTRIAN_RADIUS",
    "SIGNALS",
    "Delay",
    "FieldSet",
    "G",
    "Pedestrian",
    "Rect",
    "Script",
    "SpeedZone",
    "SutConfig",
    "Truck",
    "World",
    "run_scripts",
    "segment_hits_rect",
    "signed_distance_rect_circle",
]
