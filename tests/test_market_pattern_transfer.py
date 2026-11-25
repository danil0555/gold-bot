import numpy as np
import pandas as pd

from scripts.market_pattern_transfer import (
    _build_holdout_label_panel, _condition_mask, _label_tail_coverage,
    _wilson_interval,
    _target_feature_cuts, build_cross_symbol_transfer_report,
    build_rolling_cross_symbol_transfer_report, evaluate_cross_symbol_transfer,
)


def test_target_state_cutoffs_are_fitted_from_target_training_rows():
    target = pd.DataFrame({"feature_a": np.arange(100, dtype=float)})
    cuts = _target_feature_cuts(target, {"feature_a"})
    assert cuts["feature_a"]["low"] == 19.8
    assert cuts["feature_a"]["high"] == 79.2


def test_condition_mask_uses_target_cutoffs_not_source_numeric_thresholds():
    frame = pd.DataFrame({"feature_a": [0.0, 50.0, 100.0]})
    conditions = [{"feature": "feature_a", "state": "high", "threshold": 999999.0}]
    mask = _condition_mask(frame, conditions, {"feature_a": {"low": 20.0, "high": 80.0}})
    assert mask.tolist() == [False, False, True]


def test_cross_symbol_transfer_never_transfers_to_same_symbol(monkeypatch):
    import scripts.market_pattern_transfer as transfer

    train = pd.DataFrame({
        "symbol": ["BTC-USDT"] * 30 + ["ETH-USDT"] * 30,
        "minute_ts": list(range(30)) + list(range(30)),
        "feature_a": list(np.linspace(-2, 2, 30)) * 2,
        "feature_b": list(np.linspace(2, -2, 30)) * 2,
        "rv_60m": [0.001] * 60,
    })
    holdout = pd.DataFrame({
        "symbol": ["BTC-USDT"] * 30 + ["ETH-USDT"] * 30,
        "minute_ts": list(range(100, 130)) + list(range(100, 130)),
        "feature_a": list(np.linspace(-2, 2, 30)) * 2,
        "feature_b": list(np.linspace(2, -2, 30)) * 2,
        "rv_60m": [0.001] * 60,
        "close": [100.0] * 60,
    })
    pattern = {
        "symbol": "BTC-USDT", "horizon_minutes": 5, "direction": "UP",
        "conditions": [
            {"feature": "feature_a", "state": "high", "threshold": 1.9},
            {"feature": "feature_b", "state": "low", "threshold": -1.9},
        ], "event_support": 8,
    }
    monkeypatch.setattr(transfer, "mine_patterns", lambda *a, **k: {"status": "ok", "patterns": [pattern], "trial_registry": {"candidate_hypotheses_evaluated": 48, "reported_patterns": 1}})
    monkeypatch.setattr(
        transfer, "_build_control_frame",
        lambda *a, **k: pd.DataFrame({
            "symbol": ["ETH-USDT"] * 8,
            "horizon": [5] * 8,
            "direction": ["UP"] * 8,
            "utc_hour": [0] * 8,
            "volatility_bucket": [0] * 8,
            "feature_a": [2.0] * 8,
            "feature_b": [-2.0] * 8,
        }),
    )
    episodes = []
    for symbol in ("BTC-USDT", "ETH-USDT"):
        for index in range(8):
            episodes.append({
                "symbol": symbol, "start_ts": 100 + index,
                "horizon_minutes": 5, "direction": "UP",
                "pre_move_features": {"feature_a": 2.0, "feature_b": -2.0, "rv_60m": 0.001},
            })
    result = evaluate_cross_symbol_transfer(train, holdout, [], episodes, min_support=2)
    assert all(item["source_symbol"] != item["target_symbol"] for item in result["transfer_results"])
    assert {item["target_symbol"] for item in result["transfer_results"]} <= {"ETH-USDT"}
    evaluated = [item for item in result["transfer_results"] if item["status"] == "evaluated"]
    assert len(evaluated) == 1
    assert evaluated[0]["target_event_match_coverage_pct"] == 100.0
    assert evaluated[0]["target_control_match_coverage_pct"] == 100.0
    assert evaluated[0]["target_event_total_before_matching"] == 8
    assert evaluated[0]["target_control_total_before_matching"] == 8
    assert evaluated[0]["target_event_rate_wilson_95ci"]["lower"] < 1.0
    assert evaluated[0]["target_event_rate_wilson_95ci"]["upper"] == 1.0
    assert evaluated[0]["target_control_raw_rate_wilson_95ci"]["lower"] < 1.0
    assert evaluated[0]["target_control_raw_rate_wilson_95ci"]["upper"] == 1.0
    assert result["training_mining_status"] == "ok"
    assert result["training_trial_registry"]["candidate_hypotheses_evaluated"] == 48
    assert result["training_trial_registry"]["reported_patterns"] == 1
    assert result["transfer_result_status_counts"] == {"evaluated": 1}
    assert result["transfer_result_rows"] == 1



