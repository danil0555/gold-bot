import sqlite3

import pytest

from scripts.merge_microstructure_databases import merge_databases


SCHEMA = """
CREATE TABLE minute_candles (
 symbol TEXT NOT NULL, minute_ts INTEGER NOT NULL, close_ts INTEGER NOT NULL,
 open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, close REAL NOT NULL,
 volume REAL NOT NULL, is_closed INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(symbol, minute_ts)
);
CREATE TABLE minute_features (
 symbol TEXT NOT NULL, minute_ts INTEGER NOT NULL, trades_count INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(symbol, minute_ts)
);
CREATE TABLE derivatives_snapshots (
 symbol TEXT NOT NULL, ts_ms INTEGER NOT NULL, funding_rate REAL,
 PRIMARY KEY(symbol, ts_ms)
);
CREATE TABLE collector_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, ts_ms INTEGER NOT NULL,
 event_type TEXT NOT NULL, detail TEXT
);
CREATE TABLE collector_sessions (
 capture_id TEXT PRIMARY KEY, started_at_ms INTEGER NOT NULL,
 ended_at_ms INTEGER, symbols_json TEXT NOT NULL
);
"""


def make_capture(path, candle_rows, event):
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.executemany(
        "INSERT INTO minute_candles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        candle_rows,
    )
    db.executemany(
        "INSERT INTO minute_features(symbol, minute_ts, trades_count) VALUES (?, ?, ?)",
        [(row[0], row[1], 10) for row in candle_rows],
    )
    db.execute(
        "INSERT INTO derivatives_snapshots(symbol, ts_ms, funding_rate) VALUES (?, ?, ?)",
        ("BTC-USDT", 1000 + event, 0.0001),
    )
    db.execute(
        "INSERT INTO collector_events(ts_ms, event_type, detail) VALUES (?, ?, ?)",
        (event, "test", str(event)),
    )
    db.execute(
        "INSERT INTO collector_sessions(capture_id, started_at_ms, ended_at_ms, symbols_json) VALUES (?, ?, ?, ?)",
        (f"capture-{event}", event, event + 1000, '["BTC-USDT"]'),
    )
    db.commit()
    db.close()


def candle(ts):
    return ("BTC-USDT", ts, ts + 59_999, 100, 101, 99, 100.5, 12, 1)


def test_merge_deduplicates_market_rows_and_appends_diagnostics(tmp_path):
    first, second, output = (tmp_path / name for name in ("one.db", "two.db", "merged.db"))
    make_capture(first, [candle(60_000), candle(120_000)], 1)
    make_capture(second, [candle(120_000), candle(180_000)], 2)

    report = merge_databases([first, second], output)

    assert report["input_count"] == 2
    assert report["rows_after"]["minute_candles"] == 3
    assert report["rows_after"]["minute_features"] == 3
    assert report["rows_after"]["derivatives_snapshots"] == 2
    assert report["rows_after"]["collector_events"] == 2
    assert report["rows_after"]["collector_sessions"] == 2
    assert report["duplicate_key_rows_ignored"]["minute_candles"] == 1

    db = sqlite3.connect(output)
    assert db.execute("SELECT COUNT(*) FROM minute_candles").fetchone()[0] == 3
    assert db.execute("SELECT COUNT(*) FROM collector_events").fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM collector_sessions").fetchone()[0] == 2
    db.close()


def test_merge_rejects_missing_collector_tables(tmp_path):
    incomplete = tmp_path / "incomplete.db"
    db = sqlite3.connect(incomplete)
    db.execute("CREATE TABLE minute_candles (symbol TEXT)")
    db.commit()
    db.close()

    with pytest.raises(ValueError, match="missing collector tables"):
        merge_databases([incomplete], tmp_path / "out.db")


def test_merge_rejects_output_overwriting_input(tmp_path):
    source = tmp_path / "capture.db"
    make_capture(source, [candle(60_000)], 1)

    with pytest.raises(ValueError, match="must not be one of the input"):
        merge_databases([source], source)


def test_merge_accepts_legacy_capture_without_session_table(tmp_path):
    legacy = tmp_path / "legacy.db"
    output = tmp_path / "merged.db"
    db = sqlite3.connect(legacy)
    legacy_schema = SCHEMA.split("CREATE TABLE collector_sessions")[0]
    db.executescript(legacy_schema)
    db.execute(
        "INSERT INTO minute_candles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        candle(60_000),
    )
    db.commit()
    db.close()

    report = merge_databases([legacy], output)
    assert report["rows_after"]["minute_candles"] == 1
    assert report["rows_after"]["collector_sessions"] == 0
