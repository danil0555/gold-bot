import numpy as np
import pandas as pd
import pytest

from scripts.market_pattern_holdout import (
    chronological_split,
    evaluate_frozen_patterns,
)


def test_chronological_split_has_no_overlap_and_respects_purge_gap():
    panel = pd.DataFrame({
        "symbol": ["BTC-USDT"] * 200,
        "minute_ts": np.arange(200, dtype=np.int64) * 60_000,
        "close": np.full(200, 100.0),
    })
    train, holdout, split_ts = chronological_split(panel, 0.70, 30)
    assert train.minute_ts.max() < split_ts - 30 * 60_000
    assert holdout.minute_ts.min() == split_ts
    assert train.minute_ts.max() < holdout.minute_ts.min()


def test_split_rejects_short_history_and_too_small_purge():
    panel = pd.DataFrame({"minute_ts": np.arange(50, dtype=np.int64) * 60_000})
    with pytest.raises(ValueError):
        chronological_split(panel)
    longer = pd.DataFrame({"minute_ts": np.arange(120, dtype=np.int64) * 60_000})
    with pytest.raises(ValueError):
        chronological_split(longer, purge_minutes=5)


def test_frozen_pattern_is_evaluated_without_refitting_thresholds():
    holdout = pd.DataFrame({
        "symbol": ["BTC-USDT"] * 12,
        "minute_ts": np.arange(12, dtype=np.int64) * 60_000,
        "close": np.full(12, 100.0),
        "rv_60m": np.full(12, 0.0001),
        "rv_30m": np.full(12, 0.0001),
        "volume_surprise_z": np.linspace(-2, 2, 12),
        "trades_surprise_z": np.linspace(-2, 2, 12),
    })
    episodes = [{
        "symbol": "BTC-USDT",
        "horizon_minutes": 5,
        "start_ts": 6 * 60_000,
        "direction": "UP",
        "pre_move_features": {
            "volume_surprise_z": 1.5,
            "trades_surprise_z": 1.5,
        },
    }]
    frozen = [{
        "symbol": "BTC-USDT",
        "horizon_minutes": 5,
        "direction": "UP",
        "conditions": [
            {"feature": "volume_surprise_z", "state": "high", "threshold": 1.0},
            {"feature": "trades_surprise_z", "state": "high", "threshold": 1.0},
        ],
        "event_support": 6,
    }]
    result = evaluate_frozen_patterns(holdout, episodes, frozen, {
        "volume_surprise_z": {"low": -1.0, "high": 1.0},
        "trades_surprise_z": {"low": -1.0, "high": 1.0},
    })
    pattern = result["patterns"][0]
    assert pattern["training_event_support"] == 6
    assert pattern["holdout_event_hits"] == 1
    assert pattern["holdout_event_total"] == 1
    assert pattern["conditions"][0]["threshold"] == 1.0
    assert "holdout_descriptive_lift" in pattern
    assert pattern["holdout_matched_strata_count"] >= 1
