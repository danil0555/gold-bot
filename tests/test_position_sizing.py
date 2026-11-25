import math
from decimal import Decimal

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


def test_parse_bingx_contract_metadata():
    from scripts.position_sizing import parse_bingx_contract_specs

    specs = parse_bingx_contract_specs({
        "code": 0,
        "data": [{
            "symbol": "BTC-USDT",
            "quantityPrecision": 3,
            "tradeMinQuantity": "0.001",
            "tradeMinUSDT": "5",
            "status": 1,
            "apiStateOpen": "true",
            "maxLongLeverage": 100,
            "maxShortLeverage": "75",
        }],
    })
    spec = specs["BTC-USDT"]
    assert spec["quantity_precision"] == 3
    assert spec["min_quantity"] == Decimal("0.001")
    assert spec["min_notional"] == Decimal("5")
    assert spec["active"] is True
    assert spec["api_open"] is True


def test_bingx_quantity_rounds_down_and_recalculates_risk():
    from scripts.position_sizing import apply_bingx_contract_constraints

    result = apply_bingx_contract_constraints(
        {"quantity": 1.234567, "entry_price": 100, "stop_distance": 2},
        {
            "quantity_precision": 3,
            "min_quantity": Decimal("0.001"),
            "min_notional": Decimal("5"),
            "active": True,
            "api_open": True,
            "max_long_leverage": Decimal("20"),
        },
        "LONG", 5,
    )
    assert result["validated"] is True
    assert result["quantity"] == Decimal("1.234")
    assert result["quantity_precision"] == 3
    assert result["notional_usdt"] == Decimal("123.400")
    assert result["risk_usdt"] == Decimal("2.468")
    assert result["margin_usdt"] == Decimal("24.680")


def test_bingx_rejects_quantity_below_minimum_after_rounding():
    from scripts.position_sizing import apply_bingx_contract_constraints

    result = apply_bingx_contract_constraints(
        {"quantity": 0.0009, "entry_price": 1000, "stop_distance": 10},
        {
            "quantity_precision": 3,
            "min_quantity": Decimal("0.001"),
            "min_notional": Decimal("2"),
            "active": True,
            "api_open": True,
            "max_long_leverage": Decimal("20"),
        },
        "LONG", 5,
    )
    assert result["validated"] is False
    assert "quantity_rounds_to_zero" in result["reasons"]


def test_bingx_does_not_validate_closed_or_unknown_contract():
    from scripts.position_sizing import apply_bingx_contract_constraints

    sizing = {"quantity": 1, "entry_price": 100, "stop_distance": 2}
    unknown = apply_bingx_contract_constraints(sizing, {"quantity_precision": None}, "LONG", 5)
    assert unknown["validated"] is False
    closed = apply_bingx_contract_constraints(
        sizing,
        {
            "quantity_precision": 2, "min_quantity": Decimal("0.01"), "min_notional": Decimal("2"),
            "active": False, "api_open": False, "max_long_leverage": Decimal("20"),
        },
        "LONG", 5,
    )
    assert closed["validated"] is False
    assert "contract_not_active" in closed["reasons"]
    assert "api_open_not_confirmed" in closed["reasons"]
