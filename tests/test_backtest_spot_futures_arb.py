import pytest

from scripts.backtest_spot_futures_arb import STEP_MS, metric_pack, simulate_cash_carry


def test_cash_carry_trade_earns_basis_convergence_and_funding_after_costs():
    start = 1_700_000_000_000
    bars = [
        {"time": start, "spot": 100.0, "swap": 100.7},
        {"time": start + STEP_MS, "spot": 100.0, "swap": 100.6},
        {"time": start + 2 * STEP_MS, "spot": 100.0, "swap": 100.0},
    ]
    funding = [
        {"time": start - 1, "rate": 0.001, "mark": 100.0},
        {"time": start + STEP_MS + 1, "rate": 0.001, "mark": 100.0},
    ]
    result = simulate_cash_carry(
        bars, funding, notional=1000, min_entry_basis_pct=0.5,
        target_basis_pct=0.05, max_hold_hours=24,
        spot_fee=0, swap_fee=0, slippage_pct=0,
    )
    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["exit_reason"] == "basis_target"
    assert trade["gross_pnl_usdt"] > 0
    assert trade["funding_pnl_usdt"] > 0
    assert trade["net_pnl_usdt"] > 0


def test_cash_carry_does_not_enter_without_positive_settled_funding():
    start = 1_700_000_000_000
    bars = [
        {"time": start, "spot": 100.0, "swap": 100.8},
        {"time": start + STEP_MS, "spot": 100.0, "swap": 100.7},
    ]
    funding = [{"time": start - 1, "rate": -0.001, "mark": 100.0}]
    result = simulate_cash_carry(
        bars, funding, min_entry_basis_pct=0.5, target_basis_pct=0.05,
        spot_fee=0, swap_fee=0, slippage_pct=0,
    )
    assert result["trades"] == []


def test_open_trade_at_end_is_not_counted_as_closed_loss():
    start = 1_700_000_000_000
    bars = [{"time": start, "spot": 100.0, "swap": 100.8}]
    funding = [{"time": start - 1, "rate": 0.001, "mark": 100.0}]
    result = simulate_cash_carry(
        bars, funding, min_entry_basis_pct=0.5, target_basis_pct=0.05,
        spot_fee=0, swap_fee=0, slippage_pct=0,
    )
    assert result["trades"] == []
    assert result["open_at_end"] == 1
    assert result["open_position_mark"] is not None
    assert "net_mark_to_market_usdt" in result["open_position_mark"]


def test_metric_pack_uses_net_after_fees_and_funding():
    trades = [
        {"net_pnl_usdt": 5.0, "hold_hours": 1, "exit_reason": "basis_target"},
        {"net_pnl_usdt": -2.0, "hold_hours": 2, "exit_reason": "max_hold"},
    ]
    result = metric_pack(trades)
    assert result["closed_trades"] == 2
    assert result["net_pnl_usdt_per_1000_notional"] == 3.0
    assert result["profit_factor"] == 2.5



def test_negative_settled_funding_is_charged_to_open_short_perpetual():
    start = 1_700_000_000_000
    bars = [
        {"time": start, "spot": 100.0, "swap": 100.8},
        {"time": start + STEP_MS, "spot": 100.0, "swap": 100.7},
    ]
    funding = [
        {"time": start - 1, "rate": 0.001, "mark": 100.0},
        {"time": start + STEP_MS + 1, "rate": -0.001, "mark": 100.0},
    ]
    result = simulate_cash_carry(
        bars, funding, notional=1000, min_entry_basis_pct=0.5,
        target_basis_pct=0.05, max_hold_hours=1,
        spot_fee=0, swap_fee=0, slippage_pct=0,
    )
    assert len(result["trades"]) == 1
    assert result["trades"][0]["funding_pnl_usdt"] < 0
    assert result["trades"][0]["exit_reason"] == "max_hold"
