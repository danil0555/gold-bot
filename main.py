# ============================================================
# TRADING BOT v9.2 (FIXED)
# FULL SINGLE-FILE EDITION
# ============================================================

import os
import time
import math
import asyncio
import logging
import sqlite3

from pathlib import Path
from datetime import datetime, timezone
from contextlib import contextmanager
from typing import Any

import aiohttp
import numpy as np
import pandas as pd

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode


# ============================================================
# 1. ENVIRONMENT
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

if not BOT_TOKEN:
    raise RuntimeError(
        "Не найден BOT_TOKEN. Добавь токен в файл .env"
    )


# ============================================================
# 2. LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("TradingBot")


# ============================================================
# 3. GENERAL SETTINGS
# ============================================================

BOT_NAME = "Trading Bot"
BOT_VERSION = "9.2"

BASE = "https://open-api.bingx.com"

DB_PATH = Path(
    os.getenv("DB_PATH", "trading_bot_v9.db")
)

DB_PATH.parent.mkdir(
    parents=True,
    exist_ok=True
)

MARKETS = ("spot", "swap")

TIMEFRAMES = (
    "5m",
    "15m",
    "1h",
    "4h"
)

TF_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000
}

MIN_SCORE = 6
MIN_RR = 1.0

TP1_R = 1.8
TP2_R = 3.0

RISK_PERCENT = 2.0
FUTURES_LEVERAGE = 5

TP1_CLOSE_PERCENT = 70.0
TP2_CLOSE_PERCENT = 30.0

MAX_ACTIVE_TRADES = 5

KLINE_LIMIT = 300
MIN_CANDLE_ROWS = 205

MARKET_SCAN_INTERVAL = 60
TRADE_MONITOR_INTERVAL = 15

SIGNAL_COOLDOWN_MINUTES = 45

REAL_TRADING_ENABLED = False


# ============================================================
# 4. MARKET SYMBOLS
# ============================================================

SYMBOLS = [
    "BTC-USDT",
    "ETH-USDT",
    "BNB-USDT",
    "SOL-USDT",
    "XRP-USDT",
    "DOGE-USDT",
    "ADA-USDT",
    "AVAX-USDT",
    "LINK-USDT",
    "LTC-USDT",
    "TRX-USDT",
    "DOT-USDT",
    "BCH-USDT",
    "SUI-USDT",
    "TON-USDT"
]


# ============================================================
# 5. GLOBAL OBJECTS
# ============================================================

bot: Bot | None = None
dp = Dispatcher()
router = Router()

http_session: aiohttp.ClientSession | None = None

_api_rate_lock = asyncio.Lock()

_last_api_request = 0.0
_api_cooldown_until = 0.0

candle_cache: dict[
    tuple[str, str, str],
    pd.DataFrame
] = {}

latest_prices: dict[
    tuple[str, str],
    float
] = {}

system_status = {
    "started": False,
    "last_scan": None,
    "last_error": None,
    "api_status": "Не проверено",
    "scan_count": 0,
    "signals_count": 0
}

background_tasks: list[asyncio.Task] = []


# ============================================================
# 6. TIME AND FORMAT HELPERS
# ============================================================

def now_ms() -> int:
    return int(time.time() * 1000)


def utc_text() -> str:
    return datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d %H:%M:%S")


def safe_float(
    value: Any,
    default: float = 0.0
) -> float:

    try:
        result = float(value)

        if not math.isfinite(result):
            return default

        return result

    except (TypeError, ValueError, OverflowError):
        return default


def format_price(value: Any) -> str:
    price = safe_float(value)

    if price <= 0:
        return "—"

    if price >= 1000:
        return f"{price:,.2f}"

    if price >= 1:
        return f"{price:.4f}"

    return f"{price:.8f}".rstrip("0").rstrip(".")


def format_percent(value: Any) -> str:
    return f"{safe_float(value):.2f}%"


def market_name(market: str) -> str:
    return {
        "spot": "SPOT",
        "swap": "USDT-M Futures"
    }.get(str(market).lower(), str(market).upper())


def side_name(side: str) -> str:
    return {
        "BUY": "LONG / BUY",
        "LONG": "LONG / BUY",
        "SELL": "SHORT / SELL",
        "SHORT": "SHORT / SELL"
    }.get(str(side).upper(), str(side))


def is_long_side(side: str) -> bool:
    return str(side).strip().lower() in ("buy", "long")


# ============================================================
# 7. DATABASE CONNECTION
# ============================================================

@contextmanager
def db():

    con = sqlite3.connect(
        str(DB_PATH),
        timeout=30
    )

    con.row_factory = sqlite3.Row

    try:
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 30000")

        yield con

        con.commit()

    except Exception:
        con.rollback()
        raise

    finally:
        con.close()


def table_columns(
    con: sqlite3.Connection,
    table: str
) -> set[str]:

    rows = con.execute(
        f'PRAGMA table_info("{table}")'
    ).fetchall()

    return {
        row["name"]
        for row in rows
    }


def ensure_column(
    con: sqlite3.Connection,
    table: str,
    column: str,
    definition: str
) -> None:

    columns = table_columns(con, table)

    if column not in columns:
        con.execute(
            f'ALTER TABLE "{table}" '
            f'ADD COLUMN "{column}" {definition}'
        )

        log.info(
            "Добавлена колонка %s.%s",
            table,
            column
        )


# ============================================================
# 8. DATABASE INITIALIZATION
# ============================================================

