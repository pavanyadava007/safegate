"""ISO 13849-1 calculator tests, including the cases that must FAIL."""
import pytest
from safegate.core.model import Category, PerformanceLevel, SafetyArchitecture, Subsystem
from safegate.iso13849.pl import DCBand, MTTFdBand, component_mttfd_years, evaluate_architecture


def test_b10d_route():
    # B10d = 1e6, 30 cyc/h, 16 h/d, 250 d/y -> n_op = 120,000 cyc/y
    # MTTFd = 1e6 / (0.1 * 1.2e5) = 83.33 y
    s = Subsystem(name="relay", b10d_cycles=1e6, cycles_per_hour=30,
                  operating_hours_per_day=16, operating_days_per_year=250)
    assert component_mttfd_years(s) == pytest.approx(83.333, rel=1e-3)


def test_b10d_without_duty_cycle_is_an_error():
    s = Subsystem(name="relay", b10d_cycles=1e6)
    with pytest.raises(ValueError, match="duty cycle"):
        component_mttfd_years(s)


def _cat3(ccf=75, dc=0.95, mttfd=100.0):
    subs = []
    for ch in (1, 2):
        subs += [Subsystem(name=f"in{ch}", channel=ch, mttfd_years=mttfd, dc=dc),
                 Subsystem(name=f"out{ch}", channel=ch, mttfd_years=mttfd, dc=dc)]
    return SafetyArchitecture(ref="A", category=Category.CAT_3, subsystems=subs, ccf_score=ccf)


def test_cat3_high_mttfd_medium_dc_gives_pl_d():
    r = evaluate_architecture(_cat3())
    assert r.valid, r.violations
    assert r.mttfd_band is MTTFdBand.HIGH
    assert r.dc_band is DCBand.MEDIUM
    assert r.achieved_pl is PerformanceLevel.d
    assert r.meets(PerformanceLevel.d)
    assert not r.meets(PerformanceLevel.e)


def test_ccf_below_65_invalidates_cat3():
    r = evaluate_architecture(_cat3(ccf=55))
    assert not r.valid
    assert any("CCF" in v for v in r.violations)
    assert r.achieved_pl is None          # must NOT silently downgrade


def test_single_channel_cat3_is_invalid():
    a = SafetyArchitecture(ref="A", category=Category.CAT_3, ccf_score=80,
                           subsystems=[Subsystem(name="x", channel=1, mttfd_years=100, dc=0.95)])
    r = evaluate_architecture(a)
    assert not r.valid
    assert any("two channels" in v for v in r.violations)


def test_mttfd_is_capped_at_100_years():
    a = SafetyArchitecture(ref="A", category=Category.CAT_1, ccf_score=0,
                           subsystems=[Subsystem(name="x", mttfd_years=5000, dc=0.0)])
    r = evaluate_architecture(a)
    assert r.mttfd_years <= 100.0
    assert any("capped" in n for n in r.notes)


def test_dc_band_not_admissible_for_category():
    # Cat 4 requires high DC; medium must invalidate rather than downgrade.
    subs = [Subsystem(name=f"s{ch}", channel=ch, mttfd_years=100, dc=0.95) for ch in (1, 2)]
    a = SafetyArchitecture(ref="A", category=Category.CAT_4, subsystems=subs, ccf_score=90)
    r = evaluate_architecture(a)
    assert not r.valid
    assert any("not admissible" in v for v in r.violations)


def test_ml_in_safety_path_is_noted():
    a = _cat3()
    a = SafetyArchitecture(ref=a.ref, category=a.category, subsystems=a.subsystems,
                           ccf_score=a.ccf_score, uses_ml_in_safety_path=True)
    r = evaluate_architecture(a)
    assert any("Notified Body" in n for n in r.notes)


def test_risk_graph_plr():
    from safegate.core.model import Avoidance, Frequency, Hazard, Severity
    h = Hazard(ref="H", title="t", description="", severity=Severity.S2,
               frequency=Frequency.F2, avoidance=Avoidance.P2)
    assert h.required_pl is PerformanceLevel.e
    h2 = Hazard(ref="H2", title="t", description="", severity=Severity.S2,
                frequency=Frequency.F2, avoidance=Avoidance.P1)
    assert h2.required_pl is PerformanceLevel.d
