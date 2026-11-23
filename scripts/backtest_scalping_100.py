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
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
import pandas as pd

BASE = "https://open-api.bingx.com"
PATH = "/openApi/swap/v3/quote/klines"
INTERVAL_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000}
STRATEGIES = {
    "B_STRATEGY": ("build_signal_b", "1m"),
    "C_BREAKOUT_RETEST": ("build_signal_c_breakout_retest", "5m"),
    "D_IMPULSE": ("build_signal_d_impulse", "1m"),
    "E_OVERHEAT_REVERSAL": ("build_signal_e_overheat_reversal", "1m"),
}
WARMUP_DAYS = {"1m": 2, "5m": 2, "15m": 3, "1h": 10, "4h": 36}
TAKER_FEE_RATE = 0.0005
SLIPPAGE_PCT = 0.03
SPREAD_PCT = 0.04
MAX_ENTRY_DRIFT_R = 0.5
LAST_API_REQUEST = 0.0


def load_builders():
    source = open("main.py", encoding="utf-8").read()
    tree = ast.parse(source)
    wanted = {
        "safe_float", "indicators", "enrich_scalper_series", "_addon_reject",
        "_make_addon_signal", "_detect_market_regime", *[v[0] for v in STRATEGIES.values()],
    }
    nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in wanted]
    found = {n.name for n in nodes}
    if found != wanted:
        raise RuntimeError(f"Could not extract strategy code: missing {sorted(wanted-found)}")
    ns = {
        "Any": object, "math": math, "np": np, "pd": pd,
        "MIN_RR": 1.0, "MIN_NET_RR": 1.30, "MIN_QUALITY_SCORE": 85.0,
        "MIN_CANDLE_ROWS": 60, "TP1_R": 1.8, "TP2_R": 3.0,
        "STRATEGY_B": "B", "STRATEGY_C": "C_BREAKOUT_RETEST", "STRATEGY_D": "D_IMPULSE",
        "STRATEGY_E": "E_OVERHEAT_REVERSAL", "TAKER_FEE_RATE": TAKER_FEE_RATE,
        "MIN_ATR_PCT": 0.08, "MAX_ATR_PCT": 1.50, "MIN_SCALPER_ADX": 18.0,
        "MAX_EXTENSION_ATR": 2.20, "MIN_VOLUME_RATIO": 0.85,
        "MIN_SCALPER_RAW_SCORE": 7.0, "MIN_RR": 1.0, "MIN_NET_RR": 1.30,
        "MAX_ENTRY_DRIFT_R": MAX_ENTRY_DRIFT_R, "DEFAULT_SYNTHETIC_SPREAD_PCT": SPREAD_PCT,
        "DEFAULT_SLIPPAGE_PCT": SLIPPAGE_PCT, "TP1_R": 1.8, "TP2_R": 3.0,
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
        wait = 1.75 - (time.monotonic() - LAST_API_REQUEST)
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
        payload = None
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=25) as response:
                    raw = response.read().decode("utf-8")
                LAST_API_REQUEST = time.monotonic()
                payload = json.loads(raw)
            except urllib.error.HTTPError as exc:
                LAST_API_REQUEST = time.monotonic()
                if exc.code not in (418, 429, 500, 502, 503, 504) or attempt == 3:
                    raise
                delay = 5 * (2 ** attempt)
                print(f"RETRY BingX HTTP {exc.code} {symbol} {interval} in {delay}s", flush=True)
                time.sleep(delay)
                continue
            code = payload.get("code", 0)
            if code in (0, "0", None):
                break
            if str(code) not in ("100410", "100429", "100500") or attempt == 3:
                raise RuntimeError(f"BingX {symbol} {interval}: {payload}")
            delay = 5 * (2 ** attempt)
            print(f"RETRY BingX code={code} {symbol} {interval} in {delay}s", flush=True)
            time.sleep(delay)
        if payload is None or payload.get("code", 0) not in (0, "0", None):
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
                if ot < 10_000_000_000:
                    ot *= 1000
                if ct < 10_000_000_000:
                    ct *= 1000
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


def filter_closed_candles(frame, current_ms):
    """Exclude the currently forming candle, matching the live bot's closed-bar rule."""
    if frame.empty or "close_time" not in frame.columns:
        return frame.copy()
    return frame.loc[pd.to_numeric(frame["close_time"], errors="coerce") < int(current_ms) - 2_000].copy().reset_index(drop=True)


def closed_window(frame, close_times, stamp, limit=300):
    """Return the last closed candles up to stamp without rescanning all history."""
    end = int(np.searchsorted(close_times, int(stamp), side="right"))
    start = max(0, end - int(limit))
    return frame.iloc[start:end]


