#!/usr/bin/env python3
"""Merge read-only BingX microstructure SQLite captures into one research database.

The four core collector tables and optional session metadata are copied. Production bot databases
are never opened or modified. Duplicate (symbol, timestamp) observations are
kept once; collector diagnostics are appended.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

TABLES = ("minute_candles", "minute_features", "derivatives_snapshots", "collector_events", "collector_sessions", "capture_observations", "capture_storage_versions", "capture_minute_features", "capture_minute_candles", "capture_derivatives_snapshots")
REQUIRED_TABLES = ("minute_candles", "minute_features", "derivatives_snapshots", "collector_events")
KEYED_TABLES = (
    "minute_candles", "minute_features", "derivatives_snapshots",
    "collector_sessions", "capture_observations", "capture_storage_versions",
    "capture_minute_features", "capture_minute_candles", "capture_derivatives_snapshots",
)


def _columns(connection: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA {schema}.table_info({table})")]


def _ensure_capture_observations_schema(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='capture_observations'"
    ).fetchone()
    if row is None:
        connection.execute("""
            CREATE TABLE capture_observations (
                capture_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                observed_at_ms INTEGER NOT NULL,
                observation_type TEXT NOT NULL CHECK (
                    observation_type IN ('session', 'candle', 'features', 'derivatives')
                ),
                PRIMARY KEY(capture_id, symbol, observed_at_ms, observation_type)
            )
        """)
        return

    # PR #14's first schema version did not allow the session marker. Rebuild
    # only the newly-created merge output, preserving every existing link.
    if "'session'" not in str(row[0]).lower():
        expected = ["capture_id", "symbol", "observed_at_ms", "observation_type"]
        if _columns(connection, "capture_observations") != expected:
            raise ValueError("unsupported capture_observations schema")
        connection.execute("ALTER TABLE capture_observations RENAME TO capture_observations_legacy")
        connection.execute("""
            CREATE TABLE capture_observations (
                capture_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                observed_at_ms INTEGER NOT NULL,
                observation_type TEXT NOT NULL CHECK (
                    observation_type IN ('session', 'candle', 'features', 'derivatives')
                ),
                PRIMARY KEY(capture_id, symbol, observed_at_ms, observation_type)
            )
        """)
        connection.execute(
            "INSERT INTO capture_observations SELECT * FROM capture_observations_legacy"
        )
        connection.execute("DROP TABLE capture_observations_legacy")


def _ensure_capture_scoped_schema(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS capture_storage_versions (
            capture_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL CHECK (schema_version >= 2)
        );
        CREATE TABLE IF NOT EXISTS capture_minute_features (
            capture_id TEXT NOT NULL, symbol TEXT NOT NULL, minute_ts INTEGER NOT NULL,
            trades_count INTEGER NOT NULL DEFAULT 0, buy_trades INTEGER NOT NULL DEFAULT 0,
            sell_trades INTEGER NOT NULL DEFAULT 0, buy_quote_usdt REAL NOT NULL DEFAULT 0,
            sell_quote_usdt REAL NOT NULL DEFAULT 0, cvd_quote_usdt REAL NOT NULL DEFAULT 0,
            taker_imbalance REAL, avg_spread_bps REAL, min_spread_bps REAL, max_spread_bps REAL,
            avg_bid_depth5_usdt REAL, avg_ask_depth5_usdt REAL,
            avg_bid_depth20_usdt REAL, avg_ask_depth20_usdt REAL,
            avg_book_imbalance5 REAL, avg_book_imbalance20 REAL,
            avg_microprice_offset_bps REAL, depth_samples INTEGER NOT NULL DEFAULT 0,
            last_mid REAL,
            PRIMARY KEY(capture_id, symbol, minute_ts)
        );
        CREATE TABLE IF NOT EXISTS capture_minute_candles (
            capture_id TEXT NOT NULL, symbol TEXT NOT NULL, minute_ts INTEGER NOT NULL,
            close_ts INTEGER NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
            low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL,
            is_closed INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY(capture_id, symbol, minute_ts)
        );
        CREATE TABLE IF NOT EXISTS capture_derivatives_snapshots (
            capture_id TEXT NOT NULL, symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL,
            open_interest_base REAL, funding_rate REAL, mark_price REAL,
            index_price REAL, next_funding_time INTEGER,
            PRIMARY KEY(capture_id, symbol, ts_ms)
        );
    """)