def init_db() -> None:

    with db() as con:

        con.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                created_at TEXT,
                is_active INTEGER DEFAULT 1
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                risk_percent REAL DEFAULT 2.0,
                leverage INTEGER DEFAULT 5,
                notifications INTEGER DEFAULT 1,
                FOREIGN KEY(user_id)
                    REFERENCES users(user_id)
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT,
                market TEXT,
                symbol TEXT,
                side TEXT,
                entry REAL,
                sl REAL,
                tp REAL,
                rr REAL,
                score INTEGER,
                reasons TEXT,
                setup_key TEXT,
                status TEXT DEFAULT 'NEW',
                result TEXT
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                signal_id INTEGER,
                market TEXT,
                symbol TEXT,
                side TEXT,
                entry REAL,
                actual_entry REAL,
                current_price REAL,
                sl REAL,
                tp1 REAL,
                tp2 REAL,
                initial_sl REAL,
                risk_amount REAL,
                remaining_percent REAL DEFAULT 100,
                tp1_hit INTEGER DEFAULT 0,
                tp2_hit INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active',
                last_recommendation TEXT,
                opened_at TEXT,
                closed_at TEXT,
                updated_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(user_id),
                FOREIGN KEY(signal_id) REFERENCES signals(id)
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS trade_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INTEGER,
                user_id INTEGER,
                market TEXT,
                symbol TEXT,
                side TEXT,
                entry REAL,
                exit_price REAL,
                result TEXT,
                pnl_percent REAL DEFAULT 0,
                pnl_r REAL DEFAULT 0,
                close_reason TEXT,
                opened_at TEXT,
                closed_at TEXT
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS market_candles (
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                interval TEXT NOT NULL,
                open_time INTEGER NOT NULL,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume REAL,
                PRIMARY KEY (
                    market,
                    symbol,
                    interval,
                    open_time
                )
            )
        """)

        migrations = {
            "signals": {
                "created_at": "TEXT",
                "market": "TEXT",
                "symbol": "TEXT",
                "side": "TEXT",
                "entry": "REAL",
                "sl": "REAL",
                "tp": "REAL",
                "rr": "REAL",
                "score": "INTEGER",
                "reasons": "TEXT",
                "setup_key": "TEXT",
                "status": "TEXT DEFAULT 'NEW'",
                "result": "TEXT"
            },

            "trades": {
                "user_id": "INTEGER",
                "signal_id": "INTEGER",
                "market": "TEXT",
                "symbol": "TEXT",
                "side": "TEXT",
                "entry": "REAL",
                "actual_entry": "REAL",
                "current_price": "REAL",
                "sl": "REAL",
                "tp1": "REAL",
                "tp2": "REAL",
                "initial_sl": "REAL",
                "risk_amount": "REAL",
                "remaining_percent": "REAL DEFAULT 100",
                "tp1_hit": "INTEGER DEFAULT 0",
                "tp2_hit": "INTEGER DEFAULT 0",
                "status": "TEXT DEFAULT 'active'",
                "last_recommendation": "TEXT",
                "opened_at": "TEXT",
                "closed_at": "TEXT",
                "updated_at": "TEXT"
            },

            "trade_history": {
                "trade_id": "INTEGER",
                "user_id": "INTEGER",
                "market": "TEXT",
                "symbol": "TEXT",
                "side": "TEXT",
                "entry": "REAL",
                "exit_price": "REAL",
                "result": "TEXT",
                "pnl_percent": "REAL DEFAULT 0",
                "pnl_r": "REAL DEFAULT 0",
                "close_reason": "TEXT",
                "opened_at": "TEXT",
                "closed_at": "TEXT"
            },

            "users": {
                "username": "TEXT",
                "created_at": "TEXT",
                "is_active": "INTEGER DEFAULT 1"
            }
        }

        for table, columns in migrations.items():
            for column, definition in columns.items():
                ensure_column(con, table, column, definition)

        # Нормализуем регистр статуса активных сделок для совместимости со старыми записями
        con.execute("""
            UPDATE trades
            SET status = LOWER(status)
            WHERE status IN ('ACTIVE', 'OPEN', 'CLOSED')
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_signals_created
            ON signals(created_at)
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_signals_symbol
            ON signals(market, symbol, side)
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_trades_user_status
            ON trades(user_id, status)
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_history_user
            ON trade_history(user_id, closed_at)
        """)

    log.info(
        "База данных готова: %s",
        DB_PATH
    )


# ============================================================
# 10. BINGX PUBLIC REST API
# ============================================================

class MarketError(Exception):
    pass


async def api_get(
    session: aiohttp.ClientSession,
    path: str,
    params: dict[str, Any]
) -> Any:

    global _last_api_request
    global _api_cooldown_until

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
                            f"HTTP {response.status}: "
                            f"{raw_text[:250]}"
                        )

                    try:
                        payload = await response.json(
                            content_type=None
                        )

                    except Exception as exc:
                        raise MarketError(
                            "Некорректный JSON BingX: "
                            + raw_text[:250]
                        ) from exc

                    if isinstance(payload, dict):

                        code = payload.get("code")

                        if code not in (None, 0, "0"):

                            error_text = (
                                f"BingX {code}: "
                                f"{payload.get('msg', '')}"
                            )

                            if str(code) in (
                                "100410",
                                "100429"
                            ):

                                _api_cooldown_until = (
                                    time.monotonic()
                                    + min(
                                        30,
                                        3 * (2 ** attempt)
                                    )
                                )

                                last_error = MarketError(
                                    error_text
                                )

                            else:
                                raise MarketError(
                                    error_text
                                )

                        else:
                            system_status["api_status"] = "OK"
                            return payload

                    else:
                        system_status["api_status"] = "OK"
                        return payload

            if last_error is not None:

                if attempt < 3:
                    log.debug(
                        "Ограничение BingX API. "
                        "Повтор %s/3",
                        attempt + 1
                    )

                    await asyncio.sleep(
                        min(2 ** attempt, 8)
                    )

                    continue

                raise last_error

        except asyncio.CancelledError:
            raise

        except MarketError as exc:
            system_status["api_status"] = f"Ошибка ({exc})"
            system_status["last_error"] = str(exc)
            raise

        except (
            aiohttp.ClientError,
            asyncio.TimeoutError
        ) as exc:

            last_error = MarketError(str(exc))
            system_status["api_status"] = "Ошибка сети"
            system_status["last_error"] = str(exc)

            if attempt < 3:
                await asyncio.sleep(
                    min(2 ** attempt, 8)
                )
                continue

            raise last_error from exc

    raise last_error or MarketError(
        "Неизвестная ошибка BingX API"
    )


def extract_data(payload: Any) -> Any:

    if isinstance(payload, dict):

        return payload.get(
            "data",
            payload.get("result", payload)
        )

    return payload


# ============================================================
# 11. CANDLE NORMALIZATION
# ============================================================

def normalize_candles(
    payload: Any,
    interval: str,
    min_rows: int = MIN_CANDLE_ROWS
) -> pd.DataFrame:

    if interval not in TF_MS:
        raise MarketError(
            f"Неизвестный таймфрейм: {interval}"
        )

    raw = extract_data(payload)

    if isinstance(raw, dict):

        raw = raw.get(
            "data",
            raw.get(
                "list",
                raw.get("rows", [])
            )
        )

    if not isinstance(raw, list) or not raw:

        raise MarketError(
            "BingX вернула пустой список свечей"
        )

    rows = []

    for item in raw:

        try:

            if isinstance(item, (list, tuple)):

                if len(item) < 6:
                    continue

                ot = int(float(item[0]))

                o, h, l, c, v = map(
                    float,
                    item[1:6]
                )

                ct = (
                    int(float(item[6]))
                    if len(item) > 6
                    and item[6] not in (None, "")
                    else ot + TF_MS[interval] - 1
                )

            elif isinstance(item, dict):

                ot = int(float(
                    item.get(
                        "openTime",
                        item.get(
                            "time",
                            item.get("timestamp", 0)
                        )
                    )
                ))

                o, h, l, c, v = (
                    float(item[k])
                    for k in (
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume"
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

            if ot < 10_000_000_000:
                ot *= 1000

            if ct < 10_000_000_000:
                ct *= 1000

            if ot <= 0:
                continue

            if not all(
                np.isfinite(value) and value > 0
                for value in (o, h, l, c)
            ):
                continue

            if not np.isfinite(v) or v < 0:
                continue

            if h < max(o, c):
                continue

            if l > min(o, c):
                continue

            rows.append((
                ot,
                o,
                h,
                l,
                c,
                v,
                ct
            ))

        except (
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            OverflowError
        ):
            continue

    if not rows:
        raise MarketError(
            "Не удалось распознать формат свечей"
        )

    df = pd.DataFrame(
        rows,
        columns=[
            "open_time",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "close_time"
        ]
    )

    df = (
        df.drop_duplicates("open_time")
        .sort_values("open_time")
        .reset_index(drop=True)
    )

    current_ms = now_ms()

    df = df[
        df["close_time"] < current_ms - 2000
    ].reset_index(drop=True)

    if len(df) < min_rows:

        raise MarketError(
            f"Недостаточно закрытых свечей: "
            f"{len(df)}, требуется {min_rows}"
        )

    return df


# ============================================================
# 12. GET MARKET CANDLES
# ============================================================

async def get_klines(
    market: str,
    symbol: str,
    interval: str,
    limit: int = KLINE_LIMIT,
    min_rows: int = MIN_CANDLE_ROWS,
    session: aiohttp.ClientSession | None = None
) -> pd.DataFrame:

    active_session = session or http_session

    if (
        active_session is None
        or active_session.closed
    ):
        raise MarketError(
            "HTTP-сессия BingX ещё не запущена"
        )

    if market == "spot":
        path = "/openApi/spot/v2/market/kline"
    elif market == "swap":
        path = "/openApi/swap/v3/quote/klines"
    else:
        raise MarketError(
            f"Неизвестный рынок: {market}"
        )

    payload = await api_get(
        active_session,
        path,
        {
            "symbol": symbol,
            "interval": interval,
            "limit": limit
        }
    )

    df = normalize_candles(
        payload,
        interval,
        min_rows=min_rows
    )

    candle_cache[
        (market, symbol, interval)
    ] = df.copy()

    if not df.empty:

        latest_prices[
            (market, symbol)
        ] = float(df.iloc[-1]["close"])

    return df


# ============================================================
# 13. SAVE CLOSED CANDLES
# ============================================================

def save_closed_candles(
    market: str,
    symbol: str,
    interval: str,
    df: pd.DataFrame
) -> None:

    if df.empty:
        return

    with db() as con:

        for candle in df.itertuples():

            con.execute("""
                INSERT INTO market_candles(
                    market, symbol, interval, open_time,
                    open, high, low, close, volume
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    market, symbol, interval, open_time
                )
                DO UPDATE SET
                    open=excluded.open,
                    high=excluded.high,
                    low=excluded.low,
                    close=excluded.close,
                    volume=excluded.volume
            """, (
                market,
                symbol,
                interval,
                int(candle.open_time),
                float(candle.open),
                float(candle.high),
                float(candle.low),
                float(candle.close),
                float(candle.volume)
            ))

        con.execute("""
            DELETE FROM market_candles
            WHERE market=?
              AND symbol=?
              AND interval=?
              AND open_time NOT IN (
                  SELECT open_time
                  FROM market_candles
                  WHERE market=?
                    AND symbol=?
                    AND interval=?
                  ORDER BY open_time DESC
                  LIMIT 1500
              )
        """, (
            market, symbol, interval,
            market, symbol, interval
        ))


# ============================================================
# 14. TECHNICAL INDICATORS
# ============================================================

def indicators(
    df: pd.DataFrame
) -> pd.DataFrame:

    x = df.copy()

    close = x["close"]
    high = x["high"]
    low = x["low"]

    x["ema20"] = close.ewm(span=20, adjust=False).mean()
    x["ema50"] = close.ewm(span=50, adjust=False).mean()
    x["ema200"] = close.ewm(span=200, adjust=False).mean()

    delta = close.diff()

    gain = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()

    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - 100 / (1 + rs)

    rsi = rsi.mask((loss == 0) & (gain > 0), 100)
    rsi = rsi.mask((gain == 0) & (loss > 0), 0)
    rsi = rsi.mask((gain == 0) & (loss == 0), 50)

    x["rsi"] = rsi.fillna(50)

    tr = pd.concat(
        [
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs()
        ],
        axis=1
    ).max(axis=1)

    x["atr"] = tr.ewm(alpha=1/14, adjust=False).mean()

    up = high.diff()
    down = -low.diff()

    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)

    atr_safe = x["atr"].replace(0, np.nan)

    plus_di = (
        100
        * plus_dm.ewm(alpha=1/14, adjust=False).mean()
        / atr_safe
    )

    minus_di = (
        100
        * minus_dm.ewm(alpha=1/14, adjust=False).mean()
        / atr_safe
    )

    dx = (
        100
        * (plus_di - minus_di).abs()
        / (plus_di + minus_di).replace(0, np.nan)
    )

    x["adx"] = dx.ewm(alpha=1/14, adjust=False).mean().fillna(0)
    x["plus_di"] = plus_di.fillna(0)
    x["minus_di"] = minus_di.fillna(0)

    x["macd"] = (
        close.ewm(span=12, adjust=False).mean()
        -
        close.ewm(span=26, adjust=False).mean()
    )

    x["macd_signal"] = x["macd"].ewm(span=9, adjust=False).mean()

    return x


# ============================================================
# 15. SIGNAL STRATEGY
# ============================================================

def build_signal(
    market: str,
    symbol: str,
    frames: dict[str, pd.DataFrame]
) -> dict[str, Any] | None:

    required_frames = ("5m", "15m", "1h", "4h")

    if any(
        timeframe not in frames
        or frames[timeframe] is None
        or len(frames[timeframe]) < MIN_CANDLE_ROWS
        for timeframe in required_frames
    ):
        return None

    m5, m15, h1, h4 = (
        indicators(frames[timeframe])
        for timeframe in required_frames
    )

    a = m5.iloc[-1]
    b = m15.iloc[-1]
    c = h1.iloc[-1]
    d = h4.iloc[-1]

    price = safe_float(b["close"])
    atr = safe_float(b["atr"])

    if price <= 0 or atr <= 0:
        return None

    long_score = 0
    short_score = 0

    long_reasons = []
    short_reasons = []

    for row, label in ((c, "1H"), (d, "4H")):

        if row["close"] > row["ema200"] and row["ema20"] > row["ema50"]:
            long_score += 2
            long_reasons.append(f"Тренд {label}: выше EMA200")

        if row["close"] < row["ema200"] and row["ema20"] < row["ema50"]:
            short_score += 2
            short_reasons.append(f"Тренд {label}: ниже EMA200")

    if b["ema20"] > b["ema50"] and b["close"] > b["ema20"]:
        long_score += 2
        long_reasons.append("15м: EMA20 выше EMA50")

    if b["ema20"] < b["ema50"] and b["close"] < b["ema20"]:
        short_score += 2
        short_reasons.append("15м: EMA20 ниже EMA50")

    if 48 <= b["rsi"] <= 68:
        long_score += 1
        long_reasons.append(f"RSI 15м {b['rsi']:.1f}")

    if 32 <= b["rsi"] <= 52:
        short_score += 1
        short_reasons.append(f"RSI 15м {b['rsi']:.1f}")

    if b["macd"] > b["macd_signal"]:
        long_score += 1
        long_reasons.append("MACD 15м положительный")

    if b["macd"] < b["macd_signal"]:
        short_score += 1
        short_reasons.append("MACD 15м отрицательный")

    if b["adx"] >= 18 and b["plus_di"] > b["minus_di"]:
        long_score += 1
        long_reasons.append(
            f"ADX {b['adx']:.1f}, покупатели сильнее"
        )

    if b["adx"] >= 18 and b["minus_di"] > b["plus_di"]:
        short_score += 1
        short_reasons.append(
            f"ADX {b['adx']:.1f}, продавцы сильнее"
        )

    if a["close"] > a["ema20"]:
        long_score += 1
        long_reasons.append("5м: цена выше EMA20")

    if a["close"] < a["ema20"]:
        short_score += 1
        short_reasons.append("5м: цена ниже EMA20")

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

    if market == "spot" and side == "SELL":
        return None

    sl_dist = max(1.5 * atr, price * 0.002)
    tp1_dist = sl_dist * TP1_R
    tp2_dist = sl_dist * TP2_R

    if side == "BUY":
        sl = price - sl_dist
        tp1 = price + tp1_dist
        tp2 = price + tp2_dist
    else:
        sl = price + sl_dist
        tp1 = price - tp1_dist
        tp2 = price - tp2_dist

    if min(sl, tp1, tp2) <= 0:
        return None

    rr = abs(tp2 - price) / abs(price - sl)

    if rr < MIN_RR:
        return None

    return {
        "market": market,
        "symbol": symbol,
        "side": side,
        "entry": price,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp": tp2,
        "rr": rr,
        "score": score,
        "reasons": reasons,
        "atr": atr,
        "setup_key": (
            f"{market}:{symbol}:{side}:"
            f"{int(b['open_time'])}"
        )
    }


# ============================================================
# 16. SIGNAL STORAGE
# ============================================================

def save_signal(
    sig: dict[str, Any]
) -> int | None:

    now = utc_text()

    reasons_val = sig.get("reasons", "")
    if isinstance(reasons_val, (list, tuple)):
        reasons_text = "; ".join(str(item) for item in reasons_val)
    else:
        reasons_text = str(reasons_val)

    with db() as con:

        columns = table_columns(con, "signals")

        if "setup_key" in columns:
            exists = con.execute("""
                SELECT id FROM signals
                WHERE setup_key=?
                LIMIT 1
            """, (sig["setup_key"],)).fetchone()

            if exists:
                return None

        recent = con.execute(f"""
            SELECT id FROM signals
            WHERE market=?
              AND symbol=?
              AND side=?
              AND julianday(created_at)
                  >= julianday('now', '-{SIGNAL_COOLDOWN_MINUTES} minutes')
            ORDER BY id DESC
            LIMIT 1
        """, (
            sig["market"],
            sig["symbol"],
            sig["side"]
        )).fetchone()

        if recent:
            return None

        values = {
            "created_at": now,
            "market": sig["market"],
            "symbol": sig["symbol"],
            "side": sig["side"],
            "entry": sig["entry"],
            "sl": sig["sl"],
            "tp": sig["tp2"],
            "rr": sig["rr"],
            "score": sig["score"],
            "reasons": reasons_text,
            "setup_key": sig["setup_key"],
            "status": "NEW"
        }

        available = [
            column for column in values
            if column in columns
        ]

        if not available:
            raise RuntimeError(
                "В таблице signals отсутствуют поддерживаемые колонки"
            )

        names_sql = ", ".join(f'"{column}"' for column in available)
        placeholders = ", ".join("?" for _ in available)

        sql = (
            f"INSERT INTO signals ({names_sql}) "
            f"VALUES ({placeholders})"
        )

        cur = con.execute(sql, tuple(values[column] for column in available))

        return int(cur.lastrowid)


# ============================================================
# 17. TELEGRAM KEYBOARDS
# ============================================================

def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔎 Анализ рынка")],
            [KeyboardButton(text="📊 Мои сделки"),
             KeyboardButton(text="📚 История")],
            [KeyboardButton(text="📈 Статистика"),
             KeyboardButton(text="⚙️ Настройки")],
            [KeyboardButton(text="🖥 Статус системы")],
        ],
        resize_keyboard=True
    )


def signal_keyboard(signal_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Я вошёл в сделку",
                    callback_data=f"enter:{signal_id}"
                )
            ]
        ]
    )


def trade_keyboard(trade_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text="🔄 Обновить",
                callback_data=f"trade_refresh:{trade_id}"
            )],
            [InlineKeyboardButton(
                text="✏️ Исправить цену входа",
                callback_data=f"trade_edit_entry:{trade_id}"
            )],
            [InlineKeyboardButton(
                text="🛑 Обновить стоп-лосс",
                callback_data=f"trade_edit_sl:{trade_id}"
            )],
            [InlineKeyboardButton(
                text="Закрыть сделку",
                callback_data=f"trade_close:{trade_id}"
            )],
        ]
    )


def confirm_keyboard(action, trade_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, подтверждаю",
                    callback_data=f"{action}_confirm:{trade_id}"
                ),
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data=f"{action}_cancel:{trade_id}"
                )
            ]
        ]
    )


# ============================================================
# 18. USER STATES AND DATABASE HELPERS
# ============================================================

user_states: dict[int, dict[str, Any]] = {}


def row_to_dict(row):
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    try:
        return dict(row)
    except (TypeError, ValueError):
        return None


def ensure_part3_schema():
    """
    Добавляет необходимые поля и таблицы.
    Существующие записи не удаляются.
    ВАЖНО: вызывать только после init_db().
    """
    with db() as conn:
        conn.row_factory = sqlite3.Row

        # ----------------------------------------------------
        # USER BALANCES (миграция старой схемы)
        # ----------------------------------------------------
        table_exists = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='user_balances'"
        ).fetchone() is not None

        if table_exists:
            existing_cols = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(user_balances)"
                ).fetchall()
            }

            if "market" not in existing_cols:
                log.info(
                    "Миграция user_balances: старая схема без 'market' → перестройка"
                )

                conn.execute(
                    "ALTER TABLE user_balances RENAME TO user_balances_old"
                )

                conn.execute("""
                    CREATE TABLE user_balances (
                        user_id INTEGER NOT NULL,
                        market TEXT NOT NULL,
                        balance REAL NOT NULL DEFAULT 0,
                        updated_at TEXT,
                        PRIMARY KEY (user_id, market)
                    )
                """)

                if "balance" in existing_cols:
                    conn.execute("""
                        INSERT INTO user_balances
                            (user_id, market, balance, updated_at)
                        SELECT
                            user_id,
                            'spot',
                            COALESCE(balance, 0),
                            COALESCE(updated_at, ?)
                        FROM user_balances_old
                    """, (utc_text(),))

                conn.execute("DROP TABLE user_balances_old")
                log.info(
                    "user_balances: миграция завершена, старые балансы перенесены в SPOT"
                )

            else:
                for column, definition in {
                    "balance": "REAL NOT NULL DEFAULT 0",
                    "updated_at": "TEXT",
                }.items():
                    if column not in existing_cols:
                        conn.execute(
                            f"ALTER TABLE user_balances "
                            f"ADD COLUMN {column} {definition}"
                        )
                        log.info(
                            "user_balances: добавлена колонка %s",
                            column
                        )

        else:
            conn.execute("""
                CREATE TABLE user_balances (
                    user_id INTEGER NOT NULL,
                    market TEXT NOT NULL,
                    balance REAL NOT NULL DEFAULT 0,
                    updated_at TEXT,
                    PRIMARY KEY (user_id, market)
                )
            """)

        # ----------------------------------------------------
        # TRADE EVENTS (миграция старой схемы)
        # ----------------------------------------------------
        trade_events_exists = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='trade_events'"
        ).fetchone() is not None

        if trade_events_exists:
            te_cols = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(trade_events)"
                ).fetchall()
            }

            required_te_columns = {
                "trade_id": "INTEGER NOT NULL DEFAULT 0",
                "user_id": "INTEGER NOT NULL DEFAULT 0",
                "event_type": "TEXT NOT NULL DEFAULT 'legacy'",
                "message": "TEXT",
                "price": "REAL",
                "created_at": "TEXT",
            }

            for column, definition in required_te_columns.items():
                if column not in te_cols:
                    conn.execute(
                        f"ALTER TABLE trade_events "
                        f"ADD COLUMN {column} {definition}"
                    )
                    log.info(
                        "trade_events: добавлена колонка %s",
                        column
                    )
        else:
            conn.execute("""
                CREATE TABLE trade_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    message TEXT,
                    price REAL,
                    created_at TEXT
                )
            """)

        # ----------------------------------------------------
        # TRADES — миграции
        # ----------------------------------------------------
        existing_tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }

        if "trades" not in existing_tables:
            log.warning(
                "ensure_part3_schema: таблица trades не найдена. "
                "Схема Part 3 будет применена после init_db()."
            )
            return

        required_trade_columns = {
            "position_size": "REAL DEFAULT 0",
            "notional": "REAL DEFAULT 0",
            "margin_required": "REAL DEFAULT 0",
            "balance_snapshot": "REAL DEFAULT 0",
            "risk_amount": "REAL DEFAULT 0",
            "initial_sl": "REAL",
            "remaining_percent": "REAL DEFAULT 100",
            "tp1_hit": "INTEGER DEFAULT 0",
            "tp2_hit": "INTEGER DEFAULT 0",
            "last_recommendation": "TEXT",
            "updated_at": "TEXT",
        }

        existing_cols = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(trades)"
            ).fetchall()
        }

        for column, definition in required_trade_columns.items():
            if column not in existing_cols:
                conn.execute(
                    f"ALTER TABLE trades ADD COLUMN {column} {definition}"
                )


def save_trade_event(
    trade_id,
    user_id,
    event_type,
    message="",
    price=None
):
    with db() as conn:
        conn.execute("""
            INSERT INTO trade_events (
                trade_id, user_id, event_type,
                message, price, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            trade_id,
            user_id,
            event_type,
            str(message),
            safe_float(price, 0.0) if price is not None else None,
            utc_text()
        ))


