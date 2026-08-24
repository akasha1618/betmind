"""
Volum API: prefetch pe liga (standings + injuries o data) + single-flight.

Un bilet cu 8 meciuri facea 82 apeluri, din care standings/injuries duplicate.
Acum: o pereche per liga, iar apelurile identice in zbor se coalesc.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx

import analysts
import db
import football_data as fd
from tests.conftest import raw_fixture
from tests.test_v1b import _fake_usage, _today, _valid_analysis_json


def _now_iso() -> str:
    return fd.now_local().isoformat(timespec="seconds")


async def _seed(fid: int, league_id: int, home: tuple[int, str], away: tuple[int, str]):
    parsed = fd._parse_fixture(raw_fixture(
        fixture_id=fid, league_id=league_id,
        kickoff=f"{_today()}T19:30:00+03:00", status="NS",
        home=home, away=away,
    ))
    await db.upsert_fixture(parsed, _now_iso())


def _count(calls: list[tuple[str, dict]]) -> Counter:
    return Counter(ep for ep, _ in calls)


# ---------------------------------------------------------------------------
# (obligatoriu) shortlist cu mai multe meciuri din aceeasi liga
# ---------------------------------------------------------------------------

async def test_standings_and_injuries_once_per_league(fake_http, monkeypatch):
    monkeypatch.setenv("MAX_DAILY_API_REQUESTS", "200")
    await db.init_db()
    # 3 meciuri Serie A (135) + 1 Premier League (39) → 2 ligi.
    await _seed(1, 135, (500, "Bologna"), (487, "Lazio"))
    await _seed(2, 135, (497, "Roma"), (502, "Fiorentina"))
    await _seed(3, 135, (488, "Como"), (489, "Inter"))
    await _seed(4, 39, (36, "Fulham"), (49, "Chelsea"))

    async def fake_llm(system, user):
        fid = __import__("json").loads(user)["fixture"]["fixture_id"]
        return _valid_analysis_json(fid), _fake_usage()

    monkeypatch.setattr(analysts, "_call_analyst_llm", fake_llm)
    await analysts.analyze_matches([1, 2, 3, 4])

    standings = [p for e, p in fake_http.calls if e == "/standings"]
    injuries = [p for e, p in fake_http.calls if e == "/injuries"]

    assert len(standings) == 2
    assert {p["league"] for p in standings} == {135, 39}
    assert len(injuries) == 2
    assert {p["league"] for p in injuries} == {135, 39}
    assert all("team" not in p for p in injuries)


async def test_single_flight_coalesces_identical_in_flight_calls(monkeypatch):
    await db.init_db()
    hits = []

    async def slow_http(endpoint, params, headers):
        hits.append((endpoint, dict(params)))
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"errors": [], "response": []})

    monkeypatch.setattr(fd, "_http_get", slow_http)

    a, b, c = await asyncio.gather(
        fd.get_standings(39, 2026),
        fd.get_standings(39, 2026),
        fd.get_standings(39, 2026),
    )
    assert hits == [("/standings", {"league": 39, "season": 2026})]
    assert a == b == c == []


async def test_league_injuries_slice_matches_team_shape(fake_http):
    """Pachetul per echipa din /injuries?league= are aceleasi campuri ca get_injuries."""
    await db.init_db()
    recent = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT12:00:00+00:00")
    fake_http.response_payload = [{
        "player": {"name": "Calero", "type": "Missing Fixture", "reason": "Knee Injury"},
        "team": {"id": 2581, "name": "Otelul"},
        "fixture": {"date": recent},
    }, {
        "player": {"name": "Altcineva", "type": "Questionable", "reason": "Ankle"},
        "team": {"id": 999, "name": "Alta"},
        "fixture": {"date": recent},
    }]

    by_team = await fd.get_league_injuries_by_team(283, 2026)
    otelul = by_team[2581]
    assert otelul["count"] == 1
    assert otelul["injuries"][0]["player"] == "Calero"
    assert otelul["injuries"][0]["reason"] == "Knee Injury"
    assert "note" in otelul
    assert 999 in by_team
    assert 2581 not in (by_team[999].get("injuries") or [])


async def test_eight_match_ticket_call_volume(fake_http, monkeypatch):
    """Masuratoare: 8 meciuri (4 ligi) fata de 82 apeluri inainte."""
    monkeypatch.setenv("MAX_DAILY_API_REQUESTS", "200")
    await db.init_db()
    # 4 Serie A, 2 Liga I, 1 PL, 1 La Liga — 16 echipe distincte.
    seeds = [
        (1, 135, (500, "Bologna"), (487, "Lazio")),
        (2, 135, (497, "Roma"), (502, "Fiorentina")),
        (3, 135, (488, "Como"), (489, "Inter")),
        (4, 135, (490, "Milan"), (491, "Napoli")),
        (5, 283, (2581, "Otelul"), (2592, "Arges")),
        (6, 283, (6886, "Botosani"), (6230, "Csikszereda")),
        (7, 39, (36, "Fulham"), (49, "Chelsea")),
        (8, 140, (727, "Osasuna"), (726, "Levante")),
    ]
    for row in seeds:
        await _seed(*row)

    async def fake_llm(system, user):
        fid = __import__("json").loads(user)["fixture"]["fixture_id"]
        return _valid_analysis_json(fid), _fake_usage()

    monkeypatch.setattr(analysts, "_call_analyst_llm", fake_llm)
    await analysts.analyze_matches([1, 2, 3, 4, 5, 6, 7, 8])

    by_ep = _count(fake_http.calls)
    assert by_ep["/standings"] == 4
    assert by_ep["/injuries"] == 4
    assert by_ep["/fixtures"] == 16          # last matches, 2 per meci
    assert by_ep["/teams/statistics"] == 16
    assert by_ep["/fixtures/headtohead"] == 8
    assert by_ep["/odds"] == 8
    assert by_ep["/predictions"] == 8
    total = sum(by_ep.values())
    assert total == 64, f"apeluri pe endpoint: {dict(by_ep)}"
    # Niciun injuries pe echipa.
    assert all("team" not in p for e, p in fake_http.calls if e == "/injuries")
