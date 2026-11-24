import sqlite3

from scripts.microstructure_data_quality import audit_database


def make_db(path, candle_stamps=(60_000, 120_000, 180_000), depth_samples=4):
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE minute_candles (
                symbol TEXT, minute_ts INTEGER, is_closed INTEGER
            );
            CREATE TABLE minute_features (
                symbol TEXT, minute_ts INTEGER, trades_count INTEGER,
                depth_samples INTEGER, buy_quote_usdt REAL, sell_quote_usdt REAL
            );
            CREATE TABLE derivatives_snapshots (
                symbol TEXT, ts_ms INTEGER, open_interest_base REAL, funding_rate REAL
            );
            CREATE TABLE collector_events (
                id INTEGER PRIMARY KEY, ts_ms INTEGER, event_type TEXT, detail TEXT
            );
        """)
        db.executemany(
            "INSERT INTO minute_candles VALUES ('BTC-USDT', ?, 1)",
            [(stamp,) for stamp in candle_stamps],
        )
        db.executemany(
            "INSERT INTO minute_features VALUES ('BTC-USDT', ?, 10, ?, 500, 400)",
            [(stamp, depth_samples) for stamp in candle_stamps],
        )
        db.executemany(
            "INSERT INTO derivatives_snapshots VALUES ('BTC-USDT', ?, 1000, 0.0001)",
            [(stamp,) for stamp in (60_000, 90_000, 120_000)],
        )


def test_quality_audit_reports_complete_sample_without_warnings(tmp_path):
    db_path = tmp_path / "quality.sqlite3"
    make_db(db_path)
    report = audit_database(str(db_path))

    assert report["overall_status"] == "no_warnings_detected"
    assert report["totals"]["closed_candles"] == 3
    assert report["totals"]["depth_samples"] == 12
    assert report["totals"]["trades"] == 30
    assert report["per_symbol"]["BTC-USDT"]["depth_minute_coverage_pct"] == 100.0
    assert report["warnings"] == []


def test_quality_audit_flags_missing_candles_and_transport_errors(tmp_path):
    db_path = tmp_path / "quality_gaps.sqlite3"
    make_db(db_path, candle_stamps=(60_000, 180_000), depth_samples=0)
    with sqlite3.connect(db_path) as db:
        db.execute(
            "INSERT INTO collector_events (ts_ms, event_type, detail) VALUES (?,?,?)",
            (180_000, "ws_reconnect", "synthetic test"),
        )
    report = audit_database(str(db_path))

    codes = {warning["code"] for warning in report["warnings"]}
    assert report["overall_status"] == "warning"
    assert "candle_gaps_detected" in codes
    assert "no_depth_minutes" in codes
    assert "ws_reconnect" in codes


def test_quality_audit_accepts_single_initial_warmup_candle_without_features(tmp_path):
    db_path = tmp_path / "quality_warmup.sqlite3"
    make_db(db_path, candle_stamps=(60_000, 120_000, 180_000))
    with sqlite3.connect(db_path) as db:
        db.execute("DELETE FROM minute_features WHERE minute_ts = 60_000")
    report = audit_database(str(db_path))

    metrics = report["per_symbol"]["BTC-USDT"]
    assert metrics["initial_boundary_candles_without_features"] == 1
    assert metrics["candle_minutes_without_features"] == 0
    assert "candle_minutes_missing_features" not in {
        warning["code"] for warning in report["warnings"]
    }
