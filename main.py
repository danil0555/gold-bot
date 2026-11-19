
"""
Trading Bot v9.2 — BingX market signals and manual trade monitoring.
Python 3.10+
No real order execution. No private trading API keys.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import math
import os
import sqlite3
import time
import uuid

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import aiohttp
import numpy as np
import pandas as pd

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.utils.keyboard import ReplyKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()


# ============================================================
# 1. CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

if not BOT_TOKEN:
    raise RuntimeError(
        "Не задан TELEGRAM_BOT_TOKEN в переменных окружения"
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
    if x.strip() in ("spot", "swap")
]

if not MARKETS:
    MARKETS = ["spot", "swap"]

DB_PATH = "trading_bot_v9.db"

BASE = "https://open-api.bingx.com"

SPOT_WS = "wss://open-api-ws.bingx.com/market"
SWAP_WS = "wss://open-api-swap.bingx.com/swap-market"

RISK_PERCENT = 0.02
FUTURES_LEVERAGE = 5

TP1_R = 1.8
TP2_R = 3.0

TP1_CLOSE_PERCENT = 70.0
TP2_CLOSE_PERCENT = 30.0

TRAILING_LOOKBACK = 10
TRAILING_ATR_MULTIPLIER = 1.5

ENTRY_WARNING_R = 0.5

MAX_ACTIVE_TRADES = 5

SCAN_INTERVAL = max(
    900,
    int(os.getenv("SCAN_INTERVAL_SECONDS", "900"))
)

MONITOR_INTERVAL = max(
    30,
    int(os.getenv("MONITOR_INTERVAL_SECONDS", "60"))
)

MIN_RR = max(
    1.8,
    float(os.getenv("MIN_RR", "1.8"))
)

MIN_SCORE = min(
    10,
    max(1, int(os.getenv("MIN_SCORE", "6")))
)

ALLOWED_USER_IDS = {
    int(x)
    for x in os.getenv(
        "ALLOWED_USER_IDS", ""
    ).split(",")
    if x.strip().isdigit()
}

TF_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("trading-bot-v9.2")

_api_rate_lock = asyncio.Lock()
_last_api_request = 0.0
_api_cooldown_until = 0.0

scan_lock = asyncio.Lock()

scan_task: asyncio.Task | None = None

ws_health = {
    "spot": {
        "connected": False,
        "last_message": 0.0,
        "last_error": "",
        "reconnects": 0,
    },
    "swap": {
        "connected": False,
        "last_message": 0.0,
        "last_error": "",
        "reconnects": 0,
    },
}

latest_prices: dict[tuple[str, str], float] = {}

candle_cache: dict[
    tuple[str, str, str],
    pd.DataFrame
] = {}


# ============================================================
# 2. TIME AND FORMATTING
# ============================================================

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_text() -> str:
    return utc_now().isoformat(timespec="seconds")


def now_ms() -> int:
    return int(time.time() * 1000)


def fmt_price(value: float) -> str:
    return f"{float(value):.8f}".rstrip("0").rstrip(".")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)

        if not math.isfinite(result):
            return default

        return result

    except (TypeError, ValueError):
        return default


def allowed(user_id: int) -> bool:
    return (
        not ALLOWED_USER_IDS
        or user_id in ALLOWED_USER_IDS
    )


# ============================================================
# 3. DATABASE
# ============================================================

@contextmanager
def db():
    con = sqlite3.connect(
        DB_PATH,
        timeout=30
    )

    con.row_factory = sqlite3.Row

    try:
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=30000")

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

    return {
        row["name"]
        for row in con.execute(
            f"PRAGMA table_info({table})"
        ).fetchall()
    }


def add_column_if_missing(
    con: sqlite3.Connection,
    table: str,
    name: str,
    definition: str
) -> None:

    columns = table_columns(con, table)

    if name not in columns:
        con.execute(
            f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
        )


def init_db() -> None:
    """
    Existing v9 database is preserved.
    No tables are dropped and no existing records are deleted.
    """

    with db() as con:

        # Original v9 signals table.
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

        # Safe migrations for old signals table.
        signal_migrations = {
            "result_r": "REAL",
            "closed_at": "TEXT",
            "last_checked_ms": "INTEGER DEFAULT 0",
            "setup_key": "TEXT",
        }

        for name, definition in signal_migrations.items():
            add_column_if_missing(
                con,
                "signals",
                name,
                definition
            )

        # Manually entered balances.
        con.execute("""
            CREATE TABLE IF NOT EXISTS user_balances (
                chat_id INTEGER PRIMARY KEY,
                spot_balance REAL NOT NULL DEFAULT 0,
                futures_balance REAL NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
        """)

        # Personal trades are separate from demo signals.
        con.execute("""
            CREATE TABLE IF NOT EXISTS personal_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                signal_id INTEGER,
                created_at TEXT NOT NULL,
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,

                signal_entry REAL NOT NULL,
                entry REAL NOT NULL,

                sl REAL NOT NULL,
                initial_sl REAL NOT NULL,

                tp1 REAL NOT NULL,
                tp2 REAL NOT NULL,

                risk_amount REAL NOT NULL DEFAULT 0,
                position_notional REAL NOT NULL DEFAULT 0,
                position_quantity REAL NOT NULL DEFAULT 0,
                margin_required REAL NOT NULL DEFAULT 0,

                remaining_percent REAL NOT NULL DEFAULT 100,

                status TEXT NOT NULL DEFAULT 'ACTIVE',

                tp1_touched INTEGER NOT NULL DEFAULT 0,
                tp1_confirmed INTEGER NOT NULL DEFAULT 0,
                sl_moved_confirmed INTEGER NOT NULL DEFAULT 0,
                tp2_touched INTEGER NOT NULL DEFAULT 0,
                sl_touched INTEGER NOT NULL DEFAULT 0,
                reversal_warned INTEGER NOT NULL DEFAULT 0,

                last_checked_ms INTEGER NOT NULL DEFAULT 0,
                last_price REAL,
                last_recommendation TEXT DEFAULT '',

                trailing_sl REAL,
                monitoring_degraded INTEGER NOT NULL DEFAULT 0,

                closed_at TEXT,
                close_reason TEXT DEFAULT '',
                result_usdt REAL,
                result_r REAL,

                FOREIGN KEY(signal_id) REFERENCES signals(id)
            )
        """)

        # History of manual confirmations and important events.
        con.execute("""
            CREATE TABLE IF NOT EXISTS trade_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                details TEXT DEFAULT '',
                FOREIGN KEY(trade_id)
                    REFERENCES personal_trades(id)
            )
        """)

        # Connection and market data status.
        con.execute("""
            CREATE TABLE IF NOT EXISTS market_health (
                market TEXT PRIMARY KEY,
                connected INTEGER NOT NULL DEFAULT 0,
                last_message REAL NOT NULL DEFAULT 0,
                last_error TEXT DEFAULT '',
                reconnects INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
        """)

        # Cached closed candles for recovery and comparison.
        con.execute("""
            CREATE TABLE IF NOT EXISTS market_candles (
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                interval TEXT NOT NULL,
                open_time INTEGER NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL,
                PRIMARY KEY (
                    market,
                    symbol,
                    interval,
                    open_time
                )
            )
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_signals_status
            ON signals(status)
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_signals_setup
            ON signals(setup_key, created_at)
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_personal_active
            ON personal_trades(chat_id, status)
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_trade_events
            ON trade_events(trade_id, created_at)
        """)

        con.execute("""
            CREATE INDEX IF NOT EXISTS idx_candles_lookup
            ON market_candles(
                market,
                symbol,
                interval,
                open_time
            )
        """)

    log.info(
        "Database initialized without deleting old data: %s",
        DB_PATH
    )


# ============================================================
# 4. SETTINGS AND BALANCES
# ============================================================

def get_setting(
    key: str,
    default: str = ""
) -> str:

    with db() as con:

        row = con.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,)
        ).fetchone()

        return row["value"] if row else default


def set_setting(
    key: str,
    value: str
) -> None:

    with db() as con:

        con.execute("""
            INSERT INTO settings(key, value)
            VALUES(?, ?)
            ON CONFLICT(key)
            DO UPDATE SET value=excluded.value
        """, (key, str(value)))


def set_auto(
    chat_id: int,
    enabled: bool
) -> None:

    with db() as con:

        con.execute("""
            INSERT INTO subscribers(chat_id, auto_send)
            VALUES(?, ?)
            ON CONFLICT(chat_id)
            DO UPDATE SET auto_send=excluded.auto_send
        """, (chat_id, int(enabled)))


def auto_chats() -> list[int]:

    with db() as con:

        rows = con.execute("""
            SELECT chat_id
            FROM subscribers
            WHERE auto_send=1
        """).fetchall()

        return [
            int(row["chat_id"])
            for row in rows
        ]


def save_balance(
    chat_id: int,
    market: str,
    amount: float
) -> None:

    if amount < 0:
        raise ValueError("Баланс не может быть отрицательным")

    if market not in ("spot", "swap"):
        raise ValueError("Неизвестный рынок")

    with db() as con:

        con.execute("""
            INSERT OR IGNORE INTO user_balances(
                chat_id,
                spot_balance,
                futures_balance,
                updated_at
            )
            VALUES(?, 0, 0, ?)
        """, (chat_id, utc_text()))

        column = (
            "spot_balance"
            if market == "spot"
            else "futures_balance"
        )

        con.execute(
            f"""
            UPDATE user_balances
            SET {column}=?,
                updated_at=?
            WHERE chat_id=?
            """,
            (amount, utc_text(), chat_id)
        )


def get_balances(
    chat_id: int
) -> tuple[float, float]:

    with db() as con:

        row = con.execute("""
            SELECT spot_balance, futures_balance
            FROM user_balances
            WHERE chat_id=?
        """, (chat_id,)).fetchone()

        if not row:
            return 0.0, 0.0

        return (
            float(row["spot_balance"]),
            float(row["futures_balance"])
        )


# ============================================================
# END OF PART 1
# The next part continues in the same file.
# ============================================================

# ============================================================
# 5. BINGX REST API
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

                            message = (
                                f"BingX {code}: "
                                f"{payload.get('msg', '')}"
                            )

                            if str(code) == "100410":

                                _api_cooldown_until = (
                                    time.monotonic()
                                    + min(
                                        30,
                                        3 * (2 ** attempt)
                                    )
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
                        "BingX rate limit. Повтор %s/3",
                        attempt + 1
                    )
                    continue

                raise last_error

        except asyncio.CancelledError:
            raise

        except MarketError:
            raise

        except (
            aiohttp.ClientError,
            asyncio.TimeoutError
        ) as exc:

            last_error = MarketError(str(exc))

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
# 6. CANDLE NORMALIZATION
# ============================================================

def normalize_candles(
    payload: Any,
    interval: str,
    min_rows: int = 205
) -> pd.DataFrame:

    if interval not in TF_MS:
        raise MarketError(
            f"Неизвестный таймфрейм: {interval}"
        )

    raw = extract_data(payload)

    if isinstance(raw, dict):

        raw = raw.get(
            "data",
            raw.get("list", [])
        )

    if not isinstance(raw, list) or not raw:
        raise MarketError(
            "BingX вернула пустой список свечей"
        )

    rows = []

    for item in raw:

        try:

            if isinstance(item, (list, tuple)):

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

            if ot <= 0:
                continue

            if not all(
                np.isfinite(x) and x > 0
                for x in (o, h, l, c)
            ):
                continue

            if not np.isfinite(v) or v < 0:
                continue

            if h < max(o, c, l):
                continue

            if l > min(o, c, h):
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
            IndexError
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

    # Только полностью закрытые свечи.
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

        raise MarketError(
            f"Неизвестный рынок: {market}"
        )

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


# ============================================================
# 7. SAVE CLOSED CANDLES
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
                    market,
                    symbol,
                    interval,
                    open_time,
                    open,
                    high,
                    low,
                    close,
                    volume
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(
                    market,
                    symbol,
                    interval,
                    open_time
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

        # Ограничиваем размер истории в БД.
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
            market,
            symbol,
            interval,
            market,
            symbol,
            interval
        ))


# ============================================================
# 8. TECHNICAL INDICATORS
# ============================================================

def indicators(
    df: pd.DataFrame
) -> pd.DataFrame:

    x = df.copy()

    close = x["close"]
    high = x["high"]
    low = x["low"]

    x["ema20"] = close.ewm(
        span=20,
        adjust=False
    ).mean()

    x["ema50"] = close.ewm(
        span=50,
        adjust=False
    ).mean()

    x["ema200"] = close.ewm(
        span=200,
        adjust=False
    ).mean()

    # RSI with correct zero-gain / zero-loss handling.
    delta = close.diff()

    gain = delta.clip(
        lower=0
    ).ewm(
        alpha=1 / 14,
        adjust=False
    ).mean()

    loss = (-delta.clip(
        upper=0
    )).ewm(
        alpha=1 / 14,
        adjust=False
    ).mean()

    rs = gain / loss.replace(0, np.nan)

    rsi = 100 - 100 / (1 + rs)

    rsi = rsi.mask(
        (loss == 0) & (gain > 0),
        100
    )

    rsi = rsi.mask(
        (gain == 0) & (loss > 0),
        0
    )

    rsi = rsi.mask(
        (gain == 0) & (loss == 0),
        50
    )

    x["rsi"] = rsi.fillna(50)

    # ATR.
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

    # Directional movement.
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

    atr_safe = x["atr"].replace(
        0,
        np.nan
    )

    plus_di = (
        100
        * plus_dm.ewm(
            alpha=1 / 14,
            adjust=False
        ).mean()
        / atr_safe
    )

    minus_di = (
        100
        * minus_dm.ewm(
            alpha=1 / 14,
            adjust=False
        ).mean()
        / atr_safe
    )

    dx = (
        100
        * (plus_di - minus_di).abs()
        / (plus_di + minus_di).replace(
            0,
            np.nan
        )
    )

    x["adx"] = dx.ewm(
        alpha=1 / 14,
        adjust=False
    ).mean().fillna(0)

    x["plus_di"] = plus_di.fillna(0)
    x["minus_di"] = minus_di.fillna(0)

    x["macd"] = (
        close.ewm(
            span=12,
            adjust=False
        ).mean()
        -
        close.ewm(
            span=26,
            adjust=False
        ).mean()
    )

    x["macd_signal"] = x["macd"].ewm(
        span=9,
        adjust=False
    ).mean()

    return x


# ============================================================
# 9. SIGNAL STRATEGY
# ============================================================

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

    # Higher-timeframe trend: 1h and 4h.
    for row, label in (
        (c, "1H"),
        (d, "4H")
    ):

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

    # 15m trend.
    if (
        b.ema20 > b.ema50
        and b.close > b.ema20
    ):

        long_score += 2

        long_reasons.append(
            "15м: EMA20 выше EMA50"
        )

    if (
        b.ema20 < b.ema50
        and b.close < b.ema20
    ):

        short_score += 2

        short_reasons.append(
            "15м: EMA20 ниже EMA50"
        )

    # RSI.
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

    # MACD.
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

    # ADX and DI.
    if (
        b.adx >= 18
        and b.plus_di > b.minus_di
    ):

        long_score += 1

        long_reasons.append(
            f"ADX {b.adx:.1f}, покупатели сильнее"
        )

    if (
        b.adx >= 18
        and b.minus_di > b.plus_di
    ):

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

    # Spot is BUY-only.
    if market == "spot" and side == "SELL":
        return None

    # Preserve original v9.1 stop calculation.
    sl_dist = max(
        1.5 * atr,
        price * 0.002
    )

    # Two targets in R units.
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

    rr = abs(tp1 - price) / abs(price - sl)

    if rr < 1.8:
        return None

    return {
        "market": market,
        "symbol": symbol,
        "side": side,

        "entry": price,
        "sl": sl,

        "tp1": tp1,
        "tp2": tp2,

        # Compatibility with old v9.1 signal records.
        "tp": tp2,

        "rr": rr,
        "score": score,
        "reasons": reasons,

        "atr": atr,

        "setup_key": (
            f"{market}:{symbol}:{side}:"
            f"{int(b.open_time)}"
        )
    }


# ============================================================
# 10. SIGNAL STORAGE
# ============================================================

def save_signal(
    sig: dict[str, Any]
) -> int | None:

    now = utc_text()

    with db() as con:

        exists = con.execute("""
            SELECT id
            FROM signals
            WHERE setup_key=?
        """, (
            sig["setup_key"],
        )).fetchone()

        recent = con.execute("""
            SELECT id
            FROM signals
            WHERE market=?
              AND symbol=?
              AND side=?
              AND julianday(created_at)
                  >= julianday('now', '-45 minutes')
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
            INSERT INTO signals(
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
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            now,
            sig["market"],
            sig["symbol"],
            sig["side"],
            sig["entry"],
            sig["sl"],
            sig["tp2"],
            sig["rr"],
            sig["score"],
            "; ".join(sig["reasons"]),
            sig["setup_key"]
        ))

        return int(cur.lastrowid)


