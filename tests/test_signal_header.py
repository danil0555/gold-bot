"""Regression tests for the direction-first Telegram signal heading."""

import os
import sqlite3
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
os.environ.setdefault("DB_PATH", "/tmp/trading_bot_signal_header_test.db")
os.environ.setdefault("BACKUP_DIR", "/tmp/trading_bot_signal_header_backups")

import main
from main import FUTURES_LEVERAGE, RISK_PERCENT, signal_text


def make_signal(side: str) -> dict:
    return {
        "symbol": "BTC-USDT",
        "side": side,
        "entry": 100.0,
        "sl": 102.0 if side.upper() in ("SHORT", "SELL") else 98.0,
        "tp1": 96.4 if side.upper() in ("SHORT", "SELL") else 103.6,
        "tp2": 94.0 if side.upper() in ("SHORT", "SELL") else 106.0,
        "rr": 1.8,
        "net_rr": 1.5,
        "quality_score": 90,
        "live_price": 100.0,
        "spread_pct": 0.01,
        "reasons": ["regression test"],
        "entry_mode": "market",
    }


def test_short_signal_starts_with_red_indicator_and_symbol():
    rendered = signal_text(make_signal("SHORT"))
    assert rendered.startswith("🔴 <b>SHORT — BTC-USDT</b>\n")
    assert "FUTURES SCALP — СИГНАЛ" not in rendered


def test_long_signal_starts_with_green_indicator_and_symbol():
    rendered = signal_text(make_signal("LONG"))
    assert rendered.startswith("🟢 <b>LONG — BTC-USDT</b>\n")
    assert "FUTURES SCALP — СИГНАЛ" not in rendered


def test_risk_default_is_ten_percent_and_leverage_is_unchanged():
    assert RISK_PERCENT == 10.0
    assert FUTURES_LEVERAGE == 5

def test_signal_shows_position_size_from_saved_balance():
    rendered = signal_text(make_signal("LONG"), balance_usdt=1000.0)
    assert "Риск до SL (10.0%): <code>100.00 USDT</code>" in rendered
    assert "Объём: <code>50 BTC</code>" in rendered
    assert "Номинал позиции: <code>5,000.00 USDT</code>" in rendered
    assert "Маржа при 5x: <code>1,000.00 USDT</code>" in rendered


def test_signal_warns_when_required_margin_exceeds_balance():
    signal = make_signal("LONG")
    signal["sl"] = 99.8
    rendered = signal_text(signal, balance_usdt=1000.0)
    assert "Не хватает баланса под расчётную маржу" in rendered


def test_user_balance_is_saved_per_user_in_additive_profile_table(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "DB_PATH", tmp_path / "risk_profiles.sqlite3")
    monkeypatch.setattr(main, "BACKUP_DIR", tmp_path / "backups")
    main.BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    main.init_db()
    main.register_user(SimpleNamespace(id=101, username="first"))
    main.register_user(SimpleNamespace(id=202, username="second"))
    main.set_user_balance(101, 1234.5)

    assert main.get_user_balance(101) == 1234.5
    assert main.get_user_balance(202) is None
    with main.db() as con:
        violations = con.execute("PRAGMA foreign_key_check").fetchall()
    assert violations == []


def test_additive_balance_schema_keeps_existing_rows_and_creates_backup(tmp_path, monkeypatch):
    db_path = tmp_path / "existing_bot.sqlite3"
    backup_dir = tmp_path / "backups"
    with sqlite3.connect(db_path) as con:
        con.execute("""CREATE TABLE users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            created_at TEXT,
            is_active INTEGER DEFAULT 1
        )""")
        con.execute("""CREATE TABLE user_settings (
            user_id INTEGER PRIMARY KEY,
            risk_percent REAL DEFAULT 2.0,
            leverage INTEGER DEFAULT 5,
            notifications INTEGER DEFAULT 1,
            strategy_version TEXT DEFAULT 'B'
        )""")
        con.execute(
            "INSERT INTO users (user_id, username, created_at, is_active) VALUES (7, 'legacy', '2026-01-01', 1)"
        )
        con.execute(
            "INSERT INTO user_settings (user_id, risk_percent, leverage) VALUES (7, 2.0, 5)"
        )

    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "BACKUP_DIR", backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    main.init_db()

    with main.db() as con:
        user = con.execute("SELECT username FROM users WHERE user_id=7").fetchone()
        settings = con.execute(
            "SELECT risk_percent, leverage FROM user_settings WHERE user_id=7"
        ).fetchone()
        profile_table = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='user_risk_profiles'"
        ).fetchone()
    assert user["username"] == "legacy"
    assert settings["risk_percent"] == 2.0
    assert settings["leverage"] == 5
    assert profile_table is not None
    backups = list(backup_dir.glob("existing_bot_pre_migration_*.db"))
    assert len(backups) == 1


def test_init_db_refuses_migration_when_backup_returns_none(tmp_path, monkeypatch):
    db_path = tmp_path / 'old.sqlite3'
    with sqlite3.connect(db_path) as con:
        con.execute('CREATE TABLE legacy_marker (value TEXT)')
        con.execute("INSERT INTO legacy_marker VALUES ('keep')")
    monkeypatch.setattr(main, 'DB_PATH', db_path)
    monkeypatch.setattr(main, 'BACKUP_DIR', tmp_path / 'backups')
    monkeypatch.setattr(main, 'backup_and_verify_db', lambda: None)
    with pytest.raises(RuntimeError, match='Резервная копия БД'):
        main.init_db()
    with sqlite3.connect(db_path) as con:
        names = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        value = con.execute('SELECT value FROM legacy_marker').fetchone()[0]
    assert value == 'keep'
    assert 'user_risk_profiles' not in names
