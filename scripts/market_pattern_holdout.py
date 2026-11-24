#!/usr/bin/env python3
"""Chronological train/holdout validation for pre-move feature patterns.

Patterns and feature quantiles are learned on the older training segment only.
The frozen conditions are evaluated on a later holdout segment without refitting.
This is descriptive validation, not a trading signal or proof of causality.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

from scripts.market_move_discovery import discover_episodes, load_market_panel
from scripts.market_pattern_mining import (
    FEATURES, _build_control_frame, _signature, _volatility_cuts_from_panel, mine_patterns,
)


def chronological_split(
    panel: pd.DataFrame,
    train_fraction: float = 0.70,
    purge_minutes: int = 30,
) -> tuple[pd.DataFrame, pd.DataFrame, int]:
    """Split by unique UTC timestamps and leave a purge gap between segments."""
    if not 0.50 <= train_fraction <= 0.90:
        raise ValueError("train_fraction must be between 0.50 and 0.90")
    if purge_minutes < 30:
        raise ValueError("purge_minutes must be at least the maximum event horizon (30m)")
    if panel.empty or "minute_ts" not in panel:
        raise ValueError("panel must contain minute_ts rows")
    timestamps = np.sort(pd.to_numeric(panel.minute_ts, errors="coerce").dropna().unique())
    if len(timestamps) < 100:
        raise ValueError("at least 100 unique timestamps are required for chronological split")
    split_index = int(len(timestamps) * train_fraction)
    split_ts = int(timestamps[split_index])
    train_end = split_ts - purge_minutes * 60_000
    train = panel.loc[panel.minute_ts < train_end].copy()
    holdout = panel.loc[panel.minute_ts >= split_ts].copy()
    if train.empty or holdout.empty:
        raise ValueError("chronological split produced an empty segment")
    return train, holdout, split_ts


def evaluate_frozen_patterns(
    holdout_panel: pd.DataFrame,
    holdout_episodes: list[dict],
    frozen_patterns: list[dict],
    frozen_thresholds: dict,
    frozen_volatility_cuts: dict | None = None,
) -> dict:
    """Evaluate unchanged training patterns against later events and neutral controls."""
    events = []
    for episode in holdout_episodes:
        row = {
            "symbol": str(episode["symbol"]),
            "minute_ts": int(episode["start_ts"]),
            "horizon": int(episode["horizon_minutes"]),
            "direction": str(episode["direction"]),
        }
        row.update(episode.get("pre_move_features", {}))
        events.append(row)
    event_frame = pd.DataFrame(events)
    volatility_cut_source = "training_frozen" if frozen_volatility_cuts is not None else "holdout_fallback_compatibility"
    if frozen_volatility_cuts is None:
        # Compatibility for direct callers; the walk-forward pipeline always
        # supplies cuts fitted on the training segment.
        frozen_volatility_cuts = _volatility_cuts_from_panel(holdout_panel)
    controls = _build_control_frame(
        holdout_panel, holdout_episodes, volatility_cuts=frozen_volatility_cuts
    )
    if not event_frame.empty:
        for optional_feature in ("rv_60m", "rv_30m"):
            if optional_feature not in event_frame:
                event_frame[optional_feature] = np.nan
        event_frame["utc_hour"] = (pd.to_numeric(
            pd.Series([e["start_ts"] for e in holdout_episodes]), errors="coerce"
        ) // 3_600_000 % 24).astype(int).to_numpy()
        event_frame["volatility_bucket"] = -1
        for symbol, group in holdout_panel.groupby("symbol", sort=False):
            raw_cuts = frozen_volatility_cuts.get(str(symbol), (None, None))
            if raw_cuts[0] is None or raw_cuts[1] is None:
                continue
            low_cut, high_cut = map(float, raw_cuts)
            mask = event_frame["symbol"].astype(str).eq(str(symbol))
            event_rv = pd.to_numeric(event_frame.loc[mask, "rv_60m"], errors="coerce")
            missing_rv = event_rv.isna()
            if missing_rv.any():
                lookup_cols = ["minute_ts"] + [
                    col for col in ("rv_60m", "rv_30m") if col in holdout_panel.columns
                ]
                stamps = event_frame.loc[mask, "minute_ts"]
                fallback = pd.Series(np.nan, index=stamps.index, dtype=float)
                if len(lookup_cols) > 1:
                    lookup = holdout_panel.loc[
                        holdout_panel["symbol"].astype(str).eq(str(symbol)), lookup_cols
                    ].drop_duplicates("minute_ts").set_index("minute_ts")
                    if "rv_60m" in lookup:
                        fallback = stamps.map(lookup["rv_60m"])
                    if "rv_30m" in lookup:
                        fallback = fallback.fillna(stamps.map(lookup["rv_30m"]))
                event_rv = event_rv.fillna(fallback)
            valid = event_rv.notna()
            buckets = event_rv.gt(low_cut).astype(int) + event_rv.gt(high_cut).astype(int)
            event_frame.loc[mask & valid, "volatility_bucket"] = buckets.loc[valid].astype(int)
    results = []

    for pattern in frozen_patterns:
        symbol = pattern["symbol"]
        horizon = int(pattern["horizon_minutes"])
        direction = pattern["direction"]
        conditions = pattern["conditions"]
        ev = event_frame.loc[
            (event_frame.symbol == symbol)
            & (event_frame.horizon == horizon)
            & (event_frame.direction == direction)
        ] if not event_frame.empty else pd.DataFrame()
        co = controls.loc[
            (controls.symbol == symbol)
            & (controls.horizon == horizon)
            & (controls.direction == direction)
        ] if not controls.empty else pd.DataFrame()

        def hit_rate(frame: pd.DataFrame) -> tuple[int, int, float | None]:
            if frame.empty:
                return 0, 0, None
            matched = pd.Series(True, index=frame.index)
            for condition in conditions:
                feature = condition["feature"]
                if feature not in frame:
                    return 0, len(frame), 0.0
                values = pd.to_numeric(frame[feature], errors="coerce")
                threshold = float(condition["threshold"])
                matched &= values.ge(threshold) if condition["state"] == "high" else values.le(threshold)
            hits = int(matched.fillna(False).sum())
            return hits, int(len(frame)), hits / len(frame)

        event_count_before_matching = int(len(ev))
        control_count_before_matching = int(len(co))
        common_strata = set(map(tuple, ev[["utc_hour", "volatility_bucket"]].drop_duplicates().to_numpy())) & set(
            map(tuple, co[["utc_hour", "volatility_bucket"]].drop_duplicates().to_numpy())
        ) if not ev.empty and not co.empty else set()
        if common_strata:
            keys = ["utc_hour", "volatility_bucket"]
            ev = ev.loc[ev[keys].apply(tuple, axis=1).isin(common_strata)].copy()
            co = co.loc[co[keys].apply(tuple, axis=1).isin(common_strata)].copy()
        else:
            ev = ev.iloc[0:0]
            co = co.iloc[0:0]
        event_hits, event_total, event_rate = hit_rate(ev)
        control_hits, control_total, _ = hit_rate(co)
        control_rate = None
        if not ev.empty and not co.empty:
            weights = ev.groupby(["utc_hour", "volatility_bucket"]).size()
            weighted = 0.0
            weight_total = 0
            for key, weight in weights.items():
                key = key if isinstance(key, tuple) else (key,)
                mask = pd.Series(True, index=co.index)
                for col, value in zip(["utc_hour", "volatility_bucket"], key):
                    mask &= co[col].eq(value)
                stratum = co.loc[mask]
                if stratum.empty:
                    continue
                _, _, rate = hit_rate(stratum)
                if rate is not None:
                    weighted += rate * int(weight)
                    weight_total += int(weight)
            control_rate = weighted / weight_total if weight_total else None
        lift = (
            event_rate / control_rate
            if event_rate is not None and control_rate is not None and control_rate > 0
            else None
        )
        results.append({
            "symbol": symbol,
            "horizon_minutes": horizon,
            "direction": direction,
            "conditions": conditions,
            "training_event_support": int(pattern["event_support"]),
            "holdout_event_hits": event_hits,
            "holdout_event_total": event_total,
            "holdout_control_hits": control_hits,
            "holdout_control_total": control_total,
            "holdout_matched_strata_count": int(len(common_strata)),
            "holdout_event_match_coverage_pct": (
                round(event_total / event_count_before_matching * 100, 2)
                if event_count_before_matching else 0.0
            ),
            "holdout_control_match_coverage_pct": (
                round(control_total / control_count_before_matching * 100, 2)
                if control_count_before_matching else 0.0
            ),
            "holdout_event_rate_pct": round(event_rate * 100, 2) if event_rate is not None else None,
            "holdout_control_rate_pct": round(control_rate * 100, 2) if control_rate is not None else None,
            "holdout_descriptive_lift": round(lift, 3) if lift is not None else None,
        })

    return {
        "holdout_volatility_cut_source": volatility_cut_source,
        "holdout_event_count": len(holdout_episodes),
        "holdout_control_count_per_direction": {
            d: int((controls.direction == d).sum()) if not controls.empty else 0
            for d in ("UP", "DOWN")
        },
        "evaluated_frozen_pattern_count": len(results),
        "patterns": results,
    }


def build_walk_forward_report(
    panel: pd.DataFrame,
    train_fraction: float = 0.70,
    min_support: int = 5,
) -> dict:
    train, holdout, split_ts = chronological_split(panel, train_fraction, purge_minutes=30)
    train_episodes, _ = discover_episodes(train)
    holdout_episodes, _ = discover_episodes(holdout)

    # All quantiles, pattern selection and ranking happen on training only.
    training_report = mine_patterns(train, train_episodes, min_support=min_support)
    frozen_patterns = training_report.get("patterns", [])
    frozen_thresholds = training_report.get("feature_thresholds", {})

    evaluation = evaluate_frozen_patterns(
        holdout, holdout_episodes, frozen_patterns, frozen_thresholds,
        training_report.get("volatility_cuts", {}),
    )
    return {
        "report_type": "chronological_frozen_pattern_holdout_v1",
        "decision": "exploratory_validation_only_no_strategy_approved",
        "split": {
            "train_fraction_target": train_fraction,
            "split_timestamp_utc_ms": split_ts,
            "purge_gap_minutes": 30,
            "train_start_utc_ms": int(train.minute_ts.min()),
            "train_end_utc_ms": int(train.minute_ts.max()),
            "holdout_start_utc_ms": int(holdout.minute_ts.min()),
            "holdout_end_utc_ms": int(holdout.minute_ts.max()),
            "train_rows": int(len(train)),
            "holdout_rows": int(len(holdout)),
        },
        "training": {
            "event_count": len(train_episodes),
            "pattern_status": training_report.get("status"),
            "frozen_pattern_count": len(frozen_patterns),
            "feature_thresholds": frozen_thresholds,
            "volatility_cuts": training_report.get("volatility_cuts", {}),
            "patterns": frozen_patterns,
        },
        "holdout": evaluation,
        "methodology": [
            "Chronological split uses unique timestamps, never random row sampling.",
            "A 30-minute purge gap separates training from holdout to prevent event-horizon overlap.",
            "Feature thresholds, combinations, ranking and per-symbol volatility-tercile cut points are learned only on training data.",
            "The same frozen volatility cut points define event/control strata in the later holdout; holdout data do not refit these boundaries.",
            "Holdout events are independently discovered on the later segment.",
            "Frozen training thresholds and conditions are applied unchanged to holdout events and neutral controls.",
            "No holdout metric is used to refit, select or rank patterns.",
        ],
        "limitations": [
            "A single chronological split is not repeated walk-forward validation.",
            "Holdout lift is descriptive and may be unstable with small event counts.",
            "Controls are matched by shared UTC-hour and volatility-tercile strata; exact volatility, liquidity and market-wide state are not matched.",
            "No multiple-testing correction, execution costs, slippage, funding or market impact are modeled.",
            "A positive holdout association is not causal proof and does not approve a trading strategy.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--output", default="research-output/market_pattern_holdout.json")
    parser.add_argument("--train-fraction", type=float, default=0.70)
    parser.add_argument("--min-support", type=int, default=5)
    args = parser.parse_args()
    if args.min_support < 2:
        parser.error("min-support must be >=2")
    panel = load_market_panel(args.db)
    report = build_walk_forward_report(panel, args.train_fraction, args.min_support)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "report_type": report["report_type"],
        "training_events": report["training"]["event_count"],
        "training_patterns": report["training"]["frozen_pattern_count"],
        "holdout_events": report["holdout"]["holdout_event_count"],
        "evaluated_patterns": report["holdout"]["evaluated_frozen_pattern_count"],
        "decision": report["decision"],
        "output": str(output),
    }, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
