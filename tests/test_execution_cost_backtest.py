import pandas as pd
import pytest

from scripts.execution_cost_backtest import run_close_to_close_backtest


def _candles():
    return pd.DataFrame([
        ("BTC", ts * 60_000, price)
        for ts, price in enumerate([100, 101, 103, 102, 104, 105, 106])
    ], columns=["symbol", "minute_ts", "close"])


def _signals(rows):
    return pd.DataFrame(rows, columns=["symbol", "signal_ts", "direction"])


def _run(signals, **kwargs):
    return run_close_to_close_backtest(
        signals, _candles(), horizon_minutes=1,
        fee_bps_per_side=5, slippage_bps_per_side=2, **kwargs
    )


def test_long_and_short_direction_and_round_trip_costs():
    report = _run(_signals([
        ("BTC", 0, "LONG"), ("BTC", 2 * 60_000, "SHORT")
    ]))
    assert report["trade_count"] == 2
    # Signal at 0 is available at candle 0 close (100); next candle closes at 101.
    assert report["trades"][0]["entry_ts"] == 60_000
    assert report["trades"][0]["exit_ts"] == 120_000
    assert report["trades"][0]["entry_price"] == pytest.approx(100)
    assert report["trades"][0]["exit_price"] == pytest.approx(101)
    assert report["trades"][0]["gross_return_pct"] == pytest.approx(1.0)
    assert report["trades"][0]["execution_cost_pct"] == pytest.approx(0.14)
    # Second signal at 2m enters on candle 2 close (103), exits candle 3 close (102).
    assert report["trades"][1]["entry_price"] == pytest.approx(103)
    assert report["trades"][1]["exit_price"] == pytest.approx(102)
    assert report["trades"][1]["gross_return_pct"] == pytest.approx((103 - 102) / 103 * 100)


def test_missing_exact_entry_or_exit_is_rejected_not_bridged():
    candles = _candles().loc[lambda x: x.minute_ts != 0]
    report = run_close_to_close_backtest(
        _signals([("BTC", 0, "LONG")]), candles,
        horizon_minutes=1, fee_bps_per_side=5, slippage_bps_per_side=2
    )
    assert report["trade_count"] == 0
    assert report["rejected_signals"]["missing_entry"] == 1


def test_overlapping_positions_per_symbol_are_rejected():
    report = _run(_signals([
        ("BTC", 0, "LONG"), ("BTC", 60_000, "LONG"),
        ("BTC", 2 * 60_000, "LONG")
    ]))
    assert report["trade_count"] == 2
    assert report["rejected_signals"]["overlap"] == 1


def test_funding_sign_is_directional_and_uses_exact_settlement_time():
    candles = pd.concat([
        _candles(),
        _candles().assign(symbol="ETH")
    ], ignore_index=True)
    events = pd.DataFrame([
        ("BTC", 2 * 60_000, 0.001),
        ("ETH", 2 * 60_000, 0.001),
    ], columns=["symbol", "ts_ms", "funding_rate"])
    report = run_close_to_close_backtest(
        _signals([("BTC", 0, "LONG"), ("ETH", 0, "SHORT")]),
        candles, horizon_minutes=2, fee_bps_per_side=0,
        slippage_bps_per_side=0, funding_events=events,
        expected_funding_timestamps_ms=[2 * 60_000]
    )
    assert report["trade_count"] == 2
    assert report["trades"][0]["funding_return_pct"] == pytest.approx(-0.1)
    assert report["trades"][1]["funding_return_pct"] == pytest.approx(0.1)


def test_missing_expected_funding_event_invalidates_trade():
    report = run_close_to_close_backtest(
        _signals([("BTC", 0, "LONG")]), _candles(),
        horizon_minutes=2, fee_bps_per_side=0, slippage_bps_per_side=0,
        funding_events=pd.DataFrame(columns=["symbol", "ts_ms", "funding_rate"]),
        expected_funding_timestamps_ms=[3 * 60_000]
    )
    assert report["trade_count"] == 0
    assert report["rejected_signals"]["incomplete_funding"] == 1


