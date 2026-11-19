
"""Trading Bot v9.1 - BingX market signals only.
No real order execution. Python 3.10+ / aiogram 3.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any
from contextlib import contextmanager

import aiohttp
import numpy as np
import pandas as pd

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()

# ----------------------------- Configuration -----------------------------

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError(
        "Не задан TELEGRAM_BOT_TOKEN в переменных окружения хостинга"
    )

SYMBOLS = [
    x.strip().upper()
    for x in os.getenv(
        "SYMBOLS",
        "BTC-USDT,ETH-USDT,SOL-USDT,XRP-USDT"
    ).split(",")
    if x.strip()
]

MARKETS = [
    x.strip().lower()
    for x in os.getenv("MARKETS", "spot,swap").split(",")
    if x.strip()
]

MARKETS = [m for m in MARKETS if m in ("spot", "swap")]
if not MARKETS:
    MARKETS = ["spot", "swap"]

# ВАЖНО: сохраняем прежнее имя базы данных.
DB_PATH = "trading_bot_v9.db"

SCAN_INTERVAL = max(
    900, int(os.getenv("SCAN_INTERVAL_SECONDS", "900"))
)
MONITOR_INTERVAL = max(
    30, int(os.getenv("MONITOR_INTERVAL_SECONDS", "60"))
)

MIN_RR = max(1.8, float(os.getenv("MIN_RR", "1.8")))
MIN_SCORE = max(1, int(os.getenv("MIN_SCORE", "6")))

ALLOWED_USER_IDS = {
    int(x)
    for x in os.getenv("ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
}

BASE = "https://open-api.bingx.com"

TF_MS = {
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)
log = logging.getLogger("trading-bot-v9.1")

_api_rate_lock = asyncio.Lock()
_last_api_request = 0.0
_api_cooldown_until = 0.0

scan_lock = asyncio.Lock()
scan_task: asyncio.Task | None = None


# ----------------------------- Database ----------------------------------

@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db() -> None:
    """Create tables and safely add missing columns without deleting data."""
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry REAL NOT NULL,
                sl REAL NOT NULL,
                tp REAL NOT NULL,
                rr REAL NOT NULL,
                score INTEGER NOT NULL,
                reasons TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'OPEN',
                result_r REAL,
                closed_at TEXT,
                last_checked_ms INTEGER DEFAULT 0,
                setup_key TEXT NOT NULL
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS subscribers (
                chat_id INTEGER PRIMARY KEY,
                auto_send INTEGER NOT NULL DEFAULT 0
            )
        """)

        # Проверяем структуру старой таблицы signals.
        columns = {
            row["name"]
            for row in con.execute(
                "PRAGMA table_info(signals)"
            ).fetchall()
        }

        migrations = {
            "result_r": "REAL",
            "closed_at": "TEXT",
            "last_checked_ms": "INTEGER DEFAULT 0",
            "setup_key": "TEXT",
        }

        for name, definition in migrations.items():
            if name not in columns:
                con.execute(
                    f"ALTER TABLE signals ADD COLUMN {name} {definition}"
                )

        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_signals_status
            ON signals(status)
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_signals_setup
            ON signals(setup_key, created_at)
        """)

        log.info("Database initialized: %s", DB_PATH)


def get_setting(key: str, default: str = "") -> str:
    with db() as con:
        row = con.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,)
        ).fetchone()
        return row["value"] if row else default


def set_auto(chat_id: int, enabled: bool) -> None:
    with db() as con:
        con.execute("""
            INSERT INTO subscribers(chat_id, auto_send)
            VALUES(?, ?)
            ON CONFLICT(chat_id)
            DO UPDATE SET auto_send=excluded.auto_send
        """, (chat_id, int(enabled)))


def auto_chats() -> list[int]:
    with db() as con:
        rows = con.execute(
            "SELECT chat_id FROM subscribers WHERE auto_send=1"
        ).fetchall()
        return [int(row["chat_id"]) for row in rows]


def allowed(user_id: int) -> bool:
    return not ALLOWED_USER_IDS or user_id in ALLOWED_USER_IDS


# ----------------------------- Exchange data -----------------------------

class MarketError(Exception):
    pass


async def api_get(
    session: aiohttp.ClientSession,
    path: str,
    params: dict[str, Any]
) -> Any:
    global _last_api_request, _api_cooldown_until

    url = BASE + path
    last_error: Exception | None = None

    for attempt in range(4):
        try:
            async with _api_rate_lock:
                now = time.monotonic()

                wait = max(
                    1.6 - (now - _last_api_request),
                    _api_cooldown_until - now,
                    0.0
                )

                if wait > 0:
                    await asyncio.sleep(wait)

                async with session.get(
                    url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=25)
                ) as response:

                    _last_api_request = time.monotonic()
                    raw_text = await response.text()

                    if response.status != 200:
                        raise MarketError(
                            f"HTTP {response.status}: {raw_text[:200]}"
                        )

                    try:
                        payload = await response.json(
                            content_type=None
                        )
                    except Exception as exc:
                        raise MarketError(
                            f"Некорректный JSON от BingX: {raw_text[:200]}"
                        ) from exc

                    if isinstance(payload, dict):
                        code = payload.get("code")

                        if code not in (None, 0, "0"):
                            message = (
                                f"BingX {code}: "
                                f"{payload.get('msg', '')}"
                            )

                            if str(code) == "100410":
                                _api_cooldown_until = (
                                    time.monotonic()
                                    + min(30, 3 * (2 ** attempt))
                                )
                                last_error = MarketError(message)
                            else:
                                raise MarketError(message)
                        else:
                            return payload
                    else:
                        return payload

            if last_error is not None:
                if attempt < 3:
                    log.warning(
                        "BingX rate limit. Retry %s/3",
                        attempt + 1
                    )
                    continue
                raise last_error

        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = MarketError(str(exc))

            if attempt < 3:
                await asyncio.sleep(min(2 ** attempt, 8))
                continue

            raise last_error from exc

    raise last_error or MarketError("Неизвестная ошибка BingX API")


def extract_data(payload: Any) -> Any:
    if isinstance(payload, dict):
        return payload.get(
            "data",
            payload.get("result", payload)
        )
    return payload


def normalize_candles(
    payload: Any,
    interval: str,
    min_rows: int = 205
) -> pd.DataFrame:

    raw = extract_data(payload)

    if isinstance(raw, dict):
        raw = raw.get("data", raw.get("list", []))

    if not isinstance(raw, list) or not raw:
        raise MarketError("Пустой ответ со свечами")

    rows = []

    for item in raw:
        try:
            if isinstance(item, (list, tuple)):
                ot = int(float(item[0]))
                o, h, l, c, v = map(float, item[1:6])

                ct = (
                    int(float(item[6]))
                    if len(item) > 6 and item[6] not in (None, "")
                    else ot + TF_MS[interval] - 1
                )

            elif isinstance(item, dict):
                ot = int(float(
                    item.get(
                        "openTime",
                        item.get("time", item.get("timestamp", 0))
                    )
                ))

                o, h, l, c, v = (
                    float(item[k])
                    for k in (
                        "open", "high", "low", "close", "volume"
                    )
                )

                ct = int(float(
                    item.get(
                        "closeTime",
                        item.get(
                            "close_time",
                            ot + TF_MS[interval] - 1
                        )
                    )
                ))

            else:
                continue

            if ot <= 0:
                continue

            if not all(
                np.isfinite(x) and x > 0
                for x in (o, h, l, c)
            ):
                continue

            if not np.isfinite(v) or v < 0:
                continue

            if h < max(o, c, l) or l > min(o, c, h):
                continue

            rows.append((ot, o, h, l, c, v, ct))

        except (
            ValueError, TypeError, KeyError, IndexError
        ):
            continue

    if not rows:
        raise MarketError("Не удалось распознать формат свечей")

    df = pd.DataFrame(
        rows,
        columns=[
            "open_time", "open", "high", "low",
            "close", "volume", "close_time"
        ]
    )

    df = (
        df.drop_duplicates("open_time")
        .sort_values("open_time")
        .reset_index(drop=True)
    )

    # Используем только полностью закрытые свечи.
    now_ms = int(time.time() * 1000)

    df = df[
        df.close_time < now_ms - 2_000
    ].reset_index(drop=True)

    if len(df) < min_rows:
        raise MarketError(
            f"Недостаточно закрытых свечей: {len(df)} "
            f"(нужно минимум {min_rows})"
        )

    return df


async def get_klines(
    session: aiohttp.ClientSession,
    market: str,
    symbol: str,
    interval: str,
    limit: int = 300,
    min_rows: int = 205
) -> pd.DataFrame:

    if market == "spot":
        path = "/openApi/spot/v2/market/kline"
    elif market == "swap":
        path = "/openApi/swap/v3/quote/klines"
    else:
        raise MarketError(f"Неизвестный рынок: {market}")

    payload = await api_get(
        session,
        path,
        {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }
    )

    return normalize_candles(
        payload,
        interval,
        min_rows=min_rows
    )

# ----------------------------- Indicators --------------------------------

def indicators(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()

    close = x.close
    high = x.high
    low = x.low

    x["ema20"] = close.ewm(
        span=20, adjust=False
    ).mean()

    x["ema50"] = close.ewm(
        span=50, adjust=False
    ).mean()

    x["ema200"] = close.ewm(
        span=200, adjust=False
    ).mean()

    delta = close.diff()

    gain = delta.clip(lower=0).ewm(
        alpha=1 / 14,
        adjust=False
    ).mean()

    loss = (-delta.clip(upper=0)).ewm(
        alpha=1 / 14,
        adjust=False
    ).mean()

    rs = gain / loss.replace(0, np.nan)

    x["rsi"] = (
        100 - 100 / (1 + rs)
    ).fillna(50)

    tr = pd.concat(
        [
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs()
        ],
        axis=1
    ).max(axis=1)

    x["atr"] = tr.ewm(
        alpha=1 / 14,
        adjust=False
    ).mean()

    up = high.diff()
    down = -low.diff()

    plus_dm = up.where(
        (up > down) & (up > 0),
        0.0
    )

    minus_dm = down.where(
        (down > up) & (down > 0),
        0.0
    )

    atr_safe = x.atr.replace(0, np.nan)

    plus_di = (
        100
        * plus_dm.ewm(alpha=1 / 14, adjust=False).mean()
        / atr_safe
    )

    minus_di = (
        100
        * minus_dm.ewm(alpha=1 / 14, adjust=False).mean()
        / atr_safe
    )

    dx = (
        100
        * (plus_di - minus_di).abs()
        / (plus_di + minus_di).replace(0, np.nan)
    )

    x["adx"] = dx.ewm(
        alpha=1 / 14,
        adjust=False
    ).mean().fillna(0)

    x["plus_di"] = plus_di.fillna(0)
    x["minus_di"] = minus_di.fillna(0)

    x["macd"] = (
        close.ewm(span=12, adjust=False).mean()
        - close.ewm(span=26, adjust=False).mean()
    )

    x["macd_signal"] = x.macd.ewm(
        span=9,
        adjust=False
    ).mean()

    return x


def build_signal(
    market: str,
    symbol: str,
    frames: dict[str, pd.DataFrame]
) -> dict[str, Any] | None:

    m5, m15, h1, h4 = (
        indicators(frames[t])
        for t in ("5m", "15m", "1h", "4h")
    )

    a = m5.iloc[-1]
    b = m15.iloc[-1]
    c = h1.iloc[-1]
    d = h4.iloc[-1]

    price = float(b.close)
    atr = float(b.atr)

    if (
        not np.isfinite(atr)
        or atr <= 0
        or price <= 0
    ):
        return None

    long_score = 0
    short_score = 0

    long_reasons = []
    short_reasons = []

    # Higher-timeframe direction: 1h + 4h.
    for row, label in ((c, "1H"), (d, "4H")):

        if (
            row.close > row.ema200
            and row.ema20 > row.ema50
        ):
            long_score += 2
            long_reasons.append(
                f"Тренд {label}: выше EMA200"
            )

        if (
            row.close < row.ema200
            and row.ema20 < row.ema50
        ):
            short_score += 2
            short_reasons.append(
                f"Тренд {label}: ниже EMA200"
            )

    # 15m setup.
    if (
        b.ema20 > b.ema50
        and b.close > b.ema20
    ):
        long_score += 2
        long_reasons.append("15м: EMA20 выше EMA50")

    if (
        b.ema20 < b.ema50
        and b.close < b.ema20
    ):
        short_score += 2
        short_reasons.append("15м: EMA20 ниже EMA50")

    if 48 <= b.rsi <= 68:
        long_score += 1
        long_reasons.append(
            f"RSI 15м {b.rsi:.1f}"
        )

    if 32 <= b.rsi <= 52:
        short_score += 1
        short_reasons.append(
            f"RSI 15м {b.rsi:.1f}"
        )

    if b.macd > b.macd_signal:
        long_score += 1
        long_reasons.append(
            "MACD 15м положительный"
        )

    if b.macd < b.macd_signal:
        short_score += 1
        short_reasons.append(
            "MACD 15м отрицательный"
        )

    if b.adx >= 18 and b.plus_di > b.minus_di:
        long_score += 1
        long_reasons.append(
            f"ADX {b.adx:.1f}, покупатели сильнее"
        )

    if b.adx >= 18 and b.minus_di > b.plus_di:
        short_score += 1
        short_reasons.append(
            f"ADX {b.adx:.1f}, продавцы сильнее"
        )

    # 5m momentum filter.
    if a.close > a.ema20:
        long_score += 1
        long_reasons.append(
            "5м: цена выше EMA20"
        )

    if a.close < a.ema20:
        short_score += 1
        short_reasons.append(
            "5м: цена ниже EMA20"
        )

    if long_score > short_score:
        side = "BUY"
        score = long_score
        reasons = long_reasons
    else:
        side = "SELL"
        score = short_score
        reasons = short_reasons

    if score < MIN_SCORE:
        return None

    if abs(long_score - short_score) < 2:
        return None

    # Spot remains BUY-only.
    if market == "spot" and side == "SELL":
        return None

    # ATR-based risk. Minimum RR is 1:1.8.
    sl_dist = max(
        1.5 * atr,
        price * 0.002
    )

    tp_dist = sl_dist * MIN_RR

    if side == "BUY":
        sl = price - sl_dist
        tp = price + tp_dist
    else:
        sl = price + sl_dist
        tp = price - tp_dist

    if sl <= 0 or tp <= 0:
        return None

    rr = abs(tp - price) / abs(price - sl)

    if rr < MIN_RR:
        return None

    return {
        "market": market,
        "symbol": symbol,
        "side": side,
        "entry": price,
        "sl": sl,
        "tp": tp,
        "rr": rr,
        "score": score,
        "reasons": reasons,
        "setup_key": (
            f"{market}:{symbol}:{side}:"
            f"{int(b.open_time)}"
        )
    }


# ----------------------------- Signal storage ----------------------------

def save_signal(sig: dict[str, Any]) -> int | None:
    now = datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    )

    with db() as con:

        exists = con.execute(
            "SELECT id FROM signals WHERE setup_key=?",
            (sig["setup_key"],)
        ).fetchone()

        recent = con.execute("""
            SELECT id
            FROM signals
            WHERE market=?
              AND symbol=?
              AND side=?
              AND created_at >= datetime('now','-45 minutes')
            ORDER BY id DESC
            LIMIT 1
        """, (
            sig["market"],
            sig["symbol"],
            sig["side"]
        )).fetchone()

        if exists or recent:
            return None

        cur = con.execute("""
            INSERT INTO signals (
                created_at,
                market,
                symbol,
                side,
                entry,
                sl,
                tp,
                rr,
                score,
                reasons,
                setup_key
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            now,
            sig["market"],
            sig["symbol"],
            sig["side"],
            sig["entry"],
            sig["sl"],
            sig["tp"],
            sig["rr"],
            sig["score"],
            "; ".join(sig["reasons"]),
            sig["setup_key"]
        ))

        return int(cur.lastrowid)


