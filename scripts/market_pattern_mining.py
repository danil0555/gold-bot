#!/usr/bin/env python3
"""Mine recurring pre-move feature combinations against non-event controls.

Exploratory descriptive mining only. Quantile thresholds are learned from the
analysis panel, so this report is hypothesis generation, not out-of-sample proof.
"""
from __future__ import annotations

import argparse
import json
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

FEATURES = (
    "volume_surprise_z", "trades_surprise_z", "taker_imbalance",
    "book_imbalance5", "book_imbalance20", "microprice_offset_bps",
    "spread_bps", "open_interest_change_pct", "funding_rate",
)


def _finite_values(frame: pd.DataFrame, name: str) -> np.ndarray:
    if name not in frame:
        return np.array([], dtype=float)
    values = pd.to_numeric(frame[name], errors="coerce").to_numpy(dtype=float)
    return values[np.isfinite(values)]


def _signature(frame: pd.DataFrame, thresholds: dict) -> pd.DataFrame:
    result = pd.DataFrame(index=frame.index)
    for feature, limits in thresholds.items():
        values = pd.to_numeric(frame[feature], errors="coerce")
        result[f"{feature}:high"] = values >= limits["high"]
        result[f"{feature}:low"] = values <= limits["low"]
    return result.fillna(False)


