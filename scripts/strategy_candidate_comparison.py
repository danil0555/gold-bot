"""Fair, side-by-side research comparison for candidate signal streams.

All strategies use the same candle panel, horizon and explicit cost scenarios.
This is a diagnostic wrapper around the conservative close-to-close evaluator,
not a portfolio simulator or strategy approval mechanism.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

import pandas as pd

from scripts.execution_cost_backtest import run_close_to_close_backtest


DEFAULT_COST_SCENARIOS = (
    {"name": "low_cost", "fee_bps_per_side": 2.0, "slippage_bps_per_side": 1.0},
    {"name": "base_cost", "fee_bps_per_side": 5.0, "slippage_bps_per_side": 3.0},
    {"name": "stress_cost", "fee_bps_per_side": 8.0, "slippage_bps_per_side": 8.0},
)


def _summary(report: dict) -> dict:
    fields = (
        "status", "trade_count", "win_rate_pct", "gross_expectancy_pct",
        "net_expectancy_pct", "net_total_return_pct", "profit_factor",
        "max_drawdown_pct", "average_holding_minutes", "rejected_signals",
    )
    return {key: report.get(key) for key in fields}


def _validate_signals(name: str, signals: pd.DataFrame) -> pd.DataFrame:
    required = {"symbol", "signal_ts", "direction"}
    if not isinstance(signals, pd.DataFrame) or not required.issubset(signals.columns):
        raise ValueError(f"{name}: signals require symbol, signal_ts, direction")
    result = signals[["symbol", "signal_ts", "direction"]].copy()
    if result[["symbol", "signal_ts", "direction"]].isna().any().any():
        raise ValueError(f"{name}: signal fields cannot be null")
    result["symbol"] = result["symbol"].astype(str)
    result["signal_ts"] = pd.to_numeric(result.signal_ts, errors="raise").astype("int64")
    result["direction"] = result.direction.astype(str).str.upper()
    if not result.direction.isin(["LONG", "SHORT"]).all():
        raise ValueError(f"{name}: direction must be LONG or SHORT")
    if result.duplicated(["symbol", "signal_ts"]).any():
        raise ValueError(f"{name}: duplicate symbol/signal_ts rows")
    return result.sort_values(["symbol", "signal_ts"]).reset_index(drop=True)


def compare_strategy_candidates(
    strategy_signals: Mapping[str, pd.DataFrame],
    candles: pd.DataFrame,
    *,
    horizon_minutes: int,
    cost_scenarios: Sequence[Mapping] = DEFAULT_COST_SCENARIOS,
    baseline_name: str = "Strategy B",
    funding_events: pd.DataFrame | None = None,
    expected_funding_timestamps_ms: list[int] | None = None,
) -> dict:
    """Compare Strategy B and named candidates on identical market data.

    Candidate signal streams must be generated without look-ahead. Cost
    scenarios are applied identically. Per-symbol and UTC-day breakdowns
    expose concentration and temporal instability.
    """
    if not isinstance(strategy_signals, Mapping) or baseline_name not in strategy_signals:
        raise ValueError(f"strategy_signals must include baseline {baseline_name!r}")
    if not strategy_signals:
        raise ValueError("at least one strategy is required")
    if not cost_scenarios:
        raise ValueError("at least one cost scenario is required")
    if not {"symbol", "minute_ts", "close"}.issubset(candles.columns):
        raise ValueError("candles require symbol, minute_ts, close")

    normalized = {
        str(name): _validate_signals(str(name), frame)
        for name, frame in strategy_signals.items()
    }
    if len(normalized) != len(strategy_signals):
        raise ValueError("strategy names must be unique after string conversion")

    scenario_defs = []
    seen_scenarios = set()
    for item in cost_scenarios:
        name = str(item.get("name", "")).strip()
        if not name or name in seen_scenarios:
            raise ValueError("cost scenario names must be non-empty and unique")
        seen_scenarios.add(name)
        scenario_defs.append({
            "name": name,
            "fee_bps_per_side": item["fee_bps_per_side"],
            "slippage_bps_per_side": item["slippage_bps_per_side"],
        })

    candle_frame = candles.copy()
    candle_frame["symbol"] = candle_frame.symbol.astype(str)
    candle_frame["minute_ts"] = pd.to_numeric(
        candle_frame.minute_ts, errors="raise"
    ).astype("int64")

    results = []
    by_key = {}
    for scenario in scenario_defs:
        for name, signals in normalized.items():
            report = run_close_to_close_backtest(
                signals, candle_frame, horizon_minutes=horizon_minutes,
                fee_bps_per_side=scenario["fee_bps_per_side"],
                slippage_bps_per_side=scenario["slippage_bps_per_side"],
                funding_events=funding_events,
                expected_funding_timestamps_ms=expected_funding_timestamps_ms,
            )
            trades = pd.DataFrame(report.get("trades", []))
            row = {
                "strategy": name,
                "cost_scenario": scenario["name"],
                "costs": {
                    "fee_bps_per_side": scenario["fee_bps_per_side"],
                    "slippage_bps_per_side": scenario["slippage_bps_per_side"],
                },
                "overall": _summary(report),
                "by_symbol": [],
                "by_utc_date": [],
            }
            if not trades.empty:
                trades["utc_date"] = pd.to_datetime(
                    trades.entry_ts, unit="ms", utc=True
                ).dt.strftime("%Y-%m-%d")
                for symbol, group in trades.groupby("symbol", sort=True):
                    row["by_symbol"].append({
                        "symbol": str(symbol),
                        "trade_count": int(len(group)),
                        "net_expectancy_pct": float(group.net_return_pct.mean()),
                        "win_rate_pct": float((group.net_return_pct > 0).mean() * 100),
                        "net_total_return_pct": float(
                            ((1 + group.net_return_pct / 100).prod() - 1) * 100
                        ),
                    })
                for day, group in trades.groupby("utc_date", sort=True):
                    row["by_utc_date"].append({
                        "utc_date": str(day),
                        "trade_count": int(len(group)),
                        "net_expectancy_pct": float(group.net_return_pct.mean()),
                        "win_rate_pct": float((group.net_return_pct > 0).mean() * 100),
                    })
            results.append(row)
            by_key[(scenario["name"], name)] = row["overall"]

    deltas = []
    for scenario in scenario_defs:
        baseline = by_key[(scenario["name"], baseline_name)]
        for name in normalized:
            if name == baseline_name:
                continue
            candidate = by_key[(scenario["name"], name)]

            def delta(field):
                left, right = candidate.get(field), baseline.get(field)
                return None if left is None or right is None else float(left - right)

            deltas.append({
                "strategy": name,
                "baseline": baseline_name,
                "cost_scenario": scenario["name"],
                "trade_count_delta": (
                    None if candidate.get("trade_count") is None
                    or baseline.get("trade_count") is None
                    else int(candidate["trade_count"] - baseline["trade_count"])
                ),
                "net_expectancy_delta_pct_points": delta("net_expectancy_pct"),
                "net_total_return_delta_pct_points": delta("net_total_return_pct"),
                "max_drawdown_delta_pct_points": delta("max_drawdown_pct"),
            })

    return {
        "report_type": "strategy_candidate_comparison_v1",
        "status": "evaluated",
        "baseline": baseline_name,
        "horizon_minutes": horizon_minutes,
        "strategies": list(normalized),
        "cost_scenarios": scenario_defs,
        "results": results,
        "candidate_vs_baseline": deltas,
        "methodology": [
            "Every strategy uses the same candle panel, horizon and cost scenarios.",
            "Signals are assumed known after their candle closes; entry and exit timing follow the close-to-close evaluator.",
            "Fee and slippage assumptions are fixed per side and applied identically to every strategy.",
            "Per-symbol and UTC-entry-date breakdowns expose concentration and time instability.",
            "Parameter variants can be supplied as separate named signal streams to inspect parameter sensitivity.",
        ],
        "limitations": [
            "This is not a portfolio-capital, margin, leverage, liquidation, stop/target or order-book fill simulator.",
            "Different strategies may generate different trade counts and market exposures; raw returns are not exposure-normalized.",
            "UTC-day breakdowns with few trades are descriptive only.",
            "No confidence intervals, bootstrap, multiple-testing correction or independent future holdout is performed here.",
            "A positive comparison does not prove predictive value or approve a live strategy.",
        ],
    }
