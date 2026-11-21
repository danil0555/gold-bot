============================================================
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
import asyncio
import logging
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
BOT_VERSION = "10.0"

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

RISK_PERCENT = 2.0
FUTURES_LEVERAGE = 5

MAX_SIGNALS_PER_CYCLE = 3

KLINE_LIMIT = 300
MIN_CANDLE_ROWS = 205

MARKET_SCAN_INTERVAL_SEC = 60  # пауза между завершёнными циклами; API-запросы ограничены rate limiter

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
    "TRX-USDT", "DOT-USDT", "BCH-USDT", "SUI-USDT", "TON-USDT",
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
latest_prices: dict[tuple[str, str], float] = {}
latest_quotes: dict[tuple[str, str], dict[str, Any]] = {}
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
            backup_and_verify_db()
        except Exception:
            log.exception("Ошибка бэкапа — продолжаем инициализацию")

    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            created_at TEXT,
            is_active INTEGER DEFAULT 1
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            risk_percent REAL DEFAULT 2.0,
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

                async with session.get(url, params=params,
                                       timeout=aiohttp.ClientTimeout(total=25)) as response:
                    _last_api_request = time.monotonic()
                    raw_text = await response.text()
                    if response.status != 200:
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
                                _api_cooldown_until = time.monotonic() + min(30, 3 * (2 ** attempt))
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

    if len(df) < min_rows:
        raise MarketError(f"Недостаточно закрытых свечей: {len(df)}, требуется {min_rows}")
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

def build_signal_b(market, symbol, frames, quote=None):
    """Строгий многотаймфреймовый сетап для USDT-M Futures scalping.

    Это rule-based фильтр, а не статистическая вероятность выигрыша.
    Сигнал допускается только при согласовании 1H/15m/5m и наличии
    структурного триггера (BOS/CHoCH или sweep).
    """
    if str(market).lower() != "swap":
        return None
    required = ("1m", "5m", "15m", "1h", "4h")
    if any(tf not in frames or frames[tf] is None or len(frames[tf]) < MIN_CANDLE_ROWS for tf in required):
        return None

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
        return None

    atr_pct = atr / price * 100.0
    if atr_pct < MIN_ATR_PCT or atr_pct > MAX_ATR_PCT:
        return None
    if safe_float(m15_now["adx"]) < MIN_SCALPER_ADX:
        return None

    regime = _detect_market_regime(m15_now, m15_prev, atr, price)
    extension_atr = abs(price - safe_float(m15_now["ema20"])) / atr
    if extension_atr > MAX_EXTENSION_ATR or regime == "exhaustion":
        return None

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

    if h1_bull and not h4_bear and m15_bull and m5_bull and m1_bull:
        side = "BUY"
    elif h1_bear and not h4_bull and m15_bear and m5_bear and m1_bear:
        side = "SELL"
    else:
        return None

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
        return None

    vwap = safe_float(m15_now.get("vwap"), price)
    if (side == "BUY" and close15 > vwap) or (side == "SELL" and close15 < vwap):
        score += 1.0; reasons.append("15м: цена на правильной стороне VWAP")
    else:
        return None

    vol_ratio = safe_float(m15_now.get("vol_ratio"), 1.0)
    if vol_ratio < MIN_VOLUME_RATIO:
        return None
    if vol_ratio >= 1.2:
        score += 0.5; reasons.append(f"Объём выше среднего: x{vol_ratio:.2f}")

    score_int = int(round(score))
    if score < MIN_SCALPER_RAW_SCORE:
        return None

    sl_dist = max(1.35 * atr, price * 0.0015)
    if sl_dist > max(3.0 * atr, price * 0.012):
        return None
    tp1_dist = sl_dist * TP1_R
    tp2_dist = sl_dist * TP2_R
    if side == "BUY":
        sl, tp1, tp2 = price - sl_dist, price + tp1_dist, price + tp2_dist
    else:
        sl, tp1, tp2 = price + sl_dist, price - tp1_dist, price - tp2_dist
    if min(sl, tp1, tp2) <= 0:
        return None

    gross_rr = abs(tp2 - price) / abs(price - sl)
    spread_pct = safe_float((quote or {}).get("spread_pct"), DEFAULT_SYNTHETIC_SPREAD_PCT)
    fee_pct = (TAKER_FEE_RATE * 2.0) * 100.0
    slip_pct = DEFAULT_SLIPPAGE_PCT * 2.0
    cost_dist = price * ((spread_pct + fee_pct + slip_pct) / 100.0)
    effective_risk = max(abs(price - sl) + cost_dist * 0.5, 1e-9)
    effective_reward = max(abs(tp2 - price) - cost_dist, 0.0)
    net_rr = effective_reward / effective_risk
    if gross_rr < MIN_RR or net_rr < MIN_NET_RR:
        return None

    quality_score = min(100.0, 60.0 + score * 4.0
                        + (5.0 if net_rr >= 2.0 else 0.0)
                        + (3.0 if vol_ratio >= 1.5 else 0.0)
                        - spread_pct * 20.0)
    quality_score = round(quality_score, 1)
    if quality_score < MIN_QUALITY_SCORE:
        return None

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
            "strategy_version": STRATEGY_B,
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








