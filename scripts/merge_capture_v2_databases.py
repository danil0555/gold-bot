#!/usr/bin/env python3
"""Merge validated capture-scoped v2 market-data databases for research only.

The capture-scoped tables retain every session and its provenance. Legacy
(symbol, timestamp) tables are merged as convenience views with first-seen
rows retained on duplicate keys. No production database is accepted by design.
"""
from __future__ import annotations

import argparse
from contextlib import closing
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


def _table_names(db: sqlite3.Connection, schema: str = "main") -> set[str]:
    return {
        str(row[0]) for row in db.execute(
            f"SELECT name FROM \"{schema}\".sqlite_master WHERE type='table'"
        )
    }


def _legacy_conflicts(
    db: sqlite3.Connection, table: str, alias: str
) -> int:
    key = "ts_ms" if table == "derivatives_snapshots" else "minute_ts"
    columns = [item[0] for item in _columns(db, table)]
    comparisons = " OR ".join(
        f'existing."{column}" IS NOT incoming."{column}"'
        for column in columns if column not in {"symbol", key}
    )
    return int(db.execute(
        f'SELECT COUNT(*) FROM main."{table}" AS existing '
        f'JOIN "{alias}"."{table}" AS incoming '
        f'ON existing.symbol=incoming.symbol AND existing."{key}"=incoming."{key}" '
        f'WHERE {comparisons}'
    ).fetchone()[0])


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
    staging = target.with_name(target.name + ".partial")
    if staging.exists():
        raise FileExistsError(f"staging file already exists; refusing to overwrite: {staging}")

    for source in sources:
        if not source.is_file() or source.stat().st_size == 0:
            raise FileNotFoundError(f"capture database is missing or empty: {source}")
        validate_capture_database(source)
        with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as db:
            missing = REQUIRED_TABLES - _table_names(db)
            if missing:
                raise ValueError(f"{source} is missing required tables: {sorted(missing)}")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(f"file:{sources[0]}?mode=ro", uri=True)) as first:
            with closing(sqlite3.connect(staging)) as destination:
                first.backup(destination)
    
        before = {}
        duplicates_ignored = {table: 0 for table in LEGACY_KEYED_TABLES}
        with closing(sqlite3.connect(staging)) as destination:
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
                    source_tables = _table_names(destination, alias)
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
                        conflicts = _legacy_conflicts(destination, table, alias)
                        if conflicts:
                            raise ValueError(
                                f"{source} has {conflicts} conflicting duplicate key(s) "
                                f"in {table}; refusing ambiguous merge"
                            )
                        incoming = _count(destination, table, alias)
                        columns = [item[0] for item in _columns(destination, table)]
                        names = ", ".join(f'"{name}"' for name in columns)
                        destination.execute(
                            f'INSERT OR IGNORE INTO main."{table}" ({names}) '
                            f'SELECT {names} FROM "{alias}"."{table}"'
                        )
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
                except Exception:
                    destination.rollback()
                    raise
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
            destination.commit()
        # The SQLite connection must be closed before rename so WAL/journal
        # sidecars are checkpointed and the final path contains committed rows.
        staging.replace(target)
    except Exception:
        if staging.exists():
            staging.unlink()
        raise

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
