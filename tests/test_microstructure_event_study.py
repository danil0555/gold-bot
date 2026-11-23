import numpy as np
import pandas as pd

from scripts.microstructure_event_study import (
    add_past_only_events,
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
