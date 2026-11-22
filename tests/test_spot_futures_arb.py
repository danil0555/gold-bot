import pytest

from scripts.spot_futures_arb import (
    MarketDataError,
    calculate_cash_carry,
    depth_vwap,
    parse_order_book,
)


def books():
    return {
        "spot_bids": [(99.9, 20), (99.8, 20)],
        "spot_asks": [(100.0, 20), (100.1, 20)],
        "swap_bids": [(100.3, 20), (100.2, 20)],
        "swap_asks": [(100.4, 20), (100.5, 20)],
    }


def test_depth_vwap_consumes_multiple_levels():
    price, filled = depth_vwap([(10, 1), (12, 2)], 2, "buy")
    assert price == pytest.approx(11)
    assert filled == pytest.approx(2)


def test_depth_vwap_rejects_insufficient_liquidity():
    with pytest.raises(MarketDataError):
        depth_vwap([(10, 1)], 2, "buy")


def test_positive_funding_is_credited_to_short_perpetual():
    b = books()
    result = calculate_cash_carry(
        "BTC-USDT", b["spot_bids"], b["spot_asks"], b["swap_bids"],
        b["swap_asks"], funding_rate=0.001, funding_periods=2,
        quote_notional=1000, spot_taker_fee=0, swap_taker_fee=0,
        funding_haircut=1, stress_widening_bps=0,
    )
    assert result["expected_funding_usdt"] > 0
    assert result["direction"] == "LONG_SPOT_SHORT_PERPETUAL"


def test_negative_funding_is_not_misrepresented_as_income():
    b = books()
    result = calculate_cash_carry(
        "BTC-USDT", b["spot_bids"], b["spot_asks"], b["swap_bids"],
        b["swap_asks"], funding_rate=-0.001, funding_periods=3,
        quote_notional=1000, spot_taker_fee=0, swap_taker_fee=0,
    )
    assert result["expected_funding_usdt"] == 0


def test_all_four_execution_sides_are_charged_fees():
    b = books()
    result = calculate_cash_carry(
        "BTC-USDT", b["spot_bids"], b["spot_asks"], b["swap_bids"],
        b["swap_asks"], funding_rate=0, funding_periods=0,
        quote_notional=1000, spot_taker_fee=0.001, swap_taker_fee=0.001,
    )
    assert result["estimated_round_trip_fees_usdt"] > 3.9


def test_stress_scenario_is_not_better_than_base_scenario():
    b = books()
    result = calculate_cash_carry(
        "BTC-USDT", b["spot_bids"], b["spot_asks"], b["swap_bids"],
        b["swap_asks"], funding_rate=0.0005, funding_periods=2,
        quote_notional=1000, spot_taker_fee=0.0001, swap_taker_fee=0.0001,
        stress_widening_bps=50,
    )
    assert result["stressed_net_usdt"] < result["expected_net_at_current_basis_assumption_usdt"]


def test_order_book_parser_requires_timestamp_and_depth():
    with pytest.raises(MarketDataError):
        parse_order_book({"code": 0, "data": {"bids": [], "asks": []}}, "test")
