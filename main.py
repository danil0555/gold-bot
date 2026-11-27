# ============================================================
# TRADING BOT v10.0 — USDT-M FUTURES SCALPING
# Strict multi-timeframe signal filter; no personal trade monitoring
# REAL EXCHANGE ORDER PLACEMENT IS STRICTLY DISABLED.
# ============================================================

import os
import sys
import time
import math
import html
import shutil
import hashlib
import gzip
import zlib
from collections import deque
import uuid
import asyncio
import logging
import json
import sqlite3

from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from contextlib import contextmanager
from typing import Any

import aiohttp
import numpy as np
import pandas as pd

from dotenv import load_dotenv

from scripts.reliability import (
    RETRYABLE_HTTP_STATUS, contiguous_candle_tail, retry_after_seconds,
)
from scripts.position_sizing import calculate_linear_usdtm_position_size, parse_bingx_contract_specs, apply_bingx_contract_constraints

from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
)


# ============================================================
# 1. ENVIRONMENT
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN. Добавь токен в файл .env")

BOT_NAME = "Trading Bot"
BOT_VERSION = "10.2.2"

BASE = "https://open-api.bingx.com"

DB_PATH = Path(os.getenv("DB_PATH", "trading_bot_v9.db")).expanduser()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

BACKUP_DIR = Path(os.getenv("BACKUP_DIR", str(DB_PATH.parent / "backups")))
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger("TradingBot")


# ============================================================
# 2. TRADING PARAMETERS
# ============================================================

MARKETS = ("swap",)
TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h")

TF_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

MIN_RR = 1.0
MIN_SCALPER_RAW_SCORE = 7.0
MIN_QUALITY_SCORE = 85.0
MIN_NET_RR = 1.30
MIN_SCALPER_ADX = 18.0
MIN_VOLUME_RATIO = 0.85
MIN_ATR_PCT = 0.08
MAX_ATR_PCT = 1.50
MAX_EXTENSION_ATR = 2.20

TP1_R = 1.8
TP2_R = 3.0

RISK_PERCENT = 10.0
FUTURES_LEVERAGE = 5

MAX_SIGNALS_PER_CYCLE = 3

KLINE_LIMIT = 300
MIN_CANDLE_ROWS = 205

MARKET_SCAN_INTERVAL_SEC = 60  # пауза между завершёнными циклами
BINGX_SWAP_WS_URL = "wss://open-api-swap.bingx.com/swap-market"
WS_RECONNECT_MAX_SEC = 60
WS_CANDLE_BUFFER_LIMIT = 300

SIGNAL_COOLDOWN_MINUTES = 30
SIGNAL_TTL_MINUTES = 8
MAX_ENTRY_DRIFT_R = 0.5
MAX_SPREAD_PCT = 0.08

MAKER_FEE_RATE = float(os.getenv("MAKER_FEE_RATE", "0.0002"))
TAKER_FEE_RATE = float(os.getenv("TAKER_FEE_RATE", "0.0005"))
DEFAULT_SYNTHETIC_SPREAD_PCT = float(os.getenv("DEFAULT_SYNTHETIC_SPREAD_PCT", "0.04"))
DEFAULT_SLIPPAGE_PCT = float(os.getenv("DEFAULT_SLIPPAGE_PCT", "0.03"))

STRATEGY_B = "B"
DEFAULT_STRATEGY = STRATEGY_B

REAL_TRADING_ENABLED = False

# /diag_db доступен только этому ID (впиши свой при необходимости)
ALLOWED_DIAG_USER_IDS: set[int] = {6322560261}

SYMBOLS = [
    "BTC-USDT", "ETH-USDT", "BNB-USDT", "SOL-USDT", "XRP-USDT",
    "DOGE-USDT", "ADA-USDT", "AVAX-USDT", "LINK-USDT", "LTC-USDT",
    "TRX-USDT", "DOT-USDT", "BCH-USDT", "SUI-USDT",
]


# ============================================================
# 3. GLOBAL OBJECTS
# ============================================================

bot: Bot | None = None
dp = Dispatcher()
router = Router()

http_session: aiohttp.ClientSession | None = None

_api_rate_lock = asyncio.Lock()
_scan_lock = asyncio.Lock()
_last_api_request = 0.0
_api_cooldown_until = 0.0

candle_cache: dict[tuple[str, str, str], pd.DataFrame] = {}
# Отдельный буфер закрытых 1m свечей из публичного WebSocket BingX.
# Стратегия B остаётся на исходном REST-пути и не меняет свои фильтры.
ws_1m_frames: dict[str, pd.DataFrame] = {}
ws_current_minute: dict[str, dict[str, Any]] = {}
ws_seen_trade_ids: dict[str, tuple[deque, set[str]]] = {}
ws_status = {
    "connected": False, "last_message": None, "reconnects": 0,
    "trades_received": 0, "candles_closed": 0, "last_trade_symbol": None,
}
latest_prices: dict[tuple[str, str], float] = {}
latest_quotes: dict[tuple[str, str], dict[str, Any]] = {}
contract_specs_cache: dict[str, dict[str, Any]] = {}
contract_specs_updated_at = 0.0
CONTRACT_SPECS_MAX_AGE_SEC = 6 * 60 * 60
_unavailable_symbols: set[tuple[str, str]] = set()
_insufficient_history: dict[tuple[str, str], float] = {}
background_tasks: list[asyncio.Task] = []

system_status: dict[str, Any] = {
    "started": False,
    "last_scan": None,
    "last_error": None,
    "api_status": "Не проверено",
    "scan_count": 0,
    "signals_count": 0,
    "source_sha256": None,
    "backup_path": None,
}


# ============================================================
# 4. HELPERS
# ============================================================

def now_ms() -> int:
    return int(time.time() * 1000)

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def utc_text(dt: datetime | None = None) -> str:
    target = dt if dt is not None else utc_now()
    return target.strftime("%Y-%m-%d %H:%M:%S")

