"""Regenerate every number and report in docs/ from real runs.

    python scripts/reproduce.py [--workers 16] [--osc] [--ros2] [--s3 s3://bucket/prefix] [--out out]

Steps, all through the public CLI:
  1. keygen outside the evidence stores
  2. for T200 rev A and rev B: run (SIL), gate, report, anchor, verify
  3. --osc: rev B again through Intel scenario_execution; compare run by run
     --s3: rev A again with the evidence store in S3-compatible object storage
     --ros2: one test case through ROS 2 Jazzy (plant node, rosbag2, extraction)
  4. tamper demonstration on a copy of the rev A store
  5. micro-benchmarks
Then docs/RESULTS.md and docs/reports/*.md are written by make_results.py.
Nothing in docs/RESULTS.md is typed by hand.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VARIANTS = {
    "revA": ("examples/amr_project", "VV-T200", "A"),
    "revB": ("examples/amr_project_revb", "VV-T200", "B"),
}


def sg(*args: str, check_codes: tuple[int, ...] = (0, 1)) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "safegate.cli", *args]
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, check=False)
    if proc.returncode not in check_codes:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise SystemExit(f"command failed ({proc.returncode}): {' '.join(args)}")
    return proc


def run_variant(name: str, out: Path, workers: int, key: Path, pub: str, runner: str = "sil") -> dict:
    project, doc_id, rev = VARIANTS[name]
    tag = name if runner == "sil" else f"{name}-{runner}"
    vdir = out / tag
    if vdir.exists():
        shutil.rmtree(vdir)
    vdir.mkdir(parents=True)
    store = vdir / "evidence"
    t0 = time.perf_counter()
    run = sg("run", project, "--store", str(store), "--runner", runner,
             "--build", f"example-build-{name}", "--campaign", f"T200-{tag}",
             "--workers", str(workers), "--signing-key", str(key),
             "--json", str(vdir / "campaign.json"))
    wall = time.perf_counter() - t0
    policy = f"{project}/policy.yaml"
    gate = sg("gate", project, "--store", str(store), "--policy", policy,
              "--trusted-key", pub, "--json", str(vdir / "gate.json"))
    report = vdir / "V-and-V.md"
    sg("report", project, "--store", str(store), "--policy", policy, "--trusted-key", pub,
       "-o", str(report), "--doc-id", doc_id, "--revision", rev,
       "--manufacturer", "Example Intralogistics GmbH (fictional)", "--author", "P. Yadava",
       "--signing-key", str(key))
    sg("anchor", "--store", str(store), "-o", str(vdir / "anchor.json"))
    verify = sg("verify", "--store", str(store), "--trusted-key", pub,
                "--anchor", str(vdir / "anchor.json"), "--report", str(report))
    summary = {
        "variant": name,
        "runner": runner,
        "project": project,
        "run_exit": run.returncode,
        "gate_exit": gate.returncode,
        "verify_exit": verify.returncode,
        "verify_output": verify.stdout.strip(),
        "cli_wall_s": wall,
        "workers": workers,
    }
    (vdir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(f"{tag}: run exit {run.returncode}, gate exit {gate.returncode}, "
          f"verify exit {verify.returncode}, {wall:.1f} s")
    return summary


def manifest(store: Path) -> list[dict]:
    return [json.loads(line) for line in (store / "manifest.log").read_text().splitlines() if line]


def compare_runners(out: Path) -> dict:
    """Run-by-run comparison of the SIL and scenario_execution stores."""

    def index(store: Path) -> dict:
        runs = {}
        for e in manifest(store):
            p = e["payload"]
            if p.get("type") != "run":
                continue
            key = (p["run"]["test_case_ref"], json.dumps(p["run"]["assignment"], sort_keys=True),
                   p["run"]["pinning"]["seed"])
            runs[key] = (p["result"]["verdict"], p["result"]["robustness"])
        return runs

    a = index(out / "revB" / "evidence")
    b = index(out / "revB-scenario_execution" / "evidence")
    common = set(a) & set(b)
    same_verdict = sum(1 for k in common if a[k][0] == b[k][0])
    exact = sum(1 for k in common if a[k][1] == b[k][1])
    max_diff = max(
        (abs(a[k][1] - b[k][1]) for k in common if a[k][1] is not None and b[k][1] is not None),
        default=0.0,
    )
    result = {"runs_sil": len(a), "runs_osc": len(b), "matched_points": len(common),
              "same_verdict": same_verdict, "bit_identical_robustness": exact,
              "max_abs_robustness_diff": max_diff}
    (out / "cross_runner.json").write_text(json.dumps(result, indent=2))
    print("cross-runner:", result)
    return result


def object_storage(out: Path, uri: str, key: Path, pub: str) -> dict:
    """Rev A again with the evidence in object storage; compare with the filesystem run."""
    project = VARIANTS["revA"][0]
    location = f"{uri.rstrip('/')}/T200-revA-{int(time.time())}"
    work = out / "s3"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    t0 = time.perf_counter()
    sg("run", project, "--store", location, "--build", "example-build-revA",
       "--campaign", "T200-revA", "--workers", "16", "--signing-key", str(key),
       "--json", str(work / "campaign.json"))
    run_s = time.perf_counter() - t0
    sg("report", project, "--store", location, "--policy", f"{project}/policy.yaml",
       "--trusted-key", pub, "--signing-key", str(key), "-o", str(work / "V-and-V.md"))
    sg("anchor", "--store", location, "-o", str(work / "anchor.json"), check_codes=(0,))
    t1 = time.perf_counter()
    verify = sg("verify", "--store", location, "--trusted-key", pub, "--anchor",
                str(work / "anchor.json"), "--report", str(work / "V-and-V.md"))
    verify_s = time.perf_counter() - t1
    fs = json.loads((out / "revA" / "campaign.json").read_text())["outcomes"]
    ob = json.loads((work / "campaign.json").read_text())["outcomes"]
    same = all(
        (fs[k]["verdict"], fs[k]["n_executed"], fs[k]["n_fail"], fs[k]["worst_robustness"])
        == (ob[k]["verdict"], ob[k]["n_executed"], ob[k]["n_fail"], ob[k]["worst_robustness"])
        for k in fs
    )
    entries = int(verify.stdout.split("chain intact: ")[1].split(" entries")[0]) if verify.returncode == 0 else None
    res = {"location_scheme": location.split("://")[0], "entries": entries,
           "outcomes_identical_to_filesystem": same, "run_cli_s": run_s,
           "verify_exit": verify.returncode, "verify_cli_s": verify_s}
    (out / "s3.json").write_text(json.dumps(res, indent=2))
    print("object storage:", res)
    return res


def ros2_campaign(out: Path, key: Path) -> dict:
    """Rev B TC-CROSS-001 through ROS 2 (plant node, rosbag2, extraction) against SIL."""
    project = VARIANTS["revB"][0]
    budgets = ["--only", "TC-CROSS-001", "--boundary", "8", "--sweep", "16", "--falsify", "0"]
    res: dict = {}
    for runner, workers in (("sil", "16"), ("ros2", "8")):
        work = out / f"ros2-{runner}"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir()
        t0 = time.perf_counter()
        sg("run", project, "--store", str(work / "evidence"), "--runner", runner,
           "--build", "example-build-revB", "--campaign", f"T200-revB-{runner}",
           "--workers", workers, "--signing-key", str(key), *budgets,
           "--json", str(work / "campaign.json"))
        res[f"{runner}_wall_s"] = time.perf_counter() - t0

    def index(store: Path, first_only: bool) -> dict:
        runs: dict = {}
        for e in manifest(store):
            p = e["payload"]
            if p.get("type") != "run" or (first_only and p.get("repeat", 0) > 0):
                continue
            key_ = json.dumps(p["run"]["assignment"], sort_keys=True)
            runs.setdefault(key_, []).append((p["result"]["verdict"], p["result"]["robustness"]))
        return runs

    sil = index(out / "ros2-sil" / "evidence", True)
    ros = index(out / "ros2-ros2" / "evidence", False)
    c = json.loads((out / "ros2-ros2" / "campaign.json").read_text())["outcomes"]["TC-CROSS-001"]
    res.update({
        "points": len(ros),
        "ros2_executions": sum(len(v) for v in ros.values()),
        "executions_identical_to_sil": sum(1 for k, v in ros.items() for r in v if r == sil[k][0]),
        "ros2_verdict": c["verdict"],
        "worst_robustness": c["worst_robustness"],
    })
    (out / "ros2.json").write_text(json.dumps(res, indent=2))
    print("ros2:", res)
    return res


def tamper_demo(out: Path, pub: str) -> dict:
    src = out / "revA" / "evidence"
    report = out / "revA" / "V-and-V.md"
    anchor = out / "revA" / "anchor.json"
    work = out / "tamper"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    res: dict = {}

    def copy(name: str) -> Path:
        dst = work / name
        shutil.copytree(src, dst)
        for p in dst.rglob("*"):
            p.chmod(0o755 if p.is_dir() else 0o644)
        return dst

    def verify(store: Path, *extra: str) -> tuple[int, list[str], int]:
        proc = sg("verify", "--store", str(store), *extra)
        lines = [ln.strip()[2:] for ln in proc.stdout.splitlines() if ln.strip().startswith("- ")]
        total = 0
        for ln in proc.stdout.splitlines():
            if ln.startswith("EVIDENCE CHAIN COMPROMISED:"):
                total = int(ln.split(":")[1].split()[0])
        return proc.returncode, lines, total

    entries = manifest(src)
    failing = [e for e in entries if e["payload"].get("type") == "run"
               and e["payload"]["result"]["verdict"] == "fail"]
    res["entries"] = len(entries)
    res["failing_runs"] = len(failing)

    # 1. delete the failing runs
    s1 = copy("deleted")
    keep = [e for e in entries if e not in failing]
    (s1 / "manifest.log").write_text("\n".join(json.dumps(e, sort_keys=True, separators=(",", ":")) for e in keep) + "\n")
    code, lines, total = verify(s1, "--trusted-key", pub)
    res["delete_failing_runs"] = {"exit": code, "problems": total, "first": lines[:3]}

    # 2. flip one verdict in place
    s2 = copy("edited")
    edited = [dict(e) for e in entries]
    idx = entries.index(failing[0])
    edited[idx] = json.loads(json.dumps(entries[idx]))
    edited[idx]["payload"]["result"]["verdict"] = "pass"
    (s2 / "manifest.log").write_text("\n".join(json.dumps(e, sort_keys=True, separators=(",", ":")) for e in edited) + "\n")
    code, lines, total = verify(s2, "--trusted-key", pub)
    res["flip_one_verdict"] = {"exit": code, "problems": total, "first": lines[:3]}

    # 3. rewrite the whole chain without the failing runs and re-sign it
    s3 = copy("rewritten")
    (s3 / "manifest.log").unlink()
    subprocess.run(
        [sys.executable, "-c",
         "import json,sys\n"
         "from safegate.core.cas import EvidenceStore\n"
         "s=EvidenceStore(sys.argv[1]); s.generate_key()\n"
         "for line in open(sys.argv[2]):\n"
         "    p=json.loads(line)['payload']\n"
         "    if p.get('type')=='run' and p['result']['verdict']=='fail': continue\n"
         "    s.append(p)\n",
         str(s3), str(src / "manifest.log")],
        cwd=REPO, check=True, capture_output=True, text=True,
    )
    c_self, _, _ = verify(s3)
    c_key, _, n_key = verify(s3, "--trusted-key", pub)
    c_anchor, l_anchor, _ = verify(s3, "--anchor", str(anchor))
    res["rewrite_and_resign"] = {
        "verify_without_trusted_key_exit": c_self,
        "verify_with_trusted_key_exit": c_key,
        "trusted_key_problems": n_key,
        "verify_with_anchor_exit": c_anchor,
        "anchor_first": l_anchor[:2],
    }

    # 4. edit the issued report
    forged = work / "V-and-V-edited.md"
    forged.write_text(report.read_text().replace("**NON-CONFORMING**", "**CONFORMING**"))
    code, lines, _ = verify(src, "--report", str(forged))
    res["edit_report"] = {"exit": code, "first": lines[:2]}

    (out / "tamper.json").write_text(json.dumps(res, indent=2))
    print("tamper:", json.dumps({k: v for k, v in res.items() if k != "first"})[:400])
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("out"))
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--osc", action="store_true", help="also run rev B through scenario_execution")
    ap.add_argument("--skip-bench", action="store_true")
    ap.add_argument("--ros2", action="store_true",
                    help="also run TC-CROSS-001 through ROS 2 (needs the safegate-ros:jazzy image)")
    ap.add_argument("--s3", default=None, metavar="s3://bucket/prefix",
                    help="also run rev A with the evidence in object storage (AWS_* env)")
    args = ap.parse_args()
    out = args.out.resolve()
    os.chdir(REPO)  # every path below is relative to the repository
    if out.is_relative_to(REPO):
        out = out.relative_to(REPO)  # keeps absolute paths out of generated docs
    (REPO / out).mkdir(parents=True, exist_ok=True)

    keys = out / "keys"
    if keys.exists():
        shutil.rmtree(keys)
    proc = sg("keygen", str(keys), check_codes=(0,))
    pub = proc.stdout.strip().split("public key: ")[-1]
    (out / "trusted_signers.txt").write_text(pub + "\n")

    for name in VARIANTS:
        run_variant(name, out, args.workers, keys / "signing.key", pub)
    if args.osc:
        run_variant("revB", out, max(args.workers, 32), keys / "signing.key", pub,
                    runner="scenario_execution")
        compare_runners(out)
    if args.s3:
        object_storage(out, args.s3, keys / "signing.key", pub)
    if args.ros2:
        ros2_campaign(out, keys / "signing.key")
    tamper_demo(out, pub)
    if not args.skip_bench:
        subprocess.run([sys.executable, str(REPO / "scripts" / "bench.py"), "--out",
                        str(out / "bench.json")], cwd=REPO, check=True)
    subprocess.run([sys.executable, str(REPO / "scripts" / "make_results.py"), "--out", str(out)],
                   cwd=REPO, check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
