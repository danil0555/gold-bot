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

    frame = market_data.fetch_interval("BTC-USDT", "5m", 0, 800 * step)

    assert len(frame) == 800
    assert frame["open_time"].iloc[0] == 0
    assert frame["open_time"].iloc[-1] == 799 * step
    assert len(requested_windows) == 2
    assert requested_windows[1][0] == 400 * step



def test_bingx_rate_limit_uses_long_cooldown_and_recovers(monkeypatch):
    import json
    from contextlib import contextmanager

    from scripts import backtest_addons as market_data

    responses = [
        {"code": 100410, "msg": "rate limit", "data": []},
        {"code": 100410, "msg": "rate limit", "data": []},
        {"code": 100410, "msg": "rate limit", "data": []},
        {"code": 0, "msg": "", "data": [{
            "openTime": 0, "open": "100", "high": "101", "low": "99",
            "close": "100", "volume": "10", "closeTime": 59_999,
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

    frame = market_data.fetch_interval("BTC-USDT", "1m", 0, 60_000)

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