def fmt_price(value: float) -> str:
    return f"{value:.8f}".rstrip("0").rstrip(".")


def signal_text(
    sig: dict[str, Any],
    sid: int
) -> str:

    direction = (
        "🟢 BUY / ПОКУПКА"
        if sig["side"] == "BUY"
        else "🔴 SELL / ПРОДАЖА"
    )

    market = (
        "SPOT"
        if sig["market"] == "spot"
        else "USDT-M FUTURES"
    )

    return (
        f"⚡ <b>TRADING BOT v9.1</b>\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"{direction}\n"
        f"<b>{sig['symbol']}</b> · {market}\n\n"
        f"Вход: <code>{fmt_price(sig['entry'])}</code>\n"
        f"🛑 Stop Loss: <code>{fmt_price(sig['sl'])}</code>\n"
        f"🎯 Take Profit: <code>{fmt_price(sig['tp'])}</code>\n"
        f"⚖️ Risk/Reward: <b>1:{sig['rr']:.2f}</b>\n"
        f"🧠 Сила модели: <b>{sig['score']}/10</b> "
        f"(не вероятность выигрыша)\n\n"
        f"📊 Основания:\n"
        + "\n".join(
            f"• {reason}"
            for reason in sig["reasons"]
        )
        + f"\n\n🆔 Сигнал #{sid}\n"
        f"🕒 {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"<i>Аналитический сигнал. Не является гарантией "
        f"результата. Сделки открываются вручную.</i>"
    )


