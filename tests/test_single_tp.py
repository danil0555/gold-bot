import pandas as pd

from scripts.backtest_single_tp import simulate_single_tp


def frame(rows):
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"])


def signal(side="BUY"):
    return {"side": side, "entry": 100.0, "sl": 99.0}


def test_single_tp_closes_full_position_at_one_target():
    candles = frame([
        (100.0, 100.2, 99.8, 100.0),
        (100.0, 102.0, 99.8, 101.5),
    ])
    result = simulate_single_tp(signal(), candles, 0, 1.5)
    assert result["status"] == "tp"
    assert result["gross_r"] > 1.3
    assert result["fee_r"] > 0
    assert result["net_r"] < result["gross_r"]


def test_single_tp_stop_wins_when_stop_and_target_overlap():
    candles = frame([
        (100.0, 100.2, 99.8, 100.0),
        (100.0, 102.0, 98.0, 100.0),
    ])
    result = simulate_single_tp(signal(), candles, 0, 1.5)
    assert result["status"] == "sl"
    assert result["gross_r"] < 0


def test_single_tp_short_uses_downside_target():
    candles = frame([
        (100.0, 100.2, 99.8, 100.0),
        (100.0, 100.2, 97.5, 98.0),
    ])
    result = simulate_single_tp(signal("SELL"), candles, 0, 1.5)
    assert result["status"] == "tp"
    assert result["gross_r"] > 1.3
