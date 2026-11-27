"""Tests for Telegram scan-silence diagnostics."""

import os

os.environ.setdefault("BOT_TOKEN", "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
os.environ.setdefault("DB_PATH", "/tmp/trading_bot_scan_diagnostics_test.db")
os.environ.setdefault("BACKUP_DIR", "/tmp/trading_bot_scan_diagnostics_backups")

from main import _finalize_cancelled_scan, format_scan_rejection_summary, format_scan_symbol_summary


def test_scan_rejection_summary_orders_by_frequency():
    result = format_scan_rejection_summary({
        "нет BOS": 2,
        "ADX ниже порога": 8,
        "нет согласования тренда": 5,
    })
    assert result.splitlines() == [
        "8× ADX ниже порога",
        "5× нет согласования тренда",
        "2× нет BOS",
    ]


def test_scan_rejection_summary_limits_output():
    result = format_scan_rejection_summary({f"reason_{i}": i for i in range(1, 8)}, limit=3)
    assert len(result.splitlines()) == 3
    assert result.splitlines()[0] == "7× reason_7"


def test_scan_rejection_summary_handles_empty_and_invalid_input():
    assert format_scan_rejection_summary(None) == "—"
    assert format_scan_rejection_summary({}) == "—"
    assert format_scan_rejection_summary({"zero": 0}) == "—"


def test_scan_symbol_summary_shows_per_symbol_outcomes():
    result = format_scan_symbol_summary({
        "BTC-USDT": "ADX ниже порога",
        "ETH-USDT": "нет согласования тренда",
    })
    assert result.splitlines() == [
        "BTC-USDT: ADX ниже порога",
        "ETH-USDT: нет согласования тренда",
    ]


def test_scan_symbol_summary_limits_output_and_escapes_html():
    result = format_scan_symbol_summary({
        "BTC-USDT": "причина <не прошла>",
        "ETH-USDT": "вторая причина",
    }, limit=1)
    assert len(result.splitlines()) == 1
    assert "&lt;не прошла&gt;" in result


def test_cancelled_scan_clears_running_state_and_preserves_partial_results(monkeypatch):
    import main

    monkeypatch.setattr(main.time, "monotonic", lambda: 125.0)
    status = {
        "last_scan_completed": 3,
        "last_scan_planned": 14,
        "last_scan_current_symbol": "ETH-USDT",
        "last_scan_started_monotonic": 100.0,
        "last_scan_finished": None,
        "last_scan_duration_sec": None,
        "last_scan_rejections": {"старый отказ": 2},
    }
    partial = {"BTC-USDT": "ADX ниже порога"}
    _finalize_cancelled_scan(status, 100.0, partial, "2026-11-27 14:10:00")

    assert status["last_scan_finished"] == "2026-11-27 14:10:00"
    assert status["last_scan_duration_sec"] == 25.0
    assert status["last_scan_current_symbol"] is None
    assert status["last_scan_started_monotonic"] is None
    assert status["last_scan_symbol_results"] == partial
    assert status["last_scan_symbol_results_live"] == partial
    assert status["last_scan_summary"] == "Сканирование отменено; обработано 3/14 пар"
    assert status["last_scan_rejections"] == {"старый отказ": 2}