def simulate_trade(signal, minute_frame, signal_index):
    """Simulate fills with spread/slippage applied once per execution side."""
    if signal_index + 1 >= len(minute_frame):
        return None
    side = signal["side"]
    risk = abs(float(signal["entry"]) - float(signal["sl"]))
    if risk <= 0:
        return None

    # A market order pays half the quoted spread plus adverse slippage on entry.
    adverse = (SPREAD_PCT / 2.0 + SLIPPAGE_PCT) / 100.0
    raw_entry = float(minute_frame.iloc[signal_index + 1]["open"])
    drift_r = abs(raw_entry - float(signal["entry"])) / risk
    if drift_r > MAX_ENTRY_DRIFT_R:
        return {"status": "skipped_drift", "exit_index": signal_index + 1}
    entry = raw_entry * (1 + adverse if side == "BUY" else 1 - adverse)
    if side == "BUY":
        sl, tp1, tp2 = entry - risk, entry + 1.8 * risk, entry + 3.0 * risk
    else:
        sl, tp1, tp2 = entry + risk, entry - 1.8 * risk, entry - 3.0 * risk

    tp1_hit = False
    exit_index = None
    exit_kind = None
    exit_levels = None
    for j in range(signal_index + 1, len(minute_frame)):
        bar = minute_frame.iloc[j]
        high, low = float(bar["high"]), float(bar["low"])
        if side == "BUY":
            stop_hit, first_hit, second_hit = low <= sl, high >= tp1, high >= tp2
        else:
            stop_hit, first_hit, second_hit = high >= sl, low <= tp1, low <= tp2

        # Conservative ordering: if stop and a target overlap, assume stop first.
        if stop_hit:
            # A stop-market order can fill worse than its trigger after a gap.
            # Model the opening gap adversely instead of assuming the SL price.
            stop_fill_level = sl
            bar_open = float(bar["open"])
            if side == "BUY" and bar_open < sl:
                stop_fill_level = bar_open
            elif side == "SELL" and bar_open > sl:
                stop_fill_level = bar_open
            exit_kind = "tp1_then_sl" if tp1_hit else "sl"
            exit_levels = ([tp1, stop_fill_level] if tp1_hit else [stop_fill_level])
            exit_index = j
            break
        if not tp1_hit and first_hit:
            tp1_hit = True
        if tp1_hit and second_hit:
            exit_kind = "tp2"
            exit_levels = [tp1, tp2]
            exit_index = j
            break

    if exit_index is None:
        return {"status": "unresolved", "exit_index": len(minute_frame) - 1}

    # Apply adverse spread + slippage to each exit fill; charge taker fees on
    # the entry and the weighted exit notionals. Entry friction is not deducted twice.
    exit_weights = ([0.7, 0.3] if len(exit_levels) == 2 else [1.0])
    gross_pnl = 0.0
    exit_notional = 0.0
    for level, weight in zip(exit_levels, exit_weights):
        fill = level * (1 - adverse if side == "BUY" else 1 + adverse)
        pnl_per_unit = (fill - entry) if side == "BUY" else (entry - fill)
        gross_pnl += weight * pnl_per_unit
        exit_notional += weight * fill
    gross_r = gross_pnl / risk
    fee_cost = TAKER_FEE_RATE * (entry + exit_notional)
    fee_r = fee_cost / risk
    net_r = gross_r - fee_r
    return {
        "status": exit_kind, "gross_r": gross_r, "fee_r": fee_r,
        "net_r": net_r, "exit_index": exit_index,
    }


