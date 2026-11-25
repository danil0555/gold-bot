import sqlite3
import time

import pytest

from scripts.market_data_collector import MarketMinuteAggregator
from scripts.validate_capture_db import CaptureValidationError, validate_capture_database


def _make_valid_capture(path):
    agg = MarketMinuteAggregator(str(path), symbols=["BTC-USDT"])
    now_ms = int(time.time() * 1000)
    minute = (now_ms // 60_000 - 3) * 60_000
    try:
        agg.process_trade({
            "s": "BTC-USDT", "t": "test-trade", "T": minute + 1_000,
            "p": "100", "q": "2", "m": False,
        })
        agg.process_depth("BTC-USDT", minute + 2_000, {
            "bids": [["99", "2"]], "asks": [["101", "1"]],
        })
        agg.flush_before(minute + 60_000)
        assert agg.save_candle("BTC-USDT", {
            "K": {
                "t": minute, "T": minute + 59_999,
                "o": "100", "h": "103", "l": "99", "c": "102", "v": "12.5",
            }
        })
        agg.save_derivatives(
            "BTC-USDT", now_ms, open_interest=10, funding_rate=0.0001,
            mark_price=100, index_price=100, next_funding_time=now_ms + 3_600_000,
        )
    finally:
        agg.close()


def test_validate_capture_database_accepts_clean_v2_capture(tmp_path):
    path = tmp_path / "capture.sqlite3"
    _make_valid_capture(path)

    report = validate_capture_database(path)

    assert report["status"] == "ok"
    assert report["integrity_check"] == "ok"
    assert report["foreign_key_errors"] == 0
    capture = report["captures"][0]
    assert capture["schema_version"] == 2
    assert capture["row_counts"] == {"features": 1, "candle": 1, "derivatives": 1}


def test_validate_capture_database_rejects_missing_exact_provenance(tmp_path):
    path = tmp_path / "capture.sqlite3"
    _make_valid_capture(path)
    db = sqlite3.connect(path)
    try:
        db.execute("""
            DELETE FROM capture_observations
            WHERE observation_type='features'
        """)
        db.commit()
    finally:
        db.close()

    with pytest.raises(CaptureValidationError, match="missing features provenance"):
        validate_capture_database(path)


def test_validate_capture_database_rejects_unclean_session(tmp_path):
    path = tmp_path / "capture.sqlite3"
    _make_valid_capture(path)
    db = sqlite3.connect(path)
    try:
        db.execute("UPDATE collector_sessions SET ended_at_ms=NULL")
        db.commit()
    finally:
        db.close()

    with pytest.raises(CaptureValidationError, match="no clean end timestamp"):
        validate_capture_database(path)
