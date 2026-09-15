"""
safegate.cli
============

Command-line interface. Designed to be driven from CI, so every command
returns a meaningful exit code and writes machine-readable output
alongside the human-readable form.

    safegate validate  <project>                 # referential integrity
    safegate pl        <project>                 # PL determination only
    safegate fields    <project>                 # protective field table
    safegate run       <project> --store .evidence --build $(git rev-parse HEAD)
    safegate gate      <project> --store .evidence --policy policy.yaml
    safegate report    <project> --store .evidence -o V-and-V.md
    safegate verify    --store .evidence         # chain integrity only

Exit codes: 0 pass, 1 gate failure, 2 usage or load error.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .core.cas import EvidenceStore
from .core.loader import ProjectLoadError, load_project
from .execution.adapter import NullRunner, ReplayRunner, ScenarioExecutionRunner
from .iso13849.pl import evaluate_architecture
from .iso3691.metrics import FieldSizingTable, StoppingBudget
from .orchestrator.campaign import Campaign, CampaignConfig
from .policy.gate import Gate, PolicyConfig
from .report.technical_file import DocumentMeta, TechnicalFile

RUNNERS = {
    "null": lambda: NullRunner(),
    "scenario_execution": lambda: ScenarioExecutionRunner(),
}


def _budget_from(project_root: Path) -> StoppingBudget | None:
    import yaml

    p = project_root / "stopping_budget.yaml"
    if not p.exists():
        return None
    d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return StoppingBudget(
        t_detect_s=float(d.get("t_detect_s", 0.0)),
        t_react_s=float(d.get("t_react_s", 0.0)),
        t_comm_s=float(d.get("t_comm_s", 0.0)),
        a_brake_mps2=float(d.get("a_brake_mps2", 1.0)),
        z_s_m=float(d.get("z_s_m", 0.0)),
        human_approach=bool(d.get("human_approach", False)),
        margin_m=float(d.get("margin_m", 0.0)),
        clause_refs=tuple(d.get("clause_refs", [])),
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="safegate")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_project(sp):
        sp.add_argument("project", type=Path)

    p_val = sub.add_parser("validate", help="check referential integrity")
    add_project(p_val)

    p_pl = sub.add_parser("pl", help="determine Performance Levels")
    add_project(p_pl)

    p_f = sub.add_parser("fields", help="protective field sizing table")
    add_project(p_f)

    p_run = sub.add_parser("run", help="execute the verification campaign")
    add_project(p_run)
    p_run.add_argument("--store", type=Path, default=Path(".evidence"))
    p_run.add_argument("--runner", choices=sorted(RUNNERS), default="null")
    p_run.add_argument("--build", required=True, help="SUT build hash")
    p_run.add_argument("--config-hash", default="default")
    p_run.add_argument("--campaign", default=None)
    p_run.add_argument("--sweep", type=int, default=128)
    p_run.add_argument("--falsify", type=int, default=64)
    p_run.add_argument("--seed", type=int, default=20260914)
    p_run.add_argument("--only", nargs="*", default=None)
    p_run.add_argument("--json", type=Path, default=None)

    p_gate = sub.add_parser("gate", help="evaluate the release policy")
    add_project(p_gate)
    p_gate.add_argument("--store", type=Path, default=Path(".evidence"))
    p_gate.add_argument("--policy", type=Path, default=None)
    p_gate.add_argument("--campaign-json", type=Path, default=None)

    p_rep = sub.add_parser("report", help="generate the technical file section")
    add_project(p_rep)
    p_rep.add_argument("--store", type=Path, default=Path(".evidence"))
    p_rep.add_argument("--policy", type=Path, default=None)
    p_rep.add_argument("-o", "--out", type=Path, default=Path("V-and-V.md"))
    p_rep.add_argument("--manufacturer", default="")
    p_rep.add_argument("--author", default="")
    p_rep.add_argument("--doc-id", default="VV-001")
    p_rep.add_argument("--revision", default="A")

    p_ver = sub.add_parser("verify", help="verify evidence chain integrity")
    p_ver.add_argument("--store", type=Path, default=Path(".evidence"))

    args = ap.parse_args(argv)

    if args.cmd == "verify":
        store = EvidenceStore(args.store)
        problems = store.verify_chain(store.signer_id)
        if problems:
            print("EVIDENCE CHAIN COMPROMISED:")
            for p in problems:
                print(f"  - {p}")
            return 1
        seq, head = store.head()
        print(f"chain intact: {seq + 1} entries, head {head[:16]}…")
        return 0

    try:
        project = load_project(args.project)
    except ProjectLoadError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.cmd == "validate":
        print(
            f"{project.name}: {len(project.hazards)} hazards, "
            f"{len(project.requirements)} requirements, "
            f"{len(project.safety_functions)} safety functions, "
            f"{len(project.test_cases)} test cases — valid"
        )
        return 0

    if args.cmd == "pl":
        bad = 0
        for arch in project.architectures:
            r = evaluate_architecture(arch)
            print(r.explain())
            print()
            if not r.valid:
                bad += 1
        return 1 if bad else 0

    if args.cmd == "fields":
        b = _budget_from(args.project)
        if b is None:
            print("no stopping_budget.yaml in project", file=sys.stderr)
            return 2
        print(FieldSizingTable(b).render())
        return 0

    if args.cmd == "run":
        store = EvidenceStore(args.store)
        if store.signer_id is None:
            try:
                store.generate_key()
            except RuntimeError:
                print("warning: cryptography not installed; chain will be unsigned",
                      file=sys.stderr)
        cfg = CampaignConfig(
            campaign_id=args.campaign or f"{project.name}-{args.build[:12]}",
            sut_build_hash=args.build,
            config_hash=args.config_hash,
            sweep_budget=args.sweep,
            falsify_budget=args.falsify,
            seed=args.seed,
        )
        runner = RUNNERS[args.runner]()
        camp = Campaign(project, runner, store, cfg, progress=lambda m: print(m))
        res = camp.run(only=args.only)
        print()
        print(f"campaign {cfg.campaign_id}: merkle root {res.merkle_root[:16]}…")
        for ref, o in sorted(res.outcomes.items()):
            margin = (
                f"{o.worst_robustness:.4g}"
                if o.worst_robustness != float("inf")
                else "—"
            )
            print(
                f"  {ref:<16} {o.verdict.value.upper():<7} "
                f"runs={o.n_executed:<5} worst={margin:<12} "
                f"cov2={o.coverage.get('two_way', 0):.0%}"
            )
        if args.json:
            args.json.write_text(
                json.dumps(
                    {
                        "campaign_id": cfg.campaign_id,
                        "merkle_root": res.merkle_root,
                        "outcomes": {
                            ref: {
                                "verdict": o.verdict.value,
                                "worst_robustness": (
                                    None
                                    if o.worst_robustness == float("inf")
                                    else o.worst_robustness
                                ),
                                "worst_assignment": o.worst_assignment,
                                "n_executed": o.n_executed,
                                "coverage": o.coverage,
                            }
                            for ref, o in res.outcomes.items()
                        },
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        return 1 if res.failed else 0

    if args.cmd in ("gate", "report"):
        store = EvidenceStore(args.store)
        policy = (
            PolicyConfig.from_yaml(str(args.policy)) if args.policy else PolicyConfig()
        )
        camp = _rebuild_campaign(store, project)
        gate = Gate(project, camp, store, policy).evaluate()

        if args.cmd == "gate":
            print(gate.render())
            return 0 if gate.passed else 1

        meta = DocumentMeta(
            manufacturer=args.manufacturer or "—",
            product=f"{project.machine_type} / {project.variant}",
            document_id=args.doc_id,
            revision=args.revision,
            author=args.author or "—",
        )
        tf = TechnicalFile(
            project, camp, gate, store, meta, budget=_budget_from(args.project)
        )
        dg = tf.write(str(args.out))
        print(f"wrote {args.out} (digest {dg[:16]}…)")
        return 0 if gate.passed else 1

    return 2


def _rebuild_campaign(store: EvidenceStore, project):
    """Reconstruct campaign results from the evidence chain.

    Deliberately reads from the chain rather than from an in-memory object
    or a side-car JSON file: the gate and the report must be derived from
    the same tamper-evident record an assessor would examine, not from a
    convenience artefact that could drift from it.
    """
    from .orchestrator.campaign import CampaignConfig, CampaignResult, TestCaseOutcome
    from .core.model import ConcreteRun, RunResult, Verdict

    cfg = None
    outcomes: dict[str, TestCaseOutcome] = {}
    campaign_id = None
    for e in store.entries():
        p = e.payload
        t = p.get("type")
        if t == "campaign_start":
            campaign_id = p["campaign_id"]
            cfg = CampaignConfig(
                campaign_id=campaign_id,
                sut_build_hash=p.get("sut_build_hash", ""),
                config_hash=p.get("config_hash", ""),
            )
        elif t == "run":
            run = ConcreteRun.model_validate(p["run"])
            res = RunResult.model_validate(p["result"])
            o = outcomes.setdefault(
                run.test_case_ref, TestCaseOutcome(test_case_ref=run.test_case_ref)
            )
            o.runs.append((run, res))
            o.n_executed += 1
            if res.robustness is not None and res.robustness < o.worst_robustness:
                o.worst_robustness = res.robustness
                o.worst_assignment = dict(run.assignment)
            if res.verdict is Verdict.FAIL:
                o.falsified = True
        elif t == "test_case_summary":
            o = outcomes.setdefault(
                p["test_case_ref"], TestCaseOutcome(test_case_ref=p["test_case_ref"])
            )
            o.coverage = p.get("coverage", {})
    if cfg is None:
        cfg = CampaignConfig(campaign_id="unknown", sut_build_hash="", config_hash="")
    res = CampaignResult(config=cfg, outcomes=outcomes)
    res.merkle_root = store.campaign_root(cfg.campaign_id)
    return res


if __name__ == "__main__":
    raise SystemExit(main())
