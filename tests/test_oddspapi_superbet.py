"""Cote Superbet RO prin OddsPapi: sens din /markets, sanitate, potrivire
meciuri, rate-limit, link .ro si fallback tacut pe API-Football.

Acopera cerintele a-d:
  (a) fara ODDSPAPI_KEY -> modul inert, zero HTTP, comportament identic;
  (b) potrivire sigura + sens confirmat + suma normala -> cota Superbet
      inlocuieste cota API-Football si linkul are domeniul .ro;
  (c) potrivire incerta / sens neconfirmat / suma anormala -> fallback tacut;
  (d) timeout/eroare OddsPapi nu blocheaza pachetul de date.
"""

from __future__ import annotations

import asyncio
import copy
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import analysts
import db
import oddspapi_data as op

# ---------------------------------------------------------------------------
# Payload-uri OddsPapi in formatul validat manual (v4)
# ---------------------------------------------------------------------------

MARKETS_PAYLOAD = [
    {"marketId": 1, "marketName": "1X2", "outcomes": [
        {"outcomeId": "uuid-1", "outcomeName": "1"},
        {"outcomeId": "uuid-x", "outcomeName": "X"},
        {"outcomeId": "uuid-2", "outcomeName": "2"},
    ]},
    {"marketId": 5, "marketName": "Both teams to score", "outcomes": [
        {"outcomeId": "uuid-gg", "outcomeName": "Yes"},
        {"outcomeId": "uuid-ng", "outcomeName": "No"},
    ]},
]

FIXTURE_OPP = {
    "fixtureId": "opp-9",
    "participant1Name": "FC Rapid București",
    "participant2Name": "Universitatea Craiova",
    "startTime": "2026-08-22T16:30:00Z",  # 19:30 ora Romaniei
    "tournamentName": "Superliga",
    "categoryName": "Romania",
}

ODDS_PAYLOAD = {
    "bookmakerOdds": {
        "superbet.ro": {
            "fixturePath": "https://superbet.com/pariuri/fotbal/rapid-craiova/123",
            "markets": {
                "1": {"outcomes": {
                    "uuid-1": {"players": {"p": {"price": 2.05}}},
                    "uuid-x": {"players": {"p": {"price": 3.40}}},
                    "uuid-2": {"players": {"p": {"price": 3.60}}},
                }},
                "5": {"outcomes": {
                    "uuid-gg": {"players": {"p": {"price": 1.73}}},
                    "uuid-ng": {"players": {"p": {"price": 2.05}}},
                }},
            },
        }
    }
}

# Fixture API-Football (forma din tabela `fixtures`).
FX = {
    "fixture_id": 1001,
    "home_name": "Rapid București",
    "away_name": "CS Universitatea Craiova",
    "kickoff_iso": "2026-08-22T19:30:00+03:00",
    "date_local": "2026-08-22",
}


def af_odds_pack() -> dict:
    """Pachet de cote API-Football minimal, cum il produce aggregate_odds."""
    def out(value, odd):
        return {"value": value, "avg_odd": odd, "best_odd": odd + 0.05,
                "n_books": 4, "reference_odd": odd, "display_odd": odd,
                "display_bookmaker": "Bet365",
                "odds_label": f"{odd:.2f} (Bet365)"}
    return {
        "markets": [
            {"key": "1x2", "name": "Match Winner",
             "outcomes": [out("Home", 2.00), out("Draw", 3.30), out("Away", 3.50)]},
            {"key": "btts", "name": "Both Teams Score",
             "outcomes": [out("Yes", 1.70), out("No", 2.10)]},
        ],
        "1X2": {"Home": 2.00, "Draw": 3.30, "Away": 3.50},
        "btts": {"Yes": 1.70, "No": 2.10},
    }


class FakeOddsPapiHTTP:
    """Inlocuitor pentru op._http_get: raspunsuri programate per endpoint."""

    def __init__(self):
        self.markets = MARKETS_PAYLOAD
        self.fixtures = [FIXTURE_OPP]
        self.odds = copy.deepcopy(ODDS_PAYLOAD)
        self.calls: list[tuple[str, dict]] = []
        self.fail_endpoints: set[str] = set()
        self.latency_s = 0.0

    async def __call__(self, url: str, params: dict) -> httpx.Response:
        endpoint = url.rsplit("/", 1)[-1]
        self.calls.append((endpoint, dict(params)))
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        if endpoint in self.fail_endpoints:
            raise httpx.ConnectTimeout("timeout simulat")
        payload = {"markets": self.markets, "fixtures": self.fixtures,
                   "odds": self.odds}.get(endpoint)
        if payload is None:
            return httpx.Response(404, text="necunoscut")
        return httpx.Response(200, json=payload)

    def n_calls(self, endpoint: str) -> int:
        return sum(1 for e, _ in self.calls if e == endpoint)