def register_user(user):
    if user is None:
        return

    with db() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO users (
                user_id, username, created_at, is_active
            )
            VALUES (?, ?, ?, 1)
        """, (
            user.id,
            user.username or "",
            utc_text()
        ))

        conn.execute("""
            INSERT OR IGNORE INTO user_settings (
                user_id, risk_percent, leverage, notifications
            )
            VALUES (?, ?, ?, 1)
        """, (
            user.id,
            RISK_PERCENT,
            FUTURES_LEVERAGE
        ))


def set_user_balance(user_id, market, balance):
    market = str(market).lower()

    if market not in ("spot", "swap"):
        raise ValueError("Неизвестный тип рынка")

    balance = safe_float(balance, 0)

    if balance <= 0:
        raise ValueError("Баланс должен быть больше нуля")

    with db() as conn:
        conn.execute("""
            INSERT INTO user_balances (
                user_id, market, balance, updated_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, market)
            DO UPDATE SET
                balance = excluded.balance,
                updated_at = excluded.updated_at
        """, (
            user_id,
            market,
            balance,
            utc_text()
        ))


def get_user_balance(user_id, market):
    with db() as conn:
        conn.row_factory = sqlite3.Row

        row = conn.execute("""
            SELECT balance
            FROM user_balances
            WHERE user_id = ? AND market = ?
        """, (user_id, str(market).lower())).fetchone()

    if not row:
        return 0.0

    return safe_float(row["balance"], 0.0)


def get_active_trades(user_id):
    with db() as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute("""
            SELECT *
            FROM trades
            WHERE user_id = ?
              AND LOWER(COALESCE(status, '')) IN ('active', 'open')
            ORDER BY opened_at DESC, id DESC
        """, (user_id,)).fetchall()

    return [row_to_dict(row) for row in rows]


def get_trade(trade_id, user_id=None):
    with db() as conn:
        conn.row_factory = sqlite3.Row

        if user_id is None:
            row = conn.execute("""
                SELECT * FROM trades WHERE id = ?
            """, (trade_id,)).fetchone()
        else:
            row = conn.execute("""
                SELECT * FROM trades
                WHERE id = ? AND user_id = ?
            """, (trade_id, user_id)).fetchone()

    return row_to_dict(row)


# ============================================================
# 19. POSITION SIZE AND RISK CALCULATION
# ============================================================

def calculate_position_size(balance, entry, stop_loss, market):
    balance = safe_float(balance, 0)
    entry = safe_float(entry, 0)
    stop_loss = safe_float(stop_loss, 0)
    market = str(market).lower()

    if balance <= 0 or entry <= 0 or stop_loss <= 0:
        raise ValueError("Некорректный баланс или торговые цены")

    distance = abs(entry - stop_loss)

    if distance <= 0:
        raise ValueError("Цена входа и стоп-лосс не должны совпадать")

    risk_target = balance * (RISK_PERCENT / 100)

    quantity = risk_target / distance
    notional = quantity * entry

    if market == "spot":
        max_notional = balance
    else:
        max_notional = balance * FUTURES_LEVERAGE

    if notional > max_notional:
        notional = max_notional
        quantity = notional / entry

    actual_risk = quantity * distance

    margin_required = (
        notional / FUTURES_LEVERAGE
        if market == "swap"
        else notional
    )

    return {
        "quantity": quantity,
        "notional": notional,
        "margin_required": margin_required,
        "risk_amount": actual_risk,
        "risk_target": risk_target,
        "risk_percent": (
            actual_risk / balance * 100
            if balance > 0 else 0
        )
    }


# ============================================================
# 20. PERSONAL TRADE CREATION
# ============================================================

def create_personal_trade(user_id, signal_id, actual_entry):
    actual_entry = safe_float(actual_entry, 0)

    if actual_entry <= 0:
        return None, "Цена входа должна быть больше нуля."

    with db() as conn:
        conn.row_factory = sqlite3.Row

        signal_row = conn.execute("""
            SELECT * FROM signals WHERE id = ?
        """, (signal_id,)).fetchone()

        if not signal_row:
            return None, "Сигнал не найден или уже недоступен."

        signal = row_to_dict(signal_row)

        active_count = conn.execute("""
            SELECT COUNT(*) FROM trades
            WHERE user_id = ?
              AND LOWER(COALESCE(status, '')) IN ('active', 'open')
        """, (user_id,)).fetchone()[0]

        if active_count >= MAX_ACTIVE_TRADES:
            return None, (
                f"У тебя уже есть {MAX_ACTIVE_TRADES} активных сделок. "
                "Сначала закрой одну из них."
            )

        market = str(signal["market"]).lower()
        symbol = signal["symbol"]
        side = str(signal["side"]).lower()

        balance_row = conn.execute("""
            SELECT balance FROM user_balances
            WHERE user_id = ? AND market = ?
        """, (user_id, market)).fetchone()

        if not balance_row:
            return None, (
                "Сначала укажи баланс этого рынка в разделе "
                "«Настройки». Без баланса бот не сможет рассчитать "
                "размер позиции и риск."
            )

        balance = safe_float(balance_row["balance"], 0)

        if balance <= 0:
            return None, "Сначала укажи корректный баланс в настройках."

        signal_entry = safe_float(signal["entry"], 0)
        signal_sl = safe_float(signal["sl"], 0)

        if signal_entry <= 0 or signal_sl <= 0:
            return None, "В сигнале отсутствуют корректные цены."

        stop_distance = abs(signal_entry - signal_sl)

        if stop_distance <= 0:
            return None, "Невозможно рассчитать дистанцию стоп-лосса."

        if is_long_side(side):
            actual_sl = actual_entry - stop_distance
            tp1 = actual_entry + stop_distance * TP1_R
            tp2 = actual_entry + stop_distance * TP2_R
        else:
            actual_sl = actual_entry + stop_distance
            tp1 = actual_entry - stop_distance * TP1_R
            tp2 = actual_entry - stop_distance * TP2_R

        if min(actual_sl, tp1, tp2) <= 0:
            return None, "При указанной цене входа уровни SL/TP выходят в некорректную зону."

        sizing = calculate_position_size(
            balance,
            actual_entry,
            actual_sl,
            market
        )

        now = utc_text()

        cursor = conn.execute("""
            INSERT INTO trades (
                user_id, signal_id, market, symbol, side,
                entry, actual_entry, current_price,
                sl, tp1, tp2, initial_sl, risk_amount,
                position_size, notional, margin_required,
                balance_snapshot, remaining_percent,
                tp1_hit, tp2_hit, status, last_recommendation,
                opened_at, updated_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, 100, 0, 0, 'active', ?, ?, ?
            )
        """, (
            user_id, signal_id, market, symbol, side,
            actual_entry, actual_entry, actual_entry,
            actual_sl, tp1, tp2, actual_sl,
            sizing["risk_amount"],
            sizing["quantity"],
            sizing["notional"],
            sizing["margin_required"],
            balance,
            "Сделка открыта пользователем",
            now, now
        ))

        trade_id = cursor.lastrowid

        conn.execute("""
            INSERT INTO trade_events (
                trade_id, user_id, event_type,
                message, price, created_at
            )
            VALUES (?, ?, 'opened', ?, ?, ?)
        """, (
            trade_id, user_id,
            "Пользователь подтвердил вход в сделку",
            actual_entry, now
        ))

    return trade_id, None


# ============================================================
# 21. TRADE UPDATE FUNCTIONS
# ============================================================

def update_trade_field(trade_id, user_id, field, value):
    allowed_fields = {"actual_entry", "sl"}

    if field not in allowed_fields:
        return False, "Это поле нельзя изменять."

    value = safe_float(value, 0)

    if value <= 0:
        return False, "Цена должна быть больше нуля."

    trade = get_trade(trade_id, user_id)

    if not trade:
        return False, "Сделка не найдена."

    if str(trade.get("status", "")).lower() not in ("active", "open"):
        return False, "Эта сделка уже не активна."

    market = str(trade["market"]).lower()
    balance = safe_float(trade.get("balance_snapshot"), 0)
    if balance <= 0:
        balance = get_user_balance(user_id, market)

    if field == "actual_entry":
        old_entry = safe_float(trade["actual_entry"], 0)
        old_sl = safe_float(trade["sl"], 0)
        distance = abs(old_entry - old_sl)

        if distance <= 0:
            return False, "Не удалось определить дистанцию стопа."

        if is_long_side(trade["side"]):
            new_sl = value - distance
            new_tp1 = value + distance * TP1_R
            new_tp2 = value + distance * TP2_R
        else:
            new_sl = value + distance
            new_tp1 = value - distance * TP1_R
            new_tp2 = value - distance * TP2_R

        if min(new_sl, new_tp1, new_tp2) <= 0:
            return False, "Новая цена входа даёт некорректные уровни SL/TP."

        sizing = calculate_position_size(
            balance, value, new_sl, market
        )

        with db() as conn:
            conn.execute("""
                UPDATE trades
                SET actual_entry = ?, entry = ?, current_price = ?,
                    sl = ?, initial_sl = ?, tp1 = ?, tp2 = ?,
                    risk_amount = ?, position_size = ?,
                    notional = ?, margin_required = ?,
                    updated_at = ?
                WHERE id = ? AND user_id = ?
            """, (
                value, value, value,
                new_sl, new_sl, new_tp1, new_tp2,
                sizing["risk_amount"],
                sizing["quantity"],
                sizing["notional"],
                sizing["margin_required"],
                utc_text(),
                trade_id, user_id
            ))

            # При изменении цены входа позволяем заново отслеживать касания уровней
            conn.execute("""
                DELETE FROM trade_events
                WHERE trade_id = ? AND user_id = ?
                  AND event_type IN ('touch_tp1', 'touch_tp2', 'touch_sl')
            """, (trade_id, user_id))

        save_trade_event(
            trade_id, user_id, "entry_updated",
            "Пользователь исправил цену входа", value
        )

        return True, "Цена входа и торговые уровни пересчитаны."

    if field == "sl":
        entry = safe_float(trade["actual_entry"], 0)

        if is_long_side(trade["side"]) and value >= entry:
            return False, "Для LONG стоп-лосс должен быть ниже входа."

        if not is_long_side(trade["side"]) and value <= entry:
            return False, "Для SHORT стоп-лосс должен быть выше входа."

        sizing = calculate_position_size(
            balance, entry, value, market
        )

        with db() as conn:
            conn.execute("""
                UPDATE trades
                SET sl = ?, initial_sl = ?, risk_amount = ?,
                    position_size = ?, notional = ?,
                    margin_required = ?, updated_at = ?
                WHERE id = ? AND user_id = ?
            """, (
                value, value,
                sizing["risk_amount"],
                sizing["quantity"],
                sizing["notional"],
                sizing["margin_required"],
                utc_text(),
                trade_id, user_id
            ))

        save_trade_event(
            trade_id, user_id, "sl_updated",
            "Пользователь изменил стоп-лосс", value
        )

        return True, "Стоп-лосс обновлён. Размер риска пересчитан."

    return False, "Неизвестная операция."


# ============================================================
# 22. TRADE TEXT
# ============================================================

def trade_text(trade):
    market = market_name(trade["market"])
    side = side_name(trade["side"])

    entry = safe_float(trade.get("actual_entry"), 0)
    current = safe_float(trade.get("current_price"), 0)
    sl = safe_float(trade.get("sl"), 0)
    tp1 = safe_float(trade.get("tp1"), 0)
    tp2 = safe_float(trade.get("tp2"), 0)

    risk = safe_float(trade.get("risk_amount"), 0)
    quantity = safe_float(trade.get("position_size"), 0)
    notional = safe_float(trade.get("notional"), 0)
    margin = safe_float(trade.get("margin_required"), 0)

    remaining = safe_float(trade.get("remaining_percent"), 100)

    return (
        f"📊 <b>{trade['symbol']}</b>\n"
        f"Рынок: {market}\n"
        f"Направление: {side}\n"
        f"Статус: {trade['status']}\n\n"
        f"Цена входа: <code>{format_price(entry)}</code>\n"
        f"Текущая цена: <code>{format_price(current)}</code>\n"
        f"Стоп-лосс: <code>{format_price(sl)}</code>\n"
        f"TP1: <code>{format_price(tp1)}</code>\n"
        f"TP2: <code>{format_price(tp2)}</code>\n\n"
        f"Риск в USDT: {risk:.4f}\n"
        f"Количество: {quantity:.8f}\n"
        f"Объём позиции: {notional:.2f} USDT\n"
        f"Маржа: {margin:.2f} USDT\n"
        f"Остаток позиции: {remaining:.0f}%\n\n"
        f"TP1 подтверждён: {'Да' if trade.get('tp1_hit') else 'Нет'}\n"
        f"TP2 подтверждён: {'Да' if trade.get('tp2_hit') else 'Нет'}\n\n"
        f"Последняя рекомендация:\n"
        f"{trade.get('last_recommendation') or 'Пока нет'}"
    )


# ============================================================
# 23. MARKET ANALYSIS  (100204 + 109425 + rate-limit filtering)
# ============================================================

_unavailable_symbols: set[tuple[str, str]] = set()


async def analyze_market_symbol(market, symbol):
    key = (market, symbol)

    if key in _unavailable_symbols:
        return None

    intervals = ("5m", "15m", "1h", "4h")
    candles = {}

    for interval in intervals:
        try:
            data = await get_klines(
                market, symbol, interval, KLINE_LIMIT
            )
        except MarketError as exc:
            text = str(exc).lower()

            if (
                "100204" in str(exc)
                or "109425" in str(exc)
                or "symbol is not found" in text
                or "not exist" in text
            ):
                _unavailable_symbols.add(key)
                log.warning(
                    "Символ %s %s недоступен на BingX — исключён из сканирования",
                    market, symbol
                )
                return None

            raise

        if data is None or len(data) < MIN_CANDLE_ROWS:
            return None

        candles[interval] = data

        try:
            save_closed_candles(market, symbol, interval, data)
        except Exception:
            logging.exception(
                "Не удалось сохранить свечи %s %s %s",
                market, symbol, interval
            )

    try:
        signal = build_signal(market, symbol, candles)
    except Exception:
        logging.exception(
            "Ошибка анализа %s %s", market, symbol
        )
        return None

    return signal


async def scan_market():
    found_signals = []
    system_status["scan_count"] = int(system_status.get("scan_count", 0)) + 1
    system_status["last_scan"] = utc_text()

    for market in MARKETS:
        for symbol in SYMBOLS:
            key = (market, symbol)

            if key in _unavailable_symbols:
                continue

            try:
                signal = await analyze_market_symbol(market, symbol)

                if not signal:
                    continue

                signal_id = save_signal(signal)

                if signal_id:
                    signal["id"] = signal_id
                    found_signals.append(signal)

            except MarketError as exc:
                text = str(exc)

                if "100410" in text or "100429" in text:
                    log.debug(
                        "Rate limit %s %s (пропуск цикла)",
                        market, symbol
                    )
                else:
                    log.warning(
                        "Ошибка рынка %s %s: %s",
                        market, symbol, exc
                    )
            except Exception:
                logging.exception(
                    "Ошибка сканирования %s %s",
                    market, symbol
                )

    system_status["signals_count"] = (
        int(system_status.get("signals_count", 0)) + len(found_signals)
    )
    return found_signals


# ============================================================
# 24. SIGNAL MESSAGES
# ============================================================

def signal_text(signal):
    market = market_name(signal["market"])
    side = side_name(signal["side"])

    tp1 = safe_float(signal.get("tp1"), 0)
    tp2 = safe_float(signal.get("tp2"), signal.get("tp", 0))

    raw_reasons = signal.get("reasons", "")
    if isinstance(raw_reasons, (list, tuple)):
        reasons_formatted = "\n".join(f"• {r}" for r in raw_reasons if r)
    else:
        parts = [p.strip() for p in str(raw_reasons).split(";") if p.strip()]
        reasons_formatted = "\n".join(f"• {p}" for p in parts) if parts else "—"

    return (
        f"🚨 <b>НОВЫЙ ТОРГОВЫЙ СИГНАЛ</b>\n\n"
        f"Монета: <b>{signal['symbol']}</b>\n"
        f"Рынок: {market}\n"
        f"Направление: {side}\n\n"
        f"Вход: <code>{format_price(signal['entry'])}</code>\n"
        f"Стоп-лосс: <code>{format_price(signal['sl'])}</code>\n"
        f"TP1: <code>{format_price(tp1)}</code> ({TP1_R}R) — "
        f"закрытие {TP1_CLOSE_PERCENT:.0f}%\n"
        f"TP2: <code>{format_price(tp2)}</code> ({TP2_R}R) — "
        f"закрытие {TP2_CLOSE_PERCENT:.0f}%\n\n"
        f"Оценка сигнала: {signal.get('score', 0)}\n"
        f"Соотношение риск/прибыль (TP2): {signal.get('rr', 0):.2f}\n\n"
        f"Причины:\n{reasons_formatted}\n\n"
        f"⚠️ Это аналитический сигнал, а не исполненная сделка. "
        f"Сопровождение начнётся только после подтверждения входа "
        f"и ввода фактической цены."
    )


async def broadcast_signal(signal):
    with db() as conn:
        conn.row_factory = sqlite3.Row

        users = conn.execute("""
            SELECT user_id FROM users WHERE is_active = 1
        """).fetchall()

    for row in users:
        user_id = row["user_id"]

        try:
            await bot.send_message(
                user_id,
                signal_text(signal),
                reply_markup=signal_keyboard(signal["id"]),
                parse_mode="HTML"
            )
        except Exception:
            logging.exception(
                "Не удалось отправить сигнал пользователю %s",
                user_id
            )


# ============================================================
# 25. BASIC TELEGRAM HANDLERS
# ============================================================

@router.message(Command("start"))
async def start_command(message):
    register_user(message.from_user)

    await message.answer(
        "Привет! Это Trading Bot v9.2.\n\n"
        "Бот анализирует публичные рыночные данные BingX "
        "и отправляет торговые сигналы.\n\n"
        "Бот не размещает ордера, не перемещает стопы "
        "и не управляет твоими средствами.\n\n"
        "Выбери нужный раздел в меню.",
        reply_markup=main_keyboard()
    )


@router.message(F.text == "🔎 Анализ рынка")
async def scan_market_callback(message):
    register_user(message.from_user)

    status_message = await message.answer(
        "⏳ Начинаю анализ рынка BingX.\n"
        "Сканирование может занять некоторое время."
    )

    try:
        signals = await scan_market()

        if not signals:
            await status_message.edit_text(
                "Пока нет новых подтверждённых сигналов.\n\n"
                "Это не означает, что рынок не анализировался. "
                "Возможно, сейчас нет подходящих условий."
            )
            return

        await status_message.edit_text(
            f"✅ Анализ завершён. Найдено новых сигналов: "
            f"{len(signals)}."
        )

        for signal in signals:
            await message.answer(
                signal_text(signal),
                reply_markup=signal_keyboard(signal["id"]),
                parse_mode="HTML"
            )

    except Exception:
        logging.exception("Ошибка ручного сканирования")

        await status_message.edit_text(
            "❌ Не удалось завершить анализ рынка. "
            "Проверь соединение с BingX и попробуй позже."
        )


@router.callback_query(F.data.startswith("enter:"))
async def enter_trade_callback(callback):
    register_user(callback.from_user)

    try:
        signal_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный сигнал.", show_alert=True)
        return

    with db() as conn:
        conn.row_factory = sqlite3.Row

        signal = conn.execute("""
            SELECT id, market, symbol FROM signals WHERE id = ?
        """, (signal_id,)).fetchone()

    if not signal:
        await callback.answer("Сигнал не найден.", show_alert=True)
        return

    active_trades = get_active_trades(callback.from_user.id)

    if len(active_trades) >= MAX_ACTIVE_TRADES:
        await callback.answer(
            "Достигнут лимит в 5 активных сделок.",
            show_alert=True
        )
        return

    market = str(signal["market"]).lower()
    balance = get_user_balance(callback.from_user.id, market)

    if balance <= 0:
        await callback.message.answer(
            "Перед открытием сопровождения укажи баланс "
            f"рынка {market_name(market)} в разделе «Настройки».\n\n"
            "После этого снова нажми «Я вошёл в сделку»."
        )
        await callback.answer()
        return

    user_states[callback.from_user.id] = {
        "type": "waiting_entry",
        "signal_id": signal_id
    }

    await callback.message.answer(
        f"Ты подтвердил вход в {signal['symbol']}.\n\n"
        "Введи фактическую цену, по которой ты вошёл в сделку.\n\n"
        "Например: 67250.5\n\n"
        "Важно: бот не открывает сделку на бирже. "
        "Он начнёт сопровождение после ввода цены."
    )

    await callback.answer()


# ============================================================
# 26. USER TEXT INPUT
# ============================================================

@router.message(
    F.text,
    lambda message: (
        message.from_user
        and message.from_user.id in user_states
    )
)
async def handle_user_text(message):
    user_id = message.from_user.id
    state = user_states.get(user_id)

    if not state:
        return

    text_value = (message.text or "").strip().replace(",", ".")

    if text_value.lower() in ("отмена", "cancel", "/cancel"):
        user_states.pop(user_id, None)

        await message.answer(
            "Действие отменено.",
            reply_markup=main_keyboard()
        )
        return

    try:
        value = float(text_value)
    except ValueError:
        await message.answer(
            "Не удалось распознать число. "
            "Введи цену цифрами, например: 67250.5"
        )
        return

    if not math.isfinite(value) or value <= 0:
        await message.answer("Число должно быть больше нуля.")
        return

    state_type = state.get("type")

    if state_type == "waiting_entry":
        signal_id = state["signal_id"]

        trade_id, error = create_personal_trade(
            user_id, signal_id, value
        )

        if error:
            await message.answer(error)
            return

        user_states.pop(user_id, None)
        trade = get_trade(trade_id, user_id)

        await message.answer(
            "✅ Сделка добавлена в «Мои сделки».\n\n"
            + trade_text(trade),
            reply_markup=trade_keyboard(trade_id),
            parse_mode="HTML"
        )

        return

    if state_type == "waiting_new_entry":
        trade_id = state["trade_id"]

        success, result = update_trade_field(
            trade_id, user_id, "actual_entry", value
        )

        if success:
            user_states.pop(user_id, None)
            trade = get_trade(trade_id, user_id)

            await message.answer(
                result + "\n\n" + trade_text(trade),
                reply_markup=trade_keyboard(trade_id),
                parse_mode="HTML"
            )
        else:
            await message.answer(result)

        return

    if state_type == "waiting_sl":
        trade_id = state["trade_id"]

        success, result = update_trade_field(
            trade_id, user_id, "sl", value
        )

        if success:
            user_states.pop(user_id, None)
            trade = get_trade(trade_id, user_id)

            await message.answer(
                result + "\n\n" + trade_text(trade),
                reply_markup=trade_keyboard(trade_id),
                parse_mode="HTML"
            )
        else:
            await message.answer(result)

        return


# ============================================================
# 27. MY TRADES
# ============================================================

def trades_list_keyboard(trades):
    rows = []

    for trade in trades:
        rows.append([
            InlineKeyboardButton(
                text=f"{trade['symbol']} — {side_name(trade['side'])}",
                callback_data=f"trade_refresh:{trade['id']}"
            )
        ])

    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(F.text == "📊 Мои сделки")
async def my_trades_handler(message):
    register_user(message.from_user)

    trades = get_active_trades(message.from_user.id)

    if not trades:
        await message.answer(
            "📊 У тебя пока нет активных сопровождаемых сделок.\n\n"
            "Сделка появится здесь только после нажатия "
            "«Я вошёл в сделку» и ввода фактической цены."
        )
        return

    await message.answer(
        f"📊 Активных сделок: {len(trades)} из {MAX_ACTIVE_TRADES}",
        reply_markup=trades_list_keyboard(trades)
    )

    for trade in trades:
        await message.answer(
            trade_text(trade),
            reply_markup=trade_keyboard(trade["id"]),
            parse_mode="HTML"
        )


@router.callback_query(F.data.startswith("trade_refresh:"))
async def trade_refresh_callback(callback):
    try:
        trade_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный номер сделки.", show_alert=True)
        return

    trade = get_trade(trade_id, callback.from_user.id)

    if not trade:
        await callback.answer("Сделка не найдена.", show_alert=True)
        return

    await callback.message.answer(
        trade_text(trade),
        reply_markup=trade_keyboard(trade_id),
        parse_mode="HTML"
    )

    await callback.answer("Информация обновлена.")


@router.callback_query(F.data.startswith("trade_edit_entry:"))
async def trade_edit_entry_callback(callback):
    try:
        trade_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный номер сделки.", show_alert=True)
        return

    trade = get_trade(trade_id, callback.from_user.id)

    if not trade:
        await callback.answer("Сделка не найдена.", show_alert=True)
        return

    user_states[callback.from_user.id] = {
        "type": "waiting_new_entry",
        "trade_id": trade_id
    }

    await callback.message.answer(
        "Введи новую фактическую цену входа.\n"
        "Стоп-лосс и цели будут пересчитаны относительно новой цены."
    )

    await callback.answer()


@router.callback_query(F.data.startswith("trade_edit_sl:"))
async def trade_edit_sl_callback(callback):
    try:
        trade_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный номер сделки.", show_alert=True)
        return

    trade = get_trade(trade_id, callback.from_user.id)

    if not trade:
        await callback.answer("Сделка не найдена.", show_alert=True)
        return

    user_states[callback.from_user.id] = {
        "type": "waiting_sl",
        "trade_id": trade_id
    }

    await callback.message.answer(
        "Введи новую цену стоп-лосса.\n"
        "Бот сохранит её и пересчитает риск."
    )

    await callback.answer()


# ============================================================
# 28. TRADE HISTORY AND STATISTICS
# ============================================================

@router.message(F.text == "📚 История")
async def history_handler(message):
    register_user(message.from_user)

    with db() as conn:
        conn.row_factory = sqlite3.Row

        rows = conn.execute("""
            SELECT * FROM trade_history
            WHERE user_id = ?
            ORDER BY closed_at DESC
            LIMIT 20
        """, (message.from_user.id,)).fetchall()

    if not rows:
        await message.answer("📚 История закрытых сделок пока пуста.")
        return

    lines = ["📚 <b>Последние закрытые сделки</b>\n"]

    for row in rows:
        pnl = safe_float(row["pnl_percent"], 0)
        pnl_r = safe_float(row["pnl_r"], 0)

        lines.append(
            f"\n<b>{row['market']} | {row['symbol']}</b>\n"
            f"Направление: {side_name(row['side'])}\n"
            f"Вход: {format_price(row['entry'])}\n"
            f"Выход: {format_price(row['exit_price'])}\n"
            f"Результат: {row['result']}\n"
            f"Изменение: {pnl:.2f}%\n"
            f"Результат в R: {pnl_r:.2f}R"
        )

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(F.text == "📈 Статистика")
async def statistics_handler(message):
    register_user(message.from_user)

    with db() as conn:
        conn.row_factory = sqlite3.Row

        total = conn.execute("""
            SELECT COUNT(*) AS total FROM trade_history
            WHERE user_id = ?
        """, (message.from_user.id,)).fetchone()["total"]

        wins = conn.execute("""
            SELECT COUNT(*) AS total FROM trade_history
            WHERE user_id = ? AND result = 'win'
        """, (message.from_user.id,)).fetchone()["total"]

        losses = conn.execute("""
            SELECT COUNT(*) AS total FROM trade_history
            WHERE user_id = ? AND result = 'loss'
        """, (message.from_user.id,)).fetchone()["total"]

        pnl = conn.execute("""
            SELECT
                COALESCE(SUM(pnl_percent), 0) AS pnl_percent,
                COALESCE(SUM(pnl_r), 0) AS pnl_r
            FROM trade_history
            WHERE user_id = ?
        """, (message.from_user.id,)).fetchone()

    active = len(get_active_trades(message.from_user.id))
    winrate = wins / total * 100 if total else 0

    await message.answer(
        "📈 <b>Статистика торговли</b>\n\n"
        f"Закрытых сделок: {total}\n"
        f"Прибыльных: {wins}\n"
        f"Убыточных: {losses}\n"
        f"Процент прибыльных: {winrate:.1f}%\n"
        f"Сумма результатов (%): {safe_float(pnl['pnl_percent'], 0):.2f}%\n"
        f"Сумма результатов (R): {safe_float(pnl['pnl_r'], 0):.2f}R\n"
        f"Сейчас сопровождается: {active}/{MAX_ACTIVE_TRADES}",
        parse_mode="HTML"
    )


# ============================================================
# 29. SETTINGS
# ============================================================

@router.message(F.text == "⚙️ Настройки")
async def settings_handler(message):
    register_user(message.from_user)

    spot_balance = get_user_balance(message.from_user.id, "spot")
    swap_balance = get_user_balance(message.from_user.id, "swap")

    await message.answer(
        "⚙️ <b>Настройки торгового бота</b>\n\n"
        f"Риск на сделку: {RISK_PERCENT}%\n"
        f"Плечо Futures: {FUTURES_LEVERAGE}x\n"
        f"TP1: {TP1_R}R — закрытие {TP1_CLOSE_PERCENT:.0f}%\n"
        f"TP2: {TP2_R}R — закрытие {TP2_CLOSE_PERCENT:.0f}%\n\n"
        f"Баланс Spot: {spot_balance:.2f} USDT\n"
        f"Баланс Futures: {swap_balance:.2f} USDT\n\n"
        "Для изменения баланса отправь одну из команд:\n"
        "<code>/spotbalance 1000</code>\n"
        "<code>/swapbalance 500</code>\n\n"
        "Укажи вместо числа свой фактический баланс в USDT.\n\n"
        "Баланс вводится вручную. Бот не получает доступ "
        "к твоему аккаунту или средствам.",
        parse_mode="HTML"
    )


@router.message(Command("spotbalance"))
async def set_spot_balance_handler(message):
    parts = (message.text or "").split()

    if len(parts) != 2:
        await message.answer("Укажи баланс так:\n/spotbalance 1000")
        return

    try:
        balance = float(parts[1].replace(",", "."))
        set_user_balance(message.from_user.id, "spot", balance)
    except (ValueError, TypeError):
        await message.answer(
            "Некорректный баланс. Введи положительное число."
        )
        return

    register_user(message.from_user)

    await message.answer(
        f"✅ Баланс Spot сохранён: {balance:.2f} USDT."
    )


@router.message(Command("swapbalance"))
async def set_swap_balance_handler(message):
    parts = (message.text or "").split()

    if len(parts) != 2:
        await message.answer("Укажи баланс так:\n/swapbalance 500")
        return

    try:
        balance = float(parts[1].replace(",", "."))
        set_user_balance(message.from_user.id, "swap", balance)
    except (ValueError, TypeError):
        await message.answer(
            "Некорректный баланс. Введи положительное число."
        )
        return

    register_user(message.from_user)

    await message.answer(
        f"✅ Баланс Futures сохранён: {balance:.2f} USDT."
    )


# ============================================================
# 30. SYSTEM STATUS
# ============================================================

@router.message(F.text == "🖥 Статус системы")
async def system_status_handler(message):
    register_user(message.from_user)

    active = len(get_active_trades(message.from_user.id))

    await message.answer(
        "🖥 <b>Состояние Trading Bot v9.2</b>\n\n"
        f"Режим: публичный анализ BingX\n"
        f"Реальные ордера: отключены\n"
        f"Spot: включён в анализе\n"
        f"USDT-M Futures: включён в анализе\n"
        f"API BingX: {system_status.get('api_status', 'Не проверено')}\n"
        f"Последний скан: {system_status.get('last_scan') or '—'}\n"
        f"Лимит активных сделок: {MAX_ACTIVE_TRADES}\n"
        f"Твои активные сделки: {active}\n\n"
        "Цены и сигналы зависят от доступности публичного API BingX.",
        parse_mode="HTML"
    )


# ============================================================
# 31. LIVE PRICE
# ============================================================

async def get_live_price(market, symbol):
    if http_session is None or http_session.closed:
        return None

    if market == "spot":
        endpoint = "/openApi/spot/v1/ticker/price"
    else:
        endpoint = "/openApi/swap/v2/quote/price"

    try:
        response = await api_get(
            http_session, endpoint, {"symbol": symbol}
        )

        data = extract_data(response)

        if isinstance(data, list):
            data = data[0] if data else None

        if isinstance(data, dict):
            for key in ("price", "lastPrice", "last", "close"):
                price = safe_float(data.get(key), 0)

                if price > 0:
                    latest_prices[(market, symbol)] = price
                    return price

    except Exception:
        logging.exception(
            "Не удалось получить текущую цену %s %s",
            market, symbol
        )

    return None


# ============================================================
# 32. TRADE MONITORING
# ============================================================

def trade_event_exists(trade_id, event_type):
    with db() as conn:
        row = conn.execute("""
            SELECT id FROM trade_events
            WHERE trade_id = ? AND event_type = ?
            LIMIT 1
        """, (trade_id, event_type)).fetchone()

    return row is not None


async def notify_trade_touch(trade, event_type, price):
    trade_id = trade["id"]
    user_id = trade["user_id"]

    if trade_event_exists(trade_id, event_type):
        return

    if event_type == "touch_tp1":
        text = (
            f"🎯 <b>{trade['symbol']}: цена коснулась TP1</b>\n\n"
            f"Цена: {format_price(price)}\n"
            f"TP1: {format_price(trade['tp1'])}\n\n"
            "Если ты действительно закрыл 70% позиции, "
            "подтверди это кнопкой ниже.\n"
            "Само касание не означает исполнение ордера."
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="Подтверждаю TP1",
                    callback_data=f"tp1_confirm:{trade_id}"
                )
            ]]
        )

    elif event_type == "touch_tp2":
        text = (
            f"🎯 <b>{trade['symbol']}: цена коснулась TP2</b>\n\n"
            f"Цена: {format_price(price)}\n"
            f"TP2: {format_price(trade['tp2'])}\n\n"
            "Если ты действительно закрыл оставшуюся позицию, "
            "подтверди закрытие."
        )
        keyboard = confirm_keyboard("tp2", trade_id)

    else:
        text = (
            f"⚠️ <b>{trade['symbol']}: цена коснулась стоп-лосса</b>\n\n"
            f"Цена: {format_price(price)}\n"
            f"SL: {format_price(trade['sl'])}\n\n"
            "Касание не подтверждает исполнение стопа. "
            "Проверь биржу и вручную подтверди результат."
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[[
                InlineKeyboardButton(
                    text="Я закрыл по SL",
                    callback_data=f"sl_confirm:{trade_id}"
                ),
                InlineKeyboardButton(
                    text="Оставить сделку",
                    callback_data=f"sl_keep:{trade_id}"
                )
            ]]
        )

    try:
        await bot.send_message(
            user_id, text,
            reply_markup=keyboard,
            parse_mode="HTML"
        )

        save_trade_event(
            trade_id, user_id, event_type,
            "Обнаружено касание уровня", price
        )

    except Exception:
        logging.exception(
            "Ошибка отправки уведомления по сделке %s",
            trade_id
        )


async def monitor_one_trade(trade):
    market = str(trade["market"]).lower()
    symbol = trade["symbol"]

    price = await get_live_price(market, symbol)

    if price is None:
        return

    trade_id = trade["id"]
    user_id = trade["user_id"]

    with db() as conn:
        conn.execute("""
            UPDATE trades
            SET current_price = ?, updated_at = ?
            WHERE id = ? AND user_id = ?
        """, (price, utc_text(), trade_id, user_id))

    sl = safe_float(trade["sl"], 0)
    tp1 = safe_float(trade["tp1"], 0)
    tp2 = safe_float(trade["tp2"], 0)

    if is_long_side(trade["side"]):
        if sl > 0 and price <= sl:
            await notify_trade_touch(trade, "touch_sl", price)
            return

        if tp2 > 0 and price >= tp2:
            if not trade.get("tp1_hit") and tp1 > 0:
                await notify_trade_touch(trade, "touch_tp1", tp1)
            await notify_trade_touch(trade, "touch_tp2", price)
            return

        if tp1 > 0 and price >= tp1 and not trade.get("tp1_hit"):
            await notify_trade_touch(trade, "touch_tp1", price)

    else:
        if sl > 0 and price >= sl:
            await notify_trade_touch(trade, "touch_sl", price)
            return

        if tp2 > 0 and price <= tp2:
            if not trade.get("tp1_hit") and tp1 > 0:
                await notify_trade_touch(trade, "touch_tp1", tp1)
            await notify_trade_touch(trade, "touch_tp2", price)
            return

        if tp1 > 0 and price <= tp1 and not trade.get("tp1_hit"):
            await notify_trade_touch(trade, "touch_tp1", price)


async def background_monitor():
    logging.info("Мониторинг сделок запущен")

    while True:
        try:
            with db() as conn:
                conn.row_factory = sqlite3.Row

                rows = conn.execute("""
                    SELECT * FROM trades
                    WHERE LOWER(COALESCE(status, '')) IN ('active', 'open')
                    ORDER BY opened_at, id
                """).fetchall()

            for row in rows:
                trade = row_to_dict(row)

                try:
                    await monitor_one_trade(trade)
                except Exception:
                    logging.exception(
                        "Ошибка мониторинга сделки %s",
                        trade["id"]
                    )

        except Exception:
            logging.exception("Ошибка фонового мониторинга")

        await asyncio.sleep(max(TRADE_MONITOR_INTERVAL, 30))


# ============================================================
# 33. CONFIRMATION OF TRADE EVENTS
# ============================================================

async def close_trade_record(trade, exit_price, reason):
    trade_id = trade["id"]
    user_id = trade["user_id"]

    entry = safe_float(trade["actual_entry"], 0)
    initial_sl = safe_float(trade["initial_sl"], 0)
    quantity = safe_float(trade["position_size"], 0)

    if entry <= 0 or initial_sl <= 0 or quantity <= 0:
        return False, "Недостаточно данных для расчёта результата."

    exit_price = safe_float(exit_price, 0)
    if exit_price <= 0:
        return False, "Нет корректной цены выхода."

    direction = 1 if is_long_side(trade["side"]) else -1

    initial_risk_per_unit = abs(entry - initial_sl)

    if initial_risk_per_unit <= 0:
        return False, "Невозможно рассчитать первоначальный риск."

    tp1_hit = bool(trade.get("tp1_hit"))

    if tp1_hit:
        tp1 = safe_float(trade["tp1"], 0)
        realized = (
            quantity * 0.70 * (tp1 - entry) * direction
            + quantity * 0.30 * (exit_price - entry) * direction
        )
    else:
        realized = quantity * (exit_price - entry) * direction

    risk_amount = safe_float(trade.get("risk_amount"), 0)

    if risk_amount <= 0:
        risk_amount = quantity * initial_risk_per_unit

    pnl_r = realized / risk_amount if risk_amount > 0 else 0

    if str(trade["market"]).lower() == "swap":
        base = safe_float(trade.get("margin_required"), 0)
    else:
        base = quantity * entry

    if base <= 0:
        base = quantity * entry

    pnl_percent = realized / base * 100 if base > 0 else 0

    result = (
        "win" if realized > 0
        else ("loss" if realized < 0 else "breakeven")
    )

    now = utc_text()

    with db() as conn:
        conn.execute("""
            UPDATE trades
            SET status = 'closed',
                remaining_percent = 0,
                closed_at = ?,
                updated_at = ?
            WHERE id = ? AND user_id = ?
        """, (now, now, trade_id, user_id))

        conn.execute("""
            INSERT INTO trade_history (
                trade_id, user_id, market, symbol, side,
                entry, exit_price, result,
                pnl_percent, pnl_r, close_reason,
                opened_at, closed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade_id, user_id, trade["market"], trade["symbol"],
            trade["side"], entry, exit_price, result,
            pnl_percent, pnl_r, reason,
            trade["opened_at"], now
        ))

    save_trade_event(
        trade_id, user_id, "closed", reason, exit_price
    )

    return True, (
        f"Сделка закрыта.\n"
        f"Результат: {result}\n"
        f"PnL: {realized:.4f} USDT\n"
        f"Результат: {pnl_r:.2f}R"
    )


