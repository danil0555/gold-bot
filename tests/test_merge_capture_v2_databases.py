import json
import sqlite3

import pytest

from scripts.merge_capture_v2_databases import merge_capture_databases


SCHEMA = """
CREATE TABLE collector_sessions (
    capture_id TEXT PRIMARY KEY, started_at_ms INTEGER NOT NULL,
    ended_at_ms INTEGER, symbols_json TEXT NOT NULL
);
CREATE TABLE collector_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL,
    event_type TEXT NOT NULL, detail TEXT
);
CREATE TABLE capture_observations (
    capture_id TEXT NOT NULL, symbol TEXT NOT NULL, observed_at_ms INTEGER NOT NULL,
    observation_type TEXT NOT NULL,
    PRIMARY KEY(capture_id, symbol, observed_at_ms, observation_type)
);
CREATE TABLE capture_storage_versions (
    capture_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL
);
CREATE TABLE capture_minute_features (
    capture_id TEXT NOT NULL, symbol TEXT NOT NULL, minute_ts INTEGER NOT NULL,
    trades_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(capture_id, symbol, minute_ts)
);
CREATE TABLE capture_minute_candles (
    capture_id TEXT NOT NULL, symbol TEXT NOT NULL, minute_ts INTEGER NOT NULL,
    is_closed INTEGER NOT NULL,
    PRIMARY KEY(capture_id, symbol, minute_ts)
);
CREATE TABLE capture_derivatives_snapshots (
    capture_id TEXT NOT NULL, symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL,
    funding_rate REAL,
    PRIMARY KEY(capture_id, symbol, ts_ms)
);
CREATE TABLE minute_candles (
    symbol TEXT NOT NULL, minute_ts INTEGER NOT NULL, close REAL NOT NULL,
    PRIMARY KEY(symbol, minute_ts)
);
CREATE TABLE minute_features (
    symbol TEXT NOT NULL, minute_ts INTEGER NOT NULL, trades_count INTEGER NOT NULL,
    PRIMARY KEY(symbol, minute_ts)
);
CREATE TABLE derivatives_snapshots (
    symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL, funding_rate REAL,
    PRIMARY KEY(symbol, ts_ms)
);
"""


def make_capture(path, *, capture_id, minute_ts, candle_close=100.5, ended=True):
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        started = minute_ts
        ended_at = minute_ts + 240_000 if ended else None
        db.execute(
            "INSERT INTO collector_sessions VALUES (?, ?, ?, ?)",
            (capture_id, started, ended_at, json.dumps(["BTC-USDT"])),
        )
        db.execute(
            "INSERT INTO capture_storage_versions VALUES (?, 2)", (capture_id,)
        )
        db.execute(
            "INSERT INTO collector_events(ts_ms,event_type,detail) VALUES(?,?,?)",
            (started, "capture_session_started", capture_id),
        )
        if ended:
            db.execute(
                "INSERT INTO collector_events(ts_ms,event_type,detail) VALUES(?,?,?)",
                (ended_at, "capture_session_stopped", capture_id),
            )
        db.execute(
            "INSERT INTO capture_observations VALUES (?, '', ?, 'session')",
            (capture_id, started),
        )
        for kind, table, stamp_col in (
            ("features", "capture_minute_features", "minute_ts"),
            ("candle", "capture_minute_candles", "minute_ts"),
            ("derivatives", "capture_derivatives_snapshots", "ts_ms"),
        ):
            stamp = minute_ts if kind != "derivatives" else minute_ts + 10_000
            db.execute(
                "INSERT INTO capture_observations VALUES (?, 'BTC-USDT', ?, ?)",
                (capture_id, stamp, kind),
            )
            if kind == "features":
                db.execute(
                    "INSERT INTO capture_minute_features VALUES (?, 'BTC-USDT', ?, 10)",
                    (capture_id, stamp),
                )
            elif kind == "candle":
                db.execute(
                    "INSERT INTO capture_minute_candles VALUES (?, 'BTC-USDT', ?, 1)",
                    (capture_id, stamp),
                )
            else:
                db.execute(
                    "INSERT INTO capture_derivatives_snapshots VALUES (?, 'BTC-USDT', ?, 0.0001)",
                    (capture_id, stamp),
                )
        db.execute(
            "INSERT INTO minute_candles VALUES ('BTC-USDT', ?, ?)",
            (minute_ts, candle_close),
        )
        db.execute(
            "INSERT INTO minute_features VALUES ('BTC-USDT', ?, 10)", (minute_ts,)
        )
        db.execute(
            "INSERT INTO derivatives_snapshots VALUES ('BTC-USDT', ?, 0.0001)",
            (minute_ts + 10_000,),
        )