# ============================================================
# END OF PART 2
# ============================================================

# =========================
# PART 3 / 4
# PERSONAL TRADES AND MONITORING
# =========================

def get_signal(signal_id):
    with db() as con:
        return con.execute(
            "SELECT * FROM signals WHERE id=?",
            (signal_id,)
        ).fetchone()


def get_trade(trade_id):
    with db() as con:
        return con.execute(
            "SELECT * FROM personal_trades WHERE id=?",
            (trade_id,)
        ).fetchone()


def get_active_trades(chat_id):
    with db() as con:
        return con.execute("""
            SELECT * FROM personal_trades
            WHERE chat_id=? AND status='ACTIVE'
            ORDER BY created_at DESC
        """, (chat_id,)).fetchall()


def active_trade_count(chat_id):
    with db() as con:
        row = con.execute("""
            SELECT COUNT(*) AS n FROM personal_trades
            WHERE chat_id=? AND status='ACTIVE'
        """, (chat_id,)).fetchone()
    return int(row["n"])


def add_trade_event(trade_id, event_type, message, value=None):
    with db() as con:
        con.execute("""
            INSERT INTO trade_events
            (trade_id, created_at, event_type, message, value)
            VALUES (?, ?, ?, ?, ?)
        """, (
            trade_id,
            utc_text(),
            event_type,
            message,
            None if value is None else str(value)
        ))