def simulate_portfolio(trades, deposit, risk_percent, leverage, max_positions):
    """Risk-size trades against a small account with a shared gross exposure cap."""
    if deposit <= 0 or not 0 < risk_percent <= 10 or leverage <= 0 or max_positions < 1:
        raise ValueError("invalid portfolio parameters")
    equity = float(deposit)
    peak = equity
    max_drawdown_pct = 0.0
    open_positions = []
    accepted = []
    skipped_slots = skipped_margin = 0
    max_open = 0
    ordered = sorted(trades, key=lambda t: (int(t["entry_time"]), int(t["exit_time"])))
    for trade in ordered:
        entry_time = int(trade["entry_time"])
        remaining = []
        for pos in open_positions:
            if int(pos["exit_time"]) <= entry_time:
                equity += pos["pnl_usdt"]
                peak = max(peak, equity)
                max_drawdown_pct = max(max_drawdown_pct, (peak-equity)/peak*100.0 if peak else 0.0)
            else:
                remaining.append(pos)
        open_positions = remaining
        if len(open_positions) >= max_positions or any(p.get("symbol") == trade.get("symbol") for p in open_positions):
            skipped_slots += 1
            continue
        stop_pct = float(trade.get("stop_distance_pct") or 0.0) / 100.0
        if stop_pct <= 0 or equity <= 0:
            skipped_margin += 1
            continue
        risk_budget = equity * risk_percent / 100.0
        used_notional = sum(float(p["notional"]) for p in open_positions)
        available_notional = max(0.0, equity * leverage - used_notional)
        notional = min(risk_budget / stop_pct, available_notional)
        if notional <= 0:
            skipped_margin += 1
            continue
        actual_risk = notional * stop_pct
        is_unresolved = trade.get("status") == "unresolved"
        pnl = 0.0 if is_unresolved else float(trade["net_r"]) * actual_risk
        open_positions.append({"exit_time": int(trade["exit_time"]),
                               "notional": notional, "pnl_usdt": pnl,
                               "symbol": trade.get("symbol"),
                               "unresolved": is_unresolved})
        max_open = max(max_open, len(open_positions))
        if not is_unresolved:
            accepted.append({**trade, "notional_usdt": round(notional, 6),
                             "risk_usdt": round(actual_risk, 6), "net_pnl_usdt": round(pnl, 6)})
    for pos in sorted(open_positions, key=lambda p: p["exit_time"]):
        if not pos.get("unresolved"):
            equity += pos["pnl_usdt"]
            peak = max(peak, equity)
            max_drawdown_pct = max(max_drawdown_pct, (peak-equity)/peak*100.0 if peak else 0.0)
    wins = sum(t["net_pnl_usdt"] > 0 for t in accepted)
    losses = sum(t["net_pnl_usdt"] < 0 for t in accepted)
    open_at_end = [p for p in open_positions if p.get("unresolved")]
    return {"accepted_trades": len(accepted), "wins": wins, "losses": losses,
            "win_rate_pct": round(wins/len(accepted)*100, 2) if accepted else None,
            "skipped_max_positions": skipped_slots, "skipped_margin_cap": skipped_margin,
            "max_open_positions_observed": max_open,
            "open_positions_at_end": len(open_at_end),
            "open_notional_at_end_usdt": round(sum(p["notional"] for p in open_at_end), 6),
            "ending_equity_usdt": round(equity, 4),
            "net_pnl_usdt": round(equity-deposit, 4),
            "return_pct": round((equity/deposit-1)*100, 3),
            "max_drawdown_pct": round(max_drawdown_pct, 3), "trades": accepted,
            "assumptions": "Closed trades realize modeled PnL; unresolved positions reserve slots and gross notional through dataset end but do not realize PnL; shared gross notional limited to deposit x leverage; net R includes modeled trading friction."}


