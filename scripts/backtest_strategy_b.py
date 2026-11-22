#!/usr/bin/env python3
"""Read-only historical backtest of the exact production Strategy B builder.

Uses closed BingX USDT-M candles, causal indicators, multi-timeframe alignment,
next-1m-open entries, the live 30-minute per-symbol/side cooldown, setup-key
deduplication, 70/30 TP1/TP2 exits, conservative stop-first ordering, spread,
slippage and taker fees. No credentials, account endpoints, database or orders.
"""
import argparse
import ast
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from scripts import backtest_addons as market_data

TF_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
}
STRATEGY_CONSTANTS = {
    "MIN_RR": 1.0,
    "MIN_SCALPER_RAW_SCORE": 7.0,
    "MIN_QUALITY_SCORE": 85.0,
    "MIN_NET_RR": 1.30,
    "MIN_SCALPER_ADX": 18.0,
    "MIN_VOLUME_RATIO": 0.85,
    "MIN_ATR_PCT": 0.08,
    "MAX_ATR_PCT": 1.50,
    "MAX_EXTENSION_ATR": 2.20,
    "TP1_R": 1.8,
    "TP2_R": 3.0,
    "MIN_CANDLE_ROWS": 205,
    "STRATEGY_B": "B",
    "TAKER_FEE_RATE": 0.0005,
    "DEFAULT_SYNTHETIC_SPREAD_PCT": 0.04,
    "DEFAULT_SLIPPAGE_PCT": 0.03,
}
COOLDOWN_MS = 30 * 60_000
MINUTE_GAP_TOLERANCE_MS = 90_000


def load_strategy_b():
    """Compile the exact live B functions from main.py without importing bot runtime."""
    source = ast.parse(Path("main.py").read_text(encoding="utf-8"))
    wanted = {
        "safe_float",
        "indicators",
        "enrich_scalper_series",
        "_detect_market_regime",
        "build_signal_b",
    }
    nodes = [
        node for node in source.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    found = {node.name for node in nodes}
    if found != wanted:
        raise RuntimeError(f"Missing Strategy B functions in main.py: {sorted(wanted - found)}")
    namespace = {
        "Any": Any,
        "math": math,
        "np": np,
        "pd": pd,
        **STRATEGY_CONSTANTS,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), namespace)
    return namespace


def fetch_binance_history(symbol, interval, start_ms, end_ms):
    """Fetch public Binance USD-M klines as an explicitly labelled proxy dataset."""
    step = TF_MS[interval]
    cursor = int(start_ms)
    rows = []
    base_url = "https://fapi.binance.com/fapi/v1/klines"
    while cursor < end_ms:
        query = urllib.parse.urlencode({
            "symbol": symbol.replace("-", "").upper(), "interval": interval,
            "startTime": cursor, "endTime": int(end_ms) - 1, "limit": 1500,
        })
        request = urllib.request.Request(base_url + "?" + query, headers={
            "User-Agent": "gold-bot-readonly-backtest/1.0",
        })
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 418:
                raise RuntimeError("Binance temporarily banned this runner IP (HTTP 418)") from exc
            if exc.code == 429:
                raise RuntimeError("Binance rate limit reached (HTTP 429); stopping safely") from exc
            raise
        if isinstance(payload, dict):
            raise RuntimeError(f"Binance kline error for {symbol} {interval}: {payload}")
        if not payload:
            break
        parsed = []
        for row in payload:
            if not isinstance(row, (list, tuple)) or len(row) < 7:
                continue
            open_time, close_time = int(row[0]), int(row[6])
            if cursor <= open_time < end_ms:
                parsed.append((open_time, float(row[1]), float(row[2]), float(row[3]),
                               float(row[4]), float(row[5]), close_time))
        if not parsed:
            break
        rows.extend(parsed)
        next_cursor = parsed[-1][0] + step
        if next_cursor <= cursor:
            raise RuntimeError(f"Non-advancing Binance pagination for {symbol} {interval}")
        cursor = next_cursor
        if len(payload) < 1500:
            break
        time.sleep(0.25)
    frame = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time"
    ])
    if frame.empty:
        raise RuntimeError(f"No Binance candles returned for {symbol} {interval}")
    return frame.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)

