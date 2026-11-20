# ============================================================
# TRADING BOT v9.2 -> v9.3 UPGRADED (SINGLE-FILE EDITION)
# Strategy A (v9.2 baseline) + Strategy B (scalper structure/vol/VWAP)
# Live quote check, TTL/expiry, >0.5R entry drift guard,
# SL edit without altering historical position size/1R,
# partial fill execution ledger & fair backtest comparator.
# REAL EXCHANGE ORDER PLACEMENT IS STRICTLY DISABLED.
# ============================================================

import os
import sys
import time
import math
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
# 3. GENERAL SETTINGS & TRANSPARENT INTERVALS
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

BACKUP_DIR = Path(
    os.getenv("BACKUP_DIR", str(DB_PATH.parent / "backups"))
)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

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

MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# Базовые параметры стратегии и риск-менеджмента (не менять без согласования)
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

# Явные интервалы (в секундах): устранена неоднозначность MARKET_SCAN_INTERVAL * 60
MARKET_SCAN_INTERVAL_SEC = int(os.getenv("MARKET_SCAN_INTERVAL_SEC", "1800"))
MARKET_SCAN_INTERVAL = 60  # сохранено для обратной совместимости имени константы
TRADE_MONITOR_INTERVAL_SEC = int(os.getenv("TRADE_MONITOR_INTERVAL_SEC", "15"))
TRADE_MONITOR_INTERVAL = TRADE_MONITOR_INTERVAL_SEC

SIGNAL_COOLDOWN_MINUTES = 45
SIGNAL_TTL_MINUTES = int(os.getenv("SIGNAL_TTL_MINUTES", "20"))
MAX_ENTRY_DRIFT_R = 0.5
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "0.25"))

# Модель комиссий и спреда / проскальзывания (модельные значения при отсутствии Bid/Ask)
MAKER_FEE_RATE = float(os.getenv("MAKER_FEE_RATE", "0.0002"))     # 0.02%
TAKER_FEE_RATE = float(os.getenv("TAKER_FEE_RATE", "0.0005"))     # 0.05%
DEFAULT_SYNTHETIC_SPREAD_PCT = float(os.getenv("DEFAULT_SYNTHETIC_SPREAD_PCT", "0.04"))  # 0.04%
DEFAULT_SLIPPAGE_PCT = float(os.getenv("DEFAULT_SLIPPAGE_PCT", "0.03"))  # 0.03%

# Разделение стратегии A (эталон v9.2) и стратегии B (улучшенная)
STRATEGY_A = "A"
STRATEGY_B = "B"
DEFAULT_STRATEGY = STRATEGY_A  # Переключение на B только после явного подтверждения пользователя

# Размещение реальных ордеров через API биржи строго запрещено
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

latest_quotes: dict[
    tuple[str, str],
    dict[str, Any]
] = {}

system_status = {
    "started": False,
    "last_scan": None,
    "last_error": None,
    "api_status": "Не проверено",
    "scan_count": 0,
    "signals_count": 0,
    "source_sha256": None,
    "backup_path": None,
}

background_tasks: list[asyncio.Task] = []


# ============================================================
# 6. TIME, HASH AND FORMAT HELPERS
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
    """
    Переводит время из UTC (внутреннее хранение/расчёты) в Europe/Moscow
    исключительно для отображения в Telegram-сообщениях.
    """
    dt_utc = parse_utc_text(utc_str)
    if dt_utc is None:
        return str(utc_str or "—")
    dt_msk = dt_utc.astimezone(MOSCOW_TZ)
    return dt_msk.strftime("%Y-%m-%d %H:%M:%S")


def compute_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def freeze_source_snapshot() -> str | None:
    """
    Создаёт неизменяемую резервную копию текущего исходного файла (если файл существует)
    и возвращает его SHA-256.
    """
    try:
        src_path = Path(__file__).resolve()
        if not src_path.exists():
            return None
        sha = compute_file_sha256(src_path)
        snapshot_path = BACKUP_DIR / f"source_v9_2_{sha[:12]}.py.bak"
        if not snapshot_path.exists():
            shutil.copy2(src_path, snapshot_path)
            try:
                os.chmod(snapshot_path, 0o444)
            except OSError:
                pass
        system_status["source_sha256"] = sha
        log.info("Зафиксирован эталон исходника: %s (SHA-256=%s)", snapshot_path.name, sha)
        return sha
    except Exception:
        logging.exception("Не удалось создать резервную копию исходного файла")
        return None


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


def normalize_trade_status(status: Any) -> str:
    st = str(status or "active").strip().lower()
    if st in ("active", "open"):
        return "active"
    if st == "closed":
        return "closed"
    return st


# ============================================================
# 7. DATABASE CONNECTION, BACKUP, INSPECTION & MIGRATIONS
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


def inspect_existing_db() -> dict[str, Any]:
    """
    Изучает реальную структуру существующей SQLite БД: таблицы, колонки,
    внешние ключи, индексы, количество записей и связи между таблицами.
    """
    report: dict[str, Any] = {
        "exists": DB_PATH.exists(),
        "tables": {},
        "counts": {},
        "integrity_check": None,
        "orphan_checks": {},
    }
    if not DB_PATH.exists():
        return report

    with db() as con:
        ic = con.execute("PRAGMA integrity_check").fetchone()
        report["integrity_check"] = ic[0] if ic else "unknown"

        tables = [
            r["name"]
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        ]

        for t in tables:
            cols = [dict(r) for r in con.execute(f'PRAGMA table_info("{t}")').fetchall()]
            fks = [dict(r) for r in con.execute(f'PRAGMA foreign_key_list("{t}")').fetchall()]
            idxs = [dict(r) for r in con.execute(f'PRAGMA index_list("{t}")').fetchall()]
            cnt = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            report["tables"][t] = {
                "columns": [c["name"] for c in cols],
                "foreign_keys": fks,
                "indexes": [i["name"] for i in idxs],
            }
            report["counts"][t] = cnt

        if "trades" in tables and "users" in tables:
            report["orphan_checks"]["trades_without_user"] = con.execute(
                "SELECT COUNT(*) FROM trades WHERE user_id IS NOT NULL "
                "AND user_id NOT IN (SELECT user_id FROM users)"
            ).fetchone()[0]

        if "trades" in tables and "signals" in tables:
            report["orphan_checks"]["trades_without_signal"] = con.execute(
                "SELECT COUNT(*) FROM trades WHERE signal_id IS NOT NULL "
                "AND signal_id NOT IN (SELECT id FROM signals)"
            ).fetchone()[0]

        if "trade_events" in tables and "trades" in tables:
            report["orphan_checks"]["events_without_trade"] = con.execute(
                "SELECT COUNT(*) FROM trade_events WHERE trade_id NOT IN (SELECT id FROM trades)"
            ).fetchone()[0]

    return report


def backup_and_verify_db() -> Path | None:
    """
    Создаёт резервную копию trading_bot_v9.db перед миграциями,
    проверяет целостность резервной копии и возможность чтения/восстановления.
    """
    if not DB_PATH.exists():
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
        verify_con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    finally:
        verify_con.close()

    system_status["backup_path"] = str(backup_file)
    log.info("Резервная копия БД создана и проверена: %s", backup_file)
    return backup_file


def restore_db_from_backup(backup_file: Path) -> None:
    """
    Восстанавливает БД из проверенной резервной копии.
    """
    if not backup_file.exists():
        raise FileNotFoundError(f"Файл резервной копии не найден: {backup_file}")

    verify_con = sqlite3.connect(str(backup_file), timeout=30)
    try:
        check = verify_con.execute("PRAGMA integrity_check").fetchone()
        if not check or str(check[0]).lower() != "ok":
            raise RuntimeError(f"Повреждённый бэкап, восстановление отменено: {check}")
    finally:
        verify_con.close()

    src_con = sqlite3.connect(str(backup_file), timeout=30)
    dst_con = sqlite3.connect(str(DB_PATH), timeout=30)
    try:
        src_con.backup(dst_con)
    finally:
        dst_con.close()
        src_con.close()

    log.info("БД восстановлена из резервной копии: %s", backup_file)


# ============================================================
# 8. DATABASE INITIALIZATION & MIGRATIONS
# ============================================================

