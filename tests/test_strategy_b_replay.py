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
    assert result["signal_cooldown_minutes"] == 30
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



def test_replay_applies_exact_production_cooldown_boundary():
    calls = 0

    def builder(market, symbol, frames, quote=None):
        nonlocal calls
        calls += 1
        return {
            "setup_key": f"unique-{calls}", "strategy_version": "B",
            "side": "BUY", "entry": 100.0, "sl": 99.0,
            "tp1": 101.8, "tp2": 103.0, "quality_score": 90.0,
        }

    result = replay_strategy_b(
        {"BTC-USDT": _all_frames()}, signal_builder=builder
    )
    accepted = result["signals"]
    assert result["signal_cooldown_minutes"] == 30
    assert len(accepted) >= 2
    assert all(
        right["signal_ts"] - left["signal_ts"] == 31 * 60_000
        for left, right in zip(accepted, accepted[1:])
    )
    assert result["diagnostics"]["cooldown_rejected"] > 0


def test_replay_cooldown_is_separate_for_long_and_short_sides():
    calls = 0

    def builder(market, symbol, frames, quote=None):
        nonlocal calls
        side = "BUY" if calls % 2 == 0 else "SELL"
        calls += 1
        if side == "BUY":
            levels = {"entry": 100.0, "sl": 99.0, "tp1": 101.8, "tp2": 103.0}
        else:
            levels = {"entry": 100.0, "sl": 101.0, "tp1": 98.2, "tp2": 97.0}
        return {
            "setup_key": f"side-unique-{calls}", "strategy_version": "B",
            "side": side, **levels, "quality_score": 90.0,
        }

    result = replay_strategy_b(
        {"BTC-USDT": _all_frames()}, signal_builder=builder
    )
    longs = [row for row in result["signals"] if row["direction"] == "LONG"]
    shorts = [row for row in result["signals"] if row["direction"] == "SHORT"]
    assert len(longs) >= 2 and len(shorts) >= 2
    assert longs[1]["signal_ts"] - longs[0]["signal_ts"] == 32 * 60_000
    assert shorts[1]["signal_ts"] - shorts[0]["signal_ts"] == 32 * 60_000



def test_default_builder_rejection_diagnostics_are_aggregated(monkeypatch):
    calls = 0

    def builder(market, symbol, frames, quote=None, diagnostics=None):
        nonlocal calls
        calls += 1
        diagnostics["reason"] = (
            "trend_alignment_failed" if calls % 2 else "missing_structure"
        )
        diagnostics["stage"] = "strategy_filter"
        return None

    monkeypatch.setattr(
        "scripts.strategy_b_replay._load_production_builder",
        lambda: (builder, "10.2.2", "a" * 64, "b" * 64, 30),
    )
    result = replay_strategy_b({"BTC-USDT": _all_frames()})

    assert result["status"] == "no_signals"
    assert result["diagnostics"]["no_signal"] == calls
    assert result["diagnostics"]["builder_rejection_reasons"] == {
        "trend_alignment_failed": (calls + 1) // 2,
        "missing_structure": calls // 2,
    }
    assert result["diagnostics"]["builder_rejection_stages"] == {
        "strategy_filter": calls,
    }


def test_default_builder_trend_check_states_are_aggregated(monkeypatch):
    calls = 0
    state_a = "BUY[1H_bull=OK,4H_not_bear=OK] SELL[1H_bear=NO]"
    state_b = "BUY[1H_bull=NO,4H_not_bear=OK] SELL[1H_bear=OK]"

    def builder(market, symbol, frames, quote=None, diagnostics=None):
        nonlocal calls
        calls += 1
        diagnostics["reason"] = "no_trend_alignment"
        diagnostics["stage"] = "strategy_filter"
        diagnostics["trend_checks"] = state_a if calls % 2 else state_b
        return None

    monkeypatch.setattr(
        "scripts.strategy_b_replay._load_production_builder",
        lambda: (builder, "10.2.2", "a" * 64, "b" * 64, 30),
    )
    result = replay_strategy_b({"BTC-USDT": _all_frames()})

    assert result["diagnostics"]["builder_trend_check_states"] == {
        state_a: (calls + 1) // 2,
        state_b: calls // 2,
    }
