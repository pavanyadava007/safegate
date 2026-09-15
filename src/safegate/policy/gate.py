"""
safegate.policy.gate
====================

The release gate: a declarative policy evaluated over the evidence graph.

Why declarative and not a pile of `if` statements: the release criteria
are a *negotiated artefact*. A Notified Body, a customer's safety
department and your own engineering leadership all have opinions about
what "done" means, and those opinions change between projects and between
revisions of the standard. Policy as data means the criteria are
reviewable, diffable and version-controlled alongside the design. Policy
as code buried in a Python module means nobody outside the team can audit
what the gate actually enforced.

The rules shipped by default encode the non-negotiables:

  R-PL-001    every safety function achieves the PL required by the
              hazard it mitigates (PLr from the ISO 13849-1 Annex A risk
              graph, achieved PL from the architecture)
  R-PL-002    the PL determination itself is valid (CCF, channel count,
              admissible DC band)
  R-TRACE-001 every hazard is covered by at least one requirement
  R-TRACE-002 every requirement is verified by at least one test case
  R-TRACE-003 every requirement has a machine-checkable criterion
  R-EXEC-001  no test case may be FAIL, FLAKY or ERROR
  R-EXEC-002  test cases verifying a PL d or PL e function must have
              evidence at HIL tier or above — simulation alone does not
              discharge a verification obligation for a high-PL function
  R-COV-001   two-way parameter coverage above a threshold
  R-MARGIN-001 worst-case robustness margin above a floor (catches the
              design that passes with 2 mm to spare)
  R-EVID-001  the manifest chain is intact and signed
  R-ML-001    ML in the safety path triggers the Machinery Regulation
              Notified-Body route; self-declaration is blocked

Waivers are supported because pretending they will not happen is naive,
but a waiver requires a justification and a named approver, and every
waived finding appears in the technical file. A silent waiver is not a
feature this tool will ever have.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

import yaml

from ..core.cas import EvidenceStore
from ..core.model import (
    ExecutionTier,
    Finding,
    PerformanceLevel,
    Project,
    TIER_WEIGHT,
    Verdict,
)
from ..iso13849.pl import PLResult, evaluate_architecture
from ..orchestrator.campaign import CampaignResult


@dataclass
class PolicyConfig:
    min_two_way_coverage: float = 0.60
    min_robustness_margin: float = 0.0
    require_signed_evidence: bool = True
    high_pl_min_tier: ExecutionTier = ExecutionTier.HIL
    high_pl_threshold: PerformanceLevel = PerformanceLevel.d
    waivers: dict[str, dict[str, str]] = field(default_factory=dict)
    disabled_rules: set[str] = field(default_factory=set)

    @staticmethod
    def from_yaml(path: str) -> "PolicyConfig":
        with open(path, "r", encoding="utf-8") as fh:
            d = yaml.safe_load(fh) or {}
        return PolicyConfig(
            min_two_way_coverage=float(d.get("min_two_way_coverage", 0.60)),
            min_robustness_margin=float(d.get("min_robustness_margin", 0.0)),
            require_signed_evidence=bool(d.get("require_signed_evidence", True)),
            high_pl_min_tier=ExecutionTier(d.get("high_pl_min_tier", "hil")),
            high_pl_threshold=PerformanceLevel(d.get("high_pl_threshold", "d")),
            waivers=d.get("waivers", {}) or {},
            disabled_rules=set(d.get("disabled_rules", []) or []),
        )


@dataclass
class GateReport:
    findings: list[Finding] = field(default_factory=list)
    pl_results: dict[str, PLResult] = field(default_factory=dict)
    passed: bool = True

    def blockers(self) -> list[Finding]:
        return [
            f
            for f in self.findings
            if f.severity == "blocker" and f.disposition == "open"
        ]

    def render(self) -> str:
        if not self.findings:
            return "GATE PASS — no findings."
        order = {"blocker": 0, "major": 1, "minor": 2, "info": 3}
        rows = sorted(self.findings, key=lambda f: (order[f.severity], f.rule))
        out = [f"GATE {'PASS' if self.passed else 'FAIL'} — {len(rows)} finding(s)", ""]
        for f in rows:
            tag = f.severity.upper()
            mark = "" if f.disposition == "open" else f" [{f.disposition}]"
            out.append(f"  {tag:<8} {f.rule:<14} {f.subject}{mark}")
            out.append(f"           {f.detail}")
            if f.waiver_justification:
                out.append(
                    f"           waiver: {f.waiver_justification}"
                    f" (approved by {f.waiver_approver})"
                )
        return "\n".join(out)


class Gate:
    def __init__(
        self,
        project: Project,
        campaign: CampaignResult | None,
        store: EvidenceStore | None,
        config: PolicyConfig,
    ) -> None:
        self.project = project
        self.campaign = campaign
        self.store = store
        self.config = config
        self._findings: list[Finding] = []

    # ---- finding creation with waiver application -----------------------

    def _raise(
        self, rule: str, severity: str, subject: str, detail: str
    ) -> None:
        if rule in self.config.disabled_rules:
            return
        key = f"{rule}:{subject}"
        w = self.config.waivers.get(key) or self.config.waivers.get(rule)
        if w and w.get("justification") and w.get("approver"):
            self._findings.append(
                Finding(
                    ref=key,
                    severity=severity,  # severity is preserved, not downgraded
                    rule=rule,
                    subject=subject,
                    detail=detail,
                    disposition="waived",
                    waiver_justification=w["justification"],
                    waiver_approver=w["approver"],
                )
            )
            return
        self._findings.append(
            Finding(ref=key, severity=severity, rule=rule, subject=subject, detail=detail)
        )

    # ---- rules ----------------------------------------------------------

    def _rule_performance_levels(self, report: GateReport) -> None:
        arch_by_ref = {a.ref: a for a in self.project.architectures}
        req_by_ref = {r.ref: r for r in self.project.requirements}
        haz_by_ref = {h.ref: h for h in self.project.hazards}

        for sf in self.project.safety_functions:
            arch = arch_by_ref.get(sf.architecture_ref)
            if arch is None:
                self._raise(
                    "R-PL-002",
                    "blocker",
                    sf.ref,
                    f"references unknown architecture {sf.architecture_ref!r}",
                )
                continue
            plr_result = evaluate_architecture(arch)
            report.pl_results[sf.ref] = plr_result

            if not plr_result.valid:
                self._raise(
                    "R-PL-002",
                    "blocker",
                    sf.ref,
                    "PL determination invalid: " + "; ".join(plr_result.violations),
                )
                continue

            # Required PL = max PLr over all hazards this function mitigates
            required: PerformanceLevel | None = None
            for rref in sf.requirement_refs:
                req = req_by_ref.get(rref)
                if req is None:
                    continue
                for href in req.hazard_refs:
                    h = haz_by_ref.get(href)
                    if h is None:
                        continue
                    plr = h.required_pl
                    if required is None or plr.rank > required.rank:
                        required = plr
            if required is None:
                self._raise(
                    "R-TRACE-001",
                    "major",
                    sf.ref,
                    "no hazard reachable from this safety function; PLr undetermined",
                )
                continue
            if plr_result.achieved_pl is None or plr_result.achieved_pl.rank < required.rank:
                got = plr_result.achieved_pl.value if plr_result.achieved_pl else "none"
                self._raise(
                    "R-PL-001",
                    "blocker",
                    sf.ref,
                    f"achieved PL {got} < required PLr {required.value} "
                    f"(Cat {arch.category.value}, MTTFd {plr_result.mttfd_band.value}, "
                    f"DCavg {plr_result.dc_band.value}, CCF {plr_result.ccf_score})",
                )
            if arch.uses_ml_in_safety_path:
                self._raise(
                    "R-ML-001",
                    "blocker",
                    sf.ref,
                    "architecture declares machine learning in the safety path. "
                    "Regulation (EU) 2023/1230 Annex I requires third-party "
                    "conformity assessment by a Notified Body for safety "
                    "components with self-evolving ML behaviour; "
                    "self-declaration is not available and ISO 13849-1 offers "
                    "no quantification route.",
                )

    def _rule_traceability(self) -> None:
        req_by_ref = {r.ref: r for r in self.project.requirements}
        covered_hazards: set[str] = set()
        for r in self.project.requirements:
            covered_hazards.update(r.hazard_refs)
        for h in self.project.hazards:
            if h.ref not in covered_hazards:
                self._raise(
                    "R-TRACE-001",
                    "blocker",
                    h.ref,
                    f"hazard has no safety requirement (PLr {h.required_pl.value})",
                )
        verified: set[str] = set()
        for tc in self.project.test_cases:
            verified.update(tc.requirement_refs)
        for r in self.project.requirements:
            if r.ref not in verified:
                self._raise(
                    "R-TRACE-002", "blocker", r.ref, "requirement has no test case"
                )
            if not r.criterion_stl:
                self._raise(
                    "R-TRACE-003",
                    "major",
                    r.ref,
                    "requirement has no machine-checkable STL criterion; "
                    "verification cannot be automated",
                )
            for h in r.hazard_refs:
                if h not in {x.ref for x in self.project.hazards}:
                    self._raise(
                        "R-TRACE-001", "major", r.ref, f"dangling hazard ref {h!r}"
                    )

    def _rule_execution(self, report: GateReport) -> None:
        if self.campaign is None:
            self._raise(
                "R-EXEC-001", "blocker", self.project.name, "no campaign results supplied"
            )
            return
        req_by_ref = {r.ref: r for r in self.project.requirements}
        sf_by_req: dict[str, list[str]] = {}
        for sf in self.project.safety_functions:
            for rref in sf.requirement_refs:
                sf_by_req.setdefault(rref, []).append(sf.ref)

        for tc in self.project.test_cases:
            o = self.campaign.outcomes.get(tc.ref)
            if o is None:
                self._raise("R-EXEC-001", "blocker", tc.ref, "test case not executed")
                continue

            if o.verdict is Verdict.FAIL:
                self._raise(
                    "R-EXEC-001",
                    "blocker",
                    tc.ref,
                    f"falsified: worst robustness {o.worst_robustness:.6g} at "
                    f"{o.worst_assignment}",
                )
            elif o.verdict is Verdict.FLAKY:
                self._raise(
                    "R-EXEC-001",
                    "blocker",
                    tc.ref,
                    "non-reproducible under identical pinning; a safety test that "
                    "does not reproduce has verified nothing",
                )
            elif o.verdict is Verdict.ERROR:
                self._raise(
                    "R-EXEC-001", "blocker", tc.ref, "all runs errored; no evidence produced"
                )

            # Coverage
            two_way = o.coverage.get("two_way", 0.0)
            if two_way < self.config.min_two_way_coverage:
                self._raise(
                    "R-COV-001",
                    "major",
                    tc.ref,
                    f"two-way parameter coverage {two_way:.1%} < "
                    f"{self.config.min_two_way_coverage:.1%}",
                )

            # Margin
            if (
                o.verdict is Verdict.PASS
                and o.worst_robustness != float("inf")
                and o.worst_robustness < self.config.min_robustness_margin
            ):
                self._raise(
                    "R-MARGIN-001",
                    "major",
                    tc.ref,
                    f"passes but worst-case margin is only {o.worst_robustness:.6g}, "
                    f"below the required floor {self.config.min_robustness_margin:.6g}",
                )

            # Tier adequacy for high-PL functions
            required_pl: PerformanceLevel | None = None
            for rref in tc.requirement_refs:
                for sfref in sf_by_req.get(rref, []):
                    pr = report.pl_results.get(sfref)
                    if pr and pr.achieved_pl:
                        if required_pl is None or pr.achieved_pl.rank > required_pl.rank:
                            required_pl = pr.achieved_pl
            if required_pl and required_pl.rank >= self.config.high_pl_threshold.rank:
                best_tier = max(
                    (TIER_WEIGHT[run.tier] for run, _ in o.runs), default=-1
                )
                if best_tier < TIER_WEIGHT[self.config.high_pl_min_tier]:
                    self._raise(
                        "R-EXEC-002",
                        "blocker",
                        tc.ref,
                        f"verifies a PL {required_pl.value} function but the highest "
                        f"evidence tier is below "
                        f"{self.config.high_pl_min_tier.value}. Simulation alone does "
                        "not discharge a verification obligation at this PL.",
                    )

    def _rule_evidence_integrity(self) -> None:
        if self.store is None:
            self._raise(
                "R-EVID-001", "blocker", "evidence-store", "no evidence store supplied"
            )
            return
        pub = self.store.signer_id
        problems = self.store.verify_chain(pub)
        for p in problems:
            self._raise("R-EVID-001", "blocker", "evidence-store", p)
        if self.config.require_signed_evidence and not pub:
            self._raise(
                "R-EVID-001",
                "major",
                "evidence-store",
                "evidence chain is unsigned; integrity is detectable but not "
                "attributable",
            )

    # ---- entry point ----------------------------------------------------

    def evaluate(self) -> GateReport:
        report = GateReport()
        self._findings = []
        self._rule_performance_levels(report)
        self._rule_traceability()
        self._rule_execution(report)
        self._rule_evidence_integrity()
        report.findings = list(self._findings)
        report.passed = not report.blockers()
        return report


__all__ = ["Gate", "GateReport", "PolicyConfig"]
