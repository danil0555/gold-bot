#!/usr/bin/env python3
"""Repeated expanding-window validation for pre-move market patterns.

Every fold discovers and mines on an older training segment, freezes feature
thresholds and volatility cuts, then evaluates unchanged patterns on a later
holdout segment. Research only; no signals or orders are produced.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.market_move_discovery import discover_episodes, load_market_panel
from scripts.market_pattern_mining import mine_patterns
from scripts.market_pattern_holdout import evaluate_frozen_patterns

PURGE_MINUTES_REQUIRED = 30


def _pattern_identity(pattern: dict) -> tuple:
    conditions = tuple(sorted(
        (str(item.get("feature")), str(item.get("state")))
        for item in pattern.get("conditions", [])
    ))
    return (
        str(pattern.get("symbol")),
        int(pattern.get("horizon_minutes", 0)),
        str(pattern.get("direction")),
        conditions,
    )


def build_rolling_walk_forward_report(
    panel: pd.DataFrame,
    initial_train_fraction: float = 0.50,
    holdout_fraction: float = 0.15,
    step_fraction: float = 0.15,
    purge_minutes: int = PURGE_MINUTES_REQUIRED,
    min_support: int = 5,
) -> dict:
    """Evaluate patterns across multiple non-overlapping chronological holdouts.

    Training expands from the beginning of the dataset. A time-based purge gap
    separates each training segment from its holdout. The holdout stride must
    be at least its width, preventing overlapping test windows.
    """
    if not 0.40 <= initial_train_fraction <= 0.80:
        raise ValueError("initial_train_fraction must be between 0.40 and 0.80")
    if not 0.05 <= holdout_fraction <= 0.25:
        raise ValueError("holdout_fraction must be between 0.05 and 0.25")
    if not holdout_fraction <= step_fraction <= 0.30:
        raise ValueError("step_fraction must be >= holdout_fraction and <= 0.30")
    if purge_minutes < PURGE_MINUTES_REQUIRED:
        raise ValueError("purge_minutes must be at least the maximum event horizon (30m)")
    if min_support < 2:
        raise ValueError("min_support must be >= 2")
    if panel.empty or not {"minute_ts", "symbol"}.issubset(panel.columns):
        raise ValueError("panel must contain symbol and minute_ts columns")

    timestamps = np.sort(
        pd.to_numeric(panel["minute_ts"], errors="coerce").dropna().unique()
    )
    total = len(timestamps)
    if total < 100:
        raise ValueError("at least 100 unique timestamps are required")

    initial_count = max(1, int(total * initial_train_fraction))
    holdout_count = max(1, int(total * holdout_fraction))
    step_count = max(holdout_count, int(total * step_fraction))
    purge_ms = int(purge_minutes) * 60_000

    folds = []
    aggregate_trials = 0
    trial_registry_by_fold = []
    pattern_observations: dict[tuple, list[dict]] = []
    fold_index = 0
    start_index = initial_count

    while start_index + holdout_count <= total:
        fold_index += 1
        holdout_start = int(timestamps[start_index])
        holdout_last = int(timestamps[start_index + holdout_count - 1])
        training_cutoff = holdout_start - purge_ms

        train = panel.loc[pd.to_numeric(panel["minute_ts"], errors="coerce") < training_cutoff].copy()
        holdout = panel.loc[
            (pd.to_numeric(panel["minute_ts"], errors="coerce") >= holdout_start)
            & (pd.to_numeric(panel["minute_ts"], errors="coerce") <= holdout_last)
        ].copy()

        train_times = pd.to_numeric(train["minute_ts"], errors="coerce").dropna()
        holdout_times = pd.to_numeric(holdout["minute_ts"], errors="coerce").dropna()
        if train_times.nunique() < 50 or holdout_times.nunique() < 10:
            folds.append({
                "fold": fold_index,
                "status": "insufficient_rows_after_purge",
                "training_rows": int(len(train)),
                "holdout_rows": int(len(holdout)),
            })
            start_index += step_count
            continue

        # Discovery on each side is independent. Training labels are computed
        # only within the pre-holdout training panel, so their forward horizon
        # cannot cross the purge boundary.
        train_episodes, _ = discover_episodes(train)
        holdout_episodes, _ = discover_episodes(holdout)
        training_report = mine_patterns(train, train_episodes, min_support=min_support)
        frozen_patterns = training_report.get("patterns", [])
        frozen_thresholds = training_report.get("feature_thresholds", {})
        frozen_cuts = training_report.get("volatility_cuts", {})

        evaluation = evaluate_frozen_patterns(
            holdout,
            holdout_episodes,
            frozen_patterns,
            frozen_thresholds,
            frozen_volatility_cuts=frozen_cuts,
        )

        trial_registry = training_report.get("trial_registry", {})
        tested = int(trial_registry.get("candidate_hypotheses_evaluated", 0))
        aggregate_trials += tested
        trial_registry_by_fold.append({
            "fold": fold_index,
            "candidate_hypotheses_evaluated": tested,
            "matched_groups_evaluated": int(
                trial_registry.get("matched_symbol_horizon_direction_groups_evaluated", 0)
            ),
            "reported_patterns": len(frozen_patterns),
        })

        for pattern_result in evaluation.get("patterns", []):
            identity = _pattern_identity(pattern_result)
            pattern_observations.setdefault(identity, []).append({
                "fold": fold_index,
                "training_event_support": int(pattern_result.get("training_event_support", 0)),
                "holdout_event_total": int(pattern_result.get("holdout_event_total", 0)),
                "holdout_event_match_coverage_pct": pattern_result.get(
                    "holdout_event_match_coverage_pct"
                ),
                "holdout_control_match_coverage_pct": pattern_result.get(
                    "holdout_control_match_coverage_pct"
                ),
                "holdout_descriptive_lift": pattern_result.get("holdout_descriptive_lift"),
            })

        folds.append({
            "fold": fold_index,
            "status": "evaluated",
            "time_bounds_utc_ms": {
                "training_start": int(train_times.min()),
                "training_end": int(train_times.max()),
                "holdout_start": int(holdout_times.min()),
                "holdout_end": int(holdout_times.max()),
                "purge_minutes": int(purge_minutes),
            },
            "row_counts": {
                "training": int(len(train)),
                "holdout": int(len(holdout)),
            },
            "event_counts": {
                "training": int(len(train_episodes)),
                "holdout": int(len(holdout_episodes)),
            },
            "training_status": training_report.get("status"),
            "training_pattern_count": len(frozen_patterns),
            "training_trial_registry": trial_registry,
            "holdout": evaluation,
        })
        start_index += step_count

    evaluated_folds = [fold for fold in folds if fold.get("status") == "evaluated"]
    stability = []
    for identity, observations in pattern_observations.items():
        lifts = [
            float(item["holdout_descriptive_lift"])
            for item in observations
            if item["holdout_descriptive_lift"] is not None
            and np.isfinite(float(item["holdout_descriptive_lift"]))
        ]
        stability.append({
            "symbol": identity[0],
            "horizon_minutes": identity[1],
            "direction": identity[2],
            "conditions": [
                {"feature": feature, "state": state}
                for feature, state in identity[3]
            ],
            "folds_selected": len(observations),
            "folds_with_finite_holdout_lift": len(lifts),
            "folds_with_lift_above_1": sum(value > 1.0 for value in lifts),
            "median_holdout_descriptive_lift": (
                round(float(np.median(lifts)), 3) if lifts else None
            ),
            "minimum_holdout_descriptive_lift": (
                round(float(np.min(lifts)), 3) if lifts else None
            ),
            "maximum_holdout_descriptive_lift": (
                round(float(np.max(lifts)), 3) if lifts else None
            ),
            "observations": observations,
        })
    stability.sort(
        key=lambda item: (
            item["folds_with_lift_above_1"],
            item["folds_with_finite_holdout_lift"],
            item["folds_selected"],
        ),
        reverse=True,
    )

    return {
        "report_type": "repeated_expanding_walk_forward_patterns_v1",
        "decision": "exploratory_validation_only_no_strategy_approved",
        "status": (
            "evaluated_multiple_chronological_folds"
            if len(evaluated_folds) >= 2
            else "insufficient_chronological_windows"
        ),
        "configuration": {
            "initial_train_fraction": initial_train_fraction,
            "holdout_fraction": holdout_fraction,
            "step_fraction": step_fraction,
            "purge_minutes": purge_minutes,
            "min_support": min_support,
            "unique_timestamps": total,
            "folds_planned": len(folds),
            "folds_evaluated": len(evaluated_folds),
        },
        "trial_registry": {
            "folds_with_trial_counts": trial_registry_by_fold,
            "candidate_hypotheses_evaluated_across_folds": aggregate_trials,
            "note": (
                "The sum counts repeated search attempts across folds; folds are not "
                "independent and this is not a multiple-testing correction."
            ),
        },
        "pattern_stability": stability,
        "folds": folds,
        "methodology": [
            "Expanding training windows use only timestamps earlier than each holdout.",
            "Every holdout is a later, non-overlapping chronological window.",
            "A time-based purge gap of at least 30 minutes separates training from holdout.",
            "Episode discovery, feature thresholds, pattern selection and ranking are refit on training only in each fold.",
            "Per-symbol volatility cut points are frozen from that fold's training panel and reused unchanged in its holdout.",
            "Pattern recurrence is summarized by symbol, horizon, direction and feature/state combination; thresholds may differ by fold.",
            "All folds and negative or missing holdout outcomes remain in the report; patterns are not selected by their best fold.",
        ],
        "limitations": [
            "Adjacent expanding training sets overlap, so fold results are dependent rather than independent replications.",
            "Pattern mining still searches many feature combinations; the trial registry is descriptive and does not correct multiple testing.",
            "UTC-hour and volatility-tercile matching is coarse; exact volatility, liquidity and BTC-wide market state are not fully controlled.",
            "Small event counts, sparse data and missing holdout matches can make lift undefined or unstable.",
            "No execution costs, slippage, funding, market impact or executable entry rules are modeled.",
            "A repeated descriptive association is not causal proof and does not approve a trading strategy.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--output", default="research-output/market_pattern_rolling.json")
    parser.add_argument("--initial-train-fraction", type=float, default=0.50)
    parser.add_argument("--holdout-fraction", type=float, default=0.15)
    parser.add_argument("--step-fraction", type=float, default=0.15)
    parser.add_argument("--purge-minutes", type=int, default=30)
    parser.add_argument("--min-support", type=int, default=5)
    args = parser.parse_args()
    panel = load_market_panel(args.db)
    report = build_rolling_walk_forward_report(
        panel,
        initial_train_fraction=args.initial_train_fraction,
        holdout_fraction=args.holdout_fraction,
        step_fraction=args.step_fraction,
        purge_minutes=args.purge_minutes,
        min_support=args.min_support,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "folds_evaluated": report["configuration"]["folds_evaluated"],
        "patterns_tracked": len(report["pattern_stability"]),
        "decision": report["decision"],
        "output": str(output),
    }, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
