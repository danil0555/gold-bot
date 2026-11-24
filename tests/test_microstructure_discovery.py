import numpy as np
import pandas as pd

from scripts.microstructure_discovery import build_features, summarize_discovery


def sample_panel(rows=200):
    return pd.DataFrame({
        "symbol": ["BTC-USDT"] * rows,
        "minute_ts": np.arange(rows, dtype=np.int64) * 60_000,
        "close": 100 * (1 + np.arange(rows) * 0.0001),
        "volume": np.ones(rows) * 10,
        "trades_count": np.ones(rows) * 20,
        "buy_quote_usdt": np.ones(rows) * 600,
        "sell_quote_usdt": np.ones(rows) * 400,
        "cvd_quote_usdt": np.ones(rows) * 200,
        "taker_imbalance": np.linspace(-0.5, 0.5, rows),
        "avg_book_imbalance5": np.linspace(-0.4, 0.4, rows),
        "avg_book_imbalance20": np.linspace(-0.3, 0.3, rows),
        "avg_microprice_offset_bps": np.linspace(-1, 1, rows),
        "avg_spread_bps": np.ones(rows) * 2,
        "avg_bid_depth5_usdt": np.ones(rows) * 1000,
        "avg_ask_depth5_usdt": np.ones(rows) * 900,
        "avg_bid_depth20_usdt": np.ones(rows) * 2000,
        "avg_ask_depth20_usdt": np.ones(rows) * 1800,
        "open_interest_base": np.linspace(1000, 1020, rows),
        "funding_rate": np.ones(rows) * 0.0001,
    })


def test_feature_ranks_are_past_only_and_forward_returns_are_future_only():
    panel = sample_panel()
    result = build_features(panel, lookback=60, min_periods=30)
    assert np.isnan(result.loc[28, "rank_taker_imbalance"])
    assert result.loc[59, "rank_taker_imbalance"] > 0.95
    assert result.loc[100, "fwd_5m"] > 0
    changed = panel.copy()
    changed.loc[150:, "close"] *= 1.5
    changed_result = build_features(changed, lookback=60, min_periods=30)
    assert result.loc[149, "rank_taker_imbalance"] == changed_result.loc[149, "rank_taker_imbalance"]


def test_forward_and_lagged_features_reject_timestamp_gaps():
    panel = sample_panel(80)
    panel.loc[10:, "minute_ts"] += 60_000
    result = build_features(panel, lookback=10, min_periods=5)
    assert np.isnan(result.loc[5, "fwd_5m"])
    assert np.isnan(result.loc[10, "ret_1m"])
    assert np.isnan(result.loc[14, "momentum_5m"])


def test_discovery_report_never_approves_single_capture():
    result = build_features(sample_panel(), lookback=60, min_periods=30)
    report = summarize_discovery(result)
    assert report["decision"] == "exploration_only_no_strategy_approved"
    assert report["minimum_independent_24h_clusters_for_inference"] == 4
    feature = report["results"]["5m"]["taker_imbalance"]
    assert feature["candidate_status"] == "not_eligible_single_capture_exploratory_only"
    assert feature["top_decile_long"]["cluster_count_24h"] == 1
