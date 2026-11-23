#!/usr/bin/env python3
"""Read-only single-take-profit sweep for Strategy B on BingX USDT-M history.

Each target distance is tested independently. The signal builder's RR filter is
recomputed for that target, so a candidate is not admitted using a different
target's reward. No credentials, private endpoints, DB access, or order calls.
"""
import argparse
import json
import math
import time
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

from scripts.backtest_scalping_100 import (
    INTERVAL_MS, WARMUP_DAYS, fetch_interval, filter_closed_candles,
    closed_window, simulate_portfolio,
    TAKER_FEE_RATE, SLIPPAGE_PCT, SPREAD_PCT, MAX_ENTRY_DRIFT_R,
    load_builders,
)

SYMBOLS_DEFAULT = "BTC-USDT,ETH-USDT,SOL-USDT,XRP-USDT,ADA-USDT,DOGE-USDT,LINK-USDT,LTC-USDT"


def simulate_single_tp(signal, minute_frame, signal_index, target_r):
    if signal_index + 1 >= len(minute_frame):
        return None
    side = signal["side"]
    risk = abs(float(signal["entry"]) - float(signal["sl"]))
    if risk <= 0:
        return None
    adverse = (SPREAD_PCT / 2.0 + SLIPPAGE_PCT) / 100.0
    raw_entry = float(minute_frame.iloc[signal_index + 1]["open"])
    if abs(raw_entry - float(signal["entry"])) / risk > MAX_ENTRY_DRIFT_R:
        return {"status": "skipped_drift", "exit_index": signal_index + 1}
    entry = raw_entry * (1 + adverse if side == "BUY" else 1 - adverse)
    if side == "BUY":
        sl, tp = entry - risk, entry + target_r * risk
    else:
        sl, tp = entry + risk, entry - target_r * risk

    exit_index = None
    exit_kind = None
    exit_level = None
    for j in range(signal_index + 1, len(minute_frame)):
        bar = minute_frame.iloc[j]
        high, low = float(bar["high"]), float(bar["low"])
        stop_hit = low <= sl if side == "BUY" else high >= sl
        target_hit = high >= tp if side == "BUY" else low <= tp
        # Conservative ordering: if stop and target overlap, assume stop first.
        if stop_hit:
            stop_fill = sl
            bar_open = float(bar["open"])
            if side == "BUY" and bar_open < sl:
                stop_fill = bar_open
            elif side == "SELL" and bar_open > sl:
                stop_fill = bar_open
            exit_kind, exit_level, exit_index = "sl", stop_fill, j
            break
        if target_hit:
            exit_kind, exit_level, exit_index = "tp", tp, j
            break
    if exit_index is None:
        return {"status": "unresolved", "exit_index": len(minute_frame) - 1}

    fill = exit_level * (1 - adverse if side == "BUY" else 1 + adverse)
    gross_pnl = (fill - entry) if side == "BUY" else (entry - fill)
    gross_r = gross_pnl / risk
    fee_r = TAKER_FEE_RATE * (entry + fill) / risk
    return {
        "status": exit_kind, "gross_r": gross_r, "fee_r": fee_r,
        "net_r": gross_r - fee_r, "exit_index": exit_index,
    }


def prepare_history(args):
    now = int(time.time() * 1000)
    eval_start = now - args.days * 86_400_000
    ns = load_builders()
    original_enrich = ns["enrich_scalper_series"]

    def cached_enrich(frame):
        required = {"ema20", "ema50", "rsi", "atr", "adx", "vol_ma20",
                    "range_high_20", "range_low_20"}
        return frame if required.issubset(frame.columns) else original_enrich(frame)

    ns["enrich_scalper_series"] = cached_enrich
    histories = {}
    for raw_symbol in args.symbols.split(","):
        symbol = raw_symbol.strip().upper()
        if not symbol:
            continue
        print(f"FETCH {symbol}", flush=True)
        frames = {}
        for tf in ("1m", "5m", "15m", "1h", "4h"):
            fetch_start = eval_start - WARMUP_DAYS[tf] * 86_400_000
            fetched = fetch_interval(symbol, tf, fetch_start, now)
            frames[tf] = filter_closed_candles(fetched, now)
            print(f"FETCHED {symbol} {tf}: {len(frames[tf])}", flush=True)
        for tf, frame in frames.items():
            interval_minutes = INTERVAL_MS[tf] // 60_000
            expected = (args.days + WARMUP_DAYS[tf]) * 24 * 60
            minimum = int(expected / interval_minutes * 0.95)
            if len(frame) < minimum:
                raise RuntimeError(
                    f"Incomplete history {symbol} {tf}: {len(frame)} < {minimum}"
                )
            frames[tf] = original_enrich(frame)
        close_times = {
            tf: frame["close_time"].to_numpy(dtype=np.int64)
            for tf, frame in frames.items()
        }
        eligible = frames["1m"].index[frames["1m"]["close_time"] >= eval_start]
        if len(eligible) == 0:
            raise RuntimeError(f"No evaluation candles for {symbol}")
        histories[symbol] = {
            "frames": frames, "close_times": close_times,
            "start_i": int(eligible[0]), "eval_start": eval_start,
        }
    return ns, histories, now


