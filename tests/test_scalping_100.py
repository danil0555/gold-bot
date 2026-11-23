from scripts.backtest_scalping_100 import simulate_portfolio


def test_portfolio_uses_100_usdt_risk_sizing_and_reinvests_after_close():
    trades = [
        {"symbol": "BTC-USDT", "entry_time": 0, "exit_time": 100,
         "stop_distance_pct": 1.0, "net_r": -1.0},
        {"symbol": "ETH-USDT", "entry_time": 50, "exit_time": 150,
         "stop_distance_pct": 1.0, "net_r": 1.0},
        {"symbol": "SOL-USDT", "entry_time": 200, "exit_time": 250,
         "stop_distance_pct": 1.0, "net_r": 2.0},
    ]
    result = simulate_portfolio(trades, 100.0, 2.0, 5.0, 1)
    assert result["accepted_trades"] == 2
    assert result["skipped_max_positions"] == 1
    assert result["ending_equity_usdt"] == 101.92
    assert result["max_open_positions_observed"] == 1


def test_portfolio_caps_gross_notional_at_five_times_equity():
    trades = [
        {"symbol": "BTC-USDT", "entry_time": 0, "exit_time": 100,
         "stop_distance_pct": 0.1, "net_r": -1.0},
    ]
    result = simulate_portfolio(trades, 100.0, 2.0, 5.0, 5)
    assert result["trades"][0]["notional_usdt"] == 500.0
    assert result["trades"][0]["risk_usdt"] == 0.5
    assert result["ending_equity_usdt"] == 99.5


def test_portfolio_does_not_open_two_positions_in_same_symbol():
    trades = [
        {"symbol": "BTC-USDT", "entry_time": 0, "exit_time": 100,
         "stop_distance_pct": 1.0, "net_r": 1.0},
        {"symbol": "BTC-USDT", "entry_time": 10, "exit_time": 90,
         "stop_distance_pct": 1.0, "net_r": 1.0},
    ]
    result = simulate_portfolio(trades, 100.0, 2.0, 5.0, 5)
    assert result["accepted_trades"] == 1
    assert result["skipped_max_positions"] == 1