def test_cross_symbol_label_tail_is_added_per_symbol_but_holdout_stays_separate():
    holdout = pd.DataFrame({
        "symbol": ["BTC-USDT"] * 3 + ["ETH-USDT"] * 3,
        "minute_ts": [0, 60_000, 120_000] * 2,
        "close": [100.0] * 6,
    })
    full = pd.concat([
        holdout,
        pd.DataFrame({
            "symbol": ["BTC-USDT"] * 2 + ["ETH-USDT"],
            "minute_ts": [180_000, 240_000, 180_000],
            "close": [100.0] * 3,
        }),
    ], ignore_index=True)
    label_panel = _build_holdout_label_panel(full, holdout, requested_tail_minutes=2)
    assert len(label_panel) == len(holdout) + 3
    label_rows = set(zip(label_panel["symbol"], label_panel["minute_ts"]))
    assert ("BTC-USDT", 180_000) in label_rows
    assert ("BTC-USDT", 240_000) in label_rows
    assert ("ETH-USDT", 180_000) in label_rows
    # The evaluation/control frame itself is not mutated or extended.
    assert len(holdout) == 6
    coverage = _label_tail_coverage(full, holdout, requested_tail_minutes=2)
    assert coverage["symbols_checked"] == 2
    assert coverage["symbols_with_complete_tail"] == 1
    assert coverage["complete_for_all_symbols"] is False
    per_symbol = {item["symbol"]: item["contiguous_tail_minutes"] for item in coverage["per_symbol"]}
    assert per_symbol == {"BTC-USDT": 2, "ETH-USDT": 1}



def test_transfer_report_reserves_tail_and_bounds_event_starts(monkeypatch):
    import scripts.market_pattern_transfer as transfer

    rows = []
    for symbol in ("BTC-USDT", "ETH-USDT"):
        for minute in range(200):
            rows.append({
                "symbol": symbol, "minute_ts": minute * 60_000,
                "close": 100.0, "rv_60m": 0.001,
            })
    panel = pd.DataFrame(rows)
    discovery_calls = []

    def fake_discover(frame, **kwargs):
        discovery_calls.append((frame.copy(), kwargs))
        return [], {}

    monkeypatch.setattr(transfer, "discover_episodes", fake_discover)
    monkeypatch.setattr(
        transfer, "evaluate_cross_symbol_transfer",
        lambda *args, **kwargs: {"decision": "exploratory_transfer_test_only_no_strategy_approved"},
    )
    report = build_cross_symbol_transfer_report(panel, train_fraction=0.70, min_support=2)

    assert len(discovery_calls) == 2
    train_label_frame, train_kwargs = discovery_calls[0]
    assert train_kwargs["start_time_bounds"] == (0, 109 * 60_000)
    assert train_label_frame["minute_ts"].max() == 139 * 60_000
    holdout_label_frame, kwargs = discovery_calls[1]
    assert kwargs["start_time_bounds"] == (
        report["split"]["holdout_start_utc_ms"],
        report["split"]["holdout_end_utc_ms"],
    )
    assert holdout_label_frame["minute_ts"].max() == 199 * 60_000
    assert report["split"]["holdout_end_utc_ms"] == 169 * 60_000
    assert report["split"]["label_only_rows"] == 60
    assert report["split"]["label_tail"]["complete_for_all_symbols"] is True
    assert report["split"]["label_tail"]["symbols_with_complete_tail"] == 2



