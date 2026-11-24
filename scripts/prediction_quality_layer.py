"""Shadow-mode prediction quality features and evaluation utilities.

This module is deliberately independent from the live signal path. It enriches
historical/live research panels with causal market context and evaluates
probabilities on held-out outcomes. It never places orders or edits production
state.
"""
from __future__ import annotations

from typing import Iterable

import numpy as np
import pandas as pd


REQUIRED_CANDLE_COLUMNS = {"symbol", "minute_ts", "close"}


def _safe_return(current: pd.Series, previous: pd.Series) -> pd.Series:
    previous = pd.to_numeric(previous, errors="coerce")
    current = pd.to_numeric(current, errors="coerce")
    return current.div(previous.where(previous > 0)).sub(1.0)


def _exact_lag(frame: pd.DataFrame, value: str, minutes: int) -> pd.Series:
    """Return value from the exact prior timestamp; gaps remain missing."""
    lookup = frame[["symbol", "minute_ts", value]].copy()
    lookup["minute_ts"] = lookup["minute_ts"] + minutes * 60_000
    lookup = lookup.rename(columns={value: f"__lag_{value}_{minutes}"})
    joined = frame[["symbol", "minute_ts"]].merge(
        lookup, on=["symbol", "minute_ts"], how="left", validate="one_to_one"
    )
    return joined[f"__lag_{value}_{minutes}"].set_axis(frame.index)


def _add_symbol_features(group: pd.DataFrame) -> pd.DataFrame:
    g = group.sort_values("minute_ts").copy()
    g["return_1m"] = _safe_return(g["close"], g["close"].shift(1))
    for minutes in (5, 15, 60):
        lag = _exact_lag(g, "close", minutes)
        g[f"return_{minutes}m"] = _safe_return(g["close"], lag)
    g["rv_30m"] = g["return_1m"].rolling(30, min_periods=20).std() * np.sqrt(30)
    g["rv_60m"] = g["return_1m"].rolling(60, min_periods=40).std() * np.sqrt(60)

    abs_move = g["return_1m"].abs()
    path = abs_move.rolling(60, min_periods=40).sum()
    g["trend_efficiency_60m"] = g["return_60m"].abs().div(path.where(path > 0))
    # Regime thresholds use only the trailing observations available at this row.
    vol_ref = g["rv_60m"].rolling(360, min_periods=60).quantile(0.75)
    high_vol = g["rv_60m"].gt(vol_ref)
    trend = g["trend_efficiency_60m"].ge(0.35) & g["return_60m"].abs().ge(0.003)
    g["market_regime"] = np.select(
        [g["rv_60m"].isna() | g["trend_efficiency_60m"].isna(),
         high_vol & trend, high_vol, trend],
        ["unknown", "high_vol_trend", "high_vol_range", "trend"],
        default="range",
    )
    return g


