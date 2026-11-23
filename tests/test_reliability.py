import pandas as pd
import pytest

from scripts.reliability import contiguous_candle_tail, retry_after_seconds


def candles(open_times, interval_ms=60_000):
    return pd.DataFrame({
        "open_time": open_times,
        "close_time": [stamp + interval_ms - 1 for stamp in open_times],
        "open": [100.0] * len(open_times),
        "high": [101.0] * len(open_times),
        "low": [99.0] * len(open_times),
        "close": [100.5] * len(open_times),
        "volume": [10.0] * len(open_times),
    })


def test_retry_after_numeric_header_is_honored_and_capped():
    assert retry_after_seconds("7", attempt=0) == 7.0
    assert retry_after_seconds("90", attempt=0) == 30.0


def test_retry_backoff_is_exponential_and_capped():
    assert retry_after_seconds(None, attempt=0) == 1.0
    assert retry_after_seconds(None, attempt=3) == 8.0
    assert retry_after_seconds(None, attempt=10) == 30.0


def test_candle_validator_returns_latest_contiguous_tail():
    frame = candles([0, 60_000, 120_000, 240_000, 300_000, 360_000])
    result = contiguous_candle_tail(frame, 60_000, 3)
    assert result["open_time"].tolist() == [240_000, 300_000, 360_000]


def test_candle_validator_rejects_short_tail_after_gap():
    frame = candles([0, 60_000, 180_000, 240_000])
    with pytest.raises(ValueError, match="uninterrupted"):
        contiguous_candle_tail(frame, 60_000, 3)


def test_candle_validator_rejects_incorrect_close_time():
    frame = candles([0, 60_000, 120_000])
    frame.loc[2, "close_time"] += 1
    with pytest.raises(ValueError, match="close_time"):
        contiguous_candle_tail(frame, 60_000, 2)
