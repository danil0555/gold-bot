#!/usr/bin/env python3
"""Per-symbol minimum-history gate for multi-day microstructure holdout research.

The gate is read-only. It checks each required symbol independently so one
well-covered market cannot hide missing history for another symbol.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path


def evaluate_multiday_coverage(
    db_path: str,
    *,
    min_closed_minutes: int = 600,
    min_utc_days: int = 3,
    expected_symbols: list[str] | None = None,
) -> dict:
    if min_closed_minutes < 1 or min_utc_days < 1:
        raise ValueError("minimum minutes and UTC days must be positive")

    with sqlite3.connect(f"file:{Path(db_path).resolve()}?mode=ro", uri=True) as db:
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='minute_candles'"
        ).fetchone()
        if not exists:
            raise ValueError("database is missing minute_candles table")
        rows = db.execute("""
            SELECT symbol, COUNT(DISTINCT minute_ts),
                   COUNT(DISTINCT date(minute_ts / 1000, 'unixepoch')),
                   MIN(minute_ts), MAX(minute_ts)
            FROM minute_candles
            WHERE is_closed = 1
            GROUP BY symbol
            ORDER BY symbol
        """).fetchall()

    observed = {
        str(symbol): {
            "closed_minutes": int(count),
            "utc_days": int(days),
            "first_minute_ts": int(first_ts) if first_ts is not None else None,
            "last_minute_ts": int(last_ts) if last_ts is not None else None,
            "minutes_ready": int(count) >= min_closed_minutes,
            "days_ready": int(days) >= min_utc_days,
        }
        for symbol, count, days, first_ts, last_ts in rows
    }
    required = sorted(set(expected_symbols or []) | set(observed))
    per_symbol = {}
    for symbol in required:
        metrics = observed.get(symbol, {
            "closed_minutes": 0,
            "utc_days": 0,
            "first_minute_ts": None,
            "last_minute_ts": None,
            "minutes_ready": False,
            "days_ready": False,
        })
        per_symbol[symbol] = {
            **metrics,
            "ready": bool(metrics["minutes_ready"] and metrics["days_ready"]),
        }

    missing_symbols = sorted(set(expected_symbols or []) - set(observed))
    not_ready = [symbol for symbol, metrics in per_symbol.items() if not metrics["ready"]]
    ready = bool(per_symbol) and not missing_symbols and not not_ready
    return {
        "report_type": "multiday_holdout_coverage_gate_v1",
        "holdout_ready": ready,
        "thresholds": {
            "min_closed_minutes_per_symbol": int(min_closed_minutes),
            "min_utc_days_per_symbol": int(min_utc_days),
        },
        "required_symbols": required,
        "missing_expected_symbols": missing_symbols,
        "not_ready_symbols": not_ready,
        "per_symbol": per_symbol,
        "interpretation": (
            "History sufficiency only. This gate does not establish data quality, "
            "statistical significance, predictive value, or strategy profitability."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--output", default="research-output/multiday_coverage_gate.json")
    parser.add_argument("--min-closed-minutes", type=int, default=600)
    parser.add_argument("--min-utc-days", type=int, default=3)
    parser.add_argument("--expected-symbols", default="")
    args = parser.parse_args()
    expected = [item.strip() for item in args.expected_symbols.split(",") if item.strip()]
    report = evaluate_multiday_coverage(
        args.db,
        min_closed_minutes=args.min_closed_minutes,
        min_utc_days=args.min_utc_days,
        expected_symbols=expected,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    print(f"holdout_ready={'true' if report['holdout_ready'] else 'false'}", flush=True)


if __name__ == "__main__":
    main()
