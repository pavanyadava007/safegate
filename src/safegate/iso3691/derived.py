"""
safegate.iso3691.derived
========================

Derived signals: quantities a requirement talks about that no sensor
publishes directly.

A runner reports what happened (speed, positions, the active protective
field, whether muting was asserted). A requirement is usually phrased in
terms of what *should* have been true given the design budget: "the field
is at least as long as the stopping budget requires at this speed", "muting
never lasts longer than 2 s". Computing those from the raw trace here, once,
means the STL stays in the fifteen-minute grammar and every runner - SIL,
replay, scenario_execution or a HIL rig - gets the same definitions.

Signals added (only when their inputs are present and the name is free):

  required_field_length    L_req(|v|) from the StoppingBudget, metres
  muted_elapsed            duration of the current muting interval up to
                           the end of this sample (sample-and-hold, so N
                           muted samples read N * dt), 0 while not muted
  separation_while_moving  min_distance_to_person while |v| > v_standstill,
                           otherwise +cap. ISO 3691-4 asks the truck not to
                           strike a person; a person walking into a truck
                           that is already at standstill is not a failure
                           of the personnel-detection function.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from ..stl.robustness import Trace
from .metrics import K_HUMAN_APPROACH, StoppingBudget


@dataclass(frozen=True)
class DerivedSignals:
    budget: StoppingBudget | None = None
    v_standstill: float = 0.05
    separation_cap: float = 10.0

    def __call__(self, trace: Trace) -> Trace:
        sig = dict(trace.signals)
        t = trace.time
        sig.setdefault("__time__", t)

        speed = sig.get("speed")
        if self.budget is not None and speed is not None:
            sig.setdefault("required_field_length", self.required_length(np.abs(speed)))

        muted = sig.get("protective_device_muted")
        if muted is not None and "muted_elapsed" not in sig:
            sig["muted_elapsed"] = _elapsed_while(np.asarray(muted) > 0.5, t)

        dist = sig.get("min_distance_to_person")
        if dist is not None and speed is not None and "separation_while_moving" not in sig:
            moving = np.abs(speed) > self.v_standstill
            sig["separation_while_moving"] = np.where(
                moving, np.minimum(dist, self.separation_cap), self.separation_cap
            )
        return Trace(time=t, signals=sig)

    def required_length(self, v: np.ndarray) -> np.ndarray:
        """Vectorised StoppingBudget.required_field_length."""
        b = self.budget
        assert b is not None
        v = np.asarray(v, dtype=float)
        tl = b.total_latency_s
        k = K_HUMAN_APPROACH if b.human_approach else 0.0
        moving = v * tl + v * v / (2.0 * b.a_brake_mps2) + k * (tl + v / b.a_brake_mps2)
        return np.where(v > 0.0, moving + b.z_s_m + b.margin_m, b.z_s_m + b.margin_m)


def _elapsed_while(active: np.ndarray, t: np.ndarray) -> np.ndarray:
    """Sample-and-hold duration of each active interval, counted to the end
    of the current sample. Counting only to its start would read one control
    period short, which is the optimistic direction."""
    n = t.size
    out = np.zeros(n)
    if n == 0:
        return out
    step = np.empty(n)
    step[:-1] = np.diff(t)
    step[-1] = step[-2] if n > 1 else 0.0
    start = None
    for i in range(n):
        if active[i]:
            if start is None:
                start = t[i]
            out[i] = t[i] - start + step[i]
        else:
            start = None
    return out


__all__ = ["DerivedSignals"]