async def send_signal(
    bot: Bot,
    sig: dict[str, Any],
    sid: int,
    chat_id: int | None = None
) -> None:

    message_text = signal_text(sig, sid)

    targets = (
        [chat_id]
        if chat_id is not None
        else auto_chats()
    )

    for target in targets:
        try:
            await bot.send_message(
                target,
                message_text,
                parse_mode="HTML"
            )
        except Exception as exc:
            log.warning(
                "Не удалось отправить сигнал в %s: %s",
                target,
                exc
            )

# ----------------------------- Demo tracking -----------------------------

async def monitor_open_signals(
    session: aiohttp.ClientSession
) -> None:

    with db() as con:
        rows = con.execute("""
            SELECT *
            FROM signals
            WHERE status='OPEN'
            ORDER BY id
            LIMIT 200
        """).fetchall()

    grouped: dict[
        tuple[str, str],
        list[sqlite3.Row]
    ] = {}

    for row in rows:
        grouped.setdefault(
            (row["market"], row["symbol"]),
            []
        ).append(row)

    for (market, symbol), group_rows in grouped.items():

        try:
            # Получаем свечи один раз на пару и рынок.
            df = await get_klines(
                session,
                market,
                symbol,
                "5m",
                limit=300,
                min_rows=2
            )

            oldest_available = int(df.open_time.iloc[0])

            for row in group_rows:

                created_ms = int(
                    datetime.fromisoformat(
                        row["created_at"]
                    ).timestamp() * 1000
                )

                first_open = (
                    created_ms // TF_MS["5m"]
                ) * TF_MS["5m"] + TF_MS["5m"]

                last_ms = int(
                    row["last_checked_ms"] or 0
                )

                # Если история сигнала старше загруженных свечей,
                # нельзя честно определить результат.
                if (
                    last_ms == 0
                    and first_open < oldest_available
                ):
                    log.warning(
                        "Недостаточно истории для старого сигнала #%s",
                        row["id"]
                    )
                    continue

                future = df[
                    (df.open_time >= first_open)
                    & (df.open_time > last_ms)
                ]

                result = None
                result_r = None

                for candle in future.itertuples():

                    if row["side"] == "BUY":
                        hit_sl = candle.low <= row["sl"]
                        hit_tp = candle.high >= row["tp"]
                    else:
                        hit_sl = candle.high >= row["sl"]
                        hit_tp = candle.low <= row["tp"]

                    # Если TP и SL внутри одной свечи,
                    # считаем результат консервативно как LOSS.
                    if hit_sl and hit_tp:
                        result = "LOSS"
                        result_r = -1.0

                    elif hit_sl:
                        result = "LOSS"
                        result_r = -1.0

                    elif hit_tp:
                        result = "WIN"
                        result_r = float(row["rr"])

                    last_ms = int(candle.open_time)

                    if result:
                        break

                with db() as con:

                    if result:
                        con.execute("""
                            UPDATE signals
                            SET status=?,
                                result_r=?,
                                closed_at=?,
                                last_checked_ms=?
                            WHERE id=?
                              AND status='OPEN'
                        """, (
                            result,
                            result_r,
                            datetime.now(
                                timezone.utc
                            ).isoformat(timespec="seconds"),
                            last_ms,
                            row["id"]
                        ))

                        log.info(
                            "Demo result: signal #%s %s",
                            row["id"],
                            result
                        )

                    elif last_ms > int(
                        row["last_checked_ms"] or 0
                    ):
                        con.execute("""
                            UPDATE signals
                            SET last_checked_ms=?
                            WHERE id=?
                              AND status='OPEN'
                        """, (
                            last_ms,
                            row["id"]
                        ))

        except Exception as exc:
            for row in group_rows:
                log.warning(
                    "Ошибка мониторинга сигнала #%s "
                    "(%s %s): %s",
                    row["id"],
                    market,
                    symbol,
                    exc
                )


