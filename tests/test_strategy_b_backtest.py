from scripts.backtest_strategy_b import load_strategy_b, metric_pack


def test_strategy_b_backtest_loads_exact_live_builder():
    namespace = load_strategy_b()
    assert callable(namespace["build_signal_b"])
    assert namespace["MIN_CANDLE_ROWS"] == 205
    assert namespace["MIN_QUALITY_SCORE"] == 85.0
    assert namespace["MIN_NET_RR"] == 1.30
    assert namespace["TP1_R"] == 1.8
    assert namespace["TP2_R"] == 3.0


def test_strategy_b_backtest_metrics_are_net_of_costs():
    trades = [
        {"status": "tp2", "net_r": 2.5, "time": "2026-01-01T00:00:00+00:00"},
        {"status": "sl", "net_r": -1.2, "time": "2026-01-01T01:00:00+00:00"},
        {"status": "unresolved", "time": "2026-01-01T02:00:00+00:00"},
        {"status": "skipped_drift", "time": "2026-01-01T03:00:00+00:00"},
    ]
    result = metric_pack(trades)
    assert result["closed_trades"] == 2
    assert result["win_rate_pct"] == 50.0
    assert result["net_r"] == 1.3
    assert result["profit_factor"] == round(2.5 / 1.2, 3)
    assert result["unresolved"] == 1
    assert result["skipped_entry_drift"] == 1