@pytest.fixture
def oddspapi(monkeypatch) -> FakeOddsPapiHTTP:
    fake = FakeOddsPapiHTTP()
    monkeypatch.setenv("ODDSPAPI_KEY", "cheie-test")
    monkeypatch.setattr(op, "_http_get", fake)
    op.reset_runtime_state()
    return fake


# ---------------------------------------------------------------------------
# (a) Fara cheie: modul complet inert, zero HTTP
# ---------------------------------------------------------------------------

async def test_fara_cheie_modulul_e_inert(monkeypatch):
    async def boom(url, params):
        raise AssertionError(f"HTTP OddsPapi neasteptat: {url}")
    monkeypatch.setattr(op, "_http_get", boom)
    assert not op.enabled()
    assert await op.superbet_for_fixture(FX) is None


async def test_overlay_fara_pachet_superbet_nu_schimba_nimic():
    pack = af_odds_pack()
    snapshot = copy.deepcopy(pack)
    assert op.overlay_on_odds_pack(pack, None) == 0
    assert pack == snapshot


# ---------------------------------------------------------------------------
# (b) Flux complet: cota Superbet inlocuieste cota API-Football + link .ro
# ---------------------------------------------------------------------------

async def test_flux_complet_cota_superbet_si_link_ro(oddspapi):
    sb = await op.superbet_for_fixture(FX)
    assert sb is not None
    assert sb["odds"][("1x2", "Home")] == 2.05
    assert sb["odds"][("btts", "Yes")] == 1.73
    assert sb["bookmaker_name"] == "Superbet"
    assert sb["link"].startswith("https://superbet.ro/")
    assert "superbet.com" not in sb["link"]
    assert sb["link"].endswith("/pariuri/fotbal/rapid-craiova/123")

    pack = af_odds_pack()
    replaced = op.overlay_on_odds_pack(pack, sb)
    assert replaced == 5  # 3 outcome-uri 1X2 + 2 GG

    home = next(o for m in pack["markets"] if m["key"] == "1x2"
                for o in m["outcomes"] if o["value"] == "Home")
    assert home["avg_odd"] == 2.05
    assert home["reference_odd"] == 2.05
    assert home["display_odd"] == 2.05
    assert home["display_bookmaker"] == "Superbet"
    assert home["odds_label"] == "2.05"
    assert "[→ Superbet]" not in (home["odds_label"] or "")
    assert home["bookmaker_link"].startswith("https://superbet.ro/")
    # blocurile legacy raman consistente cu noua sursa
    assert pack["1X2"]["Home"] == 2.05
    assert pack["btts"]["Yes"] == 1.73


async def test_enrich_candidates_preia_link_si_forteaza_cota_superbet(oddspapi):
    sb = await op.superbet_for_fixture(FX)
    pack = af_odds_pack()
    op.overlay_on_odds_pack(pack, sb)

    analysis = {"confidence": "medium", "best_candidates": [
        {"market": "1X2", "pick": "Home", "prob": 0.55, "odds": 2.00},
        {"market": "Both Teams Score", "pick": "Yes", "prob": 0.60, "odds": 1.70},
    ]}
    analysts.enrich_candidates(analysis, pack)
    c1, c2 = analysis["best_candidates"]
    # cota folosita pe bilet = exact cota Superbet, aceeasi sursa cu linkul
    assert c1["odds"] == 2.05
    assert c1["implied_prob"] == round(1 / 2.05, 3)
    assert c1["odds_label"] == "2.05"
    assert "[→ Superbet]" not in (c1["odds_label"] or "")
    assert c1["bookmaker_link"].startswith("https://superbet.ro/")
    assert c1["bookmaker_name"] == "Superbet"
    assert c2["odds"] == 1.73
    assert c2["bookmaker_link"].startswith("https://superbet.ro/")


