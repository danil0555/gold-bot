#!/usr/bin/env python3
"""Read-only historical backtest for addon strategies C/D/E using BingX USDT-M candles.

No account credentials, order endpoints, database access, or live bot imports.
Signals are evaluated only on fully closed candles. Entry is the next 1m open.
"""
import argparse
import ast
import json
import math
import os
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
import pandas as pd

BASE = "https://open-api.bingx.com"
PATH = "/openApi/swap/v3/quote/klines"
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000}
STRATEGIES = {
    "C_BREAKOUT_RETEST": ("build_signal_c_breakout_retest", "5m"),
    "D_IMPULSE": ("build_signal_d_impulse", "1m"),
    "E_OVERHEAT_REVERSAL": ("build_signal_e_overheat_reversal", "1m"),
}
TAKER_FEE_RATE = 0.0005
SLIPPAGE_PCT = 0.03
SPREAD_PCT = 0.04
LAST_API_REQUEST = 0.0


def load_builders():
    source = open("main.py", encoding="utf-8").read()
    tree = ast.parse(source)
    wanted = {
        "safe_float", "indicators", "enrich_scalper_series", "_addon_reject",
        "_make_addon_signal", *[v[0] for v in STRATEGIES.values()],
    }
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    found = {n.name for n in nodes}
    if found != wanted:
        raise RuntimeError(f"Could not extract strategy code: missing {sorted(wanted-found)}")
    ns = {
        "Any": object, "math": math, "np": np, "pd": pd,
        "MIN_RR": 1.0, "MIN_NET_RR": 1.30, "MIN_QUALITY_SCORE": 85.0,
        "MIN_CANDLE_ROWS": 60, "TP1_R": 1.8, "TP2_R": 3.0,
        "STRATEGY_C": "C_BREAKOUT_RETEST", "STRATEGY_D": "D_IMPULSE",
        "STRATEGY_E": "E_OVERHEAT_REVERSAL", "TAKER_FEE_RATE": TAKER_FEE_RATE,
        "DEFAULT_SLIPPAGE_PCT": SLIPPAGE_PCT,
        "DEFAULT_SYNTHETIC_SPREAD_PCT": SPREAD_PCT,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "main.py", "exec"), ns)
    return ns


def fetch_interval(symbol, interval, start_ms, end_ms):
    rows = []
    cursor = start_ms
    step = INTERVAL_MS[interval]
    global LAST_API_REQUEST
    # Use fixed windows of 800 candles. BingX may return 999 even when
    # limit=1000, so a single long-range request is not reliable for paging.
    while cursor < end_ms:
        window_end = min(cursor + step * 800, end_ms)
        wait = 1.05 - (time.monotonic() - LAST_API_REQUEST)
        if wait > 0:
            time.sleep(wait)
        query = urllib.parse.urlencode({
            "symbol": symbol, "interval": interval, "startTime": cursor,
            "endTime": window_end, "limit": 1000,
        })
        req = urllib.request.Request(
            BASE + PATH + "?" + query,
            headers={"User-Agent": "gold-bot-readonly-backtest/1.0"},
        )
        with urllib.request.urlopen(req, timeout=25) as response:
            payload = json.loads(response.read().decode("utf-8"))
        LAST_API_REQUEST = time.monotonic()
        if payload.get("code", 0) not in (0, "0", None):
            raise RuntimeError(f"BingX {symbol} {interval}: {payload}")
        batch = payload.get("data") or []
        if isinstance(batch, dict):
            batch = batch.get("data", batch.get("list", batch.get("rows", [])))
        if not batch:
            cursor = window_end
            continue
        parsed = []
        for row in batch:
            try:
                if isinstance(row, (list, tuple)) and len(row) >= 6:
                    ot = int(float(row[0]))
                    values = [float(row[j]) for j in range(1, 6)]
                    ct = int(float(row[6])) if len(row) > 6 and row[6] not in (None, "") else ot + step - 1
                elif isinstance(row, dict):
                    ot = int(float(row.get("openTime", row.get("time", row.get("timestamp", 0)))))
                    values = [float(row[k]) for k in ("open", "high", "low", "close", "volume")]
                    ct = int(float(row.get("closeTime", row.get("close_time", ot + step - 1))))
                else:
                    continue
                if cursor <= ot < window_end and ot > 0 and all(math.isfinite(v) for v in values):
                    parsed.append((ot, *values, ct))
            except (ValueError, TypeError, KeyError, OverflowError):
                continue
        if not parsed and batch:
            preview = json.dumps(payload, ensure_ascii=False)[:1200]
            raise RuntimeError(f"Unexpected kline format for {symbol} {interval}: {preview}")
        rows.extend(parsed)
        cursor = window_end
    df = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume", "close_time"
    ])
    if df.empty:
        raise RuntimeError(f"No candles returned for {symbol} {interval}")
    return (df.drop_duplicates("open_time").sort_values("open_time")
              .reset_index(drop=True))


