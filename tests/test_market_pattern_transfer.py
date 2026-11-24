import numpy as np
import pandas as pd

from scripts.market_pattern_transfer import (
    _build_holdout_label_panel, _condition_mask, _label_tail_coverage,
    _target_feature_cuts, build_cross_symbol_transfer_report,
    evaluate_cross_symbol_transfer,
)


def test_target_state_cutoffs_are_fitted_from_target_training_rows():
    target = pd.DataFrame({"feature_a": np.arange(100, dtype=float)})
    cuts = _target_feature_cuts(target, {"feature_a"})
    assert cuts["feature_a"]["low"] == 19.8
    assert cuts["feature_a"]["high"] == 79.2


def test_condition_mask_uses_target_cutoffs_not_source_numeric_thresholds():
    frame = pd.DataFrame({"feature_a": [0.0, 50.0, 100.0]})
    conditions = [{"feature": "feature_a", "state": "high", "threshold": 999999.0}]
    mask = _condition_mask(frame, conditions, {"feature_a": {"low": 20.0, "high": 80.0}})
    assert mask.tolist() == [False, False, True]


def test_cross_symbol_transfer_never_transfers_to_same_symbol(monkeypatch):
    import scripts.market_pattern_transfer as transfer

    train = pd.DataFrame({
        "symbol": ["BTC-USDT"] * 30 + ["ETH-USDT"] * 30,
        "minute_ts": list(range(30)) + list(range(30)),
        "feature_a": list(np.linspace(-2, 2, 30)) * 2,
        "feature_b": list(np.linspace(2, -2, 30)) * 2,
        "rv_60m": [0.001] * 60,
    })
    holdout = pd.DataFrame({
        "symbol": ["BTC-USDT"] * 30 + ["ETH-USDT"] * 30,
        "minute_ts": list(range(100, 130)) + list(range(100, 130)),
        "feature_a": list(np.linspace(-2, 2, 30)) * 2,
        "feature_b": list(np.linspace(2, -2, 30)) * 2,
        "rv_60m": [0.001] * 60,
        "close": [100.0] * 60,
    })
    pattern = {
        "symbol": "BTC-USDT", "horizon_minutes": 5, "direction": "UP",
        "conditions": [
            {"feature": "feature_a", "state": "high", "threshold": 1.9},
            {"feature": "feature_b", "state": "low", "threshold": -1.9},
        ], "event_support": 8,
    }
    monkeypatch.setattr(transfer, "mine_patterns", lambda *a, **k: {"patterns": [pattern]})
    monkeypatch.setattr(
        transfer, "_build_control_frame",
        lambda *a, **k: pd.DataFrame({
            "symbol": ["ETH-USDT"] * 8,
            "horizon": [5] * 8,
            "direction": ["UP"] * 8,
            "utc_hour": [0] * 8,
            "volatility_bucket": [0] * 8,
            "feature_a": [2.0] * 8,
            "feature_b": [-2.0] * 8,
        }),
    )
    episodes = []
    for symbol in ("BTC-USDT", "ETH-USDT"):
        for index in range(8):
            episodes.append({
                "symbol": symbol, "start_ts": 100 + index,
                "horizon_minutes": 5, "direction": "UP",
                "pre_move_features": {"feature_a": 2.0, "feature_b": -2.0, "rv_60m": 0.001},
            })
    result = evaluate_cross_symbol_transfer(train, holdout, [], episodes, min_support=2)
    assert all(item["source_symbol"] != item["target_symbol"] for item in result["transfer_results"])
    assert {item["target_symbol"] for item in result["transfer_results"]} <= {"ETH-USDT"}
    evaluated = [item for item in result["transfer_results"] if item["status"] == "evaluated"]
    assert len(evaluated) == 1
    assert evaluated[0]["target_event_match_coverage_pct"] == 100.0
    assert evaluated[0]["target_control_match_coverage_pct"] == 100.0
    assert evaluated[0]["target_event_total_before_matching"] == 8
    assert evaluated[0]["target_control_total_before_matching"] == 8



def test_cross_symbol_label_tail_is_added_per_symbol_but_holdout_stays_separate():
    holdout = pd.DataFrame({
        "symbol": ["BTC-USDT"] * 3 + ["ETH-USDT"] * 3,
        "minute_ts": [0, 60_000, 120_000] * 2,
        "close": [100.0] * 6,
    })
    full = pd.concat([
        holdout,
        pd.DataFrame({
            "symbol": ["BTC-USDT"] * 2 + ["ETH-USDT"],
            "minute_ts": [180_000, 240_000, 180_000],
            "close": [100.0] * 3,
        }),
    ], ignore_index=True)
    label_panel = _build_holdout_label_panel(full, holdout, requested_tail_minutes=2)
    assert len(label_panel) == len(holdout) + 3
    label_rows = set(zip(label_panel["symbol"], label_panel["minute_ts"]))
    assert ("BTC-USDT", 180_000) in label_rows
    assert ("BTC-USDT", 240_000) in label_rows
    assert ("ETH-USDT", 180_000) in label_rows
    # The evaluation/control frame itself is not mutated or extended.
    assert len(holdout) == 6
    coverage = _label_tail_coverage(full, holdout, requested_tail_minutes=2)
    assert coverage["symbols_checked"] == 2
    assert coverage["symbols_with_complete_tail"] == 1
    assert coverage["complete_for_all_symbols"] is False
    per_symbol = {item["symbol"]: item["contiguous_tail_minutes"] for item in coverage["per_symbol"]}
    assert per_symbol == {"BTC-USDT": 2, "ETH-USDT": 1}



def test_transfer_report_reserves_tail_and_bounds_event_starts(monkeypatch):
    import scripts.market_pattern_transfer as transfer

    rows = []
    for symbol in ("BTC-USDT", "ETH-USDT"):
        for minute in range(200):
            rows.append({
                "symbol": symbol, "minute_ts": minute * 60_000,
                "close": 100.0, "rv_60m": 0.001,
            })
    panel = pd.DataFrame(rows)
    discovery_calls = []

    def fake_discover(frame, **kwargs):
        discovery_calls.append((frame.copy(), kwargs))
        return [], {}

    monkeypatch.setattr(transfer, "discover_episodes", fake_discover)
    monkeypatch.setattr(
        transfer, "evaluate_cross_symbol_transfer",
        lambda *args, **kwargs: {"decision": "exploratory_transfer_test_only_no_strategy_approved"},
    )
    report = build_cross_symbol_transfer_report(panel, train_fraction=0.70, min_support=2)

    assert len(discovery_calls) == 2
    train_label_frame, train_kwargs = discovery_calls[0]
    assert train_kwargs["start_time_bounds"] == (0, 109 * 60_000)
    assert train_label_frame["minute_ts"].max() == 139 * 60_000
    holdout_label_frame, kwargs = discovery_calls[1]
    assert kwargs["start_time_bounds"] == (
        report["split"]["holdout_start_utc_ms"],
        report["split"]["holdout_end_utc_ms"],
    )
    assert holdout_label_frame["minute_ts"].max() == 199 * 60_000
    assert report["split"]["holdout_end_utc_ms"] == 169 * 60_000
    assert report["split"]["label_only_rows"] == 60
    assert report["split"]["label_tail"]["complete_for_all_symbols"] is True
    assert report["split"]["label_tail"]["symbols_with_complete_tail"] == 2