def parse_utc_text(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            dt = datetime.fromisoformat(value)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None

def format_moscow_from_utc(utc_str: str | None) -> str:
    dt_utc = parse_utc_text(utc_str)
    if dt_utc is None:
        return str(utc_str or "—")
    return dt_utc.astimezone(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M:%S")

def safe_float(value: Any, default: float = 0.0) -> float:
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

def market_name(market: str) -> str:
    return "USDT-M Futures" if str(market).lower() == "swap" else str(market).upper()

def side_name(side: str) -> str:
    return {
        "BUY": "LONG / BUY", "LONG": "LONG / BUY",
        "SELL": "SHORT / SELL", "SHORT": "SHORT / SELL",
    }.get(str(side).upper(), str(side))

def is_long_side(side: str) -> bool:
    return str(side).strip().lower() in ("buy", "long")


def compute_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def freeze_source_snapshot() -> str | None:
    try:
        src_path = Path(__file__).resolve()
        if not src_path.exists():
            return None
        sha = compute_file_sha256(src_path)
        snapshot_path = BACKUP_DIR / f"source_v9_3_{sha[:12]}.py.bak"
        if not snapshot_path.exists():
            shutil.copy2(src_path, snapshot_path)
            try:
                os.chmod(snapshot_path, 0o444)
            except OSError:
                pass
        system_status["source_sha256"] = sha
        log.info("Эталон исходника: %s (SHA-256=%s)", snapshot_path.name, sha)
        return sha
    except Exception:
        log.exception("Не удалось создать резервную копию исходника")
        return None


# ============================================================
# 5. DATABASE
# ============================================================

@contextmanager
def db():
    con = sqlite3.connect(str(DB_PATH), timeout=30)
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

def table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    rows = con.execute(f'PRAGMA table_info("{table}")').fetchall()
    return {str(row["name"]) for row in rows}

def ensure_column(con: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    if column not in table_columns(con, table):
        con.execute(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition}')
        log.info("Добавлена колонка %s.%s", table, column)

def backup_and_verify_db() -> Path | None:
    if not DB_PATH.exists():
        return None
    try:
        with db() as con:
            check = con.execute("PRAGMA integrity_check").fetchone()
            if not check or str(check[0]).lower() != "ok":
                log.warning("integrity_check до бэкапа не ok: %s", check)
    except Exception:
        log.exception("Не удалось проверить БД перед бэкапом")
        return None

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_file = BACKUP_DIR / f"{DB_PATH.stem}_pre_migration_{stamp}.db"

    src_con = sqlite3.connect(str(DB_PATH), timeout=30)
    dst_con = sqlite3.connect(str(backup_file), timeout=30)
    try:
        src_con.backup(dst_con)
    finally:
        dst_con.close()
        src_con.close()

    verify_con = sqlite3.connect(str(backup_file), timeout=30)
    try:
        check = verify_con.execute("PRAGMA integrity_check").fetchone()
        if not check or str(check[0]).lower() != "ok":
            raise RuntimeError(f"Резервная копия не прошла integrity_check: {check}")
    finally:
        verify_con.close()

    # Ротация бэкапов: оставляем последние 10
    try:
        files = sorted(BACKUP_DIR.glob(f"{DB_PATH.stem}_pre_migration_*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[10:]:
            old.unlink(missing_ok=True)
    except Exception:
        log.exception("Не удалось очистить старые бэкапы")

    system_status["backup_path"] = str(backup_file)
    log.info("Резервная копия БД: %s", backup_file)
    return backup_file

def init_db() -> None:
    if DB_PATH.exists():
        try:
            backup_path = backup_and_verify_db()
        except Exception as exc:
            log.exception("Ошибка резервного копирования — миграция БД отменена")
            raise RuntimeError(
                "Не удалось создать и проверить резервную копию БД; "
                "инициализация и миграция остановлены."
            ) from exc
        if backup_path is None:
            raise RuntimeError(
                "Резервная копия БД не создана или не прошла проверку; "
                "инициализация и миграция остановлены."
            )

    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            created_at TEXT,
            is_active INTEGER DEFAULT 1
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS user_risk_profiles (
            user_id INTEGER PRIMARY KEY,
            balance_usdt REAL NOT NULL CHECK(balance_usdt > 0),
            updated_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            risk_percent REAL DEFAULT 10.0,
            leverage INTEGER DEFAULT 5,
            notifications INTEGER DEFAULT 1,
            strategy_version TEXT DEFAULT 'B',
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT, updated_at TEXT,
            market TEXT, symbol TEXT, side TEXT,
            entry REAL, sl REAL, tp REAL, rr REAL,
            score INTEGER, reasons TEXT, setup_key TEXT,
            status TEXT DEFAULT 'ACTIVE', result TEXT,
            strategy_version TEXT DEFAULT 'B',
            live_price REAL, bid REAL, ask REAL,
            spread_pct REAL, price_updated_at TEXT, expires_at TEXT,
            entry_mode TEXT DEFAULT 'market',
            entry_zone_low REAL, entry_zone_high REAL,
            confirmation_rule TEXT, quality_score REAL,
            market_regime TEXT, estimated_fee_pct REAL,
            estimated_slippage_pct REAL, net_rr REAL,
            cancel_reason TEXT
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS signal_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER NOT NULL, event_type TEXT NOT NULL,
            old_status TEXT, new_status TEXT, price REAL,
            reason TEXT, created_at TEXT NOT NULL,
            FOREIGN KEY(signal_id) REFERENCES signals(id)
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS market_candles (
            market TEXT NOT NULL, symbol TEXT NOT NULL,
            interval TEXT NOT NULL, open_time INTEGER NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume REAL,
            PRIMARY KEY (market, symbol, interval, open_time)
        )""")

        con.execute("CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(market, symbol, side)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_signals_status_exp ON signals(status, expires_at)")

    log.info("База данных готова: %s", DB_PATH)

def ensure_part3_schema() -> None:
    """Переводим настройки пользователей на единственную стратегию Futures Scalping B.
    Старые таблицы личных сделок, если они есть, не удаляем и не используем.
    """
    with db() as conn:
        conn.execute("UPDATE user_settings SET strategy_version=?", (STRATEGY_B,))



# ============================================================
# 6. BINGX API
# ============================================================

class MarketError(Exception):
    pass

class RetryableMarketError(MarketError):
    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = retry_after


async def api_get(session: aiohttp.ClientSession, path: str, params: dict[str, Any]) -> Any:
    global _last_api_request, _api_cooldown_until
    url = BASE + path
    last_error: Exception | None = None

    for attempt in range(4):
        try:
            async with _api_rate_lock:
                now = time.monotonic()
                wait = max(1.2 - (now - _last_api_request), _api_cooldown_until - now, 0.0)
                if wait > 0:
                    await asyncio.sleep(wait)

                async with session.get(
                    url, params=params, timeout=aiohttp.ClientTimeout(total=25)
                ) as response:
                    _last_api_request = time.monotonic()
                    raw_text = await response.text()
                    if response.status != 200:
                        if response.status in RETRYABLE_HTTP_STATUS:
                            delay = retry_after_seconds(
                                response.headers.get("Retry-After"), attempt
                            )
                            _api_cooldown_until = max(
                                _api_cooldown_until, time.monotonic() + delay
                            )
                            raise RetryableMarketError(
                                f"HTTP {response.status}: {raw_text[:250]}", delay
                            )
                        raise MarketError(f"HTTP {response.status}: {raw_text[:250]}")

                    try:
                        payload = await response.json(content_type=None)
                    except Exception as exc:
                        raise MarketError("Некорректный JSON: " + raw_text[:250]) from exc

                    if isinstance(payload, dict):
                        code = payload.get("code")
                        if code not in (None, 0, "0"):
                            error_text = f"BingX {code}: {payload.get('msg', '')}"
                            if str(code) in ("100410", "100429"):
                                delay = min(30.0, 3.0 * (2 ** attempt))
                                _api_cooldown_until = max(
                                    _api_cooldown_until, time.monotonic() + delay
                                )
                                last_error = MarketError(error_text)
                                system_status["api_status"] = "Rate limit (cooldown)"
                            else:
                                raise MarketError(error_text)
                        else:
                            system_status["api_status"] = "OK"
                            return payload
                    else:
                        system_status["api_status"] = "OK"
                        return payload

            if last_error is not None:
                if attempt < 3:
                    await asyncio.sleep(min(2 ** attempt, 8))
                    continue
                raise last_error

        except asyncio.CancelledError:
            raise
        except RetryableMarketError as exc:
            last_error = MarketError(str(exc))
            system_status["api_status"] = "Rate limit / временная ошибка HTTP"
            system_status["last_error"] = str(exc)
            if attempt < 3:
                await asyncio.sleep(exc.retry_after)
                continue
            raise last_error from exc
        except MarketError as exc:
            system_status["api_status"] = f"Ошибка ({exc})"
            system_status["last_error"] = str(exc)
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = MarketError(str(exc))
            system_status["api_status"] = "Ошибка сети"
            system_status["last_error"] = str(exc)
            if attempt < 3:
                await asyncio.sleep(min(2 ** attempt, 8))
                continue
            raise last_error from exc

    raise last_error or MarketError("Неизвестная ошибка BingX API")
def extract_data(payload: Any) -> Any:
    if isinstance(payload, dict):
        return payload.get("data", payload.get("result", payload))
    return payload


# ============================================================
# 7. CANDLES
# ============================================================

def normalize_candles(payload: Any, interval: str, min_rows: int = MIN_CANDLE_ROWS) -> pd.DataFrame:
    if interval not in TF_MS:
        raise MarketError(f"Неизвестный таймфрейм: {interval}")
    raw = extract_data(payload)
    if isinstance(raw, dict):
        raw = raw.get("data", raw.get("list", raw.get("rows", [])))
    if not isinstance(raw, list) or not raw:
        raise MarketError("BingX вернула пустой список свечей")

    rows = []
    for item in raw:
        try:
            if isinstance(item, (list, tuple)):
                if len(item) < 6:
                    continue
                ot = int(float(item[0]))
                o, h, l, c, v = map(float, item[1:6])
                ct = int(float(item[6])) if len(item) > 6 and item[6] not in (None, "") else ot + TF_MS[interval] - 1
            elif isinstance(item, dict):
                ot = int(float(item.get("openTime", item.get("time", item.get("timestamp", 0)))))
                o, h, l, c, v = (float(item[k]) for k in ("open", "high", "low", "close", "volume"))
                ct = int(float(item.get("closeTime", item.get("close_time", ot + TF_MS[interval] - 1))))
            else:
                continue
            if ot < 10_000_000_000:
                ot *= 1000
            if ct < 10_000_000_000:
                ct *= 1000
            if ot <= 0:
                continue
            if not all(np.isfinite(val) and val > 0 for val in (o, h, l, c)):
                continue
            if not np.isfinite(v) or v < 0:
                continue
            if h < max(o, c) or l > min(o, c):
                continue
            rows.append((ot, o, h, l, c, v, ct))
        except (ValueError, TypeError, KeyError, IndexError, OverflowError):
            continue

    if not rows:
        raise MarketError("Не удалось распознать формат свечей")

    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume", "close_time"])
    df = df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)
    current_ms = now_ms()
    df = df[df["close_time"] < current_ms - 2000].reset_index(drop=True)

    try:
        df = contiguous_candle_tail(df, TF_MS[interval], min_rows)
    except ValueError as exc:
        raise MarketError(
            f"Недостаточно непрерывных закрытых свечей для {interval}: {exc}"
        ) from exc
    return df

def _is_cached_frame_fresh(market: str, symbol: str, interval: str, min_rows: int) -> pd.DataFrame | None:
    cached = candle_cache.get((market, symbol, interval))
    if cached is None or cached.empty or len(cached) < min_rows:
        return None
    last_close = int(cached.iloc[-1]["close_time"])
    tf_ms = TF_MS.get(interval, 60_000)
    if now_ms() < last_close + tf_ms:
        return cached.copy()
    return None

async def get_klines(market: str, symbol: str, interval: str,
                     limit: int = KLINE_LIMIT, min_rows: int = MIN_CANDLE_ROWS,
                     session: aiohttp.ClientSession | None = None,
                     use_cache: bool = True) -> pd.DataFrame:
    if use_cache:
        fresh = _is_cached_frame_fresh(market, symbol, interval, min_rows)
        if fresh is not None:
            return fresh

    active_session = session or http_session
    if active_session is None or active_session.closed:
        raise MarketError("HTTP-сессия BingX ещё не запущена")

    if market != "swap":
        raise MarketError("Эта версия работает только с USDT-M Futures")
    path = "/openApi/swap/v3/quote/klines"

    payload = await api_get(active_session, path,
                            {"symbol": symbol, "interval": interval, "limit": limit})
    df = normalize_candles(payload, interval, min_rows=min_rows)
    candle_cache[(market, symbol, interval)] = df.copy()
    if not df.empty:
        latest_prices[(market, symbol)] = float(df.iloc[-1]["close"])
    return df

def save_closed_candles(market: str, symbol: str, interval: str, df: pd.DataFrame) -> None:
    if df.empty:
        return
    with db() as con:
        for candle in df.itertuples():
            con.execute("""INSERT INTO market_candles(
                market, symbol, interval, open_time, open, high, low, close, volume)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market, symbol, interval, open_time) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, volume=excluded.volume""",
                (market, symbol, interval, int(candle.open_time),
                 float(candle.open), float(candle.high), float(candle.low),
                 float(candle.close), float(candle.volume)))
        con.execute("""DELETE FROM market_candles
            WHERE market=? AND symbol=? AND interval=?
              AND open_time < (SELECT MIN(open_time) FROM (
                  SELECT open_time FROM market_candles
                  WHERE market=? AND symbol=? AND interval=?
                  ORDER BY open_time DESC LIMIT 1500))""",
            (market, symbol, interval, market, symbol, interval))




# ============================================================
# 8A. BINGX PUBLIC 1M KLINE WEBSOCKET (READ-ONLY)
# ============================================================

def _decode_bingx_ws_message(raw: bytes | str) -> str:
    if isinstance(raw, str):
        return raw
    try:
        return gzip.decompress(raw).decode("utf-8")
    except (OSError, EOFError, zlib.error):
        # Некоторые промежуточные прокси могут передать raw-deflate.
        try:
            return zlib.decompress(raw, 16 + zlib.MAX_WBITS).decode("utf-8")
        except zlib.error:
            return raw.decode("utf-8", errors="replace")



def _append_closed_ws_candle(symbol: str, candle: dict[str, Any]) -> None:
    """Сохраняет минуту, собранную из реальных сделок, в ограниченный буфер."""
    open_time = int(candle["open_time"])
    row = {
        "open_time": open_time, "open": safe_float(candle["open"]),
        "high": safe_float(candle["high"]), "low": safe_float(candle["low"]),
        "close": safe_float(candle["close"]), "volume": safe_float(candle["volume"]),
        "close_time": open_time + 59_999,
    }
    if min(row["open"], row["high"], row["low"], row["close"]) <= 0:
        return
    base = candle_cache.get(("swap", symbol, "1m"))
    frame = ws_1m_frames.get(symbol)
    pieces = []
    if base is not None and not base.empty:
        pieces.append(base)
    if frame is not None and not frame.empty:
        pieces.append(frame)
    pieces.append(pd.DataFrame([row]))
    merged = pd.concat(pieces, ignore_index=True)
    merged = (merged.drop_duplicates("open_time", keep="last")
                    .sort_values("open_time")
                    .tail(WS_CANDLE_BUFFER_LIMIT)
                    .reset_index(drop=True))
    ws_1m_frames[symbol] = merged
    ws_status["candles_closed"] = int(ws_status.get("candles_closed", 0)) + 1
    ws_status["last_message"] = utc_text()


def _update_ws_trade(symbol: str, trade: dict[str, Any]) -> None:
    """Агрегирует BingX trade prints в настоящие 1m OHLCV; без синтетических свечей."""
    try:
        trade_id = str(trade.get("t") or "")
        ts = int(trade.get("T") or 0)
        price = safe_float(trade.get("p"))
        qty = safe_float(trade.get("q"))
        if not trade_id or ts <= 0 or price <= 0 or qty <= 0:
            return

        queue, seen = ws_seen_trade_ids.setdefault(symbol, (deque(), set()))
        if trade_id in seen:
            return
        seen.add(trade_id)
        ws_status["trades_received"] = int(ws_status.get("trades_received", 0)) + 1
        ws_status["last_trade_symbol"] = symbol
        queue.append(trade_id)
        while len(queue) > 5000:
            old_id = queue.popleft()
            seen.discard(old_id)

        minute = (ts // 60_000) * 60_000
        current = ws_current_minute.get(symbol)
        if current is None:
            ws_current_minute[symbol] = {
                "open_time": minute, "open": price, "high": price,
                "low": price, "close": price, "volume": qty,
            }
            return

        current_minute = int(current["open_time"])
        if minute < current_minute:
            # Поздняя/переупорядоченная сделка: не переписываем закрытую свечу.
            return
        if minute == current_minute:
            current["high"] = max(safe_float(current["high"]), price)
            current["low"] = min(safe_float(current["low"]), price)
            current["close"] = price
            current["volume"] = safe_float(current["volume"]) + qty
            return

        # Переход в следующую минуту закрывает предыдущую реальными сделками.
        if now_ms() >= current_minute + 60_000:
            _append_closed_ws_candle(symbol, current)
        ws_current_minute[symbol] = {
            "open_time": minute, "open": price, "high": price,
            "low": price, "close": price, "volume": qty,
        }
    except Exception:
        log.exception("Ошибка агрегации WebSocket trade для %s", symbol)


async def bingx_kline_websocket_loop() -> None:
    """Публичный read-only trade stream USDT-M Futures; 1m свечи строятся из сделок."""
    delay = 2
    while True:
        try:
            if http_session is None or http_session.closed:
                await asyncio.sleep(2)
                continue
            async with http_session.ws_connect(
                BINGX_SWAP_WS_URL, heartbeat=None, autoping=False,
                receive_timeout=75, max_msg_size=4 * 1024 * 1024
            ) as ws:
                ws_status["connected"] = True
                delay = 2
                log.info("BingX WebSocket подключён; trade-подписок: %s", len(SYMBOLS))
                for symbol in SYMBOLS:
                    await ws.send_json({
                        "id": str(uuid.uuid4()),
                        "reqType": "sub",
                        "dataType": f"{symbol}@trade",
                    })
                async for message in ws:
                    if message.type == aiohttp.WSMsgType.BINARY:
                        raw = message.data
                    elif message.type == aiohttp.WSMsgType.TEXT:
                        raw = message.data
                    elif message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                        raise ConnectionError("BingX WebSocket закрыт")
                    elif message.type == aiohttp.WSMsgType.ERROR:
                        raise ConnectionError(f"WebSocket error: {ws.exception()}")
                    else:
                        continue
                    decoded = _decode_bingx_ws_message(raw).strip()
                    if decoded == "Ping" or decoded.lower() == "ping":
                        await ws.send_str("Pong")
                        continue
                    try:
                        payload = json.loads(decoded)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    data = payload.get("data")
                    if not isinstance(data, list):
                        continue
                    data_type = str(payload.get("dataType", ""))
                    channel_symbol = data_type.split("@", 1)[0] if "@" in data_type else ""
                    for trade in data:
                        if isinstance(trade, dict):
                            sym = str(trade.get("s") or channel_symbol)
                            if sym in SYMBOLS:
                                _update_ws_trade(sym, trade)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            ws_status["connected"] = False
            ws_status["reconnects"] = int(ws_status.get("reconnects", 0)) + 1
            log.warning("BingX WebSocket отключён: %s; повтор через %ss", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, WS_RECONNECT_MAX_SEC)
        finally:
            ws_status["connected"] = False



# ============================================================
# 8. INDICATORS + STRATEGIES
# ============================================================

def indicators(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    close, high, low = x["close"], x["high"], x["low"]
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
    tr = pd.concat([high - low, (high - close.shift()).abs(), (low - close.shift()).abs()], axis=1).max(axis=1)
    x["atr"] = tr.ewm(alpha=1/14, adjust=False).mean()
    up = high.diff(); down = -low.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    atr_safe = x["atr"].replace(0, np.nan)
    plus_di = 100 * plus_dm.ewm(alpha=1/14, adjust=False).mean() / atr_safe
    minus_di = 100 * minus_dm.ewm(alpha=1/14, adjust=False).mean() / atr_safe
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    x["adx"] = dx.ewm(alpha=1/14, adjust=False).mean().fillna(0)
    x["plus_di"] = plus_di.fillna(0)
    x["minus_di"] = minus_di.fillna(0)
    x["macd"] = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    x["macd_signal"] = x["macd"].ewm(span=9, adjust=False).mean()
    return x

def enrich_scalper_series(df: pd.DataFrame) -> pd.DataFrame:
    x = indicators(df)
    vol = x["volume"].replace(0, np.nan)
    x["vol_ma20"] = vol.rolling(20, min_periods=5).mean().fillna(0)
    x["vol_ratio"] = (x["volume"] / x["vol_ma20"].replace(0, np.nan)).fillna(1.0)
    typical = (x["high"] + x["low"] + x["close"]) / 3.0
    pv = typical * x["volume"]
    cum_pv = pv.rolling(48, min_periods=10).sum()
    cum_v = x["volume"].rolling(48, min_periods=10).sum().replace(0, np.nan)
    x["vwap"] = (cum_pv / cum_v).fillna(x["close"])
    x["swing_high_prev"] = x["high"].rolling(5, min_periods=3).max().shift(1)
    x["swing_low_prev"] = x["low"].rolling(5, min_periods=3).min().shift(1)
    x["range_high_20"] = x["high"].rolling(20, min_periods=10).max().shift(1)
    x["range_low_20"] = x["low"].rolling(20, min_periods=10).min().shift(1)
    return x


def _detect_market_regime(m15_row, m15_prev, atr, price):
    adx = safe_float(m15_row.get("adx"))
    atr_pct = (atr / price * 100.0) if price > 0 else 0.0
    vol_ratio = safe_float(m15_row.get("vol_ratio"), 1.0)
    prev_vol_ratio = safe_float(m15_prev.get("vol_ratio"), 1.0)
    if vol_ratio >= 1.6 and abs(safe_float(m15_row["close"]) - safe_float(m15_row["open"])) > atr * 1.1:
        return "impulse"
    if prev_vol_ratio >= 1.5 and vol_ratio < 0.9 and adx >= 18:
        return "exhaustion"
    if adx >= 20 and atr_pct >= 0.15:
        return "trend"
    return "range"

def build_signal_b(market, symbol, frames, quote=None, diagnostics=None):
    """Строгий многотаймфреймовый сетап для USDT-M Futures scalping.

    Это rule-based фильтр, а не статистическая вероятность выигрыша.
    Сигнал допускается только при согласовании 1H/15m/5m и наличии
    структурного триггера (BOS/CHoCH или sweep).
    """
    def reject(reason):
        if diagnostics is not None:
            diagnostics["reason"] = reason
            diagnostics["stage"] = "strategy_filter"
        return None

    if str(market).lower() != "swap":
        return reject("неверный тип рынка")
    required = ("1m", "5m", "15m", "1h", "4h")
    if any(tf not in frames or frames[tf] is None or len(frames[tf]) < MIN_CANDLE_ROWS for tf in required):
        return reject("недостаточно свечей на одном или нескольких таймфреймах")

    m1 = enrich_scalper_series(frames["1m"])
    m5 = enrich_scalper_series(frames["5m"])
    m15 = enrich_scalper_series(frames["15m"])
    h1 = enrich_scalper_series(frames["1h"])
    h4 = enrich_scalper_series(frames["4h"])
    m1_now = m1.iloc[-1]
    m5_now, m15_now, m15_prev = m5.iloc[-1], m15.iloc[-1], m15.iloc[-2]
    h1_now, h4_now = h1.iloc[-1], h4.iloc[-1]

    price = safe_float(m15_now["close"])
    atr = safe_float(m15_now["atr"])
    if price <= 0 or atr <= 0:
        return reject("ATR отсутствует или некорректен")

    atr_pct = atr / price * 100.0
    if atr_pct < MIN_ATR_PCT or atr_pct > MAX_ATR_PCT:
        return reject("ATR% вне допустимого диапазона")
    if safe_float(m15_now["adx"]) < MIN_SCALPER_ADX:
        return reject("ADX ниже минимального порога")

    regime = _detect_market_regime(m15_now, m15_prev, atr, price)
    extension_atr = abs(price - safe_float(m15_now["ema20"])) / atr
    if extension_atr > MAX_EXTENSION_ATR or regime == "exhaustion":
        return reject("цена слишком далеко от EMA20 или режим exhaustion")

    # Контекст: 1H обязан задавать направление, 4H не должен ему противоречить.
    h1_bull = h1_now["close"] > h1_now["ema200"] and h1_now["ema20"] > h1_now["ema50"]
    h1_bear = h1_now["close"] < h1_now["ema200"] and h1_now["ema20"] < h1_now["ema50"]
    h4_bull = h4_now["close"] > h4_now["ema200"] and h4_now["ema20"] > h4_now["ema50"]
    h4_bear = h4_now["close"] < h4_now["ema200"] and h4_now["ema20"] < h4_now["ema50"]

    m15_bull = m15_now["close"] > m15_now["ema20"] and m15_now["ema20"] > m15_now["ema50"]
    m15_bear = m15_now["close"] < m15_now["ema20"] and m15_now["ema20"] < m15_now["ema50"]
    m5_bull = (m5_now["close"] > m5_now["ema20"] > m5_now["ema50"]
               and m5_now["macd"] > m5_now["macd_signal"]
               and 50 <= safe_float(m5_now["rsi"]) <= 72)
    m5_bear = (m5_now["close"] < m5_now["ema20"] < m5_now["ema50"]
               and m5_now["macd"] < m5_now["macd_signal"]
               and 28 <= safe_float(m5_now["rsi"]) <= 50)
    m1_bull = m1_now["close"] > m1_now["ema20"] and m1_now["macd"] > m1_now["macd_signal"]
    m1_bear = m1_now["close"] < m1_now["ema20"] and m1_now["macd"] < m1_now["macd_signal"]

    buy_checks = {
        "1H_bull": bool(h1_bull),
        "4H_not_bear": bool(not h4_bear),
        "15m_bull": bool(m15_bull),
        "5m_bull": bool(m5_bull),
        "1m_bull": bool(m1_bull),
    }
    sell_checks = {
        "1H_bear": bool(h1_bear),
        "4H_not_bull": bool(not h4_bull),
        "15m_bear": bool(m15_bear),
        "5m_bear": bool(m5_bear),
        "1m_bear": bool(m1_bear),
    }

    if all(buy_checks.values()):
        side = "BUY"
    elif all(sell_checks.values()):
        side = "SELL"
    else:
        # Диагностика показывает каждое условие, не меняя торговую логику.
        buy_state = ",".join(f"{name}={'OK' if passed else 'NO'}" for name, passed in buy_checks.items())
        sell_state = ",".join(f"{name}={'OK' if passed else 'NO'}" for name, passed in sell_checks.items())
        details = f"BUY[{buy_state}] SELL[{sell_state}]"
        if diagnostics is not None:
            diagnostics["trend_checks"] = details
        return reject(f"нет согласования тренда/импульса: {details}")

    score = 0.0
    reasons = []
    score += 2.0; reasons.append("1H: основной тренд совпадает с направлением")
    if (side == "BUY" and h4_bull) or (side == "SELL" and h4_bear):
        score += 1.0; reasons.append("4H: старший контекст подтверждает направление")
    else:
        reasons.append("4H: нет противоположного тренда")
    score += 2.0; reasons.append("15м: цена и EMA20/50 подтверждают тренд")
    score += 2.0; reasons.append("5м: импульс подтверждён EMA, MACD и RSI")
    score += 1.0; reasons.append("1м: краткосрочный импульс совпадает с направлением")

    # Структурный триггер: BOS/CHoCH либо sweep с возвратом за уровень.
    prev_sh = safe_float(m15_prev.get("swing_high_prev"))
    prev_sl = safe_float(m15_prev.get("swing_low_prev"))
    close15 = safe_float(m15_now["close"])
    high15 = safe_float(m15_now["high"])
    low15 = safe_float(m15_now["low"])
    range_high = safe_float(m15_now.get("range_high_20"))
    range_low = safe_float(m15_now.get("range_low_20"))
    structural = False
    if side == "BUY":
        if prev_sh > 0 and close15 > prev_sh:
            score += 1.5; structural = True; reasons.append("15м: пробой структуры вверх (BOS)")
        elif range_low > 0 and low15 < range_low and close15 > range_low:
            score += 1.5; structural = True; reasons.append("15м: sweep вниз и возврат над уровнем")
    else:
        if prev_sl > 0 and close15 < prev_sl:
            score += 1.5; structural = True; reasons.append("15м: пробой структуры вниз (BOS)")
        elif range_high > 0 and high15 > range_high and close15 < range_high:
            score += 1.5; structural = True; reasons.append("15м: sweep вверх и возврат под уровень")
    if not structural:
        return reject("нет подтверждения структуры BOS/sweep")

    vwap = safe_float(m15_now.get("vwap"), price)
    if (side == "BUY" and close15 > vwap) or (side == "SELL" and close15 < vwap):
        score += 1.0; reasons.append("15м: цена на правильной стороне VWAP")
    else:
        return reject("цена не на нужной стороне VWAP")

    vol_ratio = safe_float(m15_now.get("vol_ratio"), 1.0)
    if vol_ratio < MIN_VOLUME_RATIO:
        return reject("объём ниже минимального порога")
    if vol_ratio >= 1.2:
        score += 0.5; reasons.append(f"Объём выше среднего: x{vol_ratio:.2f}")

    score_int = int(round(score))
    if score < MIN_SCALPER_RAW_SCORE:
        return reject("сырой балл стратегии ниже минимального")

    sl_dist = max(1.35 * atr, price * 0.0015)
    if sl_dist > max(3.0 * atr, price * 0.012):
        return reject("недопустимая дистанция Stop Loss")
    tp1_dist = sl_dist * TP1_R
    tp2_dist = sl_dist * TP2_R
    if side == "BUY":
        sl, tp1, tp2 = price - sl_dist, price + tp1_dist, price + tp2_dist
    else:
        sl, tp1, tp2 = price + sl_dist, price - tp1_dist, price - tp2_dist
    if min(sl, tp1, tp2) <= 0:
        return reject("расчётные уровни SL/TP некорректны")

    gross_rr = abs(tp2 - price) / abs(price - sl)
    spread_pct = safe_float((quote or {}).get("spread_pct"), DEFAULT_SYNTHETIC_SPREAD_PCT)
    fee_pct = (TAKER_FEE_RATE * 2.0) * 100.0
    slip_pct = DEFAULT_SLIPPAGE_PCT * 2.0
    cost_dist = price * ((spread_pct + fee_pct + slip_pct) / 100.0)
    effective_risk = max(abs(price - sl) + cost_dist * 0.5, 1e-9)
    effective_reward = max(abs(tp2 - price) - cost_dist, 0.0)
    net_rr = effective_reward / effective_risk
    if gross_rr < MIN_RR or net_rr < MIN_NET_RR:
        return reject("Gross RR или предварительный Net RR ниже порога")

    quality_score = min(100.0, 60.0 + score * 4.0
                        + (5.0 if net_rr >= 2.0 else 0.0)
                        + (3.0 if vol_ratio >= 1.5 else 0.0)
                        - spread_pct * 20.0)
    quality_score = round(quality_score, 1)
    if quality_score < MIN_QUALITY_SCORE:
        return reject("качество сетапа ниже минимального порога")

    entry_mode = "level" if extension_atr > 1.25 else "market"
    if side == "BUY":
        zone_low, zone_high = max(sl * 1.001, min(price, vwap)), price
        confirmation = "Вход только после удержания зоны покупателями на 5м."
    else:
        zone_low, zone_high = price, min(sl * 0.999, max(price, vwap))
        confirmation = "Вход только после подтверждения продавцов на 5м."

    return {
        "strategy_version": STRATEGY_B, "market": "swap", "symbol": symbol, "side": side,
        "entry": price, "sl": sl, "tp1": tp1, "tp2": tp2, "tp": tp2,
        "rr": gross_rr, "net_rr": net_rr, "score": score_int,
        "quality_score": quality_score, "market_regime": regime,
        "entry_mode": entry_mode,
        "entry_zone_low": min(zone_low, zone_high),
        "entry_zone_high": max(zone_low, zone_high),
        "confirmation_rule": confirmation if entry_mode == "level" else "Вход только пока 5м импульс сохраняется.",
        "spread_pct": spread_pct, "estimated_fee_pct": fee_pct,
        "estimated_slippage_pct": slip_pct, "reasons": reasons, "atr": atr,
        "setup_key": f"B:swap:{symbol}:{side}:{int(m15_now['open_time'])}",
    }




# ============================================================
# 8B. INDEPENDENT ADD-ON STRATEGIES (DO NOT MODIFY STRATEGY B)
# ============================================================

STRATEGY_C = "C_BREAKOUT_RETEST"
STRATEGY_D = "D_IMPULSE"
STRATEGY_E = "E_OVERHEAT_REVERSAL"
ADDON_STRATEGIES = (STRATEGY_C, STRATEGY_D, STRATEGY_E)

# Historical 7-day validation was negative for C/D/E. Keep them dormant by
# default until a longer out-of-sample validation supports enabling them.
ENABLE_ADDON_STRATEGIES = os.getenv("ENABLE_ADDON_STRATEGIES", "false").strip().lower() in {
    "1", "true", "yes", "on"
}


def _addon_reject(diagnostics: dict[str, Any] | None, reason: str):
    if diagnostics is not None:
        diagnostics["reason"] = reason
    return None


def _make_addon_signal(strategy: str, symbol: str, side: str, entry: float,
                       atr: float, score: int, regime: str, reasons: list[str],
                       setup_stamp: int,
                       diagnostics: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if entry <= 0 or atr <= 0:
        return _addon_reject(diagnostics, "invalid_price_or_atr")
    risk_dist = max(1.25 * atr, entry * 0.0015)
    if side == "BUY":
        sl, tp1, tp2 = entry - risk_dist, entry + risk_dist * TP1_R, entry + risk_dist * TP2_R
    else:
        sl, tp1, tp2 = entry + risk_dist, entry - risk_dist * TP1_R, entry - risk_dist * TP2_R
    if min(sl, tp1, tp2) <= 0:
        return _addon_reject(diagnostics, "invalid_sl_tp")
    gross_rr = abs(tp2 - entry) / abs(entry - sl)
    fee_pct = TAKER_FEE_RATE * 200.0
    slip_pct = DEFAULT_SLIPPAGE_PCT * 2.0
    estimated_spread = DEFAULT_SYNTHETIC_SPREAD_PCT
    cost = entry * ((estimated_spread + fee_pct + slip_pct) / 100.0)
    net_rr = max(abs(tp2 - entry) - cost, 0.0) / max(abs(entry - sl) + cost * 0.5, 1e-9)
    quality = round(min(100.0, 62 + score * 5 - estimated_spread * 10), 1)
    if gross_rr < MIN_RR:
        return _addon_reject(diagnostics, f"gross_rr_below_min:{gross_rr:.2f}")
    if net_rr < MIN_NET_RR:
        return _addon_reject(diagnostics, f"net_rr_below_min:{net_rr:.2f}")
    if quality < MIN_QUALITY_SCORE:
        return _addon_reject(diagnostics, f"quality_below_min:{quality:.1f}")
    if diagnostics is not None:
        diagnostics["reason"] = "candidate_created"
    return {
        "strategy_version": strategy, "market": "swap", "symbol": symbol, "side": side,
        "entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2, "tp": tp2,
        "rr": gross_rr, "net_rr": net_rr, "score": score,
        "quality_score": quality, "market_regime": regime,
        "entry_mode": "market", "entry_zone_low": None, "entry_zone_high": None,
        "confirmation_rule": "Сетап сформирован независимым модулем; проверь котировку перед входом.",
        "spread_pct": estimated_spread, "estimated_fee_pct": fee_pct,
        "estimated_slippage_pct": slip_pct, "reasons": reasons, "atr": atr,
        "setup_key": f"{strategy}:swap:{symbol}:{side}:{setup_stamp}",
    }


def build_signal_c_breakout_retest(symbol: str, frames: dict[str, pd.DataFrame],
                                   diagnostics: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Пробой диапазона 20 свечей и ретест уровня следующей свечой."""
    if any(tf not in frames or frames[tf] is None or len(frames[tf]) < 60 for tf in ("5m", "15m")):
        return _addon_reject(diagnostics, "insufficient_5m_or_15m_candles")
    m5 = enrich_scalper_series(frames["5m"])
    m15 = enrich_scalper_series(frames["15m"])
    cur, prev = m5.iloc[-1], m5.iloc[-2]
    ctx = m15.iloc[-1]
    price, atr = safe_float(cur["close"]), safe_float(cur["atr"])
    # У prev.range_*_20 уровень рассчитан по 20 свечам ДО prev.
    # prev подтверждает пробой, cur — удержание ретеста.
    hi, lo = safe_float(prev["range_high_20"]), safe_float(prev["range_low_20"])
    if price <= 0 or atr <= 0:
        return _addon_reject(diagnostics, "invalid_5m_price_or_atr")
    vol = safe_float(cur["volume"]) / max(safe_float(prev["vol_ma20"]), 1e-12)
    adx = safe_float(ctx["adx"])
    side, reasons = None, []
    if hi > 0 and safe_float(prev["close"]) > hi and safe_float(cur["low"]) <= hi * 1.002 and price > hi and vol >= 1.05 and adx >= 15:
        side = "BUY"
        reasons = ["5м: предыдущая свеча пробила максимум 20 свечей", "5м: текущая свеча протестировала и удержала уровень", f"Объём относительно предыдущей средней x{vol:.2f}"]
    elif lo > 0 and safe_float(prev["close"]) < lo and safe_float(cur["high"]) >= lo * 0.998 and price < lo and vol >= 1.05 and adx >= 15:
        side = "SELL"
        reasons = ["5м: предыдущая свеча пробила минимум 20 свечей", "5м: текущая свеча протестировала и удержала уровень", f"Объём относительно предыдущей средней x{vol:.2f}"]
    else:
        return _addon_reject(diagnostics, "no_confirmed_breakout_then_retest")
    if side == "BUY" and safe_float(ctx["close"]) < safe_float(ctx["ema50"]):
        return _addon_reject(diagnostics, "15m_context_below_ema50")
    if side == "SELL" and safe_float(ctx["close"]) > safe_float(ctx["ema50"]):
        return _addon_reject(diagnostics, "15m_context_above_ema50")
    return _make_addon_signal(STRATEGY_C, symbol, side, price, atr, 6, "breakout",
                              reasons, int(cur["open_time"]), diagnostics)


def build_signal_d_impulse(symbol: str, frames: dict[str, pd.DataFrame],
                           diagnostics: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Импульс 1m; ATR и средний объём берутся до текущей свечи."""
    if any(tf not in frames or frames[tf] is None or len(frames[tf]) < 60 for tf in ("1m", "5m")):
        return _addon_reject(diagnostics, "insufficient_1m_or_5m_candles")
    m1 = enrich_scalper_series(frames["1m"])
    m5 = enrich_scalper_series(frames["5m"])
    cur, prev = m1.iloc[-1], m1.iloc[-2]
    ctx = m5.iloc[-1]
    price, atr_ref = safe_float(cur["close"]), safe_float(prev["atr"])
    baseline_volume = safe_float(prev["vol_ma20"])
    body = abs(safe_float(cur["close"]) - safe_float(cur["open"]))
    vol = safe_float(cur["volume"]) / max(baseline_volume, 1e-12)
    if price <= 0 or atr_ref <= 0:
        return _addon_reject(diagnostics, "invalid_1m_price_or_reference_atr")
    if body < atr_ref:
        return _addon_reject(diagnostics, f"impulse_body_too_small:{body / atr_ref:.2f}_atr")
    if vol < 1.5:
        return _addon_reject(diagnostics, f"volume_below_1_5x:{vol:.2f}")
    if safe_float(ctx["adx"]) < 15:
        return _addon_reject(diagnostics, "5m_adx_below_15")
    if price > safe_float(cur["open"]) and safe_float(ctx["close"]) > safe_float(ctx["ema20"]) > safe_float(ctx["ema50"]):
        side = "BUY"
    elif price < safe_float(cur["open"]) and safe_float(ctx["close"]) < safe_float(ctx["ema20"]) < safe_float(ctx["ema50"]):
        side = "SELL"
    else:
        return _addon_reject(diagnostics, "1m_impulse_not_aligned_with_5m_ema")
    ctx_atr = safe_float(ctx["atr"])
    if ctx_atr <= 0 or abs(price - safe_float(ctx["ema20"])) > ctx_atr * 2.0:
        return _addon_reject(diagnostics, "price_too_extended_from_5m_ema20")
    reasons = [f"1м: импульсное тело {body / atr_ref:.2f} ATR", f"1м: объём x{vol:.2f} от предыдущей средней", "5м: EMA20/EMA50 подтверждают направление"]
    return _make_addon_signal(STRATEGY_D, symbol, side, price, atr_ref, 6, "impulse",
                              reasons, int(cur["open_time"]), diagnostics)


def build_signal_e_overheat_reversal(symbol: str, frames: dict[str, pd.DataFrame],
                                     diagnostics: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Разворот после RSI-экстремума в последних пяти свечах 1m."""
    if any(tf not in frames or frames[tf] is None or len(frames[tf]) < 60 for tf in ("1m", "5m")):
        return _addon_reject(diagnostics, "insufficient_1m_or_5m_candles")
    m1 = enrich_scalper_series(frames["1m"])
    m5 = enrich_scalper_series(frames["5m"])
    cur, prev = m1.iloc[-1], m1.iloc[-2]
    recent, ctx = m1.iloc[-6:-1], m5.iloc[-1]
    price, atr = safe_float(cur["close"]), safe_float(cur["atr"])
    rsi_now = safe_float(cur["rsi"])
    recent_high, recent_low = safe_float(recent["rsi"].max()), safe_float(recent["rsi"].min())
    vol = safe_float(cur["volume"]) / max(safe_float(prev["vol_ma20"]), 1e-12)
    if price <= 0 or atr <= 0:
        return _addon_reject(diagnostics, "invalid_1m_price_or_atr")
    if vol < 1.1:
        return _addon_reject(diagnostics, f"volume_below_1_1x:{vol:.2f}")
    if recent_high >= 80 and rsi_now < 75 and price < safe_float(cur["open"]) and price < safe_float(prev["close"]):
        side, extreme = "SELL", recent_high
    elif recent_low <= 20 and rsi_now > 25 and price > safe_float(cur["open"]) and price > safe_float(prev["close"]):
        side, extreme = "BUY", recent_low
    else:
        return _addon_reject(diagnostics, f"no_rsi_extreme_reversal:high={recent_high:.1f},low={recent_low:.1f},now={rsi_now:.1f}")
    ctx_atr = safe_float(ctx["atr"])
    if ctx_atr <= 0 or abs(price - safe_float(ctx["ema20"])) < ctx_atr * 0.4:
        return _addon_reject(diagnostics, "reversal_too_close_to_5m_ema20")
    reasons = [f"1м: RSI вернулся из экстремума ({extreme:.1f} → {rsi_now:.1f})", f"Объём x{vol:.2f} от предыдущей средней", "1м: свеча подтвердила смену направления"]
    return _make_addon_signal(STRATEGY_E, symbol, side, price, atr, 6, "overheat_reversal",
                              reasons, int(cur["open_time"]), diagnostics)



# ============================================================

# 9. LIVE QUOTE + SIGNAL STORAGE
# ============================================================

async def get_live_price(market: str, symbol: str) -> float | None:
    if http_session is None or http_session.closed:
        return None
    if market != "swap":
        return None
    # Удаляем старую котировку: нельзя использовать bid/ask из прошлого цикла.
    latest_quotes.pop((market, symbol), None)
    endpoint = "/openApi/swap/v2/quote/price"
    quote_endpoint = "/openApi/swap/v2/quote/ticker"
    try:
        response = await api_get(http_session, endpoint, {"symbol": symbol})
        data = extract_data(response)
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            return None
        price = 0.0
        for key in ("price", "lastPrice", "last", "close"):
            v = safe_float(data.get(key), 0)
            if v > 0:
                price = v
                break
        if price <= 0:
            return None
        latest_prices[(market, symbol)] = price
        # Пробуем получить реальные bid/ask
        try:
            qresp = await api_get(http_session, quote_endpoint, {"symbol": symbol})
            qdata = extract_data(qresp)
            if isinstance(qdata, list):
                qdata = qdata[0] if qdata else None
            if isinstance(qdata, dict):
                bid = safe_float(qdata.get("bidPrice") or qdata.get("bid"), 0)
                ask = safe_float(qdata.get("askPrice") or qdata.get("ask"), 0)
                if bid > 0 and ask > 0 and ask >= bid:
                    latest_quotes[(market, symbol)] = {
                        "price": price, "bid": bid, "ask": ask,
                        "spread_pct": ((ask - bid) / price * 100.0),
                        "is_synthetic_spread": False,
                        "updated_at": utc_text(),
                    }
        except Exception:
            pass
        return price
    except Exception:
        log.exception("Ошибка получения цены %s %s", market, symbol)
        return None

async def fetch_live_market_snapshot(market: str, symbol: str) -> dict[str, Any] | None:
    price = await get_live_price(market, symbol)
    if price is None or price <= 0:
        return None
    cached_quote = latest_quotes.get((market, symbol), {})
    bid = safe_float(cached_quote.get("bid"), 0.0)
    ask = safe_float(cached_quote.get("ask"), 0.0)
    is_synthetic = False
    if bid <= 0 or ask <= 0 or ask < bid:
        half_spread = price * (DEFAULT_SYNTHETIC_SPREAD_PCT / 200.0)
        bid = max(price - half_spread, 1e-9)
        ask = price + half_spread
        is_synthetic = True
    spread_pct = ((ask - bid) / price * 100.0) if price > 0 else DEFAULT_SYNTHETIC_SPREAD_PCT
    snap = {
        "price": price, "bid": bid, "ask": ask, "spread_pct": spread_pct,
        "is_synthetic_spread": is_synthetic, "updated_at": utc_text(),
    }
    latest_quotes[(market, symbol)] = snap
    return snap

def enrich_signal_with_live_data(sig: dict[str, Any], snapshot: dict[str, Any] | None) -> dict[str, Any]:
    enriched = dict(sig)
    now_dt = utc_now()
    enriched.setdefault("strategy_version", STRATEGY_B)
    enriched.setdefault("entry_mode", "market")
    enriched.setdefault("market_regime", "standard")
    enriched.setdefault("quality_score", float(enriched.get("score", 0)) * 10.0)
    enriched.setdefault("estimated_fee_pct", (TAKER_FEE_RATE * 2.0) * 100.0)
    enriched.setdefault("estimated_slippage_pct", DEFAULT_SLIPPAGE_PCT * 2.0)
    enriched.setdefault("net_rr", safe_float(enriched.get("rr"), 0.0))
    enriched["price_updated_at"] = utc_text(now_dt)
    enriched["expires_at"] = utc_text(now_dt + timedelta(minutes=SIGNAL_TTL_MINUTES))

    if not snapshot or safe_float(snapshot.get("price")) <= 0:
        enriched["status"] = "CANCELLED"
        enriched["cancel_reason"] = "Нет актуальной котировки BingX"
        return enriched
    if snapshot.get("is_synthetic_spread"):
        enriched["status"] = "CANCELLED"
        enriched["cancel_reason"] = "Нет реального bid/ask: нельзя надёжно оценить спред"
        return enriched

    if snapshot and safe_float(snapshot.get("price")) > 0:
        live_p = safe_float(snapshot["price"])
        bid = safe_float(snapshot.get("bid"), live_p)
        ask = safe_float(snapshot.get("ask"), live_p)
        spread_pct = safe_float(snapshot.get("spread_pct"), DEFAULT_SYNTHETIC_SPREAD_PCT)
        enriched["live_price"] = live_p
        enriched["bid"] = bid
        enriched["ask"] = ask
        enriched["spread_pct"] = spread_pct

        if spread_pct > MAX_SPREAD_PCT:
            enriched["status"] = "CANCELLED"
            enriched["cancel_reason"] = f"Спред {spread_pct:.3f}% > MAX_SPREAD_PCT {MAX_SPREAD_PCT}%"
            log.info("Сигнал отменён (спред): %s %s %.3f%%", sig.get("market"), sig.get("symbol"), spread_pct)
            return enriched

        base_entry = safe_float(enriched["entry"])
        sl = safe_float(enriched["sl"])
        risk_dist = abs(base_entry - sl)
        drift_r = (abs(live_p - base_entry) / risk_dist) if risk_dist > 0 else 0.0

        max_allowed_drift = MAX_ENTRY_DRIFT_R if enriched.get("entry_mode") == "market" else MAX_ENTRY_DRIFT_R * 2.0
        if drift_r > max_allowed_drift:
            enriched["status"] = "CANCELLED"
            enriched["cancel_reason"] = f"Цена ушла от зоны сигнала ({drift_r:.2f}R)"
            log.info("Сигнал отменён (дрифт %.2fR): %s %s", drift_r, sig.get("market"), sig.get("symbol"))
            return enriched

        if enriched.get("entry_mode") == "market" and risk_dist > 0:
            exec_entry = ask if is_long_side(enriched["side"]) else bid
            if exec_entry <= 0:
                enriched["status"] = "CANCELLED"
                enriched["cancel_reason"] = "Некорректная цена bid/ask"
                return enriched
            enriched["entry"] = exec_entry
            if is_long_side(enriched["side"]):
                enriched["sl"] = exec_entry - risk_dist
                enriched["tp1"] = exec_entry + risk_dist * TP1_R
                enriched["tp2"] = exec_entry + risk_dist * TP2_R
            else:
                enriched["sl"] = exec_entry + risk_dist
                enriched["tp1"] = exec_entry - risk_dist * TP1_R
                enriched["tp2"] = exec_entry - risk_dist * TP2_R
            enriched["tp"] = enriched["tp2"]
            enriched["rr"] = abs(enriched["tp2"] - exec_entry) / abs(exec_entry - enriched["sl"])

        calc_entry = safe_float(enriched.get("entry"), live_p)
        fee_pct = safe_float(enriched.get("estimated_fee_pct"), 0.0)
        slip_pct = safe_float(enriched.get("estimated_slippage_pct"), 0.0)
        cost_dist = calc_entry * ((spread_pct + fee_pct + slip_pct) / 100.0)
        eff_risk = max(abs(calc_entry - safe_float(enriched.get("sl"))) + cost_dist * 0.5, 1e-9)
        eff_rew = max(abs(safe_float(enriched.get("tp2")) - calc_entry) - cost_dist, 0.0)
        enriched["net_rr"] = eff_rew / eff_risk
        base_score = safe_float(enriched.get("score"), 0.0)
        strategy_name = str(enriched.get("strategy_version", STRATEGY_B))
        if strategy_name in ADDON_STRATEGIES:
            # Для C/D/E используем ту же формулу качества, что и при создании
            # кандидата в _make_addon_signal. Формула B ниже остаётся неизменной.
            enriched["quality_score"] = round(min(
                100.0, 62.0 + base_score * 5.0 - spread_pct * 10.0
            ), 1)
        else:
            enriched["quality_score"] = round(min(100.0, 60.0 + base_score * 4.0
                + (5.0 if enriched["net_rr"] >= 2.0 else 0.0)
                - spread_pct * 20.0), 1)
        if enriched["net_rr"] < MIN_NET_RR or enriched["quality_score"] < MIN_QUALITY_SCORE:
            enriched["status"] = "CANCELLED"
            enriched["cancel_reason"] = "Качество или Net RR не прошли проверку по реальной котировке"
            return enriched
    else:
        enriched["live_price"] = enriched["entry"]
        enriched["bid"] = enriched["entry"]
        enriched["ask"] = enriched["entry"]
        enriched["spread_pct"] = DEFAULT_SYNTHETIC_SPREAD_PCT

    if enriched.get("entry_mode") == "level":
        enriched["status"] = "WAITING_LEVEL"
    else:
        enriched.setdefault("status", "ACTIVE")
    return enriched

def log_signal_event(signal_id: int, event_type: str,
                     old_status: str | None = None, new_status: str | None = None,
                     price: float | None = None, reason: str = "") -> None:
    with db() as con:
        con.execute("""INSERT INTO signal_events
            (signal_id, event_type, old_status, new_status, price, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (signal_id, event_type, old_status, new_status,
             safe_float(price, 0.0) if price is not None else None, reason, utc_text()))

def save_signal(sig: dict[str, Any]) -> int | None:
    if sig.get("status") == "CANCELLED":
        return None
    now = utc_text()
    reasons_val = sig.get("reasons", "")
    reasons_text = "; ".join(str(x) for x in reasons_val) if isinstance(reasons_val, (list, tuple)) else str(reasons_val)

    with db() as con:
        cols = table_columns(con, "signals")
        if "setup_key" in cols:
            exists = con.execute("SELECT id FROM signals WHERE setup_key=? LIMIT 1", (sig["setup_key"],)).fetchone()
            if exists:
                return None
        recent = con.execute(f"""SELECT id FROM signals
            WHERE market=? AND symbol=? AND side=?
              AND COALESCE(strategy_version, 'A')=?
              AND julianday(created_at) >= julianday('now', '-{SIGNAL_COOLDOWN_MINUTES} minutes')
            ORDER BY id DESC LIMIT 1""",
            (sig["market"], sig["symbol"], sig["side"], sig.get("strategy_version", STRATEGY_B))).fetchone()
        if recent:
            return None

        values = {
            "created_at": now, "updated_at": now,
            "market": sig["market"], "symbol": sig["symbol"], "side": sig["side"],
            "entry": sig["entry"], "sl": sig["sl"], "tp": sig["tp2"],
            "rr": sig["rr"], "score": sig["score"], "reasons": reasons_text,
            "setup_key": sig["setup_key"], "status": sig.get("status", "ACTIVE"),
            "strategy_version": sig.get("strategy_version", STRATEGY_B),
            "live_price": sig.get("live_price", sig["entry"]),
            "bid": sig.get("bid"), "ask": sig.get("ask"),
            "spread_pct": sig.get("spread_pct"),
            "price_updated_at": sig.get("price_updated_at", now),
            "expires_at": sig.get("expires_at"),
            "entry_mode": sig.get("entry_mode", "market"),
            "entry_zone_low": sig.get("entry_zone_low"),
            "entry_zone_high": sig.get("entry_zone_high"),
            "confirmation_rule": sig.get("confirmation_rule"),
            "quality_score": sig.get("quality_score"),
            "market_regime": sig.get("market_regime"),
            "estimated_fee_pct": sig.get("estimated_fee_pct"),
            "estimated_slippage_pct": sig.get("estimated_slippage_pct"),
            "net_rr": sig.get("net_rr"),
            "cancel_reason": sig.get("cancel_reason"),
        }
        available = [c for c in values if c in cols]
        names_sql = ", ".join(f'"{c}"' for c in available)
        placeholders = ", ".join("?" for _ in available)
        cur = con.execute(f"INSERT INTO signals ({names_sql}) VALUES ({placeholders})",
                          tuple(values[c] for c in available))
        signal_id = int(cur.lastrowid)

    log_signal_event(signal_id, "created", None, values.get("status", "ACTIVE"),
                     safe_float(sig.get("live_price", sig["entry"])),
                     f"Стратегия {values.get('strategy_version', 'A')}")
    return signal_id

def expire_stale_signals() -> list[dict[str, Any]]:
    expired_items = []
    now_dt = utc_now()
    with db() as con:
        rows = con.execute("""SELECT id, symbol, market, side, status, expires_at
            FROM signals
            WHERE UPPER(COALESCE(status, '')) IN ('ACTIVE', 'WAITING_LEVEL', 'NEW')
              AND expires_at IS NOT NULL""").fetchall()
        for r in rows:
            exp_dt = parse_utc_text(r["expires_at"])
            if exp_dt and now_dt >= exp_dt:
                con.execute("""UPDATE signals SET status='EXPIRED',
                    cancel_reason='Истёк TTL', updated_at=? WHERE id=?""",
                    (utc_text(now_dt), r["id"]))
                expired_items.append(dict(r))
    for item in expired_items:
        log_signal_event(item["id"], "expired", item["status"], "EXPIRED",
                         reason="Истёк срок действия")
    return expired_items


# ============================================================
# 10. USER REGISTRATION
# ============================================================







def register_user(user):
    if user is None:
        return
    with db() as con:
        con.execute("INSERT OR IGNORE INTO users (user_id, username, created_at, is_active) VALUES (?, ?, ?, 1)",
                    (user.id, user.username or "", utc_text()))
        con.execute("""INSERT OR IGNORE INTO user_settings
            (user_id, risk_percent, leverage, notifications, strategy_version) VALUES (?, ?, ?, 1, ?)""",
            (user.id, RISK_PERCENT, FUTURES_LEVERAGE, STRATEGY_B))


def get_user_balance(user_id: int) -> float | None:
    with db() as con:
        row = con.execute(
            "SELECT balance_usdt FROM user_risk_profiles WHERE user_id=?",
            (int(user_id),),
        ).fetchone()
    if not row:
        return None
    balance = safe_float(row["balance_usdt"], 0.0)
    return balance if balance > 0 else None


def set_user_balance(user_id: int, balance_usdt: float) -> None:
    if not math.isfinite(balance_usdt) or balance_usdt <= 0:
        raise ValueError("Баланс должен быть положительным числом.")
    with db() as con:
        con.execute("""INSERT INTO user_risk_profiles (user_id, balance_usdt, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                balance_usdt=excluded.balance_usdt,
                updated_at=excluded.updated_at""",
            (int(user_id), float(balance_usdt), utc_text()))








# ============================================================
# 11. SIGNAL FORMATTING
# ============================================================




# ============================================================
# 12. TRADE CREATE / UPDATE / CLOSE
# ============================================================





# ============================================================
# 13. TEXT FORMATTERS
# ============================================================


def position_size_text(signal, balance_usdt: float | None) -> str:
    if balance_usdt is None:
        return (
            "<b>РАСЧЁТ ПОЗИЦИИ:</b> не настроен. "
            "Укажи выделенный для торговли баланс командой <code>/setbalance 1000</code> (USDT).\n"
        )
    try:
        sizing = calculate_linear_usdtm_position_size(
            balance_usdt=balance_usdt,
            entry_price=safe_float(signal.get("entry")),
            stop_price=safe_float(signal.get("sl")),
            side=str(signal.get("side") or ""),
            risk_percent=RISK_PERCENT,
            leverage=FUTURES_LEVERAGE,
        )
    except (TypeError, ValueError):
        return "<b>РАСЧЁТ ПОЗИЦИИ:</b> невозможно рассчитать — проверь цену входа и SL.\n"

    symbol = str(signal.get("symbol") or "").upper()
    spec = contract_specs_cache.get(symbol)
    fresh = bool(spec) and contract_specs_updated_at > 0 and time.time() - contract_specs_updated_at <= CONTRACT_SPECS_MAX_AGE_SEC
    contract_note = ""
    if fresh:
        checked = apply_bingx_contract_constraints(sizing, spec, str(signal.get("side") or ""), FUTURES_LEVERAGE)
        if checked.get("validated"):
            for key in ("quantity", "risk_usdt", "notional_usdt", "margin_usdt"):
                source = {"quantity": "quantity", "risk_usdt": "risk_usdt", "notional_usdt": "notional_usdt", "margin_usdt": "margin_usdt"}[key]
                sizing[key] = float(checked[source])
            sizing["fits_balance"] = sizing["margin_usdt"] <= balance_usdt
            contract_note = f"Биржевые ограничения BingX: <b>проверены</b> (точность {checked['quantity_precision']} знаков).\n"
        else:
            contract_note = "⛔ <b>Объём не подтверждён биржей:</b> " + html.escape(" / ".join(checked.get("reasons", []))) + ". Не используй расчёт как готовый ордер.\n"
    else:
        contract_note = "⛔ <b>Объём не подтверждён биржей:</b> нет свежих данных контракта. Не используй расчёт как готовый ордер.\n"
    quantity = f"{sizing['quantity']:.10f}".rstrip("0").rstrip(".")
    block = (
        "<b>РАСЧЁТ ПОЗИЦИИ (ориентир):</b>\n"
        f"Баланс: <code>{balance_usdt:,.2f} USDT</code>\n"
        f"Риск до SL ({RISK_PERCENT:.1f}%): <code>{sizing['risk_usdt']:,.2f} USDT</code>\n"
        f"Объём: <code>{quantity} {html.escape(str(signal.get('symbol', '')).split('-')[0])}</code>\n"
        f"Номинал позиции: <code>{sizing['notional_usdt']:,.2f} USDT</code>\n"
        f"Маржа при {FUTURES_LEVERAGE}x: <code>{sizing['margin_usdt']:,.2f} USDT</code>\n"
    )
    block += contract_note
    if not sizing["fits_balance"]:
        block += (
            "⛔ <b>Не хватает баланса под расчётную маржу.</b> "
            "Не используй этот объём.\n"
        )
    elif sizing["margin_usdt"] >= balance_usdt * 0.90:
        block += (
            "⚠️ <b>Маржа почти равна всему сохранённому балансу.</b> "
            "Оставь резерв; комиссии и funding не включены.\n"
        )
    block += (
        "<i>Расчёт от указанной цены входа и SL; не учтены комиссии, funding и проскальзывание. "
        "Проверка использует последние публичные ограничения контракта BingX (обновление раз в 6 часов). "
        "фактическая цена исполнения может изменить риск. "
        "10% применяется отдельно к каждому сигналу: одновременные сделки суммируют риск.</i>\n"
    )
    return block


def signal_text(signal, balance_usdt: float | None = None):
    tp1 = safe_float(signal.get("tp1"), 0)
    tp2 = safe_float(signal.get("tp2"), signal.get("tp", 0))
    live_price = safe_float(signal.get("live_price"), signal["entry"])
    spread = safe_float(signal.get("spread_pct"), 0.0)
    quality = safe_float(signal.get("quality_score"), 0.0)
    reasons = signal.get("reasons", [])
    if not isinstance(reasons, (list, tuple)):
        reasons = [part.strip() for part in str(reasons).split(";") if part.strip()]
    reasons_text = "\n".join(f"• {html.escape(str(item))}" for item in reasons if item) or "—"
    mode = "Вход от зоны" if signal.get("entry_mode") == "level" else "Рыночный вход"
    validity = format_moscow_from_utc(signal.get("expires_at")) if signal.get("expires_at") else "—"
    zone_block = ""
    if signal.get("entry_mode") == "level":
        zone_low = safe_float(signal.get("entry_zone_low"), signal["entry"])
        zone_high = safe_float(signal.get("entry_zone_high"), signal["entry"])
        zone_block = (
            f"<b>ЗОНА ВХОДА:</b> <code>{format_price(zone_low)} — {format_price(zone_high)}</code>\n"
            f"<b>Подтверждение:</b> {html.escape(str(signal.get('confirmation_rule') or 'Дождаться подтверждения на 5м.'))}\n"
        )
    direction = "LONG" if is_long_side(signal["side"]) else "SHORT"
    indicator = "🟢" if direction == "LONG" else "🔴"
    symbol = html.escape(str(signal["symbol"]))
    return (
        f"{indicator} <b>{direction} — {symbol}</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>Стратегия:</b> {html.escape(str(signal.get('strategy_version', STRATEGY_B)))}\n"
        f"<b>Качество сетапа:</b> <code>{quality:.0f}/100</code>\n"
        f"<b>Рынок:</b> USDT-M Futures  |  <b>ТФ:</b> 1m / 5m / 15m / 1h\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>ВХОД:</b> <code>{format_price(signal['entry'])}</code>\n"
        f"{zone_block}"
        f"<b>SL:</b> <code>{format_price(signal['sl'])}</code>\n"
        f"<b>TP1:</b> <code>{format_price(tp1)}</code>  ({TP1_R}R)\n"
        f"<b>TP2:</b> <code>{format_price(tp2)}</code>  ({TP2_R}R)\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"{position_size_text(signal, balance_usdt)}"
        f"<b>RR:</b> {safe_float(signal.get('rr')):.2f}  |  <b>Net RR:</b> {safe_float(signal.get('net_rr')):.2f}\n"
        f"<b>Спред:</b> {spread:.3f}%  |  <b>Текущая цена:</b> <code>{format_price(live_price)}</code>\n"
        f"<b>Вход:</b> {mode}\n"
        f"<b>Сигнал действителен до:</b> {validity} МСК\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>Почему выбран:</b>\n{reasons_text}\n\n"
        "<i>Оценка 0–100 — внутренний балл фильтра, не вероятность выигрыша. "
        "Бот не открывает ордера и не отслеживает позицию после сигнала. Проверяй уровни на бирже.</i>"
    )



# ============================================================
# 14. TELEGRAM KEYBOARDS
# ============================================================

def main_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔎 Сканировать Futures")],
        [KeyboardButton(text="📊 Статистика стратегий")],
        [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="🖥 Статус системы")],
    ], resize_keyboard=True)






# ============================================================
# 15. ANALYSIS + SCAN
# ============================================================

def _is_usable_ws_1m_frame(frame: pd.DataFrame | None, current_ms: int,
                           min_rows: int = MIN_CANDLE_ROWS) -> bool:
    """Use trade-built 1m candles only when the entire indicator window is continuous."""
    if frame is None or len(frame) < min_rows:
        return False
    required = ("open_time", "close_time")
    if any(column not in frame.columns for column in required):
        return False
    open_times = pd.to_numeric(frame["open_time"], errors="coerce")
    close_times = pd.to_numeric(frame["close_time"], errors="coerce")
    if open_times.isna().any() or close_times.isna().any():
        return False
    opens = open_times.astype("int64").to_numpy()
    closes = close_times.astype("int64").to_numpy()
    if len(opens) != len(frame) or len(closes) != len(frame):
        return False
    if len(opens) > 1 and not bool((np.diff(opens) == 60_000).all()):
        return False
    age_ms = int(current_ms) - int(closes[-1])
    return 0 <= age_ms <= 90_000


async def analyze_market_symbol(market, symbol, strategy_version=DEFAULT_STRATEGY,
                                diagnostics=None, frames_out=None):
    # Стратегия B сохраняет прежние пять таймфреймов и прежние фильтры.
    # frames_out передаёт уже загруженные данные независимым C/D/E без повторных REST-запросов.
    market = "swap"
    key = (market, symbol)

    def stop(reason, stage="market_data"):
        if diagnostics is not None:
            diagnostics["reason"] = reason
            diagnostics["stage"] = stage
        return None

    if key in _unavailable_symbols:
        return stop("контракт ранее отмечен как недоступный")

    candles = {}
    errors = {}
    for interval in ("1m", "5m", "15m", "1h", "4h"):
        try:
            data = await get_klines(market, symbol, interval, KLINE_LIMIT)
            if data is None or len(data) < MIN_CANDLE_ROWS:
                actual = 0 if data is None else len(data)
                errors[interval] = f"{interval}: получено {actual} свечей, требуется {MIN_CANDLE_ROWS}"
                continue
            candles[interval] = data
            try:
                save_closed_candles(market, symbol, interval, data)
            except Exception:
                log.exception("Не сохранились свечи %s %s", symbol, interval)
        except MarketError as exc:
            message = str(exc).lower()
            if ("100204" in str(exc) or "109425" in str(exc)
                    or "symbol is not found" in message or "not exist" in message):
                _unavailable_symbols.add(key)
                log.warning("Фьючерсный контракт %s недоступен: %s", symbol, exc)
                if frames_out is not None:
                    frames_out.update(candles)
                return stop(f"контракт недоступен: {exc}")
            errors[interval] = f"ошибка API на {interval}: {exc}"
        except asyncio.TimeoutError as exc:
            errors[interval] = f"тайм-аут API на {interval}: {exc}"

    if "1m" in candles:
        live_frame = ws_1m_frames.get(symbol)
        if live_frame is not None:
            ws_usable = _is_usable_ws_1m_frame(live_frame, now_ms())
            if ws_usable:
                candles["1m"] = live_frame.copy()
            else:
                log.debug(
                    "WS_1M_FALLBACK %s: frame is stale, incomplete, or has a gap; "
                    "using BingX REST klines", symbol
                )

    if frames_out is not None:
        frames_out.update(candles)

    required = ("1m", "5m", "15m", "1h", "4h")
    missing = [tf for tf in required if tf not in candles]
    if missing:
        reason = errors.get(missing[0], f"нет данных таймфрейма {missing[0]}")
        return stop(reason, "api_error" if ("ошибка API" in reason or "тайм-аут" in reason) else "market_data")

    if diagnostics is not None:
        diagnostics["stage"] = "strategy_filter"
    signal = build_signal_b(market, symbol, candles, quote=None, diagnostics=diagnostics)
    if not signal:
        if diagnostics is not None and not diagnostics.get("reason"):
            diagnostics["reason"] = "сетап не прошёл фильтры стратегии"
        return None

    if diagnostics is not None:
        diagnostics["stage"] = "live_quote"
    snapshot = await fetch_live_market_snapshot(market, symbol)
    if not snapshot:
        return stop("не удалось получить актуальную рыночную котировку", "live_quote")

    final_signal = enrich_signal_with_live_data(signal, snapshot)
    if final_signal.get("status") == "CANCELLED":
        return stop(final_signal.get("cancel_reason") or "сигнал отменён проверкой котировки", "live_validation")
    if diagnostics is not None:
        diagnostics["stage"] = "accepted"
        diagnostics["reason"] = "сигнал прошёл все проверки"
    return final_signal


async def analyze_addon_strategies(symbol: str, frames: dict[str, pd.DataFrame],
                                  diagnostics: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Проверяет независимые C/D/E и сохраняет точную причину каждого отказа."""
    if ("swap", symbol) in _unavailable_symbols:
        return []
    builders = (
        (STRATEGY_C, build_signal_c_breakout_retest),
        (STRATEGY_D, build_signal_d_impulse),
        (STRATEGY_E, build_signal_e_overheat_reversal),
    )
    results = []
    for strategy_name, builder in builders:
        stats = diagnostics.get(strategy_name) if diagnostics is not None else None
        if stats is not None:
            stats["checked"] += 1
        local_diag: dict[str, str] = {}
        try:
            candidate = builder(symbol, frames, local_diag)
            if not candidate:
                reason = local_diag.get("reason", "unspecified_filter_rejection")
                if stats is not None:
                    stats["rejected"] += 1
                    stats.setdefault("reasons", {})[reason] = stats.setdefault("reasons", {}).get(reason, 0) + 1
                log.info("ADDON_FILTER_REJECT %s %s: %s", strategy_name, symbol, reason)
                continue
            if stats is not None:
                stats["candidate"] += 1
            snapshot = await fetch_live_market_snapshot("swap", symbol)
            if not snapshot:
                reason = "live_quote_unavailable"
                if stats is not None:
                    stats["rejected"] += 1
                    stats.setdefault("reasons", {})[reason] = stats.setdefault("reasons", {}).get(reason, 0) + 1
                log.info("ADDON_REJECT %s %s: %s", strategy_name, symbol, reason)
                continue
            final_candidate = enrich_signal_with_live_data(candidate, snapshot)
            if final_candidate.get("status") == "CANCELLED":
                reason = str(final_candidate.get("cancel_reason") or "live_validation_cancelled")
                if stats is not None:
                    stats["rejected"] += 1
                    stats.setdefault("reasons", {})[reason] = stats.setdefault("reasons", {}).get(reason, 0) + 1
                log.info("ADDON_REJECT %s %s: %s", strategy_name, symbol, reason)
                continue
            results.append(final_candidate)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if stats is not None:
                stats["errors"] += 1
                reason = f"exception:{type(exc).__name__}"
                stats.setdefault("reasons", {})[reason] = stats.setdefault("reasons", {}).get(reason, 0) + 1
            log.exception("Ошибка дополнительной стратегии %s для %s", strategy_name, symbol)
    if not results:
        return []
    directions = {str(item.get("side")) for item in results}
    if len(directions) > 1:
        if diagnostics is not None:
            diagnostics["conflicts"] += 1
        log.info("ADDON_CONFLICT %s: стратегии C/D/E дали разные направления; сигнал пропущен", symbol)
        return []
    results.sort(key=lambda item: (
        safe_float(item.get("quality_score")),
        safe_float(item.get("net_rr")),
        safe_float(item.get("score")),
    ), reverse=True)
    return results[:1]



async def scan_market(strategy_version=DEFAULT_STRATEGY):
    async with _scan_lock:
        scan_started = time.monotonic()
        scan_started_utc = utc_text()
        found = []
        checked = 0
        completed = 0
        skipped_unavailable = 0
        reason_counts: dict[str, int] = {}
        stage_counts: dict[str, int] = {}
        symbol_results: dict[str, str] = {}
        addon_stats: dict[str, Any] = {
            name: {"checked": 0, "candidate": 0, "rejected": 0, "errors": 0, "reasons": {}}
            for name in ADDON_STRATEGIES
        }
        addon_stats["conflicts"] = 0
        addon_frames: dict[str, dict[str, pd.DataFrame]] = {}
        system_status["scan_count"] = int(system_status.get("scan_count", 0)) + 1
        scan_no = system_status["scan_count"]
        system_status["last_scan"] = scan_started_utc
        system_status["last_scan_started"] = scan_started_utc
        system_status["last_scan_finished"] = None
        system_status["last_scan_duration_sec"] = None
        system_status["last_scan_checked"] = 0
        system_status["last_scan_completed"] = 0
        system_status["last_scan_planned"] = len(SYMBOLS)
        system_status["last_scan_current_symbol"] = None
        system_status["last_scan_symbol_results_live"] = {}
        system_status["last_scan_started_monotonic"] = scan_started
        system_status["last_scan_summary"] = "Сканирование выполняется"
        active_strategies = "B,C,D,E" if ENABLE_ADDON_STRATEGIES else "B"
        log.info("SCAN #%s START market=USDT-M Futures planned=%s strategies=%s",
                 scan_no, len(SYMBOLS), active_strategies)
        expire_stale_signals()

        for symbol in SYMBOLS:
            key = ("swap", symbol)
            system_status["last_scan_current_symbol"] = symbol
            if key in _unavailable_symbols:
                skipped_unavailable += 1
                completed += 1
                reason = "контракт ранее отмечен как недоступный"
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
                symbol_results[symbol] = f"пропущена: {reason}"
                system_status["last_scan_completed"] = completed
                system_status["last_scan_symbol_results_live"] = dict(symbol_results)
                log.info("SCAN #%s %s SKIP: %s", scan_no, symbol, reason)
                system_status["last_scan_current_symbol"] = None
                continue

            checked += 1
            diag: dict[str, str] = {"stage": "starting", "reason": "анализ не завершён"}
            shared_frames: dict[str, pd.DataFrame] = {}
            try:
                log.info("SCAN #%s %s ANALYSIS_START progress=%s/%s",
                         scan_no, symbol, completed + 1, len(SYMBOLS))
                signal = await analyze_market_symbol(
                    "swap", symbol, STRATEGY_B, diagnostics=diag, frames_out=shared_frames
                )
                addon_frames[symbol] = shared_frames
                if not signal:
                    reason = diag.get("reason", "сигнал не сформирован")
                    stage = diag.get("stage", "unknown")
                    reason_counts[reason] = reason_counts.get(reason, 0) + 1
                    stage_counts[stage] = stage_counts.get(stage, 0) + 1
                    symbol_results[symbol] = reason
                    log.info("SCAN #%s %s B_REJECT stage=%s reason=%s",
                             scan_no, symbol, stage, reason)
                else:
                    signal_id = save_signal(signal)
                    if signal_id:
                        signal["id"] = signal_id
                        found.append(signal)
                        symbol_results[symbol] = f"принят сигнал {signal.get('side', '—')}"
                        log.info("SCAN #%s %s B_ACCEPT side=%s quality=%.1f net_rr=%.2f",
                                 scan_no, symbol, signal.get("side"),
                                 safe_float(signal.get("quality_score")), safe_float(signal.get("net_rr")))
                        if len(found) >= MAX_SIGNALS_PER_CYCLE:
                            if ENABLE_ADDON_STRATEGIES:
                                log.info("SCAN #%s reached MAX_SIGNALS_PER_CYCLE=%s; B signals capped, "
                                         "continuing market-data collection for independent C/D/E checks",
                                         scan_no, MAX_SIGNALS_PER_CYCLE)
                            else:
                                log.info("SCAN #%s reached MAX_SIGNALS_PER_CYCLE=%s; "
                                         "stopping B-only scan", scan_no, MAX_SIGNALS_PER_CYCLE)
                                break
                    else:
                        reason = "B: сигнал прошёл анализ, но не сохранён (дубликат/cooldown)"
                        reason_counts[reason] = reason_counts.get(reason, 0) + 1
                        symbol_results[symbol] = reason
                        log.info("SCAN #%s %s B_NOT_SAVED: %s", scan_no, symbol, reason)
            except asyncio.CancelledError:
                raise
            except MarketError as exc:
                reason = f"ошибка BingX/API: {exc}"
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
                symbol_results[symbol] = reason
                log.warning("SCAN #%s %s API_ERROR: %s", scan_no, symbol, exc)
            except Exception as exc:
                reason = f"внутренняя ошибка: {type(exc).__name__}: {exc}"
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
                symbol_results[symbol] = reason
                log.exception("SCAN #%s %s INTERNAL_ERROR", scan_no, symbol)
            finally:
                completed += 1
                system_status["last_scan_checked"] = checked
                system_status["last_scan_completed"] = completed
                system_status["last_scan_symbol_results_live"] = dict(symbol_results)
                system_status["last_scan_current_symbol"] = None
                log.info("SCAN #%s %s ANALYSIS_FINISH progress=%s/%s elapsed=%.2fs",
                         scan_no, symbol, completed, len(SYMBOLS), time.monotonic() - scan_started)

        # C/D/E получают те же свечи — второй раз REST-данные не запрашиваются.
        # Эти модули выключены по умолчанию: 7-дневная проверка показала
        # отрицательный результат для всех трёх стратегий.
        if not ENABLE_ADDON_STRATEGIES:
            log.info("SCAN #%s ADDON_STRATEGIES_DISABLED: "
                     "set ENABLE_ADDON_STRATEGIES=true only after further validation", scan_no)
        for symbol, frames in (addon_frames.items() if ENABLE_ADDON_STRATEGIES else ()):
            if ("swap", symbol) in _unavailable_symbols:
                continue
            try:
                addon_signals = await analyze_addon_strategies(symbol, frames, addon_stats)
                for addon_signal in addon_signals:
                    if any(item.get("symbol") == symbol for item in found):
                        log.info("ADDON_SKIP %s: по этой монете уже принят сигнал в текущем цикле", symbol)
                        continue
                    if len(found) >= MAX_SIGNALS_PER_CYCLE:
                        log.info("ADDON_CAP_REACHED %s strategy=%s: сигнал проверен, но общий лимит цикла уже достигнут",
                                 symbol, addon_signal.get("strategy_version"))
                        continue
                    signal_id = save_signal(addon_signal)
                    if signal_id:
                        addon_signal["id"] = signal_id
                        found.append(addon_signal)
                        log.info("SCAN #%s %s ACCEPT strategy=%s side=%s quality=%.1f net_rr=%.2f",
                                 scan_no, symbol, addon_signal.get("strategy_version"),
                                 addon_signal.get("side"), safe_float(addon_signal.get("quality_score")),
                                 safe_float(addon_signal.get("net_rr")))
                    else:
                        log.info("SCAN #%s %s ADDON_NOT_SAVED strategy=%s (duplicate/cooldown)",
                                 scan_no, symbol, addon_signal.get("strategy_version"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("Ошибка дополнительных стратегий для %s: %s", symbol, exc)

        duration = time.monotonic() - scan_started
        finished_utc = utc_text()
        system_status["signals_count"] = int(system_status.get("signals_count", 0)) + len(found)
        system_status["last_scan_finished"] = finished_utc
        system_status["last_scan_duration_sec"] = round(duration, 2)
        system_status["last_scan_checked"] = checked
        system_status["last_scan_completed"] = completed
        system_status["last_scan_current_symbol"] = None
        system_status["last_scan_started_monotonic"] = None
        system_status["last_scan_symbol_results"] = dict(symbol_results)
        system_status["last_scan_symbol_results_live"] = dict(symbol_results)
        system_status["last_scan_skipped_unavailable"] = skipped_unavailable
        system_status["last_addon_stats"] = addon_stats
        summary = (f"checked={checked}/{len(SYMBOLS)}; unavailable_skipped={skipped_unavailable}; "
                   f"accepted={len(found)}; duration={duration:.1f}s")
        system_status["last_scan_summary"] = summary
        system_status["last_scan_rejections"] = dict(reason_counts)
        system_status["last_scan_stages"] = dict(stage_counts)
        log.info("SCAN #%s FINISH started=%s finished=%s %s",
                 scan_no, scan_started_utc, finished_utc, summary)
        for strategy_name in ADDON_STRATEGIES:
            stats = addon_stats[strategy_name]
            log.info("SCAN #%s ADDON_STATS %s: checked=%s candidates=%s rejected=%s errors=%s",
                     scan_no, strategy_name, stats["checked"], stats["candidate"],
                     stats["rejected"], stats["errors"])
            if stats.get("reasons"):
                log.info("SCAN #%s ADDON_REASONS %s: %s", scan_no, strategy_name, stats["reasons"])
        log.info("SCAN #%s ADDON_CONFLICTS: %s", scan_no, addon_stats["conflicts"])
        if reason_counts:
            top_reasons = sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))[:12]
            log.info("SCAN #%s REJECTION_COUNTS: %s", scan_no,
                     " | ".join(f"{count}x {reason}" for reason, count in top_reasons))
        if stage_counts:
            log.info("SCAN #%s STAGE_COUNTS: %s", scan_no, stage_counts)
        return found



async def broadcast_signal(signal):
    with db() as con:
        users = con.execute("""SELECT u.user_id, p.balance_usdt
            FROM users AS u
            LEFT JOIN user_risk_profiles AS p ON p.user_id=u.user_id
            WHERE u.is_active=1""").fetchall()
    delivered = 0
    failed = 0
    for row in users:
        try:
            await bot.send_message(
                row["user_id"], signal_text(signal, row["balance_usdt"]), parse_mode="HTML"
            )
            delivered += 1
        except Exception:
            failed += 1
            log.exception("Не отправлен сигнал пользователю %s", row["user_id"])
    log.info("SIGNAL_DELIVERY symbol=%s side=%s delivered=%s failed=%s active_users=%s",
             signal.get("symbol"), signal.get("side"), delivered, failed, len(users))



# ============================================================
# 16. HANDLERS
# ============================================================

@router.message(CommandStart())
async def start_command(message: Message):
    register_user(message.from_user)
    await message.answer(
        f"<b>Trading Bot v{BOT_VERSION} — Futures Scalping</b>\n\n"
        "Бот анализирует только USDT-M Futures на BingX.\n"
        "Он отправляет редкие сигналы, только если совпали тренд 1H/15m, "
        "импульс 5m и 1m, структура рынка, объём и фильтры издержек.\n\n"
        "После отправки сигнала бот не сопровождает сделку, не двигает TP/SL "
        "и не отправляет уведомления о достижении уровней. Реальные ордера отключены.\n\n"
        "Если подходящего сетапа нет — бот ничего не отправляет.\n\n"
        "Для расчёта объёма позиции задай торговый баланс: <code>/setbalance 1000</code> (USDT).",
        reply_markup=main_keyboard(), parse_mode="HTML")


@router.message(Command("setbalance"))
async def set_balance_command(message: Message):
    register_user(message.from_user)
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        await message.answer(
            "Укажи выделенный для торговли баланс в USDT. Пример: <code>/setbalance 1000</code>",
            parse_mode="HTML",
        )
        return
    raw_value = parts[1].strip().replace(",", ".")
    balance = safe_float(raw_value, 0.0)
    if balance <= 0 or balance > 1_000_000_000:
        await message.answer("Баланс должен быть числом больше 0 и не более 1 000 000 000 USDT.")
        return
    set_user_balance(message.from_user.id, balance)
    await message.answer(
        f"✅ Торговый баланс сохранён: <b>{balance:,.2f} USDT</b>.\n"
        f"Риск на каждый сигнал: <b>{RISK_PERCENT:.1f}%</b> "
        f"({balance * RISK_PERCENT / 100:,.2f} USDT).\n"
        "Для изменения отправь новую команду /setbalance.",
        parse_mode="HTML",
    )


@router.message(Command("balance"))
async def balance_command(message: Message):
    register_user(message.from_user)
    balance = get_user_balance(message.from_user.id)
    if balance is None:
        await message.answer(
            "Баланс ещё не задан. Пример: <code>/setbalance 1000</code> (USDT).",
            parse_mode="HTML",
        )
        return
    await message.answer(
        f"Сохранённый торговый баланс: <b>{balance:,.2f} USDT</b>\n"
        f"Риск на один сигнал ({RISK_PERCENT:.1f}%): "
        f"<b>{balance * RISK_PERCENT / 100:,.2f} USDT</b>.",
        parse_mode="HTML",
    )


@router.message(Command("cancel"))
async def cancel_command(message: Message):
    await message.answer("Нет активных диалогов. Используй меню.", reply_markup=main_keyboard())


@router.message(F.text == "🔎 Сканировать Futures")
async def scan_market_callback(message: Message):
    register_user(message.from_user)
    status = await message.answer("⏳ Проверяю USDT-M Futures. Отправлю только сильные сетапы...")
    try:
        signals = await scan_market(strategy_version=STRATEGY_B)
        if not signals:
            await status.edit_text("Сильных подтверждённых сетапов сейчас нет. Это нормально: бот не будет отправлять слабые прогнозы.")
            return
        await status.edit_text(f"✅ Найдено сильных сетапов: {len(signals)}.")
        for signal in signals:
            await message.answer(
                signal_text(signal, get_user_balance(message.from_user.id)), parse_mode="HTML"
            )
            log.info("SIGNAL_DELIVERY mode=manual symbol=%s side=%s user_id=%s",
                     signal.get("symbol"), signal.get("side"), message.from_user.id)
    except Exception:
        log.exception("Ошибка ручного сканирования")
        await status.edit_text("❌ Не удалось завершить анализ. Подробности записаны в лог.")














@router.message(F.text == "⚙️ Настройки")
async def settings_handler(message: Message):
    register_user(message.from_user)
    await message.answer(
        "⚙️ <b>Параметры стратегии</b>\n\n"
        "Рынок: <b>USDT-M Futures</b>\n"
        f"Стратегия B: <b>активна</b> | C/D/E: <b>{'включены' if ENABLE_ADDON_STRATEGIES else 'выключены'}</b>\n"
        f"Расчётный риск: <b>{RISK_PERCENT:.1f}%</b> на каждый сигнал\n"
        f"Плечо для расчётов: <b>{FUTURES_LEVERAGE}x</b>\n"
        f"Сохранённый торговый баланс: <b>{(get_user_balance(message.from_user.id) or 0):,.2f} USDT</b> "
        "(0 — не задан)\n"
        f"TP1: <b>{TP1_R}R</b>  |  TP2: <b>{TP2_R}R</b>\n"
        f"Минимальное качество: <b>{MIN_QUALITY_SCORE}/100</b>\n"
        f"Минимальный Net RR: <b>{MIN_NET_RR:.2f}</b>\n"
        f"Цикл сканирования: примерно каждые {MARKET_SCAN_INTERVAL_SEC // 60} мин после завершения предыдущего цикла.\n\n"
        "Риск и плечо показываются как ориентиры для ручного расчёта. "
        "Бот не размещает ордера и не управляет открытыми позициями.",
        parse_mode="HTML")





def format_scan_rejection_summary(rejections: Any, limit: int = 5) -> str:
    """Return the most common rejection reasons from the latest completed scan."""
    if not isinstance(rejections, dict) or not rejections:
        return "—"
    items = sorted(
        ((str(reason), int(safe_float(count, 0))) for reason, count in rejections.items()),
        key=lambda item: (-item[1], item[0]),
    )
    return "\n".join(
        f"{count}× {reason}" for reason, count in items[:max(1, limit)] if count > 0
    ) or "—"


def format_scan_symbol_summary(results: Any, limit: int = 14) -> str:
    """Return a compact per-symbol outcome list for scan diagnostics."""
    if not isinstance(results, dict) or not results:
        return "—"
    lines = []
    for symbol, reason in list(results.items())[:max(1, limit)]:
        reason_text = str(reason)
        if len(reason_text) > 120:
            reason_text = reason_text[:117] + "..."
        lines.append(f"{html.escape(str(symbol))}: {html.escape(reason_text)}")
    return "\n".join(lines) or "—"


@router.message(F.text == "🖥 Статус системы")
async def system_status_handler(message: Message):
    register_user(message.from_user)
    scan_running = bool(
        system_status.get("last_scan_started_monotonic") is not None
        and system_status.get("last_scan_finished") is None
    )
    rejection_title = (
        "Причины отказа предыдущего завершённого скана:"
        if scan_running else "Основные причины отказа последнего скана:"
    )
    rejection_summary = html.escape(
        format_scan_rejection_summary(system_status.get("last_scan_rejections"))
    )
    symbol_results = format_scan_symbol_summary(
        system_status.get("last_scan_symbol_results_live")
        if scan_running else system_status.get("last_scan_symbol_results")
    )
    started_monotonic = system_status.get("last_scan_started_monotonic")
    elapsed = (
        max(0.0, time.monotonic() - started_monotonic)
        if scan_running and isinstance(started_monotonic, (int, float)) else None
    )
    if scan_running:
        progress_line = (
            f"Прогресс сканирования: {system_status.get('last_scan_completed', 0)}/"
            f"{system_status.get('last_scan_planned', len(SYMBOLS))} пар"
            f" | сейчас: {html.escape(str(system_status.get('last_scan_current_symbol') or 'подготовка'))}"
            + (f" | прошло: {elapsed:.1f} сек." if elapsed is not None else "")
        )
    else:
        progress_line = (
            f"Проверено пар: {system_status.get('last_scan_checked', 0)}/"
            f"{system_status.get('last_scan_planned', len(SYMBOLS))}"
        )
    await message.answer(
        f"🖥 <b>Trading Bot v{BOT_VERSION}</b>\n\n"
        "Режим: <b>USDT-M Futures Scalping</b>\n"
        f"Стратегия B: <b>активна</b> | C/D/E: <b>{'активны' if ENABLE_ADDON_STRATEGIES else 'выключены по умолчанию'}</b>\n"
        "Реальные ордера: <b>отключены</b>\n"
        "Сопровождение сделок: <b>удалено</b>\n"
        f"API BingX: {html.escape(str(system_status.get('api_status', '—')))}\n"
        f"Сканов: {system_status.get('scan_count', 0)}\n"
        f"Отправлено сигналов: {system_status.get('signals_count', 0)}\n"
        f"Последний скан (UTC): {system_status.get('last_scan') or '—'}\n"
        f"Последний цикл: {html.escape(str(system_status.get('last_scan_summary') or '—'))}\n"
        f"{progress_line}\n"
        f"{rejection_title}\n<code>{rejection_summary}</code>\n"
        f"Результаты по монетам ({'текущий скан' if scan_running else 'последний скан'}):\n<code>{symbol_results}</code>\n"
        f"Длительность последнего завершённого скана: {system_status.get('last_scan_duration_sec') if system_status.get('last_scan_duration_sec') is not None else '—'} сек.\n"
        f"Недоступных контрактов: {len(_unavailable_symbols)}\n"
        f"WebSocket 1m: {'подключён' if ws_status.get('connected') else 'ожидание/переподключение'}\n"
        f"Последнее WS-сообщение: {ws_status.get('last_message') or '—'}\n"
        f"Переподключений WS: {ws_status.get('reconnects', 0)}\n"
        f"Получено сделок WS: {ws_status.get('trades_received', 0)}\n"
        f"Закрыто 1m свечей WS: {ws_status.get('candles_closed', 0)}",
        parse_mode="HTML")


@router.message(Command("stats"))
@router.message(F.text == "📊 Статистика стратегий")
async def strategy_stats_handler(message: Message):
    register_user(message.from_user)
    try:
        with db() as con:
            rows = con.execute("""
                SELECT COALESCE(strategy_version, 'B') AS strategy,
                       COUNT(*) AS total,
                       SUM(CASE WHEN UPPER(COALESCE(status,'')) IN ('ACTIVE','WAITING_LEVEL','NEW') THEN 1 ELSE 0 END) AS active,
                       MAX(created_at) AS latest
                FROM signals
                GROUP BY COALESCE(strategy_version, 'B')
                ORDER BY strategy
            """).fetchall()
        if not rows:
            await message.answer("Статистика пока пустая: сигналов ещё нет.")
            return
        labels = {
            "B": "B — исходная многотаймфреймовая",
            STRATEGY_C: "C — пробой и ретест",
            STRATEGY_D: "D — импульс",
            STRATEGY_E: "E — разворот перегрева",
        }
        parts = ["📊 <b>Сигналы по стратегиям</b>", ""]
        for row in rows:
            strategy = str(row["strategy"])
            parts.append(
                f"<b>{html.escape(labels.get(strategy, strategy))}</b>\n"
                f"Всего: <code>{row['total']}</code> | Активных/ожидающих: <code>{row['active'] or 0}</code>\n"
                f"Последний сигнал: {html.escape(str(row['latest'] or '—'))}"
            )
        parts.extend(["", "<i>Это счётчик созданных сигналов, а не доходность. "
                      "Бот пока не фиксирует фактический результат сделок автоматически.</i>"])
        await message.answer("\n".join(parts), parse_mode="HTML")
    except Exception:
        log.exception("Ошибка статистики стратегий")
        await message.answer("Не удалось получить статистику. Подробности записаны в лог.")



@router.message(Command("diag_db"))
async def diag_db_handler(message: Message):
    if message.from_user.id not in ALLOWED_DIAG_USER_IDS:
        await message.answer("⛔ Нет доступа.")
        return
    try:
        report = {}
        with db() as con:
            ic = con.execute("PRAGMA integrity_check").fetchone()
            report["integrity"] = ic[0] if ic else "?"
            fk = con.execute("PRAGMA foreign_key_check").fetchall()
            report["fk_violations"] = len(fk)
            tables = [r["name"] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()]
            counts = {}
            for t in tables:
                try:
                    counts[t] = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                except Exception:
                    counts[t] = "?"
            report["counts"] = counts
            report["signals_active"] = con.execute(
                "SELECT COUNT(*) FROM signals WHERE status IN ('ACTIVE','WAITING_LEVEL')").fetchone()[0]
        # Escape every dynamic value before sending HTML to Telegram.
        # SQLite identifiers are normally trusted, but a custom table name can
        # still contain characters that break Telegram's HTML parser.
        text = (
            "🧪 <b>diag_db</b>\n\n"
            f"integrity: <code>{html.escape(str(report['integrity']))}</code>\n"
            f"fk_violations: <code>{html.escape(str(report['fk_violations']))}</code>\n"
            f"active_signals: <code>{html.escape(str(report['signals_active']))}</code>\n"
            f"counts: <code>{html.escape(str(report['counts']))}</code>"
        )
        await message.answer(text, parse_mode="HTML")
    except Exception:
        log.exception("diag_db error")
        # Do not echo raw exception text into HTML or expose DB internals.
        await message.answer(
            "❌ Не удалось выполнить диагностику базы. Подробности записаны в лог."
        )

# ВАЖНО: этот хендлер должен быть ПОСЛЕДНИМ среди F.text



# ============================================================
# 17. BACKGROUND FUTURES SCANNING
# ============================================================





async def background_scan():
    log.info("Фоновый Futures Scalping скан запущен (интервал %ss)", MARKET_SCAN_INTERVAL_SEC)
    await asyncio.sleep(30)
    while True:
        try:
            with db() as con:
                users_count = con.execute("SELECT COUNT(*) FROM users WHERE is_active=1").fetchone()[0]
            if users_count:
                found = await scan_market(strategy_version=STRATEGY_B)
                for signal in found:
                    await broadcast_signal(signal)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ошибка фонового Futures-скана")
        await asyncio.sleep(MARKET_SCAN_INTERVAL_SEC)



# ============================================================
# 18. MAIN
# ============================================================

async def refresh_contract_specs() -> bool:
    global contract_specs_updated_at
    if http_session is None or http_session.closed:
        return False
    try:
        payload = await api_get(http_session, "/openApi/swap/v2/quote/contracts", {})
        parsed = parse_bingx_contract_specs(payload)
        contract_specs_cache.clear()
        contract_specs_cache.update(parsed)
        contract_specs_updated_at = time.time()
        system_status["contract_specs_count"] = len(parsed)
        system_status["contract_specs_updated_at"] = utc_text()
        log.info("BingX contract rules refreshed: %s symbols", len(parsed))
        return True
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Не удалось обновить ограничения контрактов BingX")
        return False


async def contract_specs_refresh_loop() -> None:
    while True:
        if not contract_specs_updated_at or time.time() - contract_specs_updated_at > CONTRACT_SPECS_MAX_AGE_SEC:
            await asyncio.sleep(300)
            await refresh_contract_specs()
        else:
            await asyncio.sleep(CONTRACT_SPECS_MAX_AGE_SEC)


async def on_startup() -> None:
    global bot, http_session
    http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    bot = Bot(token=BOT_TOKEN)
    await refresh_contract_specs()
    system_status["started"] = True
    log.info("Bot v%s запущен; реальные ордера отключены; WS trade read-only, 1m свечи агрегируются из сделок", BOT_VERSION)

async def on_shutdown() -> None:
    for task in background_tasks:
        if not task.done():
            task.cancel()
    if background_tasks:
        await asyncio.gather(*background_tasks, return_exceptions=True)
    if http_session and not http_session.closed:
        await http_session.close()
    if bot:
        await bot.session.close()
    log.info("Bot остановлен")

async def main_async() -> None:
    global bot
    dp.include_router(router)
    await on_startup()
    background_tasks.append(asyncio.create_task(contract_specs_refresh_loop()))
    background_tasks.append(asyncio.create_task(bingx_kline_websocket_loop()))
    background_tasks.append(asyncio.create_task(background_scan()))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await on_shutdown()


def main() -> None:
    try:
        freeze_source_snapshot()
        init_db()
        ensure_part3_schema()
        asyncio.run(main_async())
    except (KeyboardInterrupt, SystemExit):
        log.info("Завершение по сигналу")
    except Exception:
        log.exception("Фатальная ошибка")
        sys.exit(1)

if __name__ == "__main__":
    main()
