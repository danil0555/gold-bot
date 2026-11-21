import ast
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


def load_strategy_functions():
    source = ast.parse(Path("main.py").read_text(encoding="utf-8"))
    wanted = {
        "safe_float", "indicators", "enrich_scalper_series", "_addon_reject",
        "_is_usable_ws_1m_frame",
        "_make_addon_signal", "build_signal_c_breakout_retest",
        "build_signal_d_impulse", "build_signal_e_overheat_reversal",
    }
    nodes = [node for node in source.body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
             and node.name in wanted]
    found = {node.name for node in nodes}
    assert found == wanted, f"Missing strategy functions: {wanted - found}"
    namespace = {
        "Any": Any, "math": math, "np": np, "pd": pd,
        "MIN_RR": 1.0, "MIN_NET_RR": 1.30, "MIN_QUALITY_SCORE": 85.0,
        "MIN_CANDLE_ROWS": 60,
        "TP1_R": 1.8, "TP2_R": 3.0,
        "STRATEGY_C": "C_BREAKOUT_RETEST",
        "STRATEGY_D": "D_IMPULSE",
        "STRATEGY_E": "E_OVERHEAT_REVERSAL",
        "TAKER_FEE_RATE": 0.0005,
        "DEFAULT_SLIPPAGE_PCT": 0.03,
        "DEFAULT_SYNTHETIC_SPREAD_PCT": 0.04,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), namespace)
    return namespace


S = load_strategy_functions()


def candles(closes, spread=0.2, volumes=None):
    closes = np.asarray(closes, dtype=float)
    opens = np.r_[closes[0], closes[:-1]]
    highs = np.maximum(opens, closes) + spread
    lows = np.minimum(opens, closes) - spread
    if volumes is None:
        volumes = np.full(len(closes), 100.0)
    return pd.DataFrame({
        "open_time": np.arange(len(closes), dtype=np.int64) * 60_000,
        "close_time": np.arange(len(closes), dtype=np.int64) * 60_000 + 59_999,
        "open": opens, "high": highs, "low": lows,
        "close": closes, "volume": np.asarray(volumes, dtype=float),
    })


def trending_frames():
    base = np.linspace(90.0, 100.0, 100)
    m5 = candles(base, spread=0.8)
    m15 = candles(np.linspace(80.0, 100.0, 100), spread=0.8)
    m1 = candles(np.linspace(95.0, 100.0, 100), spread=0.15)
    return {"1m": m1, "5m": m5, "15m": m15}


def test_addon_risk_levels_and_rr_are_consistent():
    signal = S["_make_addon_signal"](
        "TEST", "BTC-USDT", "BUY", 100.0, 0.5, 6, "test", [], 1
    )
    assert signal is not None
    assert math.isclose(signal["tp1"] - signal["entry"],
                        (signal["entry"] - signal["sl"]) * 1.8)
    assert math.isclose(signal["tp2"] - signal["entry"],
                        (signal["entry"] - signal["sl"]) * 3.0)
    assert signal["net_rr"] >= 1.30
    assert signal["quality_score"] >= 85.0


def test_breakout_retest_requires_both_breakout_and_hold():
    frames = trending_frames()
    m5 = frames["5m"].copy()
    # Previous candle closes above its pre-existing 20-candle range.
    m5.loc[m5.index[-2], ["open", "high", "low", "close", "volume"]] = [
        100.0, 106.0, 99.8, 105.0, 150.0
    ]
    # Current candle retests the old range high and closes back above it.
    m5.loc[m5.index[-1], ["open", "high", "low", "close", "volume"]] = [
        105.0, 106.0, 100.0, 101.5, 150.0
    ]
    frames["5m"] = m5
    diag = {}
    signal = S["build_signal_c_breakout_retest"]("BTC-USDT", frames, diag)
    assert signal is not None, diag
    assert signal["side"] == "BUY"
    # Without the preceding breakout, the retest alone must not trigger.
    m5.loc[m5.index[-2], "close"] = 99.0
    diag = {}
    assert S["build_signal_c_breakout_retest"](
        "BTC-USDT", {**frames, "5m": m5}, diag
    ) is None
    assert diag["reason"] == "no_confirmed_breakout_then_retest"


