#!/usr/bin/env python3
"""Cross-symbol transfer test for pre-move feature patterns.

Patterns are discovered on the training panel. Their feature/state definitions
are transferred to other symbols, while numeric cutoffs are re-fitted from each
target symbol's training history only. Holdout rows are never used to fit cuts.
Research-only; no signals or orders are produced.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.market_move_discovery import discover_episodes, load_market_panel
from scripts.market_pattern_mining import (
    _build_control_frame, _volatility_cuts_from_panel, mine_patterns,
)

PURGE_MINUTES = 30


def _wilson_interval(successes: int, trials: int, z: float = 1.959963984540054) -> dict:
    """Two-sided Wilson score interval for a binomial proportion."""
    successes = int(successes)
    trials = int(trials)
    if trials <= 0:
        return {"lower": None, "upper": None, "confidence_level_pct": 95.0}
    if successes < 0 or successes > trials:
        raise ValueError("successes must be between zero and trials")
    proportion = successes / trials
    z2 = z * z
    denominator = 1.0 + z2 / trials
    center = (proportion + z2 / (2.0 * trials)) / denominator
    margin = (
        z * np.sqrt(proportion * (1.0 - proportion) / trials + z2 / (4.0 * trials * trials))
        / denominator
    )
    return {
        "lower": round(max(0.0, center - margin), 6),
        "upper": round(min(1.0, center + margin), 6),
        "confidence_level_pct": 95.0,
    }


def _target_feature_cuts(train_target: pd.DataFrame, features: set[str]) -> dict:
    """Fit 20/80 percentile state boundaries on one target symbol's train rows."""
    cuts = {}
    for feature in sorted(features):
        if feature not in train_target:
            continue
        values = pd.to_numeric(train_target[feature], errors="coerce").to_numpy(float)
        values = values[np.isfinite(values)]
        if len(values) < 20:
            continue
        low, high = np.quantile(values, [0.20, 0.80])
        if np.isfinite(low) and np.isfinite(high) and low < high:
            cuts[feature] = {"low": float(low), "high": float(high)}
    return cuts


def _condition_mask(frame: pd.DataFrame, conditions: list[dict], cuts: dict) -> pd.Series:
    mask = pd.Series(True, index=frame.index, dtype=bool)
    for condition in conditions:
        feature = str(condition["feature"])
        if feature not in frame or feature not in cuts:
            return pd.Series(False, index=frame.index, dtype=bool)
        values = pd.to_numeric(frame[feature], errors="coerce")
        boundary = float(cuts[feature][condition["state"]])
        mask &= values.ge(boundary) if condition["state"] == "high" else values.le(boundary)
    return mask.fillna(False)