def update_trade(trade_id, **fields):
    allowed_fields = {
        "entry", "sl", "remaining_percent", "status",
        "tp1_touched", "tp1_confirmed", "sl_moved_confirmed",
        "tp2_touched", "sl_touched", "reversal_warned",
        "last_checked_ms", "last_price", "last_recommendation",
        "trailing_sl", "monitoring_degraded", "closed_at",
        "close_reason", "result_usdt", "result_r"
    }

    fields = {
        key: value for key, value in fields.items()
        if key in allowed_fields
    }

    if not fields:
        return

    sql = ", ".join(f"{key}=?" for key in fields)
    values = list(fields.values())
    values.append(trade_id)

    with db() as con:
        con.execute(
            f"UPDATE personal_trades SET {sql} WHERE id=?",
            values
        )


def calculate_position_size(market, entry, sl, balance):
    entry = safe_float(entry)
    sl = safe_float(sl)
    balance = safe_float(balance)

    if entry <= 0 or sl <= 0 or balance <= 0:
        return None

    distance = abs(entry - sl)

    if distance <= 0:
        return None

    risk_amount = balance * RISK_PERCENT

    if market == "spot":
        # Spot is BUY-only and does not use leverage.
        quantity = risk_amount / distance
        notional = quantity * entry
        notional = min(notional, balance)
        quantity = notional / entry
        margin = notional
    else:
        # Linear USDT-M Futures approximation.
        risk_fraction = distance / entry
        if risk_fraction <= 0:
            return None

        notional = risk_amount / risk_fraction
        quantity = notional / entry
        margin = notional / FUTURES_LEVERAGE

    return {
        "risk_amount": risk_amount,
        "position_notional": notional,
        "position_quantity": quantity,
        "margin_required": margin
    }


def make_trade_levels(side, entry, sl):
    risk = abs(entry - sl)

    if risk <= 0:
        return None

    if side == "LONG":
        tp1 = entry + risk * TP1_R
        tp2 = entry + risk * TP2_R
    else:
        tp1 = entry - risk * TP1_R
        tp2 = entry - risk * TP2_R

    return tp1, tp2


def signal_keyboard(signal_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Я вошёл в сделку",
                callback_data=f"enter:{signal_id}"
            )
        ]
    ])


def trade_keyboard(trade_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Обновить данные",
                callback_data=f"trade_refresh:{trade_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text="Изменить фактический вход",
                callback_data=f"trade_entry:{trade_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text="Обновить SL",
                callback_data=f"trade_sl:{trade_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text="Закрыть сделку",
                callback_data=f"trade_close:{trade_id}"
            )
        ]
    ])


def tp1_keyboard(trade_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="70% позиции закрыто",
                callback_data=f"tp1_done:{trade_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text="SL перенесён на цену входа",
                callback_data=f"be_done:{trade_id}"
            )
        ]
    ])


def sl_keyboard(trade_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Подтвердить закрытие",
                callback_data=f"sl_done:{trade_id}"
            )
        ],
        [
            InlineKeyboardButton(
                text="Позиция ещё открыта",
                callback_data=f"sl_keep:{trade_id}"
            )
        ]
    ])


def reversal_keyboard(trade_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Закрыл сделку",
                callback_data=f"reversal_close:{trade_id}"
            ),
            InlineKeyboardButton(
                text="Оставить сделку",
                callback_data=f"reversal_keep:{trade_id}"
            )
        ]
    ])


