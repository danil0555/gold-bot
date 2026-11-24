#!/usr/bin/env python3
"""Merge read-only BingX microstructure SQLite captures into one research database.

Only the four documented collector tables are copied. Production bot databases
are never opened or modified. Duplicate (symbol, timestamp) observations are
kept once; collector diagnostics are appended.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

TABLES = ("minute_candles", "minute_features", "derivatives_snapshots", "collector_events")
KEYED_TABLES = ("minute_candles", "minute_features", "derivatives_snapshots")


def _columns(connection: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA {schema}.table_info({table})")]


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
            missing = set(TABLES) - existing
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

    before = {
        table: int(destination.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in TABLES
    }
    source_summaries = []
    try:
        for index, source in enumerate(sources[1:], start=1):
            alias = f"capture_{index}"
            destination.execute(f"ATTACH DATABASE ? AS {alias}", (str(source),))
            for table in TABLES:
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
            destination.execute(f"DETACH DATABASE {alias}")
            destination.commit()
            source_summaries.append(str(source))
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
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
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
