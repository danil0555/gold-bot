#!/usr/bin/env python3
"""Merge validated capture-scoped v2 market-data databases for research only.

The capture-scoped tables retain every session and its provenance. Legacy
(symbol, timestamp) tables are merged as convenience views with first-seen
rows retained on duplicate keys. No production database is accepted by design.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from scripts.validate_capture_db import validate_capture_database

V2_TABLES = (
    "collector_sessions",
    "capture_storage_versions",
    "capture_observations",
    "capture_minute_features",
    "capture_minute_candles",
    "capture_derivatives_snapshots",
)
LEGACY_KEYED_TABLES = (
    "minute_candles",
    "minute_features",
    "derivatives_snapshots",
)
EVENT_TABLE = "collector_events"
REQUIRED_TABLES = set(V2_TABLES) | set(LEGACY_KEYED_TABLES) | {EVENT_TABLE}
COPY_ORDER = (
    "collector_sessions",
    "capture_storage_versions",
    "capture_observations",
    "capture_minute_features",
    "capture_minute_candles",
    "capture_derivatives_snapshots",
    "minute_candles",
    "minute_features",
    "derivatives_snapshots",
    "collector_events",
)


def _table_names(db: sqlite3.Connection) -> set[str]:
    return {
        str(row[0]) for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _columns(db: sqlite3.Connection, table: str, schema: str = "main") -> list[tuple]:
    return [
        (row[1], row[2], row[3], row[4], row[5])
        for row in db.execute(f'PRAGMA "{schema}".table_info("{table}")')
    ]


def _count(db: sqlite3.Connection, table: str, schema: str = "main") -> int:
    return int(db.execute(f'SELECT COUNT(*) FROM "{schema}"."{table}"').fetchone()[0])


def merge_capture_databases(
    inputs: list[str | Path], output: str | Path
) -> dict:
    sources = [Path(item).resolve() for item in inputs]
    target = Path(output).resolve()
    if not sources:
        raise ValueError("at least one input capture database is required")
    if len(set(sources)) != len(sources):
        raise ValueError("duplicate input database paths are not allowed")
    if target in sources:
        raise ValueError("output database must not be one of the input databases")
    if target.exists():
        raise FileExistsError(f"output already exists; refusing to overwrite: {target}")

    for source in sources:
        if not source.is_file() or source.stat().st_size == 0:
            raise FileNotFoundError(f"capture database is missing or empty: {source}")
        validate_capture_database(source)
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as db:
            missing = REQUIRED_TABLES - _table_names(db)
            if missing:
                raise ValueError(f"{source} is missing required tables: {sorted(missing)}")

    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(f"file:{sources[0]}?mode=ro", uri=True) as first:
        with sqlite3.connect(target) as destination:
            first.backup(destination)

    before = {}
    duplicates_ignored = {table: 0 for table in LEGACY_KEYED_TABLES}
    with sqlite3.connect(target) as destination:
        destination.execute("PRAGMA foreign_keys=ON")
        before = {table: _count(destination, table) for table in COPY_ORDER}

        first_capture_ids = {
            row[0] for row in destination.execute(
                "SELECT capture_id FROM collector_sessions"
            )
        }
        for index, source in enumerate(sources[1:], start=1):
            alias = f"capture_{index}"
            destination.execute(f'ATTACH DATABASE ? AS "{alias}"', (str(source),))
            try:
                source_tables = _table_names(destination)
                missing = REQUIRED_TABLES - source_tables
                if missing:
                    raise ValueError(f"{source} is missing required tables: {sorted(missing)}")
                for table in COPY_ORDER:
                    if _columns(destination, table) != _columns(destination, table, alias):
                        raise ValueError(f"schema mismatch in {table} for {source}")

                incoming_ids = {
                    row[0] for row in destination.execute(
                        f'SELECT capture_id FROM "{alias}".collector_sessions'
                    )
                }
                collisions = first_capture_ids & incoming_ids
                if collisions:
                    raise ValueError(
                        f"capture_id collision in {source}: {sorted(collisions)}"
                    )

                for table in V2_TABLES:
                    columns = [item[0] for item in _columns(destination, table)]
                    names = ", ".join(f'"{name}"' for name in columns)
                    destination.execute(
                        f'INSERT INTO main."{table}" ({names}) '
                        f'SELECT {names} FROM "{alias}"."{table}"'
                    )

                for table in LEGACY_KEYED_TABLES:
                    incoming = _count(destination, table, alias)
                    columns = [item[0] for item in _columns(destination, table)]
                    names = ", ".join(f'"{name}"' for name in columns)
                    destination.execute(
                        f'INSERT OR IGNORE INTO main."{table}" ({names}) '
                        f'SELECT {names} FROM "{alias}"."{table}"'
                    )
                    inserted = _count(destination, table) - before[table] - sum(
                        _count(destination, t) - before[t]
                        for t in LEGACY_KEYED_TABLES if t != table
                    )
                    # Compute ignored rows from this input by comparing the
                    # table count immediately before and after this insert.
                    # The exact per-input delta is recorded below instead.
                    del inserted
                    after_table = _count(destination, table)
                    prior_table = _count(destination, table) - max(
                        0, after_table - (_count(destination, table) - incoming)
                    )
                    del prior_table
                    # SQLite changes() is the reliable inserted-row count.
                    inserted_rows = int(destination.execute("SELECT changes()").fetchone()[0])
                    duplicates_ignored[table] += incoming - inserted_rows

                event_columns = [
                    item[0] for item in _columns(destination, EVENT_TABLE)
                    if item[0] != "id"
                ]
                names = ", ".join(f'"{name}"' for name in event_columns)
                destination.execute(
                    f'INSERT INTO main."{EVENT_TABLE}" ({names}) '
                    f'SELECT {names} FROM "{alias}"."{EVENT_TABLE}"'
                )
                first_capture_ids.update(incoming_ids)
                destination.commit()
            finally:
                destination.execute(f'DETACH DATABASE "{alias}"')

        integrity = destination.execute("PRAGMA integrity_check").fetchone()[0]
        fk_errors = [tuple(row) for row in destination.execute("PRAGMA foreign_key_check")]
        if integrity != "ok" or fk_errors:
            raise ValueError(
                f"merged database failed integrity checks: integrity={integrity}, "
                f"foreign_key_errors={len(fk_errors)}"
            )
        after = {table: _count(destination, table) for table in COPY_ORDER}

    return {
        "report_type": "capture_scoped_v2_multiday_merge_v1",
        "input_count": len(sources),
        "inputs": [str(item) for item in sources],
        "output": str(target),
        "rows_before": before,
        "rows_after": after,
        "new_rows": {table: after[table] - before[table] for table in COPY_ORDER},
        "legacy_duplicate_rows_ignored": duplicates_ignored,
        "capture_sessions": after["collector_sessions"],
        "decision": "merged_research_data_only_no_strategy_approved",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", nargs="+", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--report", default=None)
    args = parser.parse_args()
    report = merge_capture_databases(args.inputs, args.output)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(rendered, encoding="utf-8")
    print(rendered, flush=True)


if __name__ == "__main__":
    main()