def test_impulse_requires_large_body_volume_and_5m_alignment():
    frames = trending_frames()
    m1 = frames["1m"].copy()
    m1.loc[m1.index[-1], ["open", "high", "low", "close", "volume"]] = [
        100.0, 101.8, 99.9, 101.6, 500.0
    ]
    frames["1m"] = m1
    diag = {}
    signal = S["build_signal_d_impulse"]("BTC-USDT", frames, diag)
    assert signal is not None, diag
    assert signal["side"] == "BUY"
    m1.loc[m1.index[-1], "volume"] = 100.0
    diag = {}
    assert S["build_signal_d_impulse"](
        "BTC-USDT", {**frames, "1m": m1}, diag
    ) is None
    assert diag["reason"].startswith("volume_below_1_5x")


def test_overheat_reversal_needs_recent_rsi_extreme_and_reversal():
    frames = trending_frames()
    m1 = frames["1m"].copy()
    # Sustained rise creates an RSI extreme; last closed candle reverses sharply.
    m1.loc[m1.index[-7:-1], "close"] = [100.0, 100.3, 100.6, 100.9, 101.2, 101.5]
    m1.loc[m1.index[-7:-1], "open"] = [99.7, 100.0, 100.3, 100.6, 100.9, 101.2]
    m1.loc[m1.index[-7:-1], "high"] = m1.loc[m1.index[-7:-1], "close"] + 0.1
    m1.loc[m1.index[-7:-1], "low"] = m1.loc[m1.index[-7:-1], "open"] - 0.1
    m1.loc[m1.index[-1], ["open", "high", "low", "close", "volume"]] = [
        101.5, 101.6, 98.5, 98.8, 500.0
    ]
    frames["1m"] = m1
    # Put the 5m reference EMA clearly above price so the reversal is not
    # rejected by the strategy's minimum distance-from-EMA condition.
    frames["5m"] = candles(np.linspace(90.0, 110.0, 100), spread=1.5)
    diag = {}
    signal = S["build_signal_e_overheat_reversal"]("BTC-USDT", frames, diag)
    assert signal is not None, diag
    assert signal["side"] == "SELL"
    # Remove the recent overbought condition: no reversal signal should remain.
    m1.loc[m1.index[-7:-1], "close"] = np.linspace(99.0, 99.5, 6)
    m1.loc[m1.index[-7:-1], "open"] = np.linspace(98.8, 99.3, 6)
    diag = {}
    assert S["build_signal_e_overheat_reversal"](
        "BTC-USDT", {**frames, "1m": m1}, diag
    ) is None
    assert diag["reason"].startswith("no_rsi_extreme_reversal")


def test_insufficient_history_is_rejected_cleanly():
    frames = {"1m": candles(np.linspace(1, 2, 20)),
              "5m": candles(np.linspace(1, 2, 20)),
              "15m": candles(np.linspace(1, 2, 20))}
    for name in ("build_signal_c_breakout_retest",
                 "build_signal_d_impulse",
                 "build_signal_e_overheat_reversal"):
        diag = {}
        assert S[name]("BTC-USDT", frames, diag) is None
        assert diag.get("reason", "").startswith("insufficient_")



def test_websocket_1m_frame_requires_full_continuity_and_freshness():
    frame = candles(np.linspace(100.0, 101.0, 100))
    last_close = int(frame.iloc[-1]["close_time"])
    assert S["_is_usable_ws_1m_frame"](frame, last_close + 30_000, 60)
    assert not S["_is_usable_ws_1m_frame"](frame, last_close + 90_001, 60)
    assert not S["_is_usable_ws_1m_frame"](frame, last_close - 1, 60)

    # A gap in the older part of the indicator window must invalidate WS data too.
    gapped = frame.copy()
    gapped.loc[20, "open_time"] += 60_000
    assert not S["_is_usable_ws_1m_frame"](gapped, last_close + 30_000, 60)

    incomplete = frame.iloc[:-1].copy()
    assert not S["_is_usable_ws_1m_frame"](incomplete, last_close + 30_000, 101)
