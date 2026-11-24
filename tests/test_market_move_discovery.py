import sqlite3

import numpy as np
import pandas as pd

from scripts.market_move_discovery import (
    build_report, discover_episodes, load_market_panel,
)


def synthetic_panel(rows=240, jump_at=180, jump=0.012):
    close = np.full(rows, 100.0)
    close[:jump_at] *= 1 + np.arange(jump_at) * 0.00001
    close[jump_at:] = close[jump_at - 1] * (1 + jump)
    return pd.DataFrame({
        "symbol": ["BTC-USDT"] * rows,
        "minute_ts": np.arange(rows, dtype=np.int64) * 60_000 + 1_800_000_000_000,
        "close": close,
        "rv_60m": np.full(rows, 0.0005),
        "rv_30m": np.full(rows, 0.0005),
        "ret_1m": np.zeros(rows),
        "ret_5m": np.zeros(rows),
        "ret_15m": np.zeros(rows),
        "volume_surprise_z": np.zeros(rows),
        "trades_surprise_z": np.zeros(rows),
        "taker_imbalance": np.linspace(-0.5, 0.5, rows),
        "book_imbalance5": np.zeros(rows),
        "book_imbalance20": np.zeros(rows),
        "microprice_offset_bps": np.zeros(rows),
        "spread_bps": np.ones(rows),
        "open_interest_change_pct": np.zeros(rows),
        "funding_rate": np.zeros(rows),
    })


def test_discovery_detects_up_and_down_episodes_with_pre_event_features():
    up = synthetic_panel()
    episodes, summary = discover_episodes(up, horizons=(5,), min_move_pct=0.004)
    assert any(e["direction"] == "UP" for e in episodes)
    assert all("pre_move_features" in e for e in episodes)
    assert "5m_up" in summary

    down = synthetic_panel(jump=-0.012)
    down_episodes, _ = discover_episodes(
        down, horizons=(5,), min_move_pct=0.004
    )
    assert any(e["direction"] == "DOWN" for e in down_episodes)


def test_event_threshold_uses_trailing_volatility_and_minimum_move():
    panel = synthetic_panel()
    episodes, _ = discover_episodes(
        panel, horizons=(5,), min_move_pct=0.02, volatility_multiple=2.5
    )
    assert episodes == []


def test_future_price_changes_do_not_change_pre_event_features():
    original = synthetic_panel()
    altered = original.copy()
    altered.loc[185:, "taker_imbalance"] = -0.99
    altered.loc[185:, "close"] *= 1.4
    first, _ = discover_episodes(original, horizons=(5,), min_move_pct=0.004)
    second, _ = discover_episodes(altered, horizons=(5,), min_move_pct=0.004)
    first_pre = next(e for e in first if e["start_ts"] < original.minute_ts.iloc[185])
    second_pre = next(e for e in second if e["start_ts"] < altered.minute_ts.iloc[185])
    assert first_pre["pre_move_features"]["taker_imbalance"] == (
        second_pre["pre_move_features"]["taker_imbalance"]
    )


def test_report_never_approves_strategy():
    report = build_report(synthetic_panel())
    assert report["decision"] == "discovery_only_no_strategy_approved"
    assert "causes" in report["limitations"][0]


def test_loader_handles_empty_derivatives_table(tmp_path):
    path = tmp_path / "capture.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE minute_candles (
                symbol TEXT, minute_ts INTEGER, close REAL, volume REAL, is_closed INTEGER
            );
            CREATE TABLE minute_features (
                symbol TEXT, minute_ts INTEGER, trades_count INTEGER,
                buy_quote_usdt REAL, sell_quote_usdt REAL, taker_imbalance REAL,
                avg_spread_bps REAL, avg_book_imbalance5 REAL,
                avg_book_imbalance20 REAL, avg_microprice_offset_bps REAL
            );
            CREATE TABLE derivatives_snapshots (
                symbol TEXT, ts_ms INTEGER, open_interest_base REAL, funding_rate REAL
            );
            INSERT INTO minute_candles VALUES ('BTC-USDT', 60000, 100, 10, 1);
            INSERT INTO minute_features VALUES ('BTC-USDT', 60000, 10, 60, 40, 0.2, 1, 0.1, 0.2, 0.1);
        """)
    panel = load_market_panel(str(path))
    assert len(panel) == 1
    assert np.isnan(panel.loc[0, "open_interest_change_pct"])
    assert np.isnan(panel.loc[0, "funding_rate"])
