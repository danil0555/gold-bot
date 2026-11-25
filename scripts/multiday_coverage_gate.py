#!/usr/bin/env python3
"""Per-symbol minimum-history gate for multi-day microstructure holdout research.

The gate is read-only. It checks each required symbol independently so one
well-covered market cannot hide missing history for another symbol.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path


def evaluate_multiday_coverage(
    db_path: str,
    *,
    min_closed_minutes: int = 600,
    min_utc_days: int = 3,
    min_closed_minutes_per_utc_day: int = 120,
    expected_symbols: list[str] | None = None,
) -> dict:
    if min_closed_minutes < 1 or min_utc_days < 1 or min_closed_minutes_per_utc_day < 1:
        raise ValueError("minimum minutes, daily minutes and UTC days must be positive")

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
        session_table_exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='collector_sessions'"
        ).fetchone()
        sessions = db.execute("""
            SELECT capture_id, started_at_ms, ended_at_ms, symbols_json
            FROM collector_sessions ORDER BY started_at_ms, capture_id
        """).fetchall() if session_table_exists else []
        observation_table_exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='capture_observations'"
        ).fetchone() is not None
        exact_capture_ids = set()
        if observation_table_exists:
            exact_capture_ids = {
                str(row[0]) for row in db.execute(
                    "SELECT DISTINCT capture_id FROM capture_observations "
                    "WHERE observation_type='session'"
                ).fetchall()
            }
        daily_rows = db.execute("""
            SELECT symbol, date(minute_ts / 1000, 'unixepoch'),
                   COUNT(DISTINCT minute_ts), MIN(minute_ts), MAX(minute_ts)
            FROM minute_candles
            WHERE is_closed = 1
            GROUP BY symbol, date(minute_ts / 1000, 'unixepoch')
            ORDER BY symbol, date(minute_ts / 1000, 'unixepoch')
        """).fetchall()

    now_ms = int(time.time() * 1000)
    parsed_sessions = []
    for capture_id, started_at_ms, ended_at_ms, symbols_json in sessions:
        try:
            session_symbols = sorted(set(json.loads(symbols_json)))
        except (TypeError, ValueError):
            session_symbols = []
        parsed_sessions.append({
            "capture_id": str(capture_id),
            "started_at_ms": int(started_at_ms),
            "ended_at_ms": int(ended_at_ms) if ended_at_ms is not None else None,
            "effective_end_ms": int(ended_at_ms) if ended_at_ms is not None else now_ms,
            "symbols": session_symbols,
        })

    session_coverage = []
    for session in parsed_sessions:
        capture_id = session["capture_id"]
        started_at_ms = session["started_at_ms"]
        ended_at_ms = session["ended_at_ms"]
        session_symbols = session["symbols"]
        overlapping_capture_ids = []
        for other in parsed_sessions:
            if other["capture_id"] == capture_id:
                continue
            shares_symbols = bool(set(session_symbols) & set(other["symbols"]))
            overlaps_time = (
                started_at_ms <= other["effective_end_ms"]
                and other["started_at_ms"] <= session["effective_end_ms"]
            )
            if shares_symbols and overlaps_time:
                overlapping_capture_ids.append(other["capture_id"])
        per_session = {}
        for symbol in session_symbols:
            session_db = sqlite3.connect(
                f"file:{Path(db_path).resolve()}?mode=ro", uri=True
            )
            try:
                start_minute = int(started_at_ms) // 60_000 * 60_000
                end_value = int(ended_at_ms) if ended_at_ms is not None else int(time.time() * 1000)
                end_minute = end_value // 60_000 * 60_000
                if capture_id in exact_capture_ids:
                    timestamps = [
                        int(row[0]) for row in session_db.execute(
                            "SELECT observed_at_ms FROM capture_observations "
                            "WHERE capture_id=? AND symbol=? AND observation_type='candle' "
                            "ORDER BY observed_at_ms",
                            (capture_id, symbol),
                        ).fetchall()
                    ]
                else:
                    timestamps = [
                        int(row[0]) for row in session_db.execute(
                            "SELECT minute_ts FROM minute_candles WHERE symbol=? AND is_closed=1 "
                            "AND minute_ts >= ? AND minute_ts <= ? ORDER BY minute_ts",
                            (symbol, start_minute, end_minute),
                        ).fetchall()
                    ]
            finally:
                session_db.close()
            gaps = [
                max(0, (right - left) // 60_000 - 1)
                for left, right in zip(timestamps, timestamps[1:])
            ]
            per_session[str(symbol)] = {
                "closed_minutes": len(timestamps),
                "first_minute_ts": timestamps[0] if timestamps else None,
                "last_minute_ts": timestamps[-1] if timestamps else None,
                "max_internal_gap_minutes": max(gaps, default=0),
                "internal_gap_count": sum(1 for gap in gaps if gap > 0),
            }
        session_coverage.append({
            "capture_id": capture_id,
            "started_at_ms": started_at_ms,
            "ended_at_ms": ended_at_ms,
            "status": "closed" if ended_at_ms is not None else "open",
            "overlapping_capture_ids": sorted(overlapping_capture_ids),
            "session_attribution_ambiguous": bool(overlapping_capture_ids),
            "attribution_source": (
                "capture_observations" if capture_id in exact_capture_ids
                else "time_window_estimate_legacy_session"
            ),
            "symbols": per_session,
        })

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
    daily_coverage: dict[str, dict[str, dict]] = {}
    for symbol, utc_day, count, first_ts, last_ts in daily_rows:
        daily_coverage.setdefault(str(symbol), {})[str(utc_day)] = {
            "closed_minutes": int(count),
            "first_minute_ts": int(first_ts) if first_ts is not None else None,
            "last_minute_ts": int(last_ts) if last_ts is not None else None,
            "ready": int(count) >= min_closed_minutes_per_utc_day,
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
        days = daily_coverage.get(symbol, {})
        daily_minutes_ready = bool(days) and all(day["ready"] for day in days.values())
        per_symbol[symbol] = {
            **metrics,
            "daily_minutes_ready": daily_minutes_ready,
            "daily_coverage": days,
            "undercovered_utc_days": [day for day, values in days.items() if not values["ready"]],
            "ready": bool(metrics["minutes_ready"] and metrics["days_ready"] and daily_minutes_ready),
        }

    missing_symbols = sorted(set(expected_symbols or []) - set(observed))
    not_ready = [symbol for symbol, metrics in per_symbol.items() if not metrics["ready"]]
    open_capture_sessions = [
        item["capture_id"] for item in session_coverage if item["status"] == "open"
    ]
    ambiguous_capture_sessions = [
        item["capture_id"] for item in session_coverage
        if item["session_attribution_ambiguous"]
    ]
    ready = (
        bool(per_symbol) and not missing_symbols and not not_ready
        and not open_capture_sessions
    )
    return {
        "report_type": "multiday_holdout_coverage_gate_v1",
        "holdout_ready": ready,
        "thresholds": {
            "min_closed_minutes_per_symbol": int(min_closed_minutes),
            "min_utc_days_per_symbol": int(min_utc_days),
            "min_closed_minutes_per_utc_day": int(min_closed_minutes_per_utc_day),
        },
        "required_symbols": required,
        "missing_expected_symbols": missing_symbols,
        "not_ready_symbols": not_ready,
        "per_symbol": per_symbol,
        "capture_session_count": len(session_coverage),
        "open_capture_sessions": open_capture_sessions,
        "ambiguous_capture_sessions": ambiguous_capture_sessions,
        "capture_sessions": session_coverage,
        "interpretation": (
            "Daily history sufficiency only. An unclosed capture session blocks holdout readiness. "
            "Modern sessions are identified by an explicit provenance marker and use exact "
            "capture-scoped links. Legacy sessions in mixed merged databases use labeled time-window "
            "estimates because they have no marker. Overlapping sessions can still "
            "share market minutes, but their observation provenance remains distinct. This gate "
            "does not establish continuous "
            "coverage or data quality, statistical significance, predictive value, or profitability. "
            "Inspect capture-session boundaries and within-session gaps separately."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--output", default="research-output/multiday_coverage_gate.json")
    parser.add_argument("--min-closed-minutes", type=int, default=600)
    parser.add_argument("--min-utc-days", type=int, default=3)
    parser.add_argument("--min-closed-minutes-per-utc-day", type=int, default=120)
    parser.add_argument("--expected-symbols", default="")
    args = parser.parse_args()
    expected = [item.strip() for item in args.expected_symbols.split(",") if item.strip()]
    report = evaluate_multiday_coverage(
        args.db,
        min_closed_minutes=args.min_closed_minutes,
        min_utc_days=args.min_utc_days,
        min_closed_minutes_per_utc_day=args.min_closed_minutes_per_utc_day,
        expected_symbols=expected,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)
    print(f"holdout_ready={'true' if report['holdout_ready'] else 'false'}", flush=True)


if __name__ == "__main__":
    main()
