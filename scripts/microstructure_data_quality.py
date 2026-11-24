#!/usr/bin/env python3
"""Read-only data-quality audit for BingX microstructure capture databases.

This report evaluates collection completeness and transport diagnostics only.
It does not score trading signals or claim strategy profitability.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def _table_exists(db: sqlite3.Connection, name: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def audit_database(db_path: str) -> dict:
    with sqlite3.connect(db_path) as db:
        required = ("minute_candles", "minute_features", "derivatives_snapshots", "collector_events")
        missing = [name for name in required if not _table_exists(db, name)]
        if missing:
            raise ValueError("database missing required tables: " + ", ".join(missing))

        candles = db.execute("""
            SELECT symbol, minute_ts FROM minute_candles
            WHERE is_closed=1 ORDER BY symbol, minute_ts
        """).fetchall()
        features = db.execute("""
            SELECT symbol, minute_ts, trades_count, depth_samples,
                   buy_quote_usdt, sell_quote_usdt
            FROM minute_features ORDER BY symbol, minute_ts
        """).fetchall()
        derivatives = db.execute("""
            SELECT symbol, ts_ms, open_interest_base, funding_rate
            FROM derivatives_snapshots ORDER BY symbol, ts_ms
        """).fetchall()
        events = db.execute("""
            SELECT event_type, COUNT(*) FROM collector_events GROUP BY event_type
        """).fetchall()

    candle_by_symbol: dict[str, list[int]] = {}
    for symbol, stamp in candles:
        candle_by_symbol.setdefault(symbol, []).append(int(stamp))
    feature_by_symbol: dict[str, list[tuple]] = {}
    for row in features:
        feature_by_symbol.setdefault(row[0], []).append(row)
    deriv_by_symbol: dict[str, list[tuple]] = {}
    for row in derivatives:
        deriv_by_symbol.setdefault(row[0], []).append(row)

    symbols = sorted(set(candle_by_symbol) | set(feature_by_symbol) | set(deriv_by_symbol))
    per_symbol = {}
    warnings = []
    if not symbols:
        warnings.append({"code": "no_market_data_rows"})
    for symbol in symbols:
        candle_stamps = sorted(set(candle_by_symbol.get(symbol, [])))
        feature_rows = feature_by_symbol.get(symbol, [])
        derivative_rows = deriv_by_symbol.get(symbol, [])
        candle_set = set(candle_stamps)
        feature_set = {int(row[1]) for row in feature_rows}
        expected_minutes = (
            (candle_stamps[-1] - candle_stamps[0]) // 60_000 + 1
            if len(candle_stamps) >= 2 else len(candle_stamps)
        )
        candle_gaps = sum(
            max(0, (right - left) // 60_000 - 1)
            for left, right in zip(candle_stamps, candle_stamps[1:])
        )
        unmatched_candle_stamps = candle_set - feature_set
        # The collector may persist one REST warm-up candle before the first
        # complete minute of trade/depth features. Treat only that exact first
        # boundary candle as expected; later missing feature minutes remain warnings.
        initial_boundary_missing = (
            1 if candle_stamps and feature_rows
            and candle_stamps[0] not in feature_set
            and min(feature_set) == candle_stamps[0] + 60_000
            and unmatched_candle_stamps == {candle_stamps[0]}
            else 0
        )
        feature_missing = len(unmatched_candle_stamps) - initial_boundary_missing
        depth_minutes = sum(1 for row in feature_rows if int(row[3] or 0) > 0)
        trade_minutes = sum(1 for row in feature_rows if int(row[2] or 0) > 0)
        trade_count = sum(int(row[2] or 0) for row in feature_rows)
        depth_samples = sum(int(row[3] or 0) for row in feature_rows)
        quote_volume = sum(float(row[4] or 0) + float(row[5] or 0) for row in feature_rows)

        deriv_stamps = sorted(int(row[1]) for row in derivative_rows)
        oi_count = sum(row[2] is not None for row in derivative_rows)
        funding_count = sum(row[3] is not None for row in derivative_rows)
        derivative_gaps = [
            right - left for left, right in zip(deriv_stamps, deriv_stamps[1:])
        ]
        max_derivative_gap_seconds = (
            round(max(derivative_gaps) / 1000, 1) if derivative_gaps else None
        )

        metrics = {
            "closed_candles": len(candle_stamps),
            "expected_candle_minutes_in_observed_span": expected_minutes,
            "missing_candle_minutes": candle_gaps,
            "candle_gap_pct": round(candle_gaps / expected_minutes * 100, 2)
                if expected_minutes else None,
            "feature_minutes": len(feature_rows),
            "candle_minutes_without_features": feature_missing,
            "initial_boundary_candles_without_features": initial_boundary_missing,
            "depth_minutes": depth_minutes,
            "depth_minute_coverage_pct": round(depth_minutes / len(feature_rows) * 100, 2)
                if feature_rows else None,
            "depth_samples": depth_samples,
            "trade_minutes": trade_minutes,
            "trade_count": trade_count,
            "trade_quote_volume_usdt": round(quote_volume, 2),
            "derivatives_snapshots": len(derivative_rows),
            "open_interest_non_null": oi_count,
            "funding_non_null": funding_count,
            "max_derivatives_gap_seconds": max_derivative_gap_seconds,
            "first_candle_minute_ts": candle_stamps[0] if candle_stamps else None,
            "last_candle_minute_ts": candle_stamps[-1] if candle_stamps else None,
        }
        per_symbol[symbol] = metrics

        if not candle_stamps:
            warnings.append({"symbol": symbol, "code": "no_closed_candles"})
        if expected_minutes and candle_gaps:
            warnings.append({"symbol": symbol, "code": "candle_gaps_detected",
                             "missing_minutes": candle_gaps})
        if candle_stamps and feature_missing:
            warnings.append({"symbol": symbol, "code": "candle_minutes_missing_features",
                             "missing_minutes": feature_missing})
        if feature_rows and depth_minutes == 0:
            warnings.append({"symbol": symbol, "code": "no_depth_minutes"})
        if feature_rows and trade_count == 0:
            warnings.append({"symbol": symbol, "code": "no_trade_messages"})
        if derivative_rows and oi_count == 0:
            warnings.append({"symbol": symbol, "code": "no_open_interest_values"})
        if derivative_rows and funding_count == 0:
            warnings.append({"symbol": symbol, "code": "no_funding_values"})

    event_counts = {str(name): int(count) for name, count in events}
    for event_type in ("ws_reconnect", "derivatives_poll_error", "ws_subscription_error"):
        if event_counts.get(event_type, 0):
            warnings.append({"code": event_type, "count": event_counts[event_type]})

    total_candles = sum(x["closed_candles"] for x in per_symbol.values())
    total_features = sum(x["feature_minutes"] for x in per_symbol.values())
    total_depth = sum(x["depth_samples"] for x in per_symbol.values())
    total_trades = sum(x["trade_count"] for x in per_symbol.values())
    return {
        "report_type": "microstructure_data_quality",
        "symbols": symbols,
        "symbol_count": len(symbols),
        "totals": {
            "closed_candles": total_candles,
            "feature_minutes": total_features,
            "depth_samples": total_depth,
            "trades": total_trades,
            "collector_events": sum(event_counts.values()),
        },
        "collector_event_counts": event_counts,
        "per_symbol": per_symbol,
        "warnings": warnings,
        "overall_status": "warning" if warnings else "no_warnings_detected",
        "interpretation": (
            "Collection diagnostics only. Absence of warnings is not proof of "
            "complete market data, valid execution prices, or a profitable strategy."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--output", default="research-output/microstructure_data_quality.json")
    args = parser.parse_args()
    report = audit_database(args.db)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
