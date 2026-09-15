"""
safegate.orchestrator.campaign
==============================

Executes a project's verification obligations and records the result as
signed evidence.

Per test case the campaign runs three phases, in this order, because each
phase informs the next:

  Phase 1  BOUNDARY   the ODD corners plus nominal. Cheap; catches the
                      obvious design errors before spending compute.
  Phase 2  SWEEP      Sobol / low-discrepancy coverage of the space. This
                      is what the coverage claim rests on.
  Phase 3  FALSIFY    robustness-guided simulated annealing, seeded from
                      the worst point found in phases 1-2. This is the
                      phase that finds the corner a sweep would need 10^6
                      samples to hit.

Two invariants the orchestrator enforces and will not let a caller
override:

  I1  Every executed run is recorded, including failures, errors and
      runs that were cut short. Selective recording is the failure mode
      that turns a safety tool into a liability. The manifest chain makes
      omission detectable; this code makes it not happen in the first
      place.

  I2  Non-deterministic backends get repeat execution. If an adapter
      declares `deterministic = False`, each concrete point is executed
      `repeats` times with identical pinning. Disagreement in verdict, or
      robustness spread beyond `flaky_tolerance`, yields FLAKY — which the
      policy engine treats as a blocker, not as a pass. A safety test that
      does not reproduce has not verified anything.
"""

from __future__ import annotations

import statistics
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from ..core.cas import EvidenceStore
from ..core.ids import digest
from ..core.model import (
    ConcreteRun,
    ExecutionTier,
    Pinning,
    Project,
    RunResult,
    TestCase,
    Verdict,
)
from ..execution.adapter import RunnerAdapter
from ..scenario.samplers import (
    BoundarySampler,
    ParameterSpace,
    RobustnessGuidedSampler,
    SobolSampler,
    space_coverage,
)
from ..stl.parser import parse_stl
from ..stl.robustness import Formula, Trace


@dataclass
class CampaignConfig:
    campaign_id: str
    sut_build_hash: str
    config_hash: str
    boundary_budget: int = 24
    sweep_budget: int = 128
    falsify_budget: int = 64
    seed: int = 20260914
    repeats_nondeterministic: int = 3
    flaky_tolerance: float = 1e-3
    store_traces: bool = True
    trace_decimation: int = 10  # store every Nth sample; full trace on failure


@dataclass
class TestCaseOutcome:
    test_case_ref: str
    runs: list[tuple[ConcreteRun, RunResult]] = field(default_factory=list)
    worst_robustness: float = float("inf")
    worst_assignment: dict[str, float] | None = None
    falsified: bool = False
    coverage: dict[str, float] = field(default_factory=dict)
    n_executed: int = 0
    wall_time_s: float = 0.0

    @property
    def verdict(self) -> Verdict:
        if any(r.verdict is Verdict.FLAKY for _, r in self.runs):
            return Verdict.FLAKY
        if self.falsified:
            return Verdict.FAIL
        if not self.runs:
            return Verdict.SKIPPED
        if all(r.verdict is Verdict.ERROR for _, r in self.runs):
            return Verdict.ERROR
        return Verdict.PASS


@dataclass
class CampaignResult:
    config: CampaignConfig
    outcomes: dict[str, TestCaseOutcome] = field(default_factory=dict)
    merkle_root: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def failed(self) -> list[str]:
        return [
            ref
            for ref, o in self.outcomes.items()
            if o.verdict in (Verdict.FAIL, Verdict.FLAKY, Verdict.ERROR)
        ]


