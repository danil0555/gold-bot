#!/usr/bin/env python3
"""Read-only BingX USDT-M live market-data recorder.

Stores minute aggregates in a separate research SQLite database. It never
loads the production bot database, requests credentials, or places orders.
"""
from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import logging
import os
import sqlite3
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone

import aiohttp

BASE = "https://open-api.bingx.com"
WS_URL = "wss://open-api-swap.bingx.com/swap-market"
DEPTH_LEVEL = 20
DEPTH_INTERVAL = "500ms"
REST_PAUSE = 1.05
LOG = logging.getLogger("market_data_collector")


def decode_ws_payload(payload):
    if isinstance(payload, str):
        text = payload
    else:
        raw = bytes(payload)
        if raw.startswith(b"\x1f\x8b"):
            raw = gzip.decompress(raw)
        text = raw.decode("utf-8")
    if text.strip() in ("Ping", "Pong", ""):
        return text.strip()
    return json.loads(text)


def _levels(levels):
    result = []
    for item in levels or []:
        try:
            price, qty = float(item[0]), float(item[1])
            if price > 0 and qty > 0:
                result.append((price, qty))
        except (TypeError, ValueError, IndexError):
            continue
    return result


def depth_snapshot_features(data):
    bids = _levels(data.get("bids"))
    asks = _levels(data.get("asks"))
    if not bids or not asks:
        return None
    best_bid, best_ask = bids[0][0], asks[0][0]
    if best_bid <= 0 or best_ask <= best_bid:
        return None
    mid = (best_bid + best_ask) / 2.0
    spread_bps = (best_ask-best_bid) / mid * 10_000.0
    bid5 = sum(p*q for p, q in bids[:5])
    ask5 = sum(p*q for p, q in asks[:5])
    bid20 = sum(p*q for p, q in bids[:20])
    ask20 = sum(p*q for p, q in asks[:20])
    imbalance5 = (bid5-ask5)/(bid5+ask5) if bid5+ask5 else 0.0
    imbalance20 = (bid20-ask20)/(bid20+ask20) if bid20+ask20 else 0.0
    bid_qty, ask_qty = bids[0][1], asks[0][1]
    microprice = ((best_ask*bid_qty)+(best_bid*ask_qty))/(bid_qty+ask_qty) if bid_qty+ask_qty else mid
    return {
        "mid": mid, "spread_bps": spread_bps,
        "bid5": bid5, "ask5": ask5, "bid20": bid20, "ask20": ask20,
        "imbalance5": imbalance5, "imbalance20": imbalance20,
        "microprice_offset_bps": (microprice-mid)/mid*10_000.0,
    }


