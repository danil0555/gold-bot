import numpy as np
import pandas as pd
import pytest

from scripts.market_walkforward import (
    add_cross_sectional_context,
    assign_regimes,
    enforce_validation_abstention,
    make_walk_forward_folds,
    metrics_with_row_thresholds,
)


def test_walk_forward_folds_are_expanding_and_non_overlapping():
    day = 86_400_000
    folds = make_walk_forward_folds(
        0, 180 * day, train_days=60, validation_days=30,
        test_days=30, step_days=30,
    )
    assert len(folds) == 3
    assert folds[0]["train_start_ms"] == 0
    assert folds[0]["train_end_ms"] == 60 * day
    assert folds[0]["validation_start_ms"] == 60 * day
    assert folds[0]["test_start_ms"] == 90 * day
    assert folds[-1]["test_end_ms"] == 180 * day
    for left, right in zip(folds, folds[1:]):
        assert left["test_end_ms"] == right["test_start_ms"]


def test_walk_forward_rejects_overlapping_test_windows():
    with pytest.raises(ValueError):
        make_walk_forward_folds(0, 180 * 86_400_000, step_days=15)


def test_cross_sectional_breadth_uses_only_non_missing_same_hour_rows():
    panel = pd.DataFrame({
        "time": [1, 1, 1, 2, 2],
        "ret_24h": [0.02, -0.01, np.nan, 0.01, -0.03],
    })
    out = add_cross_sectional_context(panel)
    assert np.allclose(out.loc[:2, "market_breadth_24h"], 0.5)
    assert np.allclose(out.loc[3:, "market_breadth_24h"], 0.5)
    assert out.loc[:2, "market_dispersion_24h"].notna().all()


def test_regime_volatility_cutoff_comes_only_from_training_rows():
    panel = pd.DataFrame({
        "btc_ret_24h": [-0.02, 0.00, 0.03, 0.04],
        "btc_rv_24h": [0.01, 0.02, 0.03, 100.0],
        "market_breadth_24h": [0.2, 0.5, 0.8, 0.9],
    })
    training_mask = pd.Series([True, True, True, False])
    out, cutoff = assign_regimes(panel, training_mask)
    assert cutoff < 1.0
    assert out.loc[0, "btc_regime"].startswith("down_")
    assert out.loc[1, "btc_regime"].startswith("sideways_")
    assert out.loc[2, "btc_regime"].startswith("up_")
    assert out.loc[0, "breadth_regime"] == "risk_off_breadth"
    assert out.loc[1, "breadth_regime"] == "mixed_breadth"
    assert out.loc[2, "breadth_regime"] == "risk_on_breadth"


def test_aggregate_metrics_apply_each_fold_threshold_to_its_own_rows():
    result = metrics_with_row_thresholds(
        y_true=[1, 0, 1, 0],
        probabilities=[0.70, 0.30, 0.60, 0.40],
        forward_returns=[0.01, -0.01, -0.02, 0.02],
        costs=0.001,
        thresholds=[0.65, 0.65, 0.75, 0.75],
        timestamps=[0, 3_600_000, 7_200_000, 10_800_000],
    )
    assert result["signals"] == 2
    assert result["mean_net_return_pct"] == 0.9



def test_validation_with_negative_conservative_edge_forces_no_signals():
    threshold, report = enforce_validation_abstention(
        0.58,
        {
            "method": "validation_mean_net_return_minus_one_standard_error",
            "validation_score_pct": -0.12,
            "validation_cluster_count": 10,
        },
    )
    assert threshold > 1.0
    assert report["deployment_decision"] == "abstain"


def test_validation_with_too_few_time_clusters_forces_no_signals():
    threshold, report = enforce_validation_abstention(
        0.58,
        {
            "method": "validation_mean_net_return_minus_one_standard_error",
            "validation_score_pct": 0.20,
            "validation_cluster_count": 2,
        },
    )
    assert threshold > 1.0
    assert report["deployment_decision"] == "abstain"


def test_validation_positive_edge_is_only_a_candidate_not_deployment_approval():
    threshold, report = enforce_validation_abstention(
        0.62,
        {
            "method": "validation_mean_net_return_minus_one_standard_error",
            "validation_score_pct": 0.20,
            "validation_cluster_count": 10,
        },
    )
    assert threshold == 0.62
    assert report["deployment_decision"] == "validation_candidate_only"
