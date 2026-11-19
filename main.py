#!/usr/bin/env python3
# Trading Bot v8.1 — BingX Spot + USDT-M Perpetual Futures
# Telegram alerts only. No orders are placed. No BingX API keys are needed.

import asyncio
import logging
import os
import sqlite3
import time
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

# ---------------- CONFIG ----------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_IDS = {
    int(value.strip())
    for value in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if value.strip().isdigit()
}

SYMBOLS = [
    value.strip().upper()
    for value in os.getenv(
        "SYMBOLS", "BTC-USDT,ETH-USDT,SOL-USDT,XRP-USDT"
    ).split(",")
    if value.strip()
]
MARKETS = [
    value.strip().lower()
    for value in os.getenv("MARKETS", "spot,swap").split(",")
    if value.strip().lower() in {"spot", "swap"}
]

SCAN_INTERVAL_SECONDS = max(30, int(os.getenv("SCAN_INTERVAL_SECONDS", "120")))
MIN_SIGNAL_SCORE = float(os.getenv("MIN_SIGNAL_SCORE", "5.0"))
ATR_SL_MULTIPLIER = float(os.getenv("ATR_SL_MULTIPLIER", "1.5"))
ATR_TP_MULTIPLIER = float(os.getenv("ATR_TP_MULTIPLIER", "2.7"))
MIN_RR = float(os.getenv("MIN_RR", "1.8"))
DATABASE_PATH = os.getenv("DATABASE_PATH", "trading_bot_v8.db")
CANDLE_LIMIT = 250
HTTP_TIMEOUT_SECONDS = 20

BINGX_BASE_URL = "https://open-api.bingx.com"
TIMEFRAMES = ("5m", "15m", "1h", "4h")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("bingx-v8")


# ---------------- DATABASE ----------------

def db_connect():
    connection = sqlite3.connect(DATABASE_PATH, timeout=20)
    connection.row_factory = sqlite3.Row
    return connection


def init_db():
    with db_connect() as connection:
        connection.execute("""
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
                closed_at TEXT
            )
        """)
        connection.commit()