def test_merge_preserves_sessions_provenance_and_deduplicates_legacy_rows(tmp_path):
    first = tmp_path / "first.sqlite3"
    second = tmp_path / "second.sqlite3"
    output = tmp_path / "merged.sqlite3"
    make_capture(first, capture_id="capture-a", minute_ts=60_000)
    # Same legacy timestamps and values, but a separate capture with unique v2 provenance.
    make_capture(second, capture_id="capture-b", minute_ts=60_000)
    # Real collector artifacts use WAL. Reproduce that mode so the test catches
    # rows stranded in a staging -wal sidecar when the database is renamed.
    for source in (first, second):
        with sqlite3.connect(source) as source_db:
            assert source_db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"

    report = merge_capture_databases([first, second], output)

    assert report["capture_sessions"] == 2
    assert report["legacy_duplicate_rows_ignored"] == {
        "minute_candles": 1,
        "minute_features": 1,
        "derivatives_snapshots": 1,
    }
    with sqlite3.connect(output) as db:
        assert db.execute("SELECT COUNT(*) FROM collector_sessions").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM capture_observations").fetchone()[0] == 8
        assert db.execute("SELECT COUNT(*) FROM capture_minute_candles").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM minute_candles").fetchone()[0] == 1
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_merge_rejects_conflicting_legacy_duplicate_key(tmp_path):
    first = tmp_path / "first.sqlite3"
    second = tmp_path / "second.sqlite3"
    make_capture(first, capture_id="capture-a", minute_ts=60_000, candle_close=100.5)
    make_capture(second, capture_id="capture-b", minute_ts=60_000, candle_close=101.5)

    output = tmp_path / "merged.sqlite3"
    with pytest.raises(ValueError, match="conflicting duplicate"):
        merge_capture_databases([first, second], output)
    assert not output.exists()
    assert not (tmp_path / "merged.sqlite3.partial").exists()


def test_merge_rejects_incomplete_capture_session(tmp_path):
    source = tmp_path / "incomplete.sqlite3"
    make_capture(source, capture_id="capture-open", minute_ts=60_000, ended=False)

    with pytest.raises(ValueError, match="no clean end timestamp"):
        merge_capture_databases([source], tmp_path / "merged.sqlite3")


def test_merge_refuses_to_overwrite_output_or_input(tmp_path):
    source = tmp_path / "capture.sqlite3"
    make_capture(source, capture_id="capture-a", minute_ts=60_000)

    with pytest.raises(ValueError, match="must not be one of the input"):
        merge_capture_databases([source], source)
    output = tmp_path / "existing.sqlite3"
    output.write_bytes(b"keep")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        merge_capture_databases([source], output)
    assert output.read_bytes() == b"keep"


def test_merge_rejects_capture_id_collisions(tmp_path):
    first = tmp_path / "first.sqlite3"
    second = tmp_path / "second.sqlite3"
    make_capture(first, capture_id="same-id", minute_ts=60_000)
    make_capture(second, capture_id="same-id", minute_ts=120_000)

    with pytest.raises(ValueError, match="capture_id collision"):
        merge_capture_databases([first, second], tmp_path / "merged.sqlite3")