class MarketMinuteAggregator:
    def __init__(self, db_path):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.db = sqlite3.connect(db_path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS minute_features (
            symbol TEXT NOT NULL,
            minute_ts INTEGER NOT NULL,
            trades_count INTEGER NOT NULL DEFAULT 0,
            buy_trades INTEGER NOT NULL DEFAULT 0,
            sell_trades INTEGER NOT NULL DEFAULT 0,
            buy_quote_usdt REAL NOT NULL DEFAULT 0,
            sell_quote_usdt REAL NOT NULL DEFAULT 0,
            cvd_quote_usdt REAL NOT NULL DEFAULT 0,
            taker_imbalance REAL,
            avg_spread_bps REAL,
            min_spread_bps REAL,
            max_spread_bps REAL,
            avg_bid_depth5_usdt REAL,
            avg_ask_depth5_usdt REAL,
            avg_bid_depth20_usdt REAL,
            avg_ask_depth20_usdt REAL,
            avg_book_imbalance5 REAL,
            avg_book_imbalance20 REAL,
            avg_microprice_offset_bps REAL,
            depth_samples INTEGER NOT NULL DEFAULT 0,
            last_mid REAL,
            PRIMARY KEY(symbol, minute_ts)
        );
        CREATE TABLE IF NOT EXISTS derivatives_snapshots (
            symbol TEXT NOT NULL,
            ts_ms INTEGER NOT NULL,
            open_interest_base REAL,
            funding_rate REAL,
            mark_price REAL,
            index_price REAL,
            next_funding_time INTEGER,
            PRIMARY KEY(symbol, ts_ms)
        );
        CREATE TABLE IF NOT EXISTS collector_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_ms INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            detail TEXT
        );
        """)
        self.db.commit()
        self.pending = {}

    def _window(self, symbol, timestamp_ms):
        minute_ts = int(timestamp_ms // 60_000 * 60_000)
        key = (symbol, minute_ts)
        if key not in self.pending:
            self.pending[key] = {
                "trades_count": 0, "buy_trades": 0, "sell_trades": 0,
                "buy_quote": 0.0, "sell_quote": 0.0,
                "spreads": [], "bid5": [], "ask5": [], "bid20": [], "ask20": [],
                "imb5": [], "imb20": [], "micro": [], "mids": [],
            }
        return minute_ts, self.pending[key]

    def process_trade(self, trade):
        try:
            symbol = str(trade["s"]).upper()
            timestamp = int(trade["T"])
            price, qty = float(trade["p"]), float(trade["q"])
            if price <= 0 or qty <= 0 or timestamp <= 0:
                return
            _, window = self._window(symbol, timestamp)
            quote = price*qty
            window["trades_count"] += 1
            # BingX m=true means buyer is maker, so seller was the taker.
            if bool(trade.get("m", False)):
                window["sell_trades"] += 1
                window["sell_quote"] += quote
            else:
                window["buy_trades"] += 1
                window["buy_quote"] += quote
        except (KeyError, TypeError, ValueError, OverflowError):
            return

    def process_depth(self, symbol, timestamp_ms, data):
        try:
            features = depth_snapshot_features(data)
            if not features:
                return
            _, window = self._window(str(symbol).upper(), int(timestamp_ms))
            for source, target in (
                ("spread_bps", "spreads"), ("bid5", "bid5"), ("ask5", "ask5"),
                ("bid20", "bid20"), ("ask20", "ask20"),
                ("imbalance5", "imb5"), ("imbalance20", "imb20"),
                ("microprice_offset_bps", "micro"), ("mid", "mids"),
            ):
                window[target].append(features[source])
        except (TypeError, ValueError, OverflowError):
            return

    def _save(self, symbol, minute_ts, w):
        buy, sell = w["buy_quote"], w["sell_quote"]
        total = buy+sell
        spreads = w["spreads"]
        def avg(key):
            values = w[key]
            return sum(values)/len(values) if values else None
        row = (
            symbol, minute_ts, w["trades_count"], w["buy_trades"], w["sell_trades"],
            buy, sell, buy-sell, (buy-sell)/total if total else None,
            avg("spreads"), min(spreads) if spreads else None, max(spreads) if spreads else None,
            avg("bid5"), avg("ask5"), avg("bid20"), avg("ask20"),
            avg("imb5"), avg("imb20"), avg("micro"), len(spreads), w["mids"][-1] if w["mids"] else None,
        )
        self.db.execute("""
            INSERT OR REPLACE INTO minute_features VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
        """, row)

    def flush_before(self, timestamp_ms):
        cutoff = int(timestamp_ms // 60_000 * 60_000)
        ready = [key for key in self.pending if key[1] < cutoff]
        for symbol, minute_ts in sorted(ready, key=lambda item: item[1]):
            self._save(symbol, minute_ts, self.pending.pop((symbol, minute_ts)))
        if ready:
            self.db.commit()

    def flush_all(self):
        for (symbol, minute_ts), window in sorted(self.pending.items(), key=lambda item: item[0][1]):
            self._save(symbol, minute_ts, window)
        self.pending.clear()
        self.db.commit()

    def save_derivatives(self, symbol, ts_ms, open_interest=None, funding_rate=None,
                         mark_price=None, index_price=None, next_funding_time=None):
        self.db.execute("""
            INSERT OR REPLACE INTO derivatives_snapshots
            (symbol,ts_ms,open_interest_base,funding_rate,mark_price,index_price,next_funding_time)
            VALUES (?,?,?,?,?,?,?)
        """, (symbol, int(ts_ms), open_interest, funding_rate, mark_price, index_price, next_funding_time))
        self.db.commit()

    def event(self, event_type, detail=""):
        self.db.execute(
            "INSERT INTO collector_events(ts_ms,event_type,detail) VALUES(?,?,?)",
            (int(time.time()*1000), event_type, str(detail)[:1000])
        )
        self.db.commit()

    def close(self):
        self.flush_all()
        self.db.close()


async def _rest_json(session, path, params=None):
    url = BASE + path
    async with session.get(url, params=params or {}, timeout=aiohttp.ClientTimeout(total=20)) as response:
        response.raise_for_status()
        payload = await response.json(content_type=None)
    code = payload.get("code", 0)
    if code not in (0, "0", None):
        raise RuntimeError(f"BingX REST error {code}: {payload.get('msg')}")
    return payload.get("data")


async def derivatives_poll(session, aggregator, symbols, interval_seconds):
    while True:
        started = time.monotonic()
        stamp = int(time.time()*1000)
        try:
            premium = await _rest_json(session, "/openApi/swap/v2/quote/premiumIndex")
            premium_rows = premium if isinstance(premium, list) else [premium] if isinstance(premium, dict) else []
            by_symbol = {str(x.get("symbol", "")).upper(): x for x in premium_rows if isinstance(x, dict)}
            for symbol in symbols:
                try:
                    oi_data = await _rest_json(
                        session, "/openApi/swap/v2/quote/openInterest", {"symbol": symbol}
                    )
                    oi_rows = oi_data if isinstance(oi_data, list) else [oi_data] if isinstance(oi_data, dict) else []
                    oi = float(oi_rows[0].get("openInterest")) if oi_rows and oi_rows[0].get("openInterest") is not None else None
                    p = by_symbol.get(symbol, {})
                    aggregator.save_derivatives(
                        symbol, stamp, oi,
                        float(p["lastFundingRate"]) if p.get("lastFundingRate") is not None else None,
                        float(p["markPrice"]) if p.get("markPrice") is not None else None,
                        float(p["indexPrice"]) if p.get("indexPrice") is not None else None,
                        int(p["nextFundingTime"]) if p.get("nextFundingTime") is not None else None,
                    )
                except Exception as exc:
                    LOG.warning("OI poll failed for %s: %s", symbol, exc)
                await asyncio.sleep(REST_PAUSE)
        except Exception as exc:
            LOG.warning("Funding/OI poll cycle failed: %s", exc)
            aggregator.event("derivatives_poll_error", exc)
        elapsed = time.monotonic()-started
        await asyncio.sleep(max(1.0, interval_seconds-elapsed))


async def websocket_loop(session, aggregator, symbols):
    channels = []
    for symbol in symbols:
        channels.append(f"{symbol}@depth{DEPTH_LEVEL}@{DEPTH_INTERVAL}")
        channels.append(f"{symbol}@trade")
    backoff = 1
    while True:
        try:
            async with session.ws_connect(
                WS_URL, autoping=False, heartbeat=30, receive_timeout=90,
                max_msg_size=4*1024*1024
            ) as ws:
                aggregator.event("ws_connected", f"symbols={len(symbols)} channels={len(channels)}")
                for channel in channels:
                    await ws.send_json({
                        "id": str(uuid.uuid4()), "reqType": "sub", "dataType": channel
                    })
                    await asyncio.sleep(0.03)
                backoff = 1
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.BINARY:
                        decoded = decode_ws_payload(message.data)
                    elif message.type == aiohttp.WSMsgType.TEXT:
                        decoded = decode_ws_payload(message.data)
                    elif message.type == aiohttp.WSMsgType.PING:
                        await ws.pong(message.data)
                        continue
                    elif message.type == aiohttp.WSMsgType.PONG:
                        continue
                    elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                        raise ConnectionError("BingX websocket closed")
                    elif message.type == aiohttp.WSMsgType.ERROR:
                        raise ConnectionError(f"BingX websocket error: {ws.exception()}")
                    else:
                        continue
                    if decoded == "Ping":
                        await ws.send_str("Pong")
                        continue
                    if not isinstance(decoded, dict):
                        continue
                    data_type = str(decoded.get("dataType", ""))
                    data = decoded.get("data")
                    if not data:
                        continue
                    now_ms = int(time.time()*1000)
                    if data_type.endswith("@trade") and isinstance(data, list):
                        for trade in data:
                            aggregator.process_trade(trade)
                            try:
                                aggregator.flush_before(int(trade.get("T", now_ms)))
                            except (TypeError, ValueError):
                                pass
                    elif "@depth" in data_type and isinstance(data, dict):
                        symbol = data_type.split("@", 1)[0]
                        event_ts = int(decoded.get("ts") or now_ms)
                        aggregator.process_depth(symbol, event_ts, data)
                        aggregator.flush_before(event_ts)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("Websocket reconnect in %ss after error: %s", backoff, exc)
            aggregator.event("ws_reconnect", f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(backoff)
            backoff = min(backoff*2, 60)


async def run_collector(symbols, db_path, duration_hours, derivatives_interval):
    aggregator = MarketMinuteAggregator(db_path)
    timeout = aiohttp.ClientTimeout(total=25)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            tasks = [
                asyncio.create_task(websocket_loop(session, aggregator, symbols)),
                asyncio.create_task(derivatives_poll(session, aggregator, symbols, derivatives_interval)),
            ]
            try:
                if duration_hours > 0:
                    await asyncio.sleep(duration_hours*3600)
                else:
                    await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        aggregator.close()


def parse_symbols(raw):
    return list(dict.fromkeys(
        s.strip().upper() for s in (raw or "").split(",")
        if s.strip() and s.strip().upper().endswith("-USDT")
    ))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="")
    parser.add_argument("--universe", type=int, default=20)
    parser.add_argument("--min-quote-volume", type=float, default=5_000_000)
    parser.add_argument("--db-path", default="research-data/market_microstructure.sqlite3")
    parser.add_argument("--duration-hours", type=float, default=0.0,
                        help="0 means run until stopped")
    parser.add_argument("--derivatives-interval", type=int, default=180)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    symbols = parse_symbols(args.symbols)
    if not symbols:
        from scripts.market_research import discover_universe
        symbols = [item["symbol"] for item in discover_universe(
            limit=args.universe, min_quote_volume=args.min_quote_volume
        )]
    if not symbols:
        raise SystemExit("No eligible USDT-M symbols discovered")
    LOG.info("Starting read-only collector for %d symbols: %s", len(symbols), ",".join(symbols))
    LOG.info("Research database: %s (separate from production trading_bot_v9.db)", args.db_path)
    asyncio.run(run_collector(symbols, args.db_path, args.duration_hours, args.derivatives_interval))


if __name__ == "__main__":
    main()
