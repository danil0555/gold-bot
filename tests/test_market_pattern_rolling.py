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
    bounded_holdout_discoveries = []
    frozen_cut_arguments = []

    def fake_discover(frame, start_time_bounds=None):
        discovery_ranges.append((int(frame.minute_ts.min()), int(frame.minute_ts.max())))
        if start_time_bounds is not None:
            bounded_holdout_discoveries.append({
                "frame_end": int(frame.minute_ts.max()),
                "bounds": start_time_bounds,
            })
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
    # Every fold discovers training first and holdout second. The holdout
    # discovery frame includes a future label tail, but event starts are bounded
    # to the original holdout window and evaluation controls remain holdout-only.
    assert len(discovery_ranges) == 6
    assert len(bounded_holdout_discoveries) == 3
    for index in range(0, len(discovery_ranges), 2):
        train_start, train_end = discovery_ranges[index]
        label_start, label_frame_end = discovery_ranges[index + 1]
        bounds = bounded_holdout_discoveries[index // 2]["bounds"]
        assert train_end < bounds[0]
        assert train_start <= train_end
        assert label_start == bounds[0]
        assert label_frame_end >= bounds[1]
        assert bounded_holdout_discoveries[index // 2]["frame_end"] == label_frame_end

    labeling = [fold["holdout_labeling"] for fold in report["folds"]]
    assert labeling[0]["future_tail_complete_for_max_horizon"] is True
    assert labeling[1]["future_tail_complete_for_max_horizon"] is True
    assert labeling[2]["future_tail_complete_for_max_horizon"] is False
    assert labeling[2]["future_tail_minutes_available"] < 30


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
    monkeypatch.setattr(rolling, "discover_episodes", lambda frame, **kwargs: ([], {}))
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

def test_label_tail_coverage_is_per_symbol_and_requires_contiguous_minutes():
    holdout = pd.DataFrame({
        "symbol": ["BTC-USDT"] * 3 + ["ETH-USDT"] * 3,
        "minute_ts": [0, 60_000, 120_000, 0, 60_000, 120_000],
    })
    label_panel = pd.DataFrame({
        "symbol": (
            ["BTC-USDT"] * 5
            + ["ETH-USDT"] * 4
        ),
        "minute_ts": (
            [0, 60_000, 120_000, 180_000, 240_000]
            + [0, 60_000, 120_000, 240_000]
        ),
    })
    coverage = rolling._label_tail_coverage(
        holdout, label_panel, holdout_end=120_000, requested_minutes=2
    )
    assert coverage["future_tail_complete_for_max_horizon"] is False
    assert coverage["symbols_with_complete_tail"] == 1
    assert coverage["symbols_checked"] == 2
    assert coverage["future_tail_minutes_available"] == 0
    by_symbol = {item["symbol"]: item for item in coverage["per_symbol"]}
    assert by_symbol["BTC-USDT"]["contiguous_future_minutes"] == 2
    # ETH has a missing 180-second minute; a later timestamp must not
    # disguise the gap as a complete future-label window.
    assert by_symbol["ETH-USDT"]["contiguous_future_minutes"] == 0
