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
  R-TRACE-004 every requirement that covers a hazard is realised by a
              safety function, so its PL can be derived at all
  R-EXEC-001  no test case may be FAIL, FLAKY or ERROR
  R-EXEC-002  test cases verifying a PL d or PL e function must have
              evidence at HIL tier or above - simulation alone does not
              discharge a verification obligation for a high-PL function
  R-COV-001   two-way parameter coverage above a threshold
  R-MARGIN-001 worst-case robustness margin above a floor (catches the
              design that passes with 2 mm to spare)
  R-EVID-001  the manifest chain is intact, signed by a trusted key pinned
              in the policy, and consistent with any published anchor
  R-EVID-002  the evidence was produced against the design data being
              gated (project digest) and, if given, the expected build
  R-ML-001    ML in the safety path triggers the Machinery Regulation
              Notified-Body route; self-declaration is blocked

Waivers are supported because pretending they will not happen is naive,
but a waiver requires a justification and a named approver, and every
waived finding appears in the technical file. A silent waiver is not a
feature this tool will ever have.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import yaml

from ..core.cas import EvidenceStore
from ..core.model import (
    TIER_WEIGHT,
    ExecutionTier,
    Finding,
    PerformanceLevel,
    Project,
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
    trusted_signers: list[str] = field(default_factory=list)
    waivers: dict[str, dict[str, str]] = field(default_factory=dict)
    disabled_rules: set[str] = field(default_factory=set)

    @staticmethod
    def from_yaml(path: str) -> PolicyConfig:
        with open(path, encoding="utf-8") as fh:
            d = yaml.safe_load(fh) or {}
        return PolicyConfig(
            min_two_way_coverage=float(d.get("min_two_way_coverage", 0.60)),
            min_robustness_margin=float(d.get("min_robustness_margin", 0.0)),
            require_signed_evidence=bool(d.get("require_signed_evidence", True)),
            high_pl_min_tier=ExecutionTier(d.get("high_pl_min_tier", "hil")),
            high_pl_threshold=PerformanceLevel(d.get("high_pl_threshold", "d")),
            trusted_signers=[str(k).strip().lower() for k in d.get("trusted_signers", []) or []],
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
            return "GATE PASS - no findings."
        order = {"blocker": 0, "major": 1, "minor": 2, "info": 3}
        rows = sorted(self.findings, key=lambda f: (order[f.severity], f.rule))
        out = [f"GATE {'PASS' if self.passed else 'FAIL'} - {len(rows)} finding(s)", ""]
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
        anchors: list[dict] | None = None,
        expected_build: str | None = None,
    ) -> None:
        self.project = project
        self.campaign = campaign
        self.store = store
        self.config = config
        self.anchors = anchors or []
        self.expected_build = expected_build
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
        required_pls = self._required_pls()

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

            # Checked before validity: an ML architecture is usually also an
            # invalid one, and the regulatory route is a separate blocker
            # that must not be hidden behind the first.
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

            if not plr_result.valid:
                self._raise(
                    "R-PL-002",
                    "blocker",
                    sf.ref,
                    "PL determination invalid: " + "; ".join(plr_result.violations),
                )
                continue

            required = required_pls.get(sf.ref)
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

    def _rule_traceability(self) -> None:
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
        allocated: set[str] = set()
        for sf in self.project.safety_functions:
            allocated.update(sf.requirement_refs)
        for r in self.project.requirements:
            if r.hazard_refs and r.ref not in allocated:
                self._raise(
                    "R-TRACE-004",
                    "blocker",
                    r.ref,
                    "requirement covers a hazard but no safety function lists it, so no "
                    "Performance Level can be derived for it",
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
        if not self.campaign.complete:
            self._raise(
                "R-EXEC-001",
                "blocker",
                self.campaign.config.campaign_id,
                "campaign has no campaign_end entry: it was interrupted and its results "
                "are partial",
            )
        for ref in self.campaign.incomplete_test_cases:
            self._raise(
                "R-EXEC-001",
                "blocker",
                ref,
                "test case has runs but no summary: it did not finish executing",
            )
        sf_by_req: dict[str, list[str]] = {}
        for sf in self.project.safety_functions:
            for rref in sf.requirement_refs:
                sf_by_req.setdefault(rref, []).append(sf.ref)
        req_by_ref = {r.ref: r for r in self.project.requirements}
        haz_by_ref = {h.ref: h for h in self.project.hazards}
        required_pls = self._required_pls()
        achieved_pls = {
            ref: pr.achieved_pl for ref, pr in report.pl_results.items() if pr.achieved_pl
        }

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
                first = next((r.message for _, r in o.runs if r.verdict is Verdict.ERROR), "")
                self._raise(
                    "R-EXEC-001",
                    "blocker",
                    tc.ref,
                    f"{o.n_errors} of {o.n_executed} runs errored, so part of the "
                    f"declared space has no evidence. First error: {first[:200]}",
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

            # Tier adequacy. Evidence tier is what the runner that produced
            # the run declares, never what the test case asks for.
            best_tier = max((TIER_WEIGHT[run.tier] for run, _ in o.runs), default=-1)
            best_name = next(
                (run.tier.value for run, _ in o.runs if TIER_WEIGHT[run.tier] == best_tier),
                "none",
            )
            if best_tier < TIER_WEIGHT[tc.required_tier]:
                self._raise(
                    "R-EXEC-003",
                    "blocker",
                    tc.ref,
                    f"declares tier {tc.required_tier.value} but the best evidence "
                    f"is {best_name}",
                )

            # High-PL functions need physical evidence. The PL that sets the
            # obligation is the higher of required and achieved: using only
            # the achieved PL would let an invalid or under-designed function
            # escape the rule.
            # Hazards reached directly through the requirement count too, so a
            # requirement no safety function lists cannot escape the rule.
            obligation: PerformanceLevel | None = None
            for rref in tc.requirement_refs:
                candidates = [
                    pl
                    for sfref in sf_by_req.get(rref, [])
                    for pl in (required_pls.get(sfref), achieved_pls.get(sfref))
                ]
                req = req_by_ref.get(rref)
                if req is not None:
                    candidates += [
                        haz_by_ref[h].required_pl for h in req.hazard_refs if h in haz_by_ref
                    ]
                for pl in candidates:
                    if pl is not None and (obligation is None or pl.rank > obligation.rank):
                        obligation = pl
            if (
                obligation is not None
                and obligation.rank >= self.config.high_pl_threshold.rank
                and best_tier < TIER_WEIGHT[self.config.high_pl_min_tier]
            ):
                self._raise(
                    "R-EXEC-002",
                    "blocker",
                    tc.ref,
                    f"verifies a PL {obligation.value} obligation but the best evidence "
                    f"tier is {best_name}, below {self.config.high_pl_min_tier.value}. "
                    "Simulation alone does not discharge a verification obligation "
                    "at this PL.",
                )

    def _rule_evidence_integrity(self) -> None:
        if self.store is None:
            self._raise(
                "R-EVID-001", "blocker", "evidence-store", "no evidence store supplied"
            )
            return
        trusted = self.config.trusted_signers
        problems = self.store.verify_chain(trusted_keys=trusted)
        for p in problems:
            self._raise("R-EVID-001", "blocker", "evidence-store", p)
        for anchor in self.anchors:
            for p in self.store.verify_anchor(anchor):
                self._raise("R-EVID-001", "blocker", "evidence-store", f"anchor: {p}")
        signers, unsigned = self._campaign_signers()
        if self.config.require_signed_evidence:
            if unsigned:
                self._raise(
                    "R-EVID-001",
                    "blocker",
                    "evidence-store",
                    f"{unsigned} campaign entries are unsigned, and the policy requires "
                    "signed evidence",
                )
            if len(signers) > 1:
                self._raise(
                    "R-EVID-001",
                    "blocker",
                    "evidence-store",
                    f"campaign entries are signed by {len(signers)} different keys",
                )
            if signers and not trusted:
                self._raise(
                    "R-EVID-001",
                    "major",
                    "evidence-store",
                    "signatures are self-attested: no trusted_signers are pinned in "
                    "the policy, so a writer who regenerates the chain with a new key "
                    "would also pass",
                )

        if self.campaign is not None:
            cur = self.project.content_digest()
            if self.campaign.project_digest and self.campaign.project_digest != cur:
                self._raise(
                    "R-EVID-002",
                    "blocker",
                    self.campaign.config.campaign_id,
                    "evidence was produced against different design data (project "
                    f"digest {self.campaign.project_digest[:16]}..., current "
                    f"{cur[:16]}...); re-run the campaign",
                )
            if (
                self.expected_build
                and self.campaign.config.sut_build_hash != self.expected_build
            ):
                self._raise(
                    "R-EVID-002",
                    "blocker",
                    self.campaign.config.campaign_id,
                    f"evidence is for build {self.campaign.config.sut_build_hash[:16]}, "
                    f"release candidate is {self.expected_build[:16]}",
                )

    def _campaign_signers(self) -> tuple[set[str], int]:
        """Distinct signers and the number of unsigned entries in the campaign."""
        if self.store is None:
            return set(), 0
        if self.campaign is not None:
            entries = self.store.campaign_entries(self.campaign.config.campaign_id)
        else:
            entries = list(self.store.entries())
        signers = {e.signer or "" for e in entries if e.signature}
        unsigned = sum(1 for e in entries if not e.signature)
        return signers, unsigned

    def _required_pls(self) -> dict[str, PerformanceLevel]:
        req_by_ref = {r.ref: r for r in self.project.requirements}
        haz_by_ref = {h.ref: h for h in self.project.hazards}
        out: dict[str, PerformanceLevel] = {}
        for sf in self.project.safety_functions:
            for rref in sf.requirement_refs:
                req = req_by_ref.get(rref)
                if req is None:
                    continue
                for href in req.hazard_refs:
                    h = haz_by_ref.get(href)
                    if h is None:
                        continue
                    cur = out.get(sf.ref)
                    if cur is None or h.required_pl.rank > cur.rank:
                        out[sf.ref] = h.required_pl
        return out

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