# ----------------------------- Scanning ----------------------------------

async def scan_market(
    session: aiohttp.ClientSession,
    market: str,
    symbol: str
) -> dict[str, Any] | None:

    frames = {}

    for tf in ("5m", "15m", "1h", "4h"):
        frames[tf] = await get_klines(
            session,
            market,
            symbol,
            tf,
            limit=300,
            min_rows=205
        )

    return build_signal(
        market,
        symbol,
        frames
    )


async def scan_all(
    bot: Bot,
    session: aiohttp.ClientSession,
    notify_chat: int | None = None
) -> list[str]:

    found = []

    if scan_lock.locked():
        return [
            "Сканирование уже выполняется. "
            "Подожди завершения."
        ]

    async with scan_lock:

        for market in MARKETS:

            for symbol in SYMBOLS:

                try:
                    sig = await scan_market(
                        session,
                        market,
                        symbol
                    )

                    if sig:
                        sid = save_signal(sig)

                        if sid:
                            found.append(
                                f"{symbol} {market.upper()} "
                                f"{sig['side']} (#{sid})"
                            )

                            if notify_chat is not None:
                                await send_signal(
                                    bot,
                                    sig,
                                    sid,
                                    notify_chat
                                )
                            else:
                                await send_signal(
                                    bot,
                                    sig,
                                    sid
                                )

                except asyncio.CancelledError:
                    raise

                except Exception as exc:
                    log.warning(
                        "Scan error %s %s: %s",
                        market,
                        symbol,
                        exc
                    )

                # Небольшая пауза между парами.
                await asyncio.sleep(0.5)

    return found


