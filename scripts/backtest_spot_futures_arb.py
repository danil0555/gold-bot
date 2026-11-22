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
import urllib.error
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
    query = urllib.parse.urlencode(params)
    url = BASE + path + "?" + query
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "gold-bot-cash-carry-backtest/1.0",
    })
    last_error: Exception | None = None
    for attempt in range(5):
        previous = LAST_REQUEST.get(path, 0.0)
        wait = 1.25 - (time.monotonic() - previous)
        if wait > 0:
            time.sleep(wait)
        try:
            with urllib.request.urlopen(req, timeout=25) as response:
                raw = response.read().decode("utf-8")
            LAST_REQUEST[path] = time.monotonic()
            payload = json.loads(raw)
        except urllib.error.HTTPError as exc:
            LAST_REQUEST[path] = time.monotonic()
            if exc.code in (418, 429) and attempt < 4:
                time.sleep(min(30.0, 2.0 ** (attempt + 1)))
                last_error = exc
                continue
            raise BacktestDataError(f"HTTP {exc.code} for {path}") from exc
        except Exception as exc:
            LAST_REQUEST[path] = time.monotonic()
            raise BacktestDataError(f"request failed {path}: {exc}") from exc

        if isinstance(payload, dict) and payload.get("code", 0) in (0, "0", None):
            return payload
        if isinstance(payload, dict) and str(payload.get("code")) == "100410" and attempt < 4:
            last_error = BacktestDataError(f"rate limited by BingX: {payload!r}")
            time.sleep(min(30.0, 2.0 ** (attempt + 1)))
            continue
        raise BacktestDataError(f"BingX returned an error for {path}: {payload!r}")
    raise BacktestDataError(f"rate limit retries exhausted for {path}: {last_error}")


def unwrap_data(payload: dict[str, Any]) -> list:
    data = payload.get("data") or []
    if isinstance(data, dict):
        for key in ("data", "list", "rows", "candles"):
            if isinstance(data.get(key), list):
                return data[key]
        return []
    return data if isinstance(data, list) else []