def init_db() -> None:

    if DB_PATH.exists():
        backup_and_verify_db()
        info = inspect_existing_db()
        log.info(
            "Инспекция БД перед миграцией: tables=%s, counts=%s, integrity=%s",
            list(info.get("tables", {}).keys()),
            info.get("counts", {}),
            info.get("integrity_check"),
        )

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
                strategy_version TEXT DEFAULT 'A',
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
                status TEXT DEFAULT 'ACTIVE',
                result TEXT,
                strategy_version TEXT DEFAULT 'A',
                live_price REAL,
                bid REAL,
                ask REAL,
                spread_pct REAL,
                price_updated_at TEXT,
                expires_at TEXT,
                entry_mode TEXT DEFAULT 'market',
                entry_zone_low REAL,
                entry_zone_high REAL,
                confirmation_rule TEXT,
                quality_score REAL,
                market_regime TEXT,
                estimated_fee_pct REAL,
                estimated_slippage_pct REAL,
                net_rr REAL,
                cancel_reason TEXT,
                updated_at TEXT
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
                signal_entry REAL,
                entry_drift_r REAL DEFAULT 0,
                current_price REAL,
                sl REAL,
                tp1 REAL,
                tp2 REAL,
                initial_sl REAL,
                risk_amount REAL,
                initial_risk_amount REAL,
                current_risk_amount REAL,
                current_risk_r REAL,
                remaining_percent REAL DEFAULT 100,
                tp1_hit INTEGER DEFAULT 0,
                tp2_hit INTEGER DEFAULT 0,
                tp1_fill_price REAL,
                tp1_fill_qty REAL,
                tp1_fill_fee REAL DEFAULT 0,
                tp1_closed_at TEXT,
                tp2_fill_price REAL,
                tp2_fill_qty REAL,
                tp2_fill_fee REAL DEFAULT 0,
                tp2_closed_at TEXT,
                total_fees_paid REAL DEFAULT 0,
                total_slippage_cost REAL DEFAULT 0,
                strategy_version TEXT DEFAULT 'A',
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
                net_pnl_usdt REAL DEFAULT 0,
                initial_risk_amount REAL DEFAULT 0,
                total_fees_paid REAL DEFAULT 0,
                total_slippage_cost REAL DEFAULT 0,
                strategy_version TEXT DEFAULT 'A',
                close_reason TEXT,
                opened_at TEXT,
                closed_at TEXT,
                FOREIGN KEY(trade_id) REFERENCES trades(id),
                FOREIGN KEY(user_id) REFERENCES users(user_id)
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

        con.execute("""
            CREATE TABLE IF NOT EXISTS trade_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                exec_type TEXT NOT NULL,
                price REAL NOT NULL,
                quantity REAL NOT NULL,
                share_percent REAL NOT NULL,
                fee_usdt REAL DEFAULT 0,
                slippage_usdt REAL DEFAULT 0,
                realized_pnl_usdt REAL DEFAULT 0,
                executed_at TEXT NOT NULL,
                FOREIGN KEY(trade_id) REFERENCES trades(id),
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS signal_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                old_status TEXT,
                new_status TEXT,
                price REAL,
                reason TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(signal_id) REFERENCES signals(id)
            )
        """)

        con.execute("""
            CREATE TABLE IF NOT EXISTS user_dialogs (
                user_id INTEGER PRIMARY KEY,
                state_type TEXT NOT NULL,
                ref_id INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        """)

        migrations = {
            "users": {
                "username": "TEXT",
                "created_at": "TEXT",
                "is_active": "INTEGER DEFAULT 1",
            },
            "user_settings": {
                "risk_percent": "REAL DEFAULT 2.0",
                "leverage": "INTEGER DEFAULT 5",
                "notifications": "INTEGER DEFAULT 1",
                "strategy_version": "TEXT DEFAULT 'A'",
            },
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
                "status": "TEXT DEFAULT 'ACTIVE'",
                "result": "TEXT",
                "strategy_version": "TEXT DEFAULT 'A'",
                "live_price": "REAL",
                "bid": "REAL",
                "ask": "REAL",
                "spread_pct": "REAL",
                "price_updated_at": "TEXT",
                "expires_at": "TEXT",
                "entry_mode": "TEXT DEFAULT 'market'",
                "entry_zone_low": "REAL",
                "entry_zone_high": "REAL",
                "confirmation_rule": "TEXT",
                "quality_score": "REAL",
                "market_regime": "TEXT",
                "estimated_fee_pct": "REAL",
                "estimated_slippage_pct": "REAL",
                "net_rr": "REAL",
                "cancel_reason": "TEXT",
                "updated_at": "TEXT",
            },
            "trades": {
                "user_id": "INTEGER",
                "signal_id": "INTEGER",
                "market": "TEXT",
                "symbol": "TEXT",
                "side": "TEXT",
                "entry": "REAL",
                "actual_entry": "REAL",
                "signal_entry": "REAL",
                "entry_drift_r": "REAL DEFAULT 0",
                "current_price": "REAL",
                "sl": "REAL",
                "tp1": "REAL",
                "tp2": "REAL",
                "initial_sl": "REAL",
                "risk_amount": "REAL",
                "initial_risk_amount": "REAL",
                "current_risk_amount": "REAL",
                "current_risk_r": "REAL",
                "remaining_percent": "REAL DEFAULT 100",
                "tp1_hit": "INTEGER DEFAULT 0",
                "tp2_hit": "INTEGER DEFAULT 0",
                "tp1_fill_price": "REAL",
                "tp1_fill_qty": "REAL",
                "tp1_fill_fee": "REAL DEFAULT 0",
                "tp1_closed_at": "TEXT",
                "tp2_fill_price": "REAL",
                "tp2_fill_qty": "REAL",
                "tp2_fill_fee": "REAL DEFAULT 0",
                "tp2_closed_at": "TEXT",
                "total_fees_paid": "REAL DEFAULT 0",
                "total_slippage_cost": "REAL DEFAULT 0",
                "strategy_version": "TEXT DEFAULT 'A'",
                "status": "TEXT DEFAULT 'active'",
                "last_recommendation": "TEXT",
                "opened_at": "TEXT",
                "closed_at": "TEXT",
                "updated_at": "TEXT",
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
                "net_pnl_usdt": "REAL DEFAULT 0",
                "initial_risk_amount": "REAL DEFAULT 0",
                "total_fees_paid": "REAL DEFAULT 0",
                "total_slippage_cost": "REAL DEFAULT 0",
                "strategy_version": "TEXT DEFAULT 'A'",
                "close_reason": "TEXT",
                "opened_at": "TEXT",
                "closed_at": "TEXT",
            },
        }

        for table, columns in migrations.items():
            for column, definition in columns.items():
                ensure_column(con, table, column, definition)

        con.execute("""
            UPDATE trades
            SET status = LOWER(status)
            WHERE status IN ('ACTIVE', 'OPEN', 'CLOSED')
        """)

        con.execute("""
            UPDATE trades
            SET signal_entry = COALESCE(signal_entry, entry, actual_entry)
            WHERE signal_entry IS NULL
        """)
        con.execute("""
            UPDATE trades
            SET initial_risk_amount = COALESCE(initial_risk_amount, risk_amount, 0)
            WHERE initial_risk_amount IS NULL
        """)
        con.execute("""
            UPDATE trades
            SET current_risk_amount = COALESCE(current_risk_amount, risk_amount, 0)
            WHERE current_risk_amount IS NULL
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
            idx_signals_status_exp
            ON signals(status, expires_at)
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

        con.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_executions_trade
            ON trade_executions(trade_id, executed_at)
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
                    1.2 - (now - _last_api_request),
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
# 12. GET MARKET CANDLES (WITH SMART TF CACHE)
# ============================================================

def _is_cached_frame_fresh(
    market: str,
    symbol: str,
    interval: str,
    min_rows: int
) -> pd.DataFrame | None:
    cached = candle_cache.get((market, symbol, interval))
    if cached is None or cached.empty or len(cached) < min_rows:
        return None
    last_open = int(cached.iloc[-1]["open_time"])
    tf_ms = TF_MS.get(interval, 60_000)
    if now_ms() < last_open + tf_ms * 2:
        return cached.copy()
    return None


async def get_klines(
    market: str,
    symbol: str,
    interval: str,
    limit: int = KLINE_LIMIT,
    min_rows: int = MIN_CANDLE_ROWS,
    session: aiohttp.ClientSession | None = None,
    use_cache: bool = True
) -> pd.DataFrame:

    if use_cache:
        fresh = _is_cached_frame_fresh(market, symbol, interval, min_rows)
        if fresh is not None:
            return fresh

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
# 14. TECHNICAL INDICATORS (BASE + STRATEGY B EXTRAS)
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


def enrich_scalper_series(df: pd.DataFrame) -> pd.DataFrame:
    """
    Дополнительные ряды только для Стратегии B:
    объёмное среднее/всплеск, скользящий VWAP по окну (rolling 48 баров) и локальные свинги.
    """
    x = indicators(df)
    vol = x["volume"].replace(0, np.nan)
    x["vol_ma20"] = vol.rolling(20, min_periods=5).mean().fillna(0)
    x["vol_ratio"] = (x["volume"] / x["vol_ma20"].replace(0, np.nan)).fillna(1.0)

    typical_price = (x["high"] + x["low"] + x["close"]) / 3.0
    pv = typical_price * x["volume"]
    cum_pv = pv.rolling(48, min_periods=10).sum()
    cum_v = x["volume"].rolling(48, min_periods=10).sum().replace(0, np.nan)
    x["vwap"] = (cum_pv / cum_v).fillna(x["close"])

    x["swing_high_prev"] = x["high"].rolling(5, min_periods=3).max().shift(1)
    x["swing_low_prev"] = x["low"].rolling(5, min_periods=3).min().shift(1)
    x["range_high_20"] = x["high"].rolling(20, min_periods=10).max().shift(1)
    x["range_low_20"] = x["low"].rolling(20, min_periods=10).min().shift(1)
    return x


# ============================================================
# 15. STRATEGY A (UNCHANGED v9.2 BASELINE) & STRATEGY B
# ============================================================

def build_signal_a_v92(
    market: str,
    symbol: str,
    frames: dict[str, pd.DataFrame]
) -> dict[str, Any] | None:
    """
    Точная эталонная реализация сигнальной логики Trading Bot v9.2 (Стратегия A).
    Не изменять формулы, индикаторы, пороги и правила.
    """
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
        "strategy_version": STRATEGY_A,
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


def _detect_market_regime(m15_row: pd.Series, m15_prev: pd.Series, atr: float, price: float) -> str:
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


def build_signal_b(
    market: str,
    symbol: str,
    frames: dict[str, pd.DataFrame],
    quote: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """
    Улучшенная стратегия B (изолирована от Стратегии A):
    режим рынка, уровни поддержки/сопротивления, BOS/CHoCH, sweep/ложный пробой,
    объёмы и аномалии, VWAP, фильтр перерастянутого импульса, учёт издержек и качества.
    """
    required_frames = ("5m", "15m", "1h", "4h")

    if any(
        timeframe not in frames
        or frames[timeframe] is None
        or len(frames[timeframe]) < MIN_CANDLE_ROWS
        for timeframe in required_frames
    ):
        return None

    m5 = enrich_scalper_series(frames["5m"])
    m15 = enrich_scalper_series(frames["15m"])
    h1 = enrich_scalper_series(frames["1h"])
    h4 = enrich_scalper_series(frames["4h"])

    a = m5.iloc[-1]
    b = m15.iloc[-1]
    b_prev = m15.iloc[-2]
    c = h1.iloc[-1]
    d = h4.iloc[-1]

    price = safe_float(b["close"])
    atr = safe_float(b["atr"])
    if price <= 0 or atr <= 0:
        return None

    regime = _detect_market_regime(b, b_prev, atr, price)

    extension_atr = abs(price - safe_float(b["ema20"])) / atr
    if extension_atr > 3.2 and regime == "impulse":
        return None

    long_score = 0.0
    short_score = 0.0
    long_reasons: list[str] = []
    short_reasons: list[str] = []

    for row, label in ((c, "1H"), (d, "4H")):
        if row["close"] > row["ema200"] and row["ema20"] > row["ema50"]:
            long_score += 2.0
            long_reasons.append(f"Контекст {label}: бычий тренд выше EMA200")
        if row["close"] < row["ema200"] and row["ema20"] < row["ema50"]:
            short_score += 2.0
            short_reasons.append(f"Контекст {label}: медвежий тренд ниже EMA200")

    prev_sh = safe_float(b_prev.get("swing_high_prev"))
    prev_sl = safe_float(b_prev.get("swing_low_prev"))
    b_close = safe_float(b["close"])
    b_high = safe_float(b["high"])
    b_low = safe_float(b["low"])

    if prev_sh > 0 and b_close > prev_sh:
        if safe_float(b["ema20"]) >= safe_float(b["ema50"]):
            long_score += 1.5
            long_reasons.append("15м BOS вверх: пробой локального максимума")
        else:
            long_score += 1.0
            long_reasons.append("15м CHoCH вверх: смена локальной структуры")

    if prev_sl > 0 and b_close < prev_sl:
        if safe_float(b["ema20"]) <= safe_float(b["ema50"]):
            short_score += 1.5
            short_reasons.append("15м BOS вниз: пробой локального минимума")
        else:
            short_score += 1.0
            short_reasons.append("15м CHoCH вниз: смена локальной структуры")

    rng_high = safe_float(b.get("range_high_20"))
    rng_low = safe_float(b.get("range_low_20"))
    if rng_low > 0 and b_low < rng_low and b_close > rng_low:
        long_score += 1.5
        long_reasons.append("Sweep ликвидности ниже диапазона и возврат вверх")
    if rng_high > 0 and b_high > rng_high and b_close < rng_high:
        short_score += 1.5
        short_reasons.append("Sweep ликвидности выше диапазона и возврат вниз")

    vwap_15 = safe_float(b.get("vwap"), price)
    if b_close > vwap_15:
        long_score += 1.0
        long_reasons.append("15м: выше VWAP")
    elif b_close < vwap_15:
        short_score += 1.0
        short_reasons.append("15м: ниже VWAP")

    vol_ratio = safe_float(b.get("vol_ratio"), 1.0)
    if vol_ratio >= 1.4:
        if b_close > safe_float(b["open"]):
            long_score += 1.0
            long_reasons.append(f"Объём x{vol_ratio:.1f} на бычьем баре")
        elif b_close < safe_float(b["open"]):
            short_score += 1.0
            short_reasons.append(f"Объём x{vol_ratio:.1f} на медвежьем баре")

    if safe_float(a["close"]) > safe_float(a["ema20"]):
        long_score += 1.0
        long_reasons.append("5м: выше EMA20")
    if safe_float(a["close"]) < safe_float(a["ema20"]):
        short_score += 1.0
        short_reasons.append("5м: ниже EMA20")

    if long_score > short_score:
        side = "BUY"
        raw_score = long_score
        reasons = long_reasons
    else:
        side = "SELL"
        raw_score = short_score
        reasons = short_reasons

    score_int = int(round(raw_score))
    if score_int < MIN_SCORE or abs(long_score - short_score) < 1.5:
        return None

    if market == "spot" and side == "SELL":
        return None

    sl_dist = max(1.5 * atr, price * 0.0025)
    min_sl_dist = price * 0.002
    max_sl_dist = max(3.5 * atr, price * 0.025)
    if sl_dist < min_sl_dist or sl_dist > max_sl_dist:
        return None

    tp1_dist = sl_dist * TP1_R
    tp2_dist = sl_dist * TP2_R

    if side == "BUY":
        sl = price - sl_dist
        tp1 = price + tp1_dist
        tp2 = price + tp2_dist
        entry_mode = "level" if (extension_atr > 1.8 or regime == "exhaustion") else "market"
        zone_low = max(sl * 1.002, min(price, vwap_15))
        zone_high = price
        conf_rule = "Дождаться удержания уровня/зоны и подтверждения на 5м/15м перед входом."
    else:
        sl = price + sl_dist
        tp1 = price - tp1_dist
        tp2 = price - tp2_dist
        entry_mode = "level" if (extension_atr > 1.8 or regime == "exhaustion") else "market"
        zone_low = price
        zone_high = min(sl * 0.998, max(price, vwap_15))
        conf_rule = "Дождаться теста зоны сопротивления/VWAP и реакции продавцов на 5м/15м."

    if min(sl, tp1, tp2) <= 0:
        return None

    gross_rr = abs(tp2 - price) / abs(price - sl)
    if gross_rr < MIN_RR:
        return None

    spread_pct = safe_float((quote or {}).get("spread_pct"), DEFAULT_SYNTHETIC_SPREAD_PCT)
    fee_pct = (TAKER_FEE_RATE * 2.0) * 100.0
    slip_pct = DEFAULT_SLIPPAGE_PCT * 2.0
    cost_dist = price * ((spread_pct + fee_pct + slip_pct) / 100.0)
    eff_risk_dist = max(abs(price - sl) + cost_dist * 0.5, 1e-9)
    eff_reward_dist = max(abs(tp2 - price) - cost_dist, 0.0)
    net_rr = eff_reward_dist / eff_risk_dist

    if net_rr < MIN_RR:
        return None

    quality_score = round(min(100.0, raw_score * 11.0 + (10.0 if net_rr >= 2.0 else 0.0) - spread_pct * 20.0), 1)

    return {
        "strategy_version": STRATEGY_B,
        "market": market,
        "symbol": symbol,
        "side": side,
        "entry": price,
        "sl": sl,
        "tp1": tp1,
        "tp2": tp2,
        "tp": tp2,
        "rr": gross_rr,
        "net_rr": net_rr,
        "score": score_int,
        "quality_score": quality_score,
        "market_regime": regime,
        "entry_mode": entry_mode,
        "entry_zone_low": min(zone_low, zone_high),
        "entry_zone_high": max(zone_low, zone_high),
        "confirmation_rule": conf_rule if entry_mode == "level" else "Рыночный вход с проверкой фактической цены и отклонения <= 0.5R.",
        "spread_pct": spread_pct,
        "estimated_fee_pct": fee_pct,
        "estimated_slippage_pct": slip_pct,
        "reasons": reasons,
        "atr": atr,
        "setup_key": (
            f"B:{market}:{symbol}:{side}:"
            f"{int(b['open_time'])}"
        )
    }


def verify_strategy_a_unchanged(sample_frames: dict[str, pd.DataFrame] | None = None) -> dict[str, Any]:
    """
    Проверяет детерминированность и воспроизводимость Стратегии A (v9.2)
    на заданном наборе свечей (без обращения к сети).
    """
    if not sample_frames:
        return {"checked": False, "reason": "no_sample_frames"}

    res1 = build_signal_a_v92("swap", "BTC-USDT", sample_frames)
    res2 = build_signal_a_v92("swap", "BTC-USDT", sample_frames)
    identical = (res1 == res2)
    return {"checked": True, "identical_on_repeat": identical, "signal": res1}


# ============================================================
# 16. LIVE QUOTE, SIGNAL LIFECYCLE & SIGNAL STORAGE
# ============================================================

async def fetch_live_market_snapshot(market: str, symbol: str) -> dict[str, Any] | None:
    """
    Получает актуальную цену с BingX (и Bid/Ask/спред, если они доступны в ответе).
    """
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
        "price": price,
        "bid": bid,
        "ask": ask,
        "spread_pct": spread_pct,
        "is_synthetic_spread": is_synthetic,
        "updated_at": utc_text(),
    }
    latest_quotes[(market, symbol)] = snap
    return snap


def enrich_signal_with_live_data(
    sig: dict[str, Any],
    snapshot: dict[str, Any] | None
) -> dict[str, Any]:
    """
    Актуализирует цену сигнала перед отправкой, задаёт TTL и контролирует допустимое отклонение.
    """
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

        base_entry = safe_float(enriched["entry"])
        sl = safe_float(enriched["sl"])
        risk_dist = abs(base_entry - sl)
        drift_r = (abs(live_p - base_entry) / risk_dist) if risk_dist > 0 else 0.0

        if drift_r > MAX_ENTRY_DRIFT_R * 2.0 and enriched.get("entry_mode") == "market":
            enriched["status"] = "CANCELLED"
            enriched["cancel_reason"] = f"Цена ушла слишком далеко от сигнала ({drift_r:.2f}R)"
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
                enriched["rr"] = abs(enriched["tp2"] - exec_entry) / abs(exec_entry - enriched["sl"])
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


def log_signal_event(
    signal_id: int,
    event_type: str,
    old_status: str | None = None,
    new_status: str | None = None,
    price: float | None = None,
    reason: str = ""
) -> None:
    with db() as con:
        con.execute("""
            INSERT INTO signal_events (
                signal_id, event_type, old_status, new_status,
                price, reason, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            signal_id,
            event_type,
            old_status,
            new_status,
            safe_float(price, 0.0) if price is not None else None,
            reason,
            utc_text()
        ))


