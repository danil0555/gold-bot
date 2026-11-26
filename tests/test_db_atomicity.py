"""Regression tests for atomic signal/audit writes and fail-closed DB checks."""

import os
import sqlite3
from contextlib import contextmanager

# main.py validates BOT_TOKEN at import time; CI uses a nonfunctional test token.
os.environ.setdefault("BOT_TOKEN", "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
import main


def _prepare_db(tmp_path, monkeypatch):
    db_path = tmp_path / "bot.sqlite3"
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "BACKUP_DIR", backup_dir)
    main.init_db()
    return db_path, backup_dir


def _signal():
    return {
        "market": "swap",
        "symbol": "BTC-USDT",
        "side": "BUY",
        "entry": 100.0,
        "sl": 98.0,
        "tp2": 106.0,
        "rr": 3.0,
        "score": 90,
        "reasons": ["atomicity test"],
        "setup_key": "atomicity-test-unique",
        "status": "ACTIVE",
        "strategy_version": main.STRATEGY_B,
        "live_price": 100.0,
    }


def test_save_signal_rolls_back_signal_when_event_insert_fails(tmp_path, monkeypatch):
    _prepare_db(tmp_path, monkeypatch)
    with main.db() as con:
        con.execute("""
            CREATE TRIGGER reject_created_event
            BEFORE INSERT ON signal_events
            WHEN NEW.event_type = 'created'
            BEGIN
                SELECT RAISE(ABORT, 'injected audit failure');
            END
        """)

    try:
        main.save_signal(_signal())
    except sqlite3.IntegrityError as exc:
        assert "injected audit failure" in str(exc)
    else:
        raise AssertionError("Expected injected event failure")

    with main.db() as con:
        assert con.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM signal_events").fetchone()[0] == 0


def test_expire_stale_signals_rolls_back_status_when_event_insert_fails(tmp_path, monkeypatch):
    _prepare_db(tmp_path, monkeypatch)
    with main.db() as con:
        cur = con.execute("""
            INSERT INTO signals
                (created_at, updated_at, market, symbol, side, entry, sl, tp, rr,
                 score, reasons, setup_key, status, strategy_version, expires_at)
            VALUES (?, ?, 'swap', 'BTC-USDT', 'BUY', 100, 98, 106, 3,
                    90, 'test', 'expire-atomicity-test', 'ACTIVE', 'B', ?)
        """, ("2000-01-01 00:00:00", "2000-01-01 00:00:00", "2000-01-02 00:00:00"))
        signal_id = cur.lastrowid
        con.execute("""
            CREATE TRIGGER reject_expired_event
            BEFORE INSERT ON signal_events
            WHEN NEW.event_type = 'expired'
            BEGIN
                SELECT RAISE(ABORT, 'injected expiry audit failure');
            END
        """)

    try:
        main.expire_stale_signals()
    except sqlite3.IntegrityError as exc:
        assert "injected expiry audit failure" in str(exc)
    else:
        raise AssertionError("Expected injected expiry event failure")

    with main.db() as con:
        row = con.execute("SELECT status FROM signals WHERE id=?", (signal_id,)).fetchone()
        assert row["status"] == "ACTIVE"
        assert con.execute(
            "SELECT COUNT(*) FROM signal_events WHERE signal_id=?", (signal_id,)
        ).fetchone()[0] == 0


def test_backup_refuses_source_database_with_failed_integrity_check(tmp_path, monkeypatch):
    db_path = tmp_path / "damaged.sqlite3"
    db_path.write_bytes(b"placeholder: file exists so backup validation runs")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "BACKUP_DIR", backup_dir)

    class _Result:
        def fetchall(self):
            return [("database disk image is malformed",)]

    class _Connection:
        def execute(self, statement):
            assert statement == "PRAGMA integrity_check"
            return _Result()

    @contextmanager
    def fake_db():
        yield _Connection()

    monkeypatch.setattr(main, "db", fake_db)

    assert main.backup_and_verify_db() is None
    assert list(backup_dir.glob("*.db")) == []



def test_backup_refuses_source_database_with_foreign_key_violations(tmp_path, monkeypatch):
    db_path = tmp_path / "fk-invalid.sqlite3"
    db_path.write_bytes(b"placeholder: file exists so backup validation runs")
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "BACKUP_DIR", backup_dir)

    class _Result:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

    class _Connection:
        def execute(self, statement):
            if statement == "PRAGMA integrity_check":
                return _Result([("ok",)])
            if statement == "PRAGMA foreign_key_check":
                return _Result([("signals", 7, "users", 0)])
            raise AssertionError(f"Unexpected SQL: {statement}")

    @contextmanager
    def fake_db():
        yield _Connection()

    monkeypatch.setattr(main, "db", fake_db)

    assert main.backup_and_verify_db() is None
    assert list(backup_dir.glob("*.db")) == []



def test_init_db_aborts_before_migration_when_backup_is_unavailable(tmp_path, monkeypatch):
    db_path = tmp_path / "existing.sqlite3"
    db_path.write_bytes(b"existing database placeholder")
    monkeypatch.setattr(main, "DB_PATH", db_path)
    monkeypatch.setattr(main, "backup_and_verify_db", lambda: None)

    try:
        main.init_db()
    except RuntimeError as exc:
        assert "инициализация и миграция остановлены" in str(exc)
    else:
        raise AssertionError("Expected init_db to fail closed without a verified backup")

    # The placeholder must remain untouched; migration must not open it.
    assert db_path.read_bytes() == b"existing database placeholder"