@router.callback_query(F.data.startswith("trade_close:"))
async def trade_close_callback(callback):
    try:
        trade_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный номер сделки.", show_alert=True)
        return

    trade = get_trade(trade_id, callback.from_user.id)

    if not trade:
        await callback.answer("Сделка не найдена.", show_alert=True)
        return

    await callback.message.answer(
        "⚠️ Подтверди закрытие сопровождения этой сделки.\n\n"
        "Бот не закрывает позицию на бирже. "
        "Перед подтверждением самостоятельно закрой её в BingX.\n\n"
        "Для записи результата будет использована последняя "
        "полученная ботом цена.",
        reply_markup=confirm_keyboard("close", trade_id)
    )

    await callback.answer()


@router.callback_query(F.data.startswith("close_cancel:"))
async def close_cancel_callback(callback):
    await callback.answer("Закрытие отменено.")
    await callback.message.answer(
        "Сопровождение сделки продолжается."
    )


@router.callback_query(F.data.startswith("close_confirm:"))
async def close_confirm_callback(callback):
    try:
        trade_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный номер сделки.", show_alert=True)
        return

    trade = get_trade(trade_id, callback.from_user.id)

    if not trade:
        await callback.answer("Сделка не найдена.", show_alert=True)
        return

    exit_price = safe_float(trade.get("current_price"), 0)

    if exit_price <= 0:
        await callback.answer(
            "Нет актуальной цены. Закрытие не записано.",
            show_alert=True
        )
        return

    success, result = await close_trade_record(
        trade, exit_price,
        "Пользователь подтвердил закрытие"
    )

    await callback.answer(
        "Сделка закрыта." if success else "Ошибка",
        show_alert=not success
    )

    await callback.message.answer(result)


