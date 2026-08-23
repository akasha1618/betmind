"""
Teste de acceptanta V1-C (Quality & Conversation Pack) + aditiile 8-10:
(a) build_ticket cu excluded_fixture_ids: fixture-ul exclus nu apare niciodata
(b) biletul + selectiile persistate la generare (e2e mock-uit)
(c) prompturile contin lista de fraze interzise si regula orei Romaniei
(d) schema analistului respinge top_factors fara cifra/entitate numita
(8) prompt caching: prefix stabil marcat, tokenii de cache logati in usage_log
(9) analistul e instruit sa scrie in romana
(10) comutatorul classic: regulile si persistenta functioneaza si acolo
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

import agent
import analysts
import db
import football_data as fd
import prompts
from ticket_builder import build_ticket
from tests.test_v1b import _msg, _text, _tool, _FakeStream, _seed_fixture, _today, _now_iso


# ---------------------------------------------------------------------------
# (a) excluderea fixture-urilor din bilet
# ---------------------------------------------------------------------------

def test_build_ticket_excludes_fixtures_and_still_reaches_target():
    candidates = [
        {"fixture_id": 1, "match": "A vs B", "market": "1X2", "pick": "1",
         "odds": 2.0, "prob": 0.55},
        {"fixture_id": 2, "match": "C vs D", "market": "1X2", "pick": "2",
         "odds": 2.0, "prob": 0.60},
        {"fixture_id": 3, "match": "E vs F", "market": "Over 2.5", "pick": "Over 2.5",
         "odds": 1.8, "prob": 0.58},
    ]
    res = build_ticket(candidates, target_odds=3.0, excluded_fixture_ids=[2])
    assert res["ok"] is True
    picked_ids = {s["fixture_id"] for s in res["selections"]}
    assert 2 not in picked_ids  # exclusul nu intra NICIODATA
    assert res["reached_target"] is True  # tinta atinsa din 1 si 3 (2.0 x 1.8 = 3.6)
    assert res["excluded_fixture_ids"] == [2]

    # Fara excludere, comportamentul vechi ramane neschimbat.
    res_all = build_ticket(candidates, target_odds=3.0)
    assert res_all["ok"] is True and res_all["excluded_fixture_ids"] == []


# ---------------------------------------------------------------------------
# (b) persistenta biletului la generare
# ---------------------------------------------------------------------------

async def test_ticket_and_selections_persisted_on_generation_e2e(no_http, monkeypatch):
    await db.init_db()
    day = _today()
    await _seed_fixture(301, day, "19:30")
    await db.mark_day_synced(day, _now_iso())
    await db.budget_add(day, 50)

    candidates = [
        {"fixture_id": 301, "match": "NEC vs Excelsior", "market": "1X2", "pick": "1",
         "odds": 2.3, "prob": 0.5, "confidence": "high"},
        {"fixture_id": 302, "match": "Rapid vs FCSB", "market": "GG", "pick": "Yes",
         "odds": 2.2, "prob": 0.52, "confidence": "medium"},
    ]
    script = [
        _msg([_tool("t1", "build_ticket",
                    {"candidates": candidates, "target_odds": 5, "risk_level": "mediu"})],
             "tool_use"),
        _msg([_text("Gata biletul! 18+ | Pariază responsabil.")], "end_turn"),
    ]

    class _Messages:
        def stream(self, **kwargs):
            return _FakeStream(script.pop(0))

    class _Client:
        def __init__(self, **kwargs):
            self.messages = _Messages()

    monkeypatch.setattr(agent, "AsyncAnthropic", _Client)

    messages = [{"role": "user", "content": "bilet cota 5"}]
    events = [ev async for ev in agent.run_turn(messages, conversation_id="conv-42")]
    assert events[-1] == {"type": "done"}

    conn = await db._connect()
    try:
        cur = await conn.execute("SELECT * FROM tickets")
        tickets = [dict(r) for r in await cur.fetchall()]
        cur = await conn.execute("SELECT * FROM ticket_selections ORDER BY id")
        sels = [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()

    assert len(tickets) == 1
    t = tickets[0]
    assert t["conversation_id"] == "conv-42"
    assert t["status"] == "open"
    assert t["risk_level"] == "mediu"
    assert t["target_odds"] == 5
    assert t["total_odds"] >= 5

    assert len(sels) == 2
    assert {s["fixture_id"] for s in sels} == {301, 302}
    assert all(s["result"] is None for s in sels)
    assert sels[0]["confidence"] in ("high", "medium")

    # Coordinatorul primeste ticket_id inapoi in tool_result.
    tool_result = json.loads(
        next(m for m in messages
             if m["role"] == "user" and isinstance(m["content"], list))["content"][0]["content"]
    )
    assert tool_result["ticket_id"] == t["id"]


async def test_ticket_persistence_works_in_classic_mode_too(monkeypatch):
    monkeypatch.setenv("ORCHESTRATION_MODE", "classic")
    await db.init_db()
    args = {
        "candidates": [{"fixture_id": 9, "match": "A vs B", "market": "1X2",
                        "pick": "1", "odds": 2.0, "prob": 0.55}],
        "target_odds": 1.9,
    }
    result = await agent._execute_tool("build_ticket", args, conversation_id="classic-conv")
    assert result["ok"] is True
    assert isinstance(result["ticket_id"], int)

    conn = await db._connect()
    try:
        cur = await conn.execute("SELECT conversation_id FROM tickets WHERE id = ?",
                                 (result["ticket_id"],))
        row = await cur.fetchone()
    finally:
        await conn.close()
    assert row["conversation_id"] == "classic-conv"


# ---------------------------------------------------------------------------
# (c)+(9)+(10) reguli in prompturi, in AMBELE moduri
# ---------------------------------------------------------------------------

_BANNED_SAMPLES = ["echipă de calitate", "meci deschis", "tradițional cu goluri",
                   "favorită clară"]


@pytest.mark.parametrize("mode", ["analysts", "classic"])
def test_prompt_contains_banned_phrases_and_romania_time_rule(monkeypatch, mode):
    monkeypatch.setenv("ORCHESTRATION_MODE", mode)
    prompt = prompts.build_system_prompt()
    for phrase in _BANNED_SAMPLES:
        assert phrase in prompt  # lista de fraze interzise e explicita
    assert "sâmbătă 19:30" in prompt          # formatul orei Romaniei
    assert 'NEVER label any time as "UTC"' in prompt
    assert "excluded_fixture_ids" in prompt   # regula de editare a biletului
    assert "Verifică pe cont propriu" in prompt
    assert "⭐⭐⭐" in prompt                    # stelele de incredere
    assert "NEVER mention ticket_id" in prompt
    # Degradare gratioasa in classic: stelele au fallback din p propriu.
    assert "degrade gracefully" in prompt


@pytest.mark.parametrize("mode", ["analysts", "classic"])
def test_question_about_a_selection_is_not_an_edit_request(monkeypatch, mode):
    """O intrebare ("de ce Juve?") cere o EXPLICATIE, nu scoaterea de pe bilet.
    Fara aceasta regula, singurul tipar din prompt pentru "user mentioneaza o
    selectie dupa livrarea biletului" era editarea — si modelul scotea meciul."""
    monkeypatch.setenv("ORCHESTRATION_MODE", mode)
    prompt = prompts.build_system_prompt()
    assert "QUESTIONS vs EDITS" in prompt
    assert "de ce Juve?" in prompt
    assert "LEAVE THE TICKET UNCHANGED" in prompt
    assert "Never treat a question as a removal request." in prompt
    # Editarea ramane posibila, dar doar la cerere EXPLICITA.
    assert "only after an explicit change request" in prompt
    # Declansatorul vag de dinainte ("nu-mi place Y") nu mai exista.
    assert "nu-mi place Y" not in prompt

    # Si schema tool-ului spune acelasi lucru, ca sa nu contrazica promptul.
    build = next(t for t in agent.build_tools() if t["name"] == "build_ticket")
    excl = build["input_schema"]["properties"]["excluded_fixture_ids"]["description"]
    assert "DOAR cand userul cere explicit" in excl


