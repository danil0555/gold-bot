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
    assert report["trades"][0]["gross_return_pct"] == pytest.approx((103 / 101 - 1) * 100)
    assert report["trades"][0]["execution_cost_pct"] == pytest.approx(0.14)
    assert report["trades"][1]["gross_return_pct"] == pytest.approx((102 / 104 - 1) * 100)


def test_missing_exact_entry_or_exit_is_rejected_not_bridged():
    candles = _candles().loc[lambda x: x.minute_ts != 2 * 60_000]
    report = run_close_to_close_backtest(
        _signals([("BTC", 60_000, "LONG")]), candles,
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
