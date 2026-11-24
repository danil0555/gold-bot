#!/usr/bin/env python3
"""Discover volatility-normalized price-move episodes and inspect only pre-move data.

Read-only analysis of the separate microstructure capture SQLite database.
This is an episode-discovery report, not a causal explanation or trading signal.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

MINUTE_MS = 60_000
DAY_MS = 86_400_000
MAX_OI_CHANGE_INTERVAL_MS = 10 * 60_000

BASE_FEATURES = (
    "ret_1m", "ret_5m", "ret_15m", "rv_30m", "rv_60m",
    "volume_surprise_z", "trades_surprise_z", "taker_imbalance",
    "book_imbalance5", "book_imbalance20", "microprice_offset_bps",
    "spread_bps", "open_interest_change_pct", "open_interest_change_interval_ms", "funding_rate",
)


def load_market_panel(db_path: str) -> pd.DataFrame:
    with sqlite3.connect(db_path) as db:
        tables = {
            row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required = {"minute_candles", "minute_features", "derivatives_snapshots"}
        missing = sorted(required - tables)
        if missing:
            raise ValueError("database missing required tables: " + ", ".join(missing))
        candles = pd.read_sql_query(
            """SELECT symbol, minute_ts, close, volume
               FROM minute_candles WHERE is_closed=1
               ORDER BY symbol, minute_ts""", db
        )
        features = pd.read_sql_query(
            """SELECT symbol, minute_ts, trades_count, buy_quote_usdt,
                      sell_quote_usdt, taker_imbalance, avg_spread_bps,
                      avg_book_imbalance5, avg_book_imbalance20,
                      avg_microprice_offset_bps
               FROM minute_features ORDER BY symbol, minute_ts""", db
        )
        derivatives = pd.read_sql_query(
            """SELECT symbol, ts_ms, open_interest_base, funding_rate
               FROM derivatives_snapshots ORDER BY symbol, ts_ms""", db
        )
    if candles.empty:
        raise ValueError("database contains no closed candles")
    panel = candles.merge(features, on=["symbol", "minute_ts"], how="left")
    panel["quote_volume"] = (
        panel["buy_quote_usdt"].fillna(0) + panel["sell_quote_usdt"].fillna(0)
    )
    panel["trades_count"] = panel["trades_count"].fillna(0)
    # Returns are valid only across exact minute intervals; row-count lags
    # must never silently bridge missing candles.
    for horizon in (1, 5, 15):
        prior_close = panel.groupby("symbol", sort=False)["close"].shift(horizon)
        prior_ts = panel.groupby("symbol", sort=False)["minute_ts"].shift(horizon)
        valid_gap = panel["minute_ts"].sub(prior_ts).eq(horizon * MINUTE_MS)
        panel[f"ret_{horizon}m"] = (panel["close"] / prior_close - 1).where(valid_gap)
    panel["rv_30m"] = panel.groupby("symbol", sort=False)["ret_1m"].transform(
        lambda s: s.rolling(30, min_periods=20).std()
    )
    panel["rv_60m"] = panel.groupby("symbol", sort=False)["ret_1m"].transform(
        lambda s: s.rolling(60, min_periods=40).std()
    )
    for source, target in (("quote_volume", "volume_surprise_z"),
                           ("trades_count", "trades_surprise_z")):
        grouped = panel.groupby("symbol", sort=False)[source]
        past_mean = grouped.transform(lambda s: s.shift(1).rolling(120, min_periods=30).mean())
        past_std = grouped.transform(lambda s: s.shift(1).rolling(120, min_periods=30).std())
        panel[target] = (panel[source] - past_mean) / past_std.replace(0, np.nan)

    panel["book_imbalance5"] = panel["avg_book_imbalance5"]
    panel["book_imbalance20"] = panel["avg_book_imbalance20"]
    panel["microprice_offset_bps"] = panel["avg_microprice_offset_bps"]
    panel["spread_bps"] = panel["avg_spread_bps"]

    if not derivatives.empty:
        derivatives = derivatives.rename(columns={"ts_ms": "minute_ts"}).sort_values(
            ["symbol", "minute_ts"]
        )
        pieces = []
        for symbol, group in panel.groupby("symbol", sort=False):
            deriv = derivatives.loc[derivatives.symbol == symbol].drop(columns="symbol")
            left = group.sort_values("minute_ts")
            if deriv.empty:
                left = left.copy()
                left["open_interest_base"] = np.nan
                left["funding_rate"] = np.nan
                left["open_interest_change_pct"] = np.nan
                left["open_interest_change_interval_ms"] = np.nan
                pieces.append(left)
                continue

            # Calculate OI change on actual consecutive derivative snapshots,
            # not on minute candles carrying a repeated/stale as-of value.
            deriv = deriv.drop_duplicates(subset=["minute_ts"], keep="last").copy()
            deriv["open_interest_base"] = pd.to_numeric(
                deriv["open_interest_base"], errors="coerce"
            )
            previous_ts = deriv["minute_ts"].shift(1)
            previous_oi = deriv["open_interest_base"].shift(1)
            interval_ms = deriv["minute_ts"] - previous_ts
            valid_oi_pair = (
                deriv["open_interest_base"].gt(0)
                & previous_oi.gt(0)
                & interval_ms.gt(0)
                & interval_ms.le(MAX_OI_CHANGE_INTERVAL_MS)
            )
            deriv["open_interest_change_pct"] = (
                deriv["open_interest_base"] / previous_oi - 1.0
            ).where(valid_oi_pair)
            deriv["open_interest_change_interval_ms"] = interval_ms.where(valid_oi_pair)

            merged = pd.merge_asof(
                left, deriv.sort_values("minute_ts"), on="minute_ts", direction="backward",
                tolerance=120_000,
            )
            pieces.append(merged)
        panel = pd.concat(pieces, ignore_index=True).sort_values(
            ["symbol", "minute_ts"]
        ).reset_index(drop=True)
    else:
        panel["open_interest_base"] = np.nan
        panel["funding_rate"] = np.nan
        panel["open_interest_change_pct"] = np.nan
        panel["open_interest_change_interval_ms"] = np.nan
    return panel.replace([np.inf, -np.inf], np.nan)


def _episode_features(row: pd.Series) -> dict:
    return {
        name: (float(row[name]) if pd.notna(row.get(name)) else None)
        for name in BASE_FEATURES
    }


def discover_episodes(
    panel: pd.DataFrame,
    horizons=(5, 15, 30),
    min_move_pct: float = 0.004,
    volatility_multiple: float = 2.5,
    cooldown_minutes: int = 5,
) -> tuple[list[dict], dict]:
    episodes: list[dict] = []
    summaries: dict = {}
    for horizon in horizons:
        horizon_events = []
        for symbol, raw in panel.groupby("symbol", sort=False):
            group = raw.sort_values("minute_ts").reset_index(drop=True).copy()
            future_close = group["close"].shift(-horizon)
            future_ts = group["minute_ts"].shift(-horizon)
            contiguous = future_ts.eq(group["minute_ts"] + horizon * MINUTE_MS)
            forward_return = future_close / group["close"] - 1.0
            # Volatility is estimated only from returns available at the episode start.
            threshold = np.maximum(
                min_move_pct,
                group["rv_60m"].fillna(group["rv_30m"]) * np.sqrt(horizon)
                * volatility_multiple,
            )
            eligible = (
                contiguous & forward_return.notna() & threshold.notna()
                & (threshold > 0) & group["close"].gt(0)
            )
            candidates = np.flatnonzero(
                eligible.to_numpy()
                & (forward_return.abs().to_numpy() >= threshold.to_numpy())
            )
            last_kept = -10**9
            for idx in candidates:
                if idx - last_kept < max(horizon, cooldown_minutes):
                    continue
                row = group.iloc[idx]
                move = float(forward_return.iloc[idx])
                direction = "UP" if move > 0 else "DOWN"
                episode = {
                    "symbol": str(symbol),
                    "horizon_minutes": int(horizon),
                    "start_ts": int(row.minute_ts),
                    "end_ts": int(future_ts.iloc[idx]),
                    "direction": direction,
                    "forward_return_pct": round(move * 100, 5),
                    "absolute_move_pct": round(abs(move) * 100, 5),
                    "threshold_pct": round(float(threshold.iloc[idx]) * 100, 5),
                    "pre_move_features": _episode_features(row),
                }
                episodes.append(episode)
                horizon_events.append(episode)
                last_kept = int(idx)

        for direction in ("UP", "DOWN"):
            selected = [e for e in horizon_events if e["direction"] == direction]
            feature_summary = {}
            for feature in BASE_FEATURES:
                values = [
                    e["pre_move_features"][feature] for e in selected
                    if e["pre_move_features"][feature] is not None
                    and np.isfinite(e["pre_move_features"][feature])
                ]
                baseline = panel[feature].dropna().to_numpy(dtype=float)
                feature_summary[feature] = {
                    "event_count_with_value": len(values),
                    "event_mean": round(float(np.mean(values)), 6) if values else None,
                    "event_median": round(float(np.median(values)), 6) if values else None,
                    "all_minutes_median": round(float(np.median(baseline)), 6)
                        if len(baseline) else None,
                }
            summaries[f"{horizon}m_{direction.lower()}"] = {
                "event_count": len(selected),
                "independent_utc_day_clusters": len({
                    e["start_ts"] // DAY_MS for e in selected
                }),
                "symbol_count": len({e["symbol"] for e in selected}),
                "mean_forward_move_pct": round(float(np.mean([
                    e["forward_return_pct"] for e in selected
                ])), 5) if selected else None,
                "median_absolute_move_pct": round(float(np.median([
                    e["absolute_move_pct"] for e in selected
                ])), 5) if selected else None,
                "pre_move_feature_comparison": feature_summary,
                "inference_status": (
                    "insufficient_independent_days" if len({
                        e["start_ts"] // DAY_MS for e in selected
                    }) < 4 else "descriptive_exploration_only"
                ),
            }
    return episodes, summaries


def build_report(
    panel: pd.DataFrame,
    min_move_pct: float = 0.004,
    volatility_multiple: float = 2.5,
) -> dict:
    episodes, summaries = discover_episodes(
        panel, min_move_pct=min_move_pct,
        volatility_multiple=volatility_multiple,
    )
    return {
        "report_type": "market_move_pre_event_discovery_v1",
        "method": (
            "Detect contiguous 5m/15m/30m forward moves using a threshold equal "
            "to max(minimum absolute move, 2.5 times trailing 60m volatility "
            "scaled by square-root horizon); describe only features available "
            "at the episode start."
        ),
        "episodes_detected": len(episodes),
        "symbols": sorted(panel["symbol"].dropna().unique().tolist()),
        "period_utc_ms": [
            int(panel["minute_ts"].min()), int(panel["minute_ts"].max())
        ],
        "threshold_settings": {
            "minimum_absolute_move_pct": min_move_pct * 100,
            "trailing_volatility_multiple": volatility_multiple,
            "horizons_minutes": [5, 15, 30],
            "minimum_independent_days_for_inference": 4,
        },
        "directional_summaries": summaries,
        "episodes": episodes,
        "limitations": [
            "The report identifies predictive precursors and associations, not proven causes.",
            "Events are selected using future returns; pre-event features are restricted to the start timestamp.",
            "Overlapping episodes are thinned within each symbol and horizon, but cross-symbol events may be correlated.",
            "Feature comparisons are descriptive and are not adjusted for multiple testing or confounders.",
            "The all-minute baseline is not a matched control group.",
            "Fixed event thresholds do not represent executable entries, stops, targets, fees, slippage, funding, or market impact.",
            "Sparse or stale derivatives snapshots may not represent conditions exactly at event time.",
            "No strategy is approved and no trading signal is generated.",
        ],
        "decision": "discovery_only_no_strategy_approved",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument(
        "--output", default="research-output/market_move_discovery.json"
    )
    parser.add_argument("--min-move-pct", type=float, default=0.004)
    parser.add_argument("--volatility-multiple", type=float, default=2.5)
    args = parser.parse_args()
    if args.min_move_pct <= 0 or args.volatility_multiple <= 0:
        parser.error("move threshold settings must be positive")
    panel = load_market_panel(args.db)
    report = build_report(panel, args.min_move_pct, args.volatility_multiple)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "report_type": report["report_type"],
        "episodes_detected": report["episodes_detected"],
        "directional_summaries": report["directional_summaries"],
        "decision": report["decision"],
        "output": str(output),
    }, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
