import numpy as np
import pandas as pd

from scripts.market_pattern_transfer import (
    _condition_mask, _target_feature_cuts, evaluate_cross_symbol_transfer,
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