async def run_manual_scan(
    bot: Bot,
    session: aiohttp.ClientSession,
    chat_id: int,
    status_message: Message
) -> None:

    try:
        found = await scan_all(
            bot,
            session,
            notify_chat=chat_id
        )

        if found:
            result_text = (
                "✅ <b>Сканирование завершено.</b>\n\n"
                + "\n".join(found)
            )
        else:
            result_text = (
                "✅ <b>Сканирование завершено.</b>\n\n"
                "Подходящих условий пока нет. "
                "Это нормально — бот не должен "
                "выдавать сигнал любой ценой."
            )

        await status_message.edit_text(
            result_text,
            parse_mode="HTML"
        )

    except asyncio.CancelledError:
        raise

    except Exception as exc:
        log.exception("Manual scan failed")

        try:
            await status_message.edit_text(
                "❌ Ошибка сканирования:\n"
                + str(exc)[:1000]
            )
        except Exception:
            pass


# ----------------------------- Keyboard ----------------------------------

def keyboard() -> ReplyKeyboardMarkup:

    builder = ReplyKeyboardBuilder()

    labels = (
        "📊 Анализ рынка",
        "🔔 Включить авто",
        "🔕 Выключить авто",
        "📈 Статистика",
        "📜 История сигналов",
        "🧠 Демо-обучение",
        "⚙️ Настройки",
        "ℹ️ Помощь"
    )

    for label in labels:
        builder.add(
            KeyboardButton(text=label)
        )

    builder.adjust(2, 2, 2, 2)

    return builder.as_markup(
        resize_keyboard=True
    )