def trade_text(trade):
    market_name = "Spot" if trade["market"] == "spot" else "USDT-M Futures"

    return (
        f"📊 **Сделка #{trade['id']}**\n\n"
        f"Рынок: {market_name}\n"
        f"Монета: {trade['symbol']}\n"
        f"Направление: {trade['side']}\n"
        f"Состояние: {trade['status']}\n\n"
        f"Фактический вход: {fmt_price(trade['entry'])}\n"
        f"Текущая цена: {fmt_price(trade['last_price'])}\n"
        f"Stop Loss: {fmt_price(trade['sl'])}\n"
        f"TP1: {fmt_price(trade['tp1'])}\n"
        f"TP2: {fmt_price(trade['tp2'])}\n\n"
        f"Остаток позиции: {trade['remaining_percent']}%\n"
        f"Последняя рекомендация:\n"
        f"{trade['last_recommendation'] or 'Нет новых рекомендаций'}"
    )


def create_personal_trade(chat_id, signal, actual_entry):
    if active_trade_count(chat_id) >= MAX_ACTIVE_TRADES:
        return None, "Достигнут лимит: одновременно можно сопровождать не более 5 сделок."

    entry = safe_float(actual_entry)
    signal_entry = safe_float(signal["entry"])
    sl = safe_float(signal["sl"])

    if entry <= 0 or signal_entry <= 0 or sl <= 0:
        return None, "Не удалось проверить цены сигнала."

    side = signal["side"]
    market = signal["market"]

    if market == "spot" and side != "LONG":
        return None, "Для Spot разрешены только покупки."

    # A signal is not valid for a new entry if its original SL or TP2
    # was already reached before the user confirmed the entry.
    current = latest_prices.get(
        (market, signal["symbol"]),
        signal_entry
    )

    original_tp2 = safe_float(signal["tp2"], safe_float(signal["tp"]))

    if side == "LONG":
        invalidated = current <= sl or current >= original_tp2
    else:
        invalidated = current >= sl or current <= original_tp2

    if invalidated:
        return None, (
            "Исходный сценарий уже достиг одного из контрольных уровней. "
            "Этот прогноз нельзя использовать для нового входа."
        )

    levels = make_trade_levels(side, entry, sl)

    if not levels:
        return None, "Не удалось рассчитать уровни сделки."

    tp1, tp2 = levels

    balances = get_balances(chat_id)
    balance = balances.get(market)

    if balance is None or balance <= 0:
        return None, (
            "Сначала укажи баланс для этого рынка в настройках бота. "
            "Без него нельзя рассчитать размер позиции."
        )

    sizing = calculate_position_size(market, entry, sl, balance)

    if not sizing:
        return None, "Не удалось рассчитать размер позиции."

    drift_r = abs(entry - signal_entry) / abs(signal_entry - sl)

    warning = ""
    if drift_r > ENTRY_WARNING_R:
        warning = (
            "⚠️ Фактический вход заметно отличается от цены прогноза. "
            "Уровни пересчитаны от твоей фактической цены входа.\n\n"
        )

    trade_id = str(uuid.uuid4())

    with db() as con:
        con.execute("""
            INSERT INTO personal_trades (
                id, chat_id, signal_id, created_at,
                market, symbol, side, signal_entry,
                entry, sl, initial_sl, tp1, tp2,
                risk_amount, position_notional,
                position_quantity, margin_required,
                remaining_percent, status,
                last_price, last_recommendation,
                monitoring_degraded
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 100,
                    'ACTIVE', ?, ?, 0)
        """, (
            trade_id,
            chat_id,
            signal["id"],
            utc_text(),
            market,
            signal["symbol"],
            side,
            signal_entry,
            entry,
            sl,
            sl,
            tp1,
            tp2,
            sizing["risk_amount"],
            sizing["position_notional"],
            sizing["position_quantity"],
            sizing["margin_required"],
            current,
            "Сделка принята в сопровождение."
        ))

    add_trade_event(
        trade_id,
        "OPEN",
        "Пользователь подтвердил вход и указал фактическую цену.",
        entry
    )

    return trade_id, warning


async def send_trade_message(bot, chat_id, trade_id, extra=""):
    trade = get_trade(trade_id)
    if not trade:
        return

    text = trade_text(trade)

    if extra:
        text = extra + "\n\n" + text

    await bot.send_message(
        chat_id,
        text,
        reply_markup=trade_keyboard(trade_id),
        parse_mode="Markdown"
    )


async def send_once(bot, trade, key, message, keyboard=None):
    # Store a notification key in the trade event history to avoid spam.
    with db() as con:
        exists = con.execute("""
            SELECT 1 FROM trade_events
            WHERE trade_id=? AND event_type=?
            LIMIT 1
        """, (trade["id"], key)).fetchone()

    if exists:
        return

    add_trade_event(trade["id"], key, message)

    await bot.send_message(
        trade["chat_id"],
        message,
        reply_markup=keyboard
    )


def price_reached(side, price, level, direction):
    if side == "LONG":
        return price >= level if direction == "up" else price <= level
    return price <= level if direction == "up" else price >= level


async def check_trade(bot, trade):
    if trade["status"] != "ACTIVE":
        return

    key = (trade["market"], trade["symbol"])
    price = latest_prices.get(key)

    if price is None or price <= 0:
        update_trade(
            trade["id"],
            monitoring_degraded=1,
            last_recommendation="Нет актуальной рыночной цены. Сопровождение ограничено."
        )
        return

    update_trade(
        trade["id"],
        last_price=price,
        monitoring_degraded=0
    )

    side = trade["side"]
    entry = safe_float(trade["entry"])
    sl = safe_float(trade["sl"])
    tp1 = safe_float(trade["tp1"])
    tp2 = safe_float(trade["tp2"])

    # SL is a touch notification, not an automatic close.
    sl_hit = (
        price <= sl if side == "LONG" else price >= sl
    )

    if sl_hit and not trade["sl_touched"]:
        update_trade(
            trade["id"],
            sl_touched=1,
            last_recommendation="Цена коснулась Stop Loss. Подтверди фактическое закрытие на бирже."
        )
        await send_once(
            bot,
            trade,
            "SL_TOUCHED",
            f"⚠️ {trade['symbol']}: цена коснулась Stop Loss.\n"
            "Бот не закрывает позицию автоматически. Подтверди, что фактически произошло на бирже.",
            sl_keyboard(trade["id"])
        )
        return

    # TP1: touch only; user must confirm 70% close and SL move.
    tp1_hit = (
        price >= tp1 if side == "LONG" else price <= tp1
    )

    if tp1_hit and not trade["tp1_touched"]:
        update_trade(
            trade["id"],
            tp1_touched=1,
            last_recommendation="Цена достигла TP1. Требуется ручная фиксация 70%."
        )
        await send_once(
            bot,
            trade,
            "TP1_TOUCHED",
            f"🎯 {trade['symbol']}: цена достигла TP1 ({fmt_price(tp1)}).\n"
            "Рассмотри фиксацию 70% позиции вручную. Затем подтверди закрытие и перенос SL.",
            tp1_keyboard(trade["id"])
        )

    # TP2: notify, but never mark the trade closed without confirmation.
    tp2_hit = (
        price >= tp2 if side == "LONG" else price <= tp2
    )

    if tp2_hit and not trade["tp2_touched"]:
        update_trade(
            trade["id"],
            tp2_touched=1,
            last_recommendation="Цена достигла TP2. Требуется подтверждение фактического закрытия."
        )
        await send_once(
            bot,
            trade,
            "TP2_TOUCHED",
            f"🏁 {trade['symbol']}: цена достигла TP2 ({fmt_price(tp2)}).\n"
            "Если ты закрыл оставшуюся позицию на бирже, нажми «Закрыть сделку» в разделе «Мои сделки»."
        )

    # After TP1 is confirmed and SL moved to entry, calculate a
    # conservative trailing suggestion using closed 5m candles only.
    refreshed = get_trade(trade["id"])

    if refreshed and refreshed["tp1_confirmed"] and refreshed["sl_moved_confirmed"]:
        await update_trailing_stop(bot, refreshed)

    await check_reversal_warning(bot, get_trade(trade["id"]))


