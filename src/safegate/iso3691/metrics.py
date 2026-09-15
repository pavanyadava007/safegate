"""
safegate.iso3691.metrics
========================

Quantitative evaluators for EN ISO 3691-4 personnel-detection requirements,
expressed as STL predicates so they compose with everything else.

The central obligation is simple to state and easy to get wrong: a
driverless truck must detect a person in its path and come to a standstill
*before contact*. Turning that into a number requires a budget:

    L_pf  >=  v * t_lat                             travel during latency
              + v^2 / (2 * a_brake)                 braking distance
              + K * (t_lat + v / a_brake)           human approach allowance
              + Z_s                                 sensor measurement tolerance
              + m_safety                            design margin

    with t_lat = t_detect + t_react + t_comm

Where the terms come from:

  v            worst-case speed in the direction of travel at the moment
               of detection, NOT the nominal speed. Overspeed detection is
               a separate safety function (ISO 3691-4, 4.3.1); if it is
               credited, v is the overspeed trip point, not the setpoint.
  t_detect     scanner response time, from the device certificate.
  t_react      controller + safety logic latency to command a stop.
  t_comm       bus latency, worst case, not mean. This is the term teams
               routinely under-budget because they measure it on an idle
               bus.
  a_brake      *guaranteed minimum* deceleration on the worst floor
               condition in the intended operating conditions, not the
               figure from the brake datasheet.
  Z_s          measurement tolerance of the protective device, from the
               certificate. For safety laser scanners this is typically
               tens of millimetres and grows with reflectivity.
  K term       allowance for a person walking toward the truck. ISO 13855
               writes S = K * T + C with T the overall stopping time: the
               protective device response plus the time the machine needs
               to stop. A person keeps walking while the truck brakes, so T
               is t_lat + v / a_brake, not t_lat alone. K = 1.6 m/s. Whether
               the term applies depends on the situation and is an explicit
               input here rather than a hidden constant.
  m_safety     your margin. Making it an explicit input means a reviewer
               can see it, and the gate can require it to be positive.

Everything below returns *margins in metres*, so that the STL robustness
value is directly interpretable: "we cleared the requirement by 84 mm".

This module deliberately does not encode clause numbers as gospel. The
standard is behind a paywall, revisions move clauses, and a tool that
hard-codes "clause 4.3.1" and is wrong is worse than one that carries the
engineering relationship and lets the user cite. Clause references are
data (see `StoppingBudget.clause_refs`), supplied by the project.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..stl.robustness import Formula, Predicate, Trace

K_HUMAN_APPROACH = 1.6  # m/s, ISO 13855 walking-approach speed constant


@dataclass(frozen=True)
class StoppingBudget:
    """The latency and braking budget for one protective-field configuration."""

    t_detect_s: float  # protective device response time
    t_react_s: float  # safety controller reaction time
    t_comm_s: float = 0.0  # worst-case bus / network latency
    a_brake_mps2: float = 1.0  # guaranteed minimum deceleration
    z_s_m: float = 0.0  # measurement tolerance of the device
    human_approach: bool = False  # credit ISO 13855 K term
    margin_m: float = 0.0  # explicit design margin
    clause_refs: tuple[str, ...] = ()

    @property
    def total_latency_s(self) -> float:
        return self.t_detect_s + self.t_react_s + self.t_comm_s

    def human_approach_m(self, v_mps: float) -> float:
        """K * T with T = latency + stopping time (ISO 13855 overall T)."""
        if not self.human_approach:
            return 0.0
        return K_HUMAN_APPROACH * (self.total_latency_s + v_mps / self.a_brake_mps2)

    def required_field_length(self, v_mps: float) -> float:
        """Minimum protective-field length for a given speed, in metres."""
        if v_mps <= 0:
            return self.z_s_m + self.margin_m
        if self.a_brake_mps2 <= 0:
            return math.inf
        t = self.total_latency_s
        d_latency = v_mps * t
        d_brake = (v_mps * v_mps) / (2.0 * self.a_brake_mps2)
        return d_latency + d_brake + self.human_approach_m(v_mps) + self.z_s_m + self.margin_m

    def max_permitted_speed(self, field_length_m: float) -> float:
        """Invert the budget: fastest speed a given field can protect.

        Solves  qa*v^2 + qb*v + qc = 0  with
            qa = 1/(2*a_brake)
            qb = t_lat + K/a_brake          (K term only if credited)
            qc = K*t_lat + Z_s + margin - L
        Used for speed-zone design: given the field you can physically
        project, what is the speed limit that must be enforced?
        """
        t = self.total_latency_s
        k = K_HUMAN_APPROACH if self.human_approach else 0.0
        qc = k * t + self.z_s_m + self.margin_m - field_length_m
        if qc >= 0:
            return 0.0
        qa = 1.0 / (2.0 * self.a_brake_mps2)
        qb = t + k / self.a_brake_mps2
        disc = qb * qb - 4 * qa * qc
        return max(0.0, (-qb + math.sqrt(disc)) / (2 * qa))


# --------------------------------------------------------------------------
# STL predicates
# --------------------------------------------------------------------------


def protective_field_adequacy(
    budget: StoppingBudget,
    speed_signal: str = "speed",
    field_signal: str = "protective_field_length",
) -> Formula:
    """rho = L_pf(t) - L_required(v(t)).  Positive means adequate.

    Wrap in `always(...)` to assert it holds for the whole run. Evaluated
    pointwise so that dynamic field switching (speed zones) is handled
    correctly: a truck that shrinks its field before slowing down is the
    classic latent defect this catches.
    """

    def fn(sig, env):
        v = np.asarray(sig[speed_signal], dtype=float)
        L = np.asarray(sig[field_signal], dtype=float)
        req = np.array([budget.required_field_length(abs(float(x))) for x in v])
        return L - req

    return Predicate(
        "protective_field_adequacy", fn, uses={speed_signal, field_signal}
    )


def separation_maintained(
    min_separation_m: float = 0.0,
    distance_signal: str = "min_distance_to_person",
) -> Formula:
    """rho = d(t) - d_min. The contact-avoidance invariant.

    This is the requirement that actually matters; field adequacy is the
    design argument for why it holds. Test both: the first catches design
    errors, the second catches the cases the design argument missed.
    """

    def fn(sig, env):
        d = np.asarray(sig[distance_signal], dtype=float)
        floor = float(env.get("d_min", min_separation_m))
        return d - floor

    return Predicate("separation_maintained", fn, uses={distance_signal})


def stop_within_time(
    t_max_s: float,
    trigger_signal: str = "person_in_field",
    speed_signal: str = "speed",
    v_standstill: float = 0.05,
) -> Formula:
    """rho = t_max - t_actual, in seconds, for the worst trigger event.

    Computed directly rather than as an STL composition because the
    'time from first trigger to standstill' quantity is what the test
    report must state, and deriving it from a nested F/G robustness value
    loses the number.
    """

    def fn(sig, env):
        trig = np.asarray(sig[trigger_signal], dtype=float) > 0.5
        v = np.asarray(sig[speed_signal], dtype=float)
        t = np.asarray(sig["__time__"], dtype=float)
        worst = math.inf
        i = 0
        n = trig.size
        while i < n:
            if trig[i]:
                t0 = t[i]
                j = i
                stopped_at = None
                while j < n:
                    if abs(v[j]) <= v_standstill:
                        stopped_at = t[j]
                        break
                    j += 1
                elapsed = (stopped_at - t0) if stopped_at is not None else math.inf
                worst = min(worst, t_max_s - elapsed)
                while i < n and trig[i]:
                    i += 1
            else:
                i += 1
        if worst is math.inf:
            # No trigger occurred. Returned as exactly 0.0 (on the boundary)
            # rather than a comfortable margin, so a scenario that never
            # exercised the function cannot report headroom it did not show.
            return np.full(t.size, 0.0)
        return np.full(t.size, float(worst))

    return Predicate(
        "stop_within_time", fn, uses={trigger_signal, speed_signal, "__time__"}
    )


def speed_zone_respected(
    zone_signal: str = "zone_speed_limit", speed_signal: str = "speed"
) -> Formula:
    """rho = v_limit(t) - v(t). Overspeed detection (ISO 3691-4, 4.3)."""

    def fn(sig, env):
        return np.asarray(sig[zone_signal], dtype=float) - np.asarray(
            sig[speed_signal], dtype=float
        )

    return Predicate("speed_zone_respected", fn, uses={zone_signal, speed_signal})


def muting_is_bounded(
    max_mute_s: float,
    mute_signal: str = "protective_device_muted",
) -> Formula:
    """rho = max_mute - longest_muted_interval.

    Muting is where AMR safety cases go to die: a field is suppressed for
    a legitimate reason (pallet pickup, docking) and the suppression
    outlives its justification. Bounding the interval is cheap and catches
    a real class of defect.
    """

    def fn(sig, env):
        m = np.asarray(sig[mute_signal], dtype=float) > 0.5
        t = np.asarray(sig["__time__"], dtype=float)
        longest = 0.0
        start = None
        for i in range(m.size):
            if m[i] and start is None:
                start = t[i]
            elif not m[i] and start is not None:
                longest = max(longest, t[i] - start)
                start = None
        if start is not None:
            longest = max(longest, t[-1] - start)
        return np.full(t.size, max_mute_s - longest)

    return Predicate("muting_is_bounded", fn, uses={mute_signal, "__time__"})


def attach_time_signal(trace: Trace) -> Trace:
    """Predicates that need absolute time read it from `__time__`."""
    if "__time__" not in trace.signals:
        trace.signals["__time__"] = trace.time.copy()
    return trace


# --------------------------------------------------------------------------
# Reporting helper
# --------------------------------------------------------------------------


@dataclass
class FieldSizingTable:
    """Speed-zone table: the artefact that goes into the operating manual."""

    budget: StoppingBudget
    speeds_mps: list[float] = field(default_factory=lambda: [0.3, 0.6, 1.0, 1.5, 2.0])

    def rows(self) -> list[dict[str, float]]:
        return [
            {
                "speed_mps": v,
                "latency_m": v * self.budget.total_latency_s,
                "braking_m": (v * v) / (2 * self.budget.a_brake_mps2),
                "approach_m": self.budget.human_approach_m(v),
                "tolerance_m": self.budget.z_s_m,
                "margin_m": self.budget.margin_m,
                "required_field_m": self.budget.required_field_length(v),
            }
            for v in self.speeds_mps
        ]

    def render(self) -> str:
        hdr = (
            f"{'v [m/s]':>8} {'latency':>9} {'braking':>9} {'approach':>9} "
            f"{'tol':>7} {'margin':>8} {'L_req [m]':>10}"
        )
        out = [hdr, "-" * len(hdr)]
        for r in self.rows():
            out.append(
                f"{r['speed_mps']:>8.2f} {r['latency_m']:>9.3f} "
                f"{r['braking_m']:>9.3f} {r['approach_m']:>9.3f} {r['tolerance_m']:>7.3f} "
                f"{r['margin_m']:>8.3f} {r['required_field_m']:>10.3f}"
            )
        return "\n".join(out)


__all__ = [
    "K_HUMAN_APPROACH",
    "FieldSizingTable",
    "StoppingBudget",
    "attach_time_signal",
    "muting_is_bounded",
    "protective_field_adequacy",
    "separation_maintained",
    "speed_zone_respected",
    "stop_within_time",
]
