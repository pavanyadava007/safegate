"""STL parser and quantitative semantics."""
import math
import numpy as np
import pytest
from safegate.stl import parse_stl
from safegate.stl.robustness import Trace, Always, Comparison, Eventually


def tr(**sig):
    n = len(next(iter(sig.values())))
    t = np.arange(n) * 0.1
    s = {k: np.asarray(v, dtype=float) for k, v in sig.items()}
    s["__time__"] = t
    return Trace(time=t, signals=s)


def test_robustness_is_the_margin():
    t = tr(d=[1.0, 0.8, 0.5, 0.9])
    f = parse_stl("always d >= 0.3")
    assert f.evaluate(t) == pytest.approx(0.2)   # 0.5 - 0.3


def test_negative_robustness_on_violation():
    t = tr(d=[1.0, 0.1, 0.9])
    assert parse_stl("always d >= 0.3").evaluate(t) == pytest.approx(-0.2)


def test_eventually_takes_the_supremum():
    t = tr(v=[0.0, 0.0, 2.0, 0.0])
    assert parse_stl("eventually v >= 1.0").evaluate(t) == pytest.approx(1.0)


def test_bounded_interval():
    t = tr(v=[5.0, 5.0, 0.0, 0.0, 0.0])
    # within [0, 0.15]s only samples 0 and 1 are in window -> min is 5
    assert parse_stl("G[0,0.15] v >= 1.0").evaluate(t) == pytest.approx(4.0)


def test_conjunction_is_min_and_disjunction_is_max():
    t = tr(a=[3.0], b=[1.0])
    assert parse_stl("a >= 0 and b >= 0").evaluate(t) == pytest.approx(1.0)
    assert parse_stl("a >= 0 or b >= 0").evaluate(t) == pytest.approx(3.0)


def test_implication():
    t = tr(p=[1.0], q=[0.2])
    # p -> q  ==  max(-rho(p), rho(q))
    assert parse_stl("p >= 0.5 -> q >= 0.1").evaluate(t) == pytest.approx(0.1)


def test_unit_suffixes_are_normalised():
    t = tr(d=[0.05])
    # 40mm = 0.04 m, so margin is 0.01 m
    assert parse_stl("always d >= 40mm").evaluate(t) == pytest.approx(0.01)
    f = parse_stl("G[0,150ms] d >= 0")
    assert isinstance(f, Always) and f.b == pytest.approx(0.15)


def test_signal_to_signal_comparison():
    t = tr(speed=[1.0, 1.4], zone_speed_limit=[1.5, 1.5])
    assert parse_stl("always speed <= zone_speed_limit").evaluate(t) == pytest.approx(0.1)


def test_syntax_error_points_at_the_column():
    from safegate.stl.parser import STLSyntaxError
    with pytest.raises(STLSyntaxError) as e:
        parse_stl("always d >= ")
    assert "^" in str(e.value)


def test_missing_signal_raises_not_silently_passes():
    t = tr(d=[1.0])
    with pytest.raises(KeyError):
        parse_stl("always nonexistent >= 0").evaluate(t)


def test_sliding_window_matches_naive():
    rng = np.random.default_rng(0)
    vals = rng.normal(size=300)
    t = tr(x=vals)
    f = parse_stl("G[0,1.0] x >= 0")
    fast = f.rho(t, {})
    time = t.time
    for i in (0, 50, 150, 299):
        lo, hi = time[i], time[i] + 1.0
        window = vals[(time >= lo) & (time <= hi)]
        expected = window.min() if window.size else math.inf
        assert fast[i] == pytest.approx(expected)
