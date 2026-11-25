import gzip
import json
import sqlite3

from scripts.market_data_collector import (
    MarketMinuteAggregator,
    decode_ws_payload,
    depth_snapshot_features,
)


def test_depth_snapshot_calculates_spread_and_book_imbalance():
    result = depth_snapshot_features({
        "bids": [["99", "10"], ["98", "5"]],
        "asks": [["101", "4"], ["102", "6"]],
    })
    assert result is not None
    assert result["spread_bps"] > 0
    assert result["bid5"] == 99*10 + 98*5
    assert result["ask5"] == 101*4 + 102*6
    assert result["imbalance5"] > 0


def test_trade_aggressor_side_and_depth_are_aggregated_into_minute():
    agg = MarketMinuteAggregator(":memory:")
    try:
        minute = 1_800_000_000_000
        agg.process_trade({"s":"BTC-USDT","t":"trade-1","T":minute+1000,"p":"100","q":"2","m":False})
        agg.process_trade({"s":"BTC-USDT","t":"trade-2","T":minute+2000,"p":"101","q":"1","m":True})
        agg.process_depth("BTC-USDT", minute+3000, {
            "bids":[["99","2"]], "asks":[["101","1"]]
        })
        agg.flush_before(minute+60_000)
        row = agg.db.execute(
            "SELECT trades_count,buy_trades,sell_trades,buy_quote_usdt,sell_quote_usdt,cvd_quote_usdt,depth_samples FROM minute_features"
        ).fetchone()
        assert row == (2, 1, 1, 200.0, 101.0, 99.0, 1)
    finally:
        agg.close()


def test_websocket_payload_decoder_supports_gzip_and_plain_text():
    payload = {"dataType":"BTC-USDT@trade","data":[]}
    packed = gzip.compress(json.dumps(payload).encode())
    assert decode_ws_payload(packed) == payload
    assert decode_ws_payload(json.dumps(payload)) == payload
    assert decode_ws_payload("Ping") == "Ping"



def test_minute_candle_updates_are_persisted_and_closed_flag_is_time_based():
    agg = MarketMinuteAggregator(":memory:")
    try:
        now_ms = int(__import__("time").time() * 1000)
        candle = {
            "K": {
                "t": now_ms - 60_000,
                "T": now_ms + 1_000,
                "o": "100", "h": "103", "l": "99", "c": "102", "v": "12.5",
            }
        }
        # Active bars are accepted but not persisted until their close time passes.
        assert agg.save_candle("BTC-USDT", candle) is True
        assert agg.db.execute("SELECT count(*) FROM minute_candles").fetchone()[0] == 0

        candle["K"]["T"] = now_ms - 3_000
        assert agg.save_candle("BTC-USDT", candle) is True
        row = agg.db.execute(
            "SELECT is_closed FROM minute_candles WHERE symbol='BTC-USDT'"
        ).fetchone()
        assert row == (1,)
    finally:
        agg.close()


def test_minute_candle_rejects_invalid_ohlcv():
    agg = MarketMinuteAggregator(":memory:")
    try:
        assert agg.save_candle("BTC-USDT", {"K": {
            "t": 1_800_000_000_000, "T": 1_800_000_059_999,
            "o": "100", "h": "98", "l": "99", "c": "101", "v": "1",
        }}) is False
        assert agg.db.execute("SELECT count(*) FROM minute_candles").fetchone()[0] == 0
    finally:
        agg.close()



