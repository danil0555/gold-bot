"""Conservative close-to-close research backtest with explicit execution costs.

Signals are assumed known after the signal candle closes. Entry is the next
minute's close; exit is the exact configured horizon later. This intentionally
does not model intrabar stops, limit fills, spread dynamics, or order-book impact.
Funding is charged only from explicitly supplied settlement events. When a
schedule is supplied, missing expected events invalidate the affected trade.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _validate_bps(name: str, value: float) -> float:
    number = float(value)
    if not np.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be finite and >= 0")
    return number


def run_close_to_close_backtest(
    signals: pd.DataFrame,
    candles: pd.DataFrame,
    *,
    horizon_minutes: int,
    fee_bps_per_side: float,
    slippage_bps_per_side: float,
    funding_events: pd.DataFrame | None = None,
    expected_funding_timestamps_ms: list[int] | None = None,
) -> dict:
    """Evaluate LONG/SHORT signals without overlapping positions per symbol.

    Required signal columns: symbol, signal_ts, direction.
    Required candle columns: symbol, minute_ts, close.
    Funding events, when supplied, are exact settlement records with
    symbol, ts_ms, funding_rate (fraction, e.g. 0.0001 = 1 bp).
    """
    if not isinstance(horizon_minutes, int) or horizon_minutes < 1:
        raise ValueError("horizon_minutes must be a positive integer")
    fee = _validate_bps("fee_bps_per_side", fee_bps_per_side)
    slip = _validate_bps("slippage_bps_per_side", slippage_bps_per_side)
    if not {"symbol", "signal_ts", "direction"}.issubset(signals.columns):
        raise ValueError("signals require symbol, signal_ts, direction")
    if not {"symbol", "minute_ts", "close"}.issubset(candles.columns):
        raise ValueError("candles require symbol, minute_ts, close")

    prices = candles[["symbol", "minute_ts", "close"]].copy()
    prices["minute_ts"] = pd.to_numeric(prices.minute_ts, errors="coerce")
    prices["close"] = pd.to_numeric(prices.close, errors="coerce")
    prices = prices.dropna().sort_values(["symbol", "minute_ts"])
    if prices.duplicated(["symbol", "minute_ts"]).any():
        raise ValueError("duplicate symbol/minute candle keys")
    price_lookup = {
        (str(row.symbol), int(row.minute_ts)): float(row.close)
        for row in prices.itertuples(index=False)
        if float(row.close) > 0
    }

    events: dict[str, dict[int, float]] = {}
    if funding_events is not None:
        if not {"symbol", "ts_ms", "funding_rate"}.issubset(funding_events.columns):
            raise ValueError("funding_events require symbol, ts_ms, funding_rate")
        for row in funding_events.itertuples(index=False):
            rate = float(row.funding_rate)
            if not np.isfinite(rate):
                continue
            events.setdefault(str(row.symbol), {})[int(row.ts_ms)] = rate
    schedule = None if expected_funding_timestamps_ms is None else sorted(
        {int(ts) for ts in expected_funding_timestamps_ms}
    )

    frame = signals[["symbol", "signal_ts", "direction"]].copy()
    frame["signal_ts"] = pd.to_numeric(frame.signal_ts, errors="coerce")
    frame["direction"] = frame.direction.astype(str).str.upper()
    if frame.signal_ts.isna().any() or not frame.direction.isin(["LONG", "SHORT"]).all():
        raise ValueError("signals need numeric timestamps and LONG/SHORT directions")
    frame = frame.sort_values(["symbol", "signal_ts"])
    last_exit: dict[str, int] = {}
    trades, rejected = [], {"missing_entry": 0, "missing_exit": 0,
                             "overlap": 0, "invalid_price": 0,
                             "incomplete_funding": 0}
    total_cost_pct = 2.0 * (fee + slip) / 100.0

    for row in frame.itertuples(index=False):
        symbol, signal_ts = str(row.symbol), int(row.signal_ts)
        entry_ts = signal_ts + 60_000
        exit_ts = entry_ts + horizon_minutes * 60_000
        if symbol in last_exit and entry_ts <= last_exit[symbol]:
            rejected["overlap"] += 1
            continue
        entry = price_lookup.get((symbol, entry_ts))
        exit_price = price_lookup.get((symbol, exit_ts))
        if entry is None:
            rejected["missing_entry"] += 1
            continue
        if exit_price is None:
            rejected["missing_exit"] += 1
            continue
        if entry <= 0 or exit_price <= 0:
            rejected["invalid_price"] += 1
            continue

        side = 1.0 if row.direction == "LONG" else -1.0
        gross_pct = side * (exit_price / entry - 1.0) * 100.0
        funding_pct = 0.0
        matched_funding_events = 0
        if schedule is not None:
            expected = [ts for ts in schedule if entry_ts < ts <= exit_ts]
            available = events.get(symbol, {})
            if any(ts not in available for ts in expected):
                rejected["incomplete_funding"] += 1
                continue
            # Positive funding rate means longs pay and shorts receive.
            funding_pct = sum(-side * available[ts] * 100.0 for ts in expected)
            matched_funding_events = len(expected)

        net_pct = gross_pct - total_cost_pct + funding_pct
        trades.append({
            "symbol": symbol, "direction": row.direction,
            "signal_ts": signal_ts, "entry_ts": entry_ts, "exit_ts": exit_ts,
            "entry_price": entry, "exit_price": exit_price,
            "gross_return_pct": gross_pct, "execution_cost_pct": total_cost_pct,
            "funding_return_pct": funding_pct, "net_return_pct": net_pct,
            "funding_events": matched_funding_events,
        })
        last_exit[symbol] = exit_ts

    ledger = pd.DataFrame(trades)
    if ledger.empty:
        return {
            "report_type": "close_to_close_execution_cost_backtest_v1",
            "status": "no_valid_trades", "trades": [], "trade_count": 0,
            "rejected_signals": rejected, "funding_mode": (
                "exact_settlement_events" if schedule is not None else "disabled"
            ),
        }

    returns = ledger.net_return_pct.to_numpy(dtype=float) / 100.0
    equity = np.cumprod(1.0 + returns)
    peaks = np.maximum.accumulate(np.concatenate(([1.0], equity)))[1:]
    drawdowns = equity / peaks - 1.0
    gross_wins = ledger.loc[ledger.net_return_pct > 0, "net_return_pct"]
    gross_losses = ledger.loc[ledger.net_return_pct < 0, "net_return_pct"]
    loss_sum = abs(float(gross_losses.sum()))
    return {
        "report_type": "close_to_close_execution_cost_backtest_v1",
        "status": "evaluated",
        "trade_count": int(len(ledger)),
        "win_rate_pct": float((ledger.net_return_pct > 0).mean() * 100.0),
        "gross_expectancy_pct": float(ledger.gross_return_pct.mean()),
        "net_expectancy_pct": float(ledger.net_return_pct.mean()),
        "net_total_return_pct": float((equity[-1] - 1.0) * 100.0),
        "profit_factor": (float(gross_wins.sum() / loss_sum) if loss_sum > 0
                          else (None if gross_wins.empty else float("inf"))),
        "max_drawdown_pct": float(drawdowns.min() * 100.0),
        "average_holding_minutes": float(
            (ledger.exit_ts - ledger.entry_ts).mean() / 60_000.0
        ),
        "funding_mode": ("exact_settlement_events" if schedule is not None else "disabled"),
        "funding_events_charged": int(ledger.funding_events.sum()),
        "rejected_signals": rejected,
        "methodology": [
            "Signal is assumed known after its candle close; entry is next exact minute close.",
            "Exit is the exact configured horizon after entry; missing candles are rejected, never bridged.",
            "At most one position per symbol; a new entry must occur strictly after the prior exit.",
            "Fees and slippage are fixed per-side assumptions, charged on entry and exit.",
            "Funding uses only exact supplied settlement timestamps; missing scheduled events invalidate the trade.",
            "Returns are compounded in accepted trade order; this is not a portfolio-capital or margin model.",
        ],
        "limitations": [
            "Close-to-close execution is a simplified proxy, not an order-fill simulation.",
            "No intrabar stops/targets, variable spread, latency, market impact, liquidation, or capital allocation.",
            "Funding is reliable only if the supplied schedule and settlement records are complete.",
            "No confidence intervals or multiple-testing correction are applied.",
        ],
        "trades": ledger.to_dict(orient="records"),
    }
