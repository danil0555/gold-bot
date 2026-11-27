#!/usr/bin/env python3
"""Exploratory event study for live BingX microstructure SQLite captures.

Signals are deterministic, use past-only rolling thresholds, and are evaluated
on later closed candles. This is research plumbing, not an executable strategy.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

MINUTE_MS = 60_000


def load_panel(db_path: str) -> pd.DataFrame:
    with sqlite3.connect(db_path) as db:
        candles = pd.read_sql_query(
            "SELECT symbol, minute_ts, close FROM minute_candles WHERE is_closed=1",
            db,
        )
        features = pd.read_sql_query("SELECT * FROM minute_features", db)
        derivatives = pd.read_sql_query(
            "SELECT symbol, ts_ms, open_interest_base, funding_rate, mark_price, index_price "
            "FROM derivatives_snapshots ORDER BY ts_ms",
            db,
        )
    if candles.empty or features.empty:
        raise ValueError("database has no closed candles or microstructure features")
    panel = candles.merge(features, on=["symbol", "minute_ts"], how="inner")
    if not derivatives.empty:
        derivatives = derivatives.sort_values("ts_ms")
        parts = []
        for symbol, group in panel.groupby("symbol", sort=False):
            snap = derivatives.loc[derivatives["symbol"] == symbol].sort_values("ts_ms")
            if snap.empty:
                parts.append(group)
                continue
            # `symbol` is already present in the left-hand panel. Keep the
            # right-hand snapshot symbol out of merge_asof; otherwise pandas
            # suffixes both columns to symbol_x/symbol_y and downstream
            # per-symbol sorting fails.
            snap = snap.drop(columns=["symbol"], errors="ignore")
            merged = pd.merge_asof(
                group.sort_values("minute_ts"),
                snap,
                left_on="minute_ts",
                right_on="ts_ms",
                direction="backward",
                tolerance=120_000,
            )
            parts.append(merged.drop(columns=["ts_ms"], errors="ignore"))
        panel = pd.concat(parts, ignore_index=True)
    return panel.sort_values(["symbol", "minute_ts"]).reset_index(drop=True)


def add_past_only_events(panel: pd.DataFrame, lookback: int = 120, min_periods: int = 60) -> pd.DataFrame:
    out = panel.copy()
    # Captures are intentionally discontinuous. Never let rolling thresholds,
    # OI deltas, or forward labels bridge a missing minute / session boundary.
    minute_delta = out.groupby("symbol", sort=False)["minute_ts"].diff()
    starts_segment = minute_delta.ne(MINUTE_MS)
    out["segment_id"] = starts_segment.groupby(out["symbol"], sort=False).cumsum().astype(int)
    grouped = out.groupby(["symbol", "segment_id"], sort=False)
    out["ret_1m"] = grouped["close"].pct_change()
    for col in ("taker_imbalance", "avg_book_imbalance20", "avg_microprice_offset_bps",
                "avg_spread_bps", "open_interest_base"):
        if col not in out:
            out[col] = np.nan
    out["oi_change_5m"] = grouped["open_interest_base"].pct_change(5)
    for col, quantile, name in (
        ("taker_imbalance", 0.20, "taker_q20"),
        ("taker_imbalance", 0.80, "taker_q80"),
        ("avg_book_imbalance20", 0.80, "book_q80"),
        ("avg_microprice_offset_bps", 0.80, "micro_q80"),
        ("avg_spread_bps", 0.80, "spread_q80"),
        ("oi_change_5m", 0.80, "oi_q80"),
    ):
        out[name] = grouped[col].transform(
            lambda values: values.shift(1).rolling(lookback, min_periods=min_periods).quantile(quantile)
        )
    # Aggressive selling with little downward price progress can indicate passive
    # demand absorbing market sells; the mirror condition tests supply absorption.
    out["event_sell_absorption"] = (
        (out["taker_imbalance"] < out["taker_q20"])
        & (out["ret_1m"] >= -0.0002)
    )
    out["event_buy_absorption"] = (
        (out["taker_imbalance"] > out["taker_q80"])
        & (out["ret_1m"] <= 0.0002)
    )
    out["event_bid_support"] = (
        (out["avg_book_imbalance20"] > out["book_q80"])
        & (out["avg_microprice_offset_bps"] > out["micro_q80"])
        & (out["avg_spread_bps"] <= out["spread_q80"])
    )
    out["event_oi_expansion_sell_pressure"] = (
        (out["oi_change_5m"] > out["oi_q80"])
        & (out["taker_imbalance"] <= out["taker_q20"])
    )
    for horizon in (1, 5, 15):
        future = grouped["close"].shift(-horizon)
        future_time = grouped["minute_ts"].shift(-horizon)
        out[f"fwd_{horizon}m"] = (future / out["close"] - 1).where(
            future_time == out["minute_ts"] + horizon * MINUTE_MS
        )
    return out


def _thin_events(group: pd.DataFrame, gap_minutes: int) -> pd.DataFrame:
    if group.empty:
        return group
    keep = []
    last_by_symbol = {}
    for idx, row in group.sort_values(["symbol", "minute_ts"]).iterrows():
        symbol = row["symbol"]
        stamp = int(row["minute_ts"])
        if stamp - last_by_symbol.get(symbol, -10**30) >= gap_minutes * MINUTE_MS:
            keep.append(idx)
            last_by_symbol[symbol] = stamp
    return group.loc[keep]


def clustered_se(values: np.ndarray, timestamps: np.ndarray, block_hours: int = 24):
    values = np.asarray(values, dtype=float)
    timestamps = np.asarray(timestamps, dtype=np.int64)
    valid = np.isfinite(values) & np.isfinite(timestamps)
    values, timestamps = values[valid], timestamps[valid]
    clusters = np.floor_divide(timestamps, block_hours * 3_600_000)
    unique = np.unique(clusters)
    if len(values) < 2 or len(unique) < 4:
        return None, int(len(unique))
    mean = values.mean()
    scores = np.array([np.sum(values[clusters == c] - mean) for c in unique])
    variance = len(unique) / (len(unique) - 1) * np.sum(scores**2) / len(values)**2
    return float(np.sqrt(max(variance, 0))), int(len(unique))


def summarize_events(panel: pd.DataFrame, cost_bps: float = 12.0, gap_minutes: int = 5):
    # These are explicit directional hypotheses, not claims that the event
    # definitions are already profitable. Supply absorption is a SHORT setup;
    # aggressive sell pressure with expanding OI is also tested as SHORT.
    event_directions = {
        "event_sell_absorption": 1,
        "event_buy_absorption": -1,
        "event_bid_support": 1,
        "event_oi_expansion_sell_pressure": -1,
    }
    results = {}
    for event, direction in event_directions.items():
        raw = panel.loc[panel[event].fillna(False)].copy()
        selected = _thin_events(raw, gap_minutes)
        horizons = {}
        for horizon in (1, 5, 15):
            values = selected[f"fwd_{horizon}m"].dropna()
            valid = selected.loc[values.index]
            gross_bps = values.to_numpy() * direction * 10_000
            net_bps = gross_bps - cost_bps
            se, clusters = clustered_se(net_bps, valid["minute_ts"].to_numpy())
            winners = net_bps[net_bps > 0]
            losers = net_bps[net_bps < 0]
            horizons[f"{horizon}m"] = {
                "observations": int(len(net_bps)),
                "mean_gross_bps": round(float(gross_bps.mean()), 3) if len(gross_bps) else None,
                "median_gross_bps": round(float(np.median(gross_bps)), 3) if len(gross_bps) else None,
                "mean_net_bps": round(float(net_bps.mean()), 3) if len(net_bps) else None,
                "median_net_bps": round(float(np.median(net_bps)), 3) if len(net_bps) else None,
                "positive_net_pct": round(float((net_bps > 0).mean() * 100), 2) if len(net_bps) else None,
                "profit_factor": round(float(winners.sum() / abs(losers.sum())), 4)
                    if len(winners) and len(losers) else None,
                "cluster_count_24h": clusters,
                "clustered_se_bps": round(se, 3) if se is not None else None,
                "inference_status": (
                    "insufficient_independent_clusters" if clusters < 4
                    else "clustered_uncertainty_available"
                ),
            }
        results[event] = {
            "hypothesis_direction": "LONG" if direction > 0 else "SHORT",
            "raw_events": int(len(raw)),
            "events_after_5m_thinning": int(len(selected)),
            "horizons": horizons,
        }
    return {
        "method": "past-only rolling quantile event definitions; directional next-closed-minute returns; 5-minute per-symbol event thinning; fixed round-trip cost deduction; 24h time-block clustered uncertainty",
        "directional_scoring": "LONG hypotheses use forward returns; SHORT hypotheses use negated forward returns before costs",
        "minimum_independent_24h_clusters": 4,
        "cost_bps": cost_bps,
        "lookback_minutes": 120,
        "min_history_minutes": 60,
        "gap_handling": "rolling thresholds, OI deltas and forward labels reset at every missing/non-consecutive minute",
        "events": results,
        "warning": "Exploratory event study only. Direction labels are hypotheses, not validated trade instructions. Do not trade these events. Require multiple independent time clusters, multiple-regime holdouts, and execution simulation before strategy consideration.",
    }


def run(args):
    panel = load_panel(args.db)
    panel = add_past_only_events(panel, args.lookback, args.min_periods)
    report = summarize_events(panel, args.cost_bps, args.event_gap_minutes)
    report["symbols"] = sorted(panel["symbol"].dropna().unique().tolist())
    report["rows"] = int(len(panel))
    report["continuous_segments"] = int(panel[["symbol", "segment_id"]].drop_duplicates().shape[0])
    report["period_utc_ms"] = [
        int(panel["minute_ts"].min()), int(panel["minute_ts"].max())
    ]
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--output", default="research-output/microstructure_event_study.json")
    parser.add_argument("--cost-bps", type=float, default=12.0)
    parser.add_argument("--lookback", type=int, default=120)
    parser.add_argument("--min-periods", type=int, default=60)
    parser.add_argument("--event-gap-minutes", type=int, default=5)
    args = parser.parse_args()
    run(args)
