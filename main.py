#!/usr/bin/env python3
# Trading Bot v8.0 — BingX Spot + USDT-M Perpetual Futures
# Telegram alerts only. This bot does NOT place orders and needs no BingX API keys.

import asyncio
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import aiohttp
import numpy as np
import pandas as pd
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv()

# ---------------------------- CONFIGURATION ----------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
# Optional: comma-separated Telegram user IDs. Leave empty to allow anyone to use the bot.
ALLOWED_USER_IDS = {
    int(x.strip()) for x in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
}

# Symbols must be listed on BingX in BASE-USDT format.
SYMBOLS = [
    x.strip().upper() for x in
    os.getenv("SYMBOLS", "BTC-USDT,ETH-USDT,SOL-USDT,XRP-USDT").split(",")
    if x.strip()
]
MARKETS = [
    x.strip().lower() for x in
    os.getenv("MARKETS", "spot,swap").split(",")
    if x.strip().lower() in {"spot", "swap"}
]

SCAN_INTERVAL_SECONDS = int(os.getenv("SCAN_INTERVAL_SECONDS", "60"))
MIN_SIGNAL_SCORE = float(os.getenv("MIN_SIGNAL_SCORE", "5.0"))
MIN_RR = float(os.getenv("MIN_RR", "1.8"))
ATR_SL_MULTIPLIER = float(os.getenv("ATR_SL_MULTIPLIER", "1.5"))
ATR_TP_MULTIPLIER = float(os.getenv("ATR_TP_MULTIPLIER", "2.7"))
DATABASE_PATH = os.getenv("DATABASE_PATH", "trading_bot_v8.db")
HTTP_TIMEOUT_SECONDS = 15
CANDLE_LIMIT = 250

BINGX_BASE_URL = "https://open-api.bingx.com"
INTERVALS = {"5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h"}
TIMEFRAME_WEIGHT = {"5m": 1.0, "15m": 1.5, "1h": 2.0, "4h": 2.0}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("bingx-v8")

# ---------------------------- DATABASE ----------------------------

