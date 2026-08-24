"""Ritm API-Football: concurenta, token bucket, retry pe 429/rateLimit, prioritate /odds."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

import db
import football_data as fd


def _ok() -> httpx.Response:
    return httpx.Response(200, json={"errors": [], "response": []})


def test_rate_limit_defaults(monkeypatch):
    monkeypatch.delenv("API_RATE_LIMIT_PER_MINUTE", raising=False)
    monkeypatch.delenv("API_FOOTBALL_RATE_LIMIT_PER_MINUTE", raising=False)
    monkeypatch.delenv("API_MAX_CONCURRENT", raising=False)
    assert fd.rate_limit_per_minute() == 240
    assert fd.api_max_concurrent() == 8
    assert fd.rate_limiter_active() is True
    assert fd.api_http_attempts() == 3


async def test_http_concurrent_cap(monkeypatch):
    monkeypatch.setenv("API_MAX_CONCURRENT", "2")
    monkeypatch.setenv("API_RATE_LIMIT_PER_MINUTE", "10000")
    fd.reset_http_gate()
    await db.init_db()
    current = {"n": 0, "max": 0}

    async def slow(endpoint, params, headers):
        current["n"] += 1
        current["max"] = max(current["max"], current["n"])
        await asyncio.sleep(0.04)
        current["n"] -= 1
        return _ok()

    monkeypatch.setattr(fd, "_http_get", slow)
    await asyncio.gather(*[
        fd._get("/fixtures", {"team": i, "last": 6}, "last_matches")
        for i in range(6)
    ])
    assert current["max"] == 2


async def test_token_bucket_spaces_request_starts(monkeypatch):
    monkeypatch.setenv("API_MAX_CONCURRENT", "2")
    monkeypatch.setenv("API_RATE_LIMIT_PER_MINUTE", "60")  # 1 token/s, burst 2
    fd.reset_http_gate()
    await db.init_db()
    starts: list[float] = []

    async def mark(endpoint, params, headers):
        starts.append(time.monotonic())
        return _ok()

    monkeypatch.setattr(fd, "_http_get", mark)
    await asyncio.gather(*[
        fd._get("/fixtures", {"team": i, "last": 6}, "last_matches")
        for i in range(5)
    ])
    assert max(starts) - min(starts) >= 2.0


async def test_odds_jumps_the_waiter_queue(monkeypatch):
    monkeypatch.setenv("API_MAX_CONCURRENT", "1")
    monkeypatch.setenv("API_RATE_LIMIT_PER_MINUTE", "10000")
    fd.reset_http_gate()
    await db.init_db()
    order: list[str] = []

    async def slow(endpoint, params, headers):
        order.append(endpoint)
        await asyncio.sleep(0.04)
        return _ok()

    monkeypatch.setattr(fd, "_http_get", slow)

    async def blocker():
        await fd._get("/standings", {"league": 1, "season": 2026}, "standings")

    async def queued():
        await asyncio.sleep(0.015)
        odds_task = asyncio.create_task(fd._get("/odds", {"fixture": 99}, "odds"))
        await asyncio.sleep(0.01)  # /odds e deja la coada
        await asyncio.gather(
            fd._get("/fixtures", {"team": 1, "last": 6}, "last_matches"),
            fd._get("/fixtures", {"team": 2, "last": 6}, "last_matches"),
            odds_task,
        )

    await asyncio.gather(blocker(), queued())
    assert order[0] == "/standings"
    odds_i = order.index("/odds")
    fixture_i = [i for i, e in enumerate(order) if e == "/fixtures"]
    assert fixture_i and odds_i < min(fixture_i)


async def test_rate_limit_retries_three_then_fails(fake_http):
    await db.init_db()
    fake_http.errors = {"rateLimit": "Too many requests"}
    with pytest.raises(fd.FootballDataError) as exc:
        await fd._get("/odds", {"fixture": 1}, "odds")
    assert "rate_limit_in_body" in str(exc.value)
    assert len(fake_http.calls) == 3
    assert fd.api_errors_grouped("/odds") == {"rate_limit_in_body": 1}


async def test_http_429_retries_then_fails(fake_http):
    await db.init_db()
    fake_http.status_code = 429
    fake_http.text_body = "Too Many Requests"
    with pytest.raises(fd.FootballDataError) as exc:
        await fd._get("/odds", {"fixture": 2}, "odds")
    assert "http_429_rate_limit" in str(exc.value)
    assert len(fake_http.calls) == 3


async def test_rate_limit_retry_then_success(monkeypatch):
    await db.init_db()
    n = {"i": 0}

    async def flaky(endpoint, params, headers):
        n["i"] += 1
        if n["i"] < 2:
            return httpx.Response(
                200, json={"errors": {"rateLimit": "Too many"}, "response": []})
        return httpx.Response(200, json={"errors": [], "response": [{"ok": True}]})

    monkeypatch.setattr(fd, "_http_get", flaky)
    out = await fd._get("/odds", {"fixture": 5}, "odds")
    assert out == [{"ok": True}]
    assert n["i"] == 2
    assert fd.last_api_error("/odds") is None


async def test_403_is_not_retried(fake_http):
    await db.init_db()
    fake_http.status_code = 403
    fake_http.text_body = "Forbidden"
    with pytest.raises(fd.FootballDataError):
        await fd._get("/odds", {"fixture": 3}, "odds")
    assert len(fake_http.calls) == 1
    assert fd.last_api_error("/odds")["kind"] == "http_403_forbidden"


async def test_assemble_fetches_odds_before_other_endpoints(fake_http):
    """Fara cote meciul e pierdut pentru bilet — /odds pleaca primul."""
    import analysts
    from tests.conftest import raw_fixture
    from tests.test_v1b import _now_iso, _today

    await db.init_db()
    parsed = fd._parse_fixture(raw_fixture(
        fixture_id=1, league_id=135,
        kickoff=f"{_today()}T19:30:00+03:00", status="NS",
        home=(500, "Bologna"), away=(487, "Lazio"),
    ))
    await db.upsert_fixture(parsed, _now_iso())
    await analysts.assemble_data_pack(1)
    assert fake_http.calls, "niciun apel HTTP"
    assert fake_http.calls[0][0] == "/odds"
