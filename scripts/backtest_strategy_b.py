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
    raw = fetch_binance_history(symbol, interval, start_ms, end_ms) if source == "binance" else market_data.fetch_interval(symbol, interval, start_ms, end_ms)
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


def confirmation_passed(side, candle):
    """Apply the same closed-5m EMA/MACD/RSI alignment as the live builder."""
    close, ema20, ema50 = (float(candle[k]) for k in ("close", "ema20", "ema50"))
    macd, macd_signal, rsi = (float(candle[k]) for k in ("macd", "macd_signal", "rsi"))
    if side == "BUY":
        return close > ema20 > ema50 and macd > macd_signal and 50 <= rsi <= 72
    return close < ema20 < ema50 and macd < macd_signal and 28 <= rsi <= 50


def resolve_signal_entry(signal, minute_frame, frames, close_times, signal_index, max_wait_minutes=8):
    """Resolve a level entry only after zone touch and a later closed 5m confirmation."""
    if signal_index + 1 >= len(minute_frame):
        return None
    if signal.get("entry_mode") != "level":
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
        available_5m = closed_window(frames["5m"], close_times["5m"], int(bar["close_time"]), 300)
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
    rejection_counts = Counter()
    per_symbol_rejections = {symbol: Counter() for symbol in symbols}
    coverage_report = {}
    evaluations = Counter()
    generated_by_symbol = Counter()
    skipped_duplicate = Counter()
    skipped_cooldown = Counter()

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
            resolved = resolve_signal_entry(
                signal, minute, frames, close_times, i,
                max_wait_minutes=args.level_entry_wait_minutes,
            )
            if resolved is None:
                continue
            if resolved.get("status"):
                outcome = {"status": resolved["status"], "exit_index": resolved["entry_index"],
                           "raw_entry": resolved.get("raw_entry")}
            else:
                outcome = simulate_strategy_b_trade(
                    signal, minute, resolved["entry_index"], resolved["raw_entry"]
                )
            if outcome is None:
                continue
            last_signal_ms[side_key] = stamp + COOLDOWN_MS
            trades.append({
                "symbol": symbol,
                "side": signal["side"],
                "time": datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).isoformat(),
                "entry": round(float(outcome.get("raw_entry", signal["entry"])), 10),
                "signal_entry": round(float(signal["entry"]), 10),
                "entry_mode": signal.get("entry_mode", "market"),
                "entry_zone_low": signal.get("entry_zone_low"),
                "entry_zone_high": signal.get("entry_zone_high"),
                "sl": round(float(signal["sl"]), 10),
                "tp1": round(float(signal["tp1"]), 10),
                "tp2": round(float(signal["tp2"]), 10),
                "quality_score": signal["quality_score"],
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
        "market_data_note": ("Binance USD-M futures candles; price/volume may differ from BingX live feed." if args.source == "binance" else "BingX USDT-M futures candles."),
        "period_days": args.days,
        "warmup_days": args.warmup_days,
        "symbols": symbols,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "method": (
            "Only fully closed BingX USDT-M candles; indicators precomputed causally; "
            "at each 1m close use the latest closed 1m/5m/15m/1h/4h candles; signal "
            "entry at next 1m open; 0.5R maximum entry drift; live 30m per-symbol/side "
            "cooldown and setup-key deduplication; 1.8R TP1 on 70%, 3R TP2 on 30%; "
            "conservative stop-first intrabar; adverse half-spread plus slippage on "
            "each fill; 0.05% taker fee each side; funding excluded. The live "
            "15-symbol scan cap is not modeled in this focused 3-symbol study."
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
        "by_symbol": {
            symbol: metric_pack([t for t in trades if t["symbol"] == symbol])
            for symbol in symbols
        },
        "trades": sorted(trades, key=lambda x: x["time"]),
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
    parser.add_argument("--source", choices=("bingx", "binance"), default="bingx")
    parser.add_argument("--level-entry-wait-minutes", type=int, default=8)
    args = parser.parse_args()
    if not 1 <= args.days <= 60:
        parser.error("--days must be between 1 and 60")
    run(args)
