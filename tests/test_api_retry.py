import asyncio
import os
from pathlib import Path

os.environ.setdefault("BOT_TOKEN", "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
os.environ["DB_PATH"] = "/tmp/gold_bot_api_retry_test.db"
os.environ["BACKUP_DIR"] = "/tmp/gold_bot_api_retry_test_backups"

import main


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status = status
        self._payload = payload or {}
        self.headers = headers or {}

    async def text(self):
        return "temporary exchange response" if self.status != 200 else "{}"

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    closed = False

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, *args, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


def test_api_get_retries_http_429_then_returns_payload(monkeypatch):
    session = FakeSession([
        FakeResponse(429, headers={"Retry-After": "0"}),
        FakeResponse(200, payload={"code": 0, "data": {"ok": True}}),
    ])
    monkeypatch.setattr(main, "_last_api_request", 0.0)
    monkeypatch.setattr(main, "_api_cooldown_until", 0.0)
    monkeypatch.setattr(main, "system_status", {"api_status": "OK", "last_error": ""})

    result = asyncio.run(main.api_get(session, "/test", {}))

    assert result == {"code": 0, "data": {"ok": True}}
    assert session.calls == 2


def test_api_get_does_not_retry_non_retryable_http_status(monkeypatch):
    session = FakeSession([FakeResponse(400)])
    monkeypatch.setattr(main, "_last_api_request", 0.0)
    monkeypatch.setattr(main, "_api_cooldown_until", 0.0)
    monkeypatch.setattr(main, "system_status", {"api_status": "OK", "last_error": ""})

    try:
        asyncio.run(main.api_get(session, "/test", {}))
    except main.MarketError as exc:
        assert "HTTP 400" in str(exc)
    else:
        raise AssertionError("HTTP 400 must fail without retry")

    assert session.calls == 1



def test_api_get_success_clears_stale_error_and_records_last_success(monkeypatch):
    expected_success_time = "2026-11-26T20:00:00+00:00"
    monkeypatch.setattr(main, "_last_api_request", 0.0)
    monkeypatch.setattr(main, "_api_cooldown_until", 0.0)
    monkeypatch.setattr(main, "utc_text", lambda *args, **kwargs: expected_success_time)

    for payload in (
        {"code": 0, "data": {"ok": True}},
        ["ok", "raw-payload"],
    ):
        status = {
            "api_status": "Ошибка сети",
            "last_error": "stale previous failure",
            "api_last_success_at": None,
        }
        monkeypatch.setattr(main, "system_status", status)
        session = FakeSession([FakeResponse(200, payload=payload)])

        result = asyncio.run(main.api_get(session, "/test", {}))

        assert result == payload
        assert status["api_status"] == "OK"
        assert status["last_error"] is None
        assert status["api_last_success_at"] == expected_success_time
        assert session.calls == 1
