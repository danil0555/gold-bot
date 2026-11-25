"""Regression tests for the direction-first Telegram signal heading."""

import os

os.environ.setdefault("BOT_TOKEN", "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
os.environ.setdefault("DB_PATH", "/tmp/trading_bot_signal_header_test.db")
os.environ.setdefault("BACKUP_DIR", "/tmp/trading_bot_signal_header_backups")

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
