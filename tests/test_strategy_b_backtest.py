from scripts.backtest_strategy_b import load_strategy_b, metric_pack


def test_strategy_b_backtest_loads_exact_live_builder():
    namespace = load_strategy_b()
    assert callable(namespace["build_signal_b"])
    assert namespace["MIN_CANDLE_ROWS"] == 205
    assert namespace["MIN_QUALITY_SCORE"] == 85.0
    assert namespace["MIN_NET_RR"] == 1.30
    assert namespace["TP1_R"] == 1.8
    assert namespace["TP2_R"] == 3.0


def test_strategy_b_backtest_metrics_are_net_of_costs():
    trades = [
        {"status": "tp2", "net_r": 2.5, "time": "2026-01-01T00:00:00+00:00"},
        {"status": "sl", "net_r": -1.2, "time": "2026-01-01T01:00:00+00:00"},
        {"status": "unresolved", "time": "2026-01-01T02:00:00+00:00"},
        {"status": "skipped_drift", "time": "2026-01-01T03:00:00+00:00"},
    ]
    result = metric_pack(trades)
    assert result["closed_trades"] == 2
    assert result["win_rate_pct"] == 50.0
    assert result["net_r"] == 1.3
    assert result["profit_factor"] == round(2.5 / 1.2, 3)
    assert result["unresolved"] == 1
    assert result["skipped_entry_drift"] == 1