@router.callback_query(F.data.startswith("tp1_confirm:"))
async def tp1_confirm_callback(callback):
    try:
        trade_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректная сделка.", show_alert=True)
        return

    trade = get_trade(trade_id, callback.from_user.id)

    if not trade:
        await callback.answer("Сделка не найдена.", show_alert=True)
        return

    if trade.get("tp1_hit"):
        await callback.answer("TP1 уже подтверждён.")
        return

    with db() as conn:
        conn.execute("""
            UPDATE trades
            SET tp1_hit = 1,
                remaining_percent = 30,
                last_recommendation = ?,
                updated_at = ?
            WHERE id = ? AND user_id = ?
        """, (
            "TP1 подтверждён пользователем. "
            "Рекомендуется вручную перенести SL на цену входа.",
            utc_text(),
            trade_id,
            callback.from_user.id
        ))

    save_trade_event(
        trade_id, callback.from_user.id, "tp1_confirmed",
        "Пользователь подтвердил закрытие 70% позиции"
    )

    await callback.message.answer(
        "✅ TP1 подтверждён.\n\n"
        "Зафиксировано закрытие 70% позиции.\n"
        "Остаток: 30%.\n\n"
        "Рекомендация: вручную перенести стоп-лосс "
        "на фактическую цену входа в BingX.\n"
        "Бот не перемещает стоп автоматически."
    )

    await callback.answer("TP1 подтверждён.")


