# ============================================================
# TRADING BOT v9.3 (SINGLE-FILE, PYTHON 3.13 + AIOGRAM 3 + SQLITE)
# Strategy A (v9.2 baseline, unchanged) + Strategy B (scalper)
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
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from aiogram.enums import ParseMode


# ============================================================
# 1. ENVIRONMENT
# ============================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Не найден BOT_TOKEN. Добавь токен в файл .env")

BOT_NAME = "Trading Bot"
BOT_VERSION = "9.3"

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

MARKETS = ("spot", "swap")
TIMEFRAMES = ("5m", "15m", "1h", "4h")

TF_MS = {
    "1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
    "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

MIN_SCORE = 6
MIN_RR = 1.0

TP1_R = 1.8
TP2_R = 3.0

TP1_CLOSE_PERCENT = 70.0
TP2_CLOSE_PERCENT = 30.0

RISK_PERCENT = 2.0
FUTURES_LEVERAGE = 5

MAX_ACTIVE_TRADES = 5
MAX_SIGNALS_PER_CYCLE = 3

KLINE_LIMIT = 300
MIN_CANDLE_ROWS = 205

MARKET_SCAN_INTERVAL_SEC = int(os.getenv("MARKET_SCAN_INTERVAL_SEC", "1800"))
TRADE_MONITOR_INTERVAL_SEC = max(5, int(os.getenv("TRADE_MONITOR_INTERVAL_SEC", "15")))

SIGNAL_COOLDOWN_MINUTES = 45
SIGNAL_TTL_MINUTES = max(1, int(os.getenv("SIGNAL_TTL_MINUTES", "20")))
MAX_ENTRY_DRIFT_R = 0.5
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "0.25"))

MAKER_FEE_RATE = float(os.getenv("MAKER_FEE_RATE", "0.0002"))
TAKER_FEE_RATE = float(os.getenv("TAKER_FEE_RATE", "0.0005"))
DEFAULT_SYNTHETIC_SPREAD_PCT = float(os.getenv("DEFAULT_SYNTHETIC_SPREAD_PCT", "0.04"))
DEFAULT_SLIPPAGE_PCT = float(os.getenv("DEFAULT_SLIPPAGE_PCT", "0.03"))

STRATEGY_A = "A"
STRATEGY_B = "B"
DEFAULT_STRATEGY = STRATEGY_A

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
user_states: dict[int, dict[str, Any]] = {}
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
    return {"spot": "SPOT", "swap": "USDT-M Futures"}.get(str(market).lower(), str(market).upper())

def side_name(side: str) -> str:
    return {
        "BUY": "LONG / BUY", "LONG": "LONG / BUY",
        "SELL": "SHORT / SELL", "SHORT": "SHORT / SELL",
    }.get(str(side).upper(), str(side))

def is_long_side(side: str) -> bool:
    return str(side).strip().lower() in ("buy", "long")

def normalize_trade_status(status: Any) -> str:
    st = str(status or "active").strip().lower()
    if st in ("active", "open"):
        return "active"
    if st == "closed":
        return "closed"
    return st

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
            strategy_version TEXT DEFAULT 'A',
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS user_balances (
            user_id INTEGER NOT NULL,
            market TEXT NOT NULL,
            balance REAL NOT NULL DEFAULT 0,
            updated_at TEXT,
            PRIMARY KEY (user_id, market),
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT, updated_at TEXT,
            market TEXT, symbol TEXT, side TEXT,
            entry REAL, sl REAL, tp REAL, rr REAL,
            score INTEGER, reasons TEXT, setup_key TEXT,
            status TEXT DEFAULT 'ACTIVE', result TEXT,
            strategy_version TEXT DEFAULT 'A',
            live_price REAL, bid REAL, ask REAL,
            spread_pct REAL, price_updated_at TEXT, expires_at TEXT,
            entry_mode TEXT DEFAULT 'market',
            entry_zone_low REAL, entry_zone_high REAL,
            confirmation_rule TEXT, quality_score REAL,
            market_regime TEXT, estimated_fee_pct REAL,
            estimated_slippage_pct REAL, net_rr REAL,
            cancel_reason TEXT
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, signal_id INTEGER,
            market TEXT, symbol TEXT, side TEXT,
            entry REAL, actual_entry REAL, signal_entry REAL,
            entry_drift_r REAL DEFAULT 0, current_price REAL,
            sl REAL, tp1 REAL, tp2 REAL, initial_sl REAL,
            risk_amount REAL, initial_risk_amount REAL,
            current_risk_amount REAL, current_risk_r REAL,
            position_size REAL DEFAULT 0, notional REAL DEFAULT 0,
            margin_required REAL DEFAULT 0, balance_snapshot REAL DEFAULT 0,
            remaining_percent REAL DEFAULT 100,
            tp1_hit INTEGER DEFAULT 0, tp2_hit INTEGER DEFAULT 0,
            tp1_fill_price REAL, tp1_fill_qty REAL, tp1_fill_fee REAL DEFAULT 0,
            tp1_closed_at TEXT, tp2_fill_price REAL, tp2_fill_qty REAL,
            tp2_fill_fee REAL DEFAULT 0, tp2_closed_at TEXT,
            total_fees_paid REAL DEFAULT 0, total_slippage_cost REAL DEFAULT 0,
            strategy_version TEXT DEFAULT 'A',
            status TEXT DEFAULT 'active', last_recommendation TEXT,
            opened_at TEXT, closed_at TEXT, updated_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id),
            FOREIGN KEY(signal_id) REFERENCES signals(id)
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS trade_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            event_type TEXT NOT NULL, message TEXT, price REAL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(trade_id) REFERENCES trades(id),
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS trade_executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
            exec_type TEXT NOT NULL, price REAL NOT NULL,
            quantity REAL NOT NULL, share_percent REAL NOT NULL,
            fee_usdt REAL DEFAULT 0, slippage_usdt REAL DEFAULT 0,
            realized_pnl_usdt REAL DEFAULT 0, executed_at TEXT NOT NULL,
            FOREIGN KEY(trade_id) REFERENCES trades(id),
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS trade_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_id INTEGER, user_id INTEGER NOT NULL,
            market TEXT, symbol TEXT, side TEXT,
            entry REAL, exit_price REAL, result TEXT,
            pnl_percent REAL DEFAULT 0, pnl_r REAL DEFAULT 0,
            net_pnl_usdt REAL DEFAULT 0, initial_risk_amount REAL DEFAULT 0,
            total_fees_paid REAL DEFAULT 0, total_slippage_cost REAL DEFAULT 0,
            strategy_version TEXT DEFAULT 'A', close_reason TEXT,
            opened_at TEXT, closed_at TEXT,
            FOREIGN KEY(trade_id) REFERENCES trades(id),
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS signal_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id INTEGER NOT NULL, event_type TEXT NOT NULL,
            old_status TEXT, new_status TEXT, price REAL,
            reason TEXT, created_at TEXT NOT NULL,
            FOREIGN KEY(signal_id) REFERENCES signals(id)
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS user_dialogs (
            user_id INTEGER PRIMARY KEY,
            state_type TEXT NOT NULL, ref_id INTEGER,
            created_at TEXT NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
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
        con.execute("CREATE INDEX IF NOT EXISTS idx_trades_user_status ON trades(user_id, status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_trade_events_trade ON trade_events(trade_id, created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_history_user ON trade_history(user_id, closed_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_executions_trade ON trade_executions(trade_id, executed_at)")

    log.info("База данных готова: %s", DB_PATH)

def ensure_part3_schema() -> None:
    """Миграция старой user_balances (без market) в новую (PK user_id+market)."""
    with db() as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='user_balances'"
        ).fetchone()
        if not row:
            return
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(user_balances)").fetchall()}
        if "market" in existing:
            return
        log.info("Миграция user_balances: старая схема → новая")
        conn.execute("ALTER TABLE user_balances RENAME TO user_balances_old")
        conn.execute("""CREATE TABLE user_balances (
            user_id INTEGER NOT NULL, market TEXT NOT NULL,
            balance REAL NOT NULL DEFAULT 0, updated_at TEXT,
            PRIMARY KEY (user_id, market)
        )""")
        if "balance" in existing:
            conn.execute("""INSERT INTO user_balances (user_id, market, balance, updated_at)
                SELECT user_id, 'spot', COALESCE(balance, 0), COALESCE(updated_at, ?)
                FROM user_balances_old""", (utc_text(),))
        conn.execute("DROP TABLE user_balances_old")


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

    if market == "spot":
        path = "/openApi/spot/v2/market/kline"
    elif market == "swap":
        path = "/openApi/swap/v3/quote/klines"
    else:
        raise MarketError(f"Неизвестный рынок: {market}")

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

