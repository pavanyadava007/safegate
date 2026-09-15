"""Protective field budget arithmetic."""
import pytest
from safegate.iso3691 import StoppingBudget


def test_required_field_length():
    b = StoppingBudget(t_detect_s=0.08, t_react_s=0.06, t_comm_s=0.03,
                       a_brake_mps2=1.2, z_s_m=0.09, human_approach=True, margin_m=0.10)
    # t_total = 0.17 s
    # v = 1.5: latency 0.255, braking 0.9375, human 0.272, Zs 0.09, margin 0.10
    assert b.total_latency_s == pytest.approx(0.17)
    assert b.required_field_length(1.5) == pytest.approx(1.6545, abs=1e-3)


def test_inversion_round_trips():
    b = StoppingBudget(t_detect_s=0.08, t_react_s=0.06, a_brake_mps2=1.2,
                       z_s_m=0.09, margin_m=0.10)
    L = b.required_field_length(1.2)
    assert b.max_permitted_speed(L) == pytest.approx(1.2, rel=1e-6)


def test_field_too_short_forces_standstill():
    b = StoppingBudget(t_detect_s=0.1, t_react_s=0.1, a_brake_mps2=1.0,
                       z_s_m=0.5, margin_m=0.2)
    assert b.max_permitted_speed(0.3) == 0.0   # Zs + margin already exceed L