async def test_selectie_fara_cota_superbet_pastreaza_numarul_dar_primeste_linkul(oddspapi):
    # Piața over_under NU e în pachetul Superbet: numărul rămâne API-Football,
    # dar linkul paginii de meci Superbet tot se atașează.
    pack = af_odds_pack()
    pack["markets"].append({"key": "over_under", "name": "Goals Over/Under",
                            "outcomes": [{"value": "Over 2.5", "avg_odd": 1.90,
                                          "best_odd": 1.95, "n_books": 3,
                                          "reference_odd": 1.90,
                                          "display_odd": 1.90}]})
    sb = await op.superbet_for_fixture(FX)
    op.overlay_on_odds_pack(pack, sb)
    analysis = {"best_candidates": [
        {"market": "Goals Over/Under", "pick": "Over 2.5", "prob": 0.55, "odds": 1.90}]}
    analysts.enrich_candidates(analysis, pack)
    c = analysis["best_candidates"][0]
    assert c["odds"] == 1.90
    assert c["bookmaker_link"].startswith("https://superbet.ro/")


# ---------------------------------------------------------------------------
# (c) Incertitudine -> fallback tacut
# ---------------------------------------------------------------------------

async def test_sens_neconfirmat_piata_nefolosita(oddspapi):
    # /markets nu documenteaza outcome-ul X -> TOATA piata 1X2 e nefolosita,
    # chiar daca 1 si 2 au sens. GG ramane valida.
    oddspapi.markets = [
        {"marketId": 1, "marketName": "1X2", "outcomes": [
            {"outcomeId": "uuid-1", "outcomeName": "1"},
            {"outcomeId": "uuid-2", "outcomeName": "2"},
        ]},
        MARKETS_PAYLOAD[1],
    ]
    sb = await op.superbet_for_fixture(FX)
    assert sb is not None
    assert ("1x2", "Home") not in sb["odds"]
    assert ("btts", "Yes") in sb["odds"]


async def test_suma_probabilitati_anormala_piata_nefolosita(oddspapi):
    # Cote umflate: suma 1/cota = 0.57 < 0.85 -> piata GG nefolosita.
    gg = oddspapi.odds["bookmakerOdds"]["superbet.ro"]["markets"]["5"]["outcomes"]
    gg["uuid-gg"]["players"]["p"]["price"] = 3.5
    gg["uuid-ng"]["players"]["p"]["price"] = 3.5
    sb = await op.superbet_for_fixture(FX)
    assert sb is not None
    assert ("btts", "Yes") not in sb["odds"]
    assert ("1x2", "Home") in sb["odds"]  # 1X2 ramane valida


async def test_potrivire_ambigua_fallback(oddspapi):
    dublura = dict(FIXTURE_OPP, fixtureId="opp-10",
                   startTime="2026-08-22T17:30:00Z")
    oddspapi.fixtures = [FIXTURE_OPP, dublura]
    assert await op.superbet_for_fixture(FX) is None
    assert oddspapi.n_calls("odds") == 0  # nu se cere cota pe o potrivire nesigura


async def test_echipe_diferite_sau_kickoff_departe_fallback(oddspapi):
    alt_meci = dict(FIXTURE_OPP, participant2Name="FCSB")
    oddspapi.fixtures = [alt_meci]
    assert await op.superbet_for_fixture(FX) is None

    tarziu = dict(FIXTURE_OPP, startTime="2026-08-22T21:00:00Z")  # +4h30
    oddspapi.fixtures = [tarziu]
    op._fixtures_cache.clear()
    assert await op.superbet_for_fixture(FX) is None


def test_normalizare_nume_echipe():
    assert op._teams_match("FC Rapid București", "Rapid Bucuresti")
    assert op._teams_match("CS Universitatea Craiova", "Universitatea Craiova")
    assert op._teams_match("Tottenham", "Tottenham Hotspur")
    assert op._teams_match("Leverkusen", "Bayer Leverkusen")
    assert op._teams_match("Hamburg", "Hamburger SV")
    assert op._teams_match("Man City", "Manchester City")
    assert op._teams_match("Man Utd", "Manchester United")
    assert op._teams_match("Dortmund", "Borussia Dortmund")
    assert op._teams_match("PSG", "Paris Saint Germain")
    assert op._teams_match("Lyon", "Olympique Lyonnais")
    assert op._teams_match("Le Havre", "Havre AC")
    assert op._teams_match("PSV", "PSV Eindhoven")
    assert op._teams_match("Atletico Madrid", "Atletico de Madrid")
    assert not op._teams_match("Manchester United", "Manchester City")
    assert not op._teams_match("Inter", "Inter Miami")
    assert not op._teams_match("Real Madrid", "Real Sociedad")
    assert not op._teams_match("Sporting CP", "Sporting Kansas City")
    assert op._name_sim("Inter", "Inter Miami") < 0.70
    assert op._teams_match("Inter", "Internazionale")


