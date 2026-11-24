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
                })
                continue
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
                "conditions": conditions, "status": "evaluated" if len(target_events) >= min_support else "below_minimum_target_support",
                "training_source_event_support": int(pattern.get("event_support", 0)),
                "target_event_hits": event_hits, "target_event_total": int(len(target_events)),
                "target_control_hits": int(control_hit_mask.sum()), "target_control_total": int(len(target_controls)),
                "matched_strata_count": int(len(common)),
                "target_event_rate_pct": round(event_rate * 100, 2) if event_rate is not None else None,
                "target_control_rate_pct": round(control_rate * 100, 2) if control_rate is not None else None,
                "target_descriptive_lift": round(lift, 3) if lift is not None else None,
                "target_training_feature_cuts": cuts,
                "target_volatility_cuts_source": "training_only",
            })
    evaluated = [item for item in results if item.get("status") == "evaluated"]
    return {
        "report_type": "cross_symbol_pattern_transfer_v1",
        "decision": "exploratory_transfer_test_only_no_strategy_approved",
        "training_pattern_count": len(source_patterns),
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
