"""Regression tests for defects found in the independent review of 0.4.0."""
import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from safegate.core.cas import EvidenceStore
from safegate.core.loader import load_project
from safegate.core.model import ConcreteRun, ExecutionTier, Pinning, RunResult, Verdict
from safegate.execution.adapter import ExecutionOutcome
from safegate.orchestrator.campaign import (
    Campaign,
    CampaignConfig,
    TestCaseOutcome,
    load_campaign,
)
from safegate.policy.gate import Gate, PolicyConfig
from safegate.sim import FieldSet, SutConfig, World, build, run_scripts
from safegate.sim.core import _steps
from safegate.stl.robustness import Trace

ROOT = Path(__file__).resolve().parents[1] / "examples"


def one_tc_project(ref="TC-MUTE-001"):
    p = load_project(ROOT / "amr_project")
    return p.model_copy(update={"test_cases": [t for t in p.test_cases if t.ref == ref]})


class ScriptedRunner:
    """Non-deterministic runner whose verdict per execution follows a script."""

    name = "scripted"
    tier = ExecutionTier.SIL
    deterministic = False

    def __init__(self, pattern):
        self.pattern = pattern  # e.g. ["error", "fail", "fail"]
        self.calls = 0

    def backend_hash(self):
        return "scripted"

    def execute(self, run, workdir):
        kind = self.pattern[self.calls % len(self.pattern)]
        self.calls += 1
        if kind == "error":
            return ExecutionOutcome(None, False, "scripted error")
        t = np.arange(3) * 0.1
        x = np.full(3, 1.0 if kind == "pass" else 5.0)
        return ExecutionOutcome(Trace(time=t, signals={"muted_elapsed": x}), True)


def cfg(cid="c", **kw):
    base = dict(campaign_id=cid, sut_build_hash="b", config_hash="c", boundary_budget=2,
                sweep_budget=0, falsify_budget=0, seed=1)
    base.update(kw)
    return CampaignConfig(**base)


@pytest.mark.parametrize("pattern", [["error", "fail", "fail"], ["fail", "fail", "error"],
                                     ["pass", "error", "pass"]])
def test_repeat_guard_is_order_independent_and_matches_the_chain(tmp_path, pattern):
    store = EvidenceStore(tmp_path)
    project = one_tc_project()
    live = Campaign(project, ScriptedRunner(pattern), store, cfg()).run()
    loaded = load_campaign(store)
    o_live, o_loaded = live.outcomes["TC-MUTE-001"], loaded.outcomes["TC-MUTE-001"]
    assert o_live.verdict is Verdict.ERROR  # a partly errored point is not a pass or a fail
    assert (o_loaded.verdict, o_loaded.n_executed, o_loaded.n_errors) == (
        o_live.verdict, o_live.n_executed, o_live.n_errors)
    repeats = [e for e in store.entries() if e.payload.get("repeat", 0) > 0]
    assert len(repeats) == 2 * o_live.n_executed


def test_unallocated_requirement_still_carries_the_hil_obligation():
    p = load_project(ROOT / "amr_project")
    sfs = [sf.model_copy(update={"requirement_refs": [r for r in sf.requirement_refs
                                                      if r != "SR-MUTE-001"]})
           for sf in p.safety_functions]
    p = p.model_copy(update={"safety_functions": sfs})
    tc = next(t for t in p.test_cases if t.ref == "TC-MUTE-001")

    run = ConcreteRun(test_case_ref=tc.ref, assignment={}, tier=ExecutionTier.SIL,
                      pinning=Pinning(scenario_hash="s", sut_build_hash="b", backend_hash="k",
                                      config_hash="c", seed=1))
    o = TestCaseOutcome(test_case_ref=tc.ref)
    o.note(run, RunResult(run_id=run.id, verdict=Verdict.PASS, robustness=1.0))
    o.coverage = {"two_way": 1.0}
    fake = SimpleNamespace(config=cfg(), complete=True, incomplete_test_cases=[],
                           project_digest="", outcomes={t.ref: o for t in p.test_cases})
    gate = Gate(p, fake, None, PolicyConfig()).evaluate()
    found = {(f.rule, f.subject) for f in gate.findings}
    assert ("R-TRACE-004", "SR-MUTE-001") in found
    assert ("R-EXEC-002", "TC-MUTE-001") in found  # PLr d of HAZ-MUTE-001, reached directly