# ---------------------------------------------------------------------------
# (d) Timeout / eroare -> nu blocheaza pachetul de date
# ---------------------------------------------------------------------------

async def test_timeout_oddspapi_nu_blocheaza(oddspapi, monkeypatch):
    # /markets vine din cache-ul DB (deja salvat), doar /fixtures pica.
    await op.get_markets_map()
    oddspapi.fail_endpoints = {"fixtures"}
    monkeypatch.setattr(op, "FIXTURES_TTL_S", 0)
    op._fixtures_cache.clear()
    assert await op.superbet_for_fixture(FX) is None


async def test_wait_result_respecta_bugetul_de_timp():
    async def lent():
        await asyncio.sleep(10)
    t0 = time.monotonic()
    assert await op.wait_result(asyncio.create_task(lent()), timeout=0.1) is None
    assert time.monotonic() - t0 < 1.0

    async def crapa():
        raise RuntimeError("boom")
    assert await op.wait_result(asyncio.create_task(crapa()), timeout=1.0) is None


# ---------------------------------------------------------------------------
# Rate limit, cache /markets, link, sens
# ---------------------------------------------------------------------------

async def test_cooldown_500ms_intre_apeluri_odds(oddspapi):
    starturi: list[float] = []
    original = oddspapi.__call__

    async def cu_timp(url, params):
        if url.endswith("/odds"):
            starturi.append(time.monotonic())
        return await original(url, params)

    op._http_get = cu_timp  # fixture-ul l-a setat deja pe fake; il imbracam
    await asyncio.gather(
        op._get("/odds", {"fixtureId": "a"}, respect_odds_cooldown=True),
        op._get("/odds", {"fixtureId": "b"}, respect_odds_cooldown=True),
    )
    assert len(starturi) == 2
    assert starturi[1] - starturi[0] >= op.ODDS_COOLDOWN_S - 0.05


async def test_markets_cache_persistent_si_refresh_saptamanal(oddspapi):
    await op.get_markets_map()
    assert oddspapi.n_calls("markets") == 1
    # memoria procesului golita -> a doua citire vine din DB, zero API
    op._markets_mem = None
    await op.get_markets_map()
    assert oddspapi.n_calls("markets") == 1
    # cache mai vechi de 7 zile -> refetch
    vechi = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
    cached = await db.oddspapi_cache_get(op.MARKETS_CACHE_KEY)
    await db.oddspapi_cache_set(op.MARKETS_CACHE_KEY, cached["json"], vechi)
    op._markets_mem = None
    await op.get_markets_map()
    assert oddspapi.n_calls("markets") == 2


async def test_markets_api_picat_degradeaza_pe_cache_vechi(oddspapi):
    await op.get_markets_map()
    vechi = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    cached = await db.oddspapi_cache_get(op.MARKETS_CACHE_KEY)
    await db.oddspapi_cache_set(op.MARKETS_CACHE_KEY, cached["json"], vechi)
    op._markets_mem = None
    oddspapi.fail_endpoints = {"markets"}
    sens = await op.get_markets_map()
    assert sens["1"]["outcomes"]["uuid-1"] == "1"  # cache-ul vechi, nu gol


async def test_rezolva_sens_outcome_none_cand_nedocumentat(oddspapi):
    assert await op.rezolva_sens_outcome(1, "uuid-1") == "1"
    assert await op.rezolva_sens_outcome(1, "uuid-inexistent") is None
    assert await op.rezolva_sens_outcome(999, "uuid-1") is None


def test_link_ro_pastreaza_calea():
    assert op._link_ro("https://superbet.com/a/b?c=1") == "https://superbet.ro/a/b?c=1"
    assert op._link_ro("/pariuri/fotbal/x") == "https://superbet.ro/pariuri/fotbal/x"
    assert op._link_ro("pariuri/fotbal/x") == "https://superbet.ro/pariuri/fotbal/x"
    assert op._link_ro(None) is None
    assert op._link_ro("") is None


