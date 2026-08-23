"""
Teste de acceptanta V1-A (Fixture Store & Background Sync), HTTP mock-uit:
(a) NS→PST upsert creeaza exact un rand in fixture_changes
(b) get_fixtures in fereastra sincronizata = zero HTTP, source local_db
(c) buget epuizat -> BudgetExhausted, dar DB-ul raspunde in continuare
(d) schimbarea de kickoff e logata (reprogramare)
(e) /api/health raporteaza metadata de sync
+ sync, track_league, budget cross-check, status_group
"""

from __future__ import annotations

from datetime import timedelta

import pytest

import db
import football_data as fd
import sync
from tests.conftest import raw_fixture


def _parsed(**kw) -> dict:
    return fd._parse_fixture(raw_fixture(**kw))


def _today() -> str:
    return fd.today_local().isoformat()


def _now_iso() -> str:
    return fd.now_local().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# (a) NS -> PST = exact un rand de schimbare
# ---------------------------------------------------------------------------

async def test_ns_to_pst_creates_exactly_one_change_row():
    await db.init_db()
    fx = _parsed(status="NS")
    changes = await db.upsert_fixture(fx, _now_iso())
    assert changes == []  # primul insert nu e o schimbare

    fx_pst = _parsed(status="PST")
    changes = await db.upsert_fixture(fx_pst, _now_iso())
    assert [(c[0], c[1], c[2]) for c in changes] == [("status", "NS", "PST")]

    rows = await db.get_changes("2000-01-01", "2100-01-01")
    assert len(rows) == 1
    assert rows[0]["field"] == "status"
    assert rows[0]["old_value"] == "NS"
    assert rows[0]["new_value"] == "PST"

    # Re-upsert identic: nicio schimbare noua.
    await db.upsert_fixture(fx_pst, _now_iso())
    rows = await db.get_changes("2000-01-01", "2100-01-01")
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# (d) schimbarea de kickoff e logata ca reprogramare
# ---------------------------------------------------------------------------

async def test_kickoff_change_logged_and_reported():
    await db.init_db()
    day = _today()
    fx = _parsed(kickoff=f"{day}T19:30:00+03:00")
    await db.upsert_fixture(fx, _now_iso())

    fx2 = _parsed(kickoff=f"{day}T21:00:00+03:00")
    changes = await db.upsert_fixture(fx2, _now_iso())
    changed_fields = {c[0] for c in changes}
    assert "kickoff_iso" in changed_fields
    assert "time_local" in changed_fields

    report = await fd.get_fixture_changes()
    assert report["count"] >= 1
    kick = next(c for c in report["changes"] if c["field"] == "kickoff_iso")
    assert kick["meaning"] == "rescheduled (reprogramat)"
    assert kick["match"] == "NEC–Excelsior"
    assert "NEC–Excelsior" in kick["summary"]


# ---------------------------------------------------------------------------
# (b) in fereastra: zero HTTP, source local_db
# ---------------------------------------------------------------------------

async def test_in_window_get_fixtures_zero_http(no_http):
    await db.init_db()
    day = _today()
    fx = _parsed(kickoff=f"{day}T19:30:00+03:00")
    await db.upsert_fixture(fx, _now_iso())
    await db.mark_day_synced(day, _now_iso())

    res = await fd.get_fixtures(day)  # no_http ar pica testul la orice request
    assert res["source"] == "local_db"
    assert res["count"] == 1
    assert res["days"][day]["source"] == "local_db"
    assert res["days"][day]["last_synced_at"] is not None
    assert res["days"][day]["stale"] is False
    assert res["matches_per_day"] == {day: 1}

    f = res["fixtures"][0]
    assert f["date"] == day
    assert f["time"] == "19:30"
    assert f["status_group"] == "upcoming"
    assert f["home"]["name"] == "NEC"


async def test_unsynced_day_goes_live_then_serves_from_db(fake_http):
    await db.init_db()
    day = _today()
    fake_http.response_payload = [raw_fixture(kickoff=f"{day}T19:30:00+03:00")]

    res = await fd.get_fixtures(day)
    assert res["source"] == "live_api"
    assert len(fake_http.calls) == 1
    endpoint, params = fake_http.calls[0]
    assert endpoint == "/fixtures"
    assert params["timezone"] == "Europe/Bucharest"

    # A doua cerere: ziua e acum sincronizata -> local_db, fara alt HTTP.
    fd._cache.clear()  # eliminam si cache-ul in-memory ca sa dovedim DB-ul
    res2 = await fd.get_fixtures(day)
    assert res2["source"] == "local_db"
    assert res2["count"] == 1
    assert len(fake_http.calls) == 1


# ---------------------------------------------------------------------------
# (c) buget epuizat -> BudgetExhausted, DB-ul inca raspunde
# ---------------------------------------------------------------------------

async def test_budget_exhausted_raises_and_db_still_answers(no_http, monkeypatch):
    monkeypatch.setenv("MAX_DAILY_API_REQUESTS", "5")
    await db.init_db()
    day = _today()

    # Meciul e in DB, dar ziua NU e marcata sincronizata -> s-ar incerca live.
    await db.upsert_fixture(_parsed(kickoff=f"{day}T19:30:00+03:00"), _now_iso())
    await db.budget_add(day, 5)  # limita atinsa

    with pytest.raises(fd.BudgetExhausted):
        await fd._get("/odds", {"fixture": 1}, "odds")

    res = await fd.get_fixtures(day)
    assert res["budget_exhausted"] is True
    assert res["source"] == "local_db"
    assert res["count"] == 1  # DB-ul raspunde onest, cu date posibil vechi
    assert res["days"][day]["stale"] is True