def test_analyst_prompt_has_romanian_and_specificity_rules():
    p = analysts._ANALYST_SYSTEM_PROMPT
    assert "ROMANIAN" in p                    # aditia 9: stringurile umane in romana
    assert "echipă de calitate" in p          # frazele interzise si la analist
    assert "confidence: high/medium/low" in p  # enum-urile raman engleze


# ---------------------------------------------------------------------------
# (d) validatorul de specificitate al analistului
# ---------------------------------------------------------------------------

def _analysis_payload(factors: list[str]) -> dict:
    return {
        "fixture_id": 1, "match": "A vs B", "kickoff": "2026-08-22T19:30",
        "market_probs": {"home": 0.4, "draw": 0.3, "away": 0.3,
                         "over25": 0.5, "under25": 0.5, "btts_yes": 0.5},
        "best_candidates": [], "top_factors": factors,
        "angle": "test", "data_gaps": [], "confidence": "low",
    }


def test_analyst_schema_rejects_generic_top_factor():
    with pytest.raises(ValidationError, match="generic"):
        analysts.MatchAnalysis.model_validate(
            _analysis_payload(["echipa gazdă e mai bună și în formă"])
        )
    # Cu cifra -> trece; cu jucator numit -> trece.
    ok = analysts.MatchAnalysis.model_validate(
        _analysis_payload(["Gazdele au 4W din 5 acasă", "revine golgheterul Burcă"])
    )
    assert len(ok.top_factors) == 2