async def test_over_under_ia_linia_din_handicap(oddspapi):
    """OddsPapi: piața e 'Over Under Full Time' + handicap 1.5, outcome doar Over/Under."""
    oddspapi.markets = MARKETS_PAYLOAD + [{
        "marketId": 108, "marketName": "Over Under Full Time", "handicap": 1.5,
        "outcomes": [
            {"outcomeId": "uuid-ov", "outcomeName": "Over"},
            {"outcomeId": "uuid-un", "outcomeName": "Under"},
        ],
    }]
    oddspapi.odds["bookmakerOdds"]["superbet.ro"]["markets"]["108"] = {
        "outcomes": {
            "uuid-ov": {"players": {"p": {"price": 1.22}}},
            "uuid-un": {"players": {"p": {"price": 4.00}}},
        }
    }
    sb = await op.superbet_for_fixture(FX)
    assert sb is not None
    assert sb["odds"][("over_under", "Over 1.5")] == 1.22
    assert sb["odds"][("over_under", "Under 1.5")] == 4.00

    pack = af_odds_pack()
    pack["markets"].append({
        "key": "over_under", "name": "Goals Over/Under",
        "outcomes": [{"value": "Over 1.5", "avg_odd": 1.30, "best_odd": 1.32,
                      "n_books": 3, "reference_odd": 1.30, "display_odd": 1.30,
                      "display_bookmaker": "Bet365",
                      "odds_label": "1.30 (Bet365)"}],
    })
    op.overlay_on_odds_pack(pack, sb)
    over = next(o for m in pack["markets"] if m["key"] == "over_under"
                for o in m["outcomes"] if o["value"] == "Over 1.5")
    assert over["avg_odd"] == 1.22
    assert over["bookmaker_link"].startswith("https://superbet.ro/")
    assert over["odds_label"] == "1.22"
    assert "[→ Superbet]" not in (over["odds_label"] or "")


def test_inject_completeaza_linkurile_omise_din_tabel():
    md = (
        "| # | Meci | Pariu | Cotă | Încredere |\n"
        "| 1 | Liverpool – Nottingham Forest · 14:30 | Victorie | "
        "1.53 (Superbet) [→ Superbet](https://superbet.ro/a) | ⭐⭐⭐ |\n"
        "| 2 | Elversberg – Leverkusen · 16:30 | GG | 1.57 (Superbet) | ⭐⭐ |\n"
        "| 3 | Tottenham – Newcastle · 19:30 | GG | 1.54 | ⭐⭐ |\n"
        "| 4 | Viseu – Porto · 20:00 | Victorie | 1.33 | ⭐⭐⭐ |\n"
        "| 5 | Palace – Man City · 22:00 | Victorie | 1.65 (Superbet) | ⭐⭐⭐ |\n"
        "| 6 | Dortmund – Hamburg · 16:30 | Victorie | 1.50 | ⭐⭐ |\n"
        "| 7 | Barcelona – Rayo · 22:30 | Victorie | 1.18 (Betano) | ⭐⭐⭐ |\n"
    )
    sels = [
        {"match": "Liverpool vs Nottingham Forest",
         "bookmaker_link": "https://superbet.ro/a"},
        {"match": "SV Elversberg vs Bayer Leverkusen",
         "bookmaker_link": "https://superbet.ro/elv"},
        {"match": "Tottenham Hotspur vs Newcastle United",
         "bookmaker_link": "https://superbet.ro/tot"},
        {"match": "Academico Viseu vs FC Porto",
         "bookmaker_link": "https://superbet.ro/por"},
        {"match": "Crystal Palace vs Manchester City",
         "bookmaker_link": "https://superbet.ro/mci"},
        {"match": "Borussia Dortmund vs Hamburger SV",
         "bookmaker_link": "https://superbet.ro/dor"},
        {"match": "Some vs Other"},  # fără link — nu se inventează
    ]
    out = op.inject_bookmaker_links(md, sels)
    assert out.count("https://superbet.ro/a") == 1
    assert "https://superbet.ro/elv" in out
    assert "https://superbet.ro/tot" in out
    assert "https://superbet.ro/por" in out
    assert "https://superbet.ro/mci" in out
    assert "https://superbet.ro/dor" in out
    assert "(Superbet)" not in out
    assert "(Betano)" not in out


def test_inject_tabel_exotic_dupa_titlul_meciului():
    """Pariuri exotice pe un meci: echipele sunt în titlu, nu pe rând."""
    md = (
        "🎯 Juventus vs Parma — 4 pariuri exotice\n\n"
        "| # | PIAȚĂ | SELECȚIE | COTĂ | ÎNCREDERE |\n"
        "|---|-------|----------|------|----------|\n"
        "| 1 | HT/FT | Juve conduce la pauză și câștigă | 1.76 | ⭐⭐⭐ |\n"
        "| 2 | Victorie la pauză | Juve câștigă repriza 1 | 1.65 | ⭐⭐⭐ |\n"
        "| 3 | Goluri repriza 1 — Over 1.5 | Peste 1 gol în prima repriză | 2.40 | ⭐⭐ |\n"
        "| 4 | Parma goluri exacte | Parma marchează exact 0 goluri | 1.69 | ⭐⭐ |\n"
    )
    sels = [{"match": "Juventus vs Parma",
             "bookmaker_link": "https://superbet.ro/juve-parma"}]
    out = op.inject_bookmaker_links(md, sels)
    assert out.count("https://superbet.ro/juve-parma") == 4
    assert "1.76 [→ Superbet](https://superbet.ro/juve-parma)" in out
    assert "2.40 [→ Superbet](https://superbet.ro/juve-parma)" in out
    assert "Over 1.5 [→ Superbet]" not in out  # linkul nu stă pe coloana Piață


