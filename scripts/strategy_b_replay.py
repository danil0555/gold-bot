"""Leakage-safe historical replay adapter for the exact production Strategy B.

Input candle timestamps are OPEN times in milliseconds. At each decision time,
only candles whose close time is <= that decision time are passed to the
unchanged production builder. The signal builder is imported lazily from
main.py; this module never starts the bot or initializes its database.
"""
from __future__ import annotations

import hashlib
import importlib
import inspect
import math
import os
from pathlib import Path
import tempfile
from collections.abc import Callable, Mapping

import pandas as pd


TIMEFRAME_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
}
REQUIRED_COLUMNS = {"open_time", "open", "high", "low", "close", "volume"}


def _load_production_builder():
    # main.py requires a token at import time. Supply a harmless placeholder
    # and isolate its default paths; no Bot is instantiated and no DB is opened.
    old = {key: os.environ.get(key) for key in ("BOT_TOKEN", "DB_PATH", "BACKUP_DIR")}
    with tempfile.TemporaryDirectory(prefix="strategy-b-replay-") as tmp:
        os.environ["BOT_TOKEN"] = "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi"
        os.environ["DB_PATH"] = str(Path(tmp) / "replay-never-created.db")
        os.environ["BACKUP_DIR"] = str(Path(tmp) / "backups")
        try:
            production = importlib.import_module("main")
            builder = production.build_signal_b
            version = str(production.BOT_VERSION)
            source_path = Path(production.__file__).resolve()
            source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    builder_source = inspect.getsource(builder).encode("utf-8")
    builder_sha256 = hashlib.sha256(builder_source).hexdigest()
    return builder, version, source_sha256, builder_sha256