def fetch_gate_history(symbol, interval, start_ms, end_ms):
    """Fetch public Gate USDT perpetual candles; volume is normalized from quote turnover."""
    step_ms = TF_MS[interval]
    step_sec = step_ms // 1000
    cursor_sec = int(start_ms // 1000)
    end_sec = int(end_ms // 1000)
    rows = []
    base_url = "https://api.gateio.ws/api/v4/futures/usdt/candlesticks"
    contract = symbol.replace("-", "_").upper()
    while cursor_sec < end_sec:
        window_end = min(cursor_sec + step_sec * 1900, end_sec)
        query = urllib.parse.urlencode({
            "contract": contract, "interval": interval,
            "from": cursor_sec, "to": window_end,
        })
        request = urllib.request.Request(base_url + "?" + query, headers={
            "Accept": "application/json",
            "User-Agent": "gold-bot-readonly-backtest/1.0",
        })
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:1000]
            if exc.code in (418, 429):
                raise RuntimeError(f"Gate API rate/access limit HTTP {exc.code}: {body}; stopping safely") from exc
            raise RuntimeError(f"Gate API HTTP {exc.code} for {symbol} {interval}: {body}") from exc
        if isinstance(payload, dict):
            raise RuntimeError(f"Gate kline error for {symbol} {interval}: {payload}")
        parsed = []
        for row in payload or []:
            try:
                open_sec = int(float(row["t"]))
                close = float(row["c"])
                turnover = float(row.get("sum", 0) or 0)
                volume = turnover / close if close > 0 and turnover > 0 else float(row.get("v", 0) or 0)
                open_time = open_sec * 1000
                if cursor_sec <= open_sec < window_end and close > 0:
                    parsed.append((open_time, float(row["o"]), float(row["h"]),
                                   float(row["l"]), close, volume, open_time + step_ms - 1))
            except (KeyError, TypeError, ValueError, ZeroDivisionError):
                continue
        if not parsed:
            break
        rows.extend(parsed)
        next_cursor = max(item[0] for item in parsed) // 1000 + step_sec
        if next_cursor <= cursor_sec:
            raise RuntimeError(f"Non-advancing Gate pagination for {symbol} {interval}")
        cursor_sec = next_cursor
        if len(parsed) < 1900:
            break
        time.sleep(0.15)
    frame = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time"
    ])
    if frame.empty:
        raise RuntimeError(f"No Gate USDT futures candles returned for {symbol} {interval}")
    return frame.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)

def fetch_okx_history(symbol, interval, start_ms, end_ms):
    """Fetch public OKX USDT perpetual history candles using backward pagination."""
    step_ms = TF_MS[interval]
    bar = {"1h": "1H", "4h": "4H"}.get(interval, interval)
    inst_id = symbol.upper() + "-SWAP"
    cursor = int(end_ms) + 1
    rows = []
    base_url = "https://us.okx.com/api/v5/market/history-candles"
    while cursor > start_ms:
        query = urllib.parse.urlencode({
            "instId": inst_id, "bar": bar, "after": str(cursor), "limit": "300",
        })
        request = urllib.request.Request(base_url + "?" + query, headers={
            "Accept": "application/json",
            "User-Agent": "gold-bot-readonly-backtest/1.0",
        })
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"OKX HTTP {exc.code} for {inst_id} {bar}: {body}") from exc
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            raise RuntimeError(f"OKX kline error for {inst_id} {bar}: {payload}")
        batch = payload.get("data") or []
        if not batch:
            break
        parsed = []
        timestamps = []
        for row in batch:
            try:
                open_time = int(row[0])
                timestamps.append(open_time)
                if open_time < start_ms or open_time >= end_ms:
                    continue
                if len(row) > 8 and str(row[8]) == "0":
                    continue
                close = float(row[4])
                volume = float(row[6]) if len(row) > 6 and row[6] not in (None, "") else float(row[5])
                parsed.append((open_time, float(row[1]), float(row[2]), float(row[3]),
                               close, volume, open_time + step_ms - 1))
            except (IndexError, TypeError, ValueError):
                continue
        rows.extend(parsed)
        oldest = min(timestamps)
        if oldest <= start_ms:
            break
        if oldest >= cursor:
            raise RuntimeError(f"Non-advancing OKX pagination for {inst_id} {bar}")
        cursor = oldest
        time.sleep(0.11)
    frame = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time"
    ])
    if frame.empty:
        raise RuntimeError(f"No OKX swap candles returned for {inst_id} {bar}")
    return frame.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)

