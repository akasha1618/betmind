"""
Diagnostic API-Football: esecurile trebuie sa fie DISTINGIBILE, nu o eticheta
generica de tip «rate limit».

Acoperim cele patru cazuri care arata identic in interfata azi:
  - 429 real (HTTP)
  - 403 (cheie/plan)
  - errors.rateLimit in corp, cu status 200  <-- cazul din productie
  - timeout / eroare de retea
plus bugetul zilnic local si «nu exista cote» (care NU e o eroare de API).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

import db
import football_data as fd


def _today() -> str:
    return fd.today_local().isoformat()


# ------------------------------------------------- clasificarea tipului de esec

async def test_rate_limit_in_body_with_http_200(fake_http):
    """Cazul perfid: API-ul raspunde 200, dar in corp scrie ca ai depasit
    limita PE MINUT. Fara clasificare, arata ca orice alta eroare."""
    await db.init_db()
    fake_http.errors = {"rateLimit": "Too many requests. Your rate limit is 10 requests per minute."}
    fake_http.headers = {"x-ratelimit-limit": "10", "x-ratelimit-remaining": "0"}

    with pytest.raises(fd.FootballDataError) as exc:
        await fd._get("/odds", {"fixture": 1}, "odds")

    assert "rate_limit_in_body" in str(exc.value)
    last = fd.last_api_error("/odds")
    assert last["kind"] == "rate_limit_in_body"
    assert last["status"] == 200
    assert "10 requests per minute" in last["body"]
    # Headerele de ritm sunt pastrate — asta separa limita pe minut de cea zilnica.
    assert last["headers"]["x-ratelimit-limit"] == "10"
    assert last["headers"]["x-ratelimit-remaining"] == "0"


async def test_body_message_field_and_alias_headers_are_captured(monkeypatch):
    """API-ul mai pune explicatia in `message`, iar unii proxy redenumesc
    header-ul in X-RateLimit-Remaining — ambele trebuie sa ajunga in log.
    Si: un errors.plan NU trebuie etichetat «rate limit»."""
    await db.init_db()

    async def _custom(endpoint, params, headers):
        return httpx.Response(
            200,
            json={"errors": {"plan": "upgrade"}, "message": "Endpoint not available on this plan"},
            headers={"X-RateLimit-Remaining": "0", "Retry-After": "8"},
        )

    monkeypatch.setattr(fd, "_http_get", _custom)

    with pytest.raises(fd.FootballDataError) as exc:
        await fd._get("/odds", {"fixture": 8}, "odds")

    assert "api_errors_in_body" in str(exc.value)
    assert "rate_limit" not in str(exc.value)
    last = fd.last_api_error("/odds")
    assert last["kind"] == "api_errors_in_body"
    assert "Endpoint not available on this plan" in last["body"]
    assert last["headers"].get("x-ratelimit-remaining") == "0"
    assert last["headers"].get("retry-after") == "8"


async def test_http_429_is_distinct_from_body_rate_limit(fake_http):
    await db.init_db()
    fake_http.status_code = 429
    fake_http.text_body = "Too Many Requests"
    fake_http.headers = {"retry-after": "30"}

    with pytest.raises(fd.FootballDataError) as exc:
        await fd._get("/odds", {"fixture": 2}, "odds")

    assert "http_429_rate_limit" in str(exc.value)
    last = fd.last_api_error("/odds")
    assert last["kind"] == "http_429_rate_limit"
    assert last["status"] == 429
    assert last["headers"]["retry-after"] == "30"


async def test_http_403_is_distinct(fake_http):
    await db.init_db()
    fake_http.status_code = 403
    fake_http.text_body = "Forbidden"

    with pytest.raises(fd.FootballDataError):
        await fd._get("/odds", {"fixture": 3}, "odds")

    assert fd.last_api_error("/odds")["kind"] == "http_403_forbidden"


async def test_timeout_and_network_are_distinct(monkeypatch):
    await db.init_db()

    async def _timeout(endpoint, params, headers):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(fd, "_http_get", _timeout)
    with pytest.raises(fd.FootballDataError):
        await fd._get("/odds", {"fixture": 4}, "odds")
    assert fd.last_api_error("/odds")["kind"] == "timeout"

    async def _broken(endpoint, params, headers):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(fd, "_http_get", _broken)
    with pytest.raises(fd.FootballDataError):
        await fd._get("/odds", {"fixture": 5}, "odds")
    assert fd.last_api_error("/odds")["kind"] == "network_error"


async def test_budget_exhausted_is_not_a_rate_limit(fake_http, monkeypatch):
    """Bugetul zilnic LOCAL nu are legatura cu limita API-ului — nu trebuie
    confundate cand cautam cauza."""
    monkeypatch.setenv("MAX_DAILY_API_REQUESTS", "3")
    await db.init_db()
    await db.budget_add(_today(), 3)

    with pytest.raises(fd.BudgetExhausted):
        await fd._get("/odds", {"fixture": 6}, "odds")

    last = fd.last_api_error("/odds")
    assert last["kind"] == "budget_exhausted"
    assert last["status"] is None
    assert not fake_http.calls  # nici macar n-a plecat cererea


async def test_missing_odds_is_not_an_error_kind(fake_http):
    """«Nu exista cote publicate» e o stare normala, nu o limitare."""
    await db.init_db()
    fake_http.response_payload = []

    out = await fd.get_odds(999)

    assert "error" in out
    assert fd.last_api_error("/odds")["kind"] == "no_odds_data"


# ----------------------------------------------------- gruparea si raportarea

async def test_rate_headers_captured_on_success(fake_http):
    """Limita PE MINUT trebuie vazuta si cand totul merge — altfel afli ca
    esti aproape de plafon abia dupa ce incep esecurile."""
    await db.init_db()
    fake_http.headers = {"x-ratelimit-limit": "300", "x-ratelimit-remaining": "12",
                         "x-ratelimit-requests-limit": "7500",
                         "x-ratelimit-requests-remaining": "7000"}

    await fd._get("/odds", {"fixture": 42}, "odds")

    seen = fd.last_rate_headers()
    assert seen["x-ratelimit-limit"] == "300"     # cota pe minut
    assert seen["x-ratelimit-remaining"] == "12"  # cat a mai ramas in minut
    assert seen["x-ratelimit-requests-limit"] == "7500"  # cota zilnica
    assert seen["at"]


async def test_errors_grouped_by_kind(fake_http):
    await db.init_db()
    fake_http.errors = {"rateLimit": "Too many requests"}
    for i in range(3):
        with pytest.raises(fd.FootballDataError):
            await fd._get("/odds", {"fixture": 100 + i}, "odds")

    fake_http.errors = []
    fake_http.status_code = 403
    fake_http.text_body = "Forbidden"
    with pytest.raises(fd.FootballDataError):
        await fd._get("/odds", {"fixture": 200}, "odds")

    grouped = fd.api_errors_grouped("/odds", hours=1)
    assert grouped == {"rate_limit_in_body": 3, "http_403_forbidden": 1}
    # Filtrarea pe endpoint chiar filtreaza.
    assert fd.api_errors_grouped("/fixtures", hours=1) == {}


async def test_health_exposes_odds_diagnostics(fake_http, monkeypatch):
    import main

    await db.init_db()
    fake_http.errors = {"rateLimit": "Too many requests. Your rate limit is 10 requests per minute."}
    with pytest.raises(fd.FootballDataError):
        await fd._get("/odds", {"fixture": 7}, "odds")

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        body = (await client.get("/api/health")).json()

    assert body["odds_errors_last_hour"] == {"rate_limit_in_body": 1}
    assert body["last_odds_error"]["status"] == 200
    assert "requests per minute" in body["last_odds_error"]["body"]
    assert body["last_odds_error"]["at"]
    assert "api_requests_used_today" in body
    assert body["rate_limiter_active"] is True
    assert body["rate_limit_per_minute"] == 10000  # isolated_env

    monkeypatch.setenv("API_RATE_LIMIT_PER_MINUTE", "300")
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        body = (await client.get("/api/health")).json()
    assert body["rate_limit_per_minute"] == 300
    assert body["rate_limiter_active"] is True


# ------------------------------------------------- statistici per tura (dev UI)

async def test_turn_stats_count_calls_failures_and_duration(fake_http):
    await db.init_db()
    fd.set_current_turn("tura-1")

    fake_http.response_payload = [{"bookmakers": []}]
    await fd._get("/fixtures", {"date": _today()}, "fixtures")
    await fd._get("/fixtures", {"date": _today()}, "fixtures")  # cache hit

    fake_http.errors = {"rateLimit": "Too many requests"}
    with pytest.raises(fd.FootballDataError):
        await fd._get("/odds", {"fixture": 11}, "odds")

    stats = fd.turn_api_stats("tura-1")
    assert stats["api_calls"] == 1 + fd.api_http_attempts()  # fixtures + 3 retry-uri /odds
    assert stats["cache_hits"] == 1
    assert stats["failures"] == 1
    assert stats["by_kind"] == {"rate_limit_in_body": 1}
    assert stats["by_status"] == {"200": 1}
    assert stats["duration_s"] >= 0
    assert stats["endpoints"]["/odds"] == fd.api_http_attempts()

    assert fd.turn_api_stats("tura-inexistenta")["api_calls"] == 0


async def test_turn_stats_follow_parallel_tasks(fake_http):
    """Analistii ruleaza in task-uri separate — contorizarea trebuie sa-i
    urmareasca (contextvars se copiaza la crearea task-ului)."""
    await db.init_db()
    fd.set_current_turn("tura-paralela")
    fake_http.errors = {"rateLimit": "Too many requests"}

    async def one(fixture_id: int):
        with pytest.raises(fd.FootballDataError):
            await fd._get("/odds", {"fixture": fixture_id}, "odds")

    await asyncio.gather(*(one(i) for i in range(4)))

    stats = fd.turn_api_stats("tura-paralela")
    assert stats["api_calls"] == 4 * fd.api_http_attempts()
    assert stats["failures"] == 4
    assert stats["by_kind"]["rate_limit_in_body"] == 4


async def test_usage_endpoint_includes_api_stats(fake_http):
    import main

    await db.init_db()
    fd.set_current_turn("tura-usage")
    fake_http.errors = {"rateLimit": "Too many requests"}
    with pytest.raises(fd.FootballDataError):
        await fd._get("/odds", {"fixture": 12}, "odds")

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        body = (await client.get("/api/usage/tura-usage")).json()

    assert body["api"]["failures"] == 1
    assert body["api"]["by_kind"] == {"rate_limit_in_body": 1}
    assert body["api"]["api_calls"] == fd.api_http_attempts()


async def test_failure_is_logged_with_body_and_headers(fake_http, caplog):
    """Logul din productie trebuie sa contina raspunsul brut, nu doar «rate limit»."""
    await db.init_db()
    fake_http.errors = {"rateLimit": "Too many requests. Your rate limit is 10 requests per minute."}
    fake_http.headers = {"x-ratelimit-remaining": "0", "retry-after": "12"}

    with caplog.at_level("WARNING", logger="betmind.football"):
        with pytest.raises(fd.FootballDataError):
            await fd._get("/odds", {"fixture": 13}, "odds")

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "API-Football FAIL" in logged
    assert "endpoint=/odds" in logged
    assert "kind=rate_limit_in_body" in logged
    assert "status=200" in logged
    assert "10 requests per minute" in logged
    assert "retry-after" in logged
