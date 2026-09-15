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
  Phase 3  FALSIFY    robustness-guided simulated annealing. This is the
                      phase that finds a corner a sweep would need far more
                      samples to hit. It is skipped once phases 1-2 already
                      produced a counterexample: one is enough to fail the
                      gate, and the compute is better spent elsewhere.

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
      robustness spread beyond `flaky_tolerance`, yields FLAKY - which the
      policy engine treats as a blocker, not as a pass. A safety test that
      does not reproduce has not verified anything.

Reproducibility across processes: every seed is derived from the campaign
seed and a SHA-256 of the test-case reference. Python's built-in `hash()`
is salted per process and must never feed a seed.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from pathlib import Path

from .. import __version__
from ..core.cas import EvidenceStore
from ..core.ids import digest
from ..core.model import (
    ConcreteRun,
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


def stable_u32(text: str) -> int:
    """Process-independent 32-bit integer from a string."""
    return int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:4], "big")


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
    workers: int = 1  # >1 executes boundary and sweep points in parallel


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

    __test__ = False  # not a pytest class, despite the name

    @property
    def n_errors(self) -> int:
        return sum(1 for _, r in self.runs if r.verdict is Verdict.ERROR)

    @property
    def verdict(self) -> Verdict:
        if any(r.verdict is Verdict.FLAKY for _, r in self.runs):
            return Verdict.FLAKY
        if self.falsified:
            return Verdict.FAIL
        if not self.runs:
            return Verdict.SKIPPED
        # Any errored run is a hole in the sampled space. Reporting PASS
        # over a space that was only partly executed overstates coverage.
        if self.n_errors:
            return Verdict.ERROR
        return Verdict.PASS

    def note(self, run: ConcreteRun, res: RunResult) -> None:
        self.runs.append((run, res))
        self.n_executed += 1
        if res.robustness is not None and res.robustness < self.worst_robustness:
            self.worst_robustness = res.robustness
            self.worst_assignment = dict(run.assignment)
        if res.verdict is Verdict.FAIL:
            self.falsified = True


@dataclass
class CampaignResult:
    config: CampaignConfig
    outcomes: dict[str, TestCaseOutcome] = field(default_factory=dict)
    merkle_root: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    runner: str = ""
    backend_hash: str = ""
    project_digest: str = ""
    # Set by load_campaign: False when the chain has no campaign_end entry,
    # i.e. the campaign was interrupted and its results are partial.
    complete: bool = True
    incomplete_test_cases: list[str] = field(default_factory=list)

    @property
    def failed(self) -> list[str]:
        return [
            ref
            for ref, o in self.outcomes.items()
            if o.verdict in (Verdict.FAIL, Verdict.FLAKY, Verdict.ERROR)
        ]


# --------------------------------------------------------------------------
# One execution. Module-level so a process pool can run it.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _ExecContext:
    runner: RunnerAdapter
    store_root: str
    store_traces: bool
    trace_decimation: int
    derive: Callable[[Trace], Trace] | None


_FORMULAS: dict[str, Formula] = {}
_WORKER_CTX: _ExecContext | None = None


def _formula(src: str) -> Formula:
    f = _FORMULAS.get(src)
    if f is None:
        f = _FORMULAS[src] = parse_stl(src)
    return f


_STORES: dict[tuple[int, str], EvidenceStore] = {}


def _worker_store(root: str) -> EvidenceStore:
    """One store (and one object-storage client) per process, not per run."""
    key = (os.getpid(), root)
    store = _STORES.get(key)
    if store is None:
        store = _STORES[key] = EvidenceStore(root)
    return store


def _store_trace(store: EvidenceStore, trace: Trace, step: int) -> str:
    payload = {
        "time": trace.time[::step].tolist(),
        "signals": {
            k: v[::step].tolist() for k, v in trace.signals.items() if not k.startswith("__")
        },
        "decimation": step,
    }
    return store.put_json(payload)