async def update_trailing_stop(bot, trade):
    try:
        candles = await get_klines(
            trade["market"],
            trade["symbol"],
            "5m",
            limit=40
        )
    except Exception:
        return

    if len(candles) < TRAILING_LOOKBACK + 2:
        return

    closed = candles[-(TRAILING_LOOKBACK + 1):-1]
    if len(closed) < TRAILING_LOOKBACK:
        return

    df = pd.DataFrame(closed)
    atr = indicators(df)["atr"]
    if atr is None or atr <= 0:
        return

    side = trade["side"]
    current_sl = safe_float(trade["sl"])
    entry = safe_float(trade["entry"])

    if side == "LONG":
        candidate = float(df["low"].min()) - atr * TRAILING_ATR_MULTIPLIER
        candidate = max(candidate, entry)

        # Never move a long stop down.
        if candidate > current_sl:
            update_trade(
                trade["id"],
                sl=candidate,
                trailing_sl=candidate,
                last_recommendation=f"Трейлинг-уровень: {fmt_price(candidate)}. Перенеси его вручную, если согласен."
            )
            await send_once(
                bot,
                trade,
                f"TRAIL_{int(now_ms() // 300000)}",
                f"🔒 {trade['symbol']}: новый расчётный трейлинг-уровень {fmt_price(candidate)}.\n"
                "Это рекомендация. Стоп на бирже не изменён автоматически."
            )

    else:
        candidate = float(df["high"].max()) + atr * TRAILING_ATR_MULTIPLIER
        candidate = min(candidate, entry)

        # Never move a short stop up.
        if current_sl <= 0 or candidate < current_sl:
            update_trade(
                trade["id"],
                sl=candidate,
                trailing_sl=candidate,
                last_recommendation=f"Трейлинг-уровень: {fmt_price(candidate)}. Перенеси его вручную, если согласен."
            )
            await send_once(
                bot,
                trade,
                f"TRAIL_{int(now_ms() // 300000)}",
                f"🔒 {trade['symbol']}: новый расчётный трейлинг-уровень {fmt_price(candidate)}.\n"
                "Это рекомендация. Стоп на бирже не изменён автоматически."
            )


async def check_reversal_warning(bot, trade):
    if not trade or trade["reversal_warned"]:
        return

    try:
        candles = await get_klines(
            trade["market"],
            trade["symbol"],
            "15m",
            limit=80
        )
    except Exception:
        return

    if len(candles) < 30:
        return

    df = pd.DataFrame(candles)
    ind = indicators(df)

    if ind["ema20"] is None or ind["ema50"] is None or ind["rsi"] is None:
        return

    close = float(df.iloc[-1]["close"])

    if trade["side"] == "LONG":
        reversal = (
            close < ind["ema20"]
            and ind["ema20"] < ind["ema50"]
            and ind["rsi"] < 42
        )
    else:
        reversal = (
            close > ind["ema20"]
            and ind["ema20"] > ind["ema50"]
            and ind["rsi"] > 58
        )

    if reversal:
        update_trade(
            trade["id"],
            reversal_warned=1,
            last_recommendation="Первоначальный сценарий больше не подтверждается. Рассмотри закрытие оставшейся позиции."
        )
        await send_once(
            bot,
            trade,
            "REVERSAL_WARNING",
            f"⚠️ {trade['symbol']}: первоначальный сценарий больше не подтверждается.\n"
            "Рассмотри закрытие оставшейся позиции. Бот не закрывает её автоматически.",
            reversal_keyboard(trade["id"])
        )


async def finalize_trade(trade_id, reason, result_usdt=None, result_r=None):
    trade = get_trade(trade_id)
    if not trade:
        return

    update_trade(
        trade_id,
        status="CLOSED",
        closed_at=utc_text(),
        close_reason=reason,
        result_usdt=result_usdt,
        result_r=result_r,
        remaining_percent=0,
        last_recommendation=f"Сделка завершена: {reason}"
    )

    add_trade_event(
        trade_id,
        "CLOSED",
        f"Сделка закрыта пользователем: {reason}"
    )


async def monitor_personal_trades(bot):
    while True:
        try:
            with db() as con:
                trades = con.execute("""
                    SELECT * FROM personal_trades
                    WHERE status='ACTIVE'
                """).fetchall()

            for trade in trades:
                try:
                    await check_trade(bot, trade)
                except Exception:
                    logging.exception(
                        "Trade monitor error: %s",
                        trade["id"]
                    )

        except Exception:
            logging.exception("Personal trade monitor failed")

        await asyncio.sleep(MONITOR_INTERVAL)


# END OF PART 3

# =========================
# PART 4 / 4
# TELEGRAM INTERFACE AND STARTUP
# =========================

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
import inspect


# Temporary input states.
pending_entry = {}
pending_balance = {}
pending_trade_entry = {}
pending_trade_sl = {}


def main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="📈 Анализ рынка"),
                KeyboardButton(text="📊 Мои сделки")
            ],
            [
                KeyboardButton(text="📚 История"),
                KeyboardButton(text="📉 Статистика")
            ],
            [
                KeyboardButton(text="⚙️ Настройки"),
                KeyboardButton(text="🩺 Состояние системы")
            ],
            [
                KeyboardButton(text="▶️ Автоанализ"),
                KeyboardButton(text="⏹ Остановить автоанализ")
            ]
        ],
        resize_keyboard=True
    )


def signal_message(signal):
    market_name = (
        "Spot"
        if signal["market"] == "spot"
        else "USDT-M Futures"
    )

    return (
        f"📊 **Новый торговый сигнал**\n\n"
        f"Рынок: {market_name}\n"
        f"Монета: {signal['symbol']}\n"
        f"Направление: {signal['side']}\n\n"
        f"Вход по расчёту: {fmt_price(signal['entry'])}\n"
        f"Stop Loss: {fmt_price(signal['sl'])}\n"
        f"TP1 — 1.8R: {fmt_price(signal['tp1'])}\n"
        f"TP2 — 3R: {fmt_price(signal['tp2'])}\n\n"
        f"Риск на сделку: {RISK_PERCENT * 100:.0f}% баланса\n"
        f"Плечо Futures: {FUTURES_LEVERAGE}x\n\n"
        f"⚠️ Это аналитический сигнал, а не автоматическая сделка. "
        f"Проверь уровни самостоятельно перед входом."
    )