def build_signal_a_v92(market: str, symbol: str, frames: dict[str, pd.DataFrame]) -> dict[str, Any] | None:
    """Стратегия A — эталон v9.2, БЕЗ ИЗМЕНЕНИЙ."""
    required = ("5m", "15m", "1h", "4h")
    if any(tf not in frames or frames[tf] is None or len(frames[tf]) < MIN_CANDLE_ROWS for tf in required):
        return None
    m5, m15, h1, h4 = (indicators(frames[tf]) for tf in required)
    a, b, c, d = m5.iloc[-1], m15.iloc[-1], h1.iloc[-1], h4.iloc[-1]
    price = safe_float(b["close"]); atr = safe_float(b["atr"])
    if price <= 0 or atr <= 0:
        return None
    ls = ss = 0
    lr, sr = [], []
    for row, label in ((c, "1H"), (d, "4H")):
        if row["close"] > row["ema200"] and row["ema20"] > row["ema50"]:
            ls += 2; lr.append(f"Тренд {label}: выше EMA200")
        if row["close"] < row["ema200"] and row["ema20"] < row["ema50"]:
            ss += 2; sr.append(f"Тренд {label}: ниже EMA200")
    if b["ema20"] > b["ema50"] and b["close"] > b["ema20"]:
        ls += 2; lr.append("15м: EMA20 выше EMA50")
    if b["ema20"] < b["ema50"] and b["close"] < b["ema20"]:
        ss += 2; sr.append("15м: EMA20 ниже EMA50")
    if 48 <= b["rsi"] <= 68:
        ls += 1; lr.append(f"RSI 15м {b['rsi']:.1f}")
    if 32 <= b["rsi"] <= 52:
        ss += 1; sr.append(f"RSI 15м {b['rsi']:.1f}")
    if b["macd"] > b["macd_signal"]:
        ls += 1; lr.append("MACD 15м положительный")
    if b["macd"] < b["macd_signal"]:
        ss += 1; sr.append("MACD 15м отрицательный")
    if b["adx"] >= 18 and b["plus_di"] > b["minus_di"]:
        ls += 1; lr.append(f"ADX {b['adx']:.1f}, покупатели сильнее")
    if b["adx"] >= 18 and b["minus_di"] > b["plus_di"]:
        ss += 1; sr.append(f"ADX {b['adx']:.1f}, продавцы сильнее")
    if a["close"] > a["ema20"]:
        ls += 1; lr.append("5м: цена выше EMA20")
    if a["close"] < a["ema20"]:
        ss += 1; sr.append("5м: цена ниже EMA20")
    if ls > ss:
        side, score, reasons = "BUY", ls, lr
    else:
        side, score, reasons = "SELL", ss, sr
    if score < MIN_SCORE or abs(ls - ss) < 2:
        return None
    if market == "spot" and side == "SELL":
        return None
    sl_dist = max(1.5 * atr, price * 0.002)
    tp1_dist = sl_dist * TP1_R; tp2_dist = sl_dist * TP2_R
    if side == "BUY":
        sl, tp1, tp2 = price - sl_dist, price + tp1_dist, price + tp2_dist
    else:
        sl, tp1, tp2 = price + sl_dist, price - tp1_dist, price - tp2_dist
    if min(sl, tp1, tp2) <= 0:
        return None
    rr = abs(tp2 - price) / abs(price - sl)
    if rr < MIN_RR:
        return None
    return {
        "strategy_version": STRATEGY_A, "market": market, "symbol": symbol, "side": side,
        "entry": price, "sl": sl, "tp1": tp1, "tp2": tp2, "tp": tp2, "rr": rr,
        "score": score, "reasons": reasons, "atr": atr,
        "setup_key": f"{market}:{symbol}:{side}:{int(b['open_time'])}",
    }

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
    required = ("5m", "15m", "1h", "4h")
    if any(tf not in frames or frames[tf] is None or len(frames[tf]) < MIN_CANDLE_ROWS for tf in required):
        return None
    m5 = enrich_scalper_series(frames["5m"])
    m15 = enrich_scalper_series(frames["15m"])
    h1 = enrich_scalper_series(frames["1h"])
    h4 = enrich_scalper_series(frames["4h"])
    a, b, b_prev = m5.iloc[-1], m15.iloc[-1], m15.iloc[-2]
    c, d = h1.iloc[-1], h4.iloc[-1]
    price = safe_float(b["close"]); atr = safe_float(b["atr"])
    if price <= 0 or atr <= 0:
        return None
    regime = _detect_market_regime(b, b_prev, atr, price)
    extension_atr = abs(price - safe_float(b["ema20"])) / atr
    if extension_atr > 3.2 and regime == "impulse":
        return None
    ls = ss = 0.0
    lr, sr = [], []
    for row, label in ((c, "1H"), (d, "4H")):
        if row["close"] > row["ema200"] and row["ema20"] > row["ema50"]:
            ls += 2.0; lr.append(f"Контекст {label}: бычий тренд выше EMA200")
        if row["close"] < row["ema200"] and row["ema20"] < row["ema50"]:
            ss += 2.0; sr.append(f"Контекст {label}: медвежий тренд ниже EMA200")
    prev_sh = safe_float(b_prev.get("swing_high_prev"))
    prev_sl = safe_float(b_prev.get("swing_low_prev"))
    b_close = safe_float(b["close"]); b_high = safe_float(b["high"]); b_low = safe_float(b["low"])
    if prev_sh > 0 and b_close > prev_sh:
        if safe_float(b["ema20"]) >= safe_float(b["ema50"]):
            ls += 1.5; lr.append("15м BOS вверх")
        else:
            ls += 1.0; lr.append("15м CHoCH вверх")
    if prev_sl > 0 and b_close < prev_sl:
        if safe_float(b["ema20"]) <= safe_float(b["ema50"]):
            ss += 1.5; sr.append("15м BOS вниз")
        else:
            ss += 1.0; sr.append("15м CHoCH вниз")
    rng_high = safe_float(b.get("range_high_20"))
    rng_low = safe_float(b.get("range_low_20"))
    if rng_low > 0 and b_low < rng_low and b_close > rng_low:
        ls += 1.5; lr.append("Sweep вниз + возврат")
    if rng_high > 0 and b_high > rng_high and b_close < rng_high:
        ss += 1.5; sr.append("Sweep вверх + возврат")
    vwap_15 = safe_float(b.get("vwap"), price)
    if b_close > vwap_15:
        ls += 1.0; lr.append("15м: выше VWAP")
    elif b_close < vwap_15:
        ss += 1.0; sr.append("15м: ниже VWAP")
    vol_ratio = safe_float(b.get("vol_ratio"), 1.0)
    if vol_ratio >= 1.4:
        if b_close > safe_float(b["open"]):
            ls += 1.0; lr.append(f"Объём x{vol_ratio:.1f} на бычьем баре")
        elif b_close < safe_float(b["open"]):
            ss += 1.0; sr.append(f"Объём x{vol_ratio:.1f} на медвежьем баре")
    if safe_float(a["close"]) > safe_float(a["ema20"]):
        ls += 1.0; lr.append("5м: выше EMA20")
    if safe_float(a["close"]) < safe_float(a["ema20"]):
        ss += 1.0; sr.append("5м: ниже EMA20")
    if ls > ss:
        side, raw_score, reasons = "BUY", ls, lr
    else:
        side, raw_score, reasons = "SELL", ss, sr
    score_int = int(round(raw_score))
    if score_int < MIN_SCORE or abs(ls - ss) < 1.5:
        return None
    if market == "spot" and side == "SELL":
        return None
    sl_dist = max(1.5 * atr, price * 0.0025)
    min_sl_dist = price * 0.002
    max_sl_dist = max(3.5 * atr, price * 0.025)
    if sl_dist < min_sl_dist or sl_dist > max_sl_dist:
        return None
    tp1_dist = sl_dist * TP1_R; tp2_dist = sl_dist * TP2_R
    if side == "BUY":
        sl, tp1, tp2 = price - sl_dist, price + tp1_dist, price + tp2_dist
        entry_mode = "level" if (extension_atr > 1.8 or regime == "exhaustion") else "market"
        zone_low = max(sl * 1.002, min(price, vwap_15)); zone_high = price
        conf = "Дождаться удержания зоны и подтверждения на 5м/15м."
    else:
        sl, tp1, tp2 = price + sl_dist, price - tp1_dist, price - tp2_dist
        entry_mode = "level" if (extension_atr > 1.8 or regime == "exhaustion") else "market"
        zone_low = price; zone_high = min(sl * 0.998, max(price, vwap_15))
        conf = "Дождаться реакции продавцов на 5м/15м."
    if min(sl, tp1, tp2) <= 0:
        return None
    gross_rr = abs(tp2 - price) / abs(price - sl)
    if gross_rr < MIN_RR:
        return None
    spread_pct = safe_float((quote or {}).get("spread_pct"), DEFAULT_SYNTHETIC_SPREAD_PCT)
    fee_rate = MAKER_FEE_RATE if entry_mode == "level" else TAKER_FEE_RATE
    fee_pct = (fee_rate * 2.0) * 100.0
    slip_pct = DEFAULT_SLIPPAGE_PCT * 2.0
    cost_dist = price * ((spread_pct + fee_pct + slip_pct) / 100.0)
    eff_risk_dist = max(abs(price - sl) + cost_dist * 0.5, 1e-9)
    eff_reward_dist = max(abs(tp2 - price) - cost_dist, 0.0)
    net_rr = eff_reward_dist / eff_risk_dist
    if net_rr < MIN_RR:
        return None
    quality_score = round(min(100.0, raw_score * 11.0 + (10.0 if net_rr >= 2.0 else 0.0) - spread_pct * 20.0), 1)
    return {
        "strategy_version": STRATEGY_B, "market": market, "symbol": symbol, "side": side,
        "entry": price, "sl": sl, "tp1": tp1, "tp2": tp2, "tp": tp2,
        "rr": gross_rr, "net_rr": net_rr, "score": score_int,
        "quality_score": quality_score, "market_regime": regime,
        "entry_mode": entry_mode,
        "entry_zone_low": min(zone_low, zone_high),
        "entry_zone_high": max(zone_low, zone_high),
        "confirmation_rule": conf if entry_mode == "level" else "Рыночный вход с проверкой отклонения <= 0.5R.",
        "spread_pct": spread_pct, "estimated_fee_pct": fee_pct,
        "estimated_slippage_pct": slip_pct, "reasons": reasons, "atr": atr,
        "setup_key": f"B:{market}:{symbol}:{side}:{int(b['open_time'])}",
    }