def execute_point(
    ctx: _ExecContext, tc: TestCase, run: ConcreteRun
) -> tuple[ConcreteRun, RunResult]:
    t0 = time.perf_counter()
    try:
        with tempfile.TemporaryDirectory() as td:
            outcome = ctx.runner.execute(run, Path(td))
    except Exception as exc:  # noqa: BLE001 - a crashing backend is an ERROR run, still recorded
        dt = time.perf_counter() - t0
        return run, RunResult(
            run_id=run.id,
            verdict=Verdict.ERROR,
            duration_s=dt,
            message=f"runner raised {type(exc).__name__}: {exc}"[:1000],
        )
    dt = time.perf_counter() - t0

    if not outcome.ok or outcome.trace is None:
        return run, RunResult(
            run_id=run.id,
            verdict=Verdict.ERROR,
            robustness=None,
            duration_s=dt,
            message=outcome.message[:1000],
        )

    env = dict(run.assignment)
    try:
        trace = ctx.derive(outcome.trace) if ctx.derive else outcome.trace
        rho = _formula(tc.criterion_stl).evaluate(trace, env)
    except Exception as exc:  # noqa: BLE001 - a missing signal is an infra error, not a FAIL
        return run, RunResult(
            run_id=run.id,
            verdict=Verdict.ERROR,
            duration_s=dt,
            message=f"STL evaluation failed: {exc}"[:1000],
        )

    verdict = Verdict.PASS if rho >= 0 else Verdict.FAIL
    artifacts: dict[str, str] = {}
    if ctx.store_traces:
        # Full resolution on failure: that is the trace an engineer will
        # actually open.
        step = 1 if verdict is Verdict.FAIL else max(1, ctx.trace_decimation)
        artifacts["trace"] = _store_trace(_worker_store(ctx.store_root), trace, step)
    metrics: dict[str, float] = {}
    if "speed" in trace.signals:
        metrics["max_speed"] = float(abs(trace.get("speed")).max())
    if "min_distance_to_person" in trace.signals:
        metrics["min_distance"] = float(trace.get("min_distance_to_person").min())
    metrics.update(outcome.metrics)
    return run, RunResult(
        run_id=run.id,
        verdict=verdict,
        robustness=rho,
        duration_s=dt,
        artifacts=artifacts,
        metrics=metrics,
        message=outcome.message[:1000],
    )


def _worker_init(ctx: _ExecContext) -> None:
    global _WORKER_CTX
    _WORKER_CTX = ctx


def _worker_execute(args: tuple[TestCase, ConcreteRun]) -> tuple[ConcreteRun, RunResult]:
    assert _WORKER_CTX is not None
    return execute_point(_WORKER_CTX, *args)


# --------------------------------------------------------------------------