def enrich_once(frame, enrich_fn):
    required = {
        "ema20", "ema50", "ema200", "rsi", "atr", "adx", "macd",
        "macd_signal", "vol_ma20", "vol_ratio", "vwap",
        "swing_high_prev", "swing_low_prev", "range_high_20", "range_low_20",
    }
    if required.issubset(frame.columns):
        return frame
    return enrich_fn(frame)


def fetch_history(symbol, interval, start_ms, end_ms, now_ms, source):
    raw = (fetch_okx_history(symbol, interval, start_ms, end_ms) if source == "okx" else fetch_gate_history(symbol, interval, start_ms, end_ms) if source == "gate" else fetch_binance_history(symbol, interval, start_ms, end_ms) if source == "binance" else market_data.fetch_interval(symbol, interval, start_ms, end_ms))
    frame = market_data.filter_closed_candles(raw, now_ms)
    expected = max(1, int((end_ms - start_ms) / TF_MS[interval]))
    coverage = len(frame) / expected
    if coverage < 0.95:
        raise RuntimeError(
            f"Incomplete {interval} history for {symbol}: {len(frame)}/{expected} "
            f"candles ({coverage:.1%}); refusing to publish a result"
        )
    return frame, coverage


def metric_pack(trades):
    closed = [t for t in trades if t["status"] not in ("unresolved", "skipped_drift", "skipped_zone", "unfilled_level")]
    wins = [t["net_r"] for t in closed if t["net_r"] > 0]
    losses = [t["net_r"] for t in closed if t["net_r"] < 0]
    net_values = [t["net_r"] for t in closed]
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for trade in sorted(closed, key=lambda x: x["time"]):
        equity += trade["net_r"]
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "signals_generated": len(trades),
        "closed_trades": len(closed),
        "tp2": sum(t["status"] == "tp2" for t in closed),
        "tp1_then_sl": sum(t["status"] == "tp1_then_sl" for t in closed),
        "sl": sum(t["status"] == "sl" for t in closed),
        "unresolved": sum(t["status"] == "unresolved" for t in trades),
        "skipped_entry_drift": sum(t["status"] == "skipped_drift" for t in trades),
        "wins_net_of_costs": len(wins),
        "losses_net_of_costs": len(losses),
        "win_rate_pct": round(100 * len(wins) / len(closed), 2) if closed else None,
        "gross_profit_r": round(sum(wins), 3),
        "gross_loss_r": round(abs(sum(losses)), 3),
        "profit_factor": round(sum(wins) / abs(sum(losses)), 3) if losses else None,
        "net_r": round(sum(net_values), 3),
        "average_net_r": round(sum(net_values) / len(closed), 4) if closed else None,
        "max_drawdown_r": round(max_dd, 3),
    }


def paired_closed_comparison(paired_trades):
    """Compare only signal IDs that produced closed outcomes in both entry modes."""
    grouped = defaultdict(dict)
    for trade in paired_trades:
        if trade.get("status") not in ("unresolved", "skipped_drift", "skipped_zone", "unfilled_level"):
            grouped[trade["pair_id"]][trade["entry_mode"]] = trade
    matched = [(items["market"], items["level"]) for items in grouped.values()
               if "market" in items and "level" in items]
    if not matched:
        return {"matched_closed_pairs": 0, "mean_market_minus_level_net_r": None,
                "market_wins": 0, "level_wins": 0, "ties": 0}
    deltas = [m["net_r"] - l["net_r"] for m, l in matched]
    return {
        "matched_closed_pairs": len(matched),
        "mean_market_minus_level_net_r": round(sum(deltas) / len(deltas), 4),
        "market_wins": sum(d > 1e-12 for d in deltas),
        "level_wins": sum(d < -1e-12 for d in deltas),
        "ties": sum(abs(d) <= 1e-12 for d in deltas),
    }