def save_signal(signal: dict[str, Any]) -> int | None:
    now = datetime.now(timezone.utc)
    cutoff = (now.timestamp() - 45 * 60)
    cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()

    with db_connect() as connection:
        duplicate = connection.execute("""
            SELECT id FROM signals
            WHERE market=? AND symbol=? AND side=?
              AND status='OPEN' AND created_at>=?
            ORDER BY id DESC LIMIT 1
        """, (signal["market"], signal["symbol"], signal["side"], cutoff_iso)).fetchone()

        if duplicate:
            return None

        cursor = connection.execute("""
            INSERT INTO signals
            (created_at, market, symbol, side, entry, stop_loss,
             take_profit, score)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            now.isoformat(), signal["market"], signal["symbol"],
            signal["side"], signal["entry"], signal["stop_loss"],
            signal["take_profit"], signal["score"]
        ))
        connection.commit()
        return int(cursor.lastrowid)


def close_signal(signal_id: int, result: str) -> bool:
    with db_connect() as connection:
        cursor = connection.execute("""
            UPDATE signals SET status='CLOSED', result=?, closed_at=?
            WHERE id=? AND status='OPEN'
        """, (
            result.upper(), datetime.now(timezone.utc).isoformat(), signal_id
        ))
        connection.commit()
        return cursor.rowcount == 1


def get_stats() -> dict[str, Any]:
    with db_connect() as connection:
        total = connection.execute(
            "SELECT COUNT(*) FROM signals WHERE status='CLOSED'"
        ).fetchone()[0]
        wins = connection.execute(
            "SELECT COUNT(*) FROM signals WHERE result='WIN'"
        ).fetchone()[0]
        losses = connection.execute(
            "SELECT COUNT(*) FROM signals WHERE result='LOSS'"
        ).fetchone()[0]
        opened = connection.execute(
            "SELECT COUNT(*) FROM signals WHERE status='OPEN'"
        ).fetchone()[0]

    return {
        "total": total,
        "wins": wins,
        "losses": losses,
        "open": opened,
        "winrate": wins / total * 100 if total else None,
    }


# ---------------- BINGX MARKET DATA ----------------

class BingXClient:
    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def get_json(self, path: str, params: dict[str, Any]) -> Any:
        url = BINGX_BASE_URL + path
        async with self.session.get(url, params=params) as response:
            body = await response.text()
            if response.status != 200:
                raise RuntimeError(
                    f"BingX HTTP {response.status}: {body[:200]}"
                )
            try:
                payload = await response.json(content_type=None)
            except Exception as exc:
                raise RuntimeError(f"BingX вернул не JSON: {body[:200]}") from exc

        if isinstance(payload, dict):
            code = payload.get("code")
            if code not in (None, 0, "0"):
                raise RuntimeError(
                    f"BingX API {code}: {payload.get('msg', 'без описания')}"
                )
            return payload.get("data", payload)
        return payload

    async def klines(self, market: str, symbol: str, interval: str) -> pd.DataFrame:
        # BingX public market-data endpoints.
        if market == "spot":
            path = "/openApi/spot/v2/market/kline"
        else:
            path = "/openApi/swap/v3/quote/klines"

        data = await self.get_json(path, {
            "symbol": symbol,
            "interval": interval,
            "limit": CANDLE_LIMIT
        })

        if isinstance(data, dict):
            for key in ("data", "list", "klines", "rows"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break

        if not isinstance(data, list):
            raise RuntimeError(f"Неожиданный формат свечей {market} {symbol} {interval}")

        rows = []
        for item in data:
            if isinstance(item, (list, tuple)) and len(item) >= 6:
                rows.append({
                    "open_time": item[0],
                    "open": item[1], "high": item[2], "low": item[3],
                    "close": item[4], "volume": item[5],
                    "close_time": item[6] if len(item) > 6 else item[0],
                })
            elif isinstance(item, dict):
                rows.append({
                    "open_time": item.get("time", item.get("openTime", item.get("open_time"))),
                    "open": item.get("open"),
                    "high": item.get("high"),
                    "low": item.get("low"),
                    "close": item.get("close"),
                    "volume": item.get("volume", item.get("vol")),
                    "close_time": item.get("closeTime", item.get("close_time", item.get("time"))),
                })

        frame = pd.DataFrame(rows)
        required = ["open_time", "open", "high", "low", "close", "volume", "close_time"]
        if frame.empty or any(column not in frame.columns for column in required):
            raise RuntimeError("BingX изменил формат ответа свечей или список пуст")

        for column in required:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.dropna(subset=required)
        frame = frame.sort_values("open_time").drop_duplicates("open_time")
        if frame.empty:
            raise RuntimeError("BingX не вернул корректные свечи")

        # BingX timestamps can be seconds or milliseconds.
        if frame["close_time"].median() < 10_000_000_000:
            frame["close_time"] *= 1000

        # Do not analyze the still-forming candle.
        frame = frame[frame["close_time"] < int(time.time() * 1000) - 1000]
        if len(frame) < 205:
            raise RuntimeError(
                f"Мало закрытых свечей: {len(frame)}; нужно минимум 205"
            )

        if (
            (frame["high"] < frame["low"]).any()
            or (frame["volume"] < 0).any()
            or (frame[["open", "high", "low", "close"]] <= 0).any().any()
        ):
            raise RuntimeError("Получены некорректные OHLCV данные")

        return frame.tail(CANDLE_LIMIT).reset_index(drop=True)


# ---------------- INDICATORS ----------------

def add_indicators(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    close, high, low = data["close"], data["high"], data["low"]

    change = close.diff()
    gains = change.clip(lower=0)
    losses = -change.clip(upper=0)
    avg_gain = gains.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    avg_loss = losses.ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    data["rsi"] = 100 - 100 / (1 + rs)
    data.loc[(avg_loss == 0) & (avg_gain > 0), "rsi"] = 100
    data.loc[(avg_loss == 0) & (avg_gain == 0), "rsi"] = 50

    data["ema20"] = close.ewm(span=20, adjust=False).mean()
    data["ema50"] = close.ewm(span=50, adjust=False).mean()
    data["ema200"] = close.ewm(span=200, adjust=False).mean()

    macd = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    data["macd"] = macd
    data["macd_signal"] = macd.ewm(span=9, adjust=False).mean()
    data["macd_hist"] = data["macd"] - data["macd_signal"]

    previous_close = close.shift(1)
    true_range = pd.concat([
        high - low,
        (high - previous_close).abs(),
        (low - previous_close).abs()
    ], axis=1).max(axis=1)
    data["atr"] = true_range.ewm(
        alpha=1 / 14, adjust=False, min_periods=14
    ).mean()

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
    minus_dm = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
    atr_safe = data["atr"].replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=1 / 14, adjust=False).mean() / atr_safe
    minus_di = 100 * minus_dm.ewm(alpha=1 / 14, adjust=False).mean() / atr_safe
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    data["adx"] = dx.ewm(alpha=1 / 14, adjust=False).mean()
    data["plus_di"] = plus_di
    data["minus_di"] = minus_di
    return data


def analyze_timeframe(raw: pd.DataFrame) -> dict[str, Any]:
    frame = add_indicators(raw)
    current = frame.iloc[-1]
    previous = frame.iloc[-2]

    close = float(current["close"])
    atr = float(current["atr"])
    rsi = float(current["rsi"])
    adx = float(current["adx"]) if pd.notna(current["adx"]) else 0.0

    if not all(np.isfinite(value) for value in (close, atr, rsi)) or atr <= 0:
        raise RuntimeError("Индикаторы ещё не рассчитаны")

    score = 0.0
    reasons: list[str] = []

    if current["ema20"] > current["ema50"] > current["ema200"]:
        score += 2.0
        reasons.append("EMA20 выше EMA50 и EMA200")
    elif current["ema20"] < current["ema50"] < current["ema200"]:
        score -= 2.0
        reasons.append("EMA20 ниже EMA50 и EMA200")

    if current["macd"] > current["macd_signal"] and current["macd_hist"] > previous["macd_hist"]:
        score += 1.25
        reasons.append("MACD подтверждает бычий импульс")
    elif current["macd"] < current["macd_signal"] and current["macd_hist"] < previous["macd_hist"]:
        score -= 1.25
        reasons.append("MACD подтверждает медвежий импульс")

    if 50 < rsi < 70:
        score += 0.75
        reasons.append(f"RSI в бычьей зоне ({rsi:.1f})")
    elif 30 < rsi < 50:
        score -= 0.75
        reasons.append(f"RSI в медвежьей зоне ({rsi:.1f})")
    elif rsi >= 75:
        score -= 0.5
        reasons.append(f"RSI перегрет ({rsi:.1f})")
    elif rsi <= 25:
        score += 0.5
        reasons.append(f"RSI перепродан ({rsi:.1f})")

    if adx >= 20:
        if current["plus_di"] > current["minus_di"]:
            score += 0.75
            reasons.append(f"ADX/DI: восходящее движение ({adx:.1f})")
        elif current["minus_di"] > current["plus_di"]:
            score -= 0.75
            reasons.append(f"ADX/DI: нисходящее движение ({adx:.1f})")

    previous_high = frame["high"].iloc[-21:-1].max()
    previous_low = frame["low"].iloc[-21:-1].min()
    if close > previous_high:
        score += 1.0
        reasons.append("Пробой максимума 20 свечей")
    elif close < previous_low:
        score -= 1.0
        reasons.append("Пробой минимума 20 свечей")

    direction = 1 if score >= 1.5 else -1 if score <= -1.5 else 0
    return {
        "direction": direction,
        "score": score,
        "close": close,
        "atr": atr,
        "rsi": rsi,
        "adx": adx,
        "reasons": reasons,
    }


# ---------------- SIGNAL ENGINE ----------------

async def build_signal(client: BingXClient, market: str, symbol: str):
    analyses = {}
    for timeframe in TIMEFRAMES:
        candles = await client.klines(market, symbol, timeframe)
        analyses[timeframe] = analyze_timeframe(candles)
        await asyncio.sleep(0.05)

    setup = analyses["15m"]
    m5, h1, h4 = analyses["5m"], analyses["1h"], analyses["4h"]
    direction = setup["direction"]

    if direction == 0:
        return None, "M15: нет направленного сигнала"

    if h1["direction"] == -direction and h4["direction"] == -direction:
        return None, "H1 и H4 противоречат сигналу"
    if h1["direction"] != direction and h4["direction"] != direction:
        return None, "Нет подтверждения H1/H4"
    if m5["direction"] == -direction and abs(m5["score"]) >= 3:
        return None, "M5 сильно противоречит сигналу"

    weighted = (
        setup["score"] * 1.5
        + h1["score"] * 1.5
        + h4["score"] * 1.25
        + m5["score"] * 0.75
    )
    signed_score = weighted if direction > 0 else -weighted
    if signed_score < MIN_SIGNAL_SCORE:
        return None, f"Недостаточная сила сигнала ({signed_score:.2f})"

    if market == "spot" and direction < 0:
        return None, "Spot: сигнал SELL пропущен (короткая продажа не поддерживается)"

    entry = setup["close"]
    stop_distance = setup["atr"] * ATR_SL_MULTIPLIER
    target_distance = setup["atr"] * ATR_TP_MULTIPLIER
    rr = target_distance / stop_distance if stop_distance > 0 else 0
    if rr < MIN_RR:
        return None, f"Недостаточное Risk/Reward ({rr:.2f})"

    side = "BUY" if direction > 0 else "SELL"
    stop = entry - stop_distance if direction > 0 else entry + stop_distance
    target = entry + target_distance if direction > 0 else entry - target_distance

    reasons = []
    for timeframe in ("15m", "1h", "4h", "5m"):
        reasons.extend(
            f"{timeframe}: {reason}"
            for reason in analyses[timeframe]["reasons"][:3]
        )

    return {
        "market": market,
        "symbol": symbol,
        "side": side,
        "entry": entry,
        "stop_loss": stop,
        "take_profit": target,
        "score": signed_score,
        "rr": rr,
        "rsi": setup["rsi"],
        "adx": setup["adx"],
        "reasons": reasons[:9],
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
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
    reasons = "\n".join(f"• {reason}" for reason in signal["reasons"])
    return (
        f"{emoji} <b>BingX v8.1 — {signal['side']}</b>\n"
        f"<b>{signal['symbol']} | {market_name}</b>\n\n"
        f"Вход (ориентир): <code>{fmt_price(signal['entry'])}</code>\n"
        f"Stop Loss: <code>{fmt_price(signal['stop_loss'])}</code>\n"
        f"Take Profit: <code>{fmt_price(signal['take_profit'])}</code>\n"
        f"Risk/Reward: <b>1:{signal['rr']:.2f}</b>\n"
        f"Сила модели (не вероятность): <b>{signal['score']:.2f}</b>\n"
        f"RSI M15: {signal['rsi']:.1f} | ADX M15: {signal['adx']:.1f}\n\n"
        f"<b>Обоснование:</b>\n{reasons or 'Мультитаймфреймовое подтверждение'}\n\n"
        f"ID сигнала: <code>{signal_id}</code>\n"
        f"Время: {signal['time']}\n\n"
        "⚠️ Сигнал не гарантирует прибыль. Проверь цену исполнения, спред, "
        "комиссии и ликвидность. Для фьючерсов отдельно оцени плечо и риск."
    )


# ---------------- TELEGRAM ----------------

bot: Bot | None = None
dp = Dispatcher()
http_session: aiohttp.ClientSession | None = None
scan_lock = asyncio.Lock()
auto_chats: set[int] = set()


def authorized(message: Message) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    return bool(message.from_user and message.from_user.id in ALLOWED_USER_IDS)


@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not authorized(message):
        return
    await message.answer(
        "🤖 <b>Trading Bot v8.1 — BingX</b>\n\n"
        "Рынки: Spot и USDT-M Perpetual Futures.\n"
        "Бот только присылает сигналы и не открывает сделки.\n\n"
        "<b>Команды:</b>\n"
        "/scan — анализ сейчас\n"
        "/auto_on — включить периодические сигналы\n"
        "/auto_off — выключить периодические сигналы\n"
        "/stats — статистика\n"
        "/result ID WIN — отметить WIN\n"
        "/result ID LOSS — отметить LOSS\n"
        "/help — справка\n\n"
        "Автоматические уведомления после перезапуска выключены."
    )


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await cmd_start(message)


async def run_scan(chat_id: int, notify_no_trade: bool = True):
    if bot is None or http_session is None:
        return
    if scan_lock.locked():
        await bot.send_message(chat_id, "Анализ уже выполняется. Подожди.")
        return

    async with scan_lock:
        client = BingXClient(http_session)
        signals_to_send = []
        errors = []

        for market in MARKETS:
            for symbol in SYMBOLS:
                try:
                    signal, reason = await build_signal(client, market, symbol)
                    if signal:
                        signal_id = save_signal(signal)
                        if signal_id is not None:
                            signals_to_send.append(render_signal(signal, signal_id))
                    else:
                        log.info("NO TRADE %s %s: %s", market, symbol, reason)
                except Exception as exc:
                    log.exception("BingX analysis failed for %s %s", market, symbol)
                    errors.append(f"{market} {symbol}: {str(exc)[:130]}")

        for signal_text in signals_to_send:
            await bot.send_message(chat_id, signal_text, parse_mode="HTML")

        if not signals_to_send and notify_no_trade:
            await bot.send_message(
                chat_id, "🔎 Анализ завершён: подходящих сигналов нет (NO TRADE)."
            )

        if errors:
            await bot.send_message(
                chat_id,
                "⚠️ Ошибки получения данных:\n" + "\n".join(errors[:8])
            )


@dp.message(Command("scan"))
async def cmd_scan(message: Message):
    if not authorized(message):
        return
    await message.answer("Начинаю анализ BingX…")
    try:
        await run_scan(message.chat.id)
    except Exception as exc:
        log.exception("Scan command failed")
        await message.answer(f"Ошибка анализа: {str(exc)[:250]}")


@dp.message(Command("auto_on"))
async def cmd_auto_on(message: Message):
    if not authorized(message):
        return
    auto_chats.add(message.chat.id)
    await message.answer(
        f"✅ Автоотправка включена. Интервал: {SCAN_INTERVAL_SECONDS} секунд."
    )


@dp.message(Command("auto_off"))
async def cmd_auto_off(message: Message):
    if not authorized(message):
        return
    auto_chats.discard(message.chat.id)
    await message.answer("⏸ Автоотправка выключена.")


@dp.message(Command("stats"))
async def cmd_stats(message: Message):
    if not authorized(message):
        return
    stats = get_stats()
    winrate = "нет данных" if stats["winrate"] is None else f"{stats['winrate']:.1f}%"
    await message.answer(
        "📊 <b>Статистика v8.1</b>\n\n"
        f"Закрытых сигналов: {stats['total']}\n"
        f"WIN: {stats['wins']}\n"
        f"LOSS: {stats['losses']}\n"
        f"Win rate: {winrate}\n"
        f"Открытых/неразмеченных: {stats['open']}\n\n"
        "Результаты отмечаются вручную и не являются фактическим PnL."
    )


@dp.message(Command("result"))
async def cmd_result(message: Message):
    if not authorized(message):
        return
    parts = (message.text or "").split()
    if (
        len(parts) != 3
        or not parts[1].isdigit()
        or parts[2].upper() not in {"WIN", "LOSS"}
    ):
        await message.answer("Формат: /result ID WIN или /result ID LOSS")
        return
    updated = close_signal(int(parts[1]), parts[2])
    await message.answer(
        "Результат сохранён." if updated else "Не найден открытый сигнал с таким ID."
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
        await asyncio.sleep(SCAN_INTERVAL_SECONDS)


# ---------------- STARTUP ----------------

async def main():
    global bot, http_session

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "Не задан TELEGRAM_BOT_TOKEN. Добавь его в Variables/Secrets хостинга."
        )
    if ":" not in TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN имеет неверный формат. Проверь значение в настройках."
        )
    if not SYMBOLS:
        raise RuntimeError("Список SYMBOLS пуст.")
    if not MARKETS:
        raise RuntimeError("MARKETS должен содержать spot и/или swap.")

    init_db()
    bot = Bot(token=TELEGRAM_BOT_TOKEN)
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_SECONDS)
    connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
    http_session = aiohttp.ClientSession(timeout=timeout, connector=connector)

    scanner_task = asyncio.create_task(background_scanner())
    log.info(
        "BingX v8.1 запущен. Markets=%s Symbols=%s",
        MARKETS, SYMBOLS
    )

    try:
        await dp.start_polling(bot)
    finally:
        scanner_task.cancel()
        try:
            await scanner_task
        except asyncio.CancelledError:
            pass
        if http_session and not http_session.closed:
            await http_session.close()
        if bot:
            await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
