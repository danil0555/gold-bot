from __future__ import annotations

import sqlite3

import pytest

from scripts.multiday_coverage_gate import evaluate_multiday_coverage


def _database(path, rows):
    with sqlite3.connect(path) as db:
        db.execute("""
            CREATE TABLE minute_candles (
                symbol TEXT NOT NULL,
                minute_ts INTEGER NOT NULL,
                is_closed INTEGER NOT NULL,
                PRIMARY KEY(symbol, minute_ts)
            )
        """)
        db.executemany(
            "INSERT INTO minute_candles(symbol, minute_ts, is_closed) VALUES (?, ?, ?)",
            rows,
        )


def _minutes(symbol, start_day, count, *, closed=1):
    start = start_day * 86_400_000
    return [(symbol, start + i * 60_000, closed) for i in range(count)]


def test_gate_requires_each_symbol_to_meet_minute_and_day_thresholds(tmp_path):
    path = tmp_path / "coverage.sqlite3"
    rows = []
    for symbol in ("BTC-USDT", "ETH-USDT"):
        for day in (20_000, 20_001, 20_002):
            rows.extend(_minutes(symbol, day, 200))
    _database(path, rows)
    report = evaluate_multiday_coverage(
        str(path), min_closed_minutes=600, min_utc_days=3,
        expected_symbols=["BTC-USDT", "ETH-USDT"],
    )
    assert report["holdout_ready"] is True
    assert report["per_symbol"]["BTC-USDT"]["closed_minutes"] == 600
    assert report["per_symbol"]["ETH-USDT"]["utc_days"] == 3


def test_one_well_covered_symbol_cannot_hide_an_undercovered_symbol(tmp_path):
    path = tmp_path / "coverage.sqlite3"
    rows = []
    for day in (20_000, 20_001, 20_002):
        rows.extend(_minutes("BTC-USDT", day, 250))
        rows.extend(_minutes("ETH-USDT", day, 100))
    _database(path, rows)
    report = evaluate_multiday_coverage(
        str(path), min_closed_minutes=600, min_utc_days=3
    )
    assert report["holdout_ready"] is False
    assert report["per_symbol"]["BTC-USDT"]["ready"] is True
    assert report["per_symbol"]["ETH-USDT"]["ready"] is False
    assert report["not_ready_symbols"] == ["ETH-USDT"]


def test_missing_expected_symbol_blocks_gate(tmp_path):
    path = tmp_path / "coverage.sqlite3"
    _database(path, _minutes("BTC-USDT", 20_000, 600))
    report = evaluate_multiday_coverage(
        str(path), min_closed_minutes=600, min_utc_days=1,
        expected_symbols=["BTC-USDT", "SOL-USDT"],
    )
    assert report["holdout_ready"] is False
    assert report["missing_expected_symbols"] == ["SOL-USDT"]


def test_open_candles_do_not_count_and_empty_history_is_not_ready(tmp_path):
    path = tmp_path / "coverage.sqlite3"
    _database(path, _minutes("BTC-USDT", 20_000, 600, closed=0))
    report = evaluate_multiday_coverage(str(path), min_closed_minutes=1, min_utc_days=1)
    assert report["holdout_ready"] is False
    assert report["per_symbol"] == {}


@pytest.mark.parametrize("minutes,days", [(0, 1), (1, 0)])
def test_invalid_thresholds_are_rejected(tmp_path, minutes, days):
    path = tmp_path / "coverage.sqlite3"
    _database(path, [])
    with pytest.raises(ValueError):
        evaluate_multiday_coverage(
            str(path), min_closed_minutes=minutes, min_utc_days=days
        )


def test_sparse_utc_days_do_not_pass_daily_minimum(tmp_path):
    path = tmp_path / "coverage.sqlite3"
    rows = _minutes("BTC-USDT", 20_000, 590)
    rows.extend(_minutes("BTC-USDT", 20_001, 5))
    rows.extend(_minutes("BTC-USDT", 20_002, 5))
    _database(path, rows)
    report = evaluate_multiday_coverage(
        str(path), min_closed_minutes=600, min_utc_days=3,
        min_closed_minutes_per_utc_day=120,
    )
    metrics = report["per_symbol"]["BTC-USDT"]
    assert metrics["minutes_ready"] is True
    assert metrics["days_ready"] is True
    assert metrics["daily_minutes_ready"] is False
    assert metrics["undercovered_utc_days"] == ["2024-10-05", "2024-10-06"]
    assert report["holdout_ready"] is False