def structure_trigger(reasons):
    """Classify the structural trigger from the exact live builder's reason labels."""
    for reason in reasons or []:
        lowered = str(reason).lower()
        if "bos" in lowered:
            return "BOS"
        if "sweep" in lowered:
            return "sweep"
    return "unknown"


def grouped_metric_pack(trades, field):
    """Summarize all signal outcomes by one recorded signal attribute."""
    groups = defaultdict(list)
    for trade in trades:
        groups[str(trade.get(field) or "unknown")].append(trade)
    return {
        key: metric_pack(sorted(items, key=lambda item: item["time"]))
        for key, items in sorted(groups.items())
    }


def confirmation_passed(side, candle):
    """Apply the same closed-5m EMA/MACD/RSI alignment as the live builder."""
    close, ema20, ema50 = (float(candle[k]) for k in ("close", "ema20", "ema50"))
    macd, macd_signal, rsi = (float(candle[k]) for k in ("macd", "macd_signal", "rsi"))
    if side == "BUY":
        return close > ema20 > ema50 and macd > macd_signal and 50 <= rsi <= 72
    return close < ema20 < ema50 and macd < macd_signal and 28 <= rsi <= 50


def resolve_signal_entry(signal, minute_frame, frames, close_times, signal_index, max_wait_minutes=8, entry_mode=None):
    """Resolve market or level execution for a signal; optional override enables paired tests."""
    if signal_index + 1 >= len(minute_frame):
        return None
    selected_mode = entry_mode or signal.get("entry_mode", "market")
    if selected_mode != "level":
        return {"entry_index": signal_index + 1,
                "raw_entry": float(minute_frame.iloc[signal_index + 1]["open"]),
                "entry_mode": "market"}
    zone_low = float(signal.get("entry_zone_low") or 0)
    zone_high = float(signal.get("entry_zone_high") or 0)
    if zone_low <= 0 or zone_high < zone_low:
        return None
    touch_close_time = None
    last_index = min(len(minute_frame) - 2, signal_index + int(max_wait_minutes))
    for j in range(signal_index + 1, last_index + 1):
        bar = minute_frame.iloc[j]
        if touch_close_time is None:
            if float(bar["low"]) <= zone_high and float(bar["high"]) >= zone_low:
                touch_close_time = int(bar["close_time"])
            continue
        available_5m = market_data.closed_window(frames["5m"], close_times["5m"], int(bar["close_time"]), 300)
        if available_5m.empty:
            continue
        confirm = available_5m.iloc[-1]
        if int(confirm["close_time"]) <= touch_close_time:
            continue
        if not confirmation_passed(signal["side"], confirm):
            continue
        entry_index = j + 1
        raw_entry = float(minute_frame.iloc[entry_index]["open"])
        if not zone_low <= raw_entry <= zone_high:
            return {"status": "skipped_zone", "entry_index": entry_index,
                    "raw_entry": raw_entry, "entry_mode": "level"}
        return {"entry_index": entry_index, "raw_entry": raw_entry, "entry_mode": "level"}
    return {"status": "unfilled_level", "entry_index": last_index, "entry_mode": "level"}


