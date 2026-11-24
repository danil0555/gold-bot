import numpy as np
import pandas as pd
import pytest

from scripts.prediction_quality_layer import (
    add_market_context,
    apply_probability_calibrator,
    evaluate_probabilities,
    fit_probability_calibrator,
    summarize_prediction_errors,
)


def _candles():
    ts = np.arange(120, dtype=np.int64) * 60_000
    rows = []
    for symbol, start, drift in (
        ("BTC-USDT", 100.0, 0.0002),
        ("ETH-USDT", 50.0, 0.0003),
    ):
        for i, stamp in enumerate(ts):
            rows.append((symbol, int(stamp), start * (1 + drift) ** i))
    return pd.DataFrame(rows, columns=["symbol", "minute_ts", "close"])


def test_context_uses_exact_btc_timestamp_and_adds_regime():
    frame = _candles()
    result = add_market_context(frame)
    eth = result[result.symbol == "ETH-USDT"].sort_values("minute_ts")
    assert "market_regime" in eth
    assert eth.iloc[-1].btc_return_5m is not None
    assert np.isfinite(eth.iloc[-1].relative_return_5m)


def test_missing_btc_benchmark_does_not_invent_context():
    frame = _candles()
    frame = frame[frame.symbol != "BTC-USDT"]
    result = add_market_context(frame)
    assert result.btc_return_5m.isna().all()


def test_derivatives_are_backward_joined_and_stale_data_is_rejected():
    candles = _candles()
    derivatives = pd.DataFrame([
        ("ETH-USDT", 60_000, 10.0, 0.0001),
        ("ETH-USDT", 360_000, 11.0, 0.0002),
        ("ETH-USDT", 7_200_000, 12.0, 0.0003),
    ], columns=["symbol", "ts_ms", "open_interest_base", "funding_rate"])
    result = add_market_context(candles, derivatives)
    eth = result[result.symbol == "ETH-USDT"].sort_values("minute_ts")
    assert eth.iloc[1].derivatives_age_ms == 0
    assert pd.isna(eth.iloc[-1].derivatives_age_ms)
    assert not bool(eth.iloc[-1].liquidation_data_available)


def test_calibrator_is_frozen_and_metrics_are_well_formed():
    calibrator = fit_probability_calibrator(
        [0.1, 0.2, 0.8, 0.9], [0, 0, 1, 1], bins=4
    )
    calibrated = apply_probability_calibrator([0.15, 0.85], calibrator)
    assert calibrated[0] < calibrated[1]
    metrics = evaluate_probabilities(calibrated, [0, 1])
    assert metrics["rows"] == 2
    assert 0 <= metrics["brier_score"] <= 1


def test_invalid_calibration_inputs_are_rejected():
    with pytest.raises(ValueError):
        fit_probability_calibrator([0.1, 1.2], [0, 1])


def test_error_report_breaks_down_by_regime_and_direction():
    frame = pd.DataFrame({
        "predicted_direction": ["UP", "UP", "DOWN"],
        "actual_direction": ["UP", "DOWN", "DOWN"],
        "market_regime": ["trend", "trend", "range"],
    })
    report = summarize_prediction_errors(frame)
    assert report["rows"] == 3
    assert report["correct"] == 2
    assert len(report["by_regime_and_side"]) == 2