# ============================================================
# 9. LIVE QUOTE + SIGNAL STORAGE
# ============================================================

async def get_live_price(market: str, symbol: str) -> float | None:
    if http_session is None or http_session.closed:
        return None
    if market == "spot":
        endpoint = "/openApi/spot/v1/ticker/price"
        quote_endpoint = "/openApi/spot/v1/ticker/bookTicker"
    else:
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
    enriched.setdefault("strategy_version", STRATEGY_A)
    enriched.setdefault("entry_mode", "market")
    enriched.setdefault("market_regime", "standard")
    enriched.setdefault("quality_score", float(enriched.get("score", 0)) * 10.0)
    enriched.setdefault("estimated_fee_pct", (TAKER_FEE_RATE * 2.0) * 100.0)
    enriched.setdefault("estimated_slippage_pct", DEFAULT_SLIPPAGE_PCT * 2.0)
    enriched.setdefault("net_rr", safe_float(enriched.get("rr"), 0.0))
    enriched["price_updated_at"] = utc_text(now_dt)
    enriched["expires_at"] = utc_text(now_dt + timedelta(minutes=SIGNAL_TTL_MINUTES))

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

        if drift_r > MAX_ENTRY_DRIFT_R * 2.0 and enriched.get("entry_mode") == "market":
            enriched["status"] = "CANCELLED"
            enriched["cancel_reason"] = f"Цена ушла от сигнала ({drift_r:.2f}R)"
            log.info("Сигнал отменён (дрифт %.2fR): %s %s", drift_r, sig.get("market"), sig.get("symbol"))
            return enriched

        if enriched.get("entry_mode") == "market" and risk_dist > 0:
            exec_entry = ask if is_long_side(enriched["side"]) else bid
            if exec_entry > 0:
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
                gross = abs(enriched["tp2"] - exec_entry) / abs(exec_entry - enriched["sl"])
                enriched["rr"] = gross
                fee_pct = safe_float(enriched.get("estimated_fee_pct"), 0.0)
                slip_pct = safe_float(enriched.get("estimated_slippage_pct"), 0.0)
                cost_dist = exec_entry * ((spread_pct + fee_pct + slip_pct) / 100.0)
                eff_risk = max(abs(exec_entry - enriched["sl"]) + cost_dist * 0.5, 1e-9)
                eff_rew = max(abs(enriched["tp2"] - exec_entry) - cost_dist, 0.0)
                enriched["net_rr"] = eff_rew / eff_risk
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
            (sig["market"], sig["symbol"], sig["side"], sig.get("strategy_version", STRATEGY_A))).fetchone()
        if recent:
            return None

        values = {
            "created_at": now, "updated_at": now,
            "market": sig["market"], "symbol": sig["symbol"], "side": sig["side"],
            "entry": sig["entry"], "sl": sig["sl"], "tp": sig["tp2"],
            "rr": sig["rr"], "score": sig["score"], "reasons": reasons_text,
            "setup_key": sig["setup_key"], "status": sig.get("status", "ACTIVE"),
            "strategy_version": sig.get("strategy_version", STRATEGY_A),
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
# 10. USER STATES + DB HELPERS
# ============================================================

def set_user_dialog_state(user_id: int, state_type: str, ref_id: int | None = None) -> None:
    user_states[user_id] = {"type": state_type, "ref_id": ref_id,
                            "signal_id": ref_id, "trade_id": ref_id}
    with db() as con:
        con.execute("""INSERT INTO user_dialogs (user_id, state_type, ref_id, created_at)
            VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET
                state_type=excluded.state_type, ref_id=excluded.ref_id,
                created_at=excluded.created_at""",
            (user_id, state_type, ref_id, utc_text()))

def clear_user_dialog_state(user_id: int) -> None:
    user_states.pop(user_id, None)
    with db() as con:
        con.execute("DELETE FROM user_dialogs WHERE user_id=?", (user_id,))

def get_user_dialog_state(user_id: int) -> dict[str, Any] | None:
    if user_id in user_states:
        return user_states[user_id]
    with db() as con:
        row = con.execute("SELECT state_type, ref_id FROM user_dialogs WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return None
    st = {"type": row["state_type"], "ref_id": row["ref_id"],
          "signal_id": row["ref_id"], "trade_id": row["ref_id"]}
    user_states[user_id] = st
    return st

def get_user_strategy(user_id: int) -> str:
    with db() as con:
        row = con.execute("SELECT strategy_version FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return DEFAULT_STRATEGY
    ver = str(row["strategy_version"] or DEFAULT_STRATEGY).upper()
    return ver if ver in (STRATEGY_A, STRATEGY_B) else DEFAULT_STRATEGY

def set_user_strategy(user_id: int, version: str) -> str:
    ver = str(version).upper()
    if ver not in (STRATEGY_A, STRATEGY_B):
        raise ValueError("Неизвестная стратегия")
    with db() as con:
        con.execute("""INSERT INTO user_settings (user_id, risk_percent, leverage, notifications, strategy_version)
            VALUES (?, ?, ?, 1, ?) ON CONFLICT(user_id) DO UPDATE SET strategy_version=excluded.strategy_version""",
            (user_id, RISK_PERCENT, FUTURES_LEVERAGE, ver))
    return ver

def row_to_dict(row):
    if row is None:
        return None
    try:
        return dict(row)
    except (TypeError, ValueError):
        return None

def register_user(user):
    if user is None:
        return
    with db() as con:
        con.execute("INSERT OR IGNORE INTO users (user_id, username, created_at, is_active) VALUES (?, ?, ?, 1)",
                    (user.id, user.username or "", utc_text()))
        con.execute("""INSERT OR IGNORE INTO user_settings
            (user_id, risk_percent, leverage, notifications, strategy_version) VALUES (?, ?, ?, 1, ?)""",
            (user.id, RISK_PERCENT, FUTURES_LEVERAGE, DEFAULT_STRATEGY))

def set_user_balance(user_id, market, balance):
    market = str(market).lower()
    if market not in ("spot", "swap"):
        raise ValueError("Неизвестный рынок")
    balance = safe_float(balance, 0)
    if balance <= 0:
        raise ValueError("Баланс должен быть > 0")
    with db() as con:
        con.execute("""INSERT INTO user_balances (user_id, market, balance, updated_at)
            VALUES (?, ?, ?, ?) ON CONFLICT(user_id, market) DO UPDATE SET
                balance=excluded.balance, updated_at=excluded.updated_at""",
            (user_id, market, balance, utc_text()))

def get_user_balance(user_id, market):
    with db() as con:
        row = con.execute("SELECT balance FROM user_balances WHERE user_id=? AND market=?",
                          (user_id, str(market).lower())).fetchone()
    return safe_float(row["balance"], 0.0) if row else 0.0

def get_active_trades(user_id):
    with db() as con:
        rows = con.execute("""SELECT * FROM trades
            WHERE user_id=? AND LOWER(COALESCE(status, '')) IN ('active', 'open')
            ORDER BY opened_at DESC, id DESC""", (user_id,)).fetchall()
    return [row_to_dict(r) for r in rows]

def get_trade(trade_id, user_id=None):
    with db() as con:
        if user_id is None:
            row = con.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        else:
            row = con.execute("SELECT * FROM trades WHERE id=? AND user_id=?", (trade_id, user_id)).fetchone()
    return row_to_dict(row)

def save_trade_event(trade_id, user_id, event_type, message="", price=None):
    with db() as con:
        con.execute("""INSERT INTO trade_events
            (trade_id, user_id, event_type, message, price, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (trade_id, user_id, event_type, str(message),
             safe_float(price, 0.0) if price is not None else None, utc_text()))

def save_trade_execution(trade_id, user_id, exec_type, price, quantity,
                         share_percent, fee_usdt=0.0, slippage_usdt=0.0,
                         realized_pnl_usdt=0.0, executed_at=None):
    with db() as con:
        con.execute("""INSERT INTO trade_executions
            (trade_id, user_id, exec_type, price, quantity, share_percent,
             fee_usdt, slippage_usdt, realized_pnl_usdt, executed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (trade_id, user_id, exec_type, safe_float(price), safe_float(quantity),
             safe_float(share_percent), safe_float(fee_usdt), safe_float(slippage_usdt),
             safe_float(realized_pnl_usdt), executed_at or utc_text()))


# ============================================================
# 11. POSITION SIZE + DRIFT
# ============================================================

def calculate_position_size(balance, entry, stop_loss, market):
    balance = safe_float(balance, 0); entry = safe_float(entry, 0)
    stop_loss = safe_float(stop_loss, 0); market = str(market).lower()
    if balance <= 0 or entry <= 0 or stop_loss <= 0:
        raise ValueError("Некорректные баланс/цены")
    distance = abs(entry - stop_loss)
    if distance <= 0:
        raise ValueError("Вход = SL")
    risk_target = balance * (RISK_PERCENT / 100)
    quantity = risk_target / distance
    notional = quantity * entry
    max_notional = balance if market == "spot" else balance * FUTURES_LEVERAGE
    if notional > max_notional:
        notional = max_notional
        quantity = notional / entry
    actual_risk = quantity * distance
    margin_required = notional / FUTURES_LEVERAGE if market == "swap" else notional
    return {
        "quantity": quantity, "notional": notional,
        "margin_required": margin_required, "risk_amount": actual_risk,
        "risk_target": risk_target,
        "risk_percent": (actual_risk / balance * 100) if balance > 0 else 0,
    }

def assess_entry_drift(signal_entry, signal_sl, tp2, actual_entry):
    one_r = abs(signal_entry - signal_sl)
    drift_abs = abs(actual_entry - signal_entry)
    drift_r = (drift_abs / one_r) if one_r > 0 else 0.0
    new_rr = (abs(tp2 - actual_entry) / one_r) if one_r > 0 else 0.0
    exceeds = drift_r > MAX_ENTRY_DRIFT_R
    unfavorable = exceeds and (new_rr < MIN_RR or drift_r > 1.0)
    return {"drift_r": drift_r, "exceeds_0_5r": exceeds,
            "unfavorable": unfavorable, "recalculated_rr": new_rr}


# ============================================================
# 12. TRADE CREATE / UPDATE / CLOSE
# ============================================================

def create_personal_trade(user_id, signal_id, actual_entry):
    actual_entry = safe_float(actual_entry, 0)
    if actual_entry <= 0:
        return None, "Цена входа должна быть > 0.", None

    with db() as conn:
        signal_row = conn.execute("SELECT * FROM signals WHERE id=?", (signal_id,)).fetchone()
        if not signal_row:
            return None, "Сигнал не найден.", None
        signal = row_to_dict(signal_row)
        sig_status = str(signal.get("status") or "").upper()
        if sig_status in ("EXPIRED", "CANCELLED"):
            return None, f"Сигнал неактуален ({sig_status}).", None
        if sig_status == "ENTERED":
            return None, "По этому сигналу уже открыта сделка.", None

        active_count = conn.execute("""SELECT COUNT(*) FROM trades
            WHERE user_id=? AND LOWER(COALESCE(status, '')) IN ('active', 'open')""",
            (user_id,)).fetchone()[0]
        if active_count >= MAX_ACTIVE_TRADES:
            return None, f"Лимит {MAX_ACTIVE_TRADES} активных сделок.", None

        market = str(signal["market"]).lower()
        side = str(signal["side"]).lower()
        balance_row = conn.execute("SELECT balance FROM user_balances WHERE user_id=? AND market=?",
                                   (user_id, market)).fetchone()
        if not balance_row:
            return None, "Сначала укажи баланс в «Настройках».", None
        balance = safe_float(balance_row["balance"], 0)
        if balance <= 0:
            return None, "Некорректный баланс.", None

        signal_entry = safe_float(signal["entry"], 0)
        signal_sl = safe_float(signal["sl"], 0)
        if signal_entry <= 0 or signal_sl <= 0:
            return None, "В сигнале нет корректных цен.", None

        stop_distance = abs(signal_entry - signal_sl)
        if stop_distance <= 0:
            return None, "Дистанция стопа = 0.", None

        if is_long_side(side):
            actual_sl = actual_entry - stop_distance
            tp1 = actual_entry + stop_distance * TP1_R
            tp2 = actual_entry + stop_distance * TP2_R
        else:
            actual_sl = actual_entry + stop_distance
            tp1 = actual_entry - stop_distance * TP1_R
            tp2 = actual_entry - stop_distance * TP2_R
        if min(actual_sl, tp1, tp2) <= 0:
            return None, "SL/TP в некорректной зоне.", None

        drift_info = assess_entry_drift(signal_entry, signal_sl, tp2, actual_entry)
        sizing = calculate_position_size(balance, actual_entry, actual_sl, market)
        entry_fee = sizing["notional"] * TAKER_FEE_RATE
        now = utc_text()

        cursor = conn.execute("""INSERT INTO trades (
            user_id, signal_id, market, symbol, side,
            entry, actual_entry, signal_entry, entry_drift_r,
            current_price, sl, tp1, tp2, initial_sl,
            risk_amount, initial_risk_amount, current_risk_amount, current_risk_r,
            position_size, notional, margin_required, balance_snapshot,
            remaining_percent, tp1_hit, tp2_hit, total_fees_paid,
            strategy_version, status, last_recommendation, opened_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, 1.0, ?, ?, ?, ?, 100, 0, 0, ?, ?, 'active', ?, ?, ?)""",
            (user_id, signal_id, market, signal["symbol"], side,
             actual_entry, actual_entry, signal_entry, drift_info["drift_r"],
             actual_entry, actual_sl, tp1, tp2, actual_sl,
             sizing["risk_amount"], sizing["risk_amount"], sizing["risk_amount"],
             sizing["quantity"], sizing["notional"], sizing["margin_required"],
             balance, entry_fee,
             signal.get("strategy_version", STRATEGY_A),
             "Сделка открыта", now, now))
        trade_id = int(cursor.lastrowid)

        conn.execute("UPDATE signals SET status='ENTERED', updated_at=? WHERE id=?", (now, signal_id))
        conn.execute("""INSERT INTO trade_events
            (trade_id, user_id, event_type, message, price, created_at)
            VALUES (?, ?, 'opened', ?, ?, ?)""",
            (trade_id, user_id, "Пользователь подтвердил вход", actual_entry, now))
        conn.execute("""INSERT INTO trade_executions
            (trade_id, user_id, exec_type, price, quantity, share_percent,
             fee_usdt, slippage_usdt, realized_pnl_usdt, executed_at)
            VALUES (?, ?, 'entry', ?, ?, 100.0, ?, 0.0, 0.0, ?)""",
            (trade_id, user_id, actual_entry, sizing["quantity"], entry_fee, now))

    log_signal_event(signal_id, "entered", sig_status, "ENTERED", actual_entry, "Открыта сделка пользователем")

    warnings = []
    if drift_info["exceeds_0_5r"]:
        warnings.append(f"⚠️ Дрифт входа {drift_info['drift_r']:.2f}R (>0.5R). RR: {drift_info['recalculated_rr']:.2f}.")
        if drift_info["unfavorable"]:
            warnings.append("⛔ Вход невыгоден — рассмотри пропуск.")
    return trade_id, None, ("\n".join(warnings) if warnings else None)

def update_trade_field(trade_id, user_id, field, value):
    allowed = {"actual_entry", "sl"}
    if field not in allowed:
        return False, "Поле нельзя менять."
    value = safe_float(value, 0)
    if value <= 0:
        return False, "Цена должна быть > 0."
    trade = get_trade(trade_id, user_id)
    if not trade:
        return False, "Сделка не найдена."
    if normalize_trade_status(trade.get("status")) not in ("active", "open"):
        return False, "Сделка не активна."

    if field == "actual_entry" and (trade.get("tp1_hit") or trade.get("tp2_hit")):
        return False, "Нельзя менять вход после частичного/полного закрытия."

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
            return False, "Новые уровни некорректны."
        sizing = calculate_position_size(balance, value, new_sl, market)
        sig_entry = safe_float(trade.get("signal_entry"), old_entry)
        drift_info = assess_entry_drift(sig_entry, new_sl, new_tp2, value)

        with db() as conn:
            conn.execute("""UPDATE trades SET
                actual_entry=?, entry=?, current_price=?, sl=?, initial_sl=?,
                tp1=?, tp2=?, entry_drift_r=?, risk_amount=?, initial_risk_amount=?,
                current_risk_amount=?, current_risk_r=1.0, position_size=?,
                notional=?, margin_required=?, updated_at=?
                WHERE id=? AND user_id=?""",
                (value, value, value, new_sl, new_sl, new_tp1, new_tp2,
                 drift_info["drift_r"], sizing["risk_amount"], sizing["risk_amount"],
                 sizing["risk_amount"], sizing["quantity"], sizing["notional"],
                 sizing["margin_required"], utc_text(), trade_id, user_id))
            conn.execute("""DELETE FROM trade_events WHERE trade_id=? AND user_id=?
                AND event_type IN ('touch_tp1', 'touch_tp2', 'touch_sl')""",
                (trade_id, user_id))
        save_trade_event(trade_id, user_id, "entry_updated", "Изменён вход", value)
        msg = "Цена входа пересчитана."
        if drift_info["exceeds_0_5r"]:
            msg += f"\n⚠️ Дрифт: {drift_info['drift_r']:.2f}R, RR: {drift_info['recalculated_rr']:.2f}."
        return True, msg

    if field == "sl":
        entry = safe_float(trade["actual_entry"], 0)
        if is_long_side(trade["side"]) and value >= entry:
            return False, "Для LONG SL ниже входа."
        if not is_long_side(trade["side"]) and value <= entry:
            return False, "Для SHORT SL выше входа."
        remaining_pct = safe_float(trade.get("remaining_percent"), 100.0)
        rem_frac = max(0.0, min(1.0, remaining_pct / 100.0))
        full_qty = safe_float(trade.get("position_size"), 0.0)
        rem_qty = full_qty * rem_frac
        new_current_risk = rem_qty * abs(entry - value)
        base_one_r = safe_float(trade.get("initial_risk_amount"), 0.0) or safe_float(trade.get("risk_amount"), 0.0)
        new_current_risk_r = (new_current_risk / base_one_r) if base_one_r > 0 else 0.0
        with db() as conn:
            conn.execute("""UPDATE trades SET sl=?, current_risk_amount=?,
                current_risk_r=?, updated_at=? WHERE id=? AND user_id=?""",
                (value, new_current_risk, new_current_risk_r, utc_text(), trade_id, user_id))
        save_trade_event(trade_id, user_id, "sl_updated", "Изменён SL", value)
        return True, (f"SL обновлён. Текущий риск: {new_current_risk:.4f} USDT ({new_current_risk_r:.2f}R). "
                      "Объём и исходный 1R сохранены.")

    return False, "Неизвестная операция"

def close_trade_record(trade, exit_price, reason, exec_type: str = "manual_close"):
    trade_id = trade["id"]; user_id = trade["user_id"]
    entry = safe_float(trade["actual_entry"], 0)
    initial_sl = safe_float(trade["initial_sl"], 0)
    quantity = safe_float(trade["position_size"], 0)
    if entry <= 0 or initial_sl <= 0 or quantity <= 0:
        return False, "Недостаточно данных."
    exit_price = safe_float(exit_price, 0)
    if exit_price <= 0:
        return False, "Некорректная цена выхода."

    direction = 1 if is_long_side(trade["side"]) else -1
    tp1_hit = bool(trade.get("tp1_hit"))
    prev_fees = safe_float(trade.get("total_fees_paid"), 0.0)

    if tp1_hit:
        tp1 = safe_float(trade["tp1"], 0)
        qty_tp1 = safe_float(trade.get("tp1_fill_qty"), quantity * (TP1_CLOSE_PERCENT / 100.0))
        rem_qty = max(0.0, quantity - qty_tp1)
        gross_tp1 = qty_tp1 * (tp1 - entry) * direction
        gross_rem = rem_qty * (exit_price - entry) * direction
        gross_realized = gross_tp1 + gross_rem
        exit_fee = rem_qty * exit_price * TAKER_FEE_RATE
        total_fees = prev_fees + exit_fee
    else:
        rem_qty = quantity
        gross_realized = quantity * (exit_price - entry) * direction
        exit_fee = quantity * exit_price * TAKER_FEE_RATE
        total_fees = prev_fees + exit_fee

    net_realized = gross_realized - total_fees
    initial_risk = safe_float(trade.get("initial_risk_amount"), 0.0) or safe_float(trade.get("risk_amount"), 0.0)
    if initial_risk <= 0:
        initial_risk = quantity * abs(entry - initial_sl)
    pnl_r = net_realized / initial_risk if initial_risk > 0 else 0.0
    base = quantity * entry
    pnl_percent = (net_realized / base * 100.0) if base > 0 else 0.0
    result = "win" if net_realized > 0 else ("loss" if net_realized < 0 else "be")

    now = utc_text()
    with db() as con:
        con.execute("""UPDATE trades SET status='closed', closed_at=?,
            current_price=?, total_fees_paid=?, updated_at=? WHERE id=? AND user_id=?""",
            (now, exit_price, total_fees, now, trade_id, user_id))
        con.execute("""INSERT INTO trade_history (
            trade_id, user_id, market, symbol, side, entry, exit_price, result,
            pnl_percent, pnl_r, net_pnl_usdt, initial_risk_amount,
            total_fees_paid, total_slippage_cost, strategy_version,
            close_reason, opened_at, closed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (trade_id, user_id, trade["market"], trade["symbol"], trade["side"],
             entry, exit_price, result, pnl_percent, pnl_r, net_realized,
             initial_risk, total_fees, safe_float(trade.get("total_slippage_cost"), 0.0),
             trade.get("strategy_version", STRATEGY_A), reason,
             trade.get("opened_at"), now))
        con.execute("""INSERT INTO trade_events
            (trade_id, user_id, event_type, message, price, created_at)
            VALUES (?, ?, 'closed', ?, ?, ?)""",
            (trade_id, user_id, f"Сделка закрыта ({reason})", exit_price, now))

    save_trade_execution(trade_id, user_id, exec_type, exit_price, rem_qty, 100.0,
                         exit_fee, 0.0, net_realized, now)

    return True, (f"✅ Сделка закрыта.\nРезультат: {result.upper()}\n"
                  f"PnL: {net_realized:+.4f} USDT ({pnl_percent:+.2f}%, {pnl_r:+.2f}R)\n"
                  f"Комиссии: {total_fees:.4f} USDT")


# ============================================================
# 13. TEXT FORMATTERS
# ============================================================

def trade_text(trade):
    init_risk = safe_float(trade.get("initial_risk_amount"), safe_float(trade.get("risk_amount"), 0))
    curr_risk = safe_float(trade.get("current_risk_amount"), init_risk)
    st = normalize_trade_status(trade.get("status"))
    st_ru = "активна" if st == "active" else "закрыта"
    return (
        f"📊 <b>{html.escape(str(trade['symbol']))}</b> "
        f"[Стратегия {html.escape(str(trade.get('strategy_version', 'A')))}]\n"
        f"Рынок: {market_name(trade['market'])}\n"
        f"Направление: {side_name(trade['side'])}\n"
        f"Статус: {st_ru}\n\n"
        f"Цена входа: <code>{format_price(trade.get('actual_entry'))}</code>\n"
        f"Текущая цена: <code>{format_price(trade.get('current_price'))}</code>\n"
        f"Стоп-лосс: <code>{format_price(trade.get('sl'))}</code>\n"
        f"TP1: <code>{format_price(trade.get('tp1'))}</code>\n"
        f"TP2: <code>{format_price(trade.get('tp2'))}</code>\n\n"
        f"Исходный риск (1R): {init_risk:.4f} USDT\n"
        f"Текущий риск остатка: {curr_risk:.4f} USDT\n"
        f"Количество: {safe_float(trade.get('position_size')):.8f}\n"
        f"Объём: {safe_float(trade.get('notional')):.2f} USDT\n"
        f"Маржа: {safe_float(trade.get('margin_required')):.2f} USDT\n"
        f"Комиссии: {safe_float(trade.get('total_fees_paid')):.4f} USDT\n"
        f"Остаток: {safe_float(trade.get('remaining_percent'), 100):.0f}%\n\n"
        f"TP1: {'✅' if trade.get('tp1_hit') else '—'} | "
        f"TP2: {'✅' if trade.get('tp2_hit') else '—'}\n\n"
        f"<i>{html.escape(str(trade.get('last_recommendation') or 'Пока нет'))}</i>"
    )

def signal_text(signal):
    tp1 = safe_float(signal.get("tp1"), 0)
    tp2 = safe_float(signal.get("tp2"), signal.get("tp", 0))
    live_p = safe_float(signal.get("live_price"), signal["entry"])
    spread_pct = safe_float(signal.get("spread_pct"), DEFAULT_SYNTHETIC_SPREAD_PCT)
    entry_mode = signal.get("entry_mode", "market")
    mode_label = "Вход от уровня" if entry_mode == "level" else "Рыночный вход"
    raw_reasons = signal.get("reasons", "")
    if isinstance(raw_reasons, (list, tuple)):
        reasons_text = "\n".join(f"• {html.escape(str(r))}" for r in raw_reasons if r)
    else:
        parts = [p.strip() for p in str(raw_reasons).split(";") if p.strip()]
        reasons_text = "\n".join(f"• {html.escape(p)}" for p in parts) if parts else "—"

    extra = [
        f"Стратегия: <b>{html.escape(str(signal.get('strategy_version', 'A')))}</b>",
        f"Режим: <b>{mode_label}</b>",
        f"Цена: <code>{format_price(live_p)}</code> (спред ~{spread_pct:.3f}%)",
    ]
    if entry_mode == "level":
        zl = safe_float(signal.get("entry_zone_low"), signal["entry"])
        zh = safe_float(signal.get("entry_zone_high"), signal["entry"])
        extra.append(f"Зона: <code>{format_price(zl)} — {format_price(zh)}</code>")
        if signal.get("confirmation_rule"):
            extra.append(f"Подтверждение: {html.escape(signal['confirmation_rule'])}")
    if signal.get("expires_at"):
        extra.append(f"Действителен до: <code>{format_moscow_from_utc(signal['expires_at'])} МСК</code>")

    return (
        f"🚨 <b>НОВЫЙ СИГНАЛ</b>\n\n"
        f"Монета: <b>{html.escape(str(signal['symbol']))}</b>\n"
        f"Рынок: {market_name(signal['market'])}\n"
        f"Направление: {side_name(signal['side'])}\n"
        + "\n".join(extra) + "\n\n"
        f"Вход: <code>{format_price(signal['entry'])}</code>\n"
        f"SL: <code>{format_price(signal['sl'])}</code>\n"
        f"TP1: <code>{format_price(tp1)}</code> ({TP1_R}R) — {TP1_CLOSE_PERCENT:.0f}%\n"
        f"TP2: <code>{format_price(tp2)}</code> ({TP2_R}R) — {TP2_CLOSE_PERCENT:.0f}%\n\n"
        f"Оценка: {signal.get('score', 0)}\n"
        f"RR до TP2: {signal.get('rr', 0):.2f}\n\n"
        f"Причины:\n{reasons_text}"
    )


# ============================================================
# 14. TELEGRAM KEYBOARDS
# ============================================================

def main_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="🔎 Анализ рынка")],
        [KeyboardButton(text="📊 Мои сделки"), KeyboardButton(text="📚 История")],
        [KeyboardButton(text="📈 Статистика"), KeyboardButton(text="⚙️ Настройки")],
        [KeyboardButton(text="🖥 Статус системы")],
    ], resize_keyboard=True)

def signal_keyboard(signal_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Я вошёл в сделку", callback_data=f"enter:{signal_id}")
    ]])

def trade_keyboard(trade_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"trade_refresh:{trade_id}")],
        [InlineKeyboardButton(text="✏️ Исправить вход", callback_data=f"trade_edit_entry:{trade_id}")],
        [InlineKeyboardButton(text="🛑 Обновить SL", callback_data=f"trade_edit_sl:{trade_id}")],
        [InlineKeyboardButton(text="Закрыть сделку", callback_data=f"trade_close:{trade_id}")],
    ])

def confirm_keyboard(action, trade_id):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Да", callback_data=f"{action}_confirm:{trade_id}"),
        InlineKeyboardButton(text="Отмена", callback_data=f"{action}_cancel:{trade_id}"),
    ]])


# ============================================================
# 15. ANALYSIS + SCAN
# ============================================================

async def analyze_market_symbol(market, symbol, strategy_version=DEFAULT_STRATEGY):
    key = (market, symbol)
    if key in _unavailable_symbols:
        return None
    now_ts = time.monotonic()
    if key in _insufficient_history and now_ts < _insufficient_history[key]:
        return None

    intervals = ("5m", "15m", "1h", "4h")
    candles = {}
    for interval in intervals:
        try:
            data = await get_klines(market, symbol, interval, KLINE_LIMIT)
        except MarketError as exc:
            text = str(exc).lower()
            if ("100204" in str(exc) or "109425" in str(exc)
                    or "symbol is not found" in text or "not exist" in text):
                _unavailable_symbols.add(key)
                log.warning("Символ %s %s недоступен", market, symbol)
                return None
            if "недостаточно закрытых свечей" in text:
                _insufficient_history[key] = now_ts + 6 * 3600
                log.info("Мало истории %s %s — пауза 6ч", market, symbol)
                return None
            raise
        if data is None or len(data) < MIN_CANDLE_ROWS:
            return None
        candles[interval] = data
        try:
            save_closed_candles(market, symbol, interval, data)
        except Exception:
            log.exception("Не сохранились свечи %s %s %s", market, symbol, interval)

    try:
        ver = str(strategy_version or DEFAULT_STRATEGY).upper()
        if ver == STRATEGY_B:
            quote = latest_quotes.get(key)
            signal = build_signal_b(market, symbol, candles, quote=quote)
        else:
            signal = build_signal_a_v92(market, symbol, candles)
    except Exception:
        log.exception("Ошибка анализа %s %s", market, symbol)
        return None

    if not signal:
        return None
    try:
        snapshot = await fetch_live_market_snapshot(market, symbol)
    except Exception:
        snapshot = None
    return enrich_signal_with_live_data(signal, snapshot)

async def scan_market(strategy_version=DEFAULT_STRATEGY):
    async with _scan_lock:
        found = []
        system_status["scan_count"] = int(system_status.get("scan_count", 0)) + 1
        system_status["last_scan"] = utc_text()

        for market in MARKETS:
            for symbol in SYMBOLS:
                key = (market, symbol)
                if key in _unavailable_symbols:
                    continue
                try:
                    signal = await analyze_market_symbol(market, symbol, strategy_version)
                    if not signal:
                        continue
                    signal_id = save_signal(signal)
                    if signal_id:
                        signal["id"] = signal_id
                        found.append(signal)
                        if len(found) >= MAX_SIGNALS_PER_CYCLE:
                            break
                except MarketError as exc:
                    text = str(exc)
                    if "100410" in text or "100429" in text:
                        log.debug("Rate limit %s %s", market, symbol)
                    else:
                        log.warning("Ошибка %s %s: %s", market, symbol, exc)
                except Exception:
                    log.exception("Ошибка сканирования %s %s", market, symbol)
            if len(found) >= MAX_SIGNALS_PER_CYCLE:
                break

        system_status["signals_count"] = int(system_status.get("signals_count", 0)) + len(found)
        return found

async def broadcast_signal(signal):
    with db() as con:
        users = con.execute("SELECT user_id FROM users WHERE is_active=1").fetchall()
    for row in users:
        uid = row["user_id"]
        if get_user_strategy(uid) != str(signal.get("strategy_version", STRATEGY_A)).upper():
            continue
        try:
            await bot.send_message(uid, signal_text(signal),
                                   reply_markup=signal_keyboard(signal["id"]),
                                   parse_mode="HTML")
        except Exception:
            log.exception("Не отправлен сигнал %s", uid)


# ============================================================
# 16. HANDLERS
# ============================================================

@router.message(CommandStart())
async def start_command(message: Message):
    register_user(message.from_user)
    clear_user_dialog_state(message.from_user.id)
    await message.answer(
        "Привет! Trading Bot v9.3.\n\n"
        "Бот анализирует публичные данные BingX и шлёт сигналы.\n\n"
        "⚠️ Это аналитика, не исполненная сделка. Сопровождение — только после "
        "подтверждения входа и ввода фактической цены.\n\n"
        "Выбери раздел в меню.",
        reply_markup=main_keyboard(),
    )

@router.message(Command("cancel"))
async def cancel_command(message: Message):
    clear_user_dialog_state(message.from_user.id)
    await message.answer("Действие отменено.", reply_markup=main_keyboard())

@router.message(F.text == "🔎 Анализ рынка")
async def scan_market_callback(message: Message):
    register_user(message.from_user)
    clear_user_dialog_state(message.from_user.id)
    ver = get_user_strategy(message.from_user.id)
    status = await message.answer(f"⏳ Анализ рынка (стратегия {ver})...")
    try:
        expire_stale_signals()
        signals = await scan_market(strategy_version=ver)
        if not signals:
            await status.edit_text("Пока нет новых сигналов.")
            return
        await status.edit_text(f"✅ Найдено сигналов: {len(signals)}.")
        for sig in signals:
            await message.answer(signal_text(sig),
                                 reply_markup=signal_keyboard(sig["id"]),
                                 parse_mode="HTML")
    except Exception:
        log.exception("Ошибка ручного сканирования")
        await status.edit_text("❌ Не удалось завершить анализ.")

@router.callback_query(F.data.startswith("enter:"))
async def enter_trade_callback(callback: CallbackQuery):
    register_user(callback.from_user)
    try:
        signal_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный сигнал.", show_alert=True)
        return

    with db() as conn:
        signal = conn.execute("SELECT id, market, symbol, status, expires_at FROM signals WHERE id=?",
                              (signal_id,)).fetchone()
    if not signal:
        await callback.answer("Сигнал не найден.", show_alert=True)
        return
    sig_status = str(signal["status"] or "").upper()
    if sig_status == "ENTERED":
        await callback.answer("По этому сигналу уже открыта сделка.", show_alert=True)
        return
    exp_dt = parse_utc_text(signal["expires_at"])
    if sig_status in ("EXPIRED", "CANCELLED") or (exp_dt and utc_now() >= exp_dt):
        await callback.answer("Сигнал просрочен или отменён.", show_alert=True)
        return
    if len(get_active_trades(callback.from_user.id)) >= MAX_ACTIVE_TRADES:
        await callback.answer(f"Лимит {MAX_ACTIVE_TRADES} активных сделок.", show_alert=True)
        return
    market = str(signal["market"]).lower()
    if get_user_balance(callback.from_user.id, market) <= 0:
        await callback.message.answer(f"Сначала укажи баланс {market_name(market)} в «Настройках».")
        await callback.answer()
        return

    set_user_dialog_state(callback.from_user.id, "waiting_entry", signal_id)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer(
        f"Ты подтвердил вход в {signal['symbol']}.\n\n"
        "Введи фактическую цену входа (например: 67250.5).\n\n"
        "Бот НЕ открывает сделку на бирже — только сопровождает."
    )
    await callback.answer()

@router.callback_query(F.data.startswith("trade_refresh:"))
async def trade_refresh_callback(callback: CallbackQuery):
    try:
        trade_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Неверный ID.", show_alert=True)
        return
    trade = get_trade(trade_id, callback.from_user.id)
    if not trade:
        await callback.answer("Сделка не найдена.", show_alert=True)
        return
    await callback.message.answer(trade_text(trade),
                                  reply_markup=trade_keyboard(trade_id),
                                  parse_mode="HTML")
    await callback.answer("Обновлено.")

@router.callback_query(F.data.startswith("trade_edit_entry:"))
async def trade_edit_entry_callback(callback: CallbackQuery):
    try:
        trade_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Неверный ID.", show_alert=True)
        return
    trade = get_trade(trade_id, callback.from_user.id)
    if not trade:
        await callback.answer("Сделка не найдена.", show_alert=True)
        return
    set_user_dialog_state(callback.from_user.id, "waiting_new_entry", trade_id)
    await callback.message.answer("Введи новую цену входа. SL/TP пересчитаются.")
    await callback.answer()

@router.callback_query(F.data.startswith("trade_edit_sl:"))
async def trade_edit_sl_callback(callback: CallbackQuery):
    try:
        trade_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Неверный ID.", show_alert=True)
        return
    trade = get_trade(trade_id, callback.from_user.id)
    if not trade:
        await callback.answer("Сделка не найдена.", show_alert=True)
        return
    set_user_dialog_state(callback.from_user.id, "waiting_sl", trade_id)
    await callback.message.answer("Введи новый SL. Объём и 1R сохранятся.")
    await callback.answer()

@router.callback_query(F.data.startswith("trade_close:"))
async def trade_close_callback(callback: CallbackQuery):
    try:
        trade_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Неверный ID.", show_alert=True)
        return
    trade = get_trade(trade_id, callback.from_user.id)
    if not trade:
        await callback.answer("Сделка не найдена.", show_alert=True)
        return
    set_user_dialog_state(callback.from_user.id, "waiting_close_price", trade_id)
    await callback.message.answer(
        "Введи фактическую цену выхода.\n"
        "Для отмены — /cancel."
    )
    await callback.answer()

@router.callback_query(F.data.startswith("tp1_confirm:"))
async def tp1_confirm_callback(callback: CallbackQuery):
    try:
        trade_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Неверный ID.", show_alert=True)
        return
    trade = get_trade(trade_id, callback.from_user.id)
    if not trade or trade.get("tp1_hit"):
        await callback.answer("Уже подтверждено или сделка не найдена.", show_alert=True)
        return
    tp1_price = safe_float(trade["tp1"], 0)
    qty_full = safe_float(trade["position_size"], 0)
    qty_tp1 = qty_full * (TP1_CLOSE_PERCENT / 100.0)
    fee = qty_tp1 * tp1_price * TAKER_FEE_RATE
    remaining = 100.0 - TP1_CLOSE_PERCENT
    with db() as conn:
        conn.execute("""UPDATE trades SET tp1_hit=1, tp1_fill_price=?, tp1_fill_qty=?,
            tp1_fill_fee=?, tp1_closed_at=?, remaining_percent=?,
            total_fees_paid=COALESCE(total_fees_paid, 0) + ?,
            updated_at=? WHERE id=? AND user_id=?""",
            (tp1_price, qty_tp1, fee, utc_text(), remaining, fee, utc_text(),
             trade_id, callback.from_user.id))
    save_trade_event(trade_id, callback.from_user.id, "tp1_confirmed", "Подтверждён TP1", tp1_price)
    save_trade_execution(trade_id, callback.from_user.id, "tp1_close", tp1_price,
                         qty_tp1, TP1_CLOSE_PERCENT, fee, 0.0, 0.0)
    await callback.message.answer(f"✅ TP1 подтверждён. Закрыто {TP1_CLOSE_PERCENT:.0f}%.")
    await callback.answer()

@router.callback_query(F.data.startswith("tp2_confirm:"))
async def tp2_confirm_callback(callback: CallbackQuery):
    try:
        trade_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Неверный ID.", show_alert=True)
        return
    trade = get_trade(trade_id, callback.from_user.id)
    if not trade:
        await callback.answer("Сделка не найдена.", show_alert=True)
        return
    ok, msg = close_trade_record(trade, trade["tp2"], "TP2 достигнут", exec_type="tp2_close")
    await callback.message.answer(msg if ok else f"❌ {msg}")
    await callback.answer()

@router.callback_query(F.data.startswith("sl_confirm:"))
async def sl_confirm_callback(callback: CallbackQuery):
    try:
        trade_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Неверный ID.", show_alert=True)
        return
    trade = get_trade(trade_id, callback.from_user.id)
    if not trade:
        await callback.answer("Сделка не найдена.", show_alert=True)
        return
    ok, msg = close_trade_record(trade, trade["sl"], "Стоп-лосс", exec_type="sl_close")
    await callback.message.answer(msg if ok else f"❌ {msg}")
    await callback.answer()

@router.callback_query(F.data.startswith("sl_keep:"))
async def sl_keep_callback(callback: CallbackQuery):
    await callback.answer("Сделка оставлена активной.", show_alert=True)

@router.message(F.text == "📊 Мои сделки")
async def my_trades_handler(message: Message):
    register_user(message.from_user)
    clear_user_dialog_state(message.from_user.id)
    trades = get_active_trades(message.from_user.id)
    if not trades:
        await message.answer("📊 Активных сделок нет.")
        return
    await message.answer(f"📊 Активных сделок: {len(trades)} / {MAX_ACTIVE_TRADES}")
    for tr in trades:
        await message.answer(trade_text(tr), reply_markup=trade_keyboard(tr["id"]), parse_mode="HTML")

@router.message(F.text == "📚 История")
async def history_handler(message: Message):
    register_user(message.from_user)
    clear_user_dialog_state(message.from_user.id)
    with db() as con:
        rows = con.execute("""SELECT * FROM trade_history WHERE user_id=?
            ORDER BY closed_at DESC LIMIT 20""", (message.from_user.id,)).fetchall()
    if not rows:
        await message.answer("📚 История пуста.")
        return
    lines = ["📚 <b>Последние закрытые сделки</b>\n"]
    for r in rows:
        lines.append(
            f"\n<b>{r['market']} | {r['symbol']}</b>\n"
            f"{side_name(r['side'])} | {r['result']}\n"
            f"Вход: {format_price(r['entry'])} → выход: {format_price(r['exit_price'])}\n"
            f"PnL: {safe_float(r['pnl_percent']):.2f}% / {safe_float(r['pnl_r']):.2f}R"
        )
    await message.answer("\n".join(lines), parse_mode="HTML")

@router.message(F.text == "📈 Статистика")
async def statistics_handler(message: Message):
    register_user(message.from_user)
    clear_user_dialog_state(message.from_user.id)
    with db() as con:
        total = con.execute("SELECT COUNT(*) AS c FROM trade_history WHERE user_id=?",
                            (message.from_user.id,)).fetchone()["c"]
        wins = con.execute("SELECT COUNT(*) AS c FROM trade_history WHERE user_id=? AND result='win'",
                           (message.from_user.id,)).fetchone()["c"]
        losses = con.execute("SELECT COUNT(*) AS c FROM trade_history WHERE user_id=? AND result='loss'",
                             (message.from_user.id,)).fetchone()["c"]
        agg = con.execute("""SELECT COALESCE(SUM(pnl_percent),0) AS p,
            COALESCE(SUM(pnl_r),0) AS r FROM trade_history WHERE user_id=?""",
            (message.from_user.id,)).fetchone()
    active = len(get_active_trades(message.from_user.id))
    winrate = wins / total * 100 if total else 0
    await message.answer(
        "📈 <b>Статистика</b>\n\n"
        f"Закрытых: {total}\nПрибыльных: {wins}\nУбыточных: {losses}\n"
        f"Winrate: {winrate:.1f}%\n"
        f"Сумма PnL: {safe_float(agg['p']):.2f}% / {safe_float(agg['r']):.2f}R\n"
        f"Активных: {active}/{MAX_ACTIVE_TRADES}",
        parse_mode="HTML",
    )

@router.message(F.text == "⚙️ Настройки")
async def settings_handler(message: Message):
    register_user(message.from_user)
    clear_user_dialog_state(message.from_user.id)
    spot = get_user_balance(message.from_user.id, "spot")
    swap = get_user_balance(message.from_user.id, "swap")
    ver = get_user_strategy(message.from_user.id)
    await message.answer(
        "⚙️ <b>Настройки</b>\n\n"
        f"Стратегия: <b>{ver}</b>\n"
        f"Риск: {RISK_PERCENT}%\nПлечо: {FUTURES_LEVERAGE}x\n"
        f"TP1: {TP1_R}R — {TP1_CLOSE_PERCENT:.0f}%\n"
        f"TP2: {TP2_R}R — {TP2_CLOSE_PERCENT:.0f}%\n\n"
        f"Spot: {spot:.2f} USDT\nFutures: {swap:.2f} USDT\n\n"
        "Команды:\n"
        "<code>/spotbalance 1000</code>\n"
        "<code>/swapbalance 500</code>\n"
        "<code>/strategy A</code> или <code>/strategy B</code>",
        parse_mode="HTML",
    )

@router.message(Command("strategy"))
async def set_strategy_handler(message: Message):
    register_user(message.from_user)
    parts = (message.text or "").split()
    if len(parts) != 2 or parts[1].upper() not in (STRATEGY_A, STRATEGY_B):
        await message.answer("Формат: /strategy A или /strategy B")
        return
    chosen = set_user_strategy(message.from_user.id, parts[1].upper())
    await message.answer(f"✅ Стратегия: <b>{chosen}</b>", parse_mode="HTML")

@router.message(Command("spotbalance"))
async def spot_balance_handler(message: Message):
    register_user(message.from_user)
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Формат: /spotbalance 1000")
        return
    try:
        balance = float(parts[1].replace(",", "."))
        set_user_balance(message.from_user.id, "spot", balance)
    except (ValueError, TypeError):
        await message.answer("Некорректный баланс.")
        return
    await message.answer(f"✅ Spot: {balance:.2f} USDT")

@router.message(Command("swapbalance"))
async def swap_balance_handler(message: Message):
    register_user(message.from_user)
    parts = (message.text or "").split()
    if len(parts) != 2:
        await message.answer("Формат: /swapbalance 500")
        return
    try:
        balance = float(parts[1].replace(",", "."))
        set_user_balance(message.from_user.id, "swap", balance)
    except (ValueError, TypeError):
        await message.answer("Некорректный баланс.")
        return
    await message.answer(f"✅ Futures: {balance:.2f} USDT")

@router.message(F.text == "🖥 Статус системы")
async def system_status_handler(message: Message):
    register_user(message.from_user)
    clear_user_dialog_state(message.from_user.id)
    active = len(get_active_trades(message.from_user.id))
    ver = get_user_strategy(message.from_user.id)
    await message.answer(
        f"🖥 <b>Trading Bot v{BOT_VERSION}</b>\n\n"
        f"Режим: публичный анализ BingX\n"
        f"Реальные ордера: <b>отключены</b>\n"
        f"Стратегия: {ver}\n"
        f"API: {system_status.get('api_status', '—')}\n"
        f"Сканов: {system_status.get('scan_count', 0)}\n"
        f"Сигналов: {system_status.get('signals_count', 0)}\n"
        f"Последний скан: {system_status.get('last_scan') or '—'}\n"
        f"Твои сделки: {active}/{MAX_ACTIVE_TRADES}\n"
        f"Символов недоступно: {len(_unavailable_symbols)}",
        parse_mode="HTML",
    )

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
            report["trades_active"] = con.execute(
                "SELECT COUNT(*) FROM trades WHERE LOWER(status)='active'").fetchone()[0]
        text = (
            f"🧪 <b>diag_db</b>\n\n"
            f"integrity: {report['integrity']}\n"
            f"fk_violations: {report['fk_violations']}\n"
            f"active_signals: {report['signals_active']}\n"
            f"active_trades: {report['trades_active']}\n\n"
            f"counts: {report['counts']}"
        )
        await message.answer(text, parse_mode="HTML")
    except Exception as e:
        log.exception("diag_db error")
        await message.answer(f"❌ Ошибка: {e}")

# ВАЖНО: этот хендлер должен быть ПОСЛЕДНИМ среди F.text
def _parse_price_input(raw_text: str | None) -> float | None:
    if not raw_text:
        return None
    cleaned = raw_text.strip().replace("\u00a0", "").replace(" ", "").replace(",", ".")
    if not cleaned:
        return None
    try:
        val = float(cleaned)
    except ValueError:
        return None
    if not math.isfinite(val):
        return None
    return val

@router.message(
    F.text,
    lambda message: message.from_user is not None
    and get_user_dialog_state(message.from_user.id) is not None,
)
async def handle_user_text(message: Message):
    user_id = message.from_user.id
    state = get_user_dialog_state(user_id)
    if not state:
        return
    raw = (message.text or "").strip().lower()
    if raw in ("отмена", "cancel", "/cancel"):
        clear_user_dialog_state(user_id)
        await message.answer("Отменено.", reply_markup=main_keyboard())
        return

    value = _parse_price_input(message.text)
    if value is None:
        await message.answer("Не распознал число. Пример: 67250.5")
        return
    if value <= 0:
        await message.answer("Цена должна быть > 0.")
        return

    st = state.get("type")
    ref = state.get("ref_id")

    if st == "waiting_entry":
        if ref is None:
            clear_user_dialog_state(user_id)
            await message.answer("Не удалось определить сигнал. Нажми кнопку снова.")
            return
        trade_id, err, warn = create_personal_trade(user_id, int(ref), value)
        if err:
            await message.answer(err)
            return
        clear_user_dialog_state(user_id)
        trade = get_trade(trade_id, user_id)
        prefix = "✅ Сделка добавлена."
        if warn:
            prefix = f"{prefix}\n\n{warn}"
        await message.answer(f"{prefix}\n\n{trade_text(trade)}",
                             reply_markup=trade_keyboard(trade_id),
                             parse_mode="HTML")
        return

    if st == "waiting_new_entry":
        if ref is None:
            clear_user_dialog_state(user_id)
            await message.answer("Сделка не определена.")
            return
        ok, msg = update_trade_field(int(ref), user_id, "actual_entry", value)
        if ok:
            clear_user_dialog_state(user_id)
            trade = get_trade(int(ref), user_id)
            await message.answer(f"{msg}\n\n{trade_text(trade)}",
                                 reply_markup=trade_keyboard(int(ref)),
                                 parse_mode="HTML")
        else:
            await message.answer(msg)
        return

    if st == "waiting_sl":
        if ref is None:
            clear_user_dialog_state(user_id)
            await message.answer("Сделка не определена.")
            return
        ok, msg = update_trade_field(int(ref), user_id, "sl", value)
        if ok:
            clear_user_dialog_state(user_id)
            trade = get_trade(int(ref), user_id)
            await message.answer(f"{msg}\n\n{trade_text(trade)}",
                                 reply_markup=trade_keyboard(int(ref)),
                                 parse_mode="HTML")
        else:
            await message.answer(msg)
        return

    if st == "waiting_close_price":
        if ref is None:
            clear_user_dialog_state(user_id)
            await message.answer("Сделка не определена.")
            return
        trade = get_trade(int(ref), user_id)
        if not trade:
            clear_user_dialog_state(user_id)
            await message.answer("Сделка не найдена.")
            return
        ok, msg = close_trade_record(trade, value, "Ручное закрытие", exec_type="manual_close")
        clear_user_dialog_state(user_id)
        await message.answer(msg if ok else f"❌ {msg}")
        return


# ============================================================
# 17. MONITORING (NO AUTO-EXECUTION)
# ============================================================

def trade_event_exists(trade_id, event_type):
    with db() as con:
        row = con.execute("SELECT id FROM trade_events WHERE trade_id=? AND event_type=? LIMIT 1",
                          (trade_id, event_type)).fetchone()
    return row is not None

async def notify_trade_touch(trade, event_type, price):
    trade_id = trade["id"]
    user_id = trade["user_id"]
    if trade_event_exists(trade_id, event_type):
        return

    if event_type == "touch_tp1":
        text = (f"🎯 <b>{trade['symbol']}: TP1</b>\n"
                f"Цена: {format_price(price)} | TP1: {format_price(trade['tp1'])}\n\n"
                "Если реально закрыл 70% — подтверди.")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Подтверждаю TP1", callback_data=f"tp1_confirm:{trade_id}")
        ]])
    elif event_type == "touch_tp2":
        text = (f"🎯 <b>{trade['symbol']}: TP2</b>\n"
                f"Цена: {format_price(price)} | TP2: {format_price(trade['tp2'])}\n\n"
                "Если закрыл остаток — подтверди.")
        kb = confirm_keyboard("tp2", trade_id)
    else:
        text = (f"⚠️ <b>{trade['symbol']}: SL</b>\n"
                f"Цена: {format_price(price)} | SL: {format_price(trade['sl'])}\n\n"
                "Проверь биржу и подтверди закрытие.")
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Закрыл по SL", callback_data=f"sl_confirm:{trade_id}"),
            InlineKeyboardButton(text="Оставить", callback_data=f"sl_keep:{trade_id}"),
        ]])

    try:
        await bot.send_message(user_id, text, reply_markup=kb, parse_mode="HTML")
        save_trade_event(trade_id, user_id, event_type, "Касание уровня", price)
    except Exception:
        log.exception("Не отправлено уведомление %s", trade_id)

async def monitor_one_trade(trade):
    market = str(trade["market"]).lower()
    price = await get_live_price(market, trade["symbol"])
    if price is None:
        return
    with db() as con:
        con.execute("UPDATE trades SET current_price=?, updated_at=? WHERE id=? AND user_id=?",
                    (price, utc_text(), trade["id"], trade["user_id"]))
    sl = safe_float(trade["sl"], 0); tp1 = safe_float(trade["tp1"], 0); tp2 = safe_float(trade["tp2"], 0)
    long = is_long_side(trade["side"])
    if long:
        if sl > 0 and price <= sl:
            await notify_trade_touch(trade, "touch_sl", price); return
        if tp2 > 0 and price >= tp2:
            if not trade.get("tp1_hit") and tp1 > 0:
                await notify_trade_touch(trade, "touch_tp1", tp1)
            await notify_trade_touch(trade, "touch_tp2", price); return
        if tp1 > 0 and price >= tp1 and not trade.get("tp1_hit"):
            await notify_trade_touch(trade, "touch_tp1", price)
    else:
        if sl > 0 and price >= sl:
            await notify_trade_touch(trade, "touch_sl", price); return
        if tp2 > 0 and price <= tp2:
            if not trade.get("tp1_hit") and tp1 > 0:
                await notify_trade_touch(trade, "touch_tp1", tp1)
            await notify_trade_touch(trade, "touch_tp2", price); return
        if tp1 > 0 and price <= tp1 and not trade.get("tp1_hit"):
            await notify_trade_touch(trade, "touch_tp1", price)

async def background_monitor():
    log.info("Мониторинг сделок запущен")
    while True:
        try:
            expire_stale_signals()
            with db() as con:
                rows = con.execute("""SELECT * FROM trades
                    WHERE LOWER(COALESCE(status, '')) IN ('active', 'open')
                    ORDER BY opened_at, id""").fetchall()
            for row in rows:
                try:
                    await monitor_one_trade(row_to_dict(row))
                except Exception:
                    log.exception("Ошибка монитора сделки")
        except Exception:
            log.exception("Ошибка фонового мониторинга")
        await asyncio.sleep(TRADE_MONITOR_INTERVAL_SEC)

async def background_scan():
    log.info("Фоновый скан запущен (интервал %ss)", MARKET_SCAN_INTERVAL_SEC)
    await asyncio.sleep(60)
    while True:
        try:
            with db() as con:
                users = con.execute("SELECT user_id, strategy_version FROM user_settings").fetchall()
            if users:
                strategies = {str(u["strategy_version"] or DEFAULT_STRATEGY).upper() for u in users}
                for ver in strategies:
                    if ver not in (STRATEGY_A, STRATEGY_B):
                        continue
                    found = await scan_market(strategy_version=ver)
                    if found:
                        for sig in found:
                            await broadcast_signal(sig)
        except Exception:
            log.exception("Ошибка фонового скана")
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
    background_tasks.append(asyncio.create_task(background_monitor()))
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