def simulate_strategy_b_trade(signal, minute_frame, entry_index, raw_entry):
    """Simulate the resolved fill using original SL/TP levels and conservative intrabar ordering."""
    side = signal["side"]
    raw_entry = float(raw_entry)
    reference_risk = abs(float(signal["entry"]) - float(signal["sl"]))
    drift_r = abs(raw_entry - float(signal["entry"])) / max(reference_risk, 1e-12)
    max_drift_r = 1.0 if signal.get("entry_mode") == "level" else 0.5
    if drift_r > max_drift_r:
        return {"status": "skipped_drift", "exit_index": entry_index, "raw_entry": raw_entry}
    adverse = (STRATEGY_CONSTANTS["DEFAULT_SYNTHETIC_SPREAD_PCT"] / 2.0 + STRATEGY_CONSTANTS["DEFAULT_SLIPPAGE_PCT"]) / 100.0
    entry = raw_entry * (1 + adverse if side == "BUY" else 1 - adverse)
    if signal.get("entry_mode") == "market":
        # Mirror live enrichment: preserve risk distance and move SL/TP with bid/ask.
        risk = abs(float(signal["entry"]) - float(signal["sl"]))
        if side == "BUY":
            sl, tp1, tp2 = entry - risk, entry + 1.8 * risk, entry + 3.0 * risk
        else:
            sl, tp1, tp2 = entry + risk, entry - 1.8 * risk, entry - 3.0 * risk
    else:
        sl, tp1, tp2 = float(signal["sl"]), float(signal["tp1"]), float(signal["tp2"])
        risk = abs(entry - sl)
    if risk <= 0:
        return None
    tp1_hit = False
    exit_index = None
    exit_kind = None
    exit_levels = None
    for j in range(entry_index, len(minute_frame)):
        bar = minute_frame.iloc[j]
        high, low = float(bar["high"]), float(bar["low"])
        if side == "BUY":
            stop_hit, first_hit, second_hit = low <= sl, high >= tp1, high >= tp2
        else:
            stop_hit, first_hit, second_hit = high >= sl, low <= tp1, low <= tp2
        if stop_hit:
            stop_fill = sl
            bar_open = float(bar["open"])
            if side == "BUY" and bar_open < sl:
                stop_fill = bar_open
            elif side == "SELL" and bar_open > sl:
                stop_fill = bar_open
            exit_kind = "tp1_then_sl" if tp1_hit else "sl"
            exit_levels = [tp1, stop_fill] if tp1_hit else [stop_fill]
            exit_index = j
            break
        if not tp1_hit and first_hit:
            tp1_hit = True
        if tp1_hit and second_hit:
            exit_kind, exit_levels, exit_index = "tp2", [tp1, tp2], j
            break
    if exit_index is None:
        return {"status": "unresolved", "exit_index": len(minute_frame) - 1,
                "entry": entry, "raw_entry": raw_entry, "entry_index": entry_index}
    weights = [0.7, 0.3] if len(exit_levels) == 2 else [1.0]
    gross_pnl = 0.0
    exit_notional = 0.0
    fee_rate = STRATEGY_CONSTANTS["TAKER_FEE_RATE"]
    for level, weight in zip(exit_levels, weights):
        fill = level * (1 - adverse if side == "BUY" else 1 + adverse)
        pnl = (fill - entry) if side == "BUY" else (entry - fill)
        gross_pnl += weight * pnl
        exit_notional += weight * fill
    gross_r = gross_pnl / risk
    fee_r = fee_rate * (entry + exit_notional) / risk
    return {"status": exit_kind, "gross_r": gross_r, "fee_r": fee_r,
            "net_r": gross_r - fee_r, "exit_index": exit_index,
            "entry": entry, "raw_entry": raw_entry, "entry_index": entry_index}

