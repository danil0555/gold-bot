import pandas as pd
import pytest

from scripts.strategy_b_replay import replay_strategy_b


def _frame(interval_ms, count=210, future_extra=False, offset_ms=0):
    rows = []
    for i in range(count):
        close = 100 + i * 0.01
        rows.append({
            "open_time": offset_ms + i * interval_ms,
            "open": close - 0.01, "high": close + 0.02,
            "low": close - 0.02, "close": close, "volume": 10.0,
        })
    if future_extra:
        i = count
        close = 100 + i * 0.01
        rows.append({
            "open_time": offset_ms + i * interval_ms,
            "open": close - 0.01, "high": close + 0.02,
            "low": close - 0.02, "close": close, "volume": 10.0,
        })
    return pd.DataFrame(rows)


def _all_frames():
    return {
        "1m": _frame(60_000, 212, offset_ms=205 * 14_400_000),
        "5m": _frame(300_000, 212),
        "15m": _frame(900_000, 212),
        "1h": _frame(3_600_000, 212),
        "4h": _frame(14_400_000, 212),
    }


def test_replay_never_passes_unclosed_or_future_candles_to_builder():
    seen = []

    def builder(market, symbol, frames, quote=None):
        seen.append({tf: int(df.open_time.max()) for tf, df in frames.items()})
        return None

    result = replay_strategy_b(
        {"BTC-USDT": _all_frames()}, signal_builder=builder
    )
    assert result["status"] == "no_signals"
    assert seen
    for row in seen:
        assert row["1m"] + 60_000 <= max(_all_frames()["1m"].open_time) + 60_000
        assert row["5m"] + 300_000 <= row["1m"] + 60_000
        assert row["15m"] + 900_000 <= row["1m"] + 60_000
        assert row["1h"] + 3_600_000 <= row["1m"] + 60_000
        assert row["4h"] + 14_400_000 <= row["1m"] + 60_000


def test_replay_emits_each_setup_once_and_maps_direction():
    calls = 0

    def builder(market, symbol, frames, quote=None):
        nonlocal calls
        calls += 1
        return {
            "setup_key": "B:swap:BTC-USDT:BUY:123",
            "strategy_version": "B", "side": "BUY",
            "entry": 100.0, "sl": 99.0, "tp1": 101.8, "tp2": 103.0,
            "quality_score": 90.0, "market_regime": "trend",
        }

    result = replay_strategy_b(
        {"BTC-USDT": _all_frames()}, signal_builder=builder
    )
    assert result["signal_count"] == 1
    assert result["signals"][0]["direction"] == "LONG"
    assert result["diagnostics"]["duplicate_setup"] == calls - 1


def test_replay_rejects_duplicate_or_invalid_candles():
    frames = _all_frames()
    frames["5m"] = pd.concat([frames["5m"], frames["5m"].iloc[[-1]]], ignore_index=True)
    with pytest.raises(ValueError, match="duplicate"):
        replay_strategy_b({"BTC-USDT": frames}, signal_builder=lambda *a, **k: None)


def test_replay_requires_all_strategy_timeframes():
    frames = _all_frames()
    frames.pop("4h")
    with pytest.raises(ValueError, match="missing timeframes"):
        replay_strategy_b({"BTC-USDT": frames}, signal_builder=lambda *a, **k: None)

def test_default_replay_loads_production_builder_without_starting_bot():
    result = replay_strategy_b({"BTC-USDT": _all_frames()})
    assert result["checked_out_bot_version"] == "10.2.2"
    assert len(result["checked_out_main_sha256"]) == 64
    assert len(result["strategy_builder_sha256"]) == 64
    assert result["status"] in {"evaluated", "no_signals"}



def test_replay_rejects_unknown_side_instead_of_silently_marking_short():
    def builder(market, symbol, frames, quote=None):
        return {
            "setup_key": "bad-side", "side": "HOLD",
            "entry": 100.0, "sl": 99.0, "tp1": 101.8, "tp2": 103.0,
            "quality_score": 90.0,
        }

    with pytest.raises(ValueError, match="unsupported side"):
        replay_strategy_b({"BTC-USDT": _all_frames()}, signal_builder=builder)


def test_replay_rejects_inconsistent_signal_levels():
    def builder(market, symbol, frames, quote=None):
        return {
            "setup_key": "bad-levels", "side": "BUY",
            "entry": 100.0, "sl": 101.0, "tp1": 102.0, "tp2": 103.0,
            "quality_score": 90.0,
        }

    with pytest.raises(ValueError, match="ordering"):
        replay_strategy_b({"BTC-USDT": _all_frames()}, signal_builder=builder)


def test_replay_rejects_non_finite_signal_values():
    def builder(market, symbol, frames, quote=None):
        return {
            "setup_key": "bad-number", "side": "SELL",
            "entry": 100.0, "sl": 101.0, "tp1": 99.0, "tp2": float("nan"),
            "quality_score": 90.0,
        }

    with pytest.raises(ValueError, match="non-finite"):
        replay_strategy_b({"BTC-USDT": _all_frames()}, signal_builder=builder)


def test_replay_accepts_valid_short_signal():
    def builder(market, symbol, frames, quote=None):
        return {
            "setup_key": "short-setup", "side": "SELL",
            "entry": 100.0, "sl": 101.0, "tp1": 98.2, "tp2": 97.0,
            "quality_score": 91.0,
        }

    result = replay_strategy_b(
        {"BTC-USDT": _all_frames()}, signal_builder=builder
    )
    assert result["signal_count"] == 1
    assert result["signals"][0]["direction"] == "SHORT"



def test_replay_rejects_gaps_in_historical_candles():
    frames = _all_frames()
    frames["15m"] = frames["15m"].drop(index=100).reset_index(drop=True)
    with pytest.raises(ValueError, match="non-contiguous"):
        replay_strategy_b(
            {"BTC-USDT": frames}, signal_builder=lambda *a, **k: None
        )


def test_replay_rejects_timeframe_misaligned_candle_open():
    frames = _all_frames()
    frames["5m"].loc[0, "open_time"] += 60_000
    with pytest.raises(ValueError, match="timeframe-aligned"):
        replay_strategy_b(
            {"BTC-USDT": frames}, signal_builder=lambda *a, **k: None
        )
