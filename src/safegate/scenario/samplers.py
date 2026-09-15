"""
safegate.scenario.samplers
==========================

Turning an abstract scenario (a parameter *space*) into concrete runs.

Four samplers are provided. The campaign uses the last three, in order
(boundary, sweep, falsification); the grid sampler is available for spaces
small enough to enumerate:

  1. GridSampler       full factorial. Gives exhaustive-coverage claims
                       for the dimensions where exhaustiveness is actually
                       achievable.
  2. SobolSampler      scrambled Sobol low-discrepancy sequence (SciPy),
                       with a Latin hypercube fallback when SciPy is absent.
                       Covers a continuous box more evenly than uniform
                       random at the same budget. This is the broad sweep.
  3. BoundarySampler   nominal point, box corners (capped by the budget)
                       and face centres. Defects concentrate at the
                       extremes of the operational design domain, and
                       corners are cheap.
  4. RobustnessGuided  adaptive falsification: simulated annealing with
                       restarts on the STL robustness value. It converts
                       "we sampled N points and all passed" into "we
                       searched for a counterexample and the smallest
                       margin the search found was X".

The honest limitation, stated up front because a safety tool that
oversells its coverage is dangerous: none of these prove absence of
violations. They are falsification, not verification. The technical file
says so.
"""

from __future__ import annotations

import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import numpy as np

from ..core.model import ParameterRange

# --------------------------------------------------------------------------
# Space
# --------------------------------------------------------------------------