def test_daily_minimum_must_be_positive(tmp_path):
    path = tmp_path / "coverage.sqlite3"
    _database(path, [])
    with pytest.raises(ValueError):
        evaluate_multiday_coverage(
            str(path), min_closed_minutes_per_utc_day=0
        )


def test_session_gap_diagnostics_are_reported(tmp_path):
    path = tmp_path / "coverage.sqlite3"
    rows = _minutes("BTC-USDT", 20_000, 200)
    rows += _minutes("BTC-USDT", 20_001, 200)
    rows += _minutes("BTC-USDT", 20_002, 200)
    _database(path, rows)
    with sqlite3.connect(path) as db:
        db.execute("""
            CREATE TABLE collector_sessions (
                capture_id TEXT PRIMARY KEY,
                started_at_ms INTEGER NOT NULL,
                ended_at_ms INTEGER,
                symbols_json TEXT NOT NULL
            )
        """)
        db.execute(
            "INSERT INTO collector_sessions VALUES (?, ?, ?, ?)",
            ("session-a", 20_000 * 86_400_000, 20_000 * 86_400_000 + 200 * 60_000, '["BTC-USDT"]'),
        )
    report = evaluate_multiday_coverage(str(path), min_closed_minutes=1, min_utc_days=1)
    assert report["capture_session_count"] == 1
    session = report["capture_sessions"][0]
    assert session["capture_id"] == "session-a"
    assert session["symbols"]["BTC-USDT"]["closed_minutes"] == 200
    assert session["symbols"]["BTC-USDT"]["max_internal_gap_minutes"] == 0



def test_unclosed_capture_session_blocks_holdout_readiness(tmp_path):
    path = tmp_path / "coverage.sqlite3"
    rows = []
    for day in (20_000, 20_001, 20_002):
        rows.extend(_minutes("BTC-USDT", day, 200))
    _database(path, rows)
    with sqlite3.connect(path) as db:
        db.execute("""
            CREATE TABLE collector_sessions (
                capture_id TEXT PRIMARY KEY,
                started_at_ms INTEGER NOT NULL,
                ended_at_ms INTEGER,
                symbols_json TEXT NOT NULL
            )
        """)
        db.execute(
            "INSERT INTO collector_sessions VALUES (?, ?, ?, ?)",
            ("still-running", 20_000 * 86_400_000, None, '["BTC-USDT"]'),
        )
    report = evaluate_multiday_coverage(
        str(path), min_closed_minutes=600, min_utc_days=3,
        min_closed_minutes_per_utc_day=120,
    )
    assert report["holdout_ready"] is False
    assert report["open_capture_sessions"] == ["still-running"]
    assert report["capture_sessions"][0]["status"] == "open"


def test_overlapping_sessions_for_same_symbol_are_flagged_ambiguous(tmp_path):
    path = tmp_path / "coverage.sqlite3"
    rows = _minutes("BTC-USDT", 20_000, 200)
    _database(path, rows)
    start = 20_000 * 86_400_000
    with sqlite3.connect(path) as db:
        db.execute("""
            CREATE TABLE collector_sessions (
                capture_id TEXT PRIMARY KEY,
                started_at_ms INTEGER NOT NULL,
                ended_at_ms INTEGER,
                symbols_json TEXT NOT NULL
            )
        """)
        db.executemany(
            "INSERT INTO collector_sessions VALUES (?, ?, ?, ?)",
            [
                ("session-a", start, start + 120 * 60_000, '["BTC-USDT"]'),
                ("session-b", start + 60 * 60_000, start + 180 * 60_000, '["BTC-USDT"]'),
            ],
        )
    report = evaluate_multiday_coverage(str(path), min_closed_minutes=1, min_utc_days=1)
    assert report["ambiguous_capture_sessions"] == ["session-a", "session-b"]
    assert all(
        session["session_attribution_ambiguous"]
        for session in report["capture_sessions"]
    )
