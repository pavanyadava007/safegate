"""
safegate.report.technical_file
==============================

Generates the verification and validation section of the technical
documentation, as a pure function of the evidence graph.

The single design rule: nothing in this file is hand-written. If a number
appears in the report, it was computed from evidence that is content-
addressed and chained. Hand-editing the report is the failure mode this
whole system exists to eliminate — it is how a document ends up claiming
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
import textwrap
from dataclasses import dataclass
from typing import Iterable

from ..core.cas import EvidenceStore
from ..core.model import Project, Verdict
from ..iso13849.pl import PLResult
from ..iso3691.metrics import FieldSizingTable, StoppingBudget
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
        anchor = self.store.anchor(self.campaign.config.campaign_id)
        status = "CONFORMING" if self.gate.passed else "NON-CONFORMING"
        nb = self.meta.notified_body or "not engaged"
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
            | Issued | {_dt.datetime.now(_dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} |
            | Author | {self.meta.author} |
            | Approver | {self.meta.approver or '— not approved —'} |
            | Notified Body | {nb} |
            | **Gate status** | **{status}** |

            **Evidence commitment**

            | | |
            |---|---|
            | Campaign | `{anchor['campaign_id']}` |
            | Merkle root | `{anchor['merkle_root']}` |
            | Manifest head | `{anchor['head']}` |
            | SUT build | `{self.campaign.config.sut_build_hash}` |
            | Configuration | `{self.campaign.config.config_hash}` |

            Any party holding this document and the evidence store can
            recompute the Merkle root and confirm that no test record has
            been added, removed or altered since issue.
            """
        )

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
                f"| {h.ref} | {h.title} | {h.zone or '—'} | {h.severity.value} "
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
                lines.append(f"| {sf.ref} | — | — | — | — | not determined | — |")
                continue
            pl = pr.achieved_pl.value if pr.achieved_pl else "INVALID"
            band = (
                f"{pr.pfhd_band[0]:.0e} – {pr.pfhd_band[1]:.0e}" if pr.pfhd_band else "—"
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
            lines.append(f"**{sf.ref} — {sf.name}**")
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
            f"{'yes (K = 1.6 m/s)' if self.budget.human_approach else 'no'}\n"
            f"- Design margin: {self.budget.margin_m*1000:.0f} mm\n\n"
            "```\n" + FieldSizingTable(self.budget).render() + "\n```\n"
        )

    def _verification(self) -> str:
        lines = [
            "## 5 Verification results",
            "",
            "| Test case | Requirement(s) | Runs | Tier | Verdict | Worst margin | 2-way cov. |",
            "|---|---|---|---|---|---|---|",
        ]
        total_runs = 0
        for tc in sorted(self.project.test_cases, key=lambda x: x.ref):
            o = self.campaign.outcomes.get(tc.ref)
            if o is None:
                lines.append(f"| {tc.ref} | {', '.join(tc.requirement_refs)} | 0 | — | NOT RUN | — | — |")
                continue
            total_runs += o.n_executed
            margin = (
                f"{o.worst_robustness:.4g}"
                if o.worst_robustness != float("inf")
                else "—"
            )
            lines.append(
                f"| {tc.ref} | {', '.join(tc.requirement_refs)} | {o.n_executed} "
                f"| {tc.required_tier.value} | **{o.verdict.value.upper()}** "
                f"| {margin} | {o.coverage.get('two_way', 0):.0%} |"
            )
        lines.append("")
        lines.append(f"Total runs executed: **{total_runs}**.")
        lines.append("")

        # Counterexamples get their own subsection — they are the most
        # important content in the document.
        fails = [
            (ref, o)
            for ref, o in self.campaign.outcomes.items()
            if o.verdict in (Verdict.FAIL, Verdict.FLAKY)
        ]
        if fails:
            lines.append("### 5.1 Counterexamples and non-reproducible results")
            lines.append("")
            for ref, o in sorted(fails):
                lines.append(f"**{ref}** — {o.verdict.value}")
                lines.append("")
                lines.append(f"- Worst robustness: `{o.worst_robustness:.6g}`")
                lines.append(f"- Parameter assignment: `{o.worst_assignment}`")
                worst_run = min(
                    (r for r in o.runs if r[1].robustness is not None),
                    key=lambda r: r[1].robustness,
                    default=None,
                )
                if worst_run and worst_run[1].artifacts.get("trace"):
                    lines.append(
                        f"- Full-resolution trace: `{worst_run[1].artifacts['trace']}`"
                    )
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
                    f"- **{f.rule} / {f.subject}** — {f.waiver_justification} "
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
        self.store.append(
            {
                "campaign_id": self.campaign.config.campaign_id,
                "type": "technical_file",
                "document_id": self.meta.document_id,
                "revision": self.meta.revision,
                "digest": dg,
                "gate_passed": self.gate.passed,
            }
        )
        return dg


__all__ = ["DocumentMeta", "TechnicalFile"]
