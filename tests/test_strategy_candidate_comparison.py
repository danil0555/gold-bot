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