def test_htft_maps_oddspapi_outcomes():
    assert op._internal_key({"marketType": "halftime-fulltime", "period": "",
                             "nume": "Half Time / Full Time"}) == "htft"
    assert op._outcome_value("htft", "1/1", 0) == "Home/Home"
    assert op._outcome_value("htft", "1/X", 0) == "Home/Draw"
    assert op._outcome_value("htft", "X/2", 0) == "Draw/Away"
    assert op._outcome_value("htft", "2/2", 0) == "Away/Away"
    assert "htft" in op._ANALYZED_KEYS


async def test_apply_superbet_pune_linkul_chiar_daca_modelul_a_scris_betano():
    op.reset_runtime_state()
    op._sb_mem[1001] = (time.monotonic(), {
        "link": "https://superbet.ro/barca",
        "odds": {("1x2", "Home"): 1.22},
        "bookmaker_name": "Superbet",
    })
    sels = [{
        "fixture_id": 1001, "match": "Barcelona vs Rayo Vallecano",
        "market": "1x2", "pick": "Home", "odds": 1.18,
        "odds_label": "1.18 (Betano)",
    }]
    await op.apply_superbet_to_selections(sels)
    assert sels[0]["bookmaker_link"] == "https://superbet.ro/barca"
    assert sels[0]["odds"] == 1.22
    assert sels[0]["odds_label"] == "1.22"
    assert "Betano" not in sels[0]["odds_label"]


async def test_apply_aduce_superbet_si_fara_prefetch_prealabil(oddspapi):
    """Mod classic: get_odds fără analyze_matches — apply trage Superbet acum."""
    sels = [{
        "fixture_id": 1001, "match": "Rapid București vs CS Universitatea Craiova",
        "market": "1x2", "pick": "Home", "odds": 2.00,
        "kickoff": "2026-08-22T19:30:00+03:00",
    }]
    await op.apply_superbet_to_selections(sels)
    assert sels[0]["bookmaker_link"].startswith("https://superbet.ro/")
    assert sels[0]["odds"] == 2.05


# ---------------------------------------------------------------------------
# Identitate persistenta: mapare + snapshot Superbet in DB
# ---------------------------------------------------------------------------

async def test_mapare_si_snapshot_persistate_in_db(oddspapi):
    sb = await op.superbet_for_fixture(FX)
    assert sb is not None
    # maparea a fost rezolvata pe nume si salvata in DB
    row = await db.oddspapi_map_get(1001)
    assert row["opp_fixture_id"] == "opp-9"
    assert row["method"] == "name"
    # snapshot-ul de cote e in DB, deserializabil cu chei tuple
    saved = await db.superbet_pack_get(1001)
    pack = op._sb_from_json(saved["json"])
    assert pack["odds"][("1x2", "Home")] == 2.05
    assert pack["link"].startswith("https://superbet.ro/")

    # «restart de proces»: memoria golita, feed-ul gol -> pachetul vine din
    # DB fara niciun apel HTTP suplimentar
    op.reset_runtime_state()
    oddspapi.fixtures = []
    n_odds = oddspapi.n_calls("odds")
    sb2 = await op.superbet_for_fixture(FX)
    assert sb2 is not None
    assert sb2["odds"][("btts", "Yes")] == 1.73
    assert oddspapi.n_calls("odds") == n_odds


async def test_apply_citeste_snapshotul_din_db_dupa_restart(oddspapi):
    assert await op.superbet_for_fixture(FX) is not None
    op.reset_runtime_state()  # cache-ul din memorie a disparut (restart/timing)
    sels = [{"fixture_id": 1001, "match": "Rapid vs U Craiova",
             "market": "1x2", "pick": "Home", "odds": 2.00,
             "odds_label": "2.00 (Betano)"}]
    await op.apply_superbet_to_selections(sels)
    assert sels[0]["bookmaker_link"].startswith("https://superbet.ro/")
    assert sels[0]["odds"] == 2.05
    assert sels[0]["odds_label"] == "2.05"


