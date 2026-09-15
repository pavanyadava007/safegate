"""
safegate.report.technical_file
==============================

Generates the verification and validation section of the technical
documentation, as a pure function of the evidence graph.

The single design rule: nothing in this file is hand-written. If a number
appears in the report, it was computed from evidence that is content-
addressed and chained. Hand-editing the report is the failure mode this
whole system exists to eliminate: it is how a document ends up claiming
PL d for a design that achieves PL c, and it is how companies end up in
front of a market-surveillance authority.

Two things the generated document does that most vendor-produced safety
reports do not, both deliberate:

  1. It states the limits of the method. Falsification is not proof.
     Simulation is not physical verification. An assessor who finds those
     caveats in your document trusts the rest of it more, not less. An
     assessor who discovers the omission themselves trusts nothing.

  2. It includes the open and waived findings. A report that shows only
     passes is a marketing document. The disposition column is what makes
     it an engineering record.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import re
import textwrap
from dataclasses import dataclass

from .. import __version__
from ..core.cas import EvidenceStore
from ..core.model import Project, Verdict
from ..iso3691.metrics import FieldSizingTable, StoppingBudget
from ..iso13849.pl import PLResult
from ..orchestrator.campaign import CampaignResult
from ..policy.gate import GateReport


@dataclass
class DocumentMeta:
    manufacturer: str
    product: str
    document_id: str
    revision: str
    author: str
    approver: str = ""
    notified_body: str | None = None


class TechnicalFile:
    def __init__(
        self,
        project: Project,
        campaign: CampaignResult,
        gate: GateReport,
        store: EvidenceStore,
        meta: DocumentMeta,
        budget: StoppingBudget | None = None,
    ) -> None:
        self.project = project
        self.campaign = campaign
        self.gate = gate
        self.store = store
        self.meta = meta
        self.budget = budget

    # ---- sections -------------------------------------------------------

    def _cover(self) -> str:
        cfg = self.campaign.config
        root = self.store.campaign_root(cfg.campaign_id)
        status = "CONFORMING" if self.gate.passed else "NON-CONFORMING"
        nb = self.meta.notified_body or "not engaged"
        signer = self._signer() or "unsigned"
        return textwrap.dedent(
            f"""\
            # Verification and Validation Report

            | | |
            |---|---|
            | Manufacturer | {self.meta.manufacturer} |
            | Product | {self.meta.product} |
            | Document | {self.meta.document_id} rev {self.meta.revision} |
            | Machine type | {self.project.machine_type} |
            | Variant | {self.project.variant} |
            | Issued | {_dt.datetime.now(_dt.UTC).strftime('%Y-%m-%d %H:%M UTC')} |
            | Author | {self.meta.author} |
            | Approver | {self.meta.approver or 'not approved'} |
            | Notified Body | {nb} |
            | **Gate status** | **{status}** |

            **Evidence commitment**

            | | |
            |---|---|
            | Campaign | `{cfg.campaign_id}` |
            | Merkle root | `{root}` |
            | SUT build | `{cfg.sut_build_hash}` |
            | Configuration | `{cfg.config_hash}` |
            | Runner / backend | `{self.campaign.runner or 'unknown'}` / `{self.campaign.backend_hash[:16]}` |
            | Project data digest | `{self.campaign.project_digest[:16]}` |
            | Campaign seed / budgets | {cfg.seed} / boundary {cfg.boundary_budget}, sweep {cfg.sweep_budget}, falsify {cfg.falsify_budget} |
            | Signer (Ed25519) | `{signer}` |
            | SafeGate | {__version__} |

            Any party holding this document and the evidence store can
            recompute the Merkle root and confirm that no record of this
            campaign has been added, removed or altered since issue:

                safegate verify --store <evidence store> --report <this file> \\
                    --trusted-key <signer key from an independent source>
            """
        )

    def _signer(self) -> str | None:
        cid = self.campaign.config.campaign_id
        for e in self.store.campaign_entries(cid):
            return e.signer if e.signature else None
        return None

    def _standards(self) -> str:
        rows = "\n".join(f"- {s}" for s in self.project.standards)
        return f"## 1 Standards applied\n\n{rows}\n"

    def _hazards(self) -> str:
        lines = [
            "## 2 Hazard analysis and required Performance Levels",
            "",
            "Required PL derived from the ISO 13849-1 Annex A risk graph.",
            "",
            "| Ref | Hazard | Zone | S | F | P | PLr |",
            "|---|---|---|---|---|---|---|",
        ]
        for h in sorted(self.project.hazards, key=lambda x: x.ref):
            lines.append(
                f"| {h.ref} | {h.title} | {h.zone or 'n/a'} | {h.severity.value} "
                f"| {h.frequency.value} | {h.avoidance.value} | "
                f"**{h.required_pl.value}** |"
            )
        return "\n".join(lines) + "\n"

    def _safety_functions(self) -> str:
        lines = [
            "## 3 Safety functions and achieved Performance Levels",
            "",
            "| Safety function | Cat | MTTFd | DCavg | CCF | Achieved PL | PFHd band [1/h] |",
            "|---|---|---|---|---|---|---|",
        ]
        for sf in sorted(self.project.safety_functions, key=lambda x: x.ref):
            pr: PLResult | None = self.gate.pl_results.get(sf.ref)
            if pr is None:
                lines.append(f"| {sf.ref} | n/a | n/a | n/a | n/a | not determined | n/a |")
                continue
            pl = pr.achieved_pl.value if pr.achieved_pl else "INVALID"
            band = (
                f"{pr.pfhd_band[0]:.0e} to {pr.pfhd_band[1]:.0e}" if pr.pfhd_band else "n/a"
            )
            lines.append(
                f"| {sf.ref} {sf.name} | {pr.category.value} | "
                f"{pr.mttfd_years:.1f} y ({pr.mttfd_band.value}) | "
                f"{pr.dc_avg:.1%} ({pr.dc_band.value}) | {pr.ccf_score} | "
                f"**{pl}** | {band} |"
            )
        lines.append("")
        lines.append("### 3.1 Derivations")
        lines.append("")
        for sf in sorted(self.project.safety_functions, key=lambda x: x.ref):
            pr = self.gate.pl_results.get(sf.ref)
            if pr is None:
                continue
            lines.append(f"**{sf.ref}: {sf.name}**")
            lines.append("")
            lines.append("```")
            lines.append(pr.explain())
            lines.append("```")
            lines.append("")
        return "\n".join(lines)

    def _field_sizing(self) -> str:
        if self.budget is None:
            return ""
        return (
            "## 4 Protective field sizing\n\n"
            "Derived from the latency and braking budget. `L_req` is the "
            "minimum protective-field length at each speed.\n\n"
            f"- Detection + reaction + communication latency: "
            f"{self.budget.total_latency_s*1000:.0f} ms\n"
            f"- Guaranteed minimum deceleration: {self.budget.a_brake_mps2:.2f} m/s²\n"
            f"- Device measurement tolerance: {self.budget.z_s_m*1000:.0f} mm\n"
            f"- Human approach term credited: "
            f"{'yes, K = 1.6 m/s over latency plus stopping time' if self.budget.human_approach else 'no'}\n"
            f"- Design margin: {self.budget.margin_m*1000:.0f} mm\n\n"
            "```\n" + FieldSizingTable(self.budget).render() + "\n```\n"
        )

    def _verification(self) -> str:
        lines = [
            "## 5 Verification results",
            "",
            f"Runner `{self.campaign.runner or 'unknown'}`. The tier column is the tier of "
            "the evidence that was produced, followed by the tier the test case declares.",
            "",
            "| Test case | Requirement(s) | Runs | Errors | Tier (evidence / declared) | Verdict | Worst margin | 2-way cov. |",
            "|---|---|---|---|---|---|---|---|",
        ]
        total_runs = 0
        for tc in sorted(self.project.test_cases, key=lambda x: x.ref):
            o = self.campaign.outcomes.get(tc.ref)
            if o is None:
                lines.append(
                    f"| {tc.ref} | {', '.join(tc.requirement_refs)} | 0 | 0 "
                    f"| none / {tc.required_tier.value} | NOT RUN | n/a | n/a |"
                )
                continue
            total_runs += o.n_executed
            margin = f"{o.worst_robustness:.4g}" if o.worst_robustness != float("inf") else "n/a"
            tiers = sorted({run.tier.value for run, _ in o.runs}) or ["none"]
            lines.append(
                f"| {tc.ref} | {', '.join(tc.requirement_refs)} | {o.n_executed} | {o.n_errors} "
                f"| {'+'.join(tiers)} / {tc.required_tier.value} | **{o.verdict.value.upper()}** "
                f"| {margin} | {o.coverage.get('two_way', 0):.0%} |"
            )
        lines.append("")
        lines.append(f"Total runs executed: **{total_runs}**.")
        lines.append("")
        lines.append(
            "Worst margin is the minimum STL robustness over all runs, in the unit of the "
            "criterion (metres, seconds or m/s). Negative means violated."
        )
        lines.append("")

        # Counterexamples get their own subsection: they are the most
        # important content in the document.
        fails = [
            (ref, o)
            for ref, o in self.campaign.outcomes.items()
            if o.verdict in (Verdict.FAIL, Verdict.FLAKY, Verdict.ERROR)
        ]
        if fails:
            lines.append("### 5.1 Counterexamples, errors and non-reproducible results")
            lines.append("")
            for ref, o in sorted(fails):
                tc = next((t for t in self.project.test_cases if t.ref == ref), None)
                n_fail = sum(1 for _, r in o.runs if r.verdict is Verdict.FAIL)
                lines.append(f"**{ref}**: {o.verdict.value}")
                lines.append("")
                if tc is not None:
                    lines.append(f"- Criterion: `{tc.criterion_stl}`")
                lines.append(
                    f"- Failing runs: {n_fail} of {o.n_executed}; errored runs: {o.n_errors}"
                )
                if o.worst_robustness != float("inf"):
                    lines.append(f"- Worst robustness: `{o.worst_robustness:.6g}`")
                if o.worst_assignment:
                    shown = {k: round(v, 4) for k, v in sorted(o.worst_assignment.items())}
                    lines.append(f"- Parameter assignment: `{shown}`")
                worst_run = min(
                    (r for r in o.runs if r[1].robustness is not None),
                    key=lambda r: r[1].robustness,
                    default=None,
                )
                if worst_run and worst_run[1].artifacts.get("trace"):
                    lines.append(
                        f"- Full-resolution trace (evidence blob): `{worst_run[1].artifacts['trace']}`"
                    )
                first_err = next((r for _, r in o.runs if r.verdict is Verdict.ERROR), None)
                if first_err is not None:
                    lines.append(f"- First error: `{first_err.message[:300]}`")
                lines.append("")
        return "\n".join(lines)

    def _findings(self) -> str:
        lines = ["## 6 Findings", ""]
        if not self.gate.findings:
            lines.append("No findings raised.")
            return "\n".join(lines) + "\n"
        lines += [
            "| Severity | Rule | Subject | Disposition | Detail |",
            "|---|---|---|---|---|",
        ]
        order = {"blocker": 0, "major": 1, "minor": 2, "info": 3}
        for f in sorted(self.gate.findings, key=lambda x: (order[x.severity], x.rule)):
            detail = f.detail.replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {f.severity} | {f.rule} | {f.subject} | {f.disposition} | {detail} |"
            )
        waived = [f for f in self.gate.findings if f.disposition == "waived"]
        if waived:
            lines += ["", "### 6.1 Waivers", ""]
            for f in waived:
                lines.append(
                    f"- **{f.rule} / {f.subject}**: {f.waiver_justification} "
                    f"(approved by {f.waiver_approver})"
                )
        return "\n".join(lines) + "\n"

    def _limitations(self) -> str:
        return textwrap.dedent(
            """\
            ## 7 Limitations of the method

            Stated explicitly so that the scope of the claims in this
            document is not overread.

            1. **Falsification, not proof.** The verification campaign
               searches the scenario parameter space for counterexamples
               using boundary sampling, low-discrepancy sweeps and
               robustness-guided optimisation. Absence of a counterexample
               is evidence, not proof, that none exists. No claim of
               exhaustive coverage of a continuous parameter space is made
               or implied.

            2. **Simulation is not physical verification.** Results
               obtained at MIL, SIL or replay tier support the design
               argument. They do not on their own discharge the
               verification obligations of EN ISO 3691-4, which are
               ultimately physical. The evidence tier of every result is
               recorded in section 5 and enforced by policy rule
               R-EXEC-002.

            3. **Model validity bounds the result.** A simulated result is
               only as good as the vehicle, sensor and environment models
               behind it. Model validation evidence is a separate
               obligation and is not contained in this document.

            4. **The simplified route for Performance Level.** Performance
               Levels in section 3 are determined via the simplified route
               of ISO 13849-1 Annex K. A full Markov analysis may yield a
               different PFHd. Where a certified subsystem PFHd is
               available from the component manufacturer, it takes
               precedence.

            5. **Parameter coverage is a proxy.** Two-way coverage
               measures how much of the declared parameter space was
               exercised. It says nothing about whether the declared space
               correctly bounds the intended operating conditions. That
               judgement is an input to this process, not an output.
            """
        )

    # ---- entry point ----------------------------------------------------

    def render(self) -> str:
        parts = [
            self._cover(),
            self._standards(),
            self._hazards(),
            self._safety_functions(),
            self._field_sizing(),
            self._verification(),
            self._findings(),
            self._limitations(),
        ]
        return "\n".join(p for p in parts if p)

    def write(self, path: str) -> str:
        text = self.render()
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        # The report itself becomes evidence, committed into the chain.
        dg = self.store.put_bytes(text.encode("utf-8"))
        cid = self.campaign.config.campaign_id
        self.store.append(
            {
                "campaign_id": cid,
                "type": "technical_file",
                "document_id": self.meta.document_id,
                "revision": self.meta.revision,
                "digest": dg,
                "merkle_root": self.store.campaign_root(cid),
                "gate_passed": self.gate.passed,
            }
        )
        return dg


def verify_report(store: EvidenceStore, path: str) -> list[str]:
    """Check a generated report against the evidence store.

    Recomputes the campaign Merkle root printed on the cover sheet, and
    checks that the report file is byte-identical to a technical file the
    store recorded when it was issued.
    """
    problems: list[str] = []
    with open(path, "rb") as fh:
        data = fh.read()
    text = data.decode("utf-8")
    m_c = re.search(r"\| Campaign \| `([^`]+)` \|", text)
    m_r = re.search(r"\| Merkle root \| `([0-9a-f]{64})` \|", text)
    if not (m_c and m_r):
        return ["report has no campaign id or Merkle root on its cover sheet"]
    cid, claimed = m_c.group(1), m_r.group(1)
    if cid not in store.campaign_ids():
        return [f"campaign {cid} is not in the evidence store"]
    actual = store.campaign_root(cid)
    if actual != claimed:
        problems.append(
            f"Merkle root mismatch for {cid}: report {claimed[:16]}..., store {actual[:16]}..."
        )
    dg = hashlib.sha256(data).hexdigest()
    issued = [
        e
        for e in store.entries()
        if e.payload.get("type") == "technical_file" and e.payload.get("campaign_id") == cid
    ]
    if not any(e.payload.get("digest") == dg for e in issued):
        problems.append(
            "report file does not match any technical file recorded for this campaign "
            "(edited after issue, or never issued from this store)"
        )
    return problems


__all__ = ["DocumentMeta", "TechnicalFile", "verify_report"]