async def notify_new_signal(bot, signal_id):
    signal = get_signal(signal_id)
    if not signal:
        return

    text = signal_message(signal)

    for chat_id in auto_chats():
        try:
            await bot.send_message(
                chat_id,
                text,
                reply_markup=signal_keyboard(signal_id),
                parse_mode="Markdown"
            )
        except Exception:
            logging.exception(
                "Could not send signal to chat %s",
                chat_id
            )


async def scan_market(market, symbol):
    """
    Получает свечи и рассчитывает сигнал.
    Сигналы стратегии рассчитываются по закрытым свечам.
    """
    try:
        frames = {}

        for timeframe in ("5m", "15m", "1h", "4h"):
            candles = await get_klines(
                market,
                symbol,
                timeframe,
                limit=250
            )

            if len(candles) < 60:
                return None

            frames[timeframe] = pd.DataFrame(candles)

        # build_signal is the strategy function from Part 2.
        # It receives the market, symbol and prepared timeframe data.
        result = build_signal(market, symbol, frames)

        if not result:
            return None

        if not isinstance(result, dict):
            return None

        result["market"] = market
        result["symbol"] = symbol

        required = (
            "side", "entry", "sl", "tp1", "tp2",
            "score", "setup_key"
        )

        if any(key not in result for key in required):
            logging.warning(
                "Signal has missing fields: %s %s",
                market,
                symbol
            )
            return None

        return result

    except Exception:
        logging.exception(
            "Scan error: %s %s",
            market,
            symbol
        )
        return None


async def scan_all():
    found = []

    for market in MARKETS:
        for symbol in SYMBOLS:
            signal = await scan_market(market, symbol)

            if not signal:
                continue

            try:
                saved_id = save_signal(signal)

                if saved_id:
                    found.append(saved_id)

            except Exception:
                logging.exception(
                    "Could not save signal: %s %s",
                    market,
                    symbol
                )

    return found


async def background_scanner(bot):
    while True:
        try:
            chats = auto_chats()

            if chats:
                signal_ids = await scan_all()

                for signal_id in signal_ids:
                    await notify_new_signal(bot, signal_id)

        except Exception:
            logging.exception("Background scanner error")

        await asyncio.sleep(SCAN_INTERVAL)


async def background_monitor(bot):
    while True:
        try:
            # Existing demo-signal monitoring from the original bot.
            try:
                await monitor_open_signals()
            except Exception:
                logging.exception("Demo signal monitor error")

        except Exception:
            logging.exception("Background monitor error")

        await asyncio.sleep(MONITOR_INTERVAL)


async def send_my_trades(message):
    trades = get_active_trades(message.chat.id)

    if not trades:
        await message.answer(
            "У тебя пока нет активных сопровождаемых сделок.",
            reply_markup=main_keyboard()
        )
        return

    for trade in trades:
        await message.answer(
            trade_text(trade),
            reply_markup=trade_keyboard(trade["id"]),
            parse_mode="Markdown"
        )


async def send_history(message):
    with db() as con:
        rows = con.execute("""
            SELECT * FROM personal_trades
            WHERE chat_id=? AND status='CLOSED'
            ORDER BY closed_at DESC
            LIMIT 30
        """, (message.chat.id,)).fetchall()

    if not rows:
        await message.answer("История завершённых сделок пока пуста.")
        return

    for trade in rows:
        result = trade["result_usdt"]
        result_r = trade["result_r"]

        result_text = (
            "Не рассчитан"
            if result is None
            else f"{safe_float(result):.4f} USDT"
        )

        r_text = (
            "Не рассчитан"
            if result_r is None
            else f"{safe_float(result_r):.2f}R"
        )

        await message.answer(
            f"Сделка #{trade['id']}\n"
            f"{trade['symbol']} — {trade['side']}\n"
            f"Причина закрытия: {trade['close_reason'] or 'Не указана'}\n"
            f"Результат: {result_text}\n"
            f"Результат в R: {r_text}\n"
            f"Открыта: {trade['created_at']}\n"
            f"Закрыта: {trade['closed_at'] or 'Не указано'}"
        )


async def send_statistics(message):
    with db() as con:
        total = con.execute("""
            SELECT COUNT(*) AS n
            FROM signals
        """).fetchone()["n"]

        wins = con.execute("""
            SELECT COUNT(*) AS n
            FROM signals
            WHERE result='WIN'
        """).fetchone()["n"]

        losses = con.execute("""
            SELECT COUNT(*) AS n
            FROM signals
            WHERE result='LOSS'
        """).fetchone()["n"]

        active = active_trade_count(message.chat.id)

    await message.answer(
        "📉 **Статистика бота**\n\n"
        f"Всего сохранено сигналов: {total}\n"
        f"WIN: {wins}\n"
        f"LOSS: {losses}\n"
        f"Твои активные сделки: {active}/{MAX_ACTIVE_TRADES}\n\n"
        "Статистика сигналов и личных сделок ведётся отдельно.",
        parse_mode="Markdown"
    )


async def send_settings(message):
    balances = get_balances(message.chat.id)

    spot_balance = balances.get("spot")
    futures_balance = balances.get("swap")

    spot_text = (
        "не задан"
        if spot_balance is None
        else f"{spot_balance:.2f} USDT"
    )

    futures_text = (
        "не задан"
        if futures_balance is None
        else f"{futures_balance:.2f} USDT"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="Указать баланс Spot",
                callback_data="balance:spot"
            )
        ],
        [
            InlineKeyboardButton(
                text="Указать баланс Futures",
                callback_data="balance:swap"
            )
        ]
    ])

    await message.answer(
        "⚙️ **Настройки риска**\n\n"
        f"Риск на сделку: {RISK_PERCENT * 100:.0f}%\n"
        f"Плечо Futures: {FUTURES_LEVERAGE}x\n"
        f"TP1: {TP1_R}R — фиксация 70%\n"
        f"TP2: {TP2_R}R — фиксация оставшихся 30%\n\n"
        f"Баланс Spot: {spot_text}\n"
        f"Баланс Futures: {futures_text}\n\n"
        "Укажи балансы вручную. Бот не получает доступ к средствам "
        "и не подключается к торговым API-ключам.",
        reply_markup=keyboard,
        parse_mode="Markdown"
    )


async def send_system_status(message):
    lines = ["🩺 Состояние системы\n"]

    for market in ("spot", "swap"):
        health = ws_health.get(market, {})
        status = health.get("status", "REST")
        last_message = health.get("last_message")

        lines.append(
            f"{market.upper()}: {status}; "
            f"последнее сообщение: {last_message or 'нет данных'}"
        )

    lines.append(
        "\nПубличные рыночные данные используются без торговых API-ключей."
    )
    lines.append(
        "Если источник данных недоступен, бот не должен считать "
        "отсутствующие цены подтверждением сигнала."
    )

    await message.answer("\n".join(lines))


# =========================
# TELEGRAM CALLBACKS
# =========================