def test_wilson_interval_handles_empty_small_and_extreme_samples():
    assert _wilson_interval(0, 0) == {
        "lower": None, "upper": None, "confidence_level_pct": 95.0
    }
    zero = _wilson_interval(0, 10)
    assert zero["lower"] == 0.0
    assert 0.0 < zero["upper"] < 0.5
    all_hits = _wilson_interval(10, 10)
    assert 0.5 < all_hits["lower"] < 1.0
    assert all_hits["upper"] == 1.0
    balanced = _wilson_interval(5, 10)
    assert balanced["lower"] < 0.5 < balanced["upper"]



def test_rolling_transfer_uses_expanding_train_and_non_overlapping_holdouts(monkeypatch):
    import scripts.market_pattern_transfer as transfer

    panel = pd.DataFrame({
        "symbol": ["BTC-USDT"] * 300 + ["ETH-USDT"] * 300,
        "minute_ts": list(np.arange(300, dtype=np.int64) * 60_000) * 2,
        "close": [100.0] * 600,
        "rv_60m": [0.001] * 600,
    })
    discovery_calls = []
    evaluation_calls = []

    def fake_discover(frame, start_time_bounds=None):
        discovery_calls.append((frame.copy(), start_time_bounds))
        return [], {}

    def fake_evaluate(train, holdout, train_events, holdout_events, **kwargs):
        evaluation_calls.append((train.copy(), holdout.copy()))
        return {
            "training_pattern_count": 1,
            "training_mining_status": "ok",
            "training_trial_registry": {"candidate_hypotheses_evaluated": 10},
            "transfer_result_rows": 1,
            "transfer_pairs_evaluated": 1,
            "transfer_result_status_counts": {"evaluated": 1},
            "transfer_results": [{
                "source_symbol": "BTC-USDT", "target_symbol": "ETH-USDT",
                "horizon_minutes": 5, "direction": "UP",
                "conditions": [{"feature": "volume_z", "state": "high"}],
                "status": "evaluated", "target_event_total": 6,
                "target_control_total": 7, "target_descriptive_lift": 1.2,
            }],
        }

    monkeypatch.setattr(transfer, "discover_episodes", fake_discover)
    monkeypatch.setattr(transfer, "evaluate_cross_symbol_transfer", fake_evaluate)

    report = build_rolling_cross_symbol_transfer_report(
        panel, initial_train_fraction=0.40, holdout_fraction=0.15,
        step_fraction=0.15, min_support=2,
    )

    assert report["report_type"] == "rolling_cross_symbol_pattern_transfer_v1"
    assert report["configuration"]["folds_evaluated"] == 4
    assert report["configuration"]["trailing_eligible_timestamps_discarded"] == 2
    assert report["configuration"]["trailing_fragment_start_utc_ms"] == 268 * 60_000
    assert report["configuration"]["trailing_fragment_end_utc_ms"] == 269 * 60_000
    assert len(evaluation_calls) == 4
    previous_end = None
    previous_train_rows = 0
    for train, holdout in evaluation_calls:
        train_end = int(train.minute_ts.max())
        holdout_start = int(holdout.minute_ts.min())
        holdout_end = int(holdout.minute_ts.max())
        assert train_end < holdout_start - 30 * 60_000
        if previous_end is not None:
            assert holdout_start > previous_end
        assert len(train) > previous_train_rows
        previous_train_rows = len(train)
        previous_end = holdout_end

    assert len(discovery_calls) == 8
    assert report["trial_registry"]["candidate_hypotheses_evaluated_across_folds"] == 40
    assert len(report["pattern_stability"]) == 1
    stability = report["pattern_stability"][0]
    assert stability["folds_evaluated"] == 4
    assert stability["folds_with_lift_above_1"] == 4
    assert stability["median_evaluated_fold_lift"] == 1.2
    assert stability["fold_event_support"] == 24
    assert stability["fold_control_support"] == 28


def test_rolling_transfer_rejects_overlapping_holdout_windows():
    panel = pd.DataFrame({
        "symbol": ["BTC-USDT"] * 120,
        "minute_ts": np.arange(120, dtype=np.int64) * 60_000,
    })
    import pytest
    with pytest.raises(ValueError, match="step_fraction"):
        build_rolling_cross_symbol_transfer_report(
            panel, holdout_fraction=0.20, step_fraction=0.10
        )
