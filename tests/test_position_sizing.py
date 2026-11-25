import math

import pytest

from scripts.position_sizing import calculate_linear_usdtm_position_size


def test_long_position_sizing_uses_ten_percent_loss_budget():
    result = calculate_linear_usdtm_position_size(
        balance_usdt=1000, entry_price=100, stop_price=98,
        side="LONG", risk_percent=10, leverage=5,
    )
    assert result["risk_usdt"] == pytest.approx(100)
    assert result["quantity"] == pytest.approx(50)
    assert result["notional_usdt"] == pytest.approx(5000)
    assert result["margin_usdt"] == pytest.approx(1000)
    assert result["fits_balance"] is True


def test_short_position_sizing_uses_stop_distance():
    result = calculate_linear_usdtm_position_size(
        balance_usdt=1000, entry_price=100, stop_price=102,
        side="SHORT", risk_percent=10, leverage=5,
    )
    assert result["quantity"] == pytest.approx(50)


def test_rejects_stop_on_wrong_side():
    with pytest.raises(ValueError):
        calculate_linear_usdtm_position_size(1000, 100, 101, "LONG")


def test_flags_margin_requirement_above_saved_balance():
    result = calculate_linear_usdtm_position_size(
        balance_usdt=1000, entry_price=100, stop_price=99,
        side="LONG", risk_percent=10, leverage=5,
    )
    assert result["margin_usdt"] == pytest.approx(2000)
    assert result["fits_balance"] is False


def test_rejects_non_finite_inputs():
    with pytest.raises(ValueError):
        calculate_linear_usdtm_position_size(math.inf, 100, 98, "LONG")