async def register_callbacks(dp, bot):

    @dp.callback_query(F.data.startswith("enter:"))
    async def enter_signal(callback: CallbackQuery):
        try:
            signal_id = int(callback.data.split(":", 1)[1])
        except Exception:
            await callback.answer("Некорректный номер сигнала.", show_alert=True)
            return

        signal = get_signal(signal_id)

        if not signal:
            await callback.answer("Сигнал не найден.", show_alert=True)
            return

        if active_trade_count(callback.message.chat.id) >= MAX_ACTIVE_TRADES:
            await callback.answer(
                "Уже сопровождаются 5 сделок. Сначала заверши одну.",
                show_alert=True
            )
            return

        pending_entry[callback.message.chat.id] = signal_id

        await callback.message.answer(
            "Введи фактическую цену, по которой ты вошёл в сделку.\n\n"
            "Укажи только число, например: 67250.5"
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("trade_refresh:"))
    async def refresh_trade(callback: CallbackQuery):
        trade_id = callback.data.split(":", 1)[1]
        trade = get_trade(trade_id)

        if not trade or trade["chat_id"] != callback.message.chat.id:
            await callback.answer("Сделка не найдена.", show_alert=True)
            return

        await callback.message.answer(
            trade_text(trade),
            reply_markup=trade_keyboard(trade_id),
            parse_mode="Markdown"
        )
        await callback.answer("Данные обновлены.")

    @dp.callback_query(F.data.startswith("trade_entry:"))
    async def change_trade_entry(callback: CallbackQuery):
        trade_id = callback.data.split(":", 1)[1]
        trade = get_trade(trade_id)

        if not trade or trade["chat_id"] != callback.message.chat.id:
            await callback.answer("Сделка не найдена.", show_alert=True)
            return

        pending_trade_entry[callback.message.chat.id] = trade_id
        await callback.message.answer(
            "Введи правильную фактическую цену входа числом."
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("trade_sl:"))
    async def change_trade_sl(callback: CallbackQuery):
        trade_id = callback.data.split(":", 1)[1]
        trade = get_trade(trade_id)

        if not trade or trade["chat_id"] != callback.message.chat.id:
            await callback.answer("Сделка не найдена.", show_alert=True)
            return

        pending_trade_sl[callback.message.chat.id] = trade_id
        await callback.message.answer(
            "Введи новый Stop Loss, который ты уже установил на бирже."
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("trade_close:"))
    async def ask_close_trade(callback: CallbackQuery):
        trade_id = callback.data.split(":", 1)[1]

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, закрыть сопровождение",
                    callback_data=f"confirm_close:{trade_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    text="Отмена",
                    callback_data="cancel_close"
                )
            ]
        ])

        await callback.message.answer(
            "Подтверди: ты действительно закрыл позицию на бирже "
            "и хочешь завершить её сопровождение?",
            reply_markup=keyboard
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("confirm_close:"))
    async def confirm_close_trade(callback: CallbackQuery):
        trade_id = callback.data.split(":", 1)[1]
        trade = get_trade(trade_id)

        if not trade or trade["chat_id"] != callback.message.chat.id:
            await callback.answer("Сделка не найдена.", show_alert=True)
            return

        await finalize_trade(trade_id, "Закрыта пользователем")
        await callback.message.answer("Сопровождение сделки завершено.")
        await callback.answer()

    @dp.callback_query(F.data == "cancel_close")
    async def cancel_close_trade(callback: CallbackQuery):
        await callback.answer("Закрытие отменено.")

    @dp.callback_query(F.data.startswith("tp1_done:"))
    async def confirm_tp1(callback: CallbackQuery):
        trade_id = callback.data.split(":", 1)[1]
        trade = get_trade(trade_id)

        if not trade or trade["chat_id"] != callback.message.chat.id:
            await callback.answer("Сделка не найдена.", show_alert=True)
            return

        update_trade(
            trade_id,
            tp1_confirmed=1,
            remaining_percent=30,
            last_recommendation="Пользователь подтвердил фиксацию 70% позиции."
        )

        add_trade_event(
            trade_id,
            "TP1_CONFIRMED",
            "Пользователь подтвердил закрытие 70% позиции."
        )

        await callback.message.answer(
            "Зафиксировано подтверждение закрытия 70% позиции.\n"
            "Теперь отдельно подтверди перенос SL на фактическую цену входа."
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("be_done:"))
    async def confirm_break_even(callback: CallbackQuery):
        trade_id = callback.data.split(":", 1)[1]
        trade = get_trade(trade_id)

        if not trade or trade["chat_id"] != callback.message.chat.id:
            await callback.answer("Сделка не найдена.", show_alert=True)
            return

        if not trade["tp1_confirmed"]:
            await callback.answer(
                "Сначала подтверди фиксацию 70% позиции.",
                show_alert=True
            )
            return

        update_trade(
            trade_id,
            sl=trade["entry"],
            sl_moved_confirmed=1,
            last_recommendation="TP1 подтверждён. Перенос SL на вход подтверждён пользователем."
        )

        add_trade_event(
            trade_id,
            "BREAK_EVEN_CONFIRMED",
            "Пользователь подтвердил перенос SL на цену входа."
        )

        await callback.message.answer(
            "Подтверждение принято. Бот продолжит сопровождать оставшиеся 30%."
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("sl_done:"))
    async def confirm_sl_close(callback: CallbackQuery):
        trade_id = callback.data.split(":", 1)[1]
        trade = get_trade(trade_id)

        if not trade or trade["chat_id"] != callback.message.chat.id:
            await callback.answer("Сделка не найдена.", show_alert=True)
            return

        await finalize_trade(trade_id, "Stop Loss — закрытие подтверждено пользователем")
        await callback.message.answer(
            "Закрытие подтверждено. Сопровождение завершено."
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("sl_keep:"))
    async def keep_after_sl(callback: CallbackQuery):
        trade_id = callback.data.split(":", 1)[1]
        trade = get_trade(trade_id)

        if not trade or trade["chat_id"] != callback.message.chat.id:
            await callback.answer("Сделка не найдена.", show_alert=True)
            return

        update_trade(
            trade_id,
            sl_touched=1,
            last_recommendation="Пользователь сообщил, что позиция остаётся открытой."
        )

        await callback.message.answer(
            "Понял. Сопровождение продолжается. Проверь фактический Stop Loss на бирже."
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("reversal_close:"))
    async def reversal_close(callback: CallbackQuery):
        trade_id = callback.data.split(":", 1)[1]
        trade = get_trade(trade_id)

        if not trade or trade["chat_id"] != callback.message.chat.id:
            await callback.answer("Сделка не найдена.", show_alert=True)
            return

        await finalize_trade(
            trade_id,
            "Пользователь закрыл сделку после предупреждения о сломе сценария"
        )
        await callback.message.answer("Сделка отмечена как закрытая.")
        await callback.answer()

    @dp.callback_query(F.data.startswith("reversal_keep:"))
    async def reversal_keep(callback: CallbackQuery):
        trade_id = callback.data.split(":", 1)[1]
        trade = get_trade(trade_id)

        if not trade or trade["chat_id"] != callback.message.chat.id:
            await callback.answer("Сделка не найдена.", show_alert=True)
            return

        update_trade(
            trade_id,
            last_recommendation="Пользователь решил оставить сделку открытой."
        )

        await callback.message.answer(
            "Сопровождение продолжается. Бот будет следить за изменением сценария."
        )
        await callback.answer()

    @dp.callback_query(F.data.startswith("balance:"))
    async def set_balance_prompt(callback: CallbackQuery):
        market = callback.data.split(":", 1)[1]

        if market not in ("spot", "swap"):
            await callback.answer("Неизвестный рынок.", show_alert=True)
            return

        pending_balance[callback.message.chat.id] = market

        await callback.message.answer(
            "Введи текущий баланс в USDT числом, например: 250"
        )
        await callback.answer()