# ----------------------------- Statistics --------------------------------

async def stats_text() -> str:

    with db() as con:

        total = con.execute(
            "SELECT COUNT(*) n FROM signals"
        ).fetchone()["n"]

        wins = con.execute(
            "SELECT COUNT(*) n FROM signals WHERE status='WIN'"
        ).fetchone()["n"]

        losses = con.execute(
            "SELECT COUNT(*) n FROM signals WHERE status='LOSS'"
        ).fetchone()["n"]

        opened = con.execute(
            "SELECT COUNT(*) n FROM signals WHERE status='OPEN'"
        ).fetchone()["n"]

        avg = con.execute("""
            SELECT AVG(result_r) v
            FROM signals
            WHERE status IN ('WIN','LOSS')
        """).fetchone()["v"]

        by_market = con.execute("""
            SELECT
                market,
                COUNT(*) n,
                SUM(status='WIN') w,
                SUM(status='LOSS') l
            FROM signals
            WHERE status IN ('WIN','LOSS')
            GROUP BY market
        """).fetchall()

    closed = wins + losses

    win_rate = (
        wins / closed * 100
        if closed
        else 0
    )

    lines = [
        "📈 <b>СТАТИСТИКА v9.1</b>",
        f"Всего сигналов: <b>{total}</b>",
        f"Закрыто: <b>{closed}</b> | В работе: <b>{opened}</b>",
        f"Победы: <b>{wins}</b> | Убытки: <b>{losses}</b>",
        f"Win Rate: <b>{win_rate:.1f}%</b>",
        (
            f"Средний результат: <b>{avg:.2f}R</b>"
            if avg is not None
            else "Средний результат: пока нет данных"
        )
    ]

    for row in by_market:
        lines.append(
            f"{row['market'].upper()}: "
            f"{row['w']}W / {row['l']}L"
        )

    lines.append(
        "\n<i>Это статистика демо-мониторинга, "
        "а не реальная доходность.</i>"
    )

    return "\n".join(lines)