@router.callback_query(F.data.startswith("tp2_cancel:"))
async def tp2_cancel_callback(callback):
    await callback.answer("Подтверждение отменено.")
    await callback.message.answer(
        "Сделка остаётся под сопровождением."
    )


@router.callback_query(F.data.startswith("tp2_confirm:"))
async def tp2_confirm_callback(callback):
    try:
        trade_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректная сделка.", show_alert=True)
        return

    trade = get_trade(trade_id, callback.from_user.id)

    if not trade:
        await callback.answer("Сделка не найдена.", show_alert=True)
        return

    if not trade.get("tp1_hit"):
        await callback.answer(
            "Сначала подтверди TP1, если он был исполнен.",
            show_alert=True
        )
        return

    exit_price = safe_float(trade.get("current_price"), 0)

    if exit_price <= 0:
        await callback.answer(
            "Нет актуальной цены. Подтверждение не записано.",
            show_alert=True
        )
        return

    success, result = await close_trade_record(
        trade, exit_price,
        "Пользователь подтвердил исполнение TP2"
    )

    await callback.answer(
        "TP2 подтверждён." if success else "Ошибка",
        show_alert=not success
    )

    await callback.message.answer(result)


@router.callback_query(F.data.startswith("sl_keep:"))
async def sl_keep_callback(callback):
    try:
        trade_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректная сделка.", show_alert=True)
        return

    save_trade_event(
        trade_id, callback.from_user.id, "sl_touch_ignored",
        "Пользователь решил оставить сделку после касания SL"
    )

    await callback.answer("Сопровождение продолжается.")
    await callback.message.answer(
        "Ты решил оставить сделку. Мониторинг продолжается.\n"
        "Проверь стоп-лосс на бирже."
    )


