"""(e) /api/health raporteaza metadata fixture store + sync."""

from __future__ import annotations

import httpx

import db
import football_data as fd


async def test_health_reports_sync_metadata(no_http):
    # Importul se face aici, dupa ce fixture-ul autouse a setat env-ul izolat
    # (load_dotenv din main nu suprascrie variabilele deja setate).
    import main

    await db.init_db()
    day = fd.today_local().isoformat()
    now = fd.now_local().isoformat(timespec="seconds")
    await db.upsert_fixture(
        fd._parse_fixture({
            "fixture": {"id": 7, "date": f"{day}T20:00:00+03:00", "status": {"short": "NS"}},
            "league": {"id": 39, "name": "Premier League", "country": "England", "season": 2026},
            "teams": {"home": {"id": 1, "name": "A"}, "away": {"id": 2, "name": "B"}},
            "goals": {"home": None, "away": None},
        }),
        now,
    )
    await db.budget_add(day, 4)
    await db.add_sync_log(now, now, [day], 1, ok=True)

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["last_sync_at"] == now
    assert body["sync_ok"] is True
    assert body["api_requests_used_today"] == 4
    assert body["budget_limit"] == 50
    assert body["tracked_leagues_count"] == 12
    assert body["fixtures_in_db"] == 1
    assert body["timezone"] == "Europe/Bucharest"
    assert body["sync_enabled"] is False  # SYNC_ENABLED=false in teste
    assert "oddspapi_key_set" in body
    assert isinstance(body["oddspapi_key_set"], bool)


async def test_health_budget_limit_matches_guard_effective_limit(no_http, monkeypatch):
    """
    budget_limit din health == limita EFECTIVA a budget guard-ului (env).
    Dovedim ambele capete pe aceeasi valoare: health o raporteaza, iar
    guard-ul chiar blocheaza la ea (BudgetExhausted).
    """
    import pytest

    import main

    monkeypatch.setenv("MAX_DAILY_API_REQUESTS", "5")
    await db.init_db()

    # Guard-ul aplica limita 5: al 6-lea request e refuzat.
    day = fd.today_local().isoformat()
    await db.budget_add(day, 5)
    with pytest.raises(fd.BudgetExhausted):
        await fd._get("/odds", {"fixture": 1}, "odds")

    # Health raporteaza exact aceeasi limita, din aceeasi functie a guard-ului.
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        body = (await client.get("/api/health")).json()

    assert fd.max_daily_requests() == 5
    assert body["budget_limit"] == 5
    assert body["budget_limit"] == fd.max_daily_requests()
    assert body["api_requests_used_today"] == 5