def test_bingx_pagination_continues_after_interval_page_cap(monkeypatch):
    import json
    from contextlib import contextmanager
    from urllib.parse import parse_qs, urlparse

    from scripts import backtest_addons as market_data

    step = 300_000
    base = 1_700_000_000_000
    requested_windows = []

    @contextmanager
    def fake_urlopen(request, timeout=25):
        query = parse_qs(urlparse(request.full_url).query)
        cursor = int(query["startTime"][0])
        end = int(query["endTime"][0])
        requested_windows.append((cursor, end))
        # Simulate BingX returning only 400 candles despite a wider request.
        rows = []
        for open_time in range(cursor, min(end, cursor + 400 * step), step):
            rows.append({
                "openTime": open_time,
                "open": "100", "high": "101", "low": "99",
                "close": "100", "volume": "10",
                "closeTime": open_time + step - 1,
            })
        class Response:
            def read(self):
                return json.dumps({"code": 0, "data": rows}).encode()
        yield Response()

    monkeypatch.setattr(market_data.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(market_data.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(market_data.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(market_data, "LAST_API_REQUEST", 100.0)

    frame = market_data.fetch_interval("BTC-USDT", "5m", base, base + 800 * step)

    assert len(frame) == 800
    assert frame["open_time"].iloc[0] == base
    assert frame["open_time"].iloc[-1] == base + 799 * step
    assert len(requested_windows) == 2
    assert requested_windows[1][0] == base + 400 * step



def test_bingx_rate_limit_uses_long_cooldown_and_recovers(monkeypatch):
    import json
    from contextlib import contextmanager

    from scripts import backtest_addons as market_data

    base = 1_700_000_000_000
    responses = [
        {"code": 100410, "msg": "rate limit", "data": []},
        {"code": 100410, "msg": "rate limit", "data": []},
        {"code": 100410, "msg": "rate limit", "data": []},
        {"code": 0, "msg": "", "data": [{
            "openTime": base, "open": "100", "high": "101", "low": "99",
            "close": "100", "volume": "10", "closeTime": base + 59_999,
        }]},
    ]
    sleeps = []

    @contextmanager
    def fake_urlopen(request, timeout=25):
        payload = responses.pop(0)

        class Response:
            def read(self):
                return json.dumps(payload).encode()

        yield Response()

    monkeypatch.setattr(market_data.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(market_data.time, "sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(market_data.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(market_data, "LAST_API_REQUEST", 100.0)

    frame = market_data.fetch_interval("BTC-USDT", "1m", base, base + 60_000)

    assert len(frame) == 1
    assert [60, 120, 240] == [value for value in sleeps if value in (60, 120, 240)]


def test_bingx_rate_limit_failure_preserves_exchange_message(monkeypatch):
    import json
    from contextlib import contextmanager

    import pytest
    from scripts import backtest_addons as market_data

    @contextmanager
    def fake_urlopen(request, timeout=25):
        class Response:
            def read(self):
                return json.dumps({
                    "code": 100410, "msg": "IP cooldown active", "data": []
                }).encode()

        yield Response()

    monkeypatch.setattr(market_data.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(market_data.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(market_data.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(market_data, "LAST_API_REQUEST", 100.0)

    with pytest.raises(RuntimeError, match="IP cooldown active"):
        market_data.fetch_interval("BTC-USDT", "1m", 0, 60_000)



def test_binance_kline_pagination_and_normalization(monkeypatch):
    import json
    from contextlib import contextmanager
    from urllib.parse import parse_qs, urlparse

    from scripts import backtest_strategy_b as backtest

    base = 1_700_000_000_000
    step = 60_000
    requested = []

    @contextmanager
    def fake_urlopen(request, timeout=25):
        query = parse_qs(urlparse(request.full_url).query)
        cursor = int(query["startTime"][0])
        requested.append(cursor)
        rows = []
        for i in range(3):
            stamp = cursor + i * step
            rows.append([stamp, "100", "101", "99", "100.5", "12",
                         stamp + step - 1, "0", 10, "6", "0", "0"])
        class Response:
            def read(self):
                return json.dumps(rows).encode()
        yield Response()

    monkeypatch.setattr(backtest.urllib.request, "urlopen", fake_urlopen)
    frame = backtest.fetch_binance_history("XRP-USDT", "1m", base, base + 6 * step)
    assert len(frame) == 3
    assert requested == [base]
    assert frame.iloc[0]["close"] == 100.5
    assert frame.iloc[0]["volume"] == 12.0



def test_level_entry_waits_for_zone_touch_then_closed_5m_confirmation():
    import numpy as np
    import pandas as pd

    from scripts.backtest_strategy_b import resolve_signal_entry

    minute = pd.DataFrame([
        {"open": 100.0, "high": 100.2, "low": 99.8, "close_time": 60_000},
        {"open": 100.0, "high": 100.8, "low": 99.9, "close_time": 120_000},
        {"open": 100.2, "high": 100.7, "low": 100.0, "close_time": 180_000},
        {"open": 100.5, "high": 100.6, "low": 100.3, "close_time": 240_000},
    ])
    five = pd.DataFrame([{
        "close_time": 180_000, "close": 100.1, "ema20": 100.2,
        "ema50": 100.4, "macd": -0.2, "macd_signal": -0.1, "rsi": 42.0,
    }])
    close_times = {"5m": five["close_time"].to_numpy(dtype=np.int64)}
    signal = {
        "side": "SELL", "entry_mode": "level",
        "entry_zone_low": 100.0, "entry_zone_high": 101.0,
    }
    resolved = resolve_signal_entry(
        signal, minute, {"5m": five}, close_times, 0, max_wait_minutes=8
    )
    assert resolved["entry_mode"] == "level"
    assert resolved["entry_index"] == 3
    assert resolved["raw_entry"] == 100.5


def test_level_entry_without_zone_touch_is_not_filled():
    import numpy as np
    import pandas as pd

    from scripts.backtest_strategy_b import resolve_signal_entry

    minute = pd.DataFrame([
        {"open": 100.0, "high": 100.1, "low": 99.9, "close_time": 60_000},
        {"open": 100.0, "high": 100.2, "low": 99.8, "close_time": 120_000},
        {"open": 100.0, "high": 100.2, "low": 99.8, "close_time": 180_000},
    ])
    five = pd.DataFrame([{
        "close_time": 180_000, "close": 100.1, "ema20": 100.2,
        "ema50": 100.4, "macd": -0.2, "macd_signal": -0.1, "rsi": 42.0,
    }])
    close_times = {"5m": five["close_time"].to_numpy(dtype=np.int64)}
    signal = {
        "side": "SELL", "entry_mode": "level",
        "entry_zone_low": 101.0, "entry_zone_high": 102.0,
    }
    resolved = resolve_signal_entry(
        signal, minute, {"5m": five}, close_times, 0, max_wait_minutes=2
    )
    assert resolved["status"] == "unfilled_level"



def test_gate_futures_candle_normalization(monkeypatch):
    import json
    from contextlib import contextmanager

    from scripts import backtest_strategy_b as backtest

    base_sec = 1_700_000_000
    @contextmanager
    def fake_urlopen(request, timeout=25):
        class Response:
            def read(self):
                return json.dumps([{
                    "t": str(base_sec), "o": "0.50", "h": "0.51",
                    "l": "0.49", "c": "0.505", "v": "20", "sum": "50.5",
                }]).encode()
        yield Response()

    monkeypatch.setattr(backtest.urllib.request, "urlopen", fake_urlopen)
    frame = backtest.fetch_gate_history(
        "XRP-USDT", "1m", base_sec * 1000, (base_sec + 60) * 1000
    )
    assert len(frame) == 1
    assert frame.iloc[0]["close"] == 0.505
    assert frame.iloc[0]["volume"] == 100.0
