import sqlite3

import numpy as np
import pandas as pd

from scripts.microstructure_event_study import (
    add_past_only_events,
    load_panel,
    clustered_se,
    summarize_events,
)


def synthetic_panel(rows=180):
    stamps = np.arange(rows, dtype=np.int64) * 60_000
    close = 100 * np.cumprod(np.ones(rows) + np.where(np.arange(rows) == 100, 0.002, 0.0001))
    return pd.DataFrame({
        "symbol": ["BTC-USDT"] * rows,
        "minute_ts": stamps,
        "close": close,
        "taker_imbalance": np.zeros(rows),
        "avg_book_imbalance20": np.zeros(rows),
        "avg_microprice_offset_bps": np.zeros(rows),
        "avg_spread_bps": np.ones(rows),
        "open_interest_base": np.linspace(1000, 1010, rows),
    })


def test_event_thresholds_are_lagged_and_do_not_use_current_observation():
    panel = synthetic_panel()
    panel.loc[60, "taker_imbalance"] = -1.0
    result = add_past_only_events(panel, lookback=60, min_periods=60)
    assert result.loc[60, "taker_q20"] == 0.0
    assert bool(result.loc[60, "event_sell_absorption"])


def test_forward_returns_require_contiguous_minute_candles():
    panel = synthetic_panel(20)
    panel.loc[6, "minute_ts"] += 60_000
    result = add_past_only_events(panel, lookback=5, min_periods=3)
    # At row 1, row 6 is five rows ahead but the timestamp gap breaks continuity.
    assert np.isnan(result.loc[1, "fwd_5m"])


def test_event_summary_subtracts_costs_and_reports_independent_clusters():
    panel = synthetic_panel(400)
    panel.loc[100:399, "taker_imbalance"] = -0.8
    panel.loc[100:399, "close"] = np.linspace(100, 101, 300)
    scored = add_past_only_events(panel, lookback=60, min_periods=30)
    report = summarize_events(scored, cost_bps=12, gap_minutes=5)
    assert report["cost_bps"] == 12
    assert "event_sell_absorption" in report["events"]
    assert report["events"]["event_sell_absorption"]["events_after_5m_thinning"] <= report["events"]["event_sell_absorption"]["raw_events"]


def test_clustered_se_requires_four_independent_time_blocks():
    values = np.array([1.0, 2.0, 1.5])
    timestamps = np.array([0, 60_000, 120_000])
    se, clusters = clustered_se(values, timestamps, block_hours=24)
    assert se is None
    assert clusters == 1



def test_load_panel_keeps_symbol_when_joining_derivatives(tmp_path):
    db_path = tmp_path / "microstructure.sqlite3"
    with sqlite3.connect(db_path) as db:
        db.executescript("""
            CREATE TABLE minute_candles (
                symbol TEXT, minute_ts INTEGER, close REAL, is_closed INTEGER
            );
            CREATE TABLE minute_features (
                symbol TEXT, minute_ts INTEGER, taker_imbalance REAL
            );
            CREATE TABLE derivatives_snapshots (
                symbol TEXT, ts_ms INTEGER, open_interest_base REAL,
                funding_rate REAL, mark_price REAL, index_price REAL
            );
        """)
        db.executemany(
            "INSERT INTO minute_candles VALUES (?,?,?,1)",
            [("BTC-USDT", 60_000, 100.0), ("BTC-USDT", 120_000, 101.0)],
        )
        db.executemany(
            "INSERT INTO minute_features VALUES (?,?,?)",
            [("BTC-USDT", 60_000, 0.1), ("BTC-USDT", 120_000, 0.2)],
        )
        db.execute(
            "INSERT INTO derivatives_snapshots VALUES (?,?,?,?,?,?)",
            ("BTC-USDT", 60_000, 1000.0, 0.0001, 100.0, 100.0),
        )

    panel = load_panel(str(db_path))
    assert "symbol" in panel.columns
    assert panel["symbol"].tolist() == ["BTC-USDT", "BTC-USDT"]
    assert panel["open_interest_base"].iloc[0] == 1000.0



def test_event_summary_scores_short_hypotheses_with_inverted_returns():
    panel = pd.DataFrame({
        "symbol": ["BTC-USDT"],
        "minute_ts": [1_800_000_000_000],
        "event_sell_absorption": [False],
        "event_buy_absorption": [True],
        "event_bid_support": [False],
        "event_oi_expansion_sell_pressure": [False],
        "fwd_1m": [-0.002],
        "fwd_5m": [-0.002],
        "fwd_15m": [-0.002],
    })
    report = summarize_events(panel, cost_bps=12, gap_minutes=0)
    short = report["events"]["event_buy_absorption"]
    assert short["hypothesis_direction"] == "SHORT"
    assert short["horizons"]["15m"]["mean_gross_bps"] == 20.0
    assert short["horizons"]["15m"]["mean_net_bps"] == 8.0
    assert short["horizons"]["15m"]["inference_status"] == "insufficient_independent_clusters"


def test_event_summary_keeps_long_hypotheses_in_forward_direction():
    panel = pd.DataFrame({
        "symbol": ["BTC-USDT"],
        "minute_ts": [1_800_000_000_000],
        "event_sell_absorption": [True],
        "event_buy_absorption": [False],
        "event_bid_support": [False],
        "event_oi_expansion_sell_pressure": [False],
        "fwd_1m": [0.002],
        "fwd_5m": [0.002],
        "fwd_15m": [0.002],
    })
    report = summarize_events(panel, cost_bps=12, gap_minutes=0)
    long = report["events"]["event_sell_absorption"]
    assert long["hypothesis_direction"] == "LONG"
    assert long["horizons"]["15m"]["mean_gross_bps"] == 20.0
    assert long["horizons"]["15m"]["mean_net_bps"] == 8.0


def test_oi_changes_and_rolling_thresholds_reset_after_capture_gap():
    panel = synthetic_panel(30)
    panel.loc[10:, "minute_ts"] += 60_000
    panel.loc[10:, "open_interest_base"] *= 2.0
    panel.loc[10:, "taker_imbalance"] = 0.9

    result = add_past_only_events(panel, lookback=3, min_periods=3)

    # The first five-minute OI delta in a new segment must not compare with
    # the previous capture, even though the previous capture has enough rows.
    assert result.loc[10:14, "oi_change_5m"].isna().all()
    assert pd.notna(result.loc[15, "oi_change_5m"])

    # Rolling thresholds need three observations from the new segment.
    assert result.loc[10:11, "taker_q20"].isna().all()
    assert result.loc[12, "taker_q20"] == 0.9
    assert result["segment_id"].nunique() == 2
