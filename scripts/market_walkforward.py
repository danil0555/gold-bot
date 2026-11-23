#!/usr/bin/env python3
"""Rolling walk-forward market-regime research using public BingX USDT-M data.

Screening only: no credentials, private endpoints, database, or order placement.
Each test block is strictly later than its training/validation data. Labels are
purged at fold boundaries so their forward holding windows do not cross splits.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from scripts.market_research import (
    BINGX_FEATURES,
    INTERVAL_MS,
    _metrics,
    _select_available_features,
    _select_threshold,
    build_panel,
    discover_universe,
    fetch_klines,
)


def make_walk_forward_folds(start_ms, end_ms, train_days=60, validation_days=30, test_days=30, step_days=30):
    """Expanding-window folds with disjoint validation/test blocks."""
    if min(train_days, validation_days, test_days, step_days) <= 0:
        raise ValueError("fold durations must be positive")
    if step_days < test_days:
        raise ValueError("step_days must be >= test_days to avoid overlapping test blocks")
    day = 86_400_000
    folds = []
    first_test = start_ms + (train_days + validation_days) * day
    test_start = first_test
    index = 0
    while test_start + test_days * day <= end_ms:
        validation_start = test_start - validation_days * day
        folds.append({
            "fold": index + 1,
            "train_start_ms": int(start_ms),
            "train_end_ms": int(validation_start),
            "validation_start_ms": int(validation_start),
            "validation_end_ms": int(test_start),
            "test_start_ms": int(test_start),
            "test_end_ms": int(test_start + test_days * day),
        })
        test_start += step_days * day
        index += 1
    return folds


def add_cross_sectional_context(panel):
    """Cross-sectional breadth/dispersion available at the same completed hour."""
    out = panel.copy()
    grouped = out.groupby("time", sort=False)["ret_24h"]
    out["market_breadth_24h"] = grouped.transform(lambda values: (values > 0).mean())
    out["market_dispersion_24h"] = grouped.transform("std")
    return out


def assign_regimes(panel, training_mask):
    """Regime thresholds are estimated only from the current fold's training rows."""
    out = panel.copy()
    train_vol = out.loc[training_mask, "btc_rv_24h"].dropna()
    vol_cut = float(train_vol.quantile(0.70)) if len(train_vol) else math.inf
    out["btc_trend_regime"] = np.select(
        [out["btc_ret_24h"] >= 0.01, out["btc_ret_24h"] <= -0.01],
        ["up", "down"], default="sideways",
    )
    out["btc_vol_regime"] = np.where(out["btc_rv_24h"] >= vol_cut, "high_vol", "normal_vol")
    out["btc_regime"] = out["btc_trend_regime"] + "_" + out["btc_vol_regime"]
    out["breadth_regime"] = np.select(
        [out["market_breadth_24h"] >= 0.60, out["market_breadth_24h"] <= 0.40],
        ["risk_on_breadth", "risk_off_breadth"], default="mixed_breadth",
    )
    return out, vol_cut


def _time_text(value):
    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


def _evaluate_fold(train, valid, test, features, label_col, return_col, cost_bps, fallback_threshold):
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    models = {
        "logistic": make_pipeline(
            SimpleImputer(strategy="median"), StandardScaler(),
            LogisticRegression(max_iter=1200, class_weight="balanced", C=0.2),
        ),
        "hist_gradient_boosting": make_pipeline(
            SimpleImputer(strategy="median"),
            HistGradientBoostingClassifier(
                max_iter=120, learning_rate=0.05, max_leaf_nodes=15,
                l2_regularization=2.0, random_state=17,
            ),
        ),
    }
    results = {}
    for name, model in models.items():
        model.fit(train[features], train[label_col].astype(int))
        classes = list(model[-1].classes_)
        if len(classes) < 2:
            results[name] = {"status": "single_class_training_fold"}
            continue
        valid_prob = model.predict_proba(valid[features])[:, classes.index(1)]
        threshold, threshold_report = _select_threshold(
            valid[label_col].to_numpy(), valid_prob, valid[return_col].to_numpy(),
            cost_bps / 10_000, fallback_threshold,
            timestamps=valid["time"].to_numpy(),
        )
        test_prob = model.predict_proba(test[features])[:, classes.index(1)]
        cost_metrics = {}
        for bps in (cost_bps, 20, 30):
            cost_metrics[f"{bps}bps"] = _metrics(
                test[label_col].to_numpy(), test_prob, test[return_col].to_numpy(),
                bps / 10_000, threshold, timestamps=test["time"].to_numpy(),
            )
        scored = test[["time", "btc_regime", "breadth_regime", label_col, return_col]].copy()
        scored["probability_up"] = test_prob
        scored["threshold"] = threshold
        results[name] = {
            "threshold_selection": threshold_report,
            "cost_sensitivity": cost_metrics,
            "scored_rows": scored,
        }
    return results