async def test_cache_hits_do_not_consume_budget(fake_http):
    await db.init_db()
    day = _today()
    fake_http.response_payload = [raw_fixture(kickoff=f"{day}T19:30:00+03:00")]

    await fd._get("/fixtures", {"date": day, "timezone": "Europe/Bucharest"}, "fixtures")
    used_after_first = await db.budget_get(day)
    await fd._get("/fixtures", {"date": day, "timezone": "Europe/Bucharest"}, "fixtures")
    assert await db.budget_get(day) == used_after_first  # cache hit = gratis
    assert len(fake_http.calls) == 1


async def test_budget_cross_check_with_rate_limit_headers(fake_http):
    await db.init_db()
    day = _today()
    fake_http.response_payload = []
    fake_http.headers = {"x-ratelimit-requests-limit": "7500",
                         "x-ratelimit-requests-remaining": "7000"}

    await fd._get("/fixtures", {"date": day, "timezone": "Europe/Bucharest"}, "fixtures")
    # API-ul zice ca s-au folosit 500 -> contorul local urca la cel putin 500.
    assert await db.budget_get(day) >= 500


# ---------------------------------------------------------------------------
# Sync: detectie de schimbari + filtrare pe ligi urmarite + prioritate hot
# ---------------------------------------------------------------------------

async def test_sync_day_detects_postponement_and_filters_untracked(fake_http):
    await db.init_db()
    day = _today()
    fake_http.response_payload = [
        raw_fixture(fixture_id=1, kickoff=f"{day}T19:30:00+03:00", status="NS"),
        raw_fixture(fixture_id=2, league_id=99999, kickoff=f"{day}T20:00:00+03:00"),  # neurmarita
    ]
    n = await sync.sync_day(day)
    assert n == 0
    assert await db.count_fixtures() == 1  # liga neurmarita nu intra in store

    # A doua sincronizare: meciul e amanat.
    fd._cache.clear()
    fake_http.response_payload = [
        raw_fixture(fixture_id=1, kickoff=f"{day}T19:30:00+03:00", status="PST"),
    ]
    n = await sync.sync_day(day)
    assert n == 1
    report = await fd.get_fixture_changes()
    assert report["changes"][0]["meaning"] == "postponed (amanat)"


async def test_run_sync_cycle_syncs_window_and_logs(fake_http):
    await db.init_db()
    fake_http.response_payload = []
    result = await sync.run_sync_cycle(force=True)

    # 7 zile trecut + azi + maine + 14 viitor = 22 de zile, un request per zi.
    assert len(result["due"]) == 22
    assert result["synced"] == result["due"]
    assert result["ok"] is True
    assert len(fake_http.calls) == 22

    last = await db.latest_sync()
    assert last is not None and bool(last["ok"]) is True
    assert last["api_requests_used"] == 22

    # Hot are prioritate: azi si maine sunt primele sincronizate.
    today = _today()
    tomorrow = (fd.today_local() + timedelta(days=1)).isoformat()
    assert result["synced"][:2] == [today, tomorrow]


async def test_sync_cycle_stops_honestly_on_budget(fake_http, monkeypatch):
    monkeypatch.setenv("MAX_DAILY_API_REQUESTS", "3")
    await db.init_db()
    fake_http.response_payload = []
    result = await sync.run_sync_cycle(force=True)

    assert len(result["synced"]) == 3  # doar cat a permis bugetul, hot intai
    assert result["error"] is not None
    last = await db.latest_sync()
    assert last["api_requests_used"] == 3


# ---------------------------------------------------------------------------
# track_league
# ---------------------------------------------------------------------------

async def test_track_league_resolves_and_persists(fake_http):
    await db.init_db()
    fake_http.response_payload = [{
        "league": {"id": 185, "name": "Cupa României", "type": "Cup"},
        "country": {"name": "Romania"},
        "seasons": [{"year": 2026, "current": True}],
    }]
    res = await fd.track_league("Cupa Romaniei")
    assert res["tracked"] is True
    assert res["league_id"] == 185

    tracked = await db.get_tracked_leagues()
    assert 185 in tracked
    assert await db.count_tracked_leagues() == 13  # 12 implicite + 1 noua


async def test_track_league_ambiguous_returns_candidates(fake_http):
    await db.init_db()
    fake_http.response_payload = [
        {"league": {"id": 529, "name": "Super Cup", "type": "Cup"},
         "country": {"name": "Germany"}, "seasons": []},
        {"league": {"id": 531, "name": "Super Cup", "type": "Cup"},
         "country": {"name": "Spain"}, "seasons": []},
    ]
    res = await fd.track_league("Super Cup")
    assert "multiple_matches" in res
    assert len(res["multiple_matches"]) == 2
    assert await db.count_tracked_leagues() == 12  # nimic adaugat inca


# ---------------------------------------------------------------------------
# status_group
# ---------------------------------------------------------------------------

def test_status_group_mapping():
    assert fd.status_group("NS") == "upcoming"
    assert fd.status_group("TBD") == "upcoming"
    assert fd.status_group("1H") == "live"
    assert fd.status_group("HT") == "live"
    assert fd.status_group("FT") == "finished"
    assert fd.status_group("AET") == "finished"
    assert fd.status_group("PST") == "other"
    assert fd.status_group("CANC") == "other"