def add_market_context(
    candles: pd.DataFrame,
    derivatives: pd.DataFrame | None = None,
    benchmark_symbol: str = "BTC-USDT",
    max_derivatives_age_ms: int = 120_000,
) -> pd.DataFrame:
    """Add causal regime, BTC-relative and backward-only derivatives context.

    candles: one row per symbol/minute with closed-candle close prices.
    derivatives: optional snapshots with symbol, ts_ms, open_interest_base,
                 funding_rate. Only snapshots at or before minute_ts are used.
    """
    missing = REQUIRED_CANDLE_COLUMNS - set(candles.columns)
    if missing:
        raise ValueError(f"missing candle columns: {sorted(missing)}")
    if max_derivatives_age_ms < 0:
        raise ValueError("max_derivatives_age_ms must be non-negative")

    base = candles.copy()
    base["symbol"] = base["symbol"].astype(str)
    base["minute_ts"] = pd.to_numeric(base["minute_ts"], errors="raise").astype("int64")
    base["close"] = pd.to_numeric(base["close"], errors="coerce")
    if base.duplicated(["symbol", "minute_ts"]).any():
        raise ValueError("candles must be unique by symbol and minute_ts")
    if (base["close"].dropna() <= 0).any():
        raise ValueError("close prices must be positive")

    parts = [_add_symbol_features(g) for _, g in base.groupby("symbol", sort=False)]
    out = pd.concat(parts, ignore_index=True).sort_values(
        ["symbol", "minute_ts"]
    ).reset_index(drop=True)

    benchmark = out.loc[out.symbol == benchmark_symbol, ["minute_ts", "return_5m", "return_15m"]]
    benchmark = benchmark.rename(columns={
        "return_5m": "btc_return_5m", "return_15m": "btc_return_15m"
    })
    out = out.merge(benchmark, on="minute_ts", how="left", validate="many_to_one")
    out["relative_return_5m"] = out["return_5m"] - out["btc_return_5m"]
    out["relative_return_15m"] = out["return_15m"] - out["btc_return_15m"]
    out.loc[out.symbol == benchmark_symbol, ["relative_return_5m", "relative_return_15m"]] = 0.0

    out["oi_change_5m_pct"] = np.nan
    out["funding_rate"] = np.nan
    out["derivatives_age_ms"] = np.nan
    if derivatives is not None and not derivatives.empty:
        required = {"symbol", "ts_ms", "open_interest_base", "funding_rate"}
        absent = required - set(derivatives.columns)
        if absent:
            raise ValueError(f"missing derivatives columns: {sorted(absent)}")
        deriv = derivatives.copy()
        deriv["symbol"] = deriv["symbol"].astype(str)
        deriv["ts_ms"] = pd.to_numeric(deriv["ts_ms"], errors="coerce")
        deriv = deriv.dropna(subset=["ts_ms"]).sort_values(["symbol", "ts_ms"])
        deriv["open_interest_base"] = pd.to_numeric(
            deriv["open_interest_base"], errors="coerce"
        )
        deriv["funding_rate"] = pd.to_numeric(deriv["funding_rate"], errors="coerce")
        for symbol, indices in out.groupby("symbol", sort=False).groups.items():
            snapshots = deriv.loc[deriv.symbol == symbol].sort_values("ts_ms")
            if snapshots.empty:
                continue
            left = out.loc[indices, ["minute_ts"]].copy().sort_values("minute_ts")
            right = snapshots[["ts_ms", "open_interest_base", "funding_rate"]].copy()
            joined = pd.merge_asof(
                left, right, left_on="minute_ts", right_on="ts_ms",
                direction="backward", tolerance=max_derivatives_age_ms,
            )
            prior = joined[["minute_ts", "open_interest_base"]].copy()
            prior["minute_ts"] = prior["minute_ts"] + 5 * 60_000
            prior = prior.rename(columns={"open_interest_base": "prior_open_interest_base"})
            joined = joined.merge(prior, on="minute_ts", how="left", validate="one_to_one")
            oi = joined["open_interest_base"]
            prior_oi = joined["prior_open_interest_base"]
            oi_change = oi.div(prior_oi.where(prior_oi > 0)).sub(1.0) * 100.0
            target_idx = left.index
            out.loc[target_idx, "oi_change_5m_pct"] = oi_change.to_numpy()
            out.loc[target_idx, "funding_rate"] = joined["funding_rate"].to_numpy()
            out.loc[target_idx, "derivatives_age_ms"] = (
                joined["minute_ts"] - joined["ts_ms"]
            ).to_numpy()

    # OI/price quadrants are positioning proxies, not liquidation observations.
    oi_up = out["oi_change_5m_pct"] > 0
    price_up = out["return_5m"] > 0
    known = out["oi_change_5m_pct"].notna() & out["return_5m"].notna()
    out["positioning_proxy"] = np.select(
        [known & oi_up & price_up, known & oi_up & ~price_up,
         known & ~oi_up & price_up, known & ~oi_up & ~price_up],
        ["long_buildup_proxy", "short_buildup_proxy",
         "short_covering_proxy", "long_unwinding_proxy"],
        default="unknown",
    )
    out["liquidation_data_available"] = False
    return out


def fit_probability_calibrator(
    probabilities: Iterable[float],
    outcomes: Iterable[int],
    bins: int = 10,
) -> dict:
    """Fit a simple reliability-bin calibrator on TRAINING data only."""
    p = np.asarray(list(probabilities), dtype=float)
    y = np.asarray(list(outcomes), dtype=float)
    if bins < 2:
        raise ValueError("bins must be >= 2")
    if p.size != y.size or p.size == 0:
        raise ValueError("probabilities and outcomes must have equal non-zero length")
    if not np.isfinite(p).all() or not np.isfinite(y).all():
        raise ValueError("calibration inputs must be finite")
    if ((p < 0) | (p > 1)).any() or not np.isin(y, [0, 1]).all():
        raise ValueError("probabilities must be in [0,1] and outcomes must be 0/1")
    edges = np.linspace(0.0, 1.0, bins + 1)
    centers, rates, counts = [], [], []
    for i in range(bins):
        mask = (p >= edges[i]) & (
            (p < edges[i + 1]) if i < bins - 1 else (p <= edges[i + 1])
        )
        if mask.any():
            centers.append(float(p[mask].mean()))
            rates.append(float(y[mask].mean()))
            counts.append(int(mask.sum()))
    if not centers:
        raise ValueError("no populated calibration bins")
    return {"method": "equal_width_reliability_bins", "bin_centers": centers,
            "observed_rates": rates, "counts": counts, "training_rows": int(p.size)}