def _build_control_frame(panel: pd.DataFrame, episodes: list[dict]) -> pd.DataFrame:
    """Build controls with no large move in either direction and no event-window overlap."""
    if panel.empty or not episodes:
        return pd.DataFrame()

    event_windows = []
    for episode in episodes:
        event_windows.append((
            str(episode["symbol"]),
            int(episode["start_ts"]),
            int(episode["start_ts"]) + int(episode["horizon_minutes"]) * 60_000,
        ))

    controls = []
    # Define volatility buckets from the full panel distribution per symbol.
    # The same fixed cut points are then applied to events and controls.
    volatility_cuts = {}
    for symbol, group in panel.groupby("symbol", sort=False):
        rv = pd.to_numeric(group.get("rv_60m", pd.Series(dtype=float)), errors="coerce")
        rv = rv[np.isfinite(rv)]
        if len(rv) >= 10:
            low, high = np.quantile(rv, [1 / 3, 2 / 3])
            volatility_cuts[str(symbol)] = (float(low), float(high))
        else:
            volatility_cuts[str(symbol)] = (np.nan, np.nan)

    for horizon in sorted({int(e["horizon_minutes"]) for e in episodes}):
        for symbol, group in panel.groupby("symbol", sort=False):
            g = group.sort_values("minute_ts").reset_index(drop=True)
            future = g["close"].shift(-horizon)
            future_ts = g["minute_ts"].shift(-horizon)
            contiguous = future_ts.eq(g["minute_ts"] + horizon * 60_000)
            forward = future / g["close"] - 1
            rv = g.get("rv_60m", pd.Series(np.nan, index=g.index)).fillna(
                g.get("rv_30m", pd.Series(np.nan, index=g.index))
            )
            threshold = np.maximum(0.004, rv * np.sqrt(horizon) * 2.5)
            # A genuine non-event control must not have a threshold-sized move
            # in either direction over the selected forward horizon.
            eligible = contiguous & forward.notna() & threshold.notna() & (forward.abs() < threshold)

            candidate_idx = np.flatnonzero(eligible.to_numpy())
            for idx in candidate_idx[::max(1, horizon)]:
                stamp = int(g.loc[idx, "minute_ts"])
                control_end = stamp + horizon * 60_000
                overlaps_event = any(
                    event_symbol == str(symbol)
                    and stamp <= event_end
                    and control_end >= event_start
                    for event_symbol, event_start, event_end in event_windows
                )
                if overlaps_event:
                    continue
                row = g.loc[idx].to_dict()
                low_cut, high_cut = volatility_cuts.get(str(symbol), (np.nan, np.nan))
                rv_value = pd.to_numeric(pd.Series([row.get("rv_60m", np.nan)]), errors="coerce").iloc[0]
                if not np.isfinite(rv_value):
                    rv_value = pd.to_numeric(pd.Series([row.get("rv_30m", np.nan)]), errors="coerce").iloc[0]
                vol_bucket = (
                    int(rv_value > low_cut) + int(rv_value > high_cut)
                    if np.isfinite(rv_value) and np.isfinite(low_cut) and np.isfinite(high_cut)
                    else -1
                )
                row.update({
                    "symbol": str(symbol), "minute_ts": stamp, "horizon": horizon,
                    "utc_hour": int((stamp // 3_600_000) % 24),
                    "volatility_bucket": vol_bucket,
                })
                # The same neutral timestamp is a comparator for both directions;
                # it is never classified as a directional event.
                controls.append({**row, "direction": "UP"})
                controls.append({**row, "direction": "DOWN"})
    return pd.DataFrame(controls)


def mine_patterns(
    panel: pd.DataFrame,
    episodes: list[dict],
    min_support: int = 5,
    max_patterns: int = 30,
) -> dict:
    """Compare frequent two-feature extreme combinations in events vs controls."""
    available = [name for name in FEATURES if name in panel.columns]
    thresholds = {}
    for feature in available:
        vals = _finite_values(panel, feature)
        if len(vals) < 20:
            continue
        low, high = np.quantile(vals, [0.20, 0.80])
        if not np.isfinite(low) or not np.isfinite(high) or low >= high:
            continue
        thresholds[feature] = {"low": float(low), "high": float(high)}
    features = list(thresholds)
    if len(features) < 2:
        return {
            "status": "insufficient_feature_coverage",
            "patterns": [],
            "thresholds": thresholds,
            "warning": "Need at least two features with sufficient non-constant observations.",
        }

    event_rows = []
    for episode in episodes:
        row = {"symbol": episode["symbol"], "minute_ts": episode["start_ts"],
               "direction": episode["direction"], "horizon": episode["horizon_minutes"]}
        row.update(episode.get("pre_move_features", {}))
        event_rows.append(row)
    events = pd.DataFrame(event_rows)
    if events.empty:
        return {"status": "no_events", "patterns": [], "thresholds": thresholds}

    # Attach the same UTC-hour and per-symbol volatility strata to events.
    events["utc_hour"] = (pd.to_numeric(events["minute_ts"], errors="coerce") // 3_600_000 % 24).astype(int)
    events["volatility_bucket"] = -1
    for symbol, group in panel.groupby("symbol", sort=False):
        rv = pd.to_numeric(group.get("rv_60m", pd.Series(dtype=float)), errors="coerce")
        rv = rv[np.isfinite(rv)]
        if len(rv) < 10:
            continue
        low_cut, high_cut = np.quantile(rv, [1 / 3, 2 / 3])
        event_mask = events["symbol"].astype(str).eq(str(symbol))
        event_rv = pd.to_numeric(events.loc[event_mask, "rv_60m"], errors="coerce")
        if "rv_60m" not in events:
            continue
        valid = event_rv.notna()
        buckets = (event_rv.gt(low_cut).astype(int) + event_rv.gt(high_cut).astype(int))
        events.loc[event_mask & valid, "volatility_bucket"] = buckets.loc[valid].astype(int)

    control_frame = _build_control_frame(panel, episodes)
    results = []
    # Never pool symbols or horizons: each event is compared only with controls
    # from the same instrument and forward horizon.
    strata = events[["symbol", "horizon"]].drop_duplicates().itertuples(index=False)
    for symbol, horizon in strata:
        for direction in ("UP", "DOWN"):
            ev = events.loc[
                (events.symbol == symbol)
                & (events.horizon == horizon)
                & (events.direction == direction)
            ].copy()
            co = control_frame.loc[
                (control_frame.symbol == symbol)
                & (control_frame.horizon == horizon)
                & (control_frame.direction == direction)
            ].copy()
            if ev.empty or co.empty:
                continue
            # Restrict both groups to strata represented by events and controls:
            # same instrument/horizon/direction, UTC hour and volatility tercile.
            stratum = ["utc_hour", "volatility_bucket"]
            common_strata = set(map(tuple, ev[stratum].drop_duplicates().to_numpy())) & set(
                map(tuple, co[stratum].drop_duplicates().to_numpy())
            )
            if not common_strata:
                continue
            ev = ev.loc[ev[stratum].apply(tuple, axis=1).isin(common_strata)].copy()
            co = co.loc[co[stratum].apply(tuple, axis=1).isin(common_strata)].copy()
            if ev.empty or co.empty:
                continue
            # Standardize the control hit rate to the event stratum mix so a
            # different distribution of hours/volatility cannot dominate it.
            stratum_weights = ev.groupby(stratum).size()
            ev_sig = _signature(ev, thresholds)
            co_sig = _signature(co, thresholds)
            for first, second in combinations(features, 2):
                for first_state in ("high", "low"):
                    for second_state in ("high", "low"):
                        a, b = f"{first}:{first_state}", f"{second}:{second_state}"
                        event_hits = ev_sig[a] & ev_sig[b]
                        control_hits = co_sig[a] & co_sig[b]
                        n_event = int(event_hits.sum())
                        n_control = int(control_hits.sum())
                        if n_event < min_support:
                            continue
                        event_rate = n_event / len(ev)
                        weighted_control_rate = 0.0
                        weight_total = 0
                        for stratum_key, weight in stratum_weights.items():
                            key = stratum_key if isinstance(stratum_key, tuple) else (stratum_key,)
                            ev_mask = pd.Series(True, index=ev.index)
                            co_mask = pd.Series(True, index=co.index)
                            for col, value in zip(stratum, key):
                                ev_mask &= ev[col].eq(value)
                                co_mask &= co[col].eq(value)
                            stratum_controls = co.loc[co_mask]
                            if stratum_controls.empty:
                                continue
                            stratum_hits = (co_sig.loc[stratum_controls.index, a]
                                             & co_sig.loc[stratum_controls.index, b]).sum()
                            weighted_control_rate += (int(stratum_hits) / len(stratum_controls)) * int(weight)
                            weight_total += int(weight)
                        control_rate = weighted_control_rate / weight_total if weight_total else 0.0
                        lift = event_rate / control_rate if control_rate > 0 else None
                        results.append({
                            "symbol": str(symbol),
                            "horizon_minutes": int(horizon),
                            "direction": direction,
                            "conditions": [
                                {"feature": first, "state": first_state,
                                 "threshold": thresholds[first][first_state]},
                                {"feature": second, "state": second_state,
                                 "threshold": thresholds[second][second_state]},
                            ],
                            "event_support": n_event,
                            "event_total": int(len(ev)),
                            "control_support": n_control,
                            "control_total": int(len(co)),
                            "matched_strata_count": int(len(common_strata)),
                            "event_rate_pct": round(event_rate * 100, 2),
                            "control_rate_pct": round(control_rate * 100, 2),
                            "descriptive_lift": round(lift, 3) if lift is not None else None,
                        })
    results.sort(
        key=lambda x: (
            x["descriptive_lift"] if x["descriptive_lift"] is not None else -1,
            x["event_support"],
        ),
        reverse=True,
    )
    return {
        "status": "exploratory_patterns_found" if results else "no_patterns_met_support",
        "control_method": (
            "same-symbol and same-horizon neutral timestamps sampled every horizon minutes; "
            "all controls exclude threshold-sized forward moves in either direction and any "
            "forward interval overlapping a detected event window; pattern event rates are "
            "compared only within shared UTC-hour and per-symbol volatility-tercile strata, "
            "with control rates standardized to the event stratum mix"
        ),
        "minimum_event_support": min_support,
        "feature_thresholds": thresholds,
        "event_counts_by_direction": {
            direction: int((events.direction == direction).sum())
            for direction in ("UP", "DOWN")
        },
        "control_counts_by_direction": {
            direction: int((control_frame.direction == direction).sum())
            for direction in ("UP", "DOWN")
        },
        "patterns": results[:max_patterns],
        "warning": (
            "Patterns are discovered and ranked on the same sample; thresholds and combinations "
            "are data-mined. Lift is descriptive, not causal or predictive proof. Controls are "
            "coarse neutral comparators, not fully matched by volatility regime or time. No "
            "multiple-testing correction, independent holdout, execution costs, or strategy approval."
        ),
    }

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--output", default="research-output/market_patterns_smoke.json")
    parser.add_argument("--min-support", type=int, default=5)
    parser.add_argument("--max-patterns", type=int, default=30)
    args = parser.parse_args()
    if args.min_support < 2 or args.max_patterns < 1:
        parser.error("min-support must be >=2 and max-patterns must be >=1")
    from scripts.market_move_discovery import load_market_panel, discover_episodes
    panel = load_market_panel(args.db)
    episodes, _ = discover_episodes(panel)
    report = mine_patterns(panel, episodes, args.min_support, args.max_patterns)
    report["episodes_detected"] = len(episodes)
    report["decision"] = "discovery_only_no_strategy_approved"
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "status": report["status"],
        "episodes_detected": len(episodes),
        "patterns_reported": len(report["patterns"]),
        "decision": report["decision"],
        "output": str(output),
    }, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
