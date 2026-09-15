"""
safegate.cli
============

Command-line interface. Designed to be driven from CI, so every command
returns a meaningful exit code and can write machine-readable output
alongside the human-readable form.

    safegate validate <project>                  referential integrity
    safegate pl       <project>                  PL determination only
    safegate fields   <project>                  protective field table
    safegate keygen   <dir>                      Ed25519 signing key pair
    safegate run      <project> --store .evidence --build $(git rev-parse HEAD)
    safegate gate     <project> --store .evidence --policy policy.yaml
    safegate report   <project> --store .evidence -o V-and-V.md
    safegate anchor   --store .evidence -o anchor.json
    safegate verify   --store .evidence [--report V-and-V.md] [--anchor anchor.json]

Exit codes: 0 pass, 1 gate or verification failure, 2 usage or load error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

from .core.cas import EvidenceStore, write_keypair
from .core.loader import ProjectLoadError, load_project
from .iso3691.derived import DerivedSignals
from .iso3691.metrics import FieldSizingTable, StoppingBudget
from .iso13849.pl import evaluate_architecture
from .orchestrator.campaign import Campaign, CampaignConfig, CampaignNotFound, load_campaign
from .policy.gate import Gate, PolicyConfig
from .report.technical_file import DocumentMeta, TechnicalFile, verify_report

RUNNERS = ("sil", "scenario_execution", "ros2", "hil", "null")


def _budget_from(project_root: Path) -> StoppingBudget | None:
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


def _read_keys(values: list[str] | None) -> list[str]:
    """Trusted keys given as hex or as a path to a file holding hex keys."""
    out: list[str] = []
    for v in values or []:
        p = Path(v)
        text = p.read_text(encoding="utf-8") if p.exists() else v
        out += [k.strip().lower() for k in text.split() if k.strip()]
    return out


def _make_runner(args: argparse.Namespace, project_root: Path):
    sut = args.sut or (project_root / "sut_config.yaml")
    if args.runner == "sil":
        from .sim.runner import SilRunner

        return SilRunner(sut)
    if args.runner == "scenario_execution":
        from .execution.adapter import ScenarioExecutionRunner

        return ScenarioExecutionRunner(project_root, sut_config=sut)
    if args.runner == "ros2":
        from .execution.adapter import Ros2Runner

        return Ros2Runner(sut, mapping=args.bag_mapping, image=args.ros_image)
    if args.runner == "hil":
        from .execution.adapter import HilRunner

        if not args.rig_url:
            raise ValueError("--rig-url is required for the hil runner")
        return HilRunner(
            args.rig_url,
            expected_scanner_config_checksum=args.scanner_checksum,
            allow_emulated=args.allow_emulated_rig,
        )
    from .execution.adapter import NullRunner

    return NullRunner()


def _add_project(sp: argparse.ArgumentParser) -> None:
    sp.add_argument("project", type=Path)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="safegate")
    sub = ap.add_subparsers(dest="cmd", required=True)

    _add_project(sub.add_parser("validate", help="check referential integrity"))
    _add_project(sub.add_parser("pl", help="determine Performance Levels"))
    _add_project(sub.add_parser("fields", help="protective field sizing table"))

    p_key = sub.add_parser("keygen", help="write an Ed25519 signing key pair")
    p_key.add_argument("directory", type=Path)

    p_run = sub.add_parser("run", help="execute the verification campaign")
    _add_project(p_run)
    p_run.add_argument("--store", default=".evidence",
                       help="directory, or s3://bucket/prefix")
    p_run.add_argument("--runner", choices=RUNNERS, default="sil")
    p_run.add_argument("--sut", type=Path, default=None,
                       help="SUT configuration (default: <project>/sut_config.yaml)")
    p_run.add_argument("--build", default=None,
                       help="SUT build hash (a HIL rig supplies its own firmware digest)")
    p_run.add_argument("--config-hash", default=None,
                       help="default: digest of the SUT configuration the runner reports")
    p_run.add_argument("--campaign", default=None)
    p_run.add_argument("--boundary", type=int, default=24)
    p_run.add_argument("--sweep", type=int, default=128)
    p_run.add_argument("--falsify", type=int, default=64)
    p_run.add_argument("--seed", type=int, default=20260914)
    p_run.add_argument("--workers", type=int, default=1)
    p_run.add_argument("--only", nargs="*", default=None)
    p_run.add_argument("--json", type=Path, default=None)
    p_run.add_argument("--signing-key", type=Path,
                       default=os.environ.get("SAFEGATE_SIGNING_KEY") or None,
                       help="Ed25519 private key kept outside the store "
                            "(default: $SAFEGATE_SIGNING_KEY, else an in-store key)")
    p_run.add_argument("--ros-image", default="safegate-ros:jazzy",
                       help="container for --runner ros2 (docker/ros2/Dockerfile)")
    p_run.add_argument("--bag-mapping", type=Path, default=None,
                       help="rosbag2 topic-to-signal mapping for --runner ros2")
    p_run.add_argument("--rig-url", default=None)
    p_run.add_argument("--scanner-checksum", default=None)
    p_run.add_argument("--allow-emulated-rig", action="store_true")

    for name, helptext in (("gate", "evaluate the release policy"),
                           ("report", "generate the technical file section")):
        sp = sub.add_parser(name, help=helptext)
        _add_project(sp)
        sp.add_argument("--store", default=".evidence",
                       help="directory, or s3://bucket/prefix")
        sp.add_argument("--policy", type=Path, default=None)
        sp.add_argument("--campaign", default=None, help="default: latest in the store")
        sp.add_argument("--trusted-key", action="append", default=None,
                        help="trusted signer public key (hex or file); adds to the policy")
        sp.add_argument("--anchor", action="append", type=Path, default=None)
        sp.add_argument("--expected-build", default=None)
        if name == "gate":
            sp.add_argument("--json", type=Path, default=None)
        else:
            sp.add_argument("-o", "--out", type=Path, default=Path("V-and-V.md"))
            sp.add_argument("--manufacturer", default="")
            sp.add_argument("--author", default="")
            sp.add_argument("--approver", default="")
            sp.add_argument("--doc-id", default="VV-001")
            sp.add_argument("--revision", default="A")
            sp.add_argument("--signing-key", type=Path,
                            default=os.environ.get("SAFEGATE_SIGNING_KEY") or None,
                            help="key that signs the technical-file entry this command appends")

    p_anc = sub.add_parser("anchor", help="write a publishable commitment to the chain head")
    p_anc.add_argument("--store", default=".evidence",
                       help="directory, or s3://bucket/prefix")
    p_anc.add_argument("--campaign", default=None)
    p_anc.add_argument("-o", "--out", type=Path, required=True)

    p_ver = sub.add_parser("verify", help="verify evidence chain integrity")
    p_ver.add_argument("--store", default=".evidence",
                       help="directory, or s3://bucket/prefix")
    p_ver.add_argument("--trusted-key", action="append", default=None)
    p_ver.add_argument("--anchor", action="append", type=Path, default=None)
    p_ver.add_argument("--report", type=Path, default=None)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    if args.cmd == "keygen":
        pub = write_keypair(args.directory)
        print(f"wrote {args.directory}/signing.key (keep it out of the evidence store)")
        print(f"public key: {pub}")
        return 0

    if args.cmd == "verify":
        return _verify(args)

    if args.cmd == "anchor":
        store = EvidenceStore(args.store)
        ids = store.campaign_ids()
        cid = args.campaign or (ids[-1] if ids else None)
        if cid is None:
            print("no campaign in store", file=sys.stderr)
            return 2
        anchor = store.anchor(cid)
        args.out.write_text(json.dumps(anchor, indent=2) + "\n", encoding="utf-8")
        print(f"anchored {cid}: head seq {anchor['head_seq']}, root {anchor['merkle_root'][:16]}...")
        return 0

    try:
        project = load_project(args.project)
    except (ProjectLoadError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.cmd == "validate":
        print(
            f"{project.name}: {len(project.hazards)} hazards, "
            f"{len(project.requirements)} requirements, "
            f"{len(project.safety_functions)} safety functions, "
            f"{len(project.test_cases)} test cases - valid"
        )
        return 0

    if args.cmd == "pl":
        allocated = {sf.architecture_ref for sf in project.safety_functions}
        bad = 0
        for arch in project.architectures:
            r = evaluate_architecture(arch)
            print(r.explain())
            if arch.ref not in allocated:
                print("  (not allocated to any safety function; not counted in the exit code)")
            elif not r.valid:
                bad += 1
            print()
        return 1 if bad else 0

    if args.cmd == "fields":
        b = _budget_from(args.project)
        if b is None:
            print("no stopping_budget.yaml in project", file=sys.stderr)
            return 2
        print(FieldSizingTable(b).render())
        return 0

    if args.cmd == "run":
        return _run(args, project)

    return _gate_or_report(args, project)


def _run(args: argparse.Namespace, project) -> int:
    try:
        runner = _make_runner(args, args.project)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"cannot start runner {args.runner}: {exc}", file=sys.stderr)
        return 2
    rig_build = getattr(runner, "sut_build_hash", None)
    if rig_build and args.build and args.build != rig_build:
        print(f"--build {args.build} does not match the firmware on the rig ({rig_build}); "
              "refusing to file evidence under a build that did not run", file=sys.stderr)
        return 2
    args.build = args.build or rig_build
    if not args.build:
        print("--build is required for this runner", file=sys.stderr)
        return 2
    store = EvidenceStore(args.store, signing_key=args.signing_key)
    if store.signer_id is None:
        try:
            store.generate_key()
            print("note: no --signing-key given; generated an in-store key "
                  "(signatures are self-attested)", file=sys.stderr)
        except RuntimeError:
            print("warning: cryptography not installed; chain will be unsigned",
                  file=sys.stderr)
    config_hash = args.config_hash or getattr(runner, "config_hash", None) or "unspecified"
    cfg = CampaignConfig(
        campaign_id=args.campaign or f"{project.name}-{args.build[:12]}",
        sut_build_hash=args.build,
        config_hash=config_hash,
        boundary_budget=args.boundary,
        sweep_budget=args.sweep,
        falsify_budget=args.falsify,
        seed=args.seed,
        workers=args.workers,
    )
    if cfg.campaign_id in store.campaign_ids():
        print(f"campaign {cfg.campaign_id!r} already in the store; pass --campaign",
              file=sys.stderr)
        return 2
    derive = DerivedSignals(budget=_budget_from(args.project))
    camp = Campaign(project, runner, store, cfg, progress=print, derive=derive)
    res = camp.run(only=args.only)
    print()
    print(f"campaign {cfg.campaign_id}: merkle root {res.merkle_root[:16]}... "
          f"({res.finished_at - res.started_at:.1f} s)")
    for ref, o in sorted(res.outcomes.items()):
        margin = f"{o.worst_robustness:.4g}" if o.worst_robustness != float("inf") else "n/a"
        print(
            f"  {ref:<16} {o.verdict.value.upper():<7} "
            f"runs={o.n_executed:<5} errors={o.n_errors:<3} worst={margin:<12} "
            f"cov2={o.coverage.get('two_way', 0):.0%}"
        )
    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "campaign_id": cfg.campaign_id,
                    "merkle_root": res.merkle_root,
                    "runner": res.runner,
                    "wall_time_s": res.finished_at - res.started_at,
                    "outcomes": {
                        ref: {
                            "verdict": o.verdict.value,
                            "worst_robustness": (
                                None if o.worst_robustness == float("inf") else o.worst_robustness
                            ),
                            "worst_assignment": o.worst_assignment,
                            "n_executed": o.n_executed,
                            "n_errors": o.n_errors,
                            "n_fail": sum(1 for _, r in o.runs if r.verdict.value == "fail"),
                            "coverage": o.coverage,
                            "wall_time_s": o.wall_time_s,
                        }
                        for ref, o in res.outcomes.items()
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    return 1 if res.failed else 0


def _gate_or_report(args: argparse.Namespace, project) -> int:
    store = EvidenceStore(args.store, signing_key=getattr(args, "signing_key", None))
    policy = PolicyConfig.from_yaml(str(args.policy)) if args.policy else PolicyConfig()
    policy.trusted_signers = sorted(set(policy.trusted_signers) | set(_read_keys(args.trusted_key)))
    try:
        anchors = [json.loads(p.read_text(encoding="utf-8")) for p in args.anchor or []]
    except (OSError, ValueError) as exc:
        print(f"cannot read anchor: {exc}", file=sys.stderr)
        return 2
    try:
        camp = load_campaign(store, args.campaign)
    except CampaignNotFound as exc:
        print(str(exc), file=sys.stderr)
        return 2
    gate = Gate(project, camp, store, policy, anchors=anchors,
                expected_build=args.expected_build).evaluate()

    if args.cmd == "gate":
        print(f"campaign {camp.config.campaign_id} (build {camp.config.sut_build_hash[:12]})")
        print(gate.render())
        if args.json:
            args.json.write_text(
                json.dumps(
                    {
                        "campaign_id": camp.config.campaign_id,
                        "passed": gate.passed,
                        "findings": [
                            f.model_dump(mode="json", exclude={"created_at", "labels", "supersedes"})
                            for f in gate.findings
                        ],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        return 0 if gate.passed else 1

    meta = DocumentMeta(
        manufacturer=args.manufacturer or "not stated",
        product=f"{project.machine_type} / {project.variant}",
        document_id=args.doc_id,
        revision=args.revision,
        author=args.author or "not stated",
        approver=args.approver,
    )
    tf = TechnicalFile(project, camp, gate, store, meta, budget=_budget_from(args.project))
    dg = tf.write(str(args.out))
    print(f"wrote {args.out} (digest {dg[:16]}...)")
    return 0 if gate.passed else 1


def _verify(args: argparse.Namespace) -> int:
    store = EvidenceStore(args.store)
    trusted = _read_keys(args.trusted_key)
    problems = store.verify_chain(trusted_keys=trusted)
    for path in args.anchor or []:
        try:
            anchor = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"cannot read anchor {path}: {exc}", file=sys.stderr)
            return 2
        problems += [f"anchor {path}: {p}" for p in store.verify_anchor(anchor)]
    if args.report:
        if not args.report.exists():
            print(f"report {args.report} not found", file=sys.stderr)
            return 2
        problems += [f"report {args.report}: {p}" for p in verify_report(store, str(args.report))]
    if problems:
        print(f"EVIDENCE CHAIN COMPROMISED: {len(problems)} problem(s)")
        for p in problems[:50]:
            print(f"  - {p}")
        if len(problems) > 50:
            print(f"  ... {len(problems) - 50} more")
        return 1
    seq, head = store.head()
    trust = f"{len(trusted)} trusted key(s)" if trusted else "no trusted key given (self-attested)"
    print(f"chain intact: {seq + 1} entries, head {head[:16]}..., signatures: {trust}")
    if args.report:
        print(f"report {args.report}: Merkle root and file digest match the store")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