def apply_probability_calibrator(
    probabilities: Iterable[float], calibrator: dict
) -> np.ndarray:
    """Apply a frozen training calibrator; never refits on evaluation rows."""
    p = np.asarray(list(probabilities), dtype=float)
    if not np.isfinite(p).all() or ((p < 0) | (p > 1)).any():
        raise ValueError("probabilities must be finite and in [0,1]")
    centers = np.asarray(calibrator["bin_centers"], dtype=float)
    rates = np.asarray(calibrator["observed_rates"], dtype=float)
    if centers.size == 0 or centers.size != rates.size:
        raise ValueError("invalid calibrator")
    order = np.argsort(centers)
    return np.interp(p, centers[order], rates[order],
                     left=rates[order][0], right=rates[order][-1])


def evaluate_probabilities(
    probabilities: Iterable[float], outcomes: Iterable[int]
) -> dict:
    """Evaluate probabilities on a held-out set, not on calibrator training rows."""
    p = np.asarray(list(probabilities), dtype=float)
    y = np.asarray(list(outcomes), dtype=float)
    if p.size != y.size or p.size == 0:
        raise ValueError("probabilities and outcomes must have equal non-zero length")
    if not np.isfinite(p).all() or not np.isfinite(y).all():
        raise ValueError("evaluation inputs must be finite")
    if ((p < 0) | (p > 1)).any() or not np.isin(y, [0, 1]).all():
        raise ValueError("probabilities must be in [0,1] and outcomes must be 0/1")
    return {"rows": int(p.size), "brier_score": float(np.mean((p-y) ** 2)),
            "mean_predicted_probability": float(p.mean()),
            "observed_success_rate": float(y.mean())}


def summarize_prediction_errors(
    predictions: pd.DataFrame,
    regime_column: str = "market_regime",
) -> dict:
    """Summarize misses by regime and side without training a new model."""
    required = {"predicted_direction", "actual_direction", regime_column}
    missing = required - set(predictions.columns)
    if missing:
        raise ValueError(f"missing prediction columns: {sorted(missing)}")
    frame = predictions.copy()
    frame["is_correct"] = (
        frame["predicted_direction"].astype(str).str.upper()
        == frame["actual_direction"].astype(str).str.upper()
    )
    frame["market_regime"] = frame[regime_column].fillna("unknown").astype(str)
    groups = []
    for (regime, side), group in frame.groupby(
        ["market_regime", "predicted_direction"], dropna=False
    ):
        groups.append({"regime": str(regime), "predicted_direction": str(side),
                       "samples": int(len(group)),
                       "accuracy_pct": round(float(group.is_correct.mean() * 100), 2),
                       "error_count": int((~group.is_correct).sum())})
    return {"rows": int(len(frame)), "correct": int(frame.is_correct.sum()),
            "errors": int((~frame.is_correct).sum()), "by_regime_and_side": groups,
            "warning": "descriptive diagnostics only; small groups are unstable"}


def build_shadow_quality_report(
    candles: pd.DataFrame,
    derivatives: pd.DataFrame | None = None,
    benchmark_symbol: str = "BTC-USDT",
) -> dict:
    """Return feature coverage and safe-use metadata for shadow-mode monitoring."""
    enriched = add_market_context(candles, derivatives, benchmark_symbol)
    return {
        "report_type": "shadow_prediction_quality_context_v1",
        "rows": int(len(enriched)),
        "symbols": sorted(enriched.symbol.unique().tolist()),
        "regime_counts": {
            str(k): int(v) for k, v in enriched.market_regime.value_counts().items()
        },
        "btc_context_coverage_pct": round(
            float(enriched.btc_return_5m.notna().mean() * 100), 2
        ),
        "derivatives_coverage_pct": round(
            float(enriched.derivatives_age_ms.notna().mean() * 100), 2
        ),
        "liquidation_data_available": False,
        "positioning_proxy_is_not_liquidation_data": True,
        "mode": "shadow_only_no_signal_changes_no_orders",
        "features": enriched,
    }