# ---------------------------------------------------------------------------
# (8) prompt caching: markeri pe prefixul stabil + tokeni logati
# ---------------------------------------------------------------------------

def test_cache_markers_on_stable_prefix():
    system, tools, msgs = agent._with_cache_markers(
        "SYSTEM", agent.build_tools(),
        [{"role": "user", "content": "salut"},
         {"role": "assistant", "content": [{"type": "text", "text": "hei"}]}],
    )
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    assert tools[-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in tools[0]
    # Ultimul bloc din istoric poarta markerul; restul raman neatinse.
    assert msgs[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}
    assert msgs[0]["content"] == "salut"


async def test_second_turn_reports_cache_read_and_lower_input(no_http, monkeypatch):
    await db.init_db()

    captured_kwargs: list[dict] = []
    usages = [
        SimpleNamespace(input_tokens=82000, output_tokens=300,
                        cache_read_input_tokens=0, cache_creation_input_tokens=75000),
        SimpleNamespace(input_tokens=900, output_tokens=250,
                        cache_read_input_tokens=79000, cache_creation_input_tokens=1200),
    ]
    replies = [
        _msg([_text("Prima tură.")], "end_turn"),
        _msg([_text("A doua tură, din cache.")], "end_turn"),
    ]
    for r, u in zip(replies, usages):
        r.usage = u

    class _Messages:
        def stream(self, **kwargs):
            captured_kwargs.append(kwargs)
            return _FakeStream(replies.pop(0))

    class _Client:
        def __init__(self, **kwargs):
            self.messages = _Messages()

    monkeypatch.setattr(agent, "AsyncAnthropic", _Client)

    history = [{"role": "user", "content": "bilet cota 5 azi"}]
    async for _ in agent.run_turn(history, conversation_id="c1"):
        pass
    history.append({"role": "user", "content": "si un follow-up scurt"})
    async for _ in agent.run_turn(history, conversation_id="c1"):
        pass

    # Cererile au prefixul stabil marcat pentru cache.
    for kwargs in captured_kwargs:
        assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
        assert kwargs["tools"][-1]["cache_control"] == {"type": "ephemeral"}
        assert kwargs["messages"][-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}

    # Acceptanta: tura 2 raporteaza cache-read > 0 si input facturat mai mic.
    conn = await db._connect()
    try:
        cur = await conn.execute(
            "SELECT input_tokens, cache_read_tokens, cache_write_tokens "
            "FROM usage_log ORDER BY id")
        rows = [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()
    assert len(rows) == 2
    first, second = rows
    assert first["cache_read_tokens"] == 0
    assert first["cache_write_tokens"] == 75000
    assert second["cache_read_tokens"] > 0
    assert second["input_tokens"] < first["input_tokens"]


# ---------------------------------------------------------------------------
# Weekday romanesc in outputul get_fixtures (pt. "sâmbătă 19:30")
# ---------------------------------------------------------------------------

async def test_fixtures_expose_romanian_weekday(no_http):
    await db.init_db()
    day = _today()
    await _seed_fixture(401, day)
    await db.mark_day_synced(day, _now_iso())

    res = await fd.get_fixtures(day)
    weekday = res["fixtures"][0]["weekday"]
    assert weekday in ("luni", "marți", "miercuri", "joi", "vineri", "sâmbătă", "duminică")
    assert weekday == fd.weekday_ro(day)
