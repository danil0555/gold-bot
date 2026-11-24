#!/usr/bin/env python3
"""Event-agnostic, past-only microstructure feature discovery (research only)."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

MINUTE_MS = 60_000
FEATURES = [
    "taker_imbalance", "cvd_to_trade_flow", "book_imbalance5",
    "book_imbalance20", "microprice_offset_bps", "spread_bps",
    "depth_imbalance5", "depth_imbalance20", "trade_count_surprise",
    "volume_surprise", "oi_change_5m", "funding_rate",
    "momentum_5m", "momentum_15m", "realized_vol_15m",
]


def load_panel(db_path: str) -> pd.DataFrame:
    with sqlite3.connect(db_path) as db:
        candles = pd.read_sql_query(
            "SELECT symbol, minute_ts, close, volume FROM minute_candles WHERE is_closed=1", db
        )
        features = pd.read_sql_query("SELECT * FROM minute_features", db)
        derivatives = pd.read_sql_query(
            "SELECT symbol, ts_ms, open_interest_base, funding_rate "
            "FROM derivatives_snapshots ORDER BY ts_ms", db
        )
    if candles.empty or features.empty:
        raise ValueError("database has no closed candles or microstructure features")
    panel = candles.merge(features, on=["symbol", "minute_ts"], how="inner")
    if not derivatives.empty:
        parts = []
        for symbol, group in panel.groupby("symbol", sort=False):
            snap = derivatives.loc[derivatives.symbol == symbol].drop(columns="symbol")
            if snap.empty:
                parts.append(group)
                continue
            parts.append(pd.merge_asof(
                group.sort_values("minute_ts"), snap.sort_values("ts_ms"),
                left_on="minute_ts", right_on="ts_ms", direction="backward",
                tolerance=120_000,
            ).drop(columns="ts_ms"))
        panel = pd.concat(parts, ignore_index=True)
    return panel.sort_values(["symbol", "minute_ts"]).reset_index(drop=True)


def _past_rank(values: pd.Series, window: int, min_periods: int) -> pd.Series:
    # Trailing window includes the current completed observation, never future rows.
    return values.rolling(window, min_periods=min_periods).apply(
        lambda arr: float(np.mean(arr <= arr[-1])), raw=True
    )


def build_features(panel: pd.DataFrame, lookback: int = 120, min_periods: int = 60,
                   horizons=(1, 5, 15)) -> pd.DataFrame:
    if lookback < min_periods or min_periods < 2:
        raise ValueError("lookback must be >= min_periods >= 2")
    out = panel.copy().sort_values(["symbol", "minute_ts"]).reset_index(drop=True)
    required = [
        "trades_count", "buy_quote_usdt", "sell_quote_usdt", "cvd_quote_usdt",
        "taker_imbalance", "avg_book_imbalance5", "avg_book_imbalance20",
        "avg_microprice_offset_bps", "avg_spread_bps", "avg_bid_depth5_usdt",
        "avg_ask_depth5_usdt", "avg_bid_depth20_usdt", "avg_ask_depth20_usdt",
        "open_interest_base", "funding_rate",
    ]
    for col in required:
        if col not in out:
            out[col] = np.nan
    g = out.groupby("symbol", sort=False)
    previous_ts = g.minute_ts.shift(1)
    out["ret_1m"] = g.close.pct_change(fill_method=None).where(
        out.minute_ts - previous_ts == MINUTE_MS
    )
    for lag in (5, 15, 60):
        lag_ts, lag_close = g.minute_ts.shift(lag), g.close.shift(lag)
        out[f"momentum_{lag}m"] = (out.close / lag_close - 1).where(
            out.minute_ts - lag_ts == lag * MINUTE_MS
        )
    out["realized_vol_15m"] = g.ret_1m.transform(
        lambda s: s.rolling(15, min_periods=10).std()
    ) * np.sqrt(15)
    flow = (out.buy_quote_usdt.fillna(0) + out.sell_quote_usdt.fillna(0)).replace(0, np.nan)
    out["cvd_to_trade_flow"] = out.cvd_quote_usdt / flow
    out["book_imbalance5"] = out.avg_book_imbalance5
    out["book_imbalance20"] = out.avg_book_imbalance20
    out["microprice_offset_bps"] = out.avg_microprice_offset_bps
    out["spread_bps"] = out.avg_spread_bps
    for depth in (5, 20):
        bid, ask = out[f"avg_bid_depth{depth}_usdt"], out[f"avg_ask_depth{depth}_usdt"]
        out[f"depth_imbalance{depth}"] = (bid - ask) / (bid + ask).replace(0, np.nan)
    oi_change = g.open_interest_base.pct_change(5, fill_method=None)
    oi_lag_ts = g.minute_ts.shift(5)
    out["oi_change_5m"] = oi_change.where(out.minute_ts - oi_lag_ts == 5 * MINUTE_MS)
    out["funding_rate"] = out.funding_rate
    out["trade_count_surprise"] = g.trades_count.transform(
        lambda s: s / s.shift(1).rolling(lookback, min_periods=min_periods).median().replace(0, np.nan)
    )
    out["volume_surprise"] = g.volume.transform(
        lambda s: s / s.shift(1).rolling(lookback, min_periods=min_periods).median().replace(0, np.nan)
    )
    out["trend_60m"] = out["momentum_60m"]
    out["volatility_reference"] = g.realized_vol_15m.transform(
        lambda s: s.shift(1).rolling(lookback, min_periods=min_periods).quantile(0.70)
    )
    out["trend_regime"] = np.select(
        [out.trend_60m >= 0.005, out.trend_60m <= -0.005],
        ["up", "down"], default="sideways",
    )
    out["volatility_regime"] = np.where(
        out.realized_vol_15m >= out.volatility_reference, "high_vol", "normal_vol"
    )
    breadth = out.groupby("minute_ts").trend_60m.transform(
        lambda s: (s.dropna() > 0).mean() if s.notna().any() else np.nan
    )
    out["market_breadth_60m"] = breadth
    out["breadth_regime"] = np.select(
        [breadth >= 0.60, breadth <= 0.40],
        ["positive_breadth", "negative_breadth"], default="mixed_breadth",
    )
    for feature in FEATURES:
        out[f"rank_{feature}"] = g[feature].transform(
            lambda s: _past_rank(s, lookback, min_periods)
        )
    for horizon in horizons:
        future_close, future_ts = g.close.shift(-horizon), g.minute_ts.shift(-horizon)
        out[f"fwd_{horizon}m"] = (future_close / out.close - 1).where(
            future_ts == out.minute_ts + horizon * MINUTE_MS
        )
    return out


def _stats(values: pd.Series, timestamps: pd.Series, cost_bps: float) -> dict:
    valid = values.dropna()
    ts = timestamps.loc[valid.index]
    gross = valid.to_numpy(dtype=float) * 10_000
    net = gross - cost_bps
    clusters = int(pd.Series(ts.to_numpy(dtype=np.int64) // 86_400_000).nunique())
    return {
        "observations": int(len(net)),
        "mean_gross_bps": round(float(gross.mean()), 3) if len(gross) else None,
        "median_gross_bps": round(float(np.median(gross)), 3) if len(gross) else None,
        "mean_net_bps": round(float(net.mean()), 3) if len(net) else None,
        "median_net_bps": round(float(np.median(net)), 3) if len(net) else None,
        "positive_net_pct": round(float((net > 0).mean() * 100), 2) if len(net) else None,
        "cluster_count_24h": clusters,
        "inference_status": (
            "insufficient_independent_clusters" if clusters < 4
            else "exploratory_only_multiple_testing_unadjusted"
        ),
    }


def summarize_discovery(panel: pd.DataFrame, cost_bps: float = 12.0,
                        horizons=(1, 5, 15)) -> dict:
    results = {}
    for horizon in horizons:
        target = f"fwd_{horizon}m"
        feature_results = {}
        for feature in FEATURES:
            rank_col = f"rank_{feature}"
            cols = [rank_col, target, "minute_ts", "symbol",
                    "trend_regime", "volatility_regime", "breadth_regime"]
            valid = panel[cols].dropna(subset=[rank_col, target]).copy()
            if valid.empty:
                continue
            valid["decile"] = np.minimum((valid[rank_col].to_numpy() * 10).astype(int), 9)
            bins = {}
            for bucket, group in valid.groupby("decile"):
                bins[str(int(bucket) + 1)] = {
                    **_stats(group[target], group.minute_ts, cost_bps),
                    "symbol_count": int(group.symbol.nunique()),
                }
            top, bottom = valid.loc[valid.decile == 9], valid.loc[valid.decile == 0]
            item = {
                "rank_deciles_long": bins,
                "top_decile_long": _stats(top[target], top.minute_ts, cost_bps),
                "bottom_decile_short": _stats(-bottom[target], bottom.minute_ts, cost_bps),
                "top_minus_bottom_gross_bps": round(
                    (top[target].mean() - bottom[target].mean()) * 10_000, 3
                ) if len(top) and len(bottom) else None,
                "regime_slices": {},
                "candidate_status": "not_eligible_single_capture_exploratory_only",
            }
            for regime_col in ("trend_regime", "volatility_regime", "breadth_regime"):
                slices = {}
                for regime, subset in valid.groupby(regime_col):
                    high, low = subset.loc[subset.decile == 9], subset.loc[subset.decile == 0]
                    slices[str(regime)] = {
                        "rows": int(len(subset)),
                        "top_decile_long": _stats(high[target], high.minute_ts, cost_bps),
                        "bottom_decile_short": _stats(-low[target], low.minute_ts, cost_bps),
                    }
                item["regime_slices"][regime_col] = slices
            feature_results[feature] = item
        results[f"{horizon}m"] = feature_results
    return {
        "report_type": "event_agnostic_microstructure_discovery",
        "method": "past-only per-symbol rolling feature ranks; strictly contiguous future closed-candle returns; top-decile LONG and bottom-decile SHORT screens; regime slices; fixed round-trip cost deduction",
        "cost_bps_per_directional_trade_round_trip": cost_bps,
        "rank_lookback_minutes": 120,
        "rank_minimum_history_minutes": 60,
        "minimum_independent_24h_clusters_for_inference": 4,
        "feature_count": len(FEATURES),
        "results": results,
        "limitations": [
            "Single short capture cannot establish significance or regime robustness.",
            "Many feature/horizon/regime comparisons create multiple-testing risk; no p-values are treated as confirmation.",
            "Fixed cost deduction is a sensitivity assumption, not measured execution cost.",
            "No portfolio sizing, overlapping-position control, funding cashflows, or market-impact simulation.",
            "Current symbol universe is not point-in-time historical universe membership.",
            "Regime slices are descriptive, not optimized or validated for deployment.",
        ],
        "decision": "exploration_only_no_strategy_approved",
    }


def run(args):
    panel = build_features(load_panel(args.db), args.lookback, args.min_periods)
    report = summarize_discovery(panel, args.cost_bps)
    report["symbols"] = sorted(panel.symbol.dropna().unique().tolist())
    report["rows"] = int(len(panel))
    report["period_utc_ms"] = [int(panel.minute_ts.min()), int(panel.minute_ts.max())]
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--output", default="research-output/microstructure_discovery.json")
    parser.add_argument("--cost-bps", type=float, default=12.0)
    parser.add_argument("--lookback", type=int, default=120)
    parser.add_argument("--min-periods", type=int, default=60)
    args = parser.parse_args()
    run(args)