def _event_frame(episodes: list[dict]) -> pd.DataFrame:
    rows = []
    for episode in episodes:
        row = {
            "symbol": str(episode["symbol"]),
            "minute_ts": int(episode["start_ts"]),
            "horizon": int(episode["horizon_minutes"]),
            "direction": str(episode["direction"]),
        }
        row.update(episode.get("pre_move_features", {}))
        rows.append(row)
    frame = pd.DataFrame(rows)
    if not frame.empty:
        frame["utc_hour"] = (frame["minute_ts"] // 3_600_000 % 24).astype(int)
    return frame


def _add_volatility_bucket(frame: pd.DataFrame, panel: pd.DataFrame, cuts: dict) -> pd.DataFrame:
    result = frame.copy()
    result["volatility_bucket"] = -1
    if result.empty:
        return result
    for symbol in result["symbol"].astype(str).unique():
        low_high = cuts.get(symbol)
        if not low_high or low_high[0] is None or low_high[1] is None:
            continue
        low, high = map(float, low_high)
        mask = result["symbol"].astype(str).eq(symbol)
        rv = pd.to_numeric(result.loc[mask].get("rv_60m"), errors="coerce") if "rv_60m" in result else pd.Series(np.nan, index=result.loc[mask].index)
        if rv.isna().any():
            lookup = panel.loc[panel["symbol"].astype(str).eq(symbol)].drop_duplicates("minute_ts").set_index("minute_ts")
            fallback = result.loc[mask, "minute_ts"].map(lookup["rv_60m"]) if "rv_60m" in lookup else pd.Series(np.nan, index=rv.index)
            rv = rv.fillna(fallback)
        valid = rv.notna()
        buckets = rv.gt(low).astype(int) + rv.gt(high).astype(int)
        result.loc[mask & valid, "volatility_bucket"] = buckets.loc[valid].astype(int)
    return result


def evaluate_cross_symbol_transfer(
    train_panel: pd.DataFrame,
    holdout_panel: pd.DataFrame,
    train_episodes: list[dict],
    holdout_episodes: list[dict],
    min_support: int = 5,
    max_source_patterns: int = 30,
) -> dict:
    """Discover on training symbols; evaluate each frozen state pattern on other symbols."""
    if min_support < 2:
        raise ValueError("min_support must be >= 2")
    training = mine_patterns(train_panel, train_episodes, min_support=min_support,
                             max_patterns=max_source_patterns)
    source_patterns = training.get("patterns", [])
    train_symbols = set(train_panel["symbol"].astype(str).unique())
    holdout_symbols = set(holdout_panel["symbol"].astype(str).unique())
    events = _event_frame(holdout_episodes)
    target_vol_cuts = _volatility_cuts_from_panel(train_panel)
    controls = _build_control_frame(
        holdout_panel, holdout_episodes, volatility_cuts=target_vol_cuts
    )
    if not controls.empty:
        controls = controls.copy()
    if not events.empty:
        events = _add_volatility_bucket(events, holdout_panel, target_vol_cuts)

    results = []
    for pattern in source_patterns:
        source = str(pattern["symbol"])
        horizon = int(pattern["horizon_minutes"])
        direction = str(pattern["direction"])
        conditions = pattern["conditions"]
        feature_names = {str(item["feature"]) for item in conditions}
        for target in sorted((train_symbols & holdout_symbols) - {source}):
            target_train = train_panel.loc[train_panel["symbol"].astype(str).eq(target)]
            target_events = events.loc[
                events["symbol"].astype(str).eq(target)
                & events["horizon"].eq(horizon)
                & events["direction"].eq(direction)
            ].copy() if not events.empty else pd.DataFrame()
            target_controls = controls.loc[
                controls["symbol"].astype(str).eq(target)
                & controls["horizon"].eq(horizon)
                & controls["direction"].eq(direction)
            ].copy() if not controls.empty else pd.DataFrame()
            cuts = _target_feature_cuts(target_train, feature_names)
            if len(cuts) != len(feature_names):
                results.append({
                    "source_symbol": source, "target_symbol": target,
                    "horizon_minutes": horizon, "direction": direction,
                    "status": "insufficient_target_training_feature_coverage",
                    "features_with_target_cuts": sorted(cuts),
                    "features_required": sorted(feature_names),
                })
                continue
            if target_events.empty or target_controls.empty:
                results.append({
                    "source_symbol": source, "target_symbol": target,
                    "horizon_minutes": horizon, "direction": direction,
                    "status": "insufficient_target_holdout_events_or_controls",
                    "target_event_count": int(len(target_events)),
                    "target_control_count": int(len(target_controls)),
                    "target_feature_cuts": cuts,
                    "target_volatility_cuts_source": "training_only",
                })
                continue
            event_count_before_matching = int(len(target_events))
            control_count_before_matching = int(len(target_controls))
            common = set(map(tuple, target_events[["utc_hour", "volatility_bucket"]].drop_duplicates().to_numpy())) & set(
                map(tuple, target_controls[["utc_hour", "volatility_bucket"]].drop_duplicates().to_numpy())
            )
            if common:
                keys = ["utc_hour", "volatility_bucket"]
                target_events = target_events.loc[target_events[keys].apply(tuple, axis=1).isin(common)].copy()
                target_controls = target_controls.loc[target_controls[keys].apply(tuple, axis=1).isin(common)].copy()
            else:
                target_events = target_events.iloc[0:0]
                target_controls = target_controls.iloc[0:0]
            event_hits = int(_condition_mask(target_events, conditions, cuts).sum())
            control_hit_mask = _condition_mask(target_controls, conditions, cuts)
            control_rate = None
            event_rate = event_hits / len(target_events) if len(target_events) else None
            if len(target_events) and len(target_controls):
                weights = target_events.groupby(["utc_hour", "volatility_bucket"]).size()
                weighted_rate = 0.0
                weight_total = 0
                for key, weight in weights.items():
                    key = key if isinstance(key, tuple) else (key,)
                    mask = pd.Series(True, index=target_controls.index)
                    for col, value in zip(["utc_hour", "volatility_bucket"], key):
                        mask &= target_controls[col].eq(value)
                    stratum = target_controls.loc[mask]
                    if len(stratum):
                        weighted_rate += float(control_hit_mask.loc[stratum.index].mean()) * int(weight)
                        weight_total += int(weight)
                control_rate = weighted_rate / weight_total if weight_total else None
            lift = event_rate / control_rate if event_rate is not None and control_rate is not None and control_rate > 0 else None
            results.append({
                "source_symbol": source, "target_symbol": target,
                "horizon_minutes": horizon, "direction": direction,
                "conditions": conditions,
                "status": (
                    "evaluated"
                    if len(target_events) >= min_support and len(target_controls) >= min_support
                    else "below_minimum_target_event_or_control_support"
                ),
                "training_source_event_support": int(pattern.get("event_support", 0)),
                "target_event_hits": event_hits, "target_event_total": int(len(target_events)),
                "target_control_hits": int(control_hit_mask.sum()), "target_control_total": int(len(target_controls)),
                "target_event_total_before_matching": event_count_before_matching,
                "target_control_total_before_matching": control_count_before_matching,
                "target_event_match_coverage_pct": round(
                    len(target_events) / event_count_before_matching * 100, 2
                ) if event_count_before_matching else 0.0,
                "target_control_match_coverage_pct": round(
                    len(target_controls) / control_count_before_matching * 100, 2
                ) if control_count_before_matching else 0.0,
                "matched_strata_count": int(len(common)),
                "target_event_rate_pct": round(event_rate * 100, 2) if event_rate is not None else None,
                "target_event_rate_wilson_95ci": _wilson_interval(event_hits, len(target_events)),
                "target_control_rate_pct": round(control_rate * 100, 2) if control_rate is not None else None,
                "target_control_raw_rate_pct": round(
                    float(control_hit_mask.mean()) * 100, 2
                ) if len(control_hit_mask) else None,
                "target_control_raw_rate_wilson_95ci": _wilson_interval(
                    int(control_hit_mask.sum()), len(control_hit_mask)
                ),
                "target_descriptive_lift": round(lift, 3) if lift is not None else None,
                "target_training_feature_cuts": cuts,
                "target_volatility_cuts_source": "training_only",
            })
    evaluated = [item for item in results if item.get("status") == "evaluated"]
    status_counts = {
        status: sum(item.get("status") == status for item in results)
        for status in sorted({str(item.get("status", "unknown")) for item in results})
    }
    return {
        "report_type": "cross_symbol_pattern_transfer_v1",
        "decision": "exploratory_transfer_test_only_no_strategy_approved",
        "training_pattern_count": len(source_patterns),
        "training_mining_status": training.get("status", "unknown"),
        "training_trial_registry": training.get("trial_registry", {}),
        "transfer_result_status_counts": status_counts,
        "transfer_result_rows": len(results),
        "source_pattern_count_by_symbol": {
            symbol: sum(str(p["symbol"]) == symbol for p in source_patterns)
            for symbol in sorted(train_symbols)
        },
        "transfer_pairs_evaluated": len(evaluated),
        "transfer_results": results,
        "methodology": [
            "Source patterns are mined using training rows only.",
            "A pattern is transferred only to a different target symbol.",
            "Numeric high/low cutoffs are re-fitted on that target symbol's training rows; source-symbol raw thresholds are never copied.",
            "Target holdout events and neutral controls use the same target-specific frozen state cutoffs.",
            "Controls are compared within shared target UTC-hour and training-fitted per-symbol volatility-tercile strata.",
            "A pattern below minimum target event support is reported but not counted as evaluated.",
            "Wilson 95% intervals describe raw binomial hit-rate uncertainty only; the stratified control rate and lift are descriptive and do not receive naive binomial intervals.",
            "This is descriptive transfer evidence, not causal proof, a profitability test, or a multiple-testing correction.",
        ],
        "limitations": [
            "Transfer is limited to symbols present in both train and holdout.",
            "Target event labels are based on the existing move-discovery definition.",
            "No execution costs, spread/slippage, funding, liquidity matching, confidence intervals, or formal multiple-testing correction are included.",
            "Patterns are selected from a searched training set; transfer pairs are not independent statistical trials.",
        ],
    }


def _build_holdout_label_panel(
    full_panel: pd.DataFrame,
    holdout_panel: pd.DataFrame,
    requested_tail_minutes: int = PURGE_MINUTES,
) -> pd.DataFrame:
    """Add per-symbol future candles solely to complete holdout event labels."""
    if requested_tail_minutes < 0:
        raise ValueError("requested_tail_minutes must be >= 0")
    pieces = [holdout_panel.copy()]
    for symbol, group in holdout_panel.groupby("symbol", sort=False):
        last_holdout_ts = int(pd.to_numeric(group["minute_ts"], errors="coerce").max())
        tail = full_panel.loc[
            full_panel["symbol"].astype(str).eq(str(symbol))
            & pd.to_numeric(full_panel["minute_ts"], errors="coerce").gt(last_holdout_ts)
            & pd.to_numeric(full_panel["minute_ts"], errors="coerce").le(
                last_holdout_ts + requested_tail_minutes * 60_000
            )
        ]
        if not tail.empty:
            pieces.append(tail.copy())
    combined = pd.concat(pieces, ignore_index=True)
    return combined.drop_duplicates(["symbol", "minute_ts"], keep="last").sort_values(
        ["symbol", "minute_ts"]
    ).reset_index(drop=True)


def _label_tail_coverage(
    full_panel: pd.DataFrame,
    holdout_panel: pd.DataFrame,
    requested_tail_minutes: int = PURGE_MINUTES,
) -> dict:
    """Measure contiguous future-minute coverage separately for each holdout symbol."""
    details = []
    for symbol, group in holdout_panel.groupby("symbol", sort=False):
        last_ts = int(pd.to_numeric(group["minute_ts"], errors="coerce").max())
        available = set(
            pd.to_numeric(
                full_panel.loc[
                    full_panel["symbol"].astype(str).eq(str(symbol)), "minute_ts"
                ], errors="coerce"
            ).dropna().astype("int64").tolist()
        )
        contiguous = 0
        for offset in range(1, requested_tail_minutes + 1):
            if last_ts + offset * 60_000 not in available:
                break
            contiguous += 1
        details.append({
            "symbol": str(symbol),
            "last_holdout_timestamp_utc_ms": last_ts,
            "contiguous_tail_minutes": contiguous,
            "complete": contiguous >= requested_tail_minutes,
        })
    minimum = min((item["contiguous_tail_minutes"] for item in details), default=0)
    return {
        "requested_minutes": int(requested_tail_minutes),
        "minimum_contiguous_minutes_across_symbols": int(minimum),
        "symbols_checked": len(details),
        "symbols_with_complete_tail": sum(item["complete"] for item in details),
        "complete_for_all_symbols": bool(details) and all(item["complete"] for item in details),
        "per_symbol": details,
    }


def build_cross_symbol_transfer_report(
    panel: pd.DataFrame, train_fraction: float = 0.70,
    purge_minutes: int = PURGE_MINUTES, min_support: int = 5,
) -> dict:
    if not 0.50 <= train_fraction <= 0.90:
        raise ValueError("train_fraction must be between 0.50 and 0.90")
    if purge_minutes < PURGE_MINUTES:
        raise ValueError("purge_minutes must be at least 30")
    timestamps = np.sort(pd.to_numeric(panel["minute_ts"], errors="coerce").dropna().unique())
    if len(timestamps) < 100:
        raise ValueError("at least 100 unique timestamps are required")
    split_ts = int(timestamps[int(len(timestamps) * train_fraction)])
    train_end = split_ts - purge_minutes * 60_000
    train = panel.loc[panel["minute_ts"] < train_end].copy()
    # Reserve the final 30 minutes as label-only data. This keeps the latest
    # eligible holdout events evaluable without admitting tail rows as tests
    # or controls.
    holdout_end_limit = int(timestamps[-1]) - PURGE_MINUTES * 60_000
    holdout = panel.loc[
        panel["minute_ts"].ge(split_ts) & panel["minute_ts"].le(holdout_end_limit)
    ].copy()
    if train.empty or holdout.empty:
        raise ValueError("chronological split produced an empty segment")
    train_start = int(train["minute_ts"].min())
    train_last = int(train["minute_ts"].max())
    train_label_panel = _build_holdout_label_panel(panel, train, purge_minutes)
    # Purge-gap candles may complete training labels, but event starts stay
    # inside training and the mining/control panel remains train-only.
    train_episodes, _ = discover_episodes(
        train_label_panel, start_time_bounds=(train_start, train_last)
    )
    holdout_start = int(holdout["minute_ts"].min())
    holdout_end = int(holdout["minute_ts"].max())
    label_panel = _build_holdout_label_panel(panel, holdout, PURGE_MINUTES)
    # The extra rows complete forward labels only. Event starts are explicitly
    # bounded to the original holdout interval; controls still use holdout only.
    holdout_episodes, _ = discover_episodes(
        label_panel, start_time_bounds=(holdout_start, holdout_end)
    )
    report = evaluate_cross_symbol_transfer(
        train, holdout, train_episodes, holdout_episodes, min_support=min_support
    )
    report["split"] = {
        "train_start_utc_ms": int(train["minute_ts"].min()),
        "train_end_utc_ms": int(train["minute_ts"].max()),
        "holdout_start_utc_ms": holdout_start,
        "holdout_end_utc_ms": holdout_end,
        "purge_minutes": int(purge_minutes),
        "train_rows": int(len(train)), "holdout_rows": int(len(holdout)),
        "label_tail": _label_tail_coverage(panel, holdout, PURGE_MINUTES),
        "label_only_rows": int(len(label_panel) - len(holdout)),
        "label_tail_policy": (
            "up to 30 future minutes per symbol are used only to complete event labels; "
            "event starts remain bounded to the original holdout; controls use holdout rows only"
        ),
    }
    return report


def build_rolling_cross_symbol_transfer_report(
    panel: pd.DataFrame,
    initial_train_fraction: float = 0.50,
    holdout_fraction: float = 0.15,
    step_fraction: float = 0.15,
    purge_minutes: int = PURGE_MINUTES,
    min_support: int = 5,
    max_source_patterns: int = 30,
) -> dict:
    """Evaluate source→target patterns over expanding, non-overlapping holdouts."""
    if not 0.30 <= initial_train_fraction <= 0.80:
        raise ValueError("initial_train_fraction must be between 0.30 and 0.80")
    if not 0.05 <= holdout_fraction <= 0.30:
        raise ValueError("holdout_fraction must be between 0.05 and 0.30")
    if step_fraction < holdout_fraction:
        raise ValueError("step_fraction must be >= holdout_fraction to prevent holdout overlap")
    if step_fraction <= 0 or purge_minutes < PURGE_MINUTES:
        raise ValueError("step_fraction must be positive and purge_minutes must be at least 30")
    all_timestamps = np.sort(
        pd.to_numeric(panel["minute_ts"], errors="coerce").dropna().unique()
    )
    if len(all_timestamps) < 100:
        raise ValueError("at least 100 unique timestamps are required")
    # Keep the final 30 minutes outside every evaluation window so they can
    # complete forward labels without becoming holdout observations/controls.
    evaluation_end = int(all_timestamps[-1]) - PURGE_MINUTES * 60_000
    timestamps = all_timestamps[all_timestamps <= evaluation_end]
    if len(timestamps) < 100:
        raise ValueError("at least 100 eligible timestamps are required before the label-only tail")
    n = len(timestamps)
    initial_end = int(n * initial_train_fraction)
    holdout_size = max(1, int(n * holdout_fraction))
    step_size = max(1, int(n * step_fraction))
    folds = []
    aggregate = {}
    total_trials = 0
    total_patterns = 0
    start_index = initial_end
    fold_number = 0

    while start_index < n:
        end_index = min(n, start_index + holdout_size)
        holdout_start_ts = int(timestamps[start_index])
        holdout_end_ts = int(timestamps[end_index - 1])
        train_end_ts = holdout_start_ts - purge_minutes * 60_000
        train = panel.loc[pd.to_numeric(panel["minute_ts"], errors="coerce") < train_end_ts].copy()
        holdout = panel.loc[
            pd.to_numeric(panel["minute_ts"], errors="coerce").between(
                holdout_start_ts, holdout_end_ts, inclusive="both"
            )
        ].copy()
        if train.empty or holdout.empty:
            folds.append({
                "fold": fold_number + 1, "status": "insufficient_train_or_holdout_rows",
                "time_bounds_utc_ms": {
                    "training_end": int(train["minute_ts"].max()) if not train.empty else None,
                    "holdout_start": holdout_start_ts, "holdout_end": holdout_end_ts,
                },
                "train_rows": int(len(train)), "holdout_rows": int(len(holdout)),
            })
            start_index += step_size
            fold_number += 1
            continue

        train_start_ts = int(pd.to_numeric(train["minute_ts"], errors="coerce").min())
        train_last_ts = int(pd.to_numeric(train["minute_ts"], errors="coerce").max())
        train_label_panel = _build_holdout_label_panel(panel, train, purge_minutes)
        train_episodes, _ = discover_episodes(
            train_label_panel, start_time_bounds=(train_start_ts, train_last_ts)
        )
        holdout_label_panel = _build_holdout_label_panel(panel, holdout, PURGE_MINUTES)
        holdout_episodes, _ = discover_episodes(
            holdout_label_panel,
            start_time_bounds=(holdout_start_ts, holdout_end_ts),
        )
        report = evaluate_cross_symbol_transfer(
            train, holdout, train_episodes, holdout_episodes,
            min_support=min_support, max_source_patterns=max_source_patterns,
        )
        trial_count = int(
            report.get("training_trial_registry", {}).get("candidate_hypotheses_evaluated", 0)
        )
        total_trials += trial_count
        total_patterns += int(report.get("training_pattern_count", 0))
        fold_id = fold_number + 1
        for item in report.get("transfer_results", []):
            conditions_key = json.dumps(item.get("conditions", []), sort_keys=True, separators=(",", ":"))
            key = (
                str(item.get("source_symbol", "")), str(item.get("target_symbol", "")),
                int(item.get("horizon_minutes", 0)), str(item.get("direction", "")),
                conditions_key,
            )
            entry = aggregate.setdefault(key, {
                "source_symbol": key[0], "target_symbol": key[1],
                "horizon_minutes": key[2], "direction": key[3],
                "conditions": item.get("conditions", []),
                "folds_seen": 0, "folds_evaluated": 0,
                "folds_with_lift_above_1": 0, "fold_lifts": [],
                "fold_event_support": 0, "fold_control_support": 0,
            })
            entry["folds_seen"] += 1
            if item.get("status") == "evaluated":
                entry["folds_evaluated"] += 1
                entry["fold_event_support"] += int(item.get("target_event_total", 0))
                entry["fold_control_support"] += int(item.get("target_control_total", 0))
                lift = item.get("target_descriptive_lift")
                if lift is not None:
                    entry["fold_lifts"].append(float(lift))
                    if float(lift) > 1.0:
                        entry["folds_with_lift_above_1"] += 1
            entry.setdefault("fold_results", []).append({
                "fold": fold_id, "status": item.get("status"),
                "target_event_total": item.get("target_event_total"),
                "target_control_total": item.get("target_control_total"),
                "target_descriptive_lift": item.get("target_descriptive_lift"),
            })

        folds.append({
            "fold": fold_id,
            "status": "evaluated" if report.get("transfer_result_rows", 0) else "no_transfer_results",
            "time_bounds_utc_ms": {
                "training_start": train_start_ts, "training_end": train_last_ts,
                "holdout_start": holdout_start_ts, "holdout_end": holdout_end_ts,
            },
            "train_rows": int(len(train)), "holdout_rows": int(len(holdout)),
            "training_pattern_count": int(report.get("training_pattern_count", 0)),
            "training_mining_status": report.get("training_mining_status"),
            "training_trial_registry": report.get("training_trial_registry", {}),
            "transfer_result_status_counts": report.get("transfer_result_status_counts", {}),
            "transfer_result_rows": int(report.get("transfer_result_rows", 0)),
            "transfer_pairs_evaluated": int(report.get("transfer_pairs_evaluated", 0)),
            "label_tail": _label_tail_coverage(panel, holdout, PURGE_MINUTES),
            "label_only_rows": int(len(holdout_label_panel) - len(holdout)),
        })
        start_index += step_size
        fold_number += 1

    stability = []
    for item in aggregate.values():
        lifts = item.pop("fold_lifts")
        item["median_evaluated_fold_lift"] = (
            round(float(np.median(lifts)), 3) if lifts else None
        )
        item["folds_with_lift_above_1"] = int(item["folds_with_lift_above_1"])
        stability.append(item)
    stability.sort(
        key=lambda x: (x["folds_evaluated"], x["folds_with_lift_above_1"],
                       x["median_evaluated_fold_lift"] or 0.0), reverse=True
    )
    return {
        "report_type": "rolling_cross_symbol_pattern_transfer_v1",
        "decision": "exploratory_repeated_transfer_test_only_no_strategy_approved",
        "configuration": {
            "initial_train_fraction": initial_train_fraction,
            "holdout_fraction": holdout_fraction, "step_fraction": step_fraction,
            "purge_minutes": purge_minutes, "min_support": min_support,
            "label_only_final_window_minutes": PURGE_MINUTES,
            "eligible_evaluation_timestamps": int(n),\n            "folds_planned": len(folds), "folds_evaluated": sum(
                item.get("status") in {"evaluated", "no_transfer_results"} for item in folds
            ),
        },
        "folds": folds,
        "pattern_stability": stability,
        "trial_registry": {
            "candidate_hypotheses_evaluated_across_folds": total_trials,
            "training_patterns_reported_across_folds": total_patterns,
            "note": "Expanding training windows make folds dependent; counts are descriptive, not a multiple-testing correction.",
        },
        "methodology": [
            "Training windows expand chronologically; holdout windows are later and non-overlapping.",
            "Each fold mines source patterns from training rows only and refits target-symbol feature cutoffs using that fold's target training rows.",
            "A purge gap of at least 30 minutes separates training event starts from holdout starts.",
            "Per-symbol future label tails complete event outcomes only; tail rows never enter holdout controls or evaluation observations.",
            "Recurrence is grouped by source, target, horizon, direction and feature/state conditions; fold-specific numeric cutoffs may differ.",
            "Expanding training windows overlap, so folds and their trial counts are dependent.",
        ],
        "limitations": [
            "Exploratory transfer evidence only; no live strategy approval.",
            "No fees, spread/slippage, funding, liquidity matching, formal multiple-testing correction, or dependence-adjusted confidence intervals.",
            "Wilson intervals on individual fold raw rates do not make the aggregate fold recurrence statistically independent.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--output", default="research-output/cross_symbol_transfer.json")
    parser.add_argument("--train-fraction", type=float, default=0.70)
    parser.add_argument("--purge-minutes", type=int, default=30)
    parser.add_argument("--min-support", type=int, default=5)
    args = parser.parse_args()
    panel = load_market_panel(args.db)
    report = build_cross_symbol_transfer_report(
        panel, train_fraction=args.train_fraction,
        purge_minutes=args.purge_minutes, min_support=args.min_support,
    )
    path = Path(args.output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": report["decision"],
        "training_pattern_count": report["training_pattern_count"],
        "transfer_pairs_evaluated": report["transfer_pairs_evaluated"],
        "output": str(path),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