def test_invalid_inputs_and_duplicate_candles_are_rejected():
    with pytest.raises(ValueError):
        _run(_signals([("BTC", 0, "BUY")]))
    with pytest.raises(ValueError):
        run_close_to_close_backtest(
            _signals([("BTC", 0, "LONG")]),
            pd.concat([_candles(), _candles().iloc[[0]]]),
            horizon_minutes=1, fee_bps_per_side=5, slippage_bps_per_side=2
        )
    with pytest.raises(ValueError):
        _run(_signals([("BTC", 0, "LONG")]), expected_funding_timestamps_ms=[])



def test_profit_factor_without_losses_is_json_safe():
    report = _run(_signals([("BTC", 0, "LONG")]))
    assert report["trades"][0]["net_return_pct"] > 0
    assert report["profit_factor"] is None
    assert report["profit_factor_status"] == "no_losses"



def test_entry_and_exit_use_closes_exactly_one_and_horizon_minutes_after_signal():
    report = run_close_to_close_backtest(
        _signals([("BTC", 0, "LONG")]), _candles(),
        horizon_minutes=2, fee_bps_per_side=0, slippage_bps_per_side=0
    )
    trade = report["trades"][0]
    assert trade["signal_ts"] == 0
    assert trade["entry_ts"] == 60_000
    assert trade["exit_ts"] == 180_000
    assert trade["entry_price"] == pytest.approx(100)
    assert trade["exit_price"] == pytest.approx(103)



def test_missing_exact_exit_is_rejected_not_bridged():
    candles = _candles().loc[lambda x: x.minute_ts != 60_000]
    report = run_close_to_close_backtest(
        _signals([("BTC", 0, "LONG")]), candles,
        horizon_minutes=1, fee_bps_per_side=0, slippage_bps_per_side=0
    )
    assert report["trade_count"] == 0
    assert report["rejected_signals"]["missing_exit"] == 1


def test_unaligned_signal_and_candle_timestamps_are_rejected():
    with pytest.raises(ValueError, match="signal_ts"):
        _run(_signals([("BTC", 1, "LONG")]))
    bad_candles = _candles().copy()
    bad_candles.loc[0, "minute_ts"] += 1
    with pytest.raises(ValueError, match="minute_ts"):
        run_close_to_close_backtest(
            _signals([("BTC", 0, "LONG")]), bad_candles,
            horizon_minutes=1, fee_bps_per_side=0, slippage_bps_per_side=0
        )


def test_non_positive_execution_prices_are_classified_as_invalid():
    candles = _candles().copy()
    candles.loc[candles.minute_ts == 0, "close"] = 0
    report = run_close_to_close_backtest(
        _signals([("BTC", 0, "LONG")]), candles,
        horizon_minutes=1, fee_bps_per_side=0, slippage_bps_per_side=0
    )
    assert report["trade_count"] == 0
    assert report["rejected_signals"]["invalid_price"] == 1
    assert report["rejected_signals"]["missing_entry"] == 0


def test_invalid_and_duplicate_funding_records_fail_closed():
    signals = _signals([("BTC", 0, "LONG")])
    candles = _candles()
    kwargs = dict(
        horizon_minutes=2, fee_bps_per_side=0, slippage_bps_per_side=0,
        expected_funding_timestamps_ms=[2 * 60_000]
    )
    bad_rate = pd.DataFrame(
        [("BTC", 2 * 60_000, float("nan"))],
        columns=["symbol", "ts_ms", "funding_rate"]
    )
    with pytest.raises(ValueError, match="finite"):
        run_close_to_close_backtest(signals, candles, funding_events=bad_rate, **kwargs)

    duplicate = pd.DataFrame(
        [("BTC", 2 * 60_000, 0.001), ("BTC", 2 * 60_000, 0.001)],
        columns=["symbol", "ts_ms", "funding_rate"]
    )
    with pytest.raises(ValueError, match="duplicate"):
        run_close_to_close_backtest(signals, candles, funding_events=duplicate, **kwargs)

    with pytest.raises(ValueError, match="integer milliseconds"):
        run_close_to_close_backtest(
            signals, candles, funding_events=None,
            expected_funding_timestamps_ms=[2 * 60_000 + 0.5],
            horizon_minutes=2, fee_bps_per_side=0, slippage_bps_per_side=0
        )

def test_boolean_horizon_is_not_accepted_as_integer():
    with pytest.raises(ValueError, match="positive integer"):
        run_close_to_close_backtest(
            _signals([("BTC", 0, "LONG")]), _candles(),
            horizon_minutes=True, fee_bps_per_side=0, slippage_bps_per_side=0
        )
