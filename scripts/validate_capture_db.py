#!/usr/bin/env python3
"""Fail-closed integrity and provenance checks for capture-scoped v2 SQLite data."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


REQUIRED_TABLES = {
    "collector_sessions",
    "collector_events",
    "capture_observations",
    "capture_storage_versions",
    "capture_minute_features",
    "capture_minute_candles",
    "capture_derivatives_snapshots",
}

ROW_TABLES = {
    "features": ("capture_minute_features", "minute_ts"),
    "candle": ("capture_minute_candles", "minute_ts"),
    "derivatives": ("capture_derivatives_snapshots", "ts_ms"),
}


class CaptureValidationError(ValueError):
    """Raised when a capture DB is incomplete or its provenance is inconsistent."""


def validate_capture_database(db_path: str | Path) -> dict:
    path = Path(db_path)
    if not path.is_file() or path.stat().st_size == 0:
        raise CaptureValidationError(f"database file is missing or empty: {path}")

    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    try:
        db = sqlite3.connect(uri, uri=True)
        db.row_factory = sqlite3.Row
        try:
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise CaptureValidationError(f"SQLite integrity_check failed: {integrity}")
            foreign_key_errors = [tuple(row) for row in db.execute("PRAGMA foreign_key_check")]
            if foreign_key_errors:
                raise CaptureValidationError(
                    f"SQLite foreign_key_check returned {len(foreign_key_errors)} row(s)"
                )

            tables = {
                row[0] for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            missing_tables = sorted(REQUIRED_TABLES - tables)
            if missing_tables:
                raise CaptureValidationError(
                    "missing required v2 table(s): " + ", ".join(missing_tables)
                )

            captures = db.execute("""
                SELECT v.capture_id, v.schema_version, s.started_at_ms,
                       s.ended_at_ms, s.symbols_json
                FROM capture_storage_versions v
                LEFT JOIN collector_sessions s USING(capture_id)
                ORDER BY v.capture_id
            """).fetchall()
            if not captures:
                raise CaptureValidationError("no v2 capture session found")

            capture_reports = []
            for capture in captures:
                capture_id = capture["capture_id"]
                if capture["schema_version"] != 2:
                    raise CaptureValidationError(
                        f"capture {capture_id}: unsupported schema version "
                        f"{capture['schema_version']}"
                    )
                if capture["started_at_ms"] is None:
                    raise CaptureValidationError(
                        f"capture {capture_id}: session metadata is missing"
                    )
                if capture["ended_at_ms"] is None:
                    raise CaptureValidationError(
                        f"capture {capture_id}: session has no clean end timestamp"
                    )
                try:
                    symbols = json.loads(capture["symbols_json"])
                except (TypeError, json.JSONDecodeError) as exc:
                    raise CaptureValidationError(
                        f"capture {capture_id}: invalid symbols_json"
                    ) from exc
                if not isinstance(symbols, list) or not symbols or not all(
                    isinstance(symbol, str) and symbol.strip() for symbol in symbols
                ):
                    raise CaptureValidationError(
                        f"capture {capture_id}: symbols_json must be a non-empty string list"
                    )

                stopped = db.execute("""
                    SELECT 1 FROM collector_events
                    WHERE event_type='capture_session_stopped' AND detail=?
                    LIMIT 1
                """, (capture_id,)).fetchone()
                if not stopped:
                    raise CaptureValidationError(
                        f"capture {capture_id}: clean stop event is missing"
                    )

                counts = {}
                for observation_type, (table, timestamp_column) in ROW_TABLES.items():
                    rows = db.execute(
                        f'SELECT symbol, "{timestamp_column}" AS observed_at_ms '
                        f'FROM "{table}" WHERE capture_id=?',
                        (capture_id,),
                    ).fetchall()
                    counts[observation_type] = len(rows)
                    if not rows:
                        raise CaptureValidationError(
                            f"capture {capture_id}: no {observation_type} rows stored"
                        )
                    for row in rows:
                        link = db.execute("""
                            SELECT 1 FROM capture_observations
                            WHERE capture_id=? AND symbol=? AND observed_at_ms=?
                              AND observation_type=? LIMIT 1
                        """, (
                            capture_id, row["symbol"], row["observed_at_ms"],
                            observation_type,
                        )).fetchone()
                        if not link:
                            raise CaptureValidationError(
                                f"capture {capture_id}: missing {observation_type} "
                                f"provenance for {row['symbol']} at {row['observed_at_ms']}"
                            )

                    links = db.execute("""
                        SELECT symbol, observed_at_ms FROM capture_observations
                        WHERE capture_id=? AND observation_type=?
                    """, (capture_id, observation_type)).fetchall()
                    row_keys = {
                        (row["symbol"], row["observed_at_ms"]) for row in rows
                    }
                    link_keys = {
                        (row["symbol"], row["observed_at_ms"]) for row in links
                    }
                    if row_keys != link_keys:
                        raise CaptureValidationError(
                            f"capture {capture_id}: {observation_type} rows and provenance "
                            "links do not match exactly"
                        )

                    if observation_type == "candle":
                        open_rows = db.execute("""
                            SELECT COUNT(*) FROM capture_minute_candles
                            WHERE capture_id=? AND is_closed != 1
                        """, (capture_id,)).fetchone()[0]
                        if open_rows:
                            raise CaptureValidationError(
                                f"capture {capture_id}: {open_rows} non-closed candle row(s)"
                            )

                session_link = db.execute("""
                    SELECT 1 FROM capture_observations
                    WHERE capture_id=? AND symbol='' AND observed_at_ms=?
                      AND observation_type='session' LIMIT 1
                """, (capture_id, capture["started_at_ms"])).fetchone()
                if not session_link:
                    raise CaptureValidationError(
                        f"capture {capture_id}: exact session provenance link is missing"
                    )

                for symbol in symbols:
                    for kind, count in counts.items():
                        symbol_count = db.execute(
                            f'SELECT COUNT(*) FROM "{ROW_TABLES[kind][0]}" '
                            "WHERE capture_id=? AND symbol=?",
                            (capture_id, symbol),
                        ).fetchone()[0]
                        if not symbol_count:
                            raise CaptureValidationError(
                                f"capture {capture_id}: {symbol} has no {kind} rows"
                            )

                capture_reports.append({
                    "capture_id": capture_id,
                    "schema_version": capture["schema_version"],
                    "symbols": symbols,
                    "ended_at_ms": capture["ended_at_ms"],
                    "row_counts": counts,
                })

            return {
                "status": "ok",
                "integrity_check": integrity,
                "foreign_key_errors": 0,
                "captures": capture_reports,
            }
        finally:
            db.close()
    except sqlite3.Error as exc:
        raise CaptureValidationError(f"SQLite validation error: {exc}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="path to the disposable capture SQLite DB")
    args = parser.parse_args()
    try:
        report = validate_capture_database(args.db)
    except CaptureValidationError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2))
        return 1
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