async def history_text() -> str:

    with db() as con:
        rows = con.execute("""
            SELECT
                id,
                created_at,
                market,
                symbol,
                side,
                status,
                rr
            FROM signals
            ORDER BY id DESC
            LIMIT 12
        """).fetchall()

    if not rows:
        return (
            "Пока сигналов нет. "
            "Нажми «📊 Анализ рынка»."
        )

    lines = [
        "📜 <b>ПОСЛЕДНИЕ СИГНАЛЫ</b>"
    ]

    for row in rows:

        dt = row["created_at"].replace(
            "T", " "
        )[:16]

        lines.append(
            f"#{row['id']} {row['symbol']} "
            f"{row['market'].upper()} {row['side']} "
            f"— <b>{row['status']}</b> "
            f"· RR 1:{row['rr']:.1f} "
            f"· {dt} UTC"
        )

    return "\n".join(lines)


async def help_text() -> str:

    return (
        "ℹ️ <b>Как работает бот</b>\n\n"
        "📊 Анализ рынка — ручной запуск сканирования.\n"
        "🔔 Включить авто — присылать новые найденные сигналы.\n"
        "🔕 Выключить авто — отключить рассылку.\n"
        "📈 Статистика — результаты демо-наблюдения.\n"
        "📜 История — последние сигналы.\n"
        "🧠 Демо-обучение — пояснение режима.\n"
        "⚙️ Настройки — текущая конфигурация.\n\n"
        "Бот не открывает сделки, не использует "
        "торговые ключи и не гарантирует прибыль."
    )

# ----------------------------- Telegram UI --------------------------------