def test_kline_message_matches_bingx_swap_websocket_documented_shape():
    agg = MarketMinuteAggregator(":memory:")
    try:
        payload = {
            "e": "kline", "E": int(__import__("time").time() * 1000), "s": "BTC-USDT",
            "K": {
                "t": (int(__import__("time").time() * 1000) // 60_000 - 1) * 60_000,
                "T": (int(__import__("time").time() * 1000) // 60_000) * 60_000 - 1,
                "o": "100", "h": "103", "l": "99", "c": "102", "v": "12.5",
            },
        }
        assert agg.save_candle("BTC-USDT", payload) is True
        row = agg.db.execute(
            "SELECT open,high,low,close,volume FROM minute_candles"
        ).fetchone()
        assert row == (100.0, 103.0, 99.0, 102.0, 12.5)
    finally:
        agg.close()


def test_kline_without_open_timestamp_uses_minute_bucket_of_close_timestamp():
    agg = MarketMinuteAggregator(":memory:")
    try:
        now_ms = int(__import__("time").time() * 1000)
        close_ts = (now_ms // 60_000 - 1) * 60_000 - 1
        payload = {
            "T": close_ts,
            "o": "100", "h": "103", "l": "99", "c": "102", "v": "12.5",
        }
        assert agg.save_candle("BTC-USDT", payload) is True
        row = agg.db.execute(
            "SELECT minute_ts,close_ts,is_closed FROM minute_candles"
        ).fetchone()
        assert row == (close_ts // 60_000 * 60_000, close_ts, 1)
    finally:
        agg.close()



def test_live_swap_kline_without_t_uses_previous_minute_for_boundary_T():
    agg = MarketMinuteAggregator(":memory:")
    try:
        now_ms = int(__import__("time").time() * 1000)
        close_boundary = (now_ms // 60_000 - 2) * 60_000
        # Observed live payload shape: OHLCV and T at top level, no K.t.
        payload = {
            "T": close_boundary,
            "o": "100", "h": "103", "l": "99", "c": "102", "v": "12.5",
        }
        assert agg.save_candle("BTC-USDT", payload) is True
        row = agg.db.execute(
            "SELECT minute_ts,close_ts,is_closed FROM minute_candles"
        ).fetchone()
        assert row == (close_boundary - 60_000, close_boundary, 1)
    finally:
        agg.close()


def test_flush_all_persists_elapsed_minutes_but_discards_active_partial_minute(monkeypatch):
    import time as time_module

    agg = MarketMinuteAggregator(":memory:")
    try:
        current_minute = int(time_module.time() * 1000 // 60_000 * 60_000)
        monkeypatch.setattr("scripts.market_data_collector.time.time", lambda: (current_minute + 30_000) / 1000)
        agg.process_trade({
            "s": "BTC-USDT", "t": "completed", "T": current_minute - 10_000,
            "p": "100", "q": "1", "m": False,
        })
        agg.process_trade({
            "s": "BTC-USDT", "t": "partial", "T": current_minute + 10_000,
            "p": "101", "q": "1", "m": False,
        })
        agg.flush_all()
        rows = agg.db.execute(
            "SELECT minute_ts,trades_count FROM minute_features ORDER BY minute_ts"
        ).fetchall()
        assert rows == [(current_minute - 60_000, 1)]
    finally:
        agg.close()


def test_collector_records_session_start_and_stop(tmp_path):
    path = tmp_path / "capture.sqlite3"
    agg = MarketMinuteAggregator(str(path), symbols=["BTC-USDT", "ETH-USDT"])
    capture_id = agg.capture_id
    with sqlite3.connect(path) as db:
        row = db.execute(
            "SELECT started_at_ms, ended_at_ms, symbols_json FROM collector_sessions WHERE capture_id=?",
            (capture_id,),
        ).fetchone()
        assert row[0] > 0
        assert row[1] is None
        assert json.loads(row[2]) == ["BTC-USDT", "ETH-USDT"]
    agg.close()
    with sqlite3.connect(path) as db:
        ended = db.execute(
            "SELECT ended_at_ms FROM collector_sessions WHERE capture_id=?",
            (capture_id,),
        ).fetchone()[0]
        events = db.execute(
            "SELECT event_type FROM collector_events WHERE detail=? ORDER BY ts_ms",
            (capture_id,),
        ).fetchall()
    assert ended is not None and ended >= row[0]
    assert [item[0] for item in events] == [
        "capture_session_started", "capture_session_stopped"
    ]