def parse_candle(row: Any, source: str) -> tuple[int, float, float, float] | None:
    """Return candle open time, open price, close price, and close timestamp."""
    try:
        if isinstance(row, (list, tuple)):
            stamp = int(float(row[0]))
            open_price = float(row[1])
            close = float(row[4])
            close_time = int(float(row[6])) if len(row) > 6 else stamp + STEP_MS - 1
        elif isinstance(row, dict):
            stamp = int(float(row.get("time", row.get("openTime", row.get("t", 0)))))
            open_price = float(row["open"])
            close = float(row["close"])
            close_time = int(float(row.get("closeTime", stamp + STEP_MS - 1)))
        else:
            return None
        if stamp < 10_000_000_000:
            stamp *= 1000
        if close_time < 10_000_000_000:
            close_time *= 1000
        if (stamp <= 0 or open_price <= 0 or close <= 0
                or not math.isfinite(open_price) or not math.isfinite(close)):
            return None
        return stamp, open_price, close, close_time
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def fetch_candles(symbol: str, source: str, start_ms: int, end_ms: int,
                  now_ms: int) -> dict[int, dict[str, float]]:
    if source == "spot":
        path = "/openApi/spot/v2/market/kline"
    elif source == "swap":
        path = "/openApi/swap/v3/quote/klines"
    else:
        raise ValueError("source must be spot or swap")
    result: dict[int, dict[str, float]] = {}
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
        for stamp, open_price, close, _ in parsed:
            result[stamp] = {"open": open_price, "close": close}
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
    """Use completed closes for signals and the NEXT candle open for execution.

    This avoids the optimistic same-close fill that is impossible to guarantee
    after a candle-close signal becomes known.
    """
    if notional <= 0 or max_hold_hours <= 0:
        raise ValueError("notional and max_hold_hours must be positive")
    if not 0 <= target_basis_pct < min_entry_basis_pct:
        raise ValueError("target basis must be below entry threshold")
    ordered_bars = sorted(bars, key=lambda x: x["time"])
    funding = sorted(funding, key=lambda x: x["time"])
    funding_index = 0
    latest_settled_rate = None
    position = None
    last_exit_time = -10**30
    trades = []
    open_at_end = 0
    open_position_mark = None
    slip = slippage_pct / 100

    def apply_funding(until_time: int, fallback_swap_price: float) -> None:
        nonlocal funding_index, latest_settled_rate
        while (funding_index < len(funding)
               and funding[funding_index]["time"] <= until_time):
            event = funding[funding_index]
            latest_settled_rate = float(event["rate"])
            if position is not None and event["time"] > position["entry_time"]:
                mark = float(event.get("mark") or fallback_swap_price)
                position["funding_usdt"] += position["quantity"] * mark * float(event["rate"])
            funding_index += 1

    for index, bar in enumerate(ordered_bars):
        signal_time = int(bar["time"]) + STEP_MS
        spot_close = float(bar["spot_close"])
        swap_close = float(bar["swap_close"])
        if spot_close <= 0 or swap_close <= 0:
            continue
        signal_basis_pct = (swap_close / spot_close - 1) * 100
        next_bar = ordered_bars[index + 1] if index + 1 < len(ordered_bars) else None

        # Funding settled by the completed candle close belongs to any position
        # that was already open at that settlement timestamp.
        apply_funding(signal_time, swap_close)

        # No next candle means there is no defensible next-open execution price.
        if next_bar is None:
            continue
        fill_time = int(next_bar["time"])
        spot_open = float(next_bar["spot_open"])
        swap_open = float(next_bar["swap_open"])
        if spot_open <= 0 or swap_open <= 0:
            continue

        if position is None:
            if signal_time - last_exit_time < cooldown_minutes * 60_000:
                continue
            if latest_settled_rate is None or latest_settled_rate <= 0:
                continue
            # The signal is known at this close; execution is modeled at the
            # next candle open, with adverse slippage on both legs.
            spot_entry = spot_open * (1 + slip)
            swap_entry = swap_open * (1 - slip)
            executable_basis = (swap_entry / spot_entry - 1) * 100
            if signal_basis_pct < min_entry_basis_pct or executable_basis < min_entry_basis_pct:
                continue
            quantity = notional / spot_entry
            entry_fees = (spot_entry * quantity * spot_fee
                          + swap_entry * quantity * swap_fee)
            position = {
                "entry_signal_time": signal_time,
                "entry_time": fill_time,
                "entry_signal_basis_pct": signal_basis_pct,
                "entry_basis_pct": executable_basis,
                "spot_entry": spot_entry,
                "swap_entry": swap_entry,
                "quantity": quantity,
                "entry_fees": entry_fees,
                "funding_usdt": 0.0,
            }
            continue

        held_ms = signal_time - position["entry_time"]
        exit_reason = None
        if signal_basis_pct <= target_basis_pct:
            exit_reason = "basis_target"
        elif held_ms >= max_hold_hours * 3_600_000:
            exit_reason = "max_hold"
        if exit_reason is None:
            continue

        # If there is a data gap, include any settlement before the next-open
        # fill. With contiguous hourly candles this is normally a no-op.
        apply_funding(fill_time, swap_open)
        spot_exit = spot_open * (1 - slip)
        swap_exit = swap_open * (1 + slip)
        quantity = position["quantity"]
        spot_pnl = quantity * (spot_exit - position["spot_entry"])
        swap_pnl = quantity * (position["swap_entry"] - swap_exit)
        exit_fees = (spot_exit * quantity * spot_fee
                     + swap_exit * quantity * swap_fee)
        gross_pnl = spot_pnl + swap_pnl
        fees = position["entry_fees"] + exit_fees
        funding_pnl = position["funding_usdt"]
        net_pnl = gross_pnl + funding_pnl - fees
        actual_exit_basis_pct = (swap_exit / spot_exit - 1) * 100
        trades.append({
            "entry_signal_time": position["entry_signal_time"],
            "entry_time": position["entry_time"],
            "exit_signal_time": signal_time,
            "exit_time": fill_time,
            "entry_signal_basis_pct": position["entry_signal_basis_pct"],
            "entry_basis_pct": position["entry_basis_pct"],
            "exit_signal_basis_pct": signal_basis_pct,
            "exit_basis_pct": actual_exit_basis_pct,
            "hold_hours": (fill_time - position["entry_time"]) / 3_600_000,
            "gross_pnl_usdt": gross_pnl,
            "funding_pnl_usdt": funding_pnl,
            "fees_usdt": fees,
            "net_pnl_usdt": net_pnl,
            "net_return_pct": net_pnl / notional * 100,
            "exit_reason": exit_reason,
        })
        position = None
        last_exit_time = fill_time

    if position is not None and ordered_bars:
        open_at_end = 1
        last_bar = ordered_bars[-1]
        mark_time = int(last_bar["time"]) + STEP_MS
        spot_mark = float(last_bar["spot_close"])
        swap_mark = float(last_bar["swap_close"])
        spot_exit = spot_mark * (1 - slip)
        swap_exit = swap_mark * (1 + slip)
        quantity = position["quantity"]
        gross = (quantity * (spot_exit - position["spot_entry"])
                 + quantity * (position["swap_entry"] - swap_exit))
        exit_fees = (spot_exit * quantity * spot_fee
                     + swap_exit * quantity * swap_fee)
        fees = position["entry_fees"] + exit_fees
        net = gross + position["funding_usdt"] - fees
        open_position_mark = {
            "mark_time": mark_time,
            "hold_hours": (mark_time - position["entry_time"]) / 3_600_000,
            "entry_signal_time": position["entry_signal_time"],
            "entry_time": position["entry_time"],
            "entry_basis_pct": position["entry_basis_pct"],
            "current_basis_pct": (swap_mark / spot_mark - 1) * 100,
            "gross_pnl_usdt": gross,
            "funding_pnl_usdt": position["funding_usdt"],
            "estimated_total_fees_usdt": fees,
            "net_mark_to_market_usdt": net,
            "note": "Unclosed position marked at last completed hourly close with modeled exit slippage and fees",
        }
    return {
        "trades": trades,
        "open_at_end": open_at_end,
        "open_position_mark": open_position_mark,
    }

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
        bars = [{
            "time": stamp,
            "spot_open": spot[stamp]["open"],
            "spot_close": spot[stamp]["close"],
            "swap_open": swap[stamp]["open"],
            "swap_close": swap[stamp]["close"],
        } for stamp in common]
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
            "open_position_mark_to_market": simulation["open_position_mark"],
            "closed_trade_details": [{
                "entry_time": t["entry_time"], "exit_time": t["exit_time"],
                "entry_basis_pct": round(t["entry_basis_pct"], 5),
                "exit_basis_pct": round(t["exit_basis_pct"], 5),
                "hold_hours": round(t["hold_hours"], 3),
                "gross_pnl_usdt": round(t["gross_pnl_usdt"], 5),
                "funding_pnl_usdt": round(t["funding_pnl_usdt"], 5),
                "fees_usdt": round(t["fees_usdt"], 5),
                "net_pnl_usdt": round(t["net_pnl_usdt"], 5),
                "exit_reason": t["exit_reason"],
            } for t in trades],
            "assumptions": {
                "timeframe": INTERVAL,
                "entry_basis_pct": args.min_entry_basis,
                "target_basis_pct": args.target_basis,
                "max_hold_hours": args.max_hold_hours,
                "notional_usdt": args.notional,
                "spot_taker_fee": args.spot_fee,
                "swap_taker_fee": args.swap_fee,
                "slippage_per_fill_pct": args.slippage_pct,
                "signal_execution_model": "completed 1h close signal; next 1h candle open fill plus adverse slippage",
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