def run_target(args, target_r, ns, histories):
    # Signal admission and target distance are aligned for each independent run.
    ns["TP1_R"] = target_r
    ns["TP2_R"] = target_r
    builder = ns["build_signal_b"]
    rows = []
    counters = defaultdict(int)

    for symbol, history in histories.items():
        frames = history["frames"]
        m1 = frames["1m"]
        start_i = history["start_i"]
        busy_until = -1
        last_setup_key = None
        close_times = history["close_times"]
        for i in range(start_i, len(m1) - 1):
            stamp = int(m1.iloc[i]["close_time"])
            available = {
                tf: closed_window(frame, close_times[tf], stamp, 300)
                for tf, frame in frames.items()
            }
            if any(len(available[tf]) < 205 for tf in ("1m", "5m", "15m", "1h", "4h")):
                continue
            if i <= busy_until:
                continue
            diag = {}
            signal = builder("swap", symbol, available, None, diag)
            if not signal:
                continue
            setup_key = signal.get("setup_key")
            if setup_key and setup_key == last_setup_key:
                continue
            if setup_key:
                last_setup_key = setup_key
            outcome = simulate_single_tp(signal, m1, i, target_r)
            if outcome is None:
                continue
            counters["signals"] += 1
            counters[outcome["status"]] += 1
            if outcome["status"] in ("skipped_drift", "unresolved"):
                continue
            busy_until = int(outcome["exit_index"])
            outcome["entry_time"] = int(m1.iloc[i + 1]["open_time"])
            outcome["exit_time"] = int(m1.iloc[outcome["exit_index"]]["open_time"])
            outcome["stop_distance_pct"] = (
                abs(float(signal["entry"]) - float(signal["sl"]))
                / float(signal["entry"]) * 100.0
            )
            outcome.update({
                "symbol": symbol, "strategy": "B_STRATEGY",
                "side": signal["side"],
                "time": datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).isoformat(),
            })
            rows.append(outcome)

    portfolio = simulate_portfolio(
        rows, args.deposit, args.risk_percent, args.leverage, args.max_positions
    )
    accepted = portfolio["trades"]
    avg = lambda key: (
        round(sum(float(t[key]) for t in accepted) / len(accepted), 4)
        if accepted else None
    )
    by_symbol = {}
    for symbol in sorted({t["symbol"] for t in accepted}):
        group = [t for t in accepted if t["symbol"] == symbol]
        by_symbol[symbol] = {
            "trades": len(group),
            "win_rate_pct": round(sum(t["net_r"] > 0 for t in group) / len(group) * 100, 2),
            "avg_net_r": round(sum(t["net_r"] for t in group) / len(group), 4),
        }
    return {
        "target_r": target_r,
        "signals": counters["signals"],
        "closed": counters["tp"] + counters["sl"],
        "tp": counters["tp"], "sl": counters["sl"],
        "skipped_drift": counters["skipped_drift"],
        "unresolved": counters["unresolved"],
        "accepted_trades": portfolio["accepted_trades"],
        "win_rate_pct": portfolio["win_rate_pct"],
        "avg_gross_r": avg("gross_r"), "avg_fee_r": avg("fee_r"),
        "avg_net_r": avg("net_r"),
        "ending_equity_usdt": portfolio["ending_equity_usdt"],
        "return_pct": portfolio["return_pct"],
        "max_drawdown_pct": portfolio["max_drawdown_pct"],
        "skipped_max_positions": portfolio["skipped_max_positions"],
        "skipped_margin_cap": portfolio["skipped_margin_cap"],
        "by_symbol": by_symbol,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--symbols", default=SYMBOLS_DEFAULT)
    parser.add_argument("--deposit", type=float, default=100.0)
    parser.add_argument("--risk-percent", type=float, default=2.0)
    parser.add_argument("--leverage", type=float, default=5.0)
    parser.add_argument("--max-positions", type=int, default=5)
    parser.add_argument("--target-rs", default="1.5,1.8,2.1,2.4,2.7,3.0")
    args = parser.parse_args()
    if not 1 <= args.days <= 30:
        parser.error("--days must be between 1 and 30")
    target_rs = [float(x) for x in args.target_rs.split(",") if x.strip()]
    if not target_rs or any(not 0.5 <= x <= 6.0 for x in target_rs):
        parser.error("--target-rs must contain values between 0.5 and 6.0")
    ns, histories, now = prepare_history(args)
    results = [run_target(args, r, ns, histories) for r in target_rs]
    report = {
        "period_days": args.days, "symbols": args.symbols.split(","),
        "generated_utc": datetime.fromtimestamp(now / 1000, tz=timezone.utc).isoformat(),
        "account": {"deposit_usdt": args.deposit, "risk_percent": args.risk_percent,
                    "leverage_cap": args.leverage, "max_positions": args.max_positions},
        "method": (
            "Strategy B only; separate full signal-generation pass for each target; "
            "signal Net-RR filter recalculated for that target; closed candles only; "
            "next 1m open fills; spread+slippage on every fill; taker fees; "
            "stop-first intrabar; one full-position TP; 2% equity risk; 5x gross cap; "
            "max 5 concurrent positions; funding excluded"
        ),
        "results": results,
    }
    print("SINGLE_TP_SWEEP_BEGIN")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("SINGLE_TP_SWEEP_END")


if __name__ == "__main__":
    main()