async def test_superbet_links_for_fixtures_din_snapshot(oddspapi):
    assert await op.superbet_for_fixture(FX) is not None
    op.reset_runtime_state()
    links = await op.superbet_links_for_fixtures([1001, 1001, 9999])
    assert len(links) == 1
    assert links[0]["link"].startswith("https://superbet.ro/")
    assert links[0]["fixture_id"] == 1001
    assert "Rapid" in (links[0].get("match") or "")


async def test_slate_mapeaza_candidatul_unic_cu_nume_inrudite(oddspapi, monkeypatch):
    """Numele difera prea mult pentru potrivirea stricta, dar la aceeasi ora
    exista un singur candidat nerevendicat cu nume macar inrudite."""
    async def fara_potrivire(fx):
        return None
    monkeypatch.setattr(op, "match_fixture", fara_potrivire)
    departe = dict(FIXTURE_OPP, fixtureId="opp-alt",
                   startTime="2026-08-22T18:30:00Z")  # +2h: iese din pasul slate
    await op._map_slate([dict(FX)], [FIXTURE_OPP, departe])
    row = await db.oddspapi_map_get(1001)
    assert row["opp_fixture_id"] == "opp-9"
    assert row["method"] == "slate"


async def test_llm_fallback_mapeaza_si_marcheaza_none(oddspapi, monkeypatch):
    async def fara_potrivire(fx):
        return None
    monkeypatch.setattr(op, "match_fixture", fara_potrivire)

    primite: list = []
    async def llm_fals(items):
        primite.extend(items)
        return {1001: "opp-9"}
    monkeypatch.setattr(op, "_llm_match_batch", llm_fals)

    # candidatul e in fereastra ±3h dar in afara pasului de slate (±45min)
    tarziu = dict(FIXTURE_OPP, startTime="2026-08-22T18:30:00Z")
    fx2 = dict(FX, fixture_id=1002)  # fara niciun candidat -> «none»
    await op._map_slate([dict(FX), fx2], [tarziu])

    assert primite and primite[0][0]["fixture_id"] == 1001
    row = await db.oddspapi_map_get(1001)
    assert row["opp_fixture_id"] == "opp-9"
    assert row["method"] == "llm"
    row2 = await db.oddspapi_map_get(1002)
    assert row2["opp_fixture_id"] is None
    assert row2["method"] == "none"
    # «none» proaspat: nu se mai incearca potrivirea la urmatoarea cerere
    assert await op._resolve_opp_id(fx2) is None


async def test_maparea_din_db_scurtcircuiteaza_potrivirea(oddspapi, monkeypatch):
    await db.oddspapi_map_set(1001, "opp-9", "llm", 0.9,
                              datetime.now(timezone.utc).isoformat())
    async def boom(fx):
        raise AssertionError("match_fixture nu trebuia apelat")
    monkeypatch.setattr(op, "match_fixture", boom)
    sb = await op.superbet_for_fixture(FX)
    assert sb is not None
    assert sb["odds"][("1x2", "Home")] == 2.05


def test_internal_key_pe_market_type_si_period():
    assert op._internal_key({"marketType": "spreads", "period": "fulltime",
                             "nume": "Asian Handicap"}) == "asian_handicap"
    assert op._internal_key({"marketType": "1x2", "period": "p1",
                             "nume": "First Half Result"}) == "1x2_ht"
    assert op._internal_key({"marketType": "totals", "period": "p1",
                             "nume": "Over Under First Half"}) == "over_under_ht"
    assert op._internal_key({"marketType": "doublechance", "period": "fulltime",
                             "nume": "Double Chance Full Time"}) == "double_chance"
    assert op._internal_key({"marketType": "teamtotals-team1", "period": "fulltime",
                             "nume": "Over Under Team 1"}) == "team_total_home"
    assert op._internal_key({"marketType": "firstgoal", "period": "fulltime",
                             "nume": "First Goal"}) == "first_to_score"
    assert op._internal_key({"marketType": "totals-corners", "period": "fulltime",
                             "nume": "Corners - Over Under"}) is None
    assert op._internal_key({"marketType": "1x2", "period": "p2",
                             "nume": "Second Half Result"}) is None
    # fallback pe nume (teste / cache fără marketType)
    assert op._internal_key({"marketType": "", "period": "", "nume": "1X2"}) == "1x2"