def _prepare_frame(
    symbol: str, timeframe: str, frame: pd.DataFrame, interval_ms: int
) -> pd.DataFrame:
    missing = REQUIRED_COLUMNS - set(frame.columns)
    if missing:
        raise ValueError(f"{symbol} {timeframe}: missing columns {sorted(missing)}")
    out = frame.copy()
    out["open_time"] = pd.to_numeric(out.open_time, errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        out[column] = pd.to_numeric(out[column], errors="coerce")
    if out[list(REQUIRED_COLUMNS)].isna().any().any():
        raise ValueError(f"{symbol} {timeframe}: null or non-numeric candle values")
    out["open_time"] = out.open_time.astype("int64")
    if out.duplicated("open_time").any():
        raise ValueError(f"{symbol} {timeframe}: duplicate candle timestamps")
    out = out.sort_values("open_time").reset_index(drop=True)
    timestamps = out.open_time.to_numpy(dtype="int64")
    if (timestamps % interval_ms != 0).any():
        raise ValueError(f"{symbol} {timeframe}: candle open times are not timeframe-aligned")
    if len(timestamps) > 1 and (timestamps[1:] - timestamps[:-1] != interval_ms).any():
        raise ValueError(f"{symbol} {timeframe}: missing or non-contiguous candles")
    if (out[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError(f"{symbol} {timeframe}: OHLC prices must be positive")
    if (out.volume < 0).any():
        raise ValueError(f"{symbol} {timeframe}: volume cannot be negative")
    if (out.high < out[["open", "close", "low"]].max(axis=1)).any():
        raise ValueError(f"{symbol} {timeframe}: invalid high")
    if (out.low > out[["open", "close", "high"]].min(axis=1)).any():
        raise ValueError(f"{symbol} {timeframe}: invalid low")
    return out


def replay_strategy_b(
    candles_by_symbol: Mapping[str, Mapping[str, pd.DataFrame]],
    *,
    quote_by_symbol: Mapping[str, Mapping] | None = None,
    min_history_rows: int = 205,
    signal_builder: Callable | None = None,
) -> dict:
    """Replay unchanged Strategy B over closed, timestamp-aligned candles.

    All frames must use OPEN timestamps in milliseconds and contain OHLCV.
    The 1m candle close defines each decision timestamp. Higher timeframes are
    truncated to candles fully closed by that timestamp. A setup is emitted
    once, even if it remains valid over multiple 1m decisions.
    """
    if not isinstance(candles_by_symbol, Mapping) or not candles_by_symbol:
        raise ValueError("candles_by_symbol must be a non-empty mapping")
    if min_history_rows < 205:
        raise ValueError("min_history_rows cannot be below production MIN_CANDLE_ROWS")
    if signal_builder is None:
        builder, checked_out_version, checked_out_main_sha256, builder_sha256 = _load_production_builder()
    else:
        builder = signal_builder
        checked_out_version = None
        checked_out_main_sha256 = None
        builder_sha256 = None

    output = []
    diagnostics = {"decisions": 0, "insufficient_history": 0,
                  "no_signal": 0, "duplicate_setup": 0}
    for symbol, frames in candles_by_symbol.items():
        symbol = str(symbol)
        missing_tf = set(TIMEFRAME_MS) - set(frames)
        if missing_tf:
            raise ValueError(f"{symbol}: missing timeframes {sorted(missing_tf)}")
        prepared = {
            tf: _prepare_frame(symbol, tf, frames[tf], interval_ms)
            for tf, interval_ms in TIMEFRAME_MS.items()
        }
        decision_times = (
            prepared["1m"].open_time + TIMEFRAME_MS["1m"]
        ).astype("int64").tolist()
        seen_setups = set()
        for decision_ts in decision_times:
            aligned = {}
            insufficient = False
            for tf, interval_ms in TIMEFRAME_MS.items():
                # Strict close-time condition prevents unfinished/future bars.
                closed = prepared[tf].loc[
                    prepared[tf].open_time + interval_ms <= decision_ts
                ]
                if len(closed) < min_history_rows:
                    insufficient = True
                    break
                aligned[tf] = closed.tail(max(min_history_rows, 300)).copy()
            if insufficient:
                diagnostics["insufficient_history"] += 1
                continue
            diagnostics["decisions"] += 1
            quote = (quote_by_symbol or {}).get(symbol)
            if quote is not None and not isinstance(quote, Mapping):
                raise ValueError(f"{symbol}: quote must be a mapping")
            signal = builder("swap", symbol, aligned, quote=quote)
            if signal is None:
                diagnostics["no_signal"] += 1
                continue
            if not isinstance(signal, Mapping):
                raise ValueError(f"{symbol}: Strategy B builder must return a mapping or None")
            side = str(signal.get("side", "")).upper()
            if side not in {"BUY", "SELL"}:
                raise ValueError(f"{symbol}: Strategy B returned unsupported side {side!r}")
            required_signal_fields = ("entry", "sl", "tp1", "tp2", "quality_score")
            missing_signal_fields = [key for key in required_signal_fields if key not in signal]
            if missing_signal_fields:
                raise ValueError(
                    f"{symbol}: Strategy B signal missing fields {missing_signal_fields}"
                )
            levels = {}
            for key in required_signal_fields:
                try:
                    value = float(signal[key])
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError(f"{symbol}: invalid signal field {key}") from exc
                if not pd.notna(value) or not math.isfinite(value):
                    raise ValueError(f"{symbol}: non-finite signal field {key}")
                levels[key] = value
            entry, sl, tp1, tp2 = (
                levels["entry"], levels["sl"], levels["tp1"], levels["tp2"]
            )
            if min(entry, sl, tp1, tp2) <= 0:
                raise ValueError(f"{symbol}: signal price levels must be positive")
            if side == "BUY" and not (sl < entry < tp1 < tp2):
                raise ValueError(f"{symbol}: invalid BUY SL/entry/TP ordering")
            if side == "SELL" and not (tp2 < tp1 < entry < sl):
                raise ValueError(f"{symbol}: invalid SELL SL/entry/TP ordering")
            setup_key = str(signal.get("setup_key") or "")
            if not setup_key:
                raise ValueError("Strategy B signal missing setup_key; cannot deduplicate safely")
            if setup_key in seen_setups:
                diagnostics["duplicate_setup"] += 1
                continue
            seen_setups.add(setup_key)
            output.append({
                "symbol": symbol,
                "signal_ts": int(decision_ts),
                "direction": "LONG" if side == "BUY" else "SHORT",
                "setup_key": setup_key,
                "strategy_version": str(signal.get("strategy_version", "B")),
                "entry_reference": entry,
                "sl": sl,
                "tp1": tp1,
                "tp2": tp2,
                "quality_score": levels["quality_score"],
                "market_regime": str(signal.get("market_regime", "unknown")),
            })

    return {
        "report_type": "strategy_b_historical_replay_v1",
        "status": "evaluated" if output else "no_signals",
        "checked_out_bot_version": checked_out_version,
        "checked_out_main_sha256": checked_out_main_sha256,
        "strategy_builder_sha256": builder_sha256,
        "signal_count": len(output),
        "diagnostics": diagnostics,
        "signals": output,
        "methodology": [
            "Calls the unchanged build_signal_b function from main.py.",
            "Input timestamps are candle OPEN times in milliseconds.",
            "Only candles whose full interval ends at or before the 1m decision close are visible.",
            "Each Strategy B setup_key is emitted once.",
            "Signal timestamp is the decision candle close; the execution-cost evaluator enters at the next exact 1m close.",
        ],
        "limitations": [
            "Historical spread snapshots are optional; absent quotes use Strategy B's configured synthetic spread fallback.",
            "The replay imports main.py from the checked-out research branch. Its full-file SHA identifies that exact file; strategy_builder_sha256 separately fingerprints build_signal_b. Verify both against the intended production source before interpreting results.",
            "This replay does not model order fills, intrabar stop/target ordering, latency, market impact, funding or portfolio margin.",
            "The adapter is research-only and never changes live signals or places orders.",
        ],
    }
