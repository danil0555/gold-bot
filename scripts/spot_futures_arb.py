#!/usr/bin/env python3
"""Read-only BingX spot/perpetual cash-and-carry research scanner.

This module only reads public market endpoints. It never authenticates, accesses
account endpoints, places orders, or writes to the bot's SQLite database.
The initial supported direction is LONG spot + SHORT USDT-M perpetual, which
can receive funding only while the funding rate is positive.
"""
from __future__ import annotations

import argparse
import json
import math
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

BASE_URL = "https://open-api.bingx.com"
DEFAULT_SYMBOLS = ("BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT",
                   "ADA-USDT", "DOGE-USDT", "LINK-USDT", "LTC-USDT")
DEFAULT_SPOT_TAKER_FEE = 0.001
DEFAULT_SWAP_TAKER_FEE = 0.0005
DEFAULT_FUNDING_HAIRCUT = 0.50
DEFAULT_STRESS_WIDENING_BPS = 50.0
MAX_QUOTE_AGE_MS = 3_000


class MarketDataError(RuntimeError):
    pass


def _finite_positive(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{label} must be numeric") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return result


def _levels(raw: Any, label: str) -> list[tuple[float, float]]:
    if not isinstance(raw, list):
        raise MarketDataError(f"{label}: order book levels are missing")
    result = []
    for row in raw:
        try:
            price = _finite_positive(row[0], f"{label} price")
            qty = _finite_positive(row[1], f"{label} quantity")
            result.append((price, qty))
        except (IndexError, TypeError, ValueError):
            continue
    if not result:
        raise MarketDataError(f"{label}: order book is empty")
    return result


def depth_vwap(levels: list[tuple[float, float]], quantity: float,
               side: str) -> tuple[float, float]:
    """Return executable VWAP and filled base quantity for asks(buy)/bids(sell)."""
    quantity = _finite_positive(quantity, "quantity")
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    ordered = sorted(levels, key=lambda x: x[0], reverse=(side == "sell"))
    remaining = quantity
    quote = 0.0
    filled = 0.0
    for price, available in ordered:
        take = min(remaining, available)
        quote += take * price
        filled += take
        remaining -= take
        if remaining <= max(1e-12, quantity * 1e-10):
            break
    if remaining > max(1e-12, quantity * 1e-10):
        raise MarketDataError(
            f"insufficient {side} depth: requested {quantity:.10g}, filled {filled:.10g}"
        )
    return quote / filled, filled


def parse_order_book(payload: dict[str, Any], label: str) -> tuple[list, list, int]:
    if not isinstance(payload, dict) or payload.get("code", 0) not in (0, "0", None):
        raise MarketDataError(f"{label}: exchange returned {payload!r}")
    data = payload.get("data") or {}
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        raise MarketDataError(f"{label}: unexpected order book response")
    bids = _levels(data.get("bids"), f"{label} bids")
    asks = _levels(data.get("asks"), f"{label} asks")
    stamp = int(data.get("ts") or data.get("timestamp") or data.get("time") or 0)
    return bids, asks, stamp


def calculate_cash_carry(
    symbol: str,
    spot_bids: list[tuple[float, float]],
    spot_asks: list[tuple[float, float]],
    swap_bids: list[tuple[float, float]],
    swap_asks: list[tuple[float, float]],
    funding_rate: float,
    funding_periods: int,
    quote_notional: float,
    spot_taker_fee: float = DEFAULT_SPOT_TAKER_FEE,
    swap_taker_fee: float = DEFAULT_SWAP_TAKER_FEE,
    funding_haircut: float = DEFAULT_FUNDING_HAIRCUT,
    stress_widening_bps: float = DEFAULT_STRESS_WIDENING_BPS,
) -> dict[str, Any]:
    """Evaluate LONG spot + SHORT perpetual with executable depth on all four fills.

    The base quantity is sized from the requested quote notional. Entry buys spot
    at asks and sells swap at bids. Exit sells spot at bids and covers swap at asks.
    Funding is haircut and only credited when the current rate is positive.
    """
    notional = _finite_positive(quote_notional, "quote_notional")
    if funding_periods < 0:
        raise ValueError("funding_periods cannot be negative")
    if not 0 <= funding_haircut <= 1:
        raise ValueError("funding_haircut must be between 0 and 1")
    if stress_widening_bps < 0:
        raise ValueError("stress_widening_bps cannot be negative")
    spot_entry_ref = (spot_bids[0][0] + spot_asks[0][0]) / 2
    quantity = notional / spot_entry_ref

    spot_entry, _ = depth_vwap(spot_asks, quantity, "buy")
    swap_entry, _ = depth_vwap(swap_bids, quantity, "sell")
    spot_exit, _ = depth_vwap(spot_bids, quantity, "sell")
    swap_exit, _ = depth_vwap(swap_asks, quantity, "buy")

    entry_basis_pct = (swap_entry / spot_entry - 1) * 100
    exit_basis_pct = (swap_exit / spot_exit - 1) * 100
    spot_pnl = quantity * (spot_exit - spot_entry)
    swap_pnl = quantity * (swap_entry - swap_exit)
    entry_spot_fee = spot_entry * quantity * spot_taker_fee
    exit_spot_fee = spot_exit * quantity * spot_taker_fee
    entry_swap_fee = swap_entry * quantity * swap_taker_fee
    exit_swap_fee = swap_exit * quantity * swap_taker_fee
    fees = entry_spot_fee + exit_spot_fee + entry_swap_fee + exit_swap_fee

    # Funding is settled on the swap notional. Positive rate pays the short side.
    funding_per_period = max(0.0, funding_rate) * swap_entry * quantity
    expected_funding = funding_per_period * funding_periods * funding_haircut
    expected_net = spot_pnl + swap_pnl + expected_funding - fees

    # Stress test: retain spot exit price, but assume the futures basis widens
    # adversely by the configured number of basis points before closing.
    stressed_swap_exit = spot_exit * (1 + (exit_basis_pct + stress_widening_bps / 100) / 100)
    stressed_swap_pnl = quantity * (swap_entry - stressed_swap_exit)
    stressed_swap_exit_fee = stressed_swap_exit * quantity * swap_taker_fee
    stressed_fees = (entry_spot_fee + exit_spot_fee + entry_swap_fee
                     + stressed_swap_exit_fee)
    stressed_net = spot_pnl + stressed_swap_pnl + expected_funding - stressed_fees

    gross_basis_capture = spot_pnl + swap_pnl
    total_costs = fees
    return {
        "symbol": symbol,
        "direction": "LONG_SPOT_SHORT_PERPETUAL",
        "quantity_base": quantity,
        "quote_notional": notional,
        "spot_entry_vwap": spot_entry,
        "swap_entry_vwap": swap_entry,
        "spot_exit_vwap_now": spot_exit,
        "swap_exit_vwap_now": swap_exit,
        "entry_basis_pct": entry_basis_pct,
        "exit_basis_pct_now": exit_basis_pct,
        "funding_rate_current": funding_rate,
        "funding_periods_assumed": funding_periods,
        "funding_haircut": funding_haircut,
        "expected_funding_usdt": expected_funding,
        "spot_pnl_if_closed_now_usdt": spot_pnl,
        "swap_pnl_if_closed_now_usdt": swap_pnl,
        "basis_pnl_if_closed_now_usdt": gross_basis_capture,
        "estimated_round_trip_fees_usdt": total_costs,
        "expected_net_if_closed_now_usdt": spot_pnl + swap_pnl - fees,
        "expected_net_at_current_basis_assumption_usdt": expected_net,
        "stress_widening_bps": stress_widening_bps,
        "stressed_net_usdt": stressed_net,
        "net_return_pct_of_notional": expected_net / notional * 100,
        "stressed_return_pct_of_notional": stressed_net / notional * 100,
        "assumptions": [
            "current funding rate persists for the assumed number of settlements",
            "funding estimate is haircut; future funding is not guaranteed",
            "stressed exit widens the observed basis by the configured amount",
            "fees use configured taker rates; actual account tier may differ",
            "no borrow interest, transfer cost, tax, or liquidation risk is modeled",
        ],
    }


def _request(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    query = urllib.parse.urlencode(params or {})
    url = BASE_URL + path + (("?" + query) if query else "")
    request = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "gold-bot-spot-futures-research/1.0",
    })
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise MarketDataError(f"public market request failed for {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("code", 0) not in (0, "0", None):
        raise MarketDataError(f"exchange error for {path}: {payload!r}")
    return payload


def fetch_symbol_snapshot(symbol: str, now_ms: int | None = None) -> dict[str, Any]:
    """Fetch public spot/swap books and current funding; no credentials are used."""
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    spot = _request("/openApi/spot/v1/market/depth", {"symbol": symbol, "limit": 100})
    swap = _request("/openApi/swap/v2/quote/depth", {"symbol": symbol, "limit": 100})
    funding = _request("/openApi/swap/v2/quote/premiumIndex", {"symbol": symbol})
    spot_bids, spot_asks, spot_ts = parse_order_book(spot, f"{symbol} spot")
    swap_bids, swap_asks, swap_ts = parse_order_book(swap, f"{symbol} perpetual")
    funding_data = funding.get("data") or {}
    if isinstance(funding_data, list):
        funding_data = next((x for x in funding_data if x.get("symbol") == symbol), {})
    if not isinstance(funding_data, dict):
        raise MarketDataError(f"{symbol}: invalid premium index payload")
    rate = float(funding_data.get("lastFundingRate",
                                 funding_data.get("fundingRate", 0.0)))
    next_funding = int(funding_data.get("nextFundingTime") or 0)
    stamps = [x for x in (spot_ts, swap_ts) if x > 0]
    if not spot_ts or not swap_ts:
        raise MarketDataError(f"{symbol}: exchange did not timestamp both order books")
    if abs(spot_ts - swap_ts) > MAX_QUOTE_AGE_MS:
        raise MarketDataError(f"{symbol}: spot/swap books are not synchronized")
    if any(now - stamp > MAX_QUOTE_AGE_MS or stamp - now > MAX_QUOTE_AGE_MS
           for stamp in stamps):
        raise MarketDataError(f"{symbol}: stale or future-dated order book")
    return {
        "symbol": symbol, "spot_bids": spot_bids, "spot_asks": spot_asks,
        "swap_bids": swap_bids, "swap_asks": swap_asks,
        "funding_rate": rate, "next_funding_time": next_funding,
        "spot_book_time": spot_ts, "swap_book_time": swap_ts,
    }


def scan_symbols(symbols: tuple[str, ...] = DEFAULT_SYMBOLS,
                 quote_notional: float = 1000.0,
                 funding_periods: int = 3,
                 spot_taker_fee: float = DEFAULT_SPOT_TAKER_FEE,
                 swap_taker_fee: float = DEFAULT_SWAP_TAKER_FEE,
                 min_expected_net_usdt: float = 0.0) -> list[dict[str, Any]]:
    """Read-only scan; returns candidates sorted by estimated net return."""
    reports = []
    for symbol in symbols:
        try:
            snap = fetch_symbol_snapshot(symbol)
            report = calculate_cash_carry(
                symbol=symbol, spot_bids=snap["spot_bids"], spot_asks=snap["spot_asks"],
                swap_bids=snap["swap_bids"], swap_asks=snap["swap_asks"],
                funding_rate=snap["funding_rate"], funding_periods=funding_periods,
                quote_notional=quote_notional, spot_taker_fee=spot_taker_fee,
                swap_taker_fee=swap_taker_fee,
            )
            report["next_funding_time"] = snap["next_funding_time"]
            report["spot_book_time"] = snap["spot_book_time"]
            report["swap_book_time"] = snap["swap_book_time"]
            report["eligible_candidate"] = (
                snap["funding_rate"] > 0
                and report["expected_net_at_current_basis_assumption_usdt"] > min_expected_net_usdt
                and report["stressed_net_usdt"] > 0
            )
            reports.append(report)
        except (MarketDataError, ValueError, TypeError) as exc:
            reports.append({"symbol": symbol, "eligible_candidate": False,
                            "rejected_reason": str(exc)})
    return sorted(reports, key=lambda x: x.get(
        "expected_net_at_current_basis_assumption_usdt", float("-inf")), reverse=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    parser.add_argument("--notional", type=float, default=1000.0)
    parser.add_argument("--funding-periods", type=int, default=3)
    parser.add_argument("--spot-taker-fee", type=float, default=DEFAULT_SPOT_TAKER_FEE)
    parser.add_argument("--swap-taker-fee", type=float, default=DEFAULT_SWAP_TAKER_FEE)
    parser.add_argument("--min-net-usdt", type=float, default=0.0)
    args = parser.parse_args()
    reports = scan_symbols(
        tuple(x.strip().upper() for x in args.symbols.split(",") if x.strip()),
        args.notional, args.funding_periods, args.spot_taker_fee,
        args.swap_taker_fee, args.min_net_usdt,
    )
    print(json.dumps(reports, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