@dataclass
class ParameterSpace:
    dims: list[ParameterRange]

    @property
    def names(self) -> list[str]:
        return [d.name for d in self.dims]

    @property
    def continuous(self) -> list[ParameterRange]:
        return [d for d in self.dims if not d.is_discrete()]

    def cardinality(self) -> float:
        """Number of points for a full factorial, or inf if continuous."""
        n = 1.0
        for d in self.dims:
            if d.is_discrete():
                n *= len(d.values or [])
            else:
                return math.inf
        return n

    def clip(self, assignment: dict[str, float]) -> dict[str, float]:
        out = dict(assignment)
        for d in self.dims:
            if d.is_discrete() and d.values:
                out[d.name] = min(d.values, key=lambda v: abs(v - out[d.name]))
            else:
                lo = d.low if d.low is not None else -math.inf
                hi = d.high if d.high is not None else math.inf
                out[d.name] = float(min(max(out[d.name], lo), hi))
        return out

    def nominal(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for d in self.dims:
            if d.is_discrete() and d.values:
                out[d.name] = d.values[len(d.values) // 2]
            else:
                lo = d.low if d.low is not None else 0.0
                hi = d.high if d.high is not None else 1.0
                out[d.name] = (lo + hi) / 2.0
        return out


class Sampler(Protocol):
    name: str

    def sample(
        self, space: ParameterSpace, budget: int, seed: int
    ) -> list[dict[str, float]]: ...


# --------------------------------------------------------------------------
# Static samplers
# --------------------------------------------------------------------------


@dataclass
class GridSampler:
    name: str = "grid"
    points_per_continuous_dim: int = 3

    def sample(
        self, space: ParameterSpace, budget: int, seed: int
    ) -> list[dict[str, float]]:
        axes: list[list[float]] = []
        for d in space.dims:
            if d.is_discrete() and d.values:
                axes.append(list(d.values))
            else:
                lo = d.low if d.low is not None else 0.0
                hi = d.high if d.high is not None else 1.0
                k = max(2, self.points_per_continuous_dim)
                axes.append([lo + (hi - lo) * i / (k - 1) for i in range(k)])
        out: list[dict[str, float]] = []
        idx = [0] * len(axes)
        while len(out) < budget:
            out.append({d.name: axes[i][idx[i]] for i, d in enumerate(space.dims)})
            pos = len(axes) - 1
            while pos >= 0:
                idx[pos] += 1
                if idx[pos] < len(axes[pos]):
                    break
                idx[pos] = 0
                pos -= 1
            if pos < 0:
                break
        return out


@dataclass
class SobolSampler:
    """Scrambled Sobol sequence. Falls back to stratified random if SciPy
    is absent, so the package has no hard SciPy dependency."""

    name: str = "sobol"

    def sample(
        self, space: ParameterSpace, budget: int, seed: int
    ) -> list[dict[str, float]]:
        k = len(space.dims)
        if k == 0 or budget <= 0:
            return []
        try:
            from scipy.stats import qmc  # type: ignore

            m = max(1, math.ceil(math.log2(max(budget, 2))))
            pts = qmc.Sobol(d=k, scramble=True, seed=seed).random_base2(m)[:budget]
        except ImportError:
            rng = np.random.default_rng(seed)
            # Latin hypercube fallback: better than plain uniform.
            pts = np.empty((budget, k))
            for j in range(k):
                perm = rng.permutation(budget)
                pts[:, j] = (perm + rng.random(budget)) / budget
        out: list[dict[str, float]] = []
        for row in pts:
            a: dict[str, float] = {}
            for j, d in enumerate(space.dims):
                u = float(row[j])
                if d.is_discrete() and d.values:
                    a[d.name] = d.values[min(int(u * len(d.values)), len(d.values) - 1)]
                else:
                    lo = d.low if d.low is not None else 0.0
                    hi = d.high if d.high is not None else 1.0
                    a[d.name] = lo + u * (hi - lo)
            out.append(a)
        return out


@dataclass
class BoundarySampler:
    """Corners, face centres, and the nominal point of the ODD box."""

    name: str = "boundary"

    def sample(
        self, space: ParameterSpace, budget: int, seed: int
    ) -> list[dict[str, float]]:
        dims = space.dims
        k = len(dims)
        out: list[dict[str, float]] = [space.nominal()]

        def bounds(d: ParameterRange) -> tuple[float, float]:
            if d.is_discrete() and d.values:
                return min(d.values), max(d.values)
            return (
                d.low if d.low is not None else 0.0,
                d.high if d.high is not None else 1.0,
            )

        # corners (capped so k=20 does not explode)
        n_corners = min(1 << k, max(0, budget - 1 - 2 * k))
        for mask in range(n_corners):
            a = {}
            for j, d in enumerate(dims):
                lo, hi = bounds(d)
                a[d.name] = hi if (mask >> j) & 1 else lo
            out.append(a)
        # face centres
        for d in dims:
            for extreme in bounds(d):
                a = space.nominal()
                a[d.name] = extreme
                out.append(a)
        seen: set[tuple] = set()
        uniq: list[dict[str, float]] = []
        for a in out:
            key = tuple(sorted(a.items()))
            if key not in seen:
                seen.add(key)
                uniq.append(a)
        return uniq[:budget]


# --------------------------------------------------------------------------
# Adaptive falsification
# --------------------------------------------------------------------------


@dataclass
class FalsificationTrace:
    """The search history. Goes in the report: an assessor should see how
    hard you looked, not just what you found."""

    evaluations: list[tuple[dict[str, float], float]] = field(default_factory=list)
    best_assignment: dict[str, float] | None = None
    best_robustness: float = math.inf
    falsified: bool = False

    def record(self, a: dict[str, float], rho: float) -> None:
        self.evaluations.append((a, rho))
        if rho < self.best_robustness:
            self.best_robustness = rho
            self.best_assignment = dict(a)
        if rho < 0:
            self.falsified = True


@dataclass
class RobustnessGuidedSampler:
    """Simulated annealing on the STL robustness value.

    `objective(assignment) -> rho`. The search minimises rho; the first
    negative value is a counterexample and the search stops (early exit is
    correct here: one counterexample is enough to fail the gate, and
    compute is better spent on the next requirement).

    Restarts guard against the classic failure of annealing on a
    multi-modal robustness landscape, which AMR scenarios reliably have
    because of discrete mode switches (speed zones, field switching).
    """

    name: str = "falsify"
    restarts: int = 3
    temperature0: float = 1.0
    cooling: float = 0.93
    step_frac: float = 0.25
    stop_on_falsify: bool = True

    def search(
        self,
        space: ParameterSpace,
        objective: Callable[[dict[str, float]], float],
        budget: int,
        seed: int,
    ) -> FalsificationTrace:
        rng = random.Random(seed)
        tr = FalsificationTrace()
        per_restart = max(1, budget // max(1, self.restarts))

        def span(d: ParameterRange) -> float:
            if d.is_discrete() and d.values:
                return (max(d.values) - min(d.values)) or 1.0
            lo = d.low if d.low is not None else 0.0
            hi = d.high if d.high is not None else 1.0
            return (hi - lo) or 1.0

        for r in range(self.restarts):
            if tr.falsified and self.stop_on_falsify:
                break
            # Restart 0 starts at nominal; later restarts start random, so
            # the first evaluation is always the most defensible point.
            if r == 0:
                cur = space.nominal()
            else:
                cur = space.clip(
                    {
                        d.name: (
                            rng.choice(d.values)
                            if (d.is_discrete() and d.values)
                            else rng.uniform(
                                d.low if d.low is not None else 0.0,
                                d.high if d.high is not None else 1.0,
                            )
                        )
                        for d in space.dims
                    }
                )
            cur_rho = objective(cur)
            tr.record(cur, cur_rho)
            if cur_rho < 0 and self.stop_on_falsify:
                break

            temp = self.temperature0
            for _ in range(per_restart - 1):
                cand = dict(cur)
                for d in space.dims:
                    if rng.random() < 0.5:
                        continue
                    cand[d.name] = cur[d.name] + rng.gauss(
                        0.0, self.step_frac * span(d) * temp
                    )
                cand = space.clip(cand)
                rho = objective(cand)
                tr.record(cand, rho)
                if rho < 0 and self.stop_on_falsify:
                    return tr
                delta = rho - cur_rho
                # Metropolis: always accept improvement, sometimes accept
                # a worsening step, scaled by temperature.
                if delta < 0 or rng.random() < math.exp(-delta / max(temp, 1e-9)):
                    cur, cur_rho = cand, rho
                temp *= self.cooling
        return tr


# --------------------------------------------------------------------------
# Coverage
# --------------------------------------------------------------------------


def space_coverage(
    space: ParameterSpace, samples: Sequence[dict[str, float]], bins: int = 5
) -> dict[str, float]:
    """Fraction of per-dimension bins touched, plus a joint 2-way measure.

    Reported because "we ran 10,000 tests" is not a coverage claim.
    Combinatorial 2-way (pairwise) coverage is the pragmatic middle ground
    between 1-way (useless) and full factorial (impossible).
    """
    if not samples:
        return {"one_way": 0.0, "two_way": 0.0}

    def bin_of(d: ParameterRange, v: float) -> int:
        if d.is_discrete() and d.values:
            return d.values.index(min(d.values, key=lambda x: abs(x - v)))
        lo = d.low if d.low is not None else 0.0
        hi = d.high if d.high is not None else 1.0
        if hi <= lo:
            return 0
        return min(bins - 1, int((v - lo) / (hi - lo) * bins))

    one_hit: dict[str, set[int]] = {d.name: set() for d in space.dims}
    two_hit: set[tuple[str, int, str, int]] = set()
    for a in samples:
        bs = {d.name: bin_of(d, a.get(d.name, 0.0)) for d in space.dims}
        for k, v in bs.items():
            one_hit[k].add(v)
        names = sorted(bs)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                two_hit.add((names[i], bs[names[i]], names[j], bs[names[j]]))

    def nbins(d: ParameterRange) -> int:
        return len(d.values) if (d.is_discrete() and d.values) else bins

    one_total = sum(nbins(d) for d in space.dims) or 1
    one_cov = sum(len(v) for v in one_hit.values()) / one_total
    two_total = 0
    for i, di in enumerate(space.dims):
        for dj in space.dims[i + 1 :]:
            two_total += nbins(di) * nbins(dj)
    two_cov = (len(two_hit) / two_total) if two_total else 1.0
    return {"one_way": one_cov, "two_way": two_cov}


__all__ = [
    "BoundarySampler",
    "FalsificationTrace",
    "GridSampler",
    "ParameterSpace",
    "RobustnessGuidedSampler",
    "Sampler",
    "SobolSampler",
    "space_coverage",
]
