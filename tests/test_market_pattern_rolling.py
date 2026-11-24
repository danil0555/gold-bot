import numpy as np
import pandas as pd
import pytest

import scripts.market_pattern_rolling as rolling


def _fake_pattern_report(*args, **kwargs):
    return {
        "status": "exploratory_patterns_found",
        "patterns": [{
            "symbol": "BTC-USDT",
            "horizon_minutes": 5,
            "direction": "UP",
            "conditions": [
                {"feature": "volume_surprise_z", "state": "high", "threshold": 1.0},
                {"feature": "trades_surprise_z", "state": "low", "threshold": -1.0},
            ],
            "event_support": 6,
        }],
        "feature_thresholds": {"volume_surprise_z": {"low": -1.0, "high": 1.0}},
        "volatility_cuts": {"BTC-USDT": [0.0001, 0.0002]},
        "trial_registry": {
            "candidate_hypotheses_evaluated": 144,
            "matched_symbol_horizon_direction_groups_evaluated": 1,
        },
    }


def test_rolling_walk_forward_uses_multiple_later_non_overlapping_holdouts(monkeypatch):
    panel = pd.DataFrame({
        "symbol": ["BTC-USDT"] * 240,
        "minute_ts": np.arange(240, dtype=np.int64) * 60_000,
        "close": np.full(240, 100.0),
    })
    discovery_ranges = []
    frozen_cut_arguments = []

    def fake_discover(frame):
        discovery_ranges.append((int(frame.minute_ts.min()), int(frame.minute_ts.max())))
        return [], {}

    def fake_evaluate(holdout, episodes, patterns, thresholds, frozen_volatility_cuts=None):
        frozen_cut_arguments.append(frozen_volatility_cuts)
        return {
            "holdout_event_count": 3,
            "patterns": [{
                "symbol": "BTC-USDT",
                "horizon_minutes": 5,
                "direction": "UP",
                "conditions": patterns[0]["conditions"],
                "training_event_support": 6,
                "holdout_event_total": 4,
                "holdout_event_match_coverage_pct": 80.0,
                "holdout_control_match_coverage_pct": 75.0,
                "holdout_descriptive_lift": [0.8, 1.2, 0.9][len(frozen_cut_arguments) - 1],
            }],
        }

    monkeypatch.setattr(rolling, "discover_episodes", fake_discover)
    monkeypatch.setattr(rolling, "mine_patterns", _fake_pattern_report)
    monkeypatch.setattr(rolling, "evaluate_frozen_patterns", fake_evaluate)

    report = rolling.build_rolling_walk_forward_report(panel)

    assert report["status"] == "evaluated_multiple_chronological_folds"
    assert report["configuration"]["folds_evaluated"] == 3
    assert len(report["folds"]) == 3
    assert len(frozen_cut_arguments) == 3
    assert all(cuts == {"BTC-USDT": [0.0001, 0.0002]} for cuts in frozen_cut_arguments)

    previous_holdout_end = None
    for fold in report["folds"]:
        bounds = fold["time_bounds_utc_ms"]
        assert bounds["training_end"] < bounds["holdout_start"] - 30 * 60_000
        if previous_holdout_end is not None:
            assert bounds["holdout_start"] > previous_holdout_end
        previous_holdout_end = bounds["holdout_end"]

    stability = report["pattern_stability"][0]
    assert stability["folds_selected"] == 3
    assert stability["folds_with_lift_above_1"] == 1
    assert stability["median_holdout_descriptive_lift"] == 0.9
    assert report["trial_registry"]["candidate_hypotheses_evaluated_across_folds"] == 432
    # Every fold discovers training first and holdout second; no holdout begins
    # before its own training segment ends.
    assert len(discovery_ranges) == 6
    for index in range(0, len(discovery_ranges), 2):
        train_start, train_end = discovery_ranges[index]
        holdout_start, holdout_end = discovery_ranges[index + 1]
        assert train_end < holdout_start
        assert train_start <= train_end
        assert holdout_start <= holdout_end


def test_rolling_walk_forward_rejects_overlapping_holdout_stride():
    panel = pd.DataFrame({
        "symbol": ["BTC-USDT"] * 120,
        "minute_ts": np.arange(120, dtype=np.int64) * 60_000,
    })
    with pytest.raises(ValueError, match="step_fraction"):
        rolling.build_rolling_walk_forward_report(
            panel, holdout_fraction=0.20, step_fraction=0.10
        )


def test_rolling_walk_forward_reports_insufficient_folds_without_inventing_results(monkeypatch):
    panel = pd.DataFrame({
        "symbol": ["BTC-USDT"] * 120,
        "minute_ts": np.arange(120, dtype=np.int64) * 60_000,
        "close": np.full(120, 100.0),
    })
    monkeypatch.setattr(rolling, "discover_episodes", lambda frame: ([], {}))
    monkeypatch.setattr(rolling, "mine_patterns", _fake_pattern_report)
    monkeypatch.setattr(
        rolling, "evaluate_frozen_patterns",
        lambda *args, **kwargs: {"patterns": [], "holdout_event_count": 0},
    )
    report = rolling.build_rolling_walk_forward_report(
        panel, initial_train_fraction=0.80, holdout_fraction=0.15, step_fraction=0.15
    )
    assert report["status"] == "insufficient_chronological_windows"
    assert report["configuration"]["folds_evaluated"] == 1