@router.callback_query(F.data.startswith("sl_confirm:"))
async def sl_confirm_callback(callback):
    try:
        trade_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректная сделка.", show_alert=True)
        return

    trade = get_trade(trade_id, callback.from_user.id)

    if not trade:
        await callback.answer("Сделка не найдена.", show_alert=True)
        return

    exit_price = safe_float(trade.get("current_price"), 0)

    if exit_price <= 0:
        await callback.answer(
            "Нет актуальной цены. Закрытие не записано.",
            show_alert=True
        )
        return

    success, result = await close_trade_record(
        trade, exit_price,
        "Пользователь подтвердил закрытие по SL"
    )

    await callback.answer(
        "Закрытие записано." if success else "Ошибка",
        show_alert=not success
    )

    await callback.message.answer(result)


# ============================================================
# 34. BACKGROUND MARKET SCAN
# ============================================================

async def background_market_scan():
    logging.info("Фоновый анализ рынка запущен")

    while True:
        try:
            signals = await scan_market()

            for signal in signals:
                await broadcast_signal(signal)

        except Exception:
            logging.exception("Ошибка фонового анализа рынка")

        await asyncio.sleep(max(MARKET_SCAN_INTERVAL * 60, 1800))


# ============================================================
# 35. APPLICATION STARTUP
# ============================================================

async def main():
    global bot
    global http_session
    global dp

    logging.info("Запуск Trading Bot v9.2")

    init_db()
    ensure_part3_schema()

    if bot is None:
        bot = Bot(
            token=BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML)
        )

    if dp is None:
        raise RuntimeError(
            "Dispatcher не создан. Проверь инициализацию части 1."
        )

    if router.parent_router is None:
        dp.include_router(router)

    system_status["started"] = True

    async with aiohttp.ClientSession() as session:
        http_session = session

        monitor_task = asyncio.create_task(background_monitor())
        scan_task = asyncio.create_task(background_market_scan())

        background_tasks.extend([monitor_task, scan_task])

        try:
            logging.info("Telegram polling запущен")
            await dp.start_polling(bot)

        finally:
            system_status["started"] = False

            for task in background_tasks:
                task.cancel()

            await asyncio.gather(
                *background_tasks,
                return_exceptions=True
            )

            background_tasks.clear()
            http_session = None

    if bot:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
