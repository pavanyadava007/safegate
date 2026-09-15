"""The whole pipeline: load -> run -> gate -> report."""
from pathlib import Path
import pytest

from safegate.core.cas import EvidenceStore
from safegate.core.loader import load_project
from safegate.execution.adapter import NullRunner
from safegate.orchestrator.campaign import Campaign, CampaignConfig
from safegate.policy.gate import Gate, PolicyConfig
from safegate.report.technical_file import DocumentMeta, TechnicalFile

ROOT = Path(__file__).resolve().parents[1] / "examples" / "amr_project"


@pytest.fixture(scope="module")
def artefacts(tmp_path_factory):
    store = EvidenceStore(tmp_path_factory.mktemp("ev"))
    project = load_project(ROOT)
    cfg = CampaignConfig(campaign_id="t", sut_build_hash="deadbeef",
                         config_hash="cfg1", boundary_budget=12,
                         sweep_budget=24, falsify_budget=24, seed=7)
    result = Campaign(project, NullRunner(), store, cfg).run()
    gate = Gate(project, result, store, PolicyConfig(require_signed_evidence=False)).evaluate()
    return project, result, gate, store, cfg


def test_project_loads_and_validates():
    p = load_project(ROOT)
    assert len(p.hazards) == 4
    assert len(p.test_cases) == 4


def test_loader_rejects_dangling_refs(tmp_path):
    from safegate.core.loader import ProjectLoadError
    (tmp_path / "project.yaml").write_text("name: x\n")
    (tmp_path / "requirements.yaml").write_text(
        "requirements:\n  - {ref: R1, statement: s, hazards: [NOPE]}\n")
    with pytest.raises(ProjectLoadError, match="unknown hazard"):
        load_project(tmp_path)


def test_campaign_executes_every_test_case(artefacts):
    _, result, _, _, _ = artefacts
    assert set(result.outcomes) == {"TC-CROSS-001", "TC-CROSS-002",
                                    "TC-SPEED-001", "TC-MUTE-001"}
    assert all(o.n_executed > 0 for o in result.outcomes.values())


def test_falsification_finds_the_unsafe_corner(artefacts):
    """The crossing scenario space contains combinations (high speed, low
    friction, long latency, short detection range) that violate the 0.10 m
    separation requirement. If the campaign does NOT find them, the
    sampler is broken and the tool is giving false assurance."""
    _, result, _, _, _ = artefacts
    o = result.outcomes["TC-CROSS-001"]
    assert o.falsified, "expected a counterexample in the crossing space"
    assert o.worst_robustness < 0
    a = o.worst_assignment
    assert a is not None and "truck_speed" in a


def test_gate_blocks_on_the_counterexample(artefacts):
    _, _, gate, _, _ = artefacts
    assert not gate.passed
    rules = {f.rule for f in gate.blockers()}
    assert "R-EXEC-001" in rules


def test_gate_requires_hil_for_pl_d(artefacts):
    """SF-PDS-01 achieves PL d, so SIL-only evidence must be blocked."""
    _, _, gate, _, _ = artefacts
    assert any(f.rule == "R-EXEC-002" for f in gate.findings)


def test_pl_is_derived_not_asserted(artefacts):
    _, _, gate, _, _ = artefacts
    pr = gate.pl_results["SF-PDS-01"]
    assert pr.valid and pr.achieved_pl.value == "d"


def test_every_run_is_recorded_in_the_chain(artefacts):
    _, result, _, store, _ = artefacts
    recorded = sum(1 for e in store.entries() if e.payload.get("type") == "run")
    executed = sum(o.n_executed for o in result.outcomes.values())
    assert recorded >= executed, "runs were executed but not recorded"
    assert store.verify_chain() == []


def test_report_contains_the_counterexample_and_the_caveats(artefacts, tmp_path):
    project, result, gate, store, _ = artefacts
    meta = DocumentMeta(manufacturer="M", product="T200", document_id="VV-1",
                        revision="A", author="PY")
    tf = TechnicalFile(project, result, gate, store, meta)
    text = tf.render()
    assert "Counterexamples" in text
    assert "Falsification, not proof" in text
    assert "Simulation is not physical verification" in text
    assert result.merkle_root[:16] in text or store.campaign_root("t")[:16] in text


def test_determinism_same_seed_same_result(tmp_path_factory):
    project = load_project(ROOT)
    outs = []
    for _ in range(2):
        store = EvidenceStore(tmp_path_factory.mktemp("ev"))
        cfg = CampaignConfig(campaign_id="d", sut_build_hash="b", config_hash="c",
                             boundary_budget=6, sweep_budget=8, falsify_budget=8, seed=99)
        r = Campaign(project, NullRunner(), store, cfg).run(only=["TC-SPEED-001"])
        outs.append(r.outcomes["TC-SPEED-001"].worst_robustness)
    assert outs[0] == outs[1]
