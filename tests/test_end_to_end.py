"""The whole pipeline: load -> run -> gate -> report -> verify.

Each regression test here reproduces a defect found in SafeGate 0.3.0 and
fails on that version.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from safegate.cli import _budget_from
from safegate.core.cas import EvidenceStore
from safegate.core.loader import load_project
from safegate.iso3691 import DerivedSignals
from safegate.orchestrator.campaign import Campaign, CampaignConfig, load_campaign
from safegate.policy.gate import Gate, PolicyConfig
from safegate.report.technical_file import DocumentMeta, TechnicalFile, verify_report
from safegate.sim import SilRunner

ROOT = Path(__file__).resolve().parents[1] / "examples" / "amr_project"


def small(campaign_id="t", **kw):
    base = dict(campaign_id=campaign_id, sut_build_hash="deadbeef", config_hash="cfg1",
                boundary_budget=16, sweep_budget=16, falsify_budget=12, seed=7)
    base.update(kw)
    return CampaignConfig(**base)


def campaign(project_root, store, cfg, only=None):
    project = load_project(project_root)
    runner = SilRunner(project_root / "sut_config.yaml")
    derive = DerivedSignals(budget=_budget_from(project_root))
    return project, Campaign(project, runner, store, cfg, derive=derive).run(only=only)


@pytest.fixture(scope="module")
def artefacts(tmp_path_factory):
    store = EvidenceStore(tmp_path_factory.mktemp("ev"))
    store.generate_key()
    project, result = campaign(ROOT, store, small())
    gate = Gate(project, result, store, PolicyConfig()).evaluate()
    return project, result, gate, store


def test_project_loads_and_validates():
    p = load_project(ROOT)
    assert len(p.hazards) == 4
    assert len(p.test_cases) == 5


def test_loader_rejects_dangling_refs(tmp_path):
    from safegate.core.loader import ProjectLoadError
    (tmp_path / "project.yaml").write_text("name: x\n")
    (tmp_path / "requirements.yaml").write_text(
        "requirements:\n  - {ref: R1, statement: s, hazards: [NOPE]}\n")
    with pytest.raises(ProjectLoadError, match="unknown hazard"):
        load_project(tmp_path)


def test_loader_rejects_missing_scenario_and_bad_stl(tmp_path):
    from safegate.core.loader import ProjectLoadError
    (tmp_path / "test_cases.yaml").write_text(
        "test_cases:\n  - {ref: T1, title: t, scenario: scenarios/none.osc,"
        " criterion_stl: 'always d >= '}\n")
    with pytest.raises(ProjectLoadError) as e:
        load_project(tmp_path)
    assert "not found" in str(e.value) and "does not parse" in str(e.value)


def test_campaign_executes_every_test_case(artefacts):
    _, result, _, _ = artefacts
    assert set(result.outcomes) == {"TC-CROSS-001", "TC-CROSS-002", "TC-FIELD-001",
                                    "TC-SPEED-001", "TC-MUTE-001"}
    assert all(o.n_executed > 0 and o.n_errors == 0 for o in result.outcomes.values())


def test_baseline_defects_are_found(artefacts):
    """Rev A carries four latent design defects; each must be falsified."""
    _, result, _, _ = artefacts
    o = result.outcomes
    assert o["TC-FIELD-001"].worst_robustness < -1.0      # fields sized without K*T
    assert o["TC-MUTE-001"].worst_robustness < 0          # no mute timeout
    assert o["TC-SPEED-001"].worst_robustness < 0         # no ramp monitoring
    assert o["TC-CROSS-001"].falsified
    worst = o["TC-CROSS-001"].worst_assignment
    assert worst["truck_speed"] > 1.5 and worst["warning_field"] == 0.0


def test_gate_blocks_and_names_the_defects(artefacts):
    _, _, gate, _ = artefacts
    assert not gate.passed
    blockers = {(f.rule, f.subject) for f in gate.blockers()}
    assert ("R-PL-001", "SF-PDS-01") in blockers
    assert ("R-TRACE-002", "SR-COLL-003") in blockers
    assert ("R-EXEC-001", "TC-MUTE-001") in blockers


def test_gate_requires_hil_for_high_pl(artefacts):
    _, _, gate, _ = artefacts
    assert {f.subject for f in gate.findings if f.rule == "R-EXEC-002"} >= {
        "TC-CROSS-001", "TC-SPEED-001"}


def test_pl_is_derived_not_asserted(artefacts):
    _, _, gate, _ = artefacts
    pr = gate.pl_results["SF-PDS-01"]
    assert pr.valid and pr.achieved_pl.value == "d"


def test_every_run_is_recorded_in_the_chain(artefacts):
    _, result, _, store = artefacts
    recorded = sum(1 for e in store.entries() if e.payload.get("type") == "run")
    executed = sum(o.n_executed for o in result.outcomes.values())
    assert recorded == executed
    assert store.verify_chain(trusted_keys=[store.signer_id]) == []


def test_report_root_is_recomputable_after_writing(artefacts, tmp_path):
    """0.3.0: writing the report appended an entry that changed the root the
    report had just printed, so the cover sheet could never be verified."""
    project, result, gate, store = artefacts
    meta = DocumentMeta(manufacturer="M", product="T200", document_id="VV-1",
                        revision="A", author="PY")
    out = tmp_path / "VV.md"
    TechnicalFile(project, result, gate, store, meta).write(str(out))
    text = out.read_text()
    assert "Counterexamples" in text and "Falsification, not proof" in text
    assert store.campaign_root("t") in text
    assert verify_report(store, str(out)) == []
    out.write_text(text.replace("NON-CONFORMING", "CONFORMING"))
    assert verify_report(store, str(out)), "an edited report must not verify"


def test_store_with_two_campaigns_reports_only_the_selected_one(tmp_path):
    """0.3.0 merged every campaign in a store under the newest build's hash."""
    store = EvidenceStore(tmp_path)
    campaign(ROOT, store, small("old", sut_build_hash="build-old"), only=["TC-MUTE-001"])
    campaign(ROOT, store, small("new", sut_build_hash="build-new"), only=["TC-FIELD-001"])
    latest = load_campaign(store)
    assert latest.config.sut_build_hash == "build-new"
    assert set(latest.outcomes) == {"TC-FIELD-001"}
    old = load_campaign(store, "old")
    assert set(old.outcomes) == {"TC-MUTE-001"}