def test_unsigned_evidence_blocks_when_signing_is_required(tmp_path):
    store = EvidenceStore(tmp_path)  # no key
    project = one_tc_project()
    res = Campaign(project, ScriptedRunner(["pass"]), store, cfg()).run()
    gate = Gate(project, res, store, PolicyConfig(require_signed_evidence=True)).evaluate()
    assert any(f.rule == "R-EVID-001" and "unsigned" in f.detail for f in gate.blockers())


def test_mixed_signers_block(tmp_path):
    from safegate.core.cas import write_keypair
    write_keypair(tmp_path / "k1")
    write_keypair(tmp_path / "k2")
    s1 = EvidenceStore(tmp_path / "ev", signing_key=tmp_path / "k1" / "signing.key")
    project = one_tc_project()
    Campaign(project, ScriptedRunner(["pass"]), s1, cfg()).run()
    s2 = EvidenceStore(tmp_path / "ev", signing_key=tmp_path / "k2" / "signing.key")
    s2.append({"campaign_id": "c", "type": "note"})
    gate = Gate(project, load_campaign(s2), s2, PolicyConfig()).evaluate()
    assert any("different keys" in f.detail for f in gate.blockers())


def test_interrupted_campaign_blocks(tmp_path):
    store = EvidenceStore(tmp_path)
    project = one_tc_project()
    Campaign(project, ScriptedRunner(["pass"]), store, cfg()).run()
    lines = store.manifest_path.read_text().splitlines()
    store.manifest_path.write_text("\n".join(lines[:-2]) + "\n")  # drop summary and end
    loaded = load_campaign(store)
    assert not loaded.complete and loaded.incomplete_test_cases == ["TC-MUTE-001"]
    gate = Gate(project, loaded, store, PolicyConfig(require_signed_evidence=False)).evaluate()
    assert {f.subject for f in gate.blockers() if f.rule == "R-EXEC-001"} >= {"c", "TC-MUTE-001"}


class DyingRunner(ScriptedRunner):
    deterministic = True

    def execute(self, run, workdir):
        if run.pinning.seed % 3 == 0:
            os._exit(3)  # the worker process dies
        return super().execute(run, workdir)


def test_crashed_worker_becomes_recorded_error_runs(tmp_path):
    store = EvidenceStore(tmp_path)
    project = one_tc_project()
    res = Campaign(project, DyingRunner(["pass"]), store,
                   cfg(boundary_budget=4, sweep_budget=8, workers=2)).run()
    o = res.outcomes["TC-MUTE-001"]
    recorded = sum(1 for e in store.entries() if e.payload.get("type") == "run")
    assert recorded == o.n_executed == 12
    assert o.n_errors >= 1 and o.verdict is Verdict.ERROR
    assert load_campaign(store).complete


def test_rev_b_cruises_at_its_rated_speed_without_tripping():
    c = SutConfig.from_yaml(ROOT / "amr_project_revb" / "sut_config.yaml")
    spec = build("field_switching", {"truck_speed": 1.0, "goal_distance": 20.0}, c)
    w = run_scripts(spec.world, spec.scripts, spec.horizon_s)
    _, s = w.arrays()
    assert s["safe_stop"].max() == 0.0
    assert np.abs(s["speed"]).max() == pytest.approx(1.0)


def test_direction_reversal_waits_for_standstill():
    c = SutConfig(front_field_sets=(FieldSet(2.0, 6.0),), rear_field_sets=(FieldSet(2.0, 6.0),),
                  warning_field_enabled=False)
    w = World(c, truck_v0=0.8)
    w.drive_to(50.0, 0.8)
    w.step()
    w.drive_to(-50.0, 0.8)
    speeds = []
    for _ in range(300):
        w.step()
        speeds.append(w.truck.v)
    steps = np.abs(np.diff(speeds))
    assert steps.max() <= max(c.nav_accel, c.nav_decel) * w.dt + 1e-12
    assert min(speeds) < 0  # it did reverse, eventually