def run(args):
    now = int(time.time() * 1000)
    evaluation_start = now - args.days * 86_400_000
    fetch_start = evaluation_start - 10 * 86_400_000
    universe = discover_universe(args.universe, args.min_quote_volume)
    frames = {}
    for item in universe:
        symbol = item["symbol"]
        print(f"FETCH {symbol}", flush=True)
        try:
            frame = fetch_klines(symbol, fetch_start, now)
            frame = frame.loc[frame["time"] + INTERVAL_MS <= now].copy().reset_index(drop=True)
            if len(frame) < args.days * 24 * 0.90:
                print(f"SKIP_INCOMPLETE {symbol}: {len(frame)} candles", flush=True)
                continue
            frames[symbol] = frame
        except Exception as exc:
            print(f"SKIP_DATA_ERROR {symbol}: {exc}", flush=True)
    if len(frames) < 5:
        raise RuntimeError(f"Only {len(frames)} symbols have sufficient BingX history; need at least 5")

    panel = build_panel(frames, args.cost_bps / 10_000, {})
    panel = panel.loc[panel["time"] >= evaluation_start].copy()
    panel = add_cross_sectional_context(panel)
    features = list(BINGX_FEATURES) + ["market_breadth_24h", "market_dispersion_24h"]
    folds = make_walk_forward_folds(
        evaluation_start, now, args.train_days, args.validation_days,
        args.test_days, args.step_days,
    )
    if len(folds) < 2:
        raise RuntimeError(f"Only {len(folds)} walk-forward folds fit in {args.days} days; need at least 2")

    all_results = {}
    for horizon in (1, 4, 12, 24):
        label_col, return_col = f"label_{horizon}h", f"fwd_{horizon}h"
        purge_ms = horizon * INTERVAL_MS
        horizon_folds = []
        collected = {"logistic": [], "hist_gradient_boosting": []}
        for fold in folds:
            train_mask = (
                (panel["time"] >= fold["train_start_ms"])
                & (panel["time"] < fold["train_end_ms"] - purge_ms)
                & panel[label_col].notna()
            )
            valid_mask = (
                (panel["time"] >= fold["validation_start_ms"])
                & (panel["time"] < fold["validation_end_ms"] - purge_ms)
                & panel[label_col].notna()
            )
            test_mask = (
                (panel["time"] >= fold["test_start_ms"])
                & (panel["time"] < fold["test_end_ms"] - purge_ms)
                & panel[label_col].notna()
            )
            train = panel.loc[train_mask].copy()
            valid = panel.loc[valid_mask].copy()
            test = panel.loc[test_mask].copy()
            if len(train) < 500 or len(valid) < 100 or len(test) < 100:
                horizon_folds.append({
                    "fold": fold["fold"], "status": "insufficient_rows",
                    "train_rows": len(train), "validation_rows": len(valid), "test_rows": len(test),
                })
                continue

            fold_panel, vol_cut = assign_regimes(panel, train_mask)
            train = fold_panel.loc[train_mask].copy()
            valid = fold_panel.loc[valid_mask].copy()
            test = fold_panel.loc[test_mask].copy()
            selected_features, coverage = _select_available_features(
                fold_panel, features, fold["train_end_ms"] - purge_ms, min_coverage=0.60,
            )
            if len(selected_features) < 8:
                horizon_folds.append({
                    "fold": fold["fold"], "status": "insufficient_features",
                    "selected_features": selected_features, "coverage": coverage,
                })
                continue

            fold_models = _evaluate_fold(
                train, valid, test, selected_features, label_col, return_col,
                args.cost_bps, args.threshold,
            )
            fold_report = {
                "fold": fold["fold"],
                "train_period_utc": [_time_text(fold["train_start_ms"]), _time_text(fold["train_end_ms"] - purge_ms)],
                "validation_period_utc": [_time_text(fold["validation_start_ms"]), _time_text(fold["validation_end_ms"] - purge_ms)],
                "test_period_utc": [_time_text(fold["test_start_ms"]), _time_text(fold["test_end_ms"] - purge_ms)],
                "train_rows": len(train), "validation_rows": len(valid), "test_rows": len(test),
                "feature_count": len(selected_features), "selected_features": selected_features,
                "training_feature_coverage": coverage, "btc_volatility_cutoff": vol_cut,
                "models": {},
            }
            for name, model_result in fold_models.items():
                if "scored_rows" not in model_result:
                    fold_report["models"][name] = model_result
                    continue
                scored = model_result.pop("scored_rows")
                collected[name].append(scored)
                fold_report["models"][name] = model_result
            horizon_folds.append(fold_report)

        model_aggregates = {}
        for name, chunks in collected.items():
            if not chunks:
                model_aggregates[name] = {"status": "no_valid_test_folds"}
                continue
            scored = pd.concat(chunks, ignore_index=True).sort_values("time")
            threshold = scored["threshold"].to_numpy()
            p = scored["probability_up"].to_numpy()
            y = scored[label_col].astype(int).to_numpy()
            returns = scored[return_col].to_numpy()
            aggregate_costs = {}
            for bps in (args.cost_bps, 20, 30):
                aggregate_costs[f"{bps}bps"] = _metrics(
                    y, p, returns, bps / 10_000, float(np.median(threshold)),
                    timestamps=scored["time"].to_numpy(),
                )
            regime_reports = {}
            for col in ("btc_regime", "breadth_regime"):
                regime_reports[col] = {}
                for regime, group in scored.groupby(col):
                    regime_reports[col][str(regime)] = _metrics(
                        group[label_col].astype(int).to_numpy(),
                        group["probability_up"].to_numpy(),
                        group[return_col].to_numpy(),
                        args.cost_bps / 10_000, float(np.median(group["threshold"])),
                        timestamps=group["time"].to_numpy(),
                    )
            model_aggregates[name] = {
                "cost_sensitivity": aggregate_costs,
                "regime_breakdown": regime_reports,
                "fold_count": len(chunks),
                "test_rows": len(scored),
                "test_signals": int(sum(
                    ((scored["probability_up"] >= scored["threshold"])
                     | (scored["probability_up"] <= 1 - scored["threshold"])).to_numpy()
                )),
            }
        all_results[f"{horizon}h"] = {
            "folds": horizon_folds,
            "models": model_aggregates,
        }

    report = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "method": "BingX-only expanding walk-forward; rolling validation and disjoint test blocks; horizon purge at each boundary; train-only feature coverage and BTC volatility thresholds; validation-only probability threshold; cross-sectional breadth/dispersion; logistic and histogram gradient boosting; no execution/portfolio simulation.",
        "evaluation_days": args.days,
        "universe_requested": args.universe,
        "symbols_used": list(frames),
        "symbol_count": len(frames),
        "fold_configuration_days": {
            "minimum_training": args.train_days,
            "validation": args.validation_days,
            "test": args.test_days,
            "step": args.step_days,
        },
        "feature_set": features,
        "cross_sectional_features": ["market_breadth_24h", "market_dispersion_24h"],
        "cost_bps": args.cost_bps,
        "data_limitations": [
            "Current listed contracts only; survivorship/listing-history bias remains.",
            "Hourly candle flow fields are aggregate proxies, not historical trade-by-trade order flow or order-book replay.",
            "Historical liquidation and open-interest history are not reconstructed.",
            "Funding cashflows, portfolio sizing, overlapping positions, margin and exchange-specific execution are not simulated.",
            "The report screens directional predictability and is not an executable strategy backtest.",
            "The universe is selected using current quote volume; historical universe membership is not reconstructed.",
        ],
        "results": all_results,
    }
    os.makedirs(args.output_dir, exist_ok=True)
    output = os.path.join(args.output_dir, "market_walkforward_regimes.json")
    with open(output, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print("MARKET_WALKFORWARD_REPORT_BEGIN")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("MARKET_WALKFORWARD_REPORT_END")
    print(f"REPORT_PATH={output}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--universe", type=int, default=30)
    parser.add_argument("--min-quote-volume", type=float, default=5_000_000)
    parser.add_argument("--cost-bps", type=float, default=12.0)
    parser.add_argument("--threshold", type=float, default=0.58)
    parser.add_argument("--train-days", type=int, default=60)
    parser.add_argument("--validation-days", type=int, default=30)
    parser.add_argument("--test-days", type=int, default=30)
    parser.add_argument("--step-days", type=int, default=30)
    parser.add_argument("--output-dir", default="research-output")
    args = parser.parse_args()
    if not 120 <= args.days <= 365:
        parser.error("--days must be between 120 and 365")
    if not 5 <= args.universe <= 100:
        parser.error("--universe must be between 5 and 100")
    if not 0.50 <= args.threshold <= 0.90:
        parser.error("--threshold must be between 0.50 and 0.90")
    if args.step_days < args.test_days:
        parser.error("--step-days must be >= --test-days")
    run(args)