def run(args):
    ns = load_builders()
    now = int(time.time() * 1000)
    eval_start = now - args.days * 86_400_000
    totals = {name: defaultdict(float) for name in STRATEGIES}
    rows_out = []
    original_enrich = ns["enrich_scalper_series"]
    def cached_enrich(frame):
        # The backtest precomputes indicators causally over the full fetched
        # history. Builders receive only rows closed by the evaluation time.
        required = {"ema20", "ema50", "rsi", "atr", "adx", "vol_ma20",
                    "range_high_20", "range_low_20"}
        if required.issubset(frame.columns):
            return frame
        return original_enrich(frame)

    ns["enrich_scalper_series"] = cached_enrich
    for symbol in args.symbols.split(","):
        symbol = symbol.strip().upper()
        if not symbol:
            continue
        print(f"FETCH {symbol}", flush=True)
        frames = {}
        for tf in ("1m", "5m", "15m", "1h", "4h"):
            print(f"FETCH_INTERVAL {symbol} {tf}", flush=True)
            fetch_start = eval_start - WARMUP_DAYS[tf] * 86_400_000
            fetched = fetch_interval(symbol, tf, fetch_start, now)
            frames[tf] = filter_closed_candles(fetched, now)
            print(f"FETCHED {symbol} {tf}: {len(frames[tf])} closed candles", flush=True)
        for tf, frame in frames.items():
            interval_minutes = INTERVAL_MS[tf] // 60_000
            expected_minutes = (args.days + WARMUP_DAYS[tf]) * 24 * 60
            minimum_rows = int(expected_minutes / interval_minutes * 0.95)
            if len(frame) < minimum_rows:
                raise RuntimeError(
                    f"Incomplete BingX history for {symbol} {tf}: "
                    f"{len(frame)} candles, expected at least {minimum_rows}"
                )
            # Compute indicators once for the whole causal history instead of
            # recalculating them on every rolling 300-candle slice.
            frames[tf] = original_enrich(frame)
        close_time_arrays = {
            tf: frame["close_time"].to_numpy(dtype=np.int64)
            for tf, frame in frames.items()
        }
        m1 = frames["1m"]
        eligible = m1.index[m1["close_time"] >= eval_start]
        if len(eligible) == 0:
            raise RuntimeError(f"No evaluation candles for {symbol}")
        start_i = int(eligible[0])
        busy_until = {name: -1 for name in STRATEGIES}
        last_setup_key = {name: None for name in STRATEGIES}
        last_c5 = None
        total_steps = max(1, len(m1) - 1 - start_i)
        for i in range(start_i, len(m1) - 1):
            if (i - start_i) % 1000 == 0:
                print(f"PROGRESS {symbol} {i-start_i}/{total_steps} "
                      f"C={int(totals['C_BREAKOUT_RETEST']['signals'])} "
                      f"D={int(totals['D_IMPULSE']['signals'])} "
                      f"E={int(totals['E_OVERHEAT_REVERSAL']['signals'])}", flush=True)
            stamp = int(m1.iloc[i]["close_time"])
            available = {
                tf: closed_window(frame, close_time_arrays[tf], stamp, 300)
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
                if strategy == "B_STRATEGY" and any(len(available[tf]) < 205 for tf in ("1m", "5m", "15m", "1h", "4h")):
                    continue
                diag = {}
                if strategy == "B_STRATEGY":
                    signal = ns[fn_name]("swap", symbol, available, None, diag)
                else:
                    signal = ns[fn_name](symbol, available, diag)
                if not signal:
                    continue
                setup_key = signal.get("setup_key")
                if setup_key and setup_key == last_setup_key[strategy]:
                    continue
                if setup_key:
                    last_setup_key[strategy] = setup_key
                outcome = simulate_trade(signal, m1, i)
                if outcome is None:
                    continue
                stat = totals[strategy]
                stat["signals"] += 1
                stat[outcome["status"]] += 1
                if outcome["status"] == "skipped_drift":
                    continue
                busy_until[strategy] = int(outcome["exit_index"])
                if outcome["status"] != "unresolved":
                    outcome["entry_time"] = int(m1.iloc[i + 1]["open_time"])
                    outcome["exit_time"] = int(m1.iloc[outcome["exit_index"]]["open_time"])
                    outcome["stop_distance_pct"] = abs(float(signal["entry"]) - float(signal["sl"])) / float(signal["entry"]) * 100.0
                    stat["net_r_sum"] += outcome["net_r"]
                    stat["gross_r_sum"] += outcome["gross_r"]
                    stat["fee_r_sum"] += outcome["fee_r"]
                    stat["closed"] += 1
                    rows_out.append({
                        "symbol": symbol, "strategy": strategy,
                        "side": signal["side"], "time": datetime.fromtimestamp(
                            stamp / 1000, tz=timezone.utc).isoformat(),
                        **outcome,
                    })
        print(f"DONE {symbol}", flush=True)

    portfolio = simulate_portfolio(rows_out, args.deposit, args.risk_percent,
                                   args.leverage, args.max_positions)
    report = {
        "period_days": args.days, "symbols": args.symbols.split(","),
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "account": {"deposit_usdt": args.deposit, "risk_percent": args.risk_percent,
                    "leverage_cap": args.leverage, "max_positions": args.max_positions},
        "method": "closed candles only; next 1m open fills; half-spread+slippage each fill; taker fees; stop-first intrabar; TP1 70%/1.8R, TP2 30%/3R; risk-sized portfolio; aggregate gross notional capped at deposit x leverage; funding excluded",
        "portfolio": portfolio,
        "results": {},
    }
    for strategy, stat in totals.items():
        closed = int(stat["closed"])
        report["results"][strategy] = {
            "signals": int(stat["signals"]), "closed": closed,
            "tp2": int(stat["tp2"]), "tp1_then_sl": int(stat["tp1_then_sl"]),
            "sl": int(stat["sl"]), "unresolved": int(stat["unresolved"]),
            "skipped_drift": int(stat["skipped_drift"]),
            "gross_r_total": round(stat["gross_r_sum"], 3),
            "fees_r_total": round(stat["fee_r_sum"], 3),
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
            f.write("\n## $100 portfolio simulation\n\n")
            f.write(f"Deposit: {args.deposit}; ending equity: {portfolio['ending_equity_usdt']}; net: {portfolio['net_pnl_usdt']} USDT ({portfolio['return_pct']}%); max drawdown: {portfolio['max_drawdown_pct']}%.\n\n")
            f.write("Funding costs are excluded. This is a historical simulation, not a guarantee of future results.\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--symbols", default="BTC-USDT,ETH-USDT,SOL-USDT")
    parser.add_argument("--deposit", type=float, default=100.0)
    parser.add_argument("--risk-percent", type=float, default=2.0)
    parser.add_argument("--leverage", type=float, default=5.0)
    parser.add_argument("--max-positions", type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.days <= 30:
        parser.error("--days must be between 1 and 30")
    run(args)
