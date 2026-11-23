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


def test_unresolved_trade_blocks_new_entries_until_dataset_end():
    from scripts.backtest_single_tp import update_busy_until

    unresolved = {"status": "unresolved", "exit_index": 99}
    assert update_busy_until(unresolved, 10, 500) == 500


def test_skipped_entry_does_not_block_later_entries():
    from scripts.backtest_single_tp import update_busy_until

    skipped = {"status": "skipped_drift", "exit_index": 11}
    assert update_busy_until(skipped, 10, 500) == 10


def test_closed_trade_blocks_until_its_exit_candle():
    from scripts.backtest_single_tp import update_busy_until

    closed = {"status": "tp", "exit_index": 42}
    assert update_busy_until(closed, 10, 500) == 42
