"""Small, deterministic reliability helpers shared by live code and tests.

This module has no network, exchange, database, or order-execution side effects.
"""
from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

RETRYABLE_HTTP_STATUS = frozenset({418, 429, 500, 502, 503, 504})


def retry_after_seconds(value: Any, attempt: int, cap_seconds: float = 30.0) -> float:
    """Parse Retry-After seconds/date; otherwise use capped exponential backoff."""
    fallback = min(2.0 ** max(0, int(attempt)), cap_seconds)
    if value is None:
        return fallback
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        try:
            dt = parsedate_to_datetime(str(value))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            seconds = (dt - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return fallback
    if seconds != seconds or seconds in (float("inf"), float("-inf")):
        return fallback
    return max(0.0, min(float(seconds), cap_seconds))


def contiguous_candle_tail(frame, interval_ms: int, min_rows: int):
    """Return the newest uninterrupted OHLCV segment or raise ValueError.

    Input must already be sorted and de-duplicated by open_time. A gap earlier
    in the response is allowed; a gap in the tail used by indicators is not.
    """
    if interval_ms <= 0 or min_rows < 1:
        raise ValueError("interval_ms and min_rows must be positive")
    required = {"open_time", "close_time"}
    if frame is None or frame.empty or not required.issubset(frame.columns):
        raise ValueError("candle frame is empty or missing timestamps")

    opens = frame["open_time"].astype("int64").to_numpy()
    closes = frame["close_time"].astype("int64").to_numpy()
    if len(opens) != len(closes):
        raise ValueError("candle timestamp arrays have different lengths")
    if len(opens) > 1 and not (opens[1:] > opens[:-1]).all():
        raise ValueError("candle open_time values are not strictly increasing")
    if not (closes == opens + interval_ms - 1).all():
        raise ValueError("candle close_time does not match its timeframe")

    breaks = [i for i in range(1, len(opens)) if opens[i] - opens[i - 1] != interval_ms]
    start = breaks[-1] if breaks else 0
    tail = frame.iloc[start:].copy().reset_index(drop=True)
    if len(tail) < min_rows:
        raise ValueError(
            f"latest uninterrupted candle segment has {len(tail)} rows; "
            f"{min_rows} required"
        )
    return tail