def save_signal(
    sig: dict[str, Any]
) -> int | None:

    if sig.get("status") == "CANCELLED":
        return None

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
              AND COALESCE(strategy_version, 'A')=?
              AND julianday(created_at)
                  >= julianday('now', '-{SIGNAL_COOLDOWN_MINUTES} minutes')
            ORDER BY id DESC
            LIMIT 1
        """, (
            sig["market"],
            sig["symbol"],
            sig["side"],
            sig.get("strategy_version", STRATEGY_A)
        )).fetchone()

        if recent:
            return None

        values = {
            "created_at": now,
            "updated_at": now,
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
            "status": sig.get("status", "ACTIVE"),
            "strategy_version": sig.get("strategy_version", STRATEGY_A),
            "live_price": sig.get("live_price", sig["entry"]),
            "bid": sig.get("bid"),
            "ask": sig.get("ask"),
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
        signal_id = int(cur.lastrowid)

    log_signal_event(
        signal_id=signal_id,
        event_type="created",
        old_status=None,
        new_status=values.get("status", "ACTIVE"),
        price=safe_float(sig.get("live_price", sig["entry"])),
        reason=f"Стратегия {values.get('strategy_version', 'A')}, режим {values.get('entry_mode', 'market')}"
    )

    return signal_id


def expire_stale_signals() -> list[dict[str, Any]]:
    """
    Помечает просроченные сигналы (по expires_at) как EXPIRED и пишет в signal_events.
    """
    expired_items: list[dict[str, Any]] = []
    now_dt = utc_now()

    with db() as con:
        rows = con.execute("""
            SELECT id, symbol, market, side, status, expires_at
            FROM signals
            WHERE UPPER(COALESCE(status, '')) IN ('ACTIVE', 'WAITING_LEVEL', 'NEW')
              AND expires_at IS NOT NULL
        """).fetchall()

        for r in rows:
            exp_dt = parse_utc_text(r["expires_at"])
            if exp_dt and now_dt >= exp_dt:
                old_st = r["status"]
                con.execute("""
                    UPDATE signals
                    SET status = 'EXPIRED',
                        cancel_reason = 'Истёк срок действия сигнала (TTL)',
                        updated_at = ?
                    WHERE id = ?
                """, (utc_text(now_dt), r["id"]))
                expired_items.append(dict(r))

    for item in expired_items:
        log_signal_event(
            signal_id=item["id"],
            event_type="expired",
            old_status=item["status"],
            new_status="EXPIRED",
            reason="Истёк срок действия сигнала (TTL)"
        )

    return expired_items


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
# 18. USER STATES (DB + MEMORY) & DATABASE HELPERS
# ============================================================

user_states: dict[int, dict[str, Any]] = {}


def set_user_dialog_state(user_id: int, state_type: str, ref_id: int | None = None) -> None:
    user_states[user_id] = {"type": state_type, "ref_id": ref_id, "signal_id": ref_id, "trade_id": ref_id}
    with db() as con:
        con.execute("""
            INSERT INTO user_dialogs (user_id, state_type, ref_id, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                state_type = excluded.state_type,
                ref_id = excluded.ref_id,
                created_at = excluded.created_at
        """, (user_id, state_type, ref_id, utc_text()))


def clear_user_dialog_state(user_id: int) -> None:
    user_states.pop(user_id, None)
    with db() as con:
        con.execute("DELETE FROM user_dialogs WHERE user_id = ?", (user_id,))


def get_user_dialog_state(user_id: int) -> dict[str, Any] | None:
    if user_id in user_states:
        return user_states[user_id]
    with db() as con:
        row = con.execute(
            "SELECT state_type, ref_id FROM user_dialogs WHERE user_id = ?",
            (user_id,)
        ).fetchone()
    if not row:
        return None
    st = {
        "type": row["state_type"],
        "ref_id": row["ref_id"],
        "signal_id": row["ref_id"],
        "trade_id": row["ref_id"],
    }
    user_states[user_id] = st
    return st


def get_user_strategy(user_id: int) -> str:
    with db() as con:
        row = con.execute(
            "SELECT strategy_version FROM user_settings WHERE user_id = ?",
            (user_id,)
        ).fetchone()
    if not row:
        return DEFAULT_STRATEGY
    ver = str(row["strategy_version"] or DEFAULT_STRATEGY).upper()
    return ver if ver in (STRATEGY_A, STRATEGY_B) else DEFAULT_STRATEGY


def set_user_strategy(user_id: int, version: str) -> str:
    ver = str(version).upper()
    if ver not in (STRATEGY_A, STRATEGY_B):
        raise ValueError("Неизвестная версия стратегии")
    with db() as con:
        con.execute("""
            INSERT INTO user_settings (user_id, risk_percent, leverage, notifications, strategy_version)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(user_id) DO UPDATE SET strategy_version = excluded.strategy_version
        """, (user_id, RISK_PERCENT, FUTURES_LEVERAGE, ver))
    return ver


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
    Совместимость со старой схемой user_balances и trade_events.
    Вызывается после init_db() без удаления данных.
    """
    with db() as conn:
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
                conn.execute("ALTER TABLE user_balances RENAME TO user_balances_old")
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


def save_trade_execution(
    trade_id: int,
    user_id: int,
    exec_type: str,
    price: float,
    quantity: float,
    share_percent: float,
    fee_usdt: float = 0.0,
    slippage_usdt: float = 0.0,
    realized_pnl_usdt: float = 0.0,
    executed_at: str | None = None
) -> None:
    with db() as conn:
        conn.execute("""
            INSERT INTO trade_executions (
                trade_id, user_id, exec_type, price, quantity,
                share_percent, fee_usdt, slippage_usdt,
                realized_pnl_usdt, executed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade_id,
            user_id,
            exec_type,
            safe_float(price),
            safe_float(quantity),
            safe_float(share_percent),
            safe_float(fee_usdt),
            safe_float(slippage_usdt),
            safe_float(realized_pnl_usdt),
            executed_at or utc_text(),
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
                user_id, risk_percent, leverage, notifications, strategy_version
            )
            VALUES (?, ?, ?, 1, ?)
        """, (
            user.id,
            RISK_PERCENT,
            FUTURES_LEVERAGE,
            DEFAULT_STRATEGY
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
# 19. POSITION SIZE, RISK & DRIFT CALCULATION
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


def assess_entry_drift(
    signal_entry: float,
    signal_sl: float,
    tp2: float,
    actual_entry: float
) -> dict[str, Any]:
    """
    Оценивает отклонение фактической цены входа от сигнальной в единицах 1R.
    При отклонении > 0.5R выдаёт предупреждение и пересчитанный RR.
    """
    one_r = abs(signal_entry - signal_sl)
    drift_abs = abs(actual_entry - signal_entry)
    drift_r = (drift_abs / one_r) if one_r > 0 else 0.0
    new_rr = (abs(tp2 - actual_entry) / one_r) if one_r > 0 else 0.0
    exceeds_threshold = drift_r > MAX_ENTRY_DRIFT_R
    unfavorable = exceeds_threshold and (new_rr < MIN_RR or drift_r > 1.0)

    return {
        "drift_r": drift_r,
        "exceeds_0_5r": exceeds_threshold,
        "unfavorable": unfavorable,
        "recalculated_rr": new_rr,
    }


# ============================================================
# 20. PERSONAL TRADE CREATION
# ============================================================

def create_personal_trade(user_id, signal_id, actual_entry):
    actual_entry = safe_float(actual_entry, 0)

    if actual_entry <= 0:
        return None, "Цена входа должна быть больше нуля.", None

    with db() as conn:
        signal_row = conn.execute("""
            SELECT * FROM signals WHERE id = ?
        """, (signal_id,)).fetchone()

        if not signal_row:
            return None, "Сигнал не найден или уже недоступен.", None

        signal = row_to_dict(signal_row)
        sig_status = str(signal.get("status") or "").upper()
        if sig_status in ("EXPIRED", "CANCELLED"):
            return None, f"Сигнал неактуален (статус: {sig_status}). Вход не открыт.", None

        active_count = conn.execute("""
            SELECT COUNT(*) FROM trades
            WHERE user_id = ?
              AND LOWER(COALESCE(status, '')) IN ('active', 'open')
        """, (user_id,)).fetchone()[0]

        if active_count >= MAX_ACTIVE_TRADES:
            return None, (
                f"У тебя уже есть {MAX_ACTIVE_TRADES} активных сделок. "
                "Сначала закрой одну из них."
            ), None

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
            ), None

        balance = safe_float(balance_row["balance"], 0)

        if balance <= 0:
            return None, "Сначала укажи корректный баланс в настройках.", None

        signal_entry = safe_float(signal["entry"], 0)
        signal_sl = safe_float(signal["sl"], 0)

        if signal_entry <= 0 or signal_sl <= 0:
            return None, "В сигнале отсутствуют корректные цены.", None

        stop_distance = abs(signal_entry - signal_sl)

        if stop_distance <= 0:
            return None, "Невозможно рассчитать дистанцию стоп-лосса.", None

        if is_long_side(side):
            actual_sl = actual_entry - stop_distance
            tp1 = actual_entry + stop_distance * TP1_R
            tp2 = actual_entry + stop_distance * TP2_R
        else:
            actual_sl = actual_entry + stop_distance
            tp1 = actual_entry - stop_distance * TP1_R
            tp2 = actual_entry - stop_distance * TP2_R

        if min(actual_sl, tp1, tp2) <= 0:
            return None, "При указанной цене входа уровни SL/TP выходят в некорректную зону.", None

        drift_info = assess_entry_drift(signal_entry, signal_sl, tp2, actual_entry)

        sizing = calculate_position_size(
            balance,
            actual_entry,
            actual_sl,
            market
        )

        entry_fee = sizing["notional"] * TAKER_FEE_RATE
        now = utc_text()

        cursor = conn.execute("""
            INSERT INTO trades (
                user_id, signal_id, market, symbol, side,
                entry, actual_entry, signal_entry, entry_drift_r,
                current_price, sl, tp1, tp2, initial_sl,
                risk_amount, initial_risk_amount,
                current_risk_amount, current_risk_r,
                position_size, notional, margin_required,
                balance_snapshot, remaining_percent,
                tp1_hit, tp2_hit, total_fees_paid,
                strategy_version, status, last_recommendation,
                opened_at, updated_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, 1.0, ?, ?, ?, ?, 100, 0, 0, ?, ?,
                'active', ?, ?, ?
            )
        """, (
            user_id, signal_id, market, symbol, side,
            actual_entry, actual_entry, signal_entry, drift_info["drift_r"],
            actual_entry, actual_sl, tp1, tp2, actual_sl,
            sizing["risk_amount"], sizing["risk_amount"], sizing["risk_amount"],
            sizing["quantity"], sizing["notional"], sizing["margin_required"],
            balance, entry_fee,
            signal.get("strategy_version", STRATEGY_A),
            "Сделка открыта пользователем",
            now, now
        ))

        trade_id = int(cursor.lastrowid)

        conn.execute("""
            UPDATE signals
            SET status = 'ENTERED', updated_at = ?
            WHERE id = ?
        """, (now, signal_id))

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

        conn.execute("""
            INSERT INTO trade_executions (
                trade_id, user_id, exec_type, price, quantity,
                share_percent, fee_usdt, slippage_usdt,
                realized_pnl_usdt, executed_at
            )
            VALUES (?, ?, 'entry', ?, ?, 100.0, ?, 0.0, 0.0, ?)
        """, (
            trade_id, user_id, actual_entry,
            sizing["quantity"], entry_fee, now
        ))

    warning_lines: list[str] = []
    if drift_info["exceeds_0_5r"]:
        warning_lines.append(
            f"⚠️ Отклонение входа от сигнала составило {drift_info['drift_r']:.2f}R (> 0.5R). "
            f"Пересчитанный RR до TP2: {drift_info['recalculated_rr']:.2f}."
        )
        if drift_info["unfavorable"]:
            warning_lines.append(
                "⛔ Вход стал невыгодным относительно риска/целей — при возможности лучше пропустить такую сделку."
            )

    return trade_id, None, ("\n".join(warning_lines) if warning_lines else None)


# ============================================================
# 21. TRADE UPDATE FUNCTIONS (PRESERVING HISTORICAL SIZE & 1R)
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

    if normalize_trade_status(trade.get("status")) not in ("active", "open"):
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
        sig_entry = safe_float(trade.get("signal_entry"), old_entry)
        drift_info = assess_entry_drift(sig_entry, new_sl, new_tp2, value)

        with db() as conn:
            conn.execute("""
                UPDATE trades
                SET actual_entry = ?, entry = ?, current_price = ?,
                    sl = ?, initial_sl = ?, tp1 = ?, tp2 = ?,
                    entry_drift_r = ?,
                    risk_amount = ?, initial_risk_amount = ?,
                    current_risk_amount = ?, current_risk_r = 1.0,
                    position_size = ?, notional = ?, margin_required = ?,
                    updated_at = ?
                WHERE id = ? AND user_id = ?
            """, (
                value, value, value,
                new_sl, new_sl, new_tp1, new_tp2,
                drift_info["drift_r"],
                sizing["risk_amount"], sizing["risk_amount"], sizing["risk_amount"],
                sizing["quantity"], sizing["notional"], sizing["margin_required"],
                utc_text(),
                trade_id, user_id
            ))

            conn.execute("""
                DELETE FROM trade_events
                WHERE trade_id = ? AND user_id = ?
                  AND event_type IN ('touch_tp1', 'touch_tp2', 'touch_sl')
            """, (trade_id, user_id))

        save_trade_event(
            trade_id, user_id, "entry_updated",
            "Пользователь исправил цену входа", value
        )

        msg = "Цена входа и торговые уровни пересчитаны."
        if drift_info["exceeds_0_5r"]:
            msg += (
                f"\n⚠️ Отклонение от сигнала: {drift_info['drift_r']:.2f}R (> 0.5R), "
                f"RR: {drift_info['recalculated_rr']:.2f}."
            )
        return True, msg

    if field == "sl":
        entry = safe_float(trade["actual_entry"], 0)

        if is_long_side(trade["side"]) and value >= entry:
            return False, "Для LONG стоп-лосс должен быть ниже входа."

        if not is_long_side(trade["side"]) and value <= entry:
            return False, "Для SHORT стоп-лосс должен быть выше входа."

        remaining_pct = safe_float(trade.get("remaining_percent"), 100.0)
        rem_frac = max(0.0, min(1.0, remaining_pct / 100.0))
        full_qty = safe_float(trade.get("position_size"), 0.0)
        rem_qty = full_qty * rem_frac
        new_current_risk = rem_qty * abs(entry - value)

        base_one_r = safe_float(trade.get("initial_risk_amount"), 0.0)
        if base_one_r <= 0:
            base_one_r = safe_float(trade.get("risk_amount"), 0.0)
        new_current_risk_r = (new_current_risk / base_one_r) if base_one_r > 0 else 0.0

        with db() as conn:
            conn.execute("""
                UPDATE trades
                SET sl = ?,
                    current_risk_amount = ?,
                    current_risk_r = ?,
                    updated_at = ?
                WHERE id = ? AND user_id = ?
            """, (
                value,
                new_current_risk,
                new_current_risk_r,
                utc_text(),
                trade_id, user_id
            ))

        save_trade_event(
            trade_id, user_id, "sl_updated",
            "Пользователь изменил стоп-лосс (размер позиции и 1R сохранены)", value
        )

        return True, (
            f"Стоп-лосс обновлён. Текущий риск остатка: {new_current_risk:.4f} USDT "
            f"({new_current_risk_r:.2f}R). Объём позиции и исходный 1R сохранены."
        )

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

    init_risk = safe_float(trade.get("initial_risk_amount"), safe_float(trade.get("risk_amount"), 0))
    curr_risk = safe_float(trade.get("current_risk_amount"), init_risk)
    quantity = safe_float(trade.get("position_size"), 0)
    notional = safe_float(trade.get("notional"), 0)
    margin = safe_float(trade.get("margin_required"), 0)
    fees = safe_float(trade.get("total_fees_paid"), 0)

    remaining = safe_float(trade.get("remaining_percent"), 100)

    return (
        f"📊 <b>{trade['symbol']}</b> [Стратегия {trade.get('strategy_version', 'A')}]\n"
        f"Рынок: {market}\n"
        f"Направление: {side}\n"
        f"Статус: {trade['status']}\n\n"
        f"Цена входа: <code>{format_price(entry)}</code>\n"
        f"Текущая цена: <code>{format_price(current)}</code>\n"
        f"Стоп-лосс: <code>{format_price(sl)}</code>\n"
        f"TP1: <code>{format_price(tp1)}</code>\n"
        f"TP2: <code>{format_price(tp2)}</code>\n\n"
        f"Исходный риск (1R): {init_risk:.4f} USDT\n"
        f"Текущий риск остатка: {curr_risk:.4f} USDT\n"
        f"Количество: {quantity:.8f}\n"
        f"Объём позиции: {notional:.2f} USDT\n"
        f"Маржа: {margin:.2f} USDT\n"
        f"Комиссии (учтено): {fees:.4f} USDT\n"
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


async def analyze_market_symbol(market, symbol, strategy_version: str = DEFAULT_STRATEGY):
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
        ver = str(strategy_version or DEFAULT_STRATEGY).upper()
        if ver == STRATEGY_B:
            quote = latest_quotes.get(key)
            signal = build_signal_b(market, symbol, candles, quote=quote)
        else:
            signal = build_signal_a_v92(market, symbol, candles)
    except Exception:
        logging.exception(
            "Ошибка анализа %s %s", market, symbol
        )
        return None

    if not signal:
        return None

    try:
        snapshot = await fetch_live_market_snapshot(market, symbol)
    except Exception:
        snapshot = None

    return enrich_signal_with_live_data(signal, snapshot)


async def scan_market(strategy_version: str = DEFAULT_STRATEGY):
    found_signals = []
    system_status["scan_count"] = int(system_status.get("scan_count", 0)) + 1
    system_status["last_scan"] = utc_text()

    for market in MARKETS:
        for symbol in SYMBOLS:
            key = (market, symbol)

            if key in _unavailable_symbols:
                continue

            try:
                signal = await analyze_market_symbol(market, symbol, strategy_version=strategy_version)

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
    live_p = safe_float(signal.get("live_price"), signal["entry"])
    spread_pct = safe_float(signal.get("spread_pct"), DEFAULT_SYNTHETIC_SPREAD_PCT)
    entry_mode = signal.get("entry_mode", "market")
    mode_label = "Вход от уровня / отката" if entry_mode == "level" else "Рыночный вход"

    raw_reasons = signal.get("reasons", "")
    if isinstance(raw_reasons, (list, tuple)):
        reasons_formatted = "\n".join(f"• {r}" for r in raw_reasons if r)
    else:
        parts = [p.strip() for p in str(raw_reasons).split(";") if p.strip()]
        reasons_formatted = "\n".join(f"• {p}" for p in parts) if parts else "—"

    extra_lines = [
        f"Стратегия: <b>{signal.get('strategy_version', 'A')}</b>",
        f"Режим входа: <b>{mode_label}</b>",
        f"Актуальная цена: <code>{format_price(live_p)}</code> (спред ~{spread_pct:.3f}%)",
    ]
    if entry_mode == "level":
        z_low = safe_float(signal.get("entry_zone_low"), signal["entry"])
        z_high = safe_float(signal.get("entry_zone_high"), signal["entry"])
        extra_lines.append(f"Зона входа: <code>{format_price(z_low)} — {format_price(z_high)}</code>")
        if signal.get("confirmation_rule"):
            extra_lines.append(f"Условие подтверждения: {signal['confirmation_rule']}")
    if signal.get("expires_at"):
        msk_expires = format_moscow_from_utc(signal["expires_at"])
        extra_lines.append(f"Действителен до: <code>{msk_expires} МСК</code>")

    return (
        f"🚨 <b>НОВЫЙ ТОРГОВЫЙ СИГНАЛ</b>\n\n"
        f"Монета: <b>{signal['symbol']}</b>\n"
        f"Рынок: {market}\n"
        f"Направление: {side}\n"
        + "\n".join(extra_lines) + "\n\n"
        f"Ориентир входа: <code>{format_price(signal['entry'])}</code>\n"
        f"Стоп-лосс: <code>{format_price(signal['sl'])}</code>\n"
        f"TP1: <code>{format_price(tp1)}</code> ({TP1_R}R) — "
        f"закрытие {TP1_CLOSE_PERCENT:.0f}%\n"
        f"TP2: <code>{format_price(tp2)}</code> ({TP2_R}R) — "
        f"закрытие {TP2_CLOSE_PERCENT:.0f}%\n\n"
        f"Оценка сигнала: {signal.get('score', 0)}\n"
        f"Соотношение риск/прибыль (TP2): {signal.get('rr', 0):.2f}\n\n"
        f"Причины:\n{reasons_formatted}"
    )


async def broadcast_signal(signal):
    with db() as conn:
        users = conn.execute("""
            SELECT user_id FROM users WHERE is_active = 1
        """).fetchall()

    for row in users:
        user_id = row["user_id"]
        if get_user_strategy(user_id) != str(signal.get("strategy_version", STRATEGY_A)).upper():
            continue

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

@router.message(CommandStart())
async def start_command(message: Message):
    register_user(message.from_user)
    clear_user_dialog_state(message.from_user.id)

    await message.answer(
        "Привет! Это Trading Bot v9.2.\n\n"
        "Бот анализирует публичные рыночные данные BingX "
        "и отправляет торговые сигналы.\n\n"
        "⚠️ Это аналитический сигнал, а не исполненная сделка. "
        "Сопровождение начнётся только после подтверждения входа "
        "и ввода фактической цены.\n\n"
        "Выбери нужный раздел в меню.",
        reply_markup=main_keyboard()
    )


@router.message(F.text == "🔎 Анализ рынка")
async def scan_market_callback(message: Message):
    register_user(message.from_user)
    ver = get_user_strategy(message.from_user.id)

    status_message = await message.answer(
        f"⏳ Начинаю анализ рынка BingX (стратегия {ver}).\n"
        "Сканирование может занять некоторое время."
    )

    try:
        expire_stale_signals()
        signals = await scan_market(strategy_version=ver)

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
async def enter_trade_callback(callback: CallbackQuery):
    register_user(callback.from_user)

    try:
        signal_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный сигнал.", show_alert=True)
        return

    with db() as conn:
        signal = conn.execute("""
            SELECT id, market, symbol, status, expires_at FROM signals WHERE id = ?
        """, (signal_id,)).fetchone()

    if not signal:
        await callback.answer("Сигнал не найден.", show_alert=True)
        return

    sig_status = str(signal["status"] or "").upper()
    exp_dt = parse_utc_text(signal["expires_at"])
    if sig_status in ("EXPIRED", "CANCELLED") or (exp_dt and utc_now() >= exp_dt):
        await callback.answer("Сигнал просрочен или отменён.", show_alert=True)
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

    set_user_dialog_state(callback.from_user.id, "waiting_entry", signal_id)

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

def _parse_price_input(raw_text: str | None) -> float | None:
    if not raw_text:
        return None
    cleaned = (
        raw_text.strip()
        .replace("\u00a0", "")
        .replace(" ", "")
        .replace(",", ".")
    )
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
    lambda message: (
        message.from_user is not None
        and get_user_dialog_state(message.from_user.id) is not None
    )
)
async def handle_user_text(message: Message):
    user_id = message.from_user.id
    state = get_user_dialog_state(user_id)

    if not state:
        return

    raw_stripped = (message.text or "").strip().lower()
    if raw_stripped in ("отмена", "cancel", "/cancel"):
        clear_user_dialog_state(user_id)

        await message.answer(
            "Действие отменено.",
            reply_markup=main_keyboard()
        )
        return

    value = _parse_price_input(message.text)
    if value is None:
        await message.answer(
            "Не удалось распознать число. Введи цену цифрами, например: 67250.5"
        )
        return

    if value <= 0:
        await message.answer("Число должно быть больше нуля.")
        return

    state_type = state.get("type")

    if state_type == "waiting_entry":
        signal_id = state.get("signal_id") if state.get("signal_id") is not None else state.get("ref_id")
        if signal_id is None:
            clear_user_dialog_state(user_id)
            await message.answer("Не удалось определить сигнал. Нажми «Я вошёл в сделку» ещё раз.")
            return

        trade_id, error, warning = create_personal_trade(
            user_id, int(signal_id), value
        )

        if error:
            await message.answer(error)
            return

        clear_user_dialog_state(user_id)
        trade = get_trade(trade_id, user_id)

        prefix = "✅ Сделка добавлена в «Мои сделки»."
        if warning:
            prefix = f"{prefix}\n\n{warning}"

        await message.answer(
            f"{prefix}\n\n" + trade_text(trade),
            reply_markup=trade_keyboard(trade_id),
            parse_mode="HTML"
        )
        return

    if state_type == "waiting_new_entry":
        trade_id = state.get("trade_id") if state.get("trade_id") is not None else state.get("ref_id")
        if trade_id is None:
            clear_user_dialog_state(user_id)
            await message.answer("Не удалось определить сделку. Попробуй снова через «📊 Мои сделки».")
            return

        success, result = update_trade_field(
            int(trade_id), user_id, "actual_entry", value
        )

        if success:
            clear_user_dialog_state(user_id)
            trade = get_trade(int(trade_id), user_id)

            await message.answer(
                result + "\n\n" + trade_text(trade),
                reply_markup=trade_keyboard(int(trade_id)),
                parse_mode="HTML"
            )
        else:
            await message.answer(result)
        return

    if state_type == "waiting_sl":
        trade_id = state.get("trade_id") if state.get("trade_id") is not None else state.get("ref_id")
        if trade_id is None:
            clear_user_dialog_state(user_id)
            await message.answer("Не удалось определить сделку. Попробуй снова через «📊 Мои сделки».")
            return

        success, result = update_trade_field(
            int(trade_id), user_id, "sl", value
        )

        if success:
            clear_user_dialog_state(user_id)
            trade = get_trade(int(trade_id), user_id)

            await message.answer(
                result + "\n\n" + trade_text(trade),
                reply_markup=trade_keyboard(int(trade_id)),
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
async def my_trades_handler(message: Message):
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
async def trade_refresh_callback(callback: CallbackQuery):
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
async def trade_edit_entry_callback(callback: CallbackQuery):
    try:
        trade_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный номер сделки.", show_alert=True)
        return

    trade = get_trade(trade_id, callback.from_user.id)

    if not trade:
        await callback.answer("Сделка не найдена.", show_alert=True)
        return

    set_user_dialog_state(callback.from_user.id, "waiting_new_entry", trade_id)

    await callback.message.answer(
        "Введи новую фактическую цену входа.\n"
        "Стоп-лосс и цели будут пересчитаны относительно новой цены."
    )

    await callback.answer()


@router.callback_query(F.data.startswith("trade_edit_sl:"))
async def trade_edit_sl_callback(callback: CallbackQuery):
    try:
        trade_id = int(callback.data.split(":")[1])
    except (ValueError, IndexError):
        await callback.answer("Некорректный номер сделки.", show_alert=True)
        return

    trade = get_trade(trade_id, callback.from_user.id)

    if not trade:
        await callback.answer("Сделка не найдена.", show_alert=True)
        return

    set_user_dialog_state(callback.from_user.id, "waiting_sl", trade_id)

    await callback.message.answer(
        "Введи новую цену стоп-лосса.\n"
        "Бот сохранит её и пересчитает текущий риск остатка (исходный 1R и объём сохраняются)."
    )

    await callback.answer()


# ============================================================
# 28. TRADE HISTORY AND STATISTICS
# ============================================================

@router.message(F.text == "📚 История")
async def history_handler(message: Message):
    register_user(message.from_user)

    with db() as conn:
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
async def statistics_handler(message: Message):
    register_user(message.from_user)

    with db() as conn:
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
async def settings_handler(message: Message):
    register_user(message.from_user)

    spot_balance = get_user_balance(message.from_user.id, "spot")
    swap_balance = get_user_balance(message.from_user.id, "swap")
    ver = get_user_strategy(message.from_user.id)

    await message.answer(
        "⚙️ <b>Настройки торгового бота</b>\n\n"
        f"Активная стратегия: <b>{ver}</b>\n"
        f"Риск на сделку: {RISK_PERCENT}%\n"
        f"Плечо Futures: {FUTURES_LEVERAGE}x\n"
        f"TP1: {TP1_R}R — закрытие {TP1_CLOSE_PERCENT:.0f}%\n"
        f"TP2: {TP2_R}R — закрытие {TP2_CLOSE_PERCENT:.0f}%\n\n"
        f"Баланс Spot: {spot_balance:.2f} USDT\n"
        f"Баланс Futures: {swap_balance:.2f} USDT\n\n"
        "Команды:\n"
        "<code>/spotbalance 1000</code>\n"
        "<code>/swapbalance 500</code>\n"
        "<code>/strategy A</code> или <code>/strategy B</code>\n\n"
        "Баланс вводится вручную. Бот не получает доступ "
        "к твоему аккаунту или средствам.",
        parse_mode="HTML"
    )


@router.message(Command("strategy"))
async def set_strategy_handler(message: Message):
    register_user(message.from_user)
    parts = (message.text or "").split()
    if len(parts) != 2 or parts[1].upper() not in (STRATEGY_A, STRATEGY_B):
        await message.answer("Укажи стратегию так: /strategy A или /strategy B")
        return

    chosen = set_user_strategy(message.from_user.id, parts[1].upper())
    await message.answer(f"✅ Выбрана стратегия: <b>{chosen}</b>", parse_mode="HTML")


@router.message(Command("spotbalance"))
async def set_spot_balance_handler(message: Message):
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
async def set_swap_balance_handler(message: Message):
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
async def system_status_handler(message: Message):
    register_user(message.from_user)

    active = len(get_active_trades(message.from_user.id))
    ver = get_user_strategy(message.from_user.id)

    await message.answer(
        "🖥 <b>Состояние Trading Bot v9.2</b>\n\n"
        f"Режим: публичный анализ BingX\n"
        f"Реальные ордера: отключены\n"
        f"Стратегия пользователя: {ver}\n"
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
# 31. LIVE PRICE & BID/ASK
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
            bid = safe_float(data.get("bidPrice") or data.get("bid"), 0.0)
            ask = safe_float(data.get("askPrice") or data.get("ask"), 0.0)
            for key in ("price", "lastPrice", "last", "close"):
                price = safe_float(data.get(key), 0)

                if price > 0:
                    latest_prices[(market, symbol)] = price
                    if bid > 0 and ask > 0 and ask >= bid:
                        latest_quotes[(market, symbol)] = {
                            "price": price,
                            "bid": bid,
                            "ask": ask,
                            "spread_pct": ((ask - bid) / price * 100.0),
                            "is_synthetic_spread": False,
                            "updated_at": utc_text(),
                        }
                    return price

    except Exception:
        logging.exception(
            "Не удалось получить текущую цену %s %s",
            market, symbol
        )

    return None


# ============================================================
# 32. TRADE MONITORING (NOTIFICATIONS ONLY, NO AUTO-EXECUTION)
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
            expire_stale_signals()

            with db() as conn:
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

        await asyncio.sleep(max(TRADE_MONITOR_INTERVAL_SEC, 5))


# ============================================================
# 33. CONFIRMATION OF USER EXECUTIONS & CLOSING
# ============================================================

async def close_trade_record(trade, exit_price, reason, exec_type: str = "manual_close"):
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
    tp1_hit = bool(trade.get("tp1_hit"))
    prev_fees = safe_float(trade.get("total_fees_paid"), 0.0)

    if tp1_hit:
        tp1 = safe_float(trade["tp1"], 0)
        qty_tp1 = safe_float(trade.get("tp1_fill_qty"), quantity * 0.70)
        rem_qty = max(0.0, quantity - qty_tp1)
        gross_tp1 = qty_tp1 * (tp1 - entry) * direction
        gross_rem = rem_qty * (exit_price - entry) * direction
        gross_realized = gross_tp1 + gross_rem
        exit_fee = rem_qty * exit_price * TAKER_FEE_RATE
        total_fees = prev_fees + exit_fee
    else:
        qty_tp1 = 0.0
        rem_qty = quantity
        gross_realized = quantity * (exit_price - entry) * direction
        exit_fee = quantity * exit_price * TAKER_FEE_RATE
        total_fees = prev_fees + exit_fee

    net_realized = gross_realized - total_fees

    initial_risk = safe_float(trade.get("initial_risk_amount"), 0.0)
    if initial_risk <= 0:
        initial_risk = safe_float(trade.get("risk_amount"), 0.0)
    if initial_risk <= 0:
        initial_risk = quantity * abs(entry - initial_sl)

    pnl_r = net_realized / initial_risk if initial_risk > 0 else 0.0

    if str(trade["market"]).lower() == "swap":
        base = safe_float(trade.get("margin_required"), 0.0)
    else:
        base = quantity * entry

    if base <= 0:
        base = quantity * entry

    pnl_percent = net_realized / base * 100.0 if base > 0 else 0.0

    result = (
        "win" if net_realized > 0
        else ("loss" if net_realized < 0 else "breakeven")
    )

    now = utc_text()

    with db() as conn:
        conn.execute("""
            UPDATE trades
            SET status = 'closed',
                remaining_percent = 0,
                tp2_fill_price = ?,
                tp2_fill_qty = ?,
                tp2_fill_fee = ?,
                tp2_closed_at = ?,
                total_fees_paid = ?,
                closed_at = ?,
                updated_at = ?
            WHERE id = ? AND user_id = ?
        """, (
            exit_price, rem_qty, exit_fee, now,
            total_fees, now, now, trade_id, user_id
        ))

        conn.execute("""
            INSERT INTO trade_history (
                trade_id, user_id, market, symbol, side,
                entry, exit_price, result,
                pnl_percent, pnl_r, net_pnl_usdt,
                initial_risk_amount, total_fees_paid,
                total_slippage_cost, strategy_version,
                close_reason, opened_at, closed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade_id, user_id, trade["market"], trade["symbol"],
            trade["side"], entry, exit_price, result,
            pnl_percent, pnl_r, net_realized,
            initial_risk, total_fees,
            safe_float(trade.get("total_slippage_cost"), 0.0),
            trade.get("strategy_version", STRATEGY_A),
            reason, trade["opened_at"], now
        ))

    save_trade_execution(
        trade_id=trade_id,
        user_id=user_id,
        exec_type=exec_type,
        price=exit_price,
        quantity=rem_qty,
        share_percent=safe_float(trade.get("remaining_percent"), 100.0),
        fee_usdt=exit_fee,
        realized_pnl_usdt=gross_realized - exit_fee,
        executed_at=now
    )

    save_trade_event(
        trade_id, user_id, "closed", reason, exit_price
    )

    return True, (
        f"Сделка закрыта.\n"
        f"Результат: {result}\n"
        f"Чистый PnL (после комиссий): {net_realized:.4f} USDT\n"
        f"Результат (от 1R): {pnl_r:.2f}R"
    )


@router.callback_query(F.data.startswith("trade_close:"))
async def trade_close_callback(callback: CallbackQuery):
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
async def close_cancel_callback(callback: CallbackQuery):
    await callback.answer("Закрытие отменено.")
    await callback.message.answer(
        "Сопровождение сделки продолжается."
    )


@router.callback_query(F.data.startswith("close_confirm:"))
async def close_confirm_callback(callback: CallbackQuery):
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
        "Пользователь подтвердил закрытие",
        exec_type="manual_close"
    )

    await callback.answer(
        "Сделка закрыта." if success else "Ошибка",
        show_alert=not success
    )

    await callback.message.answer(result)


@router.callback_query(F.data.startswith("tp1_confirm:"))
async def tp1_confirm_callback(callback: CallbackQuery):
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

    qty_full = safe_float(trade.get("position_size"), 0.0)
    qty_tp1 = qty_full * (TP1_CLOSE_PERCENT / 100.0)
    tp1_price = safe_float(trade.get("tp1"), safe_float(trade.get("current_price"), 0.0))
    tp1_fee = qty_tp1 * tp1_price * TAKER_FEE_RATE if tp1_price > 0 else 0.0
    prev_fees = safe_float(trade.get("total_fees_paid"), 0.0)
    now = utc_text()

    with db() as conn:
        conn.execute("""
            UPDATE trades
            SET tp1_hit = 1,
                remaining_percent = ?,
                tp1_fill_price = ?,
                tp1_fill_qty = ?,
                tp1_fill_fee = ?,
                tp1_closed_at = ?,
                total_fees_paid = ?,
                last_recommendation = ?,
                updated_at = ?
            WHERE id = ? AND user_id = ?
        """, (
            TP2_CLOSE_PERCENT,
            tp1_price,
            qty_tp1,
            tp1_fee,
            now,
            prev_fees + tp1_fee,
            "TP1 подтверждён пользователем. "
            "Рекомендуется вручную перенести SL на цену входа.",
            now,
            trade_id,
            callback.from_user.id
        ))

    save_trade_execution(
        trade_id=trade_id,
        user_id=callback.from_user.id,
        exec_type="tp1_partial",
        price=tp1_price,
        quantity=qty_tp1,
        share_percent=TP1_CLOSE_PERCENT,
        fee_usdt=tp1_fee,
        executed_at=now
    )

    save_trade_event(
        trade_id, callback.from_user.id, "tp1_confirmed",
        "Пользователь подтвердил закрытие 70% позиции", tp1_price
    )

    await callback.message.answer(
        "✅ TP1 подтверждён.\n\n"
        f"Зафиксировано закрытие {TP1_CLOSE_PERCENT:.0f}% позиции.\n"
        f"Остаток: {TP2_CLOSE_PERCENT:.0f}%.\n\n"
        "Рекомендация: вручную перенести стоп-лосс "
        "на фактическую цену входа в BingX.\n"
        "Бот не перемещает стоп автоматически."
    )

    await callback.answer("TP1 подтверждён.")


@router.callback_query(F.data.startswith("tp2_cancel:"))
async def tp2_cancel_callback(callback: CallbackQuery):
    await callback.answer("Подтверждение отменено.")
    await callback.message.answer(
        "Сделка остаётся под сопровождением."
    )


@router.callback_query(F.data.startswith("tp2_confirm:"))
async def tp2_confirm_callback(callback: CallbackQuery):
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
        "Пользователь подтвердил исполнение TP2",
        exec_type="tp2_final"
    )

    await callback.answer(
        "TP2 подтверждён." if success else "Ошибка",
        show_alert=not success
    )

    await callback.message.answer(result)


@router.callback_query(F.data.startswith("sl_keep:"))
async def sl_keep_callback(callback: CallbackQuery):
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
async def sl_confirm_callback(callback: CallbackQuery):
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
        "Пользователь подтвердил закрытие по SL",
        exec_type="sl_close"
    )

    await callback.answer(
        "Закрытие записано." if success else "Ошибка",
        show_alert=not success
    )

    await callback.message.answer(result)


# ============================================================
# 34. BACKGROUND MARKET SCAN & FAIR BACKTEST ENGINE
# ============================================================

async def background_market_scan():
    logging.info("Фоновый анализ рынка запущен")

    while True:
        try:
            expire_stale_signals()
            signals = await scan_market(strategy_version=DEFAULT_STRATEGY)

            for signal in signals:
                await broadcast_signal(signal)

        except Exception:
            logging.exception("Ошибка фонового анализа рынка")

        await asyncio.sleep(max(MARKET_SCAN_INTERVAL_SEC, 60))


def simulate_strategy_on_frames(
    strategy_version: str,
    market: str,
    symbol: str,
    frames: dict[str, pd.DataFrame],
    initial_balance: float = 1000.0,
    synthetic_spread_pct: float = DEFAULT_SYNTHETIC_SPREAD_PCT,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
) -> dict[str, Any]:
    m15_full = frames.get("15m")
    if m15_full is None or len(m15_full) <= MIN_CANDLE_ROWS:
        return {
            "strategy": strategy_version,
            "is_modeled_spread": True,
            "net_pnl": 0.0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "max_drawdown": 0.0,
            "avg_r": 0.0,
            "signals_count": 0,
            "trades_count": 0,
            "reward_to_risk": 0.0,
        }

    balance = float(initial_balance)
    peak_balance = balance
    max_dd = 0.0
    open_positions: list[dict[str, Any]] = []
    closed_pnls: list[float] = []
    closed_rs: list[float] = []
    signals_count = 0

    for idx in range(MIN_CANDLE_ROWS, len(m15_full)):
        cutoff_ms = int(m15_full.iloc[idx]["open_time"])
        sub_frames = {
            tf: df[df["open_time"] <= cutoff_ms].reset_index(drop=True)
            for tf, df in frames.items()
            if df is not None and not df.empty
        }
        if any(len(sub_frames.get(tf, [])) < MIN_CANDLE_ROWS for tf in ("5m", "15m", "1h", "4h")):
            continue

        bar = sub_frames["15m"].iloc[-1]
        b_high = safe_float(bar["high"])
        b_low = safe_float(bar["low"])
        b_open = safe_float(bar["open"])
        b_close = safe_float(bar["close"])
        half_sp = b_close * (synthetic_spread_pct / 200.0)
        bid_bar = b_close - half_sp
        ask_bar = b_close + half_sp

        still_open: list[dict[str, Any]] = []
        for pos in open_positions:
            long_pos = is_long_side(pos["side"])
            sl_px = pos["sl"]
            tp1_px = pos["tp1"]
            tp2_px = pos["tp2"]

            if long_pos:
                hit_sl = (b_low - half_sp) <= sl_px
                hit_tp1 = (b_high - half_sp) >= tp1_px
                hit_tp2 = (b_high - half_sp) >= tp2_px
            else:
                hit_sl = (b_high + half_sp) >= sl_px
                hit_tp1 = (b_low + half_sp) <= tp1_px
                hit_tp2 = (b_low + half_sp) <= tp2_px

            if hit_sl:
                exit_px = (b_open - half_sp) if (long_pos and b_open < sl_px) else (
                    (b_open + half_sp) if (not long_pos and b_open > sl_px) else sl_px
                )
                exit_px *= (1.0 - slippage_pct / 100.0) if long_pos else (1.0 + slippage_pct / 100.0)
                rem_qty = pos["qty"] * (pos["rem_pct"] / 100.0)
                pnl_leg = rem_qty * (exit_px - pos["entry"]) * (1.0 if long_pos else -1.0)
                fee_leg = rem_qty * exit_px * TAKER_FEE_RATE
                total_net = pos["realized_gross"] + pnl_leg - pos["fees"] - fee_leg
                balance += pnl_leg - fee_leg
                closed_pnls.append(total_net)
                closed_rs.append(total_net / pos["init_risk"] if pos["init_risk"] > 0 else 0.0)
                continue

            if hit_tp1 and not pos["tp1_hit"]:
                qty_70 = pos["qty"] * (TP1_CLOSE_PERCENT / 100.0)
                tp1_exec = tp1_px * ((1.0 - slippage_pct / 100.0) if long_pos else (1.0 + slippage_pct / 100.0))
                leg1 = qty_70 * (tp1_exec - pos["entry"]) * (1.0 if long_pos else -1.0)
                fee1 = qty_70 * tp1_exec * TAKER_FEE_RATE
                pos["realized_gross"] += leg1
                pos["fees"] += fee1
                pos["tp1_hit"] = True
                pos["rem_pct"] = TP2_CLOSE_PERCENT
                pos["sl"] = pos["entry"]
                balance += leg1 - fee1
                still_open.append(pos)
                continue

            if pos["tp1_hit"] and hit_tp2:
                qty_30 = pos["qty"] * (pos["rem_pct"] / 100.0)
                tp2_exec = tp2_px * ((1.0 - slippage_pct / 100.0) if long_pos else (1.0 + slippage_pct / 100.0))
                leg2 = qty_30 * (tp2_exec - pos["entry"]) * (1.0 if long_pos else -1.0)
                fee2 = qty_30 * tp2_exec * TAKER_FEE_RATE
                total_net = pos["realized_gross"] + leg2 - pos["fees"] - fee2
                balance += leg2 - fee2
                closed_pnls.append(total_net)
                closed_rs.append(total_net / pos["init_risk"] if pos["init_risk"] > 0 else 0.0)
                continue

            still_open.append(pos)

        open_positions = still_open
        peak_balance = max(peak_balance, balance)
        dd = peak_balance - balance
        if dd > max_dd:
            max_dd = dd

        if len(open_positions) < MAX_ACTIVE_TRADES:
            sig = (
                build_signal_b(market, symbol, sub_frames, quote={"spread_pct": synthetic_spread_pct})
                if strategy_version == STRATEGY_B
                else build_signal_a_v92(market, symbol, sub_frames)
            )
            if sig:
                signals_count += 1
                long_s = is_long_side(sig["side"])
                raw_entry = ask_bar if long_s else bid_bar
                entry_exec = raw_entry * ((1.0 + slippage_pct / 100.0) if long_s else (1.0 - slippage_pct / 100.0))
                dist = abs(safe_float(sig["entry"]) - safe_float(sig["sl"]))
                if dist > 0 and entry_exec > 0:
                    sl_exec = (entry_exec - dist) if long_s else (entry_exec + dist)
                    tp1_exec = (entry_exec + dist * TP1_R) if long_s else (entry_exec - dist * TP1_R)
                    tp2_exec = (entry_exec + dist * TP2_R) if long_s else (entry_exec - dist * TP2_R)
                    if min(sl_exec, tp1_exec, tp2_exec) > 0:
                        sizing = calculate_position_size(balance, entry_exec, sl_exec, market)
                        entry_fee = sizing["notional"] * TAKER_FEE_RATE
                        balance -= entry_fee
                        open_positions.append({
                            "side": sig["side"],
                            "entry": entry_exec,
                            "sl": sl_exec,
                            "tp1": tp1_exec,
                            "tp2": tp2_exec,
                            "qty": sizing["quantity"],
                            "rem_pct": 100.0,
                            "tp1_hit": False,
                            "init_risk": sizing["risk_amount"],
                            "realized_gross": 0.0,
                            "fees": entry_fee,
                        })

    wins = [p for p in closed_pnls if p > 0]
    losses = [abs(p) for p in closed_pnls if p < 0]
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    pf = (gross_profit / gross_loss) if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    avg_win = (gross_profit / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0

    return {
        "strategy": strategy_version,
        "is_modeled_spread": True,
        "net_pnl": balance - initial_balance,
        "win_rate": (len(wins) / len(closed_pnls) * 100.0) if closed_pnls else 0.0,
        "profit_factor": pf,
        "max_drawdown": max_dd,
        "avg_r": (sum(closed_rs) / len(closed_rs)) if closed_rs else 0.0,
        "signals_count": signals_count,
        "trades_count": len(closed_pnls),
        "reward_to_risk": (avg_win / avg_loss) if avg_loss > 0 else 0.0,
    }


def compare_strategies_fairly(
    market: str,
    symbol: str,
    frames: dict[str, pd.DataFrame],
    oos_ratio: float = 0.3,
    initial_balance: float = 1000.0,
) -> dict[str, Any]:
    m15 = frames["15m"]
    split_idx = max(MIN_CANDLE_ROWS + 1, int(len(m15) * (1.0 - oos_ratio)))
    oos_cutoff = int(m15.iloc[split_idx]["open_time"])

    oos_frames = {
        tf: df[df["open_time"] >= oos_cutoff].reset_index(drop=True)
        for tf, df in frames.items()
        if df is not None and not df.empty
    }

    return {
        "full_sample": {
            "A": simulate_strategy_on_frames(STRATEGY_A, market, symbol, frames, initial_balance=initial_balance),
            "B": simulate_strategy_on_frames(STRATEGY_B, market, symbol, frames, initial_balance=initial_balance),
        },
        "out_of_sample": {
            "A": simulate_strategy_on_frames(STRATEGY_A, market, symbol, oos_frames, initial_balance=initial_balance),
            "B": simulate_strategy_on_frames(STRATEGY_B, market, symbol, oos_frames, initial_balance=initial_balance),
        },
    }


# ============================================================
# 35. APPLICATION STARTUP
# ============================================================

async def main():
    global bot
    global http_session
    global dp

    logging.info("Запуск Trading Bot v9.2")
    logging.info(
        "Предупреждение: это аналитический сигнал, а не исполненная сделка. "
        "Сопровождение начнётся только после подтверждения входа и ввода фактической цены."
    )

    freeze_source_snapshot()
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
