"""STL parser and quantitative semantics."""
import math

import numpy as np
import pytest

from safegate.stl import parse_stl
from safegate.stl.robustness import Always, Trace


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


def test_linear_expressions_keep_physical_units():
    t = tr(speed=[0.5, 0.64], zone_speed_limit=[0.6, 0.6])
    # 0.6 + 0.05 - 0.64 = 0.01 m/s of headroom
    assert parse_stl("always speed <= zone_speed_limit + 0.05").evaluate(t) == pytest.approx(0.01)
    assert parse_stl("always speed - zone_speed_limit <= 0.05").evaluate(t) == pytest.approx(0.01)


def test_negative_numbers_and_implication_arrow_coexist():
    t = tr(a=[-0.2], b=[1.0])
    assert parse_stl("a >= -0.5").evaluate(t) == pytest.approx(0.3)
    assert parse_stl("a >= -0.5 -> b >= 0.5").evaluate(t) == pytest.approx(0.5)


@pytest.mark.parametrize("src, fn", [("always x >= 0", np.min), ("eventually x >= 0", np.max)])
def test_unbounded_fast_path_matches_naive(src, fn):
    rng = np.random.default_rng(1)
    vals = rng.normal(size=500)
    rho = parse_stl(src).rho(tr(x=vals), {})
    for i in (0, 1, 250, 499):
        assert rho[i] == pytest.approx(fn(vals[i:]))


@pytest.mark.parametrize("src", ["always[2, 0.5] x >= 0", "G[inf, inf] x >= 0", "F[1, 0] x >= 0"])
def test_empty_or_inverted_intervals_are_rejected(src):
    from safegate.stl.parser import STLSyntaxError
    with pytest.raises(STLSyntaxError, match="interval"):
        parse_stl(src)


def test_unbounded_window_includes_last_sample_for_any_start_time():
    rng = np.random.default_rng(3)
    for _ in range(200):
        t0 = float(rng.uniform(0, 1e4))
        n = int(rng.integers(2, 400))
        t = t0 + np.arange(n) * float(rng.choice([0.01, 0.033, 0.1]))
        x = np.ones(n)
        x[-1] = -5.0
        trace = Trace(time=t, signals={"x": x})
        assert parse_stl("always x >= 0").evaluate(trace) == -5.0
        assert parse_stl("always (x >= 0 or x >= 0)").rho(trace, {})[0] == -5.0
