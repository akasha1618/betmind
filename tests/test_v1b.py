"""
Teste de acceptanta V1-B (Coordinator + Match Analysts paraleli), LLM mock-uit:
(a) analyze_matches ruleaza concurent in limita semaforului si persista analize
(b) JSON invalid -> 1 retry -> inregistrare analysis_failed, fluxul supravietuieste
(c) e2e mock-uit "bilet cota 5 azi": store -> analisti -> build_ticket,
    cu status de paralelism pe SSE
(d) follow-up pe un meci deja analizat refoloseste analiza (zero apeluri LLM noi)
+ comutatorul de siguranta ORCHESTRATION_MODE=classic identic cu pre-V1-B.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import agent
import analysts
import db
import football_data as fd
import prompts
from tests.conftest import raw_fixture


def _today() -> str:
    return fd.today_local().isoformat()


def _now_iso() -> str:
    return fd.now_local().isoformat(timespec="seconds")


async def _seed_fixture(fid: int, day: str, time: str = "19:30", status: str = "NS",
                        home=(10, "NEC"), away=(20, "Excelsior"), league_id: int = 39):
    parsed = fd._parse_fixture(raw_fixture(
        fixture_id=fid, league_id=league_id,
        kickoff=f"{day}T{time}:00+03:00", status=status, home=home, away=away,
    ))
    await db.upsert_fixture(parsed, _now_iso())
    return parsed


def _valid_analysis_json(fid: int) -> str:
    return json.dumps({
        "fixture_id": fid,
        "match": "NEC vs Excelsior",
        "kickoff": f"{_today()}T19:30",
        "market_probs": {"home": 0.45, "draw": 0.28, "away": 0.27,
                         "over25": 0.55, "under25": 0.45, "btts_yes": 0.52},
        "best_candidates": [
            {"market": "1X2", "pick": "1", "odds": 2.30, "prob": 0.48,
             "reason": "4W din ultimele 5 acasa, 11 goluri marcate"},
            {"market": "Over 2.5", "pick": "Over 2.5", "odds": 1.85, "prob": 0.55,
             "reason": "media 3.1 goluri/meci in ultimele 6"},
        ],
        "top_factors": ["Gazdele au 4W din 5 acasa (11-3 golaveraj)",
                        "Oaspetii n-au tinut poarta intacta din 12 aprilie"],
        "angle": "Oaspetii au jucat joi in Conference League — doar 3 zile de refacere.",
        "data_gaps": [],
        "confidence": "medium",
    })


def _fake_usage():
    return SimpleNamespace(input_tokens=10, output_tokens=20)


# ---------------------------------------------------------------------------
# (a) paralelism in limita semaforului + persistare
# ---------------------------------------------------------------------------

async def test_analyze_matches_parallel_within_semaphore(no_http, monkeypatch):
    monkeypatch.setenv("MAX_PARALLEL_ANALYSTS", "2")
    await db.init_db()
    day = _today()
    for fid in (1, 2, 3, 4, 5):
        await _seed_fixture(fid, day)
    await db.budget_add(day, 50)  # buget epuizat -> pack-urile nu ating HTTP

    state = {"current": 0, "max": 0, "calls": 0}

    async def fake_llm(system, user):
        state["current"] += 1
        state["calls"] += 1
        state["max"] = max(state["max"], state["current"])
        await asyncio.sleep(0.05)
        fid = json.loads(user)["fixture"]["fixture_id"]
        state["current"] -= 1
        return _valid_analysis_json(fid), _fake_usage()

    monkeypatch.setattr(analysts, "_call_analyst_llm", fake_llm)

    res = await analysts.analyze_matches([1, 2, 3, 4, 5])
    assert res["analyzed"] == 5
    assert res["failed"] == 0
    assert state["calls"] == 5
    assert state["max"] == 2  # concurrent, dar niciodata peste semafor
    assert await db.count_analyses() == 5  # fiecare analiza persistata


# ---------------------------------------------------------------------------
# (b) JSON invalid -> retry -> analysis_failed
# ---------------------------------------------------------------------------

async def test_invalid_json_retries_once_then_analysis_failed(no_http, monkeypatch):
    await db.init_db()
    day = _today()
    await _seed_fixture(1, day)
    await db.budget_add(day, 50)

    calls = []

    async def bad_llm(system, user):
        calls.append(1)
        return "imi pare rau, nu pot { asta nu e JSON valid", _fake_usage()

    monkeypatch.setattr(analysts, "_call_analyst_llm", bad_llm)

    res = await analysts.analyze_match(1)
    assert len(calls) == 2  # apel initial + exact UN retry
    assert res["analysis_failed"] is True

    stored = await db.latest_analysis(1)
    assert stored is not None
    assert json.loads(stored["json"])["analysis_failed"] is True

    # Fluxul de batch supravietuieste si raporteaza esecul onest.
    batch = await analysts.analyze_matches([1])
    assert batch["failed"] == 1
    assert batch["failed_fixtures"][0]["fixture_id"] == 1


# ---------------------------------------------------------------------------
# (d) refolosirea analizelor la follow-up (zero apeluri LLM noi)
# ---------------------------------------------------------------------------

async def test_followup_reuses_stored_analysis_without_new_llm_call(no_http, monkeypatch):
    await db.init_db()
    day = _today()
    await _seed_fixture(7, day)
    await db.budget_add(day, 50)

    calls = []

    async def fake_llm(system, user):
        calls.append(1)
        return _valid_analysis_json(7), _fake_usage()

    monkeypatch.setattr(analysts, "_call_analyst_llm", fake_llm)

    first = await analysts.analyze_matches([7])
    assert first["analyzed"] == 1 and len(calls) == 1

    second = await analysts.analyze_matches([7])  # follow-up
    assert second["analyzed"] == 1
    assert len(calls) == 1  # niciun apel LLM nou
    assert second["analyses"][0]["reused"] is True
    assert await db.count_analyses(7) == 1  # nu s-a duplicat


# ---------------------------------------------------------------------------
# (c) e2e mock-uit: "bilet cota 5 azi" -> store -> analisti -> build_ticket
# ---------------------------------------------------------------------------

class _FakeStream:
    def __init__(self, msg):
        self._msg = msg

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def __aiter__(self):
        async def gen():
            for b in self._msg.content:
                if b.type == "text":
                    yield SimpleNamespace(type="text", text=b.text)
        return gen()

    async def get_final_message(self):
        return self._msg


def _msg(content, stop_reason):
    return SimpleNamespace(content=content, stop_reason=stop_reason,
                           usage=SimpleNamespace(input_tokens=100, output_tokens=50))


def _text(t):
    return SimpleNamespace(type="text", text=t)


def _tool(tid, name, args):
    return SimpleNamespace(type="tool_use", id=tid, name=name, input=args)


def _fake_anthropic(script: list):
    class _Messages:
        def stream(self, **kwargs):
            return _FakeStream(script.pop(0))

    class _Client:
        def __init__(self, **kwargs):
            self.messages = _Messages()

    return _Client


async def test_e2e_ticket_flow_streams_parallel_status(no_http, monkeypatch):
    await db.init_db()
    day = _today()
    await _seed_fixture(101, day, "19:30", home=(10, "NEC"), away=(20, "Excelsior"))
    await _seed_fixture(102, day, "21:00", home=(30, "Rapid"), away=(40, "FCSB"))
    await db.mark_day_synced(day, _now_iso())
    await db.budget_add(day, 50)  # analistii lucreaza doar cu store-ul local

    async def fake_llm(system, user):
        fid = json.loads(user)["fixture"]["fixture_id"]
        return _valid_analysis_json(fid), _fake_usage()

    monkeypatch.setattr(analysts, "_call_analyst_llm", fake_llm)

    candidates = [
        {"fixture_id": 101, "match": "NEC vs Excelsior", "market": "1X2",
         "pick": "1", "odds": 2.30, "prob": 0.48},
        {"fixture_id": 102, "match": "Rapid vs FCSB", "market": "Over 2.5",
         "pick": "Over 2.5", "odds": 2.20, "prob": 0.50},
    ]
    script = [
        _msg([_tool("t1", "get_fixtures", {"date_from": day})], "tool_use"),
        _msg([_tool("t2", "analyze_matches", {"fixture_ids": [101, 102]})], "tool_use"),
        _msg([_tool("t3", "build_ticket",
                    {"candidates": candidates, "target_odds": 5})], "tool_use"),
        _msg([_text("Biletul tău e gata: cotă totală 5.06. "
                    "18+ | Pariază responsabil.")], "end_turn"),
    ]
    monkeypatch.setattr(agent, "AsyncAnthropic", _fake_anthropic(script))

    events = []
    messages = [{"role": "user", "content": "bilet cota 5 azi"}]
    async for ev in agent.run_turn(messages):
        events.append(ev)

    # Fluxul s-a terminat curat si a streamuit statusul de paralelism per batch.
    assert events[-1] == {"type": "done"}
    statuses = [e["label"] for e in events if e["type"] == "status"]
    assert any("Analizez 2 meciuri în paralel" in s for s in statuses)
    assert any("(2/2 gata)" in s for s in statuses)
    deltas = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "Biletul" in deltas

    # Store -> analisti -> build_ticket: analizele au fost persistate,
    # iar rezultatul build_ticket a intrat in istoric ca tool_result.
    assert await db.count_analyses() == 2
    ticket_result = json.loads(
        next(m for m in messages
             if m["role"] == "user" and isinstance(m["content"], list)
             and m["content"][0].get("tool_use_id") == "t3")["content"][0]["content"]
    )
    assert ticket_result["ok"] is True
    assert ticket_result["total_odds"] >= 5

    # Jurnalul de cost: 4 mesaje coordinator + 2 analisti, acelasi turn_id.
    conn = await db._connect()
    try:
        cur = await conn.execute("SELECT COUNT(*) AS n, COUNT(DISTINCT turn_id) AS t FROM usage_log")
        row = await cur.fetchone()
    finally:
        await conn.close()
    assert row["n"] == 6
    assert row["t"] == 1


# ---------------------------------------------------------------------------
# Comutatorul de siguranta ORCHESTRATION_MODE
# ---------------------------------------------------------------------------

# Setul clasic = tool-urile pre-V1-B + tool-urile independente de mod adaugate
# ulterior (V1-D: get_my_tickets — memorie de bilete, valabila in ambele moduri).
_CLASSIC_TOOLS = {
    "get_fixtures", "get_fixture_changes", "track_league", "list_leagues",
    "get_team_last_matches", "get_team_statistics", "get_h2h",
    "get_injuries", "get_standings", "get_odds", "build_ticket",
    "get_my_tickets",
}


def test_classic_mode_is_identical_to_pre_v1b(monkeypatch):
    monkeypatch.setenv("ORCHESTRATION_MODE", "classic")
    assert {t["name"] for t in agent.build_tools()} == _CLASSIC_TOOLS
    prompt = prompts.build_system_prompt()
    assert "analyze_matches" not in prompt
    assert "WORKFLOW FOR A TICKET REQUEST:" in prompt  # sectiunea clasica, intacta

    monkeypatch.setenv("ORCHESTRATION_MODE", "analysts")
    assert {t["name"] for t in agent.build_tools()} == _CLASSIC_TOOLS | {"analyze_matches"}
    prompt = prompts.build_system_prompt()
    assert "analyze_matches" in prompt
    assert "you are the Coordinator" in prompt


# ---------------------------------------------------------------------------
# Data pack: campurile calculate din fixture store
# ---------------------------------------------------------------------------

async def test_data_pack_computed_fields_from_local_store(no_http):
    await db.init_db()
    day = fd.today_local()
    target_day = day.isoformat()
    d_minus3 = (day - __import__("datetime").timedelta(days=3)).isoformat()
    d_minus2 = (day - __import__("datetime").timedelta(days=2)).isoformat()

    # Meciul analizat + ultimul meci terminat al gazdelor acum 3 zile
    # + meci european (UCL, liga 2) al oaspetilor acum 2 zile.
    await _seed_fixture(201, target_day, "19:30", home=(10, "NEC"), away=(20, "Excelsior"))
    finished = fd._parse_fixture(raw_fixture(
        fixture_id=202, kickoff=f"{d_minus3}T19:30:00+03:00", status="FT",
        home=(10, "NEC"), away=(99, "Altcineva"), goals=(2, 1),
    ))
    await db.upsert_fixture(finished, _now_iso())
    await _seed_fixture(203, d_minus2, "22:00", home=(20, "Excelsior"),
                        away=(88, "Euroteam"), league_id=2)

    await db.budget_add(target_day, 50)  # fara HTTP: totul din store + gaps
    pack = await analysts.assemble_data_pack(201)

    assert pack["fixture"]["fixture_id"] == 201
    assert pack["home"]["days_since_last_match"] == 3
    assert pack["home"]["midweek_european_game"] is False
    assert pack["away"]["midweek_european_game"] is True
    assert pack["data_gaps"]  # apelurile API au cazut onest in gaps (buget epuizat)