def test_run_tier_comes_from_the_runner_not_the_yaml(tmp_path):
    """0.3.0 recorded runs at the tier the test case declared, so editing
    `tier: hil` in YAML turned simulator runs into HIL evidence."""
    project = load_project(ROOT)
    from safegate.core.model import ExecutionTier
    tc = project.test_cases[0].model_copy(update={"required_tier": ExecutionTier.HIL})
    project = project.model_copy(update={"test_cases": [tc]})
    store = EvidenceStore(tmp_path)
    runner = SilRunner(ROOT / "sut_config.yaml")
    res = Campaign(project, runner, store, small(), derive=DerivedSignals(_budget_from(ROOT))).run()
    tiers = {run.tier.value for run, _ in res.outcomes[tc.ref].runs}
    assert tiers == {"sil"}
    gate = Gate(project, res, store, PolicyConfig(require_signed_evidence=False)).evaluate()
    assert ("R-EXEC-003", tc.ref) in {(f.rule, f.subject) for f in gate.findings}


def test_rewritten_chain_is_caught_by_pinned_key_and_anchor(tmp_path):
    """0.3.0 verified signatures against the key stored next to the manifest,
    so a rewritten, re-signed chain without the failing runs passed."""
    store = EvidenceStore(tmp_path / "ev")
    store.generate_key()
    trusted = store.signer_id
    campaign(ROOT, store, small(), only=["TC-MUTE-001"])
    anchor = store.anchor("t")

    entries = [json.loads(line)["payload"] for line in store.manifest_path.read_text().splitlines()]
    store.manifest_path.unlink()
    for name in ("signing.key", "signing.pub"):
        (tmp_path / "ev" / "keys" / name).unlink()
    forged = EvidenceStore(tmp_path / "ev")
    forged.generate_key()
    for p in entries:
        if p.get("type") == "run" and p["result"]["verdict"] == "fail":
            continue
        forged.append(p)

    assert forged.verify_chain() == []                    # self-consistent
    assert forged.verify_chain(trusted_keys=[trusted])    # wrong signer
    assert forged.verify_anchor(anchor)                    # rewritten after anchoring


def test_ml_rule_fires_even_when_the_determination_is_invalid():
    """0.3.0 skipped R-ML-001 whenever the PL determination was invalid."""
    project = load_project(ROOT)
    sf = project.safety_functions[0].model_copy(update={"architecture_ref": "ARCH-PDS-ML"})
    project = project.model_copy(update={"safety_functions": [sf, project.safety_functions[1]]})
    gate = Gate(project, None, None, PolicyConfig()).evaluate()
    rules = {f.rule for f in gate.findings if f.subject == "SF-PDS-01"}
    assert {"R-ML-001", "R-PL-002"} <= rules


def test_changed_design_data_invalidates_evidence(artefacts):
    project, result, _, store = artefacts
    req = project.requirements[0].model_copy(update={"statement": "edited after the campaign"})
    edited = project.model_copy(update={"requirements": [req, *project.requirements[1:]]})
    gate = Gate(edited, result, store, PolicyConfig()).evaluate()
    assert "R-EVID-002" in {f.rule for f in gate.blockers()}


def _cli_campaign(tmp, tag):
    env = dict(os.environ, PYTHONHASHSEED=str(tag))
    subprocess.run(
        [sys.executable, "-m", "safegate.cli", "run", str(ROOT), "--store", str(tmp / f"s{tag}"),
         "--build", "b", "--only", "TC-CROSS-002", "--boundary", "8", "--sweep", "16",
         "--falsify", "0", "--json", str(tmp / f"c{tag}.json")],
        check=False, capture_output=True, text=True, env=env,
    )
    runs = [json.loads(line)["payload"] for line in (tmp / f"s{tag}" / "manifest.log").read_text().splitlines()]
    return [(r["run"]["assignment"], r["run"]["pinning"]["seed"], r["result"]["robustness"])
            for r in runs if r.get("type") == "run"]


def test_campaign_is_identical_across_processes(tmp_path):
    """0.3.0 seeded each test case with Python's salted hash(), so two CLI
    invocations sampled different points with different seeds."""
    a = _cli_campaign(tmp_path, 1)
    b = _cli_campaign(tmp_path, 2)
    assert len(a) == 24 and a == b


def test_parallel_workers_record_the_same_evidence(tmp_path):
    store1, store4 = EvidenceStore(tmp_path / "w1"), EvidenceStore(tmp_path / "w4")
    _, r1 = campaign(ROOT, store1, small(), only=["TC-SPEED-001"])
    _, r4 = campaign(ROOT, store4, small(workers=4), only=["TC-SPEED-001"])
    seq = lambda r: [(run.id, res.robustness) for run, res in r.outcomes["TC-SPEED-001"].runs]  # noqa: E731
    assert seq(r1) == seq(r4)