class Campaign:
    """Runs a project's test cases against one runner into one store.

    `derive` adds derived signals to each trace before the STL criterion
    is evaluated (for example the required protective-field length from
    the stopping budget). It is injected by the caller so that this layer
    stays independent of any particular standard.
    """

    def __init__(
        self,
        project: Project,
        runner: RunnerAdapter,
        store: EvidenceStore,
        config: CampaignConfig,
        progress: Callable[[str], None] | None = None,
        derive: Callable[[Trace], Trace] | None = None,
    ) -> None:
        self.project = project
        self.runner = runner
        self.store = store
        self.config = config
        self._log = progress or (lambda _m: None)
        self._backend_hash = runner.backend_hash()
        self._ctx = _ExecContext(
            runner=runner,
            store_root=store.location,
            store_traces=config.store_traces,
            trace_decimation=config.trace_decimation,
            derive=derive,
        )
        self._pool: ProcessPoolExecutor | None = None

    # ---- helpers --------------------------------------------------------

    def _concrete(
        self, tc: TestCase, assignment: dict[str, float], seed: int
    ) -> ConcreteRun:
        return ConcreteRun(
            test_case_ref=tc.ref,
            scenario=tc.scenario_template,
            assignment=assignment,
            tier=self.runner.tier,
            pinning=Pinning(
                scenario_hash=digest(
                    {
                        "tc": tc.ref,
                        "template": tc.scenario_template,
                        "stl": tc.criterion_stl,
                    }
                ),
                sut_build_hash=self.config.sut_build_hash,
                backend_hash=self._backend_hash,
                config_hash=self.config.config_hash,
                seed=seed,
            ),
        )

    def _execute_many(
        self, tc: TestCase, points: Sequence[tuple[dict[str, float], int]]
    ) -> list[tuple[ConcreteRun, RunResult]]:
        """Execute points and apply the determinism guard. Order preserved."""
        repeats = 1 if self.runner.deterministic else max(1, self.config.repeats_nondeterministic)
        jobs = [
            (tc, self._concrete(tc, a, seed)) for a, seed in points for _ in range(repeats)
        ]
        if self._pool is not None and len(jobs) > 1:
            raw = self._execute_in_pool(jobs)
        else:
            raw = [execute_point(self._ctx, t, r) for t, r in jobs]

        out: list[tuple[ConcreteRun, RunResult]] = []
        for i in range(0, len(raw), repeats):
            group = raw[i : i + repeats]
            run, res = group[0]
            if repeats > 1:
                res = self._guard(run, group)
            # Invariant I1: record every execution, repeats included. The
            # first entry carries the group's result; the others are tagged
            # as repeats so that reading the chain back counts the point once.
            self._record(run, res)
            for k, (r2, s2) in enumerate(group[1:], start=1):
                self._record(r2, s2, repeat=k)
            out.append((run, res))
        return out

    def _execute_in_pool(
        self, jobs: list[tuple[TestCase, ConcreteRun]]
    ) -> list[tuple[ConcreteRun, RunResult]]:
        """Run jobs in the pool; a job whose worker died becomes an ERROR run.

        `pool.map` would raise on the first broken worker and discard the
        results that had already finished, which breaks invariant I1.
        """
        assert self._pool is not None
        futures = [self._pool.submit(_worker_execute, job) for job in jobs]
        raw: list[tuple[ConcreteRun, RunResult]] = []
        broken = False
        for (_, run), fut in zip(jobs, futures, strict=True):
            try:
                raw.append(fut.result())
            except Exception as exc:  # noqa: BLE001 - recorded as an ERROR run
                broken = broken or isinstance(exc, BrokenProcessPool)
                raw.append(
                    (
                        run,
                        RunResult(
                            run_id=run.id,
                            verdict=Verdict.ERROR,
                            message=f"worker failed: {type(exc).__name__}: {exc}"[:1000],
                        ),
                    )
                )
        if broken:
            self._pool.shutdown(cancel_futures=True)
            self._pool = self._new_pool()
        return raw

    def _new_pool(self) -> ProcessPoolExecutor:
        return ProcessPoolExecutor(
            max_workers=min(self.config.workers, os.cpu_count() or 1),
            initializer=_worker_init,
            initargs=(self._ctx,),
        )

    def _guard(
        self, run: ConcreteRun, group: list[tuple[ConcreteRun, RunResult]]
    ) -> RunResult:
        """Combine repeats of one point into a single result.

        Independent of the order the repeats came back in: all errored is
        ERROR; some errored is ERROR (the point was not fully executed);
        agreeing verdicts within tolerance is the first result; anything
        else is FLAKY.
        """
        first = group[0][1]
        verdicts = [r.verdict for _, r in group]
        n_err = sum(1 for v in verdicts if v is Verdict.ERROR)
        if n_err:
            if n_err == len(group):
                return first
            msg = next(r.message for _, r in group if r.verdict is Verdict.ERROR)
            return RunResult(
                run_id=run.id,
                verdict=Verdict.ERROR,
                duration_s=first.duration_s,
                message=f"{n_err} of {len(group)} repeats errored: {msg}"[:1000],
            )
        rhos = [r.robustness for _, r in group if r.robustness is not None]
        spread = (max(rhos) - min(rhos)) if len(rhos) > 1 else 0.0
        if len(set(verdicts)) == 1 and spread <= self.config.flaky_tolerance:
            return first
        return RunResult(
            run_id=run.id,
            verdict=Verdict.FLAKY,
            robustness=min(rhos) if rhos else None,
            duration_s=first.duration_s,
            artifacts=first.artifacts,
            metrics={"robustness_spread": spread},
            message=(
                f"non-reproducible under identical pinning: verdicts="
                f"{[v.value for v in verdicts]}, robustness spread={spread:.6g}"
            ),
        )

    def _record(self, run: ConcreteRun, res: RunResult, repeat: int = 0) -> None:
        """Invariant I1: everything executed is recorded, no exceptions."""
        self.store.append(
            {
                "campaign_id": self.config.campaign_id,
                "type": "run",
                "repeat": repeat,
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
        seed_base = (self.config.seed ^ stable_u32(tc.ref)) & 0x7FFFFFFF

        # Phase 1 - boundary
        self._log(f"  {tc.ref}: boundary")
        pts = BoundarySampler().sample(space, self.config.boundary_budget, seed_base)
        seen += pts
        for run, res in self._execute_many(
            tc, [(a, seed_base + i) for i, a in enumerate(pts)]
        ):
            out.note(run, res)

        # Phase 2 - sweep
        self._log(f"  {tc.ref}: sweep")
        pts = SobolSampler().sample(space, self.config.sweep_budget, seed_base + 1)
        seen += pts
        for run, res in self._execute_many(
            tc, [(a, seed_base + 1000 + i) for i, a in enumerate(pts)]
        ):
            out.note(run, res)

        # Phase 3 - falsification (sequential: each step depends on the last)
        if not out.falsified and self.config.falsify_budget > 0:
            self._log(f"  {tc.ref}: falsify")
            counter = {"n": 0}

            def objective(a: dict[str, float]) -> float:
                counter["n"] += 1
                seen.append(a)
                (run, res), = self._execute_many(tc, [(a, seed_base + 5000 + counter["n"])])
                out.note(run, res)
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
                "n_errors": out.n_errors,
                "coverage": out.coverage,
                "wall_time_s": out.wall_time_s,
            }
        )
        return out

    # ---- entry point ----------------------------------------------------

    def run(self, only: Sequence[str] | None = None) -> CampaignResult:
        cfg = self.config
        result = CampaignResult(
            config=cfg,
            started_at=time.time(),
            runner=self.runner.name,
            backend_hash=self._backend_hash,
            project_digest=self.project.content_digest(),
        )
        self.store.append(
            {
                "campaign_id": cfg.campaign_id,
                "type": "campaign_start",
                "project": self.project.name,
                "project_digest": result.project_digest,
                "safegate_version": __version__,
                "runner": self.runner.name,
                "tier": self.runner.tier.value,
                "deterministic": self.runner.deterministic,
                "sut_build_hash": cfg.sut_build_hash,
                "config_hash": cfg.config_hash,
                "backend_hash": self._backend_hash,
                "seed": cfg.seed,
                "boundary_budget": cfg.boundary_budget,
                "sweep_budget": cfg.sweep_budget,
                "falsify_budget": cfg.falsify_budget,
                "repeats_nondeterministic": cfg.repeats_nondeterministic,
                "flaky_tolerance": cfg.flaky_tolerance,
                "only": list(only) if only else None,
            }
        )
        if cfg.workers > 1:
            self._pool = self._new_pool()
        try:
            for tc in self.project.test_cases:
                if only and tc.ref not in only:
                    continue
                self._log(f"[{tc.ref}] {tc.title}")
                result.outcomes[tc.ref] = self.run_test_case(tc)
        finally:
            if self._pool is not None:
                self._pool.shutdown()
                self._pool = None

        result.finished_at = time.time()
        self.store.append(
            {
                "campaign_id": cfg.campaign_id,
                "type": "campaign_end",
                "failed": result.failed,
                "n_test_cases": len(result.outcomes),
                "n_runs": sum(o.n_executed for o in result.outcomes.values()),
                "wall_time_s": result.finished_at - result.started_at,
            }
        )
        result.merkle_root = self.store.campaign_root(cfg.campaign_id)
        return result