# ============================================================
# 11. SIGNAL FORMATTING
# ============================================================




# ============================================================
# 12. TRADE CREATE / UPDATE / CLOSE
# ============================================================





# ============================================================
# 13. TEXT FORMATTERS
# ============================================================


def signal_text(signal):
    tp1 = safe_float(signal.get("tp1"), 0)
    tp2 = safe_float(signal.get("tp2"), signal.get("tp", 0))
    live_price = safe_float(signal.get("live_price"), signal["entry"])
    spread = safe_float(signal.get("spread_pct"), 0.0)
    quality = safe_float(signal.get("quality_score"), 0.0)
    reasons = signal.get("reasons", [])
    if not isinstance(reasons, (list, tuple)):
        reasons = [part.strip() for part in str(reasons).split(";") if part.strip()]
    reasons_text = "\n".join(f"• {html.escape(str(item))}" for item in reasons if item) or "—"
    side = "🟢 LONG" if is_long_side(signal["side"]) else "🔴 SHORT"
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
    return (
        "🚨 <b>FUTURES SCALP — СИГНАЛ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>{html.escape(str(signal['symbol']))}</b>  |  <b>{side}</b>\n"
        f"<b>Качество сетапа:</b> <code>{quality:.0f}/100</code>\n"
        f"<b>Рынок:</b> USDT-M Futures  |  <b>ТФ:</b> 1m / 5m / 15m / 1h\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"<b>ВХОД:</b> <code>{format_price(signal['entry'])}</code>\n"
        f"{zone_block}"
        f"<b>SL:</b> <code>{format_price(signal['sl'])}</code>\n"
        f"<b>TP1:</b> <code>{format_price(tp1)}</code>  ({TP1_R}R)\n"
        f"<b>TP2:</b> <code>{format_price(tp2)}</code>  ({TP2_R}R)\n"
        "━━━━━━━━━━━━━━━━━━\n"
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
        [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="🖥 Статус системы")],
    ], resize_keyboard=True)






# ============================================================
# 15. ANALYSIS + SCAN
# ============================================================

async def analyze_market_symbol(market, symbol, strategy_version=DEFAULT_STRATEGY):
    # В новой версии анализируем только USDT-M Futures и только стратегию B.
    market = "swap"
    key = (market, symbol)
    if key in _unavailable_symbols:
        return None
    now_ts = time.monotonic()
    if key in _insufficient_history and now_ts < _insufficient_history[key]:
        return None

    candles = {}
    for interval in ("1m", "5m", "15m", "1h", "4h"):
        try:
            data = await get_klines(market, symbol, interval, KLINE_LIMIT)
        except MarketError as exc:
            message = str(exc).lower()
            if ("100204" in str(exc) or "109425" in str(exc)
                    or "symbol is not found" in message or "not exist" in message):
                _unavailable_symbols.add(key)
                log.warning("Фьючерсный контракт %s недоступен", symbol)
                return None
            if "недостаточно закрытых свечей" in message:
                _insufficient_history[key] = now_ts + 6 * 3600
                return None
            raise
        if data is None or len(data) < MIN_CANDLE_ROWS:
            return None
        candles[interval] = data
        try:
            save_closed_candles(market, symbol, interval, data)
        except Exception:
            log.exception("Не сохранились свечи %s %s", symbol, interval)

    signal = build_signal_b(market, symbol, candles, quote=None)
    if not signal:
        return None
    snapshot = await fetch_live_market_snapshot(market, symbol)
    if not snapshot:
        return None
    return enrich_signal_with_live_data(signal, snapshot)