def simulate_trade(signal, minute_frame, signal_index):
    if signal_index + 1 >= len(minute_frame):
        return None
    next_bar = minute_frame.iloc[signal_index + 1]
    raw_entry = float(next_bar["open"])
    side = signal["side"]
    slip = SLIPPAGE_PCT / 100.0
    entry = raw_entry * (1 + slip if side == "BUY" else 1 - slip)
    risk = abs(float(signal["entry"]) - float(signal["sl"]))
    if risk <= 0:
        return None
    if side == "BUY":
        sl, tp1, tp2 = entry - risk, entry + 1.8 * risk, entry + 3.0 * risk
    else:
        sl, tp1, tp2 = entry + risk, entry - 1.8 * risk, entry - 3.0 * risk

    tp1_hit = False
    exit_index = None
    gross_r = None
    for j in range(signal_index + 1, len(minute_frame)):
        bar = minute_frame.iloc[j]
        high, low = float(bar["high"]), float(bar["low"])
        if side == "BUY":
            stop_hit = low <= sl
            first_hit = high >= tp1
            second_hit = high >= tp2
        else:
            stop_hit = high >= sl
            first_hit = low <= tp1
            second_hit = low <= tp2

        # Conservative intrabar ordering: if stop and target overlap, assume stop first.
        if stop_hit:
            gross_r = (0.7 * 1.8 - 0.3) if tp1_hit else -1.0
            exit_index = j
            break
        if not tp1_hit and first_hit:
            tp1_hit = True
        if tp1_hit and second_hit:
            gross_r = 0.7 * 1.8 + 0.3 * 3.0
            exit_index = j
            break
    if exit_index is None:
        return {"status": "unresolved", "exit_index": len(minute_frame) - 1}
    # Round-trip taker fees + modeled slippage and spread; converted to R.
    cost_pct = 2 * TAKER_FEE_RATE * 100 + 2 * SLIPPAGE_PCT + SPREAD_PCT
    cost_r = (entry * cost_pct / 100.0) / risk
    return {
        "status": "tp2" if gross_r > 2.0 else ("tp1_then_sl" if tp1_hit else "sl"),
        "gross_r": gross_r, "net_r": gross_r - cost_r,
        "exit_index": exit_index,
    }


