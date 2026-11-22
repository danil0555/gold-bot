#!/usr/bin/env python3
"""Historical, read-only BingX cash-and-carry backtest.

Uses 1h spot/perpetual candles and actual settled historical funding records.
No account endpoints, credentials, database access, or orders. Historical books
are unavailable, so fills use explicit conservative synthetic slippage; results
are a research proxy, not an execution-quality backtest.
"""
from __future__ import annotations

import argparse
import json
import math
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Any

BASE = "https://open-api.bingx.com"
INTERVAL = "1h"
STEP_MS = 60 * 60_000
PAGE_SIZE = 900
DEFAULT_SYMBOLS = ("BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT",
                   "ADA-USDT", "DOGE-USDT", "LINK-USDT", "LTC-USDT")
SPOT_FEE = 0.001
SWAP_FEE = 0.0005
SLIPPAGE_PCT = 0.02
LAST_REQUEST: dict[str, float] = {}


class BacktestDataError(RuntimeError):
    pass


def request(path: str, params: dict[str, Any]) -> dict[str, Any]:
    previous = LAST_REQUEST.get(path, 0.0)
    wait = 1.05 - (time.monotonic() - previous)
    if wait > 0:
        time.sleep(wait)
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(BASE + path + "?" + query, headers={
        "Accept": "application/json",
        "User-Agent": "gold-bot-cash-carry-backtest/1.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=25) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise BacktestDataError(f"request failed {path}: {exc}") from exc
    LAST_REQUEST[path] = time.monotonic()
    if not isinstance(payload, dict) or payload.get("code", 0) not in (0, "0", None):
        raise BacktestDataError(f"BingX returned an error for {path}: {payload!r}")
    return payload


def unwrap_data(payload: dict[str, Any]) -> list:
    data = payload.get("data") or []
    if isinstance(data, dict):
        for key in ("data", "list", "rows", "candles"):
            if isinstance(data.get(key), list):
                return data[key]
        return []
    return data if isinstance(data, list) else []


def parse_candle(row: Any, source: str) -> tuple[int, float, float] | None:
    try:
        if isinstance(row, (list, tuple)):
            stamp = int(float(row[0]))
            close = float(row[4])
            close_time = int(float(row[6])) if len(row) > 6 else stamp + STEP_MS - 1
        elif isinstance(row, dict):
            stamp = int(float(row.get("time", row.get("openTime", row.get("t", 0)))))
            close = float(row["close"])
            close_time = int(float(row.get("closeTime", stamp + STEP_MS - 1)))
        else:
            return None
        if stamp < 10_000_000_000:
            stamp *= 1000
        if close_time < 10_000_000_000:
            close_time *= 1000
        if stamp <= 0 or close <= 0 or not math.isfinite(close):
            return None
        return stamp, close, close_time
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def fetch_candles(symbol: str, source: str, start_ms: int, end_ms: int,
                  now_ms: int) -> dict[int, float]:
    if source == "spot":
        path = "/openApi/spot/v2/market/kline"
    elif source == "swap":
        path = "/openApi/swap/v3/quote/klines"
    else:
        raise ValueError("source must be spot or swap")
    result: dict[int, float] = {}
    cursor = start_ms
    while cursor < end_ms:
        window_end = min(cursor + PAGE_SIZE * STEP_MS, end_ms)
        payload = request(path, {
            "symbol": symbol, "interval": INTERVAL,
            "startTime": cursor, "endTime": window_end, "limit": PAGE_SIZE,
        })
        batch = unwrap_data(payload)
        if not batch:
            cursor = window_end
            continue
        parsed = [parse_candle(row, source) for row in batch]
        parsed = [row for row in parsed if row and cursor <= row[0] < window_end and row[2] < now_ms - 2_000]
        for stamp, close, _ in parsed:
            result[stamp] = close
        newest = max((row[0] for row in parsed), default=0)
        if newest <= cursor:
            cursor = window_end
        else:
            cursor = newest + STEP_MS
    return result


def fetch_funding(symbol: str, start_ms: int, end_ms: int) -> list[dict[str, float]]:
    path = "/openApi/swap/v2/quote/fundingRate"
    cursor = start_ms
    records: dict[int, dict[str, float]] = {}
    while cursor < end_ms:
        payload = request(path, {
            "symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": 1000,
        })
        batch = unwrap_data(payload)
        if not batch:
            break
        newest = 0
        for row in batch:
            try:
                stamp = int(row["fundingTime"])
                rate = float(row["fundingRate"])
                mark = float(row.get("markPrice") or 0)
                if stamp < 10_000_000_000:
                    stamp *= 1000
                if stamp >= start_ms and stamp <= end_ms and math.isfinite(rate):
                    records[stamp] = {"time": stamp, "rate": rate, "mark": mark}
                    newest = max(newest, stamp)
            except (KeyError, TypeError, ValueError, OverflowError):
                continue
        if newest < cursor or len(batch) < 1000:
            break
        cursor = newest + 1
    return [records[k] for k in sorted(records)]


def simulate_cash_carry(
    bars: list[dict[str, float]],
    funding: list[dict[str, float]],
    *,
    notional: float = 1000.0,
    min_entry_basis_pct: float = 0.45,
    target_basis_pct: float = 0.05,
    max_hold_hours: float = 72.0,
    cooldown_minutes: float = 60.0,
    spot_fee: float = SPOT_FEE,
    swap_fee: float = SWAP_FEE,
    slippage_pct: float = SLIPPAGE_PCT,
) -> dict[str, Any]:
    """Simulate LONG spot + SHORT perpetual using only data known by each bar."""
    if notional <= 0 or max_hold_hours <= 0:
        raise ValueError("notional and max_hold_hours must be positive")
    if not 0 <= target_basis_pct < min_entry_basis_pct:
        raise ValueError("target basis must be below entry threshold")
    funding = sorted(funding, key=lambda x: x["time"])
    funding_index = 0
    latest_settled_rate = None
    position = None
    last_exit_time = -10**30
    trades = []
    open_at_end = 0
    slip = slippage_pct / 100

    for bar in sorted(bars, key=lambda x: x["time"]):
        decision_time = int(bar["time"]) + STEP_MS
        spot_close = float(bar["spot"])
        swap_close = float(bar["swap"])
        if spot_close <= 0 or swap_close <= 0:
            continue
        basis_pct = (swap_close / spot_close - 1) * 100

        while funding_index < len(funding) and funding[funding_index]["time"] <= decision_time:
            event = funding[funding_index]
            latest_settled_rate = float(event["rate"])
            if position is not None and event["time"] > position["entry_time"]:
                mark = float(event.get("mark") or swap_close)
                position["funding_usdt"] += position["quantity"] * mark * float(event["rate"])
            funding_index += 1

        if position is None:
            if decision_time - last_exit_time < cooldown_minutes * 60_000:
                continue
            if latest_settled_rate is None or latest_settled_rate <= 0:
                continue
            spot_entry = spot_close * (1 + slip)
            swap_entry = swap_close * (1 - slip)
            executable_basis = (swap_entry / spot_entry - 1) * 100
            if executable_basis < min_entry_basis_pct:
                continue
            quantity = notional / spot_entry
            entry_fees = (spot_entry * quantity * spot_fee
                          + swap_entry * quantity * swap_fee)
            position = {
                "entry_time": decision_time,
                "entry_basis_pct": executable_basis,
                "spot_entry": spot_entry,
                "swap_entry": swap_entry,
                "quantity": quantity,
                "entry_fees": entry_fees,
                "funding_usdt": 0.0,
            }
            continue

        held_ms = decision_time - position["entry_time"]
        exit_reason = None
        if basis_pct <= target_basis_pct:
            exit_reason = "basis_target"
        elif held_ms >= max_hold_hours * 3_600_000:
            exit_reason = "max_hold"
        if exit_reason is None:
            continue

        spot_exit = spot_close * (1 - slip)
        swap_exit = swap_close * (1 + slip)
        quantity = position["quantity"]
        spot_pnl = quantity * (spot_exit - position["spot_entry"])
        swap_pnl = quantity * (position["swap_entry"] - swap_exit)
        exit_fees = (spot_exit * quantity * spot_fee
                     + swap_exit * quantity * swap_fee)
        gross_pnl = spot_pnl + swap_pnl
        fees = position["entry_fees"] + exit_fees
        funding_pnl = position["funding_usdt"]
        net_pnl = gross_pnl + funding_pnl - fees
        trades.append({
            "entry_time": position["entry_time"],
            "exit_time": decision_time,
            "entry_basis_pct": position["entry_basis_pct"],
            "exit_basis_pct": basis_pct,
            "hold_hours": held_ms / 3_600_000,
            "gross_pnl_usdt": gross_pnl,
            "funding_pnl_usdt": funding_pnl,
            "fees_usdt": fees,
            "net_pnl_usdt": net_pnl,
            "net_return_pct": net_pnl / notional * 100,
            "exit_reason": exit_reason,
        })
        position = None
        last_exit_time = decision_time

    if position is not None:
        open_at_end = 1
    return {"trades": trades, "open_at_end": open_at_end}


def metric_pack(trades: list[dict[str, Any]]) -> dict[str, Any]:
    values = [float(t["net_pnl_usdt"]) for t in trades]
    wins = [x for x in values if x > 0]
    losses = [x for x in values if x < 0]
    equity = peak = max_dd = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "closed_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 2) if trades else None,
        "net_pnl_usdt_per_1000_notional": round(sum(values), 4),
        "average_net_usdt_per_trade": round(sum(values) / len(values), 4) if values else None,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 3) if losses else None,
        "max_drawdown_usdt": round(max_dd, 4),
        "average_hold_hours": round(sum(t["hold_hours"] for t in trades) / len(trades), 2) if trades else None,
        "basis_target_exits": sum(t["exit_reason"] == "basis_target" for t in trades),
        "max_hold_exits": sum(t["exit_reason"] == "max_hold" for t in trades),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    now = int(time.time() * 1000)
    start = now - args.days * 86_400_000
    expected = int(args.days * 24 * 60 * 60_000 / STEP_MS)
    results = {}
    for symbol in args.symbols.split(","):
        symbol = symbol.strip().upper()
        if not symbol:
            continue
        print(f"FETCH {symbol}", flush=True)
        spot = fetch_candles(symbol, "spot", start, now, now)
        swap = fetch_candles(symbol, "swap", start, now, now)
        funding = fetch_funding(symbol, start, now)
        common = sorted(set(spot).intersection(swap))
        coverage = len(common) / max(1, expected)
        if coverage < 0.95:
            raise BacktestDataError(
                f"{symbol}: only {len(common)}/{expected} aligned candles ({coverage:.1%}); "
                "refusing to publish incomplete backtest"
            )
        bars = [{"time": stamp, "spot": spot[stamp], "swap": swap[stamp]}
                for stamp in common]
        simulation = simulate_cash_carry(
            bars, funding, notional=args.notional,
            min_entry_basis_pct=args.min_entry_basis,
            target_basis_pct=args.target_basis,
            max_hold_hours=args.max_hold_hours,
            spot_fee=args.spot_fee, swap_fee=args.swap_fee,
            slippage_pct=args.slippage_pct,
        )
        trades = simulation["trades"]
        midpoint = start + (now - start) // 2
        first_half = [t for t in trades if t["exit_time"] < midpoint]
        second_half = [t for t in trades if t["exit_time"] >= midpoint]
        results[symbol] = {
            "coverage_pct": round(coverage * 100, 2),
            "funding_records": len(funding),
            "all": metric_pack(trades),
            "first_half": metric_pack(first_half),
            "second_half": metric_pack(second_half),
            "open_at_end": simulation["open_at_end"],
            "assumptions": {
                "timeframe": INTERVAL,
                "entry_basis_pct": args.min_entry_basis,
                "target_basis_pct": args.target_basis,
                "max_hold_hours": args.max_hold_hours,
                "notional_usdt": args.notional,
                "spot_taker_fee": args.spot_fee,
                "swap_taker_fee": args.swap_fee,
                "slippage_per_fill_pct": args.slippage_pct,
                "historical_order_book": "not available; synthetic slippage used",
            },
        }
        print(f"RESULT {symbol} {json.dumps(results[symbol], separators=(',', ':'))}", flush=True)
    return {"days": args.days, "symbols": results}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--notional", type=float, default=1000.0)
    parser.add_argument("--min-entry-basis", type=float, default=0.45)
    parser.add_argument("--target-basis", type=float, default=0.05)
    parser.add_argument("--max-hold-hours", type=float, default=72.0)
    parser.add_argument("--spot-fee", type=float, default=SPOT_FEE)
    parser.add_argument("--swap-fee", type=float, default=SWAP_FEE)
    parser.add_argument("--slippage-pct", type=float, default=SLIPPAGE_PCT)
    args = parser.parse_args()
    if args.days <= 0:
        parser.error("--days must be positive")
    report = run(args)
    print("SPOT_FUTURES_BACKTEST_REPORT_BEGIN", flush=True)
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    print("SPOT_FUTURES_BACKTEST_REPORT_END", flush=True)


if __name__ == "__main__":
    main()
