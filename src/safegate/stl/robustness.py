"""
safegate.stl.robustness
=======================

Signal Temporal Logic with quantitative (robustness) semantics.

Why STL and not assertions
--------------------------
A boolean assertion answers "did it pass?". For a safety argument that is
almost useless, because it tells you nothing about *margin*. STL's
quantitative semantics returns a real number rho:

    rho > 0  -> satisfied, and rho is the distance to violation
    rho < 0  -> violated, and |rho| is the depth of the violation
    rho = 0  -> on the boundary

Three things fall out of that, all of which matter commercially:

  1. Falsification becomes optimisation. Minimising rho over the scenario
     parameter space is a search for the worst case. This is how you find
     the 1-in-10^6 corner without running 10^6 tests. (See
     scenario/samplers.py: RobustnessGuidedSampler.)
  2. Margin is reportable. "The protective-field requirement held with a
     minimum margin of 38 mm across 12,400 runs" is an argument an
     assessor can weigh. "1240/1240 passed" is not.
  3. Regression detection gets sensitive. A commit that cuts margin from
     380 mm to 40 mm passes every boolean test and is a serious
     regression. The gate can trip on margin deltas.

Semantics implemented (Donzé & Maler, and Fainekos & Pappas):

    rho(mu, s, t)            = f_mu(s(t))
    rho(not p, s, t)         = -rho(p, s, t)
    rho(p and q, s, t)       = min(rho(p,s,t), rho(q,s,t))
    rho(p or q, s, t)        = max(rho(p,s,t), rho(q,s,t))
    rho(G_[a,b] p, s, t)     = inf_{t' in [t+a, t+b]} rho(p, s, t')
    rho(F_[a,b] p, s, t)     = sup_{t' in [t+a, t+b]} rho(p, s, t')
    rho(p U_[a,b] q, s, t)   = sup_{t' in [t+a,t+b]} min( rho(q,s,t'),
                                   inf_{t'' in [t,t']} rho(p,s,t'') )

Signals are piecewise-constant over a sampled time grid, which is the
honest model for ROS topic data. Interval operators use a monotonic-wedge
(Lemire) sliding-window min/max, giving O(n) per temporal operator rather
than the O(n*w) of the naive implementation — this matters when a
campaign evaluates 10^5 traces of 10^4 samples in CI.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

import numpy as np

# --------------------------------------------------------------------------
# Traces
# --------------------------------------------------------------------------


@dataclass
class Trace:
    """A multi-variate, uniformly-or-irregularly sampled signal bundle."""

    time: np.ndarray  # shape (n,), strictly increasing, seconds
    signals: dict[str, np.ndarray]  # each shape (n,)

    def __post_init__(self) -> None:
        self.time = np.asarray(self.time, dtype=float)
        if self.time.ndim != 1:
            raise ValueError("time must be 1-D")
        if np.any(np.diff(self.time) <= 0):
            raise ValueError("time must be strictly increasing")
        for k, v in list(self.signals.items()):
            arr = np.asarray(v, dtype=float)
            if arr.shape != self.time.shape:
                raise ValueError(
                    f"signal {k!r} has shape {arr.shape}, expected {self.time.shape}"
                )
            self.signals[k] = arr

    @property
    def n(self) -> int:
        return self.time.size

    def get(self, name: str) -> np.ndarray:
        if name not in self.signals:
            raise KeyError(
                f"signal {name!r} not in trace; available: {sorted(self.signals)}"
            )
        return self.signals[name]


# --------------------------------------------------------------------------
# Sliding-window extrema (Lemire's monotonic wedge), O(n)
# --------------------------------------------------------------------------


def _window_extremum(
    time: np.ndarray, values: np.ndarray, a: float, b: float, want_min: bool
) -> np.ndarray:
    """For each index i, the extremum of `values` over t in [t_i+a, t_i+b].

    Samples beyond the end of the trace are treated as *absent*, and the
    window shrinks. If the window is entirely outside the trace the result
    is +inf (for min) / -inf (for max), i.e. vacuous truth for `always`
    and vacuous falsity for `eventually` — the standard convention, and
    the conservative one for safety: `always` over an empty window must
    not manufacture a violation.
    """
    n = time.size
    out = np.empty(n, dtype=float)
    dq: deque[int] = deque()
    j_start = 0
    j_end = 0  # exclusive

    def better(x: float, y: float) -> bool:
        return x <= y if want_min else x >= y

    for i in range(n):
        lo = time[i] + a
        hi = time[i] + b
        # extend right edge
        while j_end < n and time[j_end] <= hi:
            v = values[j_end]
            while dq and better(v, values[dq[-1]]):
                dq.pop()
            dq.append(j_end)
            j_end += 1
        # retract left edge
        while j_start < j_end and time[j_start] < lo:
            if dq and dq[0] == j_start:
                dq.popleft()
            j_start += 1
        if dq:
            out[i] = values[dq[0]]
        else:
            out[i] = math.inf if want_min else -math.inf
    return out


# --------------------------------------------------------------------------
# AST
# --------------------------------------------------------------------------


class Formula:
    """Base class. `rho` returns the robustness signal, one value per sample."""

    def rho(self, trace: Trace, env: Mapping[str, float]) -> np.ndarray:
        raise NotImplementedError

    def evaluate(self, trace: Trace, env: Mapping[str, float] | None = None) -> float:
        """Robustness at t=0 — the value the gate and the sampler consume."""
        return float(self.rho(trace, env or {})[0])

    def signals_used(self) -> set[str]:
        return set()

    def __and__(self, other: "Formula") -> "And":
        return And(self, other)

    def __or__(self, other: "Formula") -> "Or":
        return Or(self, other)

    def __invert__(self) -> "Not":
        return Not(self)


@dataclass
class Const(Formula):
    value: float

    def rho(self, trace: Trace, env: Mapping[str, float]) -> np.ndarray:
        return np.full(trace.n, float(self.value))


@dataclass
class Predicate(Formula):
    """`expr(signals, env) >= 0` — robustness IS the expression value.

    Encoding the predicate as a margin rather than a comparison is the
    whole trick. `dist >= d_min` becomes `dist - d_min`, whose value in
    metres is directly meaningful to a safety engineer.
    """

    name: str
    fn: Callable[[Mapping[str, np.ndarray], Mapping[str, float]], np.ndarray]
    uses: set[str] = field(default_factory=set)

    def rho(self, trace: Trace, env: Mapping[str, float]) -> np.ndarray:
        out = np.asarray(self.fn(trace.signals, env), dtype=float)
        if out.shape != trace.time.shape:
            out = np.full(trace.n, float(out))
        return out

    def signals_used(self) -> set[str]:
        return set(self.uses)


@dataclass
class Comparison(Formula):
    """`lhs <op> rhs` where each side is a signal name, env key, or number."""

    lhs: str | float
    op: str  # ">=", ">", "<=", "<"
    rhs: str | float
    scale: float = 1.0

    def _resolve(
        self, side: str | float, trace: Trace, env: Mapping[str, float]
    ) -> np.ndarray:
        if isinstance(side, (int, float)):
            return np.full(trace.n, float(side))
        if side in trace.signals:
            return trace.get(side)
        if side in env:
            return np.full(trace.n, float(env[side]))
        raise KeyError(f"{side!r} is neither a signal nor a parameter")

    def rho(self, trace: Trace, env: Mapping[str, float]) -> np.ndarray:
        l = self._resolve(self.lhs, trace, env)
        r = self._resolve(self.rhs, trace, env)
        if self.op in (">=", ">"):
            return (l - r) * self.scale
        if self.op in ("<=", "<"):
            return (r - l) * self.scale
        raise ValueError(f"unsupported operator {self.op!r}")

    def signals_used(self) -> set[str]:
        return {s for s in (self.lhs, self.rhs) if isinstance(s, str)}


@dataclass
class Not(Formula):
    inner: Formula

    def rho(self, trace: Trace, env: Mapping[str, float]) -> np.ndarray:
        return -self.inner.rho(trace, env)

    def signals_used(self) -> set[str]:
        return self.inner.signals_used()


@dataclass
class And(Formula):
    left: Formula
    right: Formula

    def rho(self, trace: Trace, env: Mapping[str, float]) -> np.ndarray:
        return np.minimum(self.left.rho(trace, env), self.right.rho(trace, env))

    def signals_used(self) -> set[str]:
        return self.left.signals_used() | self.right.signals_used()


@dataclass
class Or(Formula):
    left: Formula
    right: Formula

    def rho(self, trace: Trace, env: Mapping[str, float]) -> np.ndarray:
        return np.maximum(self.left.rho(trace, env), self.right.rho(trace, env))

    def signals_used(self) -> set[str]:
        return self.left.signals_used() | self.right.signals_used()


@dataclass
class Implies(Formula):
    left: Formula
    right: Formula

    def rho(self, trace: Trace, env: Mapping[str, float]) -> np.ndarray:
        return np.maximum(-self.left.rho(trace, env), self.right.rho(trace, env))

    def signals_used(self) -> set[str]:
        return self.left.signals_used() | self.right.signals_used()


@dataclass
class Always(Formula):
    """G_[a,b] phi"""

    inner: Formula
    a: float = 0.0
    b: float = math.inf

    def rho(self, trace: Trace, env: Mapping[str, float]) -> np.ndarray:
        inner = self.inner.rho(trace, env)
        b = self.b if math.isfinite(self.b) else float(trace.time[-1] - trace.time[0])
        return _window_extremum(trace.time, inner, self.a, b, want_min=True)

    def signals_used(self) -> set[str]:
        return self.inner.signals_used()


@dataclass
class Eventually(Formula):
    """F_[a,b] phi"""

    inner: Formula
    a: float = 0.0
    b: float = math.inf

    def rho(self, trace: Trace, env: Mapping[str, float]) -> np.ndarray:
        inner = self.inner.rho(trace, env)
        b = self.b if math.isfinite(self.b) else float(trace.time[-1] - trace.time[0])
        return _window_extremum(trace.time, inner, self.a, b, want_min=False)

    def signals_used(self) -> set[str]:
        return self.inner.signals_used()


@dataclass
class Until(Formula):
    """phi U_[a,b] psi"""

    left: Formula
    right: Formula
    a: float = 0.0
    b: float = math.inf

    def rho(self, trace: Trace, env: Mapping[str, float]) -> np.ndarray:
        t = trace.time
        n = t.size
        l = self.left.rho(trace, env)
        r = self.right.rho(trace, env)
        b = self.b if math.isfinite(self.b) else float(t[-1] - t[0])
        out = np.full(n, -math.inf)
        # O(n^2) worst case; acceptable because Until is rare in safety
        # requirements and n is bounded by the trace decimation applied in
        # the runner. Documented rather than hidden.
        for i in range(n):
            lo, hi = t[i] + self.a, t[i] + b
            running_left = math.inf
            best = -math.inf
            for j in range(i, n):
                if t[j] > hi:
                    break
                running_left = min(running_left, l[j])
                if t[j] >= lo:
                    best = max(best, min(r[j], running_left))
            out[i] = best
        return out

    def signals_used(self) -> set[str]:
        return self.left.signals_used() | self.right.signals_used()


@dataclass
class Once(Formula):
    """Past-time O_[a,b] phi — needed for 'the estop was pressed at some
    point in the last 200 ms', which is how latency requirements are
    actually phrased."""

    inner: Formula
    a: float = 0.0
    b: float = math.inf

    def rho(self, trace: Trace, env: Mapping[str, float]) -> np.ndarray:
        inner = self.inner.rho(trace, env)
        b = self.b if math.isfinite(self.b) else float(trace.time[-1] - trace.time[0])
        return _window_extremum(trace.time, inner, -b, -self.a, want_min=False)

    def signals_used(self) -> set[str]:
        return self.inner.signals_used()


# --------------------------------------------------------------------------
# Convenience constructors used by the standards libraries
# --------------------------------------------------------------------------


def always(f: Formula, a: float = 0.0, b: float = math.inf) -> Always:
    return Always(f, a, b)


def eventually(f: Formula, a: float = 0.0, b: float = math.inf) -> Eventually:
    return Eventually(f, a, b)


def geq(lhs: str | float, rhs: str | float) -> Comparison:
    return Comparison(lhs, ">=", rhs)


def leq(lhs: str | float, rhs: str | float) -> Comparison:
    return Comparison(lhs, "<=", rhs)


def implies(p: Formula, q: Formula) -> Implies:
    return Implies(p, q)


__all__ = [
    "Always",
    "And",
    "Comparison",
    "Const",
    "Eventually",
    "Formula",
    "Implies",
    "Not",
    "Once",
    "Or",
    "Predicate",
    "Trace",
    "Until",
    "always",
    "eventually",
    "geq",
    "implies",
    "leq",
]