def merge_databases(inputs: list[str | Path], output: str | Path) -> dict:
    sources = [Path(item).resolve() for item in inputs]
    target = Path(output).resolve()
    if not sources:
        raise ValueError("at least one input database is required")
    if target in sources:
        raise ValueError("output database must not be one of the input databases")
    for source in sources:
        if not source.is_file():
            raise FileNotFoundError(source)
        check = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        try:
            existing = {row[0] for row in check.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            missing = set(REQUIRED_TABLES) - existing
            if missing:
                raise ValueError(f"{source} is missing collector tables: {sorted(missing)}")
        finally:
            check.close()

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    first = sqlite3.connect(f"file:{sources[0]}?mode=ro", uri=True)
    destination = sqlite3.connect(target)
    try:
        first.backup(destination)
    finally:
        first.close()

    destination.execute("""
        CREATE TABLE IF NOT EXISTS collector_sessions (
            capture_id TEXT PRIMARY KEY,
            started_at_ms INTEGER NOT NULL,
            ended_at_ms INTEGER,
            symbols_json TEXT NOT NULL
        )
    """)
    _ensure_capture_observations_schema(destination)
    _ensure_capture_scoped_schema(destination)
    destination.commit()
    before = {
        table: int(destination.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in TABLES
    }
    try:
        for index, source in enumerate(sources[1:], start=1):
            alias = f"capture_{index}"
            destination.execute(f"ATTACH DATABASE ? AS {alias}", (str(source),))
            source_tables = {row[0] for row in destination.execute(
                f"SELECT name FROM {alias}.sqlite_master WHERE type='table'"
            )}
            for table in TABLES:
                if table not in source_tables:
                    continue
                dest_columns = _columns(destination, table)
                src_columns = _columns(destination, table, alias)
                if dest_columns != src_columns:
                    raise ValueError(f"schema mismatch in {table} for {source}")
                names = ", ".join(f'"{name}"' for name in dest_columns)
                if table in KEYED_TABLES:
                    destination.execute(
                        f'INSERT OR IGNORE INTO main."{table}" ({names}) '
                        f'SELECT {names} FROM "{alias}"."{table}"'
                    )
                else:
                    # Diagnostic IDs are local to each capture; omit them so SQLite
                    # assigns fresh IDs instead of silently dropping colliding IDs.
                    event_columns = ", ".join(f'"{name}"' for name in dest_columns if name != "id")
                    destination.execute(
                        f'INSERT INTO main."{table}" ({event_columns}) '
                        f'SELECT {event_columns} FROM "{alias}"."{table}"'
                    )
            destination.commit()
            destination.execute(f"DETACH DATABASE {alias}")
        after = {
            table: int(destination.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in TABLES
        }
    finally:
        destination.close()

    return {
        "report_type": "microstructure_capture_merge_v1",
        "input_count": len(sources),
        "inputs": [str(item) for item in sources],
        "output": str(target),
        "rows_before": before,
        "rows_after": after,
        "new_rows": {table: after[table] - before[table] for table in TABLES},
        "duplicate_key_rows_ignored": {
            table: sum(
                _count_rows(source, table) for source in sources[1:]
            ) - (after[table] - before[table])
            for table in KEYED_TABLES
        },
        "decision": "merged_research_data_only_no_strategy_approved",
    }


def _count_rows(path: Path, table: str) -> int:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        # Older collector captures legitimately predate optional v2 tables.
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) if exists else 0
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inputs", nargs="+", required=True, help="SQLite capture database paths")
    parser.add_argument("--output", required=True, help="Merged research database path")
    parser.add_argument("--report", default=None, help="Optional JSON merge report path")
    args = parser.parse_args()
    report = merge_databases(args.inputs, args.output)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(rendered, encoding="utf-8")
    print(rendered, flush=True)


if __name__ == "__main__":
    main()