class Campaign:
    def __init__(
        self,
        project: Project,
        runner: RunnerAdapter,
        store: EvidenceStore,
        config: CampaignConfig,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.project = project
        self.runner = runner
        self.store = store
        self.config = config
        self._log = progress or (lambda _m: None)
        self._formula_cache: dict[str, Formula] = {}

    # ---- helpers --------------------------------------------------------

    def _formula(self, src: str) -> Formula:
        if src not in self._formula_cache:
            self._formula_cache[src] = parse_stl(src)
        return self._formula_cache[src]

    def _pinning(self, tc: TestCase, assignment: dict[str, float], seed: int) -> Pinning:
        return Pinning(
            scenario_hash=digest(
                {"tc": tc.ref, "template": tc.scenario_template, "stl": tc.criterion_stl}
            ),
            sut_build_hash=self.config.sut_build_hash,
            backend_hash=self.runner.backend_hash(),
            config_hash=self.config.config_hash,
            seed=seed,
        )

    def _store_trace(self, trace: Trace, full: bool) -> str:
        step = 1 if full else max(1, self.config.trace_decimation)
        payload = {
            "time": trace.time[::step].tolist(),
            "signals": {
                k: v[::step].tolist()
                for k, v in trace.signals.items()
                if not k.startswith("__")
            },
            "decimation": step,
        }
        return self.store.put_json(payload)

    # ---- single execution ----------------------------------------------

    def _execute_once(
        self, tc: TestCase, assignment: dict[str, float], seed: int, tier: ExecutionTier
    ) -> tuple[ConcreteRun, RunResult]:
        run = ConcreteRun(
            test_case_ref=tc.ref,
            assignment=assignment,
            tier=tier,
            pinning=self._pinning(tc, assignment, seed),
        )
        t0 = time.perf_counter()
        with tempfile.TemporaryDirectory() as td:
            outcome = self.runner.execute(run, Path(td))
        dt = time.perf_counter() - t0

        if not outcome.ok or outcome.trace is None:
            res = RunResult(
                run_id=run.id,
                verdict=Verdict.ERROR,
                robustness=None,
                duration_s=dt,
                message=outcome.message[:1000],
            )
            return run, res

        formula = self._formula(tc.criterion_stl)
        env = dict(assignment)
        try:
            rho = formula.evaluate(outcome.trace, env)
        except Exception as exc:  # a missing signal is an infra error, not a FAIL
            res = RunResult(
                run_id=run.id,
                verdict=Verdict.ERROR,
                duration_s=dt,
                message=f"STL evaluation failed: {exc}",
            )
            return run, res

        verdict = Verdict.PASS if rho >= 0 else Verdict.FAIL
        artifacts: dict[str, str] = {}
        if self.config.store_traces:
            # Full resolution on failure: that is the trace an engineer
            # will actually open.
            artifacts["trace"] = self._store_trace(
                outcome.trace, full=(verdict is Verdict.FAIL)
            )
        metrics = {
            "min_speed": float(outcome.trace.get("speed").min())
            if "speed" in outcome.trace.signals
            else 0.0,
            "min_distance": float(outcome.trace.get("min_distance_to_person").min())
            if "min_distance_to_person" in outcome.trace.signals
            else 0.0,
        }
        res = RunResult(
            run_id=run.id,
            verdict=verdict,
            robustness=rho,
            duration_s=dt,
            artifacts=artifacts,
            metrics=metrics,
        )
        return run, res

    def _execute(
        self, tc: TestCase, assignment: dict[str, float], seed: int, tier: ExecutionTier
    ) -> tuple[ConcreteRun, RunResult]:
        """Execute with the determinism guard applied."""
        run, res = self._execute_once(tc, assignment, seed, tier)
        if self.runner.deterministic or res.verdict is Verdict.ERROR:
            self._record(run, res)
            return run, res

        rhos = [res.robustness] if res.robustness is not None else []
        verdicts = [res.verdict]
        extras: list[tuple[ConcreteRun, RunResult]] = []
        for _ in range(self.config.repeats_nondeterministic - 1):
            r2, s2 = self._execute_once(tc, assignment, seed, tier)
            extras.append((r2, s2))
            verdicts.append(s2.verdict)
            if s2.robustness is not None:
                rhos.append(s2.robustness)

        spread = (max(rhos) - min(rhos)) if len(rhos) > 1 else 0.0
        inconsistent = len(set(verdicts)) > 1 or spread > self.config.flaky_tolerance
        if inconsistent:
            res = RunResult(
                run_id=run.id,
                verdict=Verdict.FLAKY,
                robustness=min(rhos) if rhos else None,
                duration_s=res.duration_s,
                artifacts=res.artifacts,
                metrics={"robustness_spread": spread},
                message=(
                    f"non-reproducible under identical pinning: verdicts="
                    f"{[v.value for v in verdicts]}, robustness spread={spread:.6g}"
                ),
            )
        self._record(run, res)
        for r2, s2 in extras:
            self._record(r2, s2)
        return run, res

    def _record(self, run: ConcreteRun, res: RunResult) -> None:
        """Invariant I1: everything executed is recorded, no exceptions."""
        self.store.append(
            {
                "campaign_id": self.config.campaign_id,
                "type": "run",
                "run": run.model_dump(mode="json"),
                "result": res.model_dump(mode="json"),
            }
        )

    # ---- per-test-case --------------------------------------------------

    def run_test_case(self, tc: TestCase) -> TestCaseOutcome:
        space = ParameterSpace(list(tc.parameter_space))
        out = TestCaseOutcome(test_case_ref=tc.ref)
        t0 = time.perf_counter()
        seen: list[dict[str, float]] = []
        seed_base = self.config.seed ^ (hash(tc.ref) & 0xFFFFFFFF)

        def note(run: ConcreteRun, res: RunResult) -> None:
            out.runs.append((run, res))
            out.n_executed += 1
            if res.robustness is not None and res.robustness < out.worst_robustness:
                out.worst_robustness = res.robustness
                out.worst_assignment = dict(run.assignment)
            if res.verdict is Verdict.FAIL:
                out.falsified = True

        # Phase 1 — boundary
        self._log(f"  {tc.ref}: boundary")
        for i, a in enumerate(
            BoundarySampler().sample(space, self.config.boundary_budget, seed_base)
        ):
            seen.append(a)
            note(*self._execute(tc, a, seed_base + i, tc.required_tier))

        # Phase 2 — sweep
        self._log(f"  {tc.ref}: sweep")
        for i, a in enumerate(
            SobolSampler().sample(space, self.config.sweep_budget, seed_base + 1)
        ):
            seen.append(a)
            note(*self._execute(tc, a, seed_base + 1000 + i, tc.required_tier))

        # Phase 3 — falsification
        if not out.falsified and self.config.falsify_budget > 0:
            self._log(f"  {tc.ref}: falsify")
            counter = {"n": 0}

            def objective(a: dict[str, float]) -> float:
                counter["n"] += 1
                seen.append(a)
                run, res = self._execute(
                    tc, a, seed_base + 5000 + counter["n"], tc.required_tier
                )
                note(run, res)
                return res.robustness if res.robustness is not None else float("inf")

            RobustnessGuidedSampler().search(
                space, objective, self.config.falsify_budget, seed_base + 2
            )

        out.coverage = space_coverage(space, seen)
        out.wall_time_s = time.perf_counter() - t0
        self.store.append(
            {
                "campaign_id": self.config.campaign_id,
                "type": "test_case_summary",
                "test_case_ref": tc.ref,
                "verdict": out.verdict.value,
                "worst_robustness": out.worst_robustness,
                "worst_assignment": out.worst_assignment,
                "n_executed": out.n_executed,
                "coverage": out.coverage,
            }
        )
        return out

    # ---- entry point ----------------------------------------------------

    def run(self, only: Sequence[str] | None = None) -> CampaignResult:
        result = CampaignResult(config=self.config, started_at=time.time())
        self.store.append(
            {
                "campaign_id": self.config.campaign_id,
                "type": "campaign_start",
                "project": self.project.name,
                "runner": self.runner.name,
                "tier": self.runner.tier.value,
                "deterministic": self.runner.deterministic,
                "sut_build_hash": self.config.sut_build_hash,
                "config_hash": self.config.config_hash,
                "backend_hash": self.runner.backend_hash(),
            }
        )
        for tc in self.project.test_cases:
            if only and tc.ref not in only:
                continue
            self._log(f"[{tc.ref}] {tc.title}")
            result.outcomes[tc.ref] = self.run_test_case(tc)

        result.finished_at = time.time()
        self.store.append(
            {
                "campaign_id": self.config.campaign_id,
                "type": "campaign_end",
                "failed": result.failed,
                "n_test_cases": len(result.outcomes),
            }
        )
        result.merkle_root = self.store.campaign_root(self.config.campaign_id)
        return result


__all__ = ["Campaign", "CampaignConfig", "CampaignResult", "TestCaseOutcome"]