async def main() -> None:

    global scan_task

    init_db()

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()

    timeout = aiohttp.ClientTimeout(total=25)

    connector = aiohttp.TCPConnector(
        limit=10,
        ttl_dns_cache=300
    )

    session = aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
        headers={
            "User-Agent": "TradingBot-v9.1"
        }
    )

    @dp.message(CommandStart())
    async def start(message: Message):

        if not allowed(message.from_user.id):
            return

        with db() as con:
            con.execute("""
                INSERT OR IGNORE INTO subscribers(
                    chat_id,
                    auto_send
                )
                VALUES(?, 0)
            """, (message.chat.id,))

        await message.answer(
            "🤖 <b>Trading Bot v9.1 запущен</b>\n\n"
            "Анализ BingX Spot и USDT-M Futures.\n"
            "Сделки открываются только вручную.\n\n"
            "Выбери действие в меню ниже.",
            reply_markup=keyboard(),
            parse_mode="HTML"
        )

    @dp.message(Command("scan"))
    async def cmd_scan(message: Message):

        global scan_task

        if not allowed(message.from_user.id):
            return

        if scan_lock.locked():
            await message.answer(
                "⏳ Сканирование уже выполняется. "
                "Подожди его завершения."
            )
            return

        status = await message.answer(
            "🔎 <b>Сканирование запущено.</b>\n"
            "Бот проверяет рынок. Результат придёт "
            "отдельным сообщением после завершения.",
            parse_mode="HTML"
        )

        scan_task = asyncio.create_task(
            run_manual_scan(
                bot,
                session,
                message.chat.id,
                status
            )
        )

    @dp.message(Command("auto_on"))
    async def cmd_on(message: Message):

        if not allowed(message.from_user.id):
            return

        set_auto(message.chat.id, True)

        await message.answer(
            "🔔 Автоматическая отправка включена.",
            reply_markup=keyboard()
        )

    @dp.message(Command("auto_off"))
    async def cmd_off(message: Message):

        if not allowed(message.from_user.id):
            return

        set_auto(message.chat.id, False)

        await message.answer(
            "🔕 Автоматическая отправка выключена.",
            reply_markup=keyboard()
        )

    @dp.message(F.text)
    async def buttons(message: Message):

        global scan_task

        if not allowed(message.from_user.id):
            return

        text_value = (message.text or "").strip()

        if text_value == "📊 Анализ рынка":

            if scan_lock.locked():
                await message.answer(
                    "⏳ Сканирование уже выполняется. "
                    "Подожди его завершения."
                )
                return

            status = await message.answer(
                "🔎 <b>Сканирование запущено.</b>\n"
                "Бот проверяет рынок. Результат придёт "
                "отдельным сообщением после завершения.",
                parse_mode="HTML"
            )

            scan_task = asyncio.create_task(
                run_manual_scan(
                    bot,
                    session,
                    message.chat.id,
                    status
                )
            )

        elif text_value == "🔔 Включить авто":

            set_auto(message.chat.id, True)

            await message.answer(
                "🔔 Автоматическая отправка включена.",
                reply_markup=keyboard()
            )

        elif text_value == "🔕 Выключить авто":

            set_auto(message.chat.id, False)

            await message.answer(
                "🔕 Автоматическая отправка выключена.",
                reply_markup=keyboard()
            )

        elif text_value == "📈 Статистика":

            await message.answer(
                await stats_text(),
                parse_mode="HTML"
            )

        elif text_value == "📜 История сигналов":

            await message.answer(
                await history_text(),
                parse_mode="HTML"
            )

        elif text_value == "🧠 Демо-обучение":

            await message.answer(
                "🧠 <b>Демо-режим</b>\n\n"
                "Бот записывает сигналы и автоматически "
                "проверяет, достигла ли цена TP или SL "
                "по закрытым 5-минутным свечам.\n\n"
                "Если обе границы попали в одну свечу, "
                "результат считается консервативно как LOSS.\n\n"
                "Статистика сохраняется в SQLite.\n"
                "Автоматическое изменение параметров "
                "стратегии отключено.",
                parse_mode="HTML"
            )

        elif text_value == "⚙️ Настройки":

            await message.answer(
                "⚙️ <b>Настройки Trading Bot v9.1</b>\n\n"
                f"Рынки: {', '.join(MARKETS)}\n"
                f"Пары: {', '.join(SYMBOLS)}\n"
                "Таймфреймы: 5м / 15м / 1ч / 4ч\n"
                f"Минимальный RR: 1:{MIN_RR}\n"
                f"Порог силы модели: {MIN_SCORE}/10\n"
                f"Интервал авто-сканирования: "
                f"{SCAN_INTERVAL // 60} мин\n"
                f"Интервал демо-мониторинга: "
                f"{MONITOR_INTERVAL} сек\n"
                f"База данных: {DB_PATH}\n\n"
                "Реальные ордера отключены.",
                parse_mode="HTML"
            )

        elif text_value == "ℹ️ Помощь":

            await message.answer(
                await help_text(),
                parse_mode="HTML"
            )

    # ------------------------- Background scanner -------------------------

    async def background_scanner():

        await asyncio.sleep(15)

        while True:

            try:
                log.info("Starting scheduled market scan")

                await scan_all(
                    bot,
                    session
                )

            except asyncio.CancelledError:
                raise

            except Exception:
                log.exception(
                    "Background scan failed"
                )

            await asyncio.sleep(
                SCAN_INTERVAL
            )

    # ------------------------- Background monitor -------------------------

    async def background_monitor():

        while True:

            try:
                await monitor_open_signals(
                    session
                )

            except asyncio.CancelledError:
                raise

            except Exception:
                log.exception(
                    "Demo monitor failed"
                )

            await asyncio.sleep(
                MONITOR_INTERVAL
            )

    tasks = [
        asyncio.create_task(
            background_scanner()
        ),
        asyncio.create_task(
            background_monitor()
        )
    ]

    try:

        log.info(
            "Trading Bot v9.1 started | symbols=%s | "
            "markets=%s | database=%s",
            SYMBOLS,
            MARKETS,
            DB_PATH
        )

        await dp.start_polling(bot)

    finally:

        for task in tasks:
            task.cancel()

        if scan_task and not scan_task.done():
            scan_task.cancel()

        await asyncio.gather(
            *tasks,
            return_exceptions=True
        )

        if scan_task:
            await asyncio.gather(
                scan_task,
                return_exceptions=True
            )

        await session.close()
        await bot.session.close()


# ----------------------------- Entry point --------------------------------

if __name__ == "__main__":

    try:
        asyncio.run(main())

    except (KeyboardInterrupt, SystemExit):
        pass