def test_mute_timeout_is_exact_for_any_start_step():
    c = SutConfig(mute_timeout_s=2.0, warning_field_enabled=False)
    lengths = set()
    for start in range(0, 300, 7):
        w = World(c)
        for i in range(start + 400):
            if i == start:
                w.request_muting(True)
            w.step()
        lengths.add(int(sum(w.recorded["protective_device_muted"])))
    assert lengths == {200}


def test_muted_elapsed_counts_whole_samples():
    from safegate.iso3691 import DerivedSignals
    t = np.arange(10) * 0.01
    muted = np.array([0, 1, 1, 1, 0, 0, 1, 0, 0, 0], dtype=float)
    out = DerivedSignals()(Trace(time=t, signals={"protective_device_muted": muted}))
    assert out.signals["muted_elapsed"].max() == pytest.approx(0.03)


def test_latency_steps_round_up():
    assert _steps(0.025, 0.01) == 3
    assert _steps(0.09, 0.02) == 5
    assert _steps(0.06 + 0.03, 0.01) == 9
    assert _steps(0.0, 0.01) == 0


def test_hil_build_must_match_rig_firmware(tmp_path):
    from safegate.cli import main
    from safegate.execution.hil_emulator import EmulatedRig, serve_in_thread
    rig = EmulatedRig(SutConfig.from_yaml(ROOT / "amr_project_revb" / "sut_config.yaml"))
    server, url = serve_in_thread(rig)
    try:
        args = ["run", str(ROOT / "amr_project_revb"), "--runner", "hil", "--rig-url", url,
                "--allow-emulated-rig", "--only", "TC-MUTE-001", "--boundary", "2",
                "--sweep", "0", "--falsify", "0", "--store", str(tmp_path / "ev")]
        assert main([*args, "--build", "not-the-rig-firmware"]) == 2
        assert main(args) in (0, 1)
        start = next(e for e in EvidenceStore(tmp_path / "ev").entries()
                     if e.payload.get("type") == "campaign_start")
        assert start.payload["sut_build_hash"] == rig.firmware_digest
    finally:
        server.shutdown()


def test_restart_waits_exactly_the_configured_delay():
    c = SutConfig(restart_delay_s=1.0, warning_field_enabled=False,
                  front_field_sets=(FieldSet(2.0, 6.0),))
    w = World(c, truck_v0=0.0)
    w.drive_to(50.0, 0.5)
    w.safe_stop = True  # a stop with no remaining cause, at standstill
    steps = 0
    while w.safe_stop:
        w.step()
        steps += 1
    assert steps == 101  # the first clear step starts the count, 100 more complete 1.0 s


def test_first_sample_records_the_real_zone_and_floor():
    from safegate.sim import SpeedZone
    c = SutConfig(speed_zones=(SpeedZone(-10.0, 10.0, 0.3),), warning_field_enabled=False)
    w = World(c, floor_mu=0.2, truck_v0=0.25)
    assert w.recorded["zone_speed_limit"][0] == 0.3
    assert w.recorded["floor_decel_limit"][0] == pytest.approx(0.2 * 9.81)
    spec = build("reversing_at_dock", {}, SutConfig())
    rear = SutConfig().rear_field_sets[0].length
    assert spec.world.recorded["protective_field_length"][0] == rear


def test_expected_build_mismatch_blocks(tmp_path):
    store = EvidenceStore(tmp_path)
    project = one_tc_project()
    res = Campaign(project, ScriptedRunner(["pass"]), store, cfg(sut_build_hash="build-A")).run()
    gate = Gate(project, res, store, PolicyConfig(require_signed_evidence=False),
                expected_build="build-B").evaluate()
    assert any(f.rule == "R-EVID-002" and "build-A" in f.detail for f in gate.blockers())


def test_cli_missing_rig_url_is_a_usage_error(tmp_path):
    from safegate.cli import main
    assert main(["run", str(ROOT / "amr_project"), "--runner", "hil", "--build", "b",
                 "--store", str(tmp_path / "ev")]) == 2
