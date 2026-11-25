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
# Fallback for injected test builders; the checked-out production constant is loaded below.
SIGNAL_COOLDOWN_MINUTES = 30


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
            cooldown_minutes = int(production.SIGNAL_COOLDOWN_MINUTES)
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
    builder_source = inspect.getsource(builder).encode("utf-8")
    builder_sha256 = hashlib.sha256(builder_source).hexdigest()
    return builder, version, source_sha256, builder_sha256, cooldown_minutes


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
    collect_builder_diagnostics = signal_builder is None
    if collect_builder_diagnostics:
        (builder, checked_out_version, checked_out_main_sha256,
         builder_sha256, cooldown_minutes) = _load_production_builder()
    else:
        builder = signal_builder
        checked_out_version = None
        checked_out_main_sha256 = None
        builder_sha256 = None
        cooldown_minutes = SIGNAL_COOLDOWN_MINUTES

    if not isinstance(cooldown_minutes, int) or cooldown_minutes < 1:
        raise ValueError("production signal cooldown must be a positive integer")

    output = []
    diagnostics = {
        "decisions": 0,
        "insufficient_history": 0,
        "no_signal": 0,
        "duplicate_setup": 0,
        "cooldown_rejected": 0,
        "builder_rejection_reasons": {},
        "builder_rejection_stages": {},
        "builder_trend_check_states": {},
        "builder_rejections_by_symbol": {},
    }
    last_accepted_by_cooldown: dict[tuple[str, str, str, str], int] = {}
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
            if collect_builder_diagnostics:
                builder_diagnostics = {}
                signal = builder(
                    "swap", symbol, aligned, quote=quote,
                    diagnostics=builder_diagnostics,
                )
            else:
                builder_diagnostics = None
                signal = builder("swap", symbol, aligned, quote=quote)
            if signal is None:
                diagnostics["no_signal"] += 1
                if builder_diagnostics is not None:
                    reason = str(builder_diagnostics.get("reason") or "unspecified")
                    stage = str(builder_diagnostics.get("stage") or "unspecified")
                    reasons = diagnostics["builder_rejection_reasons"]
                    stages = diagnostics["builder_rejection_stages"]
                    reasons[reason] = reasons.get(reason, 0) + 1
                    stages[stage] = stages.get(stage, 0) + 1
                    trend_state = builder_diagnostics.get("trend_checks")
                    if trend_state:
                        trend_states = diagnostics["builder_trend_check_states"]
                        trend_state = str(trend_state)
                        trend_states[trend_state] = trend_states.get(trend_state, 0) + 1

                    symbol_stats = diagnostics["builder_rejections_by_symbol"].setdefault(
                        symbol, {"reasons": {}, "stages": {}, "trend_check_states": {}}
                    )
                    symbol_reasons = symbol_stats["reasons"]
                    symbol_stages = symbol_stats["stages"]
                    symbol_reasons[reason] = symbol_reasons.get(reason, 0) + 1
                    symbol_stages[stage] = symbol_stages.get(stage, 0) + 1
                    if trend_state:
                        symbol_trends = symbol_stats["trend_check_states"]
                        symbol_trends[trend_state] = symbol_trends.get(trend_state, 0) + 1
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
            market = str(signal.get("market", "swap")).strip().lower()
            strategy_version = str(signal.get("strategy_version", "B")).strip()
            if not market or not strategy_version:
                raise ValueError(f"{symbol}: signal market and strategy_version must be non-empty")
            cooldown_key = (market, symbol, side, strategy_version)
            previous_signal_ts = last_accepted_by_cooldown.get(cooldown_key)
            cooldown_ms = cooldown_minutes * 60_000
            # Production save_signal rejects an existing same-market/symbol/side/version
            # signal whose created_at is within the last cooldown window, including
            # the exact boundary. Replay decision timestamps stand in for created_at.
            if previous_signal_ts is not None and decision_ts - previous_signal_ts <= cooldown_ms:
                diagnostics["cooldown_rejected"] += 1
                continue
            seen_setups.add(setup_key)
            last_accepted_by_cooldown[cooldown_key] = int(decision_ts)
            output.append({
                "symbol": symbol,
                "signal_ts": int(decision_ts),
                "direction": "LONG" if side == "BUY" else "SHORT",
                "setup_key": setup_key,
                "strategy_version": strategy_version,
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
        "signal_cooldown_minutes": cooldown_minutes,
        "signal_count": len(output),
        "diagnostics": diagnostics,
        "signals": output,
        "methodology": [
            "Calls the unchanged build_signal_b function from main.py.",
            "Input timestamps are candle OPEN times in milliseconds.",
            "Only candles whose full interval ends at or before the 1m decision close are visible.",
            "Each accepted Strategy B setup_key is emitted once; cooldown-rejected setups are not marked as saved.",
            "Production SIGNAL_COOLDOWN_MINUTES is applied per market/symbol/side/strategy_version; the exact boundary is rejected just as in save_signal.",
            "When using the checked-out production builder, its existing rejection reason/stage diagnostics are counted without changing strategy decisions or thresholds.",
            "When trend alignment fails, the builder's existing BUY/SELL sub-check state is aggregated verbatim to expose which timeframe checks most often disagree.",
            "Rejection reasons, stages, and trend-check combinations are also grouped by symbol so cross-symbol bottlenecks are visible.",
            "Signal timestamp is the decision candle close; the execution-cost evaluator enters at the next exact 1m close.",
        ],
        "limitations": [
            "Historical spread snapshots are optional; absent quotes use Strategy B's configured synthetic spread fallback.",
            "The replay imports main.py from the checked-out research branch. Its full-file SHA identifies that exact file; strategy_builder_sha256 separately fingerprints build_signal_b. Verify both against the intended production source before interpreting results.",
            "This replay does not model order fills, intrabar stop/target ordering, latency, market impact, funding or portfolio margin.",
            "The adapter is research-only and never changes live signals or places orders.",
            "Rejection counts describe the checked-out builder's current diagnostic labels; they are not calibrated probabilities and may change when builder wording changes.",
            "Trend-check state counts are exact combinations from the builder, not independent causal estimates for each filter.",
        ],
    }