# =========================
# MESSAGE HANDLERS
# =========================

async def register_messages(dp, bot):

    @dp.message(CommandStart())
    async def start_command(message):
        if not allowed(message.from_user.id):
            await message.answer("Доступ к боту не разрешён.")
            return

        await message.answer(
            "Привет! Это Trading Bot v9.2.\n\n"
            "Бот анализирует публичные рыночные данные BingX "
            "и отправляет торговые сигналы.\n\n"
            "Он не размещает ордера, не перемещает стопы "
            "и не управляет твоими средствами.\n\n"
            "Чтобы сопровождать прогноз как личную сделку, "
            "нажми под ним «Я вошёл в сделку» и укажи фактическую цену входа.",
            reply_markup=main_keyboard()
        )

    @dp.message(Command("scan"))
    async def scan_command(message):
        if not allowed(message.from_user.id):
            return

        await message.answer("Начинаю анализ рынка. Это может занять некоторое время.")

        signal_ids = await scan_all()

        if not signal_ids:
            await message.answer(
                "Новых подтверждённых сигналов не найдено."
            )
            return

        await message.answer(
            f"Анализ завершён. Найдено новых сигналов: {len(signal_ids)}"
        )

        for signal_id in signal_ids:
            signal = get_signal(signal_id)

            if signal:
                await message.answer(
                    signal_message(signal),
                    reply_markup=signal_keyboard(signal_id),
                    parse_mode="Markdown"
                )

    @dp.message(Command("auto_on"))
    async def auto_on_command(message):
        if not allowed(message.from_user.id):
            return

        set_auto(message.chat.id, True)
        await message.answer("Автоанализ включён.")

    @dp.message(Command("auto_off"))
    async def auto_off_command(message):
        if not allowed(message.from_user.id):
            return

        set_auto(message.chat.id, False)
        await message.answer("Автоанализ выключен.")

    @dp.message(F.text)
    async def text_handler(message):
        if not allowed(message.from_user.id):
            return

        chat_id = message.chat.id
        text_value = message.text.strip()

        # User is entering the actual entry price for a new trade.
        if chat_id in pending_entry:
            try:
                actual_entry = float(text_value.replace(",", "."))
                if actual_entry <= 0:
                    raise ValueError
            except ValueError:
                await message.answer("Введи корректную цену числом.")
                return

            signal_id = pending_entry.pop(chat_id)
            signal = get_signal(signal_id)

            if not signal:
                await message.answer("Сигнал больше не найден.")
                return

            trade_id, warning = create_personal_trade(
                chat_id,
                signal,
                actual_entry
            )

            if not trade_id:
                await message.answer(warning)
                return

            await message.answer(
                "✅ Сделка добавлена в личное сопровождение.\n\n"
                + (warning or "")
            )
            await send_trade_message(bot, chat_id, trade_id)
            return

        # User corrects actual entry price.
        if chat_id in pending_trade_entry:
            try:
                new_entry = float(text_value.replace(",", "."))
                if new_entry <= 0:
                    raise ValueError
            except ValueError:
                await message.answer("Введи корректную цену числом.")
                return

            trade_id = pending_trade_entry.pop(chat_id)
            trade = get_trade(trade_id)

            if not trade or trade["chat_id"] != chat_id:
                await message.answer("Сделка не найдена.")
                return

            levels = make_trade_levels(
                trade["side"],
                new_entry,
                safe_float(trade["initial_sl"])
            )

            if not levels:
                await message.answer("Не удалось пересчитать уровни.")
                return

            update_trade(
                trade_id,
                entry=new_entry,
                tp1=levels[0],
                tp2=levels[1],
                last_recommendation="Фактическая цена входа исправлена пользователем."
            )

            add_trade_event(
                trade_id,
                "ENTRY_CORRECTED",
                "Пользователь исправил фактическую цену входа.",
                new_entry
            )

            await send_trade_message(
                bot,
                chat_id,
                trade_id,
                "Фактический вход и цели пересчитаны."
            )
            return

        # User reports a manually changed Stop Loss.
        if chat_id in pending_trade_sl:
            try:
                new_sl = float(text_value.replace(",", "."))
                if new_sl <= 0:
                    raise ValueError
            except ValueError:
                await message.answer("Введи корректную цену Stop Loss.")
                return

            trade_id = pending_trade_sl.pop(chat_id)
            trade = get_trade(trade_id)

            if not trade or trade["chat_id"] != chat_id:
                await message.answer("Сделка не найдена.")
                return

            update_trade(
                trade_id,
                sl=new_sl,
                last_recommendation="Пользователь сообщил о ручном изменении Stop Loss."
            )

            add_trade_event(
                trade_id,
                "SL_UPDATED",
                "Пользователь сообщил о новом Stop Loss.",
                new_sl
            )

            await send_trade_message(
                bot,
                chat_id,
                trade_id,
                "Новый Stop Loss сохранён в сопровождении."
            )
            return

        # User enters a balance.
        if chat_id in pending_balance:
            try:
                amount = float(text_value.replace(",", "."))
                if amount <= 0:
                    raise ValueError
            except ValueError:
                await message.answer("Введи положительное число.")
                return

            market = pending_balance.pop(chat_id)
            save_balance(chat_id, market, amount)

            await message.answer(
                f"Баланс {market.upper()} сохранён: {amount:.2f} USDT."
            )
            return

        # Main menu.
        if text_value == "📈 Анализ рынка":
            await message.answer("Запускаю анализ рынка...")
            signal_ids = await scan_all()

            if not signal_ids:
                await message.answer("Новых подтверждённых сигналов пока нет.")
            else:
                await message.answer(
                    f"Найдено новых сигналов: {len(signal_ids)}"
                )
                for signal_id in signal_ids:
                    signal = get_signal(signal_id)
                    if signal:
                        await message.answer(
                            signal_message(signal),
                            reply_markup=signal_keyboard(signal_id),
                            parse_mode="Markdown"
                        )

        elif text_value == "📊 Мои сделки":
            await send_my_trades(message)

        elif text_value == "📚 История":
            await send_history(message)

        elif text_value == "📉 Статистика":
            await send_statistics(message)

        elif text_value == "⚙️ Настройки":
            await send_settings(message)

        elif text_value == "🩺 Состояние системы":
            await send_system_status(message)

        elif text_value == "▶️ Автоанализ":
            set_auto(chat_id, True)
            await message.answer("Автоанализ включён.")

        elif text_value == "⏹ Остановить автоанализ":
            set_auto(chat_id, False)
            await message.answer("Автоанализ выключен.")


# =========================
# APPLICATION STARTUP
# =========================

async def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не задан BOT_TOKEN. Проверь переменную BOT_TOKEN в настройках."
        )

    init_db()

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    await register_callbacks(dp, bot)
    await register_messages(dp, bot)

    tasks = [
        asyncio.create_task(background_scanner(bot)),
        asyncio.create_task(background_monitor(bot)),
        asyncio.create_task(monitor_personal_trades(bot))
    ]

    logging.info("Trading Bot v9.2 starting")

    try:
        await dp.start_polling(bot)
    finally:
        for task in tasks:
            task.cancel()

        await asyncio.gather(
            *tasks,
            return_exceptions=True
        )

        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Trading Bot stopped")


# END OF PART 4