def db_connect():
    conn = sqlite3.connect(DATABASE_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db_connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry REAL NOT NULL,
                stop_loss REAL NOT NULL,
                take_profit REAL NOT NULL,
                score REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                result TEXT,
                closed_at TEXT,
                UNIQUE(market, symbol, side, created_at)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.commit()


def save_signal(signal: dict[str, Any]) -> int | None:
    with db_connect() as conn:
        # Avoid repeated alerts for the same market/symbol/direction within 45 minutes.
        recent = conn.execute("""
            SELECT id FROM signals
            WHERE market=? AND symbol=? AND side=? AND status='OPEN'
              AND created_at >= ?
            ORDER BY id DESC LIMIT 1
        """, (
            signal["market"], signal["symbol"], signal["side"],
            datetime.fromtimestamp(
                datetime.now(timezone.utc).timestamp() - 2700,
                tz=timezone.utc
            ).isoformat()
        )).fetchone()
        if recent:
            return None

        cur = conn.execute("""
            INSERT INTO signals
            (created_at, market, symbol, side, entry, stop_loss, take_profit, score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            signal["market"], signal["symbol"], signal["side"],
            signal["entry"], signal["stop_loss"], signal["take_profit"],
            signal["score"]
        ))
        conn.commit()
        return int(cur.lastrowid)


def close_signal(signal_id: int, result: str) -> bool:
    with db_connect() as conn:
        cur = conn.execute("""
            UPDATE signals SET status='CLOSED', result=?, closed_at=?
            WHERE id=? AND status='OPEN'
        """, (result.upper(), datetime.now(timezone.utc).isoformat(), signal_id))
        conn.commit()
        return cur.rowcount > 0


def get_stats() -> dict[str, Any]:
    with db_connect() as conn:
        total = conn.execute(
            "SELECT COUNT(*) n FROM signals WHERE status='CLOSED'"
        ).fetchone()["n"]
        wins = conn.execute(
            "SELECT COUNT(*) n FROM signals WHERE result='WIN'"
        ).fetchone()["n"]
        losses = conn.execute(
            "SELECT COUNT(*) n FROM signals WHERE result='LOSS'"
        ).fetchone()["n"]
        opened = conn.execute(
            "SELECT COUNT(*) n FROM signals WHERE status='OPEN'"
        ).fetchone()["n"]
        rows = conn.execute("""
            SELECT market, COUNT(*) total,
                   SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) wins,
                   SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) losses
            FROM signals WHERE status='CLOSED' GROUP BY market
        """).fetchall()
    return {
        "total": total, "wins": wins, "losses": losses, "open": opened,
        "winrate": (wins / total * 100) if total else None,
        "by_market": [dict(r) for r in rows]
    }


# ---------------------------- BINGX DATA ----------------------------

class BingXClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def get_json(self, path: str, params: dict[str, Any]) -> Any:
        url = BINGX_BASE_URL + path
        async with self.session.get(url, params=params) as response:
            body = await response.text()
            if response.status != 200:
                raise RuntimeError(f"BingX HTTP {response.status}: {body[:250]}")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"Invalid JSON from BingX: {body[:200]}") from exc

            if isinstance(payload, dict) and payload.get("code") not in (None, 0, "0"):
                raise RuntimeError(
                    f"BingX API error {payload.get('code')}: {payload.get('msg', '')}"
                )
            return payload.get("data", payload) if isinstance(payload, dict) else payload

    async def klines(self, market: str, symbol: str, interval: str) -> pd.DataFrame:
        if market == "spot":
            path = "/openApi/spot/v2/market/kline"
        else:
            path = "/openApi/swap/v3/quote/klines"

        data = await self.get_json(path, {
            "symbol": symbol,
            "interval": INTERVALS[interval],
            "limit": CANDLE_LIMIT
        })

        # BingX commonly returns list rows:
        # [openTime, open, high, low, close, volume, closeTime, quoteVolume]
        if isinstance(data, dict):
            for key in ("data", "list", "klines", "rows"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break

        if not isinstance(data, list) or len(data) < 60:
            raise RuntimeError(f"Not enough {market} {symbol} {interval} candles")

        records = []
        for row in data:
            if isinstance(row, (list, tuple)) and len(row) >= 6:
                records.append({
                    "open_time": row[0], "open": row[1], "high": row[2],
                    "low": row[3], "close": row[4], "volume": row[5],
                    "close_time": row[6] if len(row) > 6 else row[0]
                })
            elif isinstance(row, dict):
                records.append({
                    "open_time": row.get("time", row.get("openTime", row.get("open_time"))),
                    "open": row.get("open"), "high": row.get("high"),
                    "low": row.get("low"), "close": row.get("close"),
                    "volume": row.get("volume", row.get("vol")),
                    "close_time": row.get("closeTime", row.get("close_time", row.get("time")))
                })

        df = pd.DataFrame(records)
        required = ["open_time", "open", "high", "low", "close", "volume", "close_time"]
        if df.empty or any(c not in df.columns for c in required):
            raise RuntimeError("Unexpected BingX kline schema")

        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["open_time"] = pd.to_numeric(df["open_time"], errors="coerce")
        df["close_time"] = pd.to_numeric(df["close_time"], errors="coerce")
        df = df.dropna(subset=required).sort_values("open_time")
        df = df.drop_duplicates(subset=["open_time"]).reset_index(drop=True)

        # Use only fully closed candles. If the API returns seconds, convert to ms.
        now_ms = int(time.time() * 1000)
        if df["close_time"].median() < 10_000_000_000:
            df["close_time"] *= 1000
        df = df[df["close_time"] < now_ms - 1000].copy()

        if len(df) < 60:
            raise RuntimeError("Not enough fully closed candles after validation")
        if (df["high"] < df["low"]).any() or (df["volume"] < 0).any():
            raise RuntimeError("Invalid OHLCV data")
        return df.tail(CANDLE_LIMIT).reset_index(drop=True)


# ---------------------------- INDICATORS ----------------------------

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    close, high, low = x["close"], x["high"], x["low"]

    # Wilder RSI
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False, min_periods=14).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    x["rsi"] = 100 - (100 / (1 + rs))
    x.loc[(avg_loss == 0) & (avg_gain > 0), "rsi"] = 100
    x.loc[(avg_loss == 0) & (avg_gain == 0), "rsi"] = 50

    x["ema20"] = close.ewm(span=20, adjust=False).mean()
    x["ema50"] = close.ewm(span=50, adjust=False).mean()
    x["ema200"] = close.ewm(span=200, adjust=False).mean()

    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    x["macd"] = ema12 - ema26
    x["macd_signal"] = x["macd"].ewm(span=9, adjust=False).mean()
    x["macd_hist"] = x["macd"] - x["macd_signal"]

    prev_close = close.shift(1)
    tr = pd.concat([
        high - low, (high - prev_close).abs(), (low - prev_close).abs()
    ], axis=1).max(axis=1)
    x["atr"] = tr.ewm(alpha=1/14, adjust=False, min_periods=14).mean()

    up = high.diff()
    down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    atr_safe = x["atr"].replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=1/14, adjust=False).mean() / atr_safe
    minus_di = 100 * minus_dm.ewm(alpha=1/14, adjust=False).mean() / atr_safe
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    x["adx"] = dx.ewm(alpha=1/14, adjust=False).mean()
    x["plus_di"] = plus_di
    x["minus_di"] = minus_di

    x["vol_sma20"] = x["volume"].rolling(20).mean()
    return x


@dataclass
class TFAnalysis:
    direction: int  # +1 bullish, -1 bearish, 0 neutral
    score: float
    close: float
    atr: float
    rsi: float
    adx: float
    reasons: list[str]


def analyze_timeframe(raw: pd.DataFrame) -> TFAnalysis:
    df = add_indicators(raw)
    row = df.iloc[-1]
    prev = df.iloc[-2]
    close = float(row["close"])
    atr = float(row["atr"])
    rsi = float(row["rsi"])
    adx = float(row["adx"]) if pd.notna(row["adx"]) else 0.0

    if not all(np.isfinite(v) for v in [close, atr, rsi]) or atr <= 0:
        raise RuntimeError("Indicators are not ready")

    score = 0.0
    reasons = []

    if row["ema20"] > row["ema50"] > row["ema200"]:
        score += 2.0
        reasons.append("EMA20 > EMA50 > EMA200")
    elif row["ema20"] < row["ema50"] < row["ema200"]:
        score -= 2.0
        reasons.append("EMA20 < EMA50 < EMA200")

    if row["macd"] > row["macd_signal"] and row["macd_hist"] > prev["macd_hist"]:
        score += 1.25
        reasons.append("MACD bullish momentum")
    elif row["macd"] < row["macd_signal"] and row["macd_hist"] < prev["macd_hist"]:
        score -= 1.25
        reasons.append("MACD bearish momentum")

    if 50 < rsi < 70:
        score += 0.75
        reasons.append(f"RSI bullish zone ({rsi:.1f})")
    elif 30 < rsi < 50:
        score -= 0.75
        reasons.append(f"RSI bearish zone ({rsi:.1f})")
    elif rsi >= 75:
        score -= 0.5
        reasons.append(f"RSI overbought caution ({rsi:.1f})")
    elif rsi <= 25:
        score += 0.5
        reasons.append(f"RSI oversold caution ({rsi:.1f})")

    if adx >= 20:
        if row["plus_di"] > row["minus_di"]:
            score += 0.75
            reasons.append(f"ADX trend bullish ({adx:.1f})")
        elif row["minus_di"] > row["plus_di"]:
            score -= 0.75
            reasons.append(f"ADX trend bearish ({adx:.1f})")

    # A close beyond the previous 20-candle range is a simple breakout confirmation.
    prior_high = df["high"].iloc[-21:-1].max()
    prior_low = df["low"].iloc[-21:-1].min()
    if close > prior_high:
        score += 1.0
        reasons.append("20-candle high breakout")
    elif close < prior_low:
        score -= 1.0
        reasons.append("20-candle low breakdown")

    direction = 1 if score >= 1.5 else (-1 if score <= -1.5 else 0)
    return TFAnalysis(direction, score, close, atr, rsi, adx, reasons)


# ---------------------------- SIGNAL ENGINE ----------------------------

async def build_signal(client: BingXClient, market: str, symbol: str):
    analyses: dict[str, TFAnalysis] = {}
    for tf in ("5m", "15m", "1h", "4h"):
        raw = await client.klines(market, symbol, tf)
        analyses[tf] = analyze_timeframe(raw)
        await asyncio.sleep(0.08)

    # The 15m timeframe is the setup; H1/H4 provide trend context.
    setup = analyses["15m"]
    h1, h4, m5 = analyses["1h"], analyses["4h"], analyses["5m"]
    direction = setup.direction

    if direction == 0:
        return None, "15m setup is neutral"

    # Avoid entering against both higher timeframes.
    if h1.direction == -direction and h4.direction == -direction:
        return None, "H1 and H4 oppose the setup"

    # Require at least one higher timeframe to agree and M5 not to strongly oppose.
    if h1.direction != direction and h4.direction != direction:
        return None, "No H1/H4 confirmation"
    if m5.direction == -direction and abs(m5.score) >= 3:
        return None, "M5 momentum strongly opposes the setup"

    weighted = (
        setup.score * 1.5 +
        h1.score * 1.5 +
        h4.score * 1.25 +
        m5.score * 0.75
    )
    signed_score = weighted if direction > 0 else -weighted
    if signed_score < MIN_SIGNAL_SCORE:
        return None, f"Score too low ({signed_score:.2f})"

    # Spot has no native short selling in this signal-only strategy.
    if market == "spot" and direction < 0:
        return None, "Spot mode: bearish setup is NO TRADE (no short)"

    entry = setup.close
    atr = setup.atr
    stop_distance = atr * ATR_SL_MULTIPLIER
    target_distance = atr * ATR_TP_MULTIPLIER
    rr = target_distance / stop_distance if stop_distance else 0
    if rr < MIN_RR:
        return None, f"Risk/reward too low ({rr:.2f})"

    side = "BUY" if direction > 0 else "SELL"
    if direction > 0:
        sl = entry - stop_distance
        tp = entry + target_distance
    else:
        sl = entry + stop_distance
        tp = entry - target_distance

    reasons = []
    for tf in ("15m", "1h", "4h", "5m"):
        reasons.extend([f"{tf}: {r}" for r in analyses[tf].reasons[:3]])

    return {
        "market": market,
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "stop_loss": sl,
        "take_profit": tp,
        "score": signed_score,
        "rr": rr,
        "rsi": setup.rsi,
        "adx": setup.adx,
        "reasons": reasons[:9],
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    }, None


def fmt_price(value: float) -> str:
    if value >= 1000:
        return f"{value:,.2f}"
    if value >= 1:
        return f"{value:.4f}"
    return f"{value:.8f}".rstrip("0").rstrip(".")


def render_signal(signal: dict[str, Any], signal_id: int) -> str:
    market_name = "SPOT" if signal["market"] == "spot" else "PERPETUAL FUTURES"
    emoji = "🟢" if signal["side"] == "BUY" else "🔴"
    reasons = "\n".join(f"• {x}" for x in signal["reasons"]) or "• Multi-timeframe confirmation"
    return (
        f"{emoji} <b>BingX v8.0 — {signal['side']}</b>\n"
        f"<b>{signal['symbol']} | {market_name}</b>\n\n"
        f"Вход (ориентир): <code>{fmt_price(signal['entry'])}</code>\n"
        f"Stop Loss: <code>{fmt_price(signal['stop_loss'])}</code>\n"
        f"Take Profit: <code>{fmt_price(signal['take_profit'])}</code>\n"
        f"Risk/Reward: <b>1:{signal['rr']:.2f}</b>\n"
        f"Сила модели: <b>{signal['score']:.2f}</b>\n"
        f"RSI M15: {signal['rsi']:.1f} | ADX M15: {signal['adx']:.1f}\n\n"
        f"<b>Обоснование:</b>\n{reasons}\n\n"
        f"ID сигнала: <code>{signal_id}</code>\n"
        f"Время: {signal['time']}\n\n"
        f"⚠️ Это аналитический сигнал, не гарантия результата. "
        f"Проверь спред, ликвидность, комиссии и цену исполнения. "
        f"Плечо и размер позиции рассчитывай отдельно."
    )


# ---------------------------- TELEGRAM ----------------------------

bot = Bot(token=TELEGRAM_BOT_TOKEN)
dp = Dispatcher()
scan_lock = asyncio.Lock()
background_task: asyncio.Task | None = None
http_session: aiohttp.ClientSession | None = None
auto_chats: set[int] = set()


def authorized(message: Message) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return bool(message.from_user and message.from_user.id in ALLOWED_USER_IDS)


async def reply_error(message: Message, exc: Exception):
    log.exception("Command failed")
    await message.answer(f"Ошибка: {str(exc)[:350]}")


@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not authorized(message):
        return
    await message.answer(
        "🤖 <b>Trading Bot v8.0 для BingX</b>\n\n"
        "Поддержка: Spot и USDT-M Perpetual Futures.\n"
        "Бот отправляет сигналы, но не открывает сделки.\n\n"
        "<b>Команды:</b>\n"
        "/scan — запустить анализ сейчас\n"
        "/auto_on — включить автоматическую отправку сигналов\n"
        "/auto_off — выключить автоматическую отправку\n"
        "/stats — статистика отмеченных результатов\n"
        "/result ID WIN — отметить прибыльный сигнал\n"
        "/result ID LOSS — отметить убыточный сигнал\n"
        "/help — справка\n\n"
        "По умолчанию автоматическая отправка выключена."
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    if not authorized(message):
        return
    await cmd_start(message)


async def run_scan(chat_id: int, notify_no_trade: bool = True):
    global http_session
    if scan_lock.locked():
        await bot.send_message(chat_id, "Анализ уже выполняется. Подожди завершения.")
        return

    async with scan_lock:
        messages = []
        failures = []
        client = BingXClient(http_session)
        for market in MARKETS:
            for symbol in SYMBOLS:
                try:
                    signal, reason = await build_signal(client, market, symbol)
                    if signal:
                        signal_id = save_signal(signal)
                        if signal_id:
                            messages.append(render_signal(signal, signal_id))
                    else:
                        log.info("NO TRADE %s %s: %s", market, symbol, reason)
                except Exception as exc:
                    log.warning("Analysis failed %s %s: %s", market, symbol, exc)
                    failures.append(f"{market} {symbol}: {str(exc)[:120]}")

        if messages:
            for text in messages:
                await bot.send_message(chat_id, text, parse_mode="HTML")
        elif notify_no_trade:
            await bot.send_message(
                chat_id,
                "🔎 Анализ завершён: подходящих сигналов нет (NO TRADE)."
            )

        if failures:
            await bot.send_message(
                chat_id,
                "⚠️ Не удалось проверить часть инструментов:\n" + "\n".join(failures[:8])
            )


@dp.message(Command("scan"))
async def cmd_scan(message: Message):
    if not authorized(message):
        return
    await message.answer("Начинаю анализ BingX Spot и Perpetual Futures…")
    try:
        await run_scan(message.chat.id)
    except Exception as exc:
        await reply_error(message, exc)


@dp.message(Command("auto_on"))
async def cmd_auto_on(message: Message):
    if not authorized(message):
        return
    auto_chats.add(message.chat.id)
    await message.answer(
        f"✅ Автоматическая отправка включена. Проверка каждые "
        f"{SCAN_INTERVAL_SECONDS} секунд."
    )


@dp.message(Command("auto_off"))
async def cmd_auto_off(message: Message):
    if not authorized(message):
        return
    auto_chats.discard(message.chat.id)
    await message.answer("⏸ Автоматическая отправка выключена.")


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not authorized(message):
        return
    s = get_stats()
    wr = "нет данных" if s["winrate"] is None else f"{s['winrate']:.1f}%"
    text = (
        "📊 <b>Статистика v8.0</b>\n\n"
        f"Закрытых сигналов: {s['total']}\n"
        f"WIN: {s['wins']}\nLOSS: {s['losses']}\n"
        f"Win rate: {wr}\n"
        f"Открытых/неразмеченных: {s['open']}\n\n"
        "Результаты учитываются только после ручной отметки командой /result. "
        "Это не фактический PnL и не учитывает комиссии/проскальзывание."
    )
    await message.answer(text)


@dp.message(Command("result"))
async def cmd_result(message: Message):
    if not authorized(message):
        return
    parts = (message.text or "").split()
    if len(parts) != 3 or not parts[1].isdigit() or parts[2].upper() not in {"WIN", "LOSS"}:
        await message.answer("Формат: /result ID WIN или /result ID LOSS")
        return
    ok = close_signal(int(parts[1]), parts[2])
    await message.answer(
        "Результат сохранён." if ok else
        "Не найден открытый сигнал с таким ID."
    )


async def background_scanner():
    while True:
        try:
            for chat_id in list(auto_chats):
                await run_scan(chat_id, notify_no_trade=False)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Background scanner error")
        await asyncio.sleep(max(30, SCAN_INTERVAL_SECONDS))


async def main():
    global http_session, background_task
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "Не задан TELEGRAM_BOT_TOKEN. Создай файл .env по примеру .env.example"
        )
    if not SYMBOLS:
        raise RuntimeError("Список SYMBOLS пуст")
    if not MARKETS:
        raise RuntimeError("MARKETS должен содержать spot и/или swap")

    init_db()
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
    http_session = aiohttp.ClientSession(timeout=timeout, connector=connector)

    background_task = asyncio.create_task(background_scanner())
    log.info("BingX Trading Bot v8.0 started. Markets=%s Symbols=%s", MARKETS, SYMBOLS)

    try:
        await dp.start_polling(bot)
    finally:
        if background_task:
            background_task.cancel()
            try:
                await background_task
            except asyncio.CancelledError:
                pass
        await http_session.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
