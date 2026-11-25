import pandas as pd
import pytest

from scripts.strategy_candidate_comparison import compare_strategy_candidates


def _candles():
    rows = []
    for symbol, base, drift in (("BTC-USDT", 100.0, 0.01), ("ETH-USDT", 50.0, -0.01)):
        for minute in range(20):
            rows.append({
                "symbol": symbol, "minute_ts": minute * 60_000,
                "close": base * (1 + drift) ** minute,
            })
    return pd.DataFrame(rows)


def _signals(symbol, direction, stamps=(0, 5 * 60_000)):
    return pd.DataFrame({
        "symbol": [symbol] * len(stamps),
        "signal_ts": list(stamps),
        "direction": [direction] * len(stamps),
    })


def test_comparison_uses_same_cost_scenarios_and_reports_baseline_deltas():
    result = compare_strategy_candidates(
        {
            "Strategy B": _signals("BTC-USDT", "LONG"),
            "Candidate": _signals("BTC-USDT", "SHORT"),
        },
        _candles(), horizon_minutes=2,
        cost_scenarios=[
            {"name": "base", "fee_bps_per_side": 1, "slippage_bps_per_side": 1},
            {"name": "stress", "fee_bps_per_side": 5, "slippage_bps_per_side": 5},
        ],
    )
    assert result["status"] == "evaluated"
    assert len(result["results"]) == 4
    assert len(result["candidate_vs_baseline"]) == 2
    assert {r["cost_scenario"] for r in result["results"]} == {"base", "stress"}
    assert all(r["overall"]["trade_count"] == 2 for r in result["results"])
    assert result["candidate_vs_baseline"][0]["strategy"] == "Candidate"
    assert result["results"][0]["by_symbol"][0]["symbol"] == "BTC-USDT"
    assert result["results"][0]["by_utc_date"][0]["utc_date"] == "1970-01-01"


def test_comparison_requires_strategy_b_and_unique_signal_timestamps():
    with pytest.raises(ValueError, match="baseline"):
        compare_strategy_candidates({"Candidate": _signals("BTC-USDT", "LONG")}, _candles(), horizon_minutes=1)
    duplicate = _signals("BTC-USDT", "LONG", stamps=(0, 0))
    with pytest.raises(ValueError, match="duplicate"):
        compare_strategy_candidates(
            {"Strategy B": duplicate}, _candles(), horizon_minutes=1
        )


def test_comparison_rejects_duplicate_scenario_names():
    with pytest.raises(ValueError, match="unique"):
        compare_strategy_candidates(
            {"Strategy B": _signals("BTC-USDT", "LONG")}, _candles(),
            horizon_minutes=1,
            cost_scenarios=[
                {"name": "same", "fee_bps_per_side": 1, "slippage_bps_per_side": 1},
                {"name": "same", "fee_bps_per_side": 2, "slippage_bps_per_side": 2},
            ],
        )


def test_no_valid_trades_are_reported_without_inventing_metrics():
    result = compare_strategy_candidates(
        {"Strategy B": _signals("BTC-USDT", "LONG", stamps=(100 * 60_000,))},
        _candles(), horizon_minutes=1,
    )
    assert result["results"][0]["overall"]["status"] == "no_valid_trades"
    assert result["results"][0]["overall"]["net_expectancy_pct"] is None


@pytest.mark.parametrize("bad_ts", [1.5, 60_000.5, float("inf"), float("nan")])
def test_comparison_rejects_non_integer_or_non_finite_signal_timestamps(bad_ts):
    bad = pd.DataFrame({
        "symbol": ["BTC-USDT"], "signal_ts": [bad_ts], "direction": ["LONG"]
    })
    with pytest.raises(ValueError, match="signal_ts"):
        compare_strategy_candidates(
            {"Strategy B": bad}, _candles(), horizon_minutes=1
        )


def test_comparison_rejects_empty_symbol_and_blank_strategy_name():
    empty_symbol = pd.DataFrame({
        "symbol": ["  "], "signal_ts": [0], "direction": ["LONG"]
    })
    with pytest.raises(ValueError, match="symbol"):
        compare_strategy_candidates(
            {"Strategy B": empty_symbol}, _candles(), horizon_minutes=1
        )
    with pytest.raises(ValueError, match="strategy names"):
        compare_strategy_candidates(
            {
                "Strategy B": _signals("BTC-USDT", "LONG"),
                "   ": _signals("ETH-USDT", "SHORT"),
            }, _candles(), horizon_minutes=1, baseline_name="Strategy B"
        )


def test_breakdowns_include_drawdown_profit_factor_and_sample_breadth():
    stamps = tuple(i * 24 * 60 * 60_000 for i in range(4))
    candles = pd.DataFrame([
        {"symbol": "BTC-USDT", "minute_ts": ts, "close": 100.0}
        for ts in range(0, stamps[-1] + 3 * 60_000, 60_000)
    ])
    signals = _signals("BTC-USDT", "LONG", stamps=stamps)
    result = compare_strategy_candidates(
        {"Strategy B": signals}, candles, horizon_minutes=1,
        cost_scenarios=[{
            "name": "zero", "fee_bps_per_side": 0, "slippage_bps_per_side": 0
        }],
    )
    row = result["results"][0]
    assert row["breadth"]["utc_entry_dates_observed"] == 4
    assert row["breadth"]["four_or_more_utc_entry_dates_observed"] is True
    assert row["by_symbol"][0]["max_drawdown_pct"] == 0.0
    assert row["by_symbol"][0]["profit_factor"] is None
