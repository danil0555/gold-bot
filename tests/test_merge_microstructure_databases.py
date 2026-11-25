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
CREATE TABLE capture_observations (
 capture_id TEXT NOT NULL, symbol TEXT NOT NULL, observed_at_ms INTEGER NOT NULL,
 observation_type TEXT NOT NULL CHECK (
  observation_type IN ('session', 'candle', 'features', 'derivatives')
 ),
 PRIMARY KEY(capture_id, symbol, observed_at_ms, observation_type)
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
    db.executemany(
        "INSERT INTO capture_observations VALUES (?, ?, ?, ?)",
        [
            (f"capture-{event}", row[0], row[1], "candle")
            for row in candle_rows
        ] + [
            (f"capture-{event}", "BTC-USDT", 1000 + event, "derivatives"),
            (f"capture-{event}", "", event, "session"),
        ],
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


def test_merge_preserves_capture_observation_links(tmp_path):
    first, second, output = (tmp_path / name for name in ("one.db", "two.db", "merged.db"))
    make_capture(first, [candle(60_000)], 1)
    make_capture(second, [candle(60_000)], 2)
    report = merge_databases([first, second], output)
    assert report["rows_after"]["capture_observations"] == 6
    with sqlite3.connect(output) as db:
        sessions = db.execute(
            "SELECT COUNT(DISTINCT capture_id) FROM capture_observations"
        ).fetchone()[0]
        assert sessions == 2



def test_merge_upgrades_older_capture_observation_check_constraint(tmp_path):
    legacy, modern, output = (
        tmp_path / "legacy-mapped.db",
        tmp_path / "modern.db",
        tmp_path / "merged.db",
    )
    old_schema = SCHEMA.replace(
        "observation_type TEXT NOT NULL CHECK (\n  observation_type IN ('session', 'candle', 'features', 'derivatives')\n ),",
        "observation_type TEXT NOT NULL CHECK (\n  observation_type IN ('candle', 'features', 'derivatives')\n ),",
    )
    with sqlite3.connect(legacy) as db:
        db.executescript(old_schema)
        db.execute(
            "INSERT INTO minute_candles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            candle(60_000),
        )
        db.execute(
            "INSERT INTO collector_sessions VALUES (?, ?, ?, ?)",
            ("legacy", 1, 1000, '["BTC-USDT"]'),
        )
        db.execute(
            "INSERT INTO capture_observations VALUES (?, ?, ?, ?)",
            ("legacy", "BTC-USDT", 60_000, "candle"),
        )
    make_capture(modern, [candle(120_000)], 2)

    report = merge_databases([legacy, modern], output)
    assert report["rows_after"]["capture_observations"] == 4
    with sqlite3.connect(output) as db:
        markers = db.execute(
            "SELECT COUNT(*) FROM capture_observations WHERE observation_type='session'"
        ).fetchone()[0]
        assert markers == 1
        db.execute(
            "INSERT INTO capture_observations VALUES (?, ?, ?, ?)",
            ("post-merge", "", 999, "session"),
        )


def test_merge_preserves_capture_scoped_feature_rows(tmp_path):
    from scripts.market_data_collector import MarketMinuteAggregator

    source_a = tmp_path / "capture-a.sqlite3"
    source_b = tmp_path / "capture-b.sqlite3"
    output = tmp_path / "merged.sqlite3"
    aggs = [
        MarketMinuteAggregator(str(source_a), symbols=["BTC-USDT"]),
        MarketMinuteAggregator(str(source_b), symbols=["BTC-USDT"]),
    ]
    try:
        now_ms = int(__import__("time").time() * 1000)
        minute_ts = (now_ms // 60_000 - 1) * 60_000
        for agg, price in zip(aggs, (100.0, 200.0)):
            agg.process_trade({
                "s": "BTC-USDT", "t": f"trade-{price}", "T": minute_ts + 10_000,
                "p": str(price), "q": "1", "m": False,
            })
            agg.flush_before(now_ms)
        ids = {agg.capture_id for agg in aggs}
    finally:
        for agg in aggs:
            agg.close()
    report = merge_databases([source_a, source_b], output)
    with sqlite3.connect(output) as db:
        rows = db.execute(
            "SELECT capture_id,buy_quote_usdt FROM capture_minute_features "
            "WHERE minute_ts=? ORDER BY capture_id", (minute_ts,)
        ).fetchall()
    assert {row[0] for row in rows} == ids
    assert {row[1] for row in rows} == {100.0, 200.0}
    assert report["rows_after"]["capture_minute_features"] == 2


def test_legacy_capture_row_count_treats_missing_optional_table_as_zero(tmp_path):
    from scripts.merge_microstructure_databases import _count_rows

    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE minute_candles (symbol TEXT, minute_ts INTEGER)")
        db.execute("INSERT INTO minute_candles VALUES ('BTC-USDT', 1)")
    assert _count_rows(path, "minute_candles") == 1
    assert _count_rows(path, "capture_minute_features") == 0


def test_merge_legacy_first_then_v2_capture_preserves_scoped_payloads(tmp_path):
    from scripts.market_data_collector import MarketMinuteAggregator

    legacy = tmp_path / "legacy-first.sqlite3"
    modern = tmp_path / "v2-second.sqlite3"
    output = tmp_path / "mixed-merge.sqlite3"
    make_capture(legacy, [candle(60_000)], 1)

    agg = MarketMinuteAggregator(str(modern), symbols=["BTC-USDT"])
    try:
        now_ms = int(__import__("time").time() * 1000)
        minute_ts = (now_ms // 60_000 - 1) * 60_000
        agg.process_trade({
            "s": "BTC-USDT", "t": "v2-trade", "T": minute_ts + 10_000,
            "p": "123", "q": "2", "m": False,
        })
        agg.flush_before(now_ms)
        capture_id = agg.capture_id
    finally:
        agg.close()

    report = merge_databases([legacy, modern], output)
    with sqlite3.connect(output) as db:
        rows = db.execute(
            "SELECT capture_id,buy_quote_usdt FROM capture_minute_features"
        ).fetchall()
        version = db.execute(
            "SELECT schema_version FROM capture_storage_versions WHERE capture_id=?",
            (capture_id,),
        ).fetchone()
    assert rows == [(capture_id, 246.0)]
    assert version == (2,)
    assert report["rows_after"]["capture_minute_features"] == 1