# --------------------------------------------------------------------------
# Reading a campaign back from the chain
# --------------------------------------------------------------------------


class CampaignNotFound(LookupError):
    pass


def load_campaign(store: EvidenceStore, campaign_id: str | None = None) -> CampaignResult:
    """Reconstruct one campaign's results from the evidence chain.

    Reads the chain, not an in-memory object or a side-car JSON file: the
    gate and the report must be derived from the same tamper-evident record
    an assessor would examine.

    Exactly one campaign is read. A store that persists across CI runs holds
    many campaigns; merging them would report runs of an old firmware build
    under the new build's hash, which is threat T4. Without an explicit id
    the most recently started campaign is used.
    """
    ids = store.campaign_ids()
    if not ids:
        raise CampaignNotFound("evidence store contains no campaign")
    if campaign_id is None:
        campaign_id = ids[-1]
    elif campaign_id not in ids:
        raise CampaignNotFound(
            f"campaign {campaign_id!r} not in store; available: {ids}"
        )

    cfg: CampaignConfig | None = None
    result: CampaignResult | None = None
    outcomes: dict[str, TestCaseOutcome] = {}
    summarised: set[str] = set()
    ended = False
    for e in store.campaign_entries(campaign_id):
        p = e.payload
        t = p.get("type")
        if t == "campaign_start":
            cfg = CampaignConfig(
                campaign_id=campaign_id,
                sut_build_hash=p.get("sut_build_hash", ""),
                config_hash=p.get("config_hash", ""),
                boundary_budget=int(p.get("boundary_budget", 24)),
                sweep_budget=int(p.get("sweep_budget", 128)),
                falsify_budget=int(p.get("falsify_budget", 64)),
                seed=int(p.get("seed", 20260914)),
            )
            result = CampaignResult(
                config=cfg,
                runner=p.get("runner", ""),
                backend_hash=p.get("backend_hash", ""),
                project_digest=p.get("project_digest", ""),
            )
        elif t == "run":
            if int(p.get("repeat", 0)) > 0:
                continue  # repeats are evidence, but the point was counted once
            run = ConcreteRun.model_validate(p["run"])
            res = RunResult.model_validate(p["result"])
            o = outcomes.setdefault(
                run.test_case_ref, TestCaseOutcome(test_case_ref=run.test_case_ref)
            )
            o.note(run, res)
        elif t == "test_case_summary":
            o = outcomes.setdefault(
                p["test_case_ref"], TestCaseOutcome(test_case_ref=p["test_case_ref"])
            )
            o.coverage = p.get("coverage", {})
            o.wall_time_s = float(p.get("wall_time_s", 0.0))
            summarised.add(p["test_case_ref"])
        elif t == "campaign_end":
            ended = True
    assert cfg is not None and result is not None  # campaign_ids() guarantees a start entry
    result.outcomes = outcomes
    result.complete = ended
    result.incomplete_test_cases = sorted(set(outcomes) - summarised)
    result.merkle_root = store.campaign_root(campaign_id)
    return result


__all__ = [
    "Campaign",
    "CampaignConfig",
    "CampaignNotFound",
    "CampaignResult",
    "TestCaseOutcome",
    "execute_point",
    "load_campaign",
    "stable_u32",
]