async def scan_market(strategy_version=DEFAULT_STRATEGY):
    async with _scan_lock:
        found = []
        system_status["scan_count"] = int(system_status.get("scan_count", 0)) + 1
        system_status["last_scan"] = utc_text()
        expire_stale_signals()

        for symbol in SYMBOLS:
            key = ("swap", symbol)
            if key in _unavailable_symbols:
                continue
            try:
                signal = await analyze_market_symbol("swap", symbol, STRATEGY_B)
                if not signal:
                    continue
                signal_id = save_signal(signal)
                if signal_id:
                    signal["id"] = signal_id
                    found.append(signal)
                    if len(found) >= MAX_SIGNALS_PER_CYCLE:
                        break
            except MarketError as exc:
                message = str(exc)
                if "100410" in message or "100429" in message:
                    log.debug("Rate limit %s: %s", symbol, message)
                else:
                    log.warning("Ошибка сканирования %s: %s", symbol, exc)
            except Exception:
                log.exception("Ошибка сканирования %s", symbol)

        system_status["signals_count"] = int(system_status.get("signals_count", 0)) + len(found)
        return found


async def broadcast_signal(signal):
    with db() as con:
        users = con.execute("SELECT user_id FROM users WHERE is_active=1").fetchall()
    for row in users:
        try:
            await bot.send_message(row["user_id"], signal_text(signal), parse_mode="HTML")
        except Exception:
            log.exception("Не отправлен сигнал пользователю %s", row["user_id"])



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
        "Если подходящего сетапа нет — бот ничего не отправляет.",
        reply_markup=main_keyboard(), parse_mode="HTML")


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
            await message.answer(signal_text(signal), parse_mode="HTML")
    except Exception:
        log.exception("Ошибка ручного сканирования")
        await status.edit_text("❌ Не удалось завершить анализ. Подробности записаны в лог.")














@router.message(F.text == "⚙️ Настройки")
async def settings_handler(message: Message):
    register_user(message.from_user)
    await message.answer(
        "⚙️ <b>Параметры стратегии</b>\n\n"
        "Рынок: <b>USDT-M Futures</b>\n"
        "Стратегия: <b>строгий Scalping B</b>\n"
        f"Расчётный риск: <b>{RISK_PERCENT:.1f}%</b>\n"
        f"Плечо для расчётов: <b>{FUTURES_LEVERAGE}x</b>\n"
        f"TP1: <b>{TP1_R}R</b>  |  TP2: <b>{TP2_R}R</b>\n"
        f"Минимальное качество: <b>{MIN_QUALITY_SCORE}/100</b>\n"
        f"Минимальный Net RR: <b>{MIN_NET_RR:.2f}</b>\n"
        f"Цикл сканирования: примерно каждые {MARKET_SCAN_INTERVAL_SEC // 60} мин после завершения предыдущего цикла.\n\n"
        "Риск и плечо показываются как ориентиры для ручного расчёта. "
        "Бот не размещает ордера и не управляет открытыми позициями.",
        parse_mode="HTML")





@router.message(F.text == "🖥 Статус системы")
async def system_status_handler(message: Message):
    register_user(message.from_user)
    await message.answer(
        f"🖥 <b>Trading Bot v{BOT_VERSION}</b>\n\n"
        "Режим: <b>USDT-M Futures Scalping</b>\n"
        "Стратегия: <b>B — строгий фильтр</b>\n"
        "Реальные ордера: <b>отключены</b>\n"
        "Сопровождение сделок: <b>удалено</b>\n"
        f"API BingX: {html.escape(str(system_status.get('api_status', '—')))}\n"
        f"Сканов: {system_status.get('scan_count', 0)}\n"
        f"Отправлено сигналов: {system_status.get('signals_count', 0)}\n"
        f"Последний скан: {system_status.get('last_scan') or '—'}\n"
        f"Недоступных контрактов: {len(_unavailable_symbols)}",
        parse_mode="HTML")


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
        text = (
            f"🧪 <b>diag_db</b>\n\n"
            f"integrity: {report['integrity']}\n"
            f"fk_violations: {report['fk_violations']}\n"
            f"active_signals: {report['signals_active']}\n"
            f"counts: {report['counts']}"
        )
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        log.exception("diag_db error")
        await message.answer(f"❌ Ошибка: {e}")

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
        except Exception:
            log.exception("Ошибка фонового Futures-скана")
        await asyncio.sleep(MARKET_SCAN_INTERVAL_SEC)



# ============================================================
# 18. MAIN
# ============================================================

async def on_startup() -> None:
    global bot, http_session
    http_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    bot = Bot(token=BOT_TOKEN)
    system_status["started"] = True
    log.info("Bot v%s запущен", BOT_VERSION)

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