def run(args):
    ns = load_builders()
    now = int(time.time() * 1000)
    eval_start = now - args.days * 86_400_000
    fetch_start = eval_start - 2 * 86_400_000  # warm-up covers EMA50/ADX context
    totals = {name: defaultdict(float) for name in STRATEGIES}
    rows_out = []
    for symbol in args.symbols.split(","):
        symbol = symbol.strip().upper()
        if not symbol:
            continue
        print(f"FETCH {symbol}", flush=True)
        frames = {
            tf: fetch_interval(symbol, tf, fetch_start, now)
            for tf in ("1m", "5m", "15m")
        }
        expected_minutes = (args.days + 2) * 24 * 60
        for tf, frame in frames.items():
            interval_minutes = INTERVAL_MS[tf] // 60_000
            minimum_rows = int(expected_minutes / interval_minutes * 0.95)
            if len(frame) < minimum_rows:
                raise RuntimeError(
                    f"Incomplete BingX history for {symbol} {tf}: "
                    f"{len(frame)} candles, expected at least {minimum_rows}"
                )
        m1 = frames["1m"]
        eligible = m1.index[m1["close_time"] >= eval_start]
        if len(eligible) == 0:
            raise RuntimeError(f"No evaluation candles for {symbol}")
        start_i = int(eligible[0])
        busy_until = {name: -1 for name in STRATEGIES}
        last_c5 = None
        for i in range(start_i, len(m1) - 1):
            stamp = int(m1.iloc[i]["close_time"])
            available = {
                tf: frame.loc[frame["close_time"] <= stamp].tail(300).reset_index(drop=True)
                for tf, frame in frames.items()
            }
            if any(len(available[tf]) < 60 for tf in available):
                continue
            for strategy, (fn_name, trigger_tf) in STRATEGIES.items():
                if i <= busy_until[strategy]:
                    continue
                if trigger_tf == "5m":
                    current_c5 = int(available["5m"].iloc[-1]["close_time"])
                    if current_c5 == last_c5:
                        continue
                    last_c5 = current_c5
                    if current_c5 != stamp:
                        continue
                diag = {}
                signal = ns[fn_name](symbol, available, diag)
                if not signal:
                    continue
                outcome = simulate_trade(signal, m1, i)
                if outcome is None:
                    continue
                busy_until[strategy] = int(outcome["exit_index"])
                stat = totals[strategy]
                stat["signals"] += 1
                stat[outcome["status"]] += 1
                if outcome["status"] != "unresolved":
                    stat["net_r_sum"] += outcome["net_r"]
                    stat["closed"] += 1
                    rows_out.append({
                        "symbol": symbol, "strategy": strategy,
                        "side": signal["side"], "time": datetime.fromtimestamp(
                            stamp / 1000, tz=timezone.utc).isoformat(),
                        **outcome,
                    })
        print(f"DONE {symbol}", flush=True)

    report = {
        "period_days": args.days, "symbols": args.symbols.split(","),
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "method": "closed candles; next 1m open entry; conservative stop-first intrabar; TP1 70%/1.8R, TP2 30%/3R; taker fee, modeled slippage and spread deducted; funding excluded",
        "results": {},
    }
    for strategy, stat in totals.items():
        closed = int(stat["closed"])
        report["results"][strategy] = {
            "signals": int(stat["signals"]), "closed": closed,
            "tp2": int(stat["tp2"]), "tp1_then_sl": int(stat["tp1_then_sl"]),
            "sl": int(stat["sl"]), "unresolved": int(stat["unresolved"]),
            "net_r_total": round(stat["net_r_sum"], 3),
            "net_r_per_closed_trade": round(stat["net_r_sum"] / closed, 3) if closed else None,
        }
    report["trades"] = rows_out
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print("BACKTEST_REPORT_BEGIN")
    print(rendered)
    print("BACKTEST_REPORT_END")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("# Historical C/D/E backtest\n\n")
            f.write(f"Period: {args.days} days; symbols: {args.symbols}\n\n")
            f.write("| Strategy | Signals | Closed | TP2 | TP1 then SL | SL | Net R | Avg net R |\n")
            f.write("|---|---:|---:|---:|---:|---:|---:|---:|\n")
            for name, result in report["results"].items():
                f.write(f"| {name} | {result['signals']} | {result['closed']} | {result['tp2']} | {result['tp1_then_sl']} | {result['sl']} | {result['net_r_total']} | {result['net_r_per_closed_trade']} |\n")
            f.write("\nFunding costs are excluded. This is a historical simulation, not a guarantee of future results.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--symbols", default="BTC-USDT,ETH-USDT,SOL-USDT")
    args = parser.parse_args()
    if not 1 <= args.days <= 7:
        parser.error("--days must be between 1 and 7")
    run(args)
