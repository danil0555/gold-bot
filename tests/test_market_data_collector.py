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