def run(args):
    if args.warmup_days < 35:
        raise ValueError("--warmup-days must be at least 35 to warm up the 4h EMA200")
    now_ms = int(time.time() * 1000)
    evaluation_start = now_ms - args.days * 86_400_000
    history_start = evaluation_start - args.warmup_days * 86_400_000
    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        raise ValueError("At least one symbol is required")

    # The shared read-only downloader is reused; extend its interval table for 1h/4h.
    market_data.INTERVAL_MS.update(TF_MS)
    strategy = load_strategy_b()
    original_enrich = strategy["enrich_scalper_series"]
    strategy["enrich_scalper_series"] = lambda frame: enrich_once(frame, original_enrich)
    print(
        f"STRATEGY_B_BACKTEST days={args.days} warmup_days={args.warmup_days} "
        f"symbols={','.join(symbols)}",
        flush=True,
    )

    trades = []
    paired_trades = []
    rejection_counts = Counter()
    per_symbol_rejections = {symbol: Counter() for symbol in symbols}
    coverage_report = {}
    evaluations = Counter()
    generated_by_symbol = Counter()
    skipped_duplicate = Counter()
    skipped_cooldown = Counter()
    replay_events = []
    replay_target_ms = (int(datetime.fromisoformat(args.replay_time_utc.replace("Z", "+00:00")).timestamp() * 1000)
                       if args.replay_time_utc else None)
    replay_symbol = args.replay_symbol.upper() if args.replay_symbol else None

    for symbol in symbols:
        print(f"FETCH {symbol}", flush=True)
        frames = {}
        coverage_report[symbol] = {}
        for tf in ("1m", "5m", "15m", "1h", "4h"):
            print(f"FETCH_INTERVAL {symbol} {tf}", flush=True)
            frame, coverage = fetch_history(
                symbol, tf, history_start, now_ms, now_ms, args.source
            )
            frames[tf] = original_enrich(frame)
            coverage_report[symbol][tf] = {
                "candles": len(frame), "coverage_pct": round(coverage * 100, 2)
            }
            print(
                f"FETCHED {symbol} {tf}: {len(frame)} closed candles "
                f"coverage={coverage:.1%}",
                flush=True,
            )

        minute = frames["1m"]
        close_times = {
            tf: frame["close_time"].to_numpy(dtype=np.int64)
            for tf, frame in frames.items()
        }
        eligible = minute.index[minute["close_time"] >= evaluation_start]
        if len(eligible) == 0:
            raise RuntimeError(f"No evaluation candles for {symbol}")
        start_index = int(eligible[0])

        # A setup_key is unique in the live DB; cooldown is per symbol and side.
        seen_setup_keys = set()
        last_signal_ms = {}
        for i in range(start_index, len(minute) - 1):
            stamp = int(minute.iloc[i]["close_time"])
            next_open_time = int(minute.iloc[i + 1]["open_time"])
            if next_open_time - stamp > MINUTE_GAP_TOLERANCE_MS:
                continue

            available = {
                tf: market_data.closed_window(frame, close_times[tf], stamp, 300)
                for tf, frame in frames.items()
            }
            if any(len(available[tf]) < STRATEGY_CONSTANTS["MIN_CANDLE_ROWS"]
                   for tf in available):
                continue

            evaluations[symbol] += 1
            diagnostics = {}
            signal = strategy["build_signal_b"](
                "swap", symbol, available, quote=None, diagnostics=diagnostics
            )
            replay_event = None
            if (replay_target_ms is not None and symbol == replay_symbol
                    and abs(stamp - replay_target_ms) <= 60_000):
                replay_event = {
                    "timestamp": datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).isoformat(),
                    "signal_detected": signal is not None,
                    "diagnostics": dict(diagnostics),
                    "signal": ({key: signal.get(key) for key in (
                        "side", "entry", "entry_mode", "entry_zone_low", "entry_zone_high",
                        "sl", "tp1", "tp2", "quality_score", "net_rr", "reasons",
                        "confirmation_rule",
                    )} if signal else None),
                }
                replay_events.append(replay_event)
            if signal is None:
                reason = diagnostics.get("reason", "unknown_rejection")
                rejection_counts[reason] += 1
                per_symbol_rejections[symbol][reason] += 1
                continue

            generated_by_symbol[symbol] += 1
            setup_key = signal["setup_key"]
            if setup_key in seen_setup_keys:
                skipped_duplicate[symbol] += 1
                continue
            seen_setup_keys.add(setup_key)

            side_key = (symbol, signal["side"])
            if stamp < last_signal_ms.get(side_key, -1):
                skipped_cooldown[symbol] += 1
                continue

            # Live validation permits up to 0.5R drift for market entries and
            # up to 1.0R for level entries.
            if args.paired_entry_comparison:
                for paired_mode in ("market", "level"):
                    paired_resolved = resolve_signal_entry(
                        signal, minute, frames, close_times, i,
                        max_wait_minutes=args.level_entry_wait_minutes,
                        entry_mode=paired_mode,
                    )
                    if paired_resolved is None:
                        paired_outcome = {"status": "unresolved"}
                    elif paired_resolved.get("status"):
                        paired_outcome = {
                            "status": paired_resolved["status"],
                            "exit_index": paired_resolved["entry_index"],
                            "raw_entry": paired_resolved.get("raw_entry"),
                        }
                    else:
                        paired_signal = dict(signal, entry_mode=paired_mode)
                        paired_outcome = simulate_strategy_b_trade(
                            paired_signal, minute,
                            paired_resolved["entry_index"], paired_resolved["raw_entry"],
                        ) or {"status": "unresolved"}
                    paired_trades.append({
                        "pair_id": setup_key, "symbol": symbol, "side": signal["side"],
                        "time": datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).isoformat(),
                        "entry_mode": paired_mode,
                        "market_regime": signal.get("market_regime", "unknown"),
                        **paired_outcome,
                    })

            resolved = resolve_signal_entry(
                signal, minute, frames, close_times, i,
                max_wait_minutes=args.level_entry_wait_minutes,
            )
            if resolved is None:
                if replay_event is not None:
                    replay_event["entry_resolution"] = None
                continue
            if replay_event is not None:
                replay_event["entry_resolution"] = dict(resolved)
            if resolved.get("status"):
                outcome = {"status": resolved["status"], "exit_index": resolved["entry_index"],
                           "raw_entry": resolved.get("raw_entry")}
            else:
                outcome = simulate_strategy_b_trade(
                    signal, minute, resolved["entry_index"], resolved["raw_entry"]
                )
            if outcome is None:
                continue
            if replay_event is not None:
                replay_event["outcome"] = dict(outcome)
            last_signal_ms[side_key] = stamp + COOLDOWN_MS
            trades.append({
                "symbol": symbol,
                "side": signal["side"],
                "time": datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).isoformat(),
                "entry": round(float(outcome.get("raw_entry") or signal["entry"]), 10),
                "signal_entry": round(float(signal["entry"]), 10),
                "entry_mode": signal.get("entry_mode", "market"),
                "entry_zone_low": signal.get("entry_zone_low"),
                "entry_zone_high": signal.get("entry_zone_high"),
                "sl": round(float(signal["sl"]), 10),
                "tp1": round(float(signal["tp1"]), 10),
                "tp2": round(float(signal["tp2"]), 10),
                "quality_score": signal["quality_score"],
                "score": signal.get("score"),
                "market_regime": signal.get("market_regime", "unknown"),
                "structure_trigger": structure_trigger(signal.get("reasons", [])),
                "reasons": list(signal.get("reasons", [])),
                "net_rr_estimate": round(float(signal["net_rr"]), 3),
                **outcome,
            })
        print(
            f"DONE {symbol}: evaluations={evaluations[symbol]} "
            f"generated={generated_by_symbol[symbol]} "
            f"trades={sum(t['symbol'] == symbol for t in trades)}",
            flush=True,
        )

    report = {
        "strategy": "B",
        "source": "exact build_signal_b and indicator functions extracted from main.py",
        "market_data_source": args.source,
        "market_data_note": ("OKX USDT perpetual history candles; OHLCV may differ from BingX live feed." if args.source == "okx" else "Gate USDT perpetual candles; volume normalized from quote turnover and may differ from BingX." if args.source == "gate" else "Binance USD-M futures candles; price/volume may differ from BingX live feed." if args.source == "binance" else "BingX USDT-M futures candles."),
        "period_days": args.days,
        "warmup_days": args.warmup_days,
        "symbols": symbols,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "method": (
            "Only fully closed candles from the selected public futures data source; indicators "
            "precomputed causally; at each 1m close use the latest closed 1m/5m/15m/1h/4h "
            "candles. Market entries use the next 1m open; level entries wait for a zone "
            "touch and a later closed 5m candle satisfying the same EMA/MACD/RSI checks, "
            "then use the next 1m open only if it remains inside the zone. Level-entry "
            "wait is capped at the configured number of minutes. Live 30m per-symbol/side "
            "cooldown and setup-key deduplication; original SL/TP levels retained; 1.8R "
            "TP1 on 70%, 3R TP2 on 30%; conservative stop-first intrabar; adverse "
            "half-spread plus slippage on each fill; 0.05% taker fee each side; funding "
            "excluded. The live 15-symbol scan cap is not modeled in this focused study."
        ),
        "cost_assumptions": {
            "spread_pct": 0.04,
            "slippage_pct_per_side": 0.03,
            "taker_fee_pct_per_side": 0.05,
            "max_entry_drift_r": {"market": 0.5, "level": 1.0},
            "level_entry_confirmation": "closed 5m candle after zone touch; same EMA/MACD/RSI checks as live builder",
            "level_entry_max_wait_minutes": args.level_entry_wait_minutes,
        },
        "data_coverage": coverage_report,
        "evaluations_by_symbol": dict(evaluations),
        "signals_generated_by_symbol": dict(generated_by_symbol),
        "duplicate_setup_skips_by_symbol": dict(skipped_duplicate),
        "cooldown_skips_by_symbol": dict(skipped_cooldown),
        "rejection_counts": dict(rejection_counts.most_common()),
        "rejections_by_symbol": {
            symbol: dict(counts.most_common())
            for symbol, counts in per_symbol_rejections.items()
        },
        "overall": metric_pack(trades),
        "paired_entry_comparison": ({
            "signals_compared": len({t["pair_id"] for t in paired_trades}),
            "by_mode": {
                mode: metric_pack([t for t in paired_trades if t["entry_mode"] == mode])
                for mode in ("market", "level")
            },
            "by_symbol_and_mode": {
                symbol: {
                    mode: metric_pack([t for t in paired_trades
                                       if t["symbol"] == symbol and t["entry_mode"] == mode])
                    for mode in ("market", "level")
                } for symbol in symbols
            },
            "paired_closed_comparison": paired_closed_comparison(paired_trades),
        } if args.paired_entry_comparison else None),
        "by_symbol": {
            symbol: metric_pack([t for t in trades if t["symbol"] == symbol])
            for symbol in symbols
        },
        "by_side": grouped_metric_pack(trades, "side"),
        "by_market_regime": grouped_metric_pack(trades, "market_regime"),
        "by_structure_trigger": grouped_metric_pack(trades, "structure_trigger"),
        "by_entry_mode": grouped_metric_pack(trades, "entry_mode"),
        "by_raw_score": grouped_metric_pack(trades, "score"),
        "by_quality_score": grouped_metric_pack(trades, "quality_score"),
        "trades": sorted(trades, key=lambda x: x["time"]),
        "replay_events": replay_events,
    }
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print("STRATEGY_B_REPORT_BEGIN")
    print(rendered)
    print("STRATEGY_B_REPORT_END")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary:
            overall = report["overall"]
            summary.write("# Strategy B historical backtest\n\n")
            summary.write(
                f"Period: {args.days} days; warm-up: {args.warmup_days} days; "
                f"symbols: {', '.join(symbols)}\n\n"
            )
            summary.write(
                "| Trades | Win rate | Profit factor | Net R | Avg net R | Max DD R |\n"
                "|---:|---:|---:|---:|---:|---:|\n"
            )
            summary.write(
                f"| {overall['closed_trades']} | {overall['win_rate_pct']}% | "
                f"{overall['profit_factor']} | {overall['net_r']} | "
                f"{overall['average_net_r']} | {overall['max_drawdown_r']} |\n\n"
            )
            summary.write("## Rejection reasons\n\n")
            summary.write("| Reason | Count |\n|---|---:|\n")
            for reason, count in rejection_counts.most_common():
                summary.write(f"| {reason} | {count} |\n")
            summary.write(
                "\nThis is a historical simulation, not a guarantee of future results. "
                "Funding costs and the full 15-symbol portfolio scan cap are excluded.\n"
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--warmup-days", type=int, default=60)
    parser.add_argument("--symbols", default="BTC-USDT,ETH-USDT,SOL-USDT")
    parser.add_argument("--source", choices=("bingx", "binance", "gate", "okx"), default="bingx")
    parser.add_argument("--level-entry-wait-minutes", type=int, default=8)
    parser.add_argument("--paired-entry-comparison", action="store_true",
                        help="Evaluate market and level execution on the same eligible signals")
    parser.add_argument("--replay-symbol", default="")
    parser.add_argument("--replay-time-utc", default="")
    args = parser.parse_args()
    if not 1 <= args.days <= 60:
        parser.error("--days must be between 1 and 60")
    if bool(args.replay_symbol) != bool(args.replay_time_utc):
        parser.error("--replay-symbol and --replay-time-utc must be supplied together")
    run(args)