def test_ah_si_total_echipa_din_handicap():
    assert op._outcome_value("asian_handicap", "1", -0.5) == "Home -0.5"
    assert op._outcome_value("asian_handicap", "2", -0.5) == "Away +0.5"
    assert op._outcome_value("team_total_home", "Over", 1.5) == "Over 1.5"
    assert op._outcome_value("1x2_ht", "X", 0) == "Draw"
    assert op._outcome_value("first_to_score", "No Goal", 0) == "None"


async def test_overlay_ah_si_pauza_nu_corners(oddspapi):
    oddspapi.markets = MARKETS_PAYLOAD + [
        {"marketId": 40, "marketType": "spreads", "period": "fulltime",
         "marketName": "Asian Handicap", "handicap": -0.5,
         "outcomes": [{"outcomeId": "ah-1", "outcomeName": "1"},
                      {"outcomeId": "ah-2", "outcomeName": "2"}]},
        {"marketId": 41, "marketType": "1x2", "period": "p1",
         "marketName": "First Half Result",
         "outcomes": [{"outcomeId": "ht-1", "outcomeName": "1"},
                      {"outcomeId": "ht-x", "outcomeName": "X"},
                      {"outcomeId": "ht-2", "outcomeName": "2"}]},
        {"marketId": 99, "marketType": "totals-corners", "period": "fulltime",
         "marketName": "Corners - Over Under Full Time", "handicap": 9.5,
         "outcomes": [{"outcomeId": "c-o", "outcomeName": "Over"},
                      {"outcomeId": "c-u", "outcomeName": "Under"}]},
    ]
    mk = oddspapi.odds["bookmakerOdds"]["superbet.ro"]["markets"]
    mk["40"] = {"outcomes": {
        "ah-1": {"players": {"p": {"price": 1.90}}},
        "ah-2": {"players": {"p": {"price": 1.90}}},
    }}
    mk["41"] = {"outcomes": {
        "ht-1": {"players": {"p": {"price": 2.20}}},
        "ht-x": {"players": {"p": {"price": 3.30}}},
        "ht-2": {"players": {"p": {"price": 3.50}}},
    }}
    mk["99"] = {"outcomes": {
        "c-o": {"players": {"p": {"price": 1.85}}},
        "c-u": {"players": {"p": {"price": 1.95}}},
    }}
    sb = await op.superbet_for_fixture(FX)
    assert sb is not None
    assert sb["odds"][("asian_handicap", "Home -0.5")] == 1.90
    assert sb["odds"][("1x2_ht", "Home")] == 2.20
    assert all(k[0] != "over_under" or "9.5" not in k[1] for k in sb["odds"])
    assert ("over_under", "Over 9.5") not in sb["odds"]

    pack = af_odds_pack()
    pack["markets"].append({
        "key": "asian_handicap", "name": "Asian Handicap",
        "outcomes": [{"value": "Home -0.5", "avg_odd": 1.85, "best_odd": 1.88,
                      "n_books": 3, "reference_odd": 1.85, "display_odd": 1.85}],
    })
    pack["markets"].append({
        "key": "1x2_ht", "name": "First Half Winner",
        "outcomes": [{"value": "Home", "avg_odd": 2.00, "best_odd": 2.05,
                      "n_books": 3, "reference_odd": 2.00, "display_odd": 2.00}],
    })
    n = op.overlay_on_odds_pack(pack, sb)
    assert n >= 2
    ah = next(o for m in pack["markets"] if m["key"] == "asian_handicap"
              for o in m["outcomes"] if o["value"] == "Home -0.5")
    assert ah["avg_odd"] == 1.90
    assert ah["bookmaker_link"].startswith("https://superbet.ro/")


# ---------------------------------------------------------------------------
# Latenta: un val de 5 meciuri concurente incape in bugetul de asteptare
# ---------------------------------------------------------------------------

async def test_latenta_val_de_5_meciuri_sub_buget(oddspapi):
    oddspapi.latency_s = 0.15  # HTTP realist
    await op.get_markets_map()          # o singura data per proces
    await op._fixtures_feed("2026-08-21", "2026-08-23")  # o singura data per zi
    oddspapi.calls.clear()

    t0 = time.monotonic()
    rezultate = await asyncio.gather(*[
        op.superbet_for_fixture(dict(FX, fixture_id=1001 + i)) for i in range(5)
    ])
    total = time.monotonic() - t0

    assert all(r is not None for r in rezultate)
    assert oddspapi.n_calls("odds") == 5          # serializate prin coada
    assert oddspapi.n_calls("fixtures") == 0      # feed-ul vine din cache
    assert total <= op.WAIT_BUDGET_S + 0.5, f"val de 5 meciuri in {total:.2f}s"
