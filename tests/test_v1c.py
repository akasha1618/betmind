"""
Teste de acceptanta V1-C (Quality & Conversation Pack) + aditiile 8-10:
(a) build_ticket cu excluded_fixture_ids: fixture-ul exclus nu apare niciodata
(b) biletul + selectiile persistate la generare (e2e mock-uit)
(c) prompturile contin lista de fraze interzise si regula orei Romaniei
(d) schema analistului elimina top_factors generici, nu invalideaza analiza
(8) prompt caching: prefix stabil marcat, tokenii de cache logati in usage_log
(9) analistul e instruit sa scrie in romana
(10) comutatorul classic: regulile si persistenta functioneaza si acolo
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import agent
import analysts
import db
import football_data as fd
import prompts
from ticket_builder import build_ticket
from tests.test_v1b import (
    _msg, _text, _tool, _FakeStream, _seed_fixture, _today, _now_iso,
    _valid_analysis_json, _fake_usage,
)


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


def test_build_ticket_one_selection_per_match():
    """Două piețe pe același meci nu pot intra pe un bilet combinat."""
    candidates = [
        {"fixture_id": 1, "match": "Juve vs Parma", "market": "1X2", "pick": "Home",
         "odds": 1.26, "prob": 0.72},
        {"fixture_id": 1, "match": "Juve vs Parma", "market": "htft", "pick": "Home/Home",
         "odds": 1.76, "prob": 0.52},
        {"fixture_id": 2, "match": "A vs B", "market": "Over 1.5", "pick": "Over 1.5",
         "odds": 1.40, "prob": 0.68},
    ]
    res = build_ticket(candidates, target_odds=1.5)
    assert res["ok"] is True
    ids = [s["fixture_id"] for s in res["selections"]]
    assert ids.count(1) == 1
    assert set(ids) <= {1, 2}


def test_annotate_same_match_menu_pe_tabel_exotic():
    from ticket_builder import annotate_same_match_menu, SAME_MATCH_MENU_NOTE
    md = (
        "🎯 12 Pariuri Exotice — Juventus vs Parma\n\n"
        "| # | PIAȚĂ | SELECȚIE | COTĂ | TIP |\n"
        "|---|-------|----------|------|-----|\n"
        "| 1 | Câștigătoare | Juventus | 1.26 | Clasic |\n"
        "| 2 | HT/FT | Juve/Juve | 1.76 | Combinat |\n"
    )
    out = annotate_same_match_menu(md)
    assert SAME_MATCH_MENU_NOTE in out
    assert annotate_same_match_menu(out) == out  # idempotent


def test_annotate_nu_atinge_biletul_clasic():
    from ticket_builder import annotate_same_match_menu
    md = (
        "| # | Meci (ziua, ora) | Pariu | Cotă | Încredere |\n"
        "| 1 | Juventus vs Parma · 21:45 | Victorie | 1.26 | ⭐⭐⭐ |\n"
        "| 2 | Inter vs Como · 18:00 | Over 1.5 | 1.40 | ⭐⭐ |\n"
        "\n**Cotă totală: 1.76**\n"
    )
    assert annotate_same_match_menu(md) == md


def test_parse_selection_request_ignores_odds_target():
    from ticket_builder import parse_selection_request
    assert parse_selection_request("Recomandă-mi un bilet cu cota 5 din meciurile de azi") == {}
    assert parse_selection_request("fă-mi un bilet cu 5 selecții") == {"target_selections": 5}
    assert parse_selection_request("vreau mai multe meciuri pe bilet") == {"min_selections": 5}
    assert parse_selection_request("prea puține meciuri") == {"min_selections": 5}


def test_target_selections_keeps_five_and_reports_raw_product():
    """Când userul cere 5 selecții, nu ne oprim la 3 doar pentru că s-a atins cota."""
    candidates = [
        {"fixture_id": i, "match": f"M{i} vs X", "market": "1X2", "pick": "1",
         "odds": round(2.40 - i * 0.05, 2), "prob": 0.50, "confidence": "medium",
         "kickoff": f"2026-08-23T{10 + i}:00"}
        for i in range(8)
    ]
    compact = build_ticket(candidates, target_odds=11)
    assert compact["selections_count"] == 3
    assert compact["honesty"] is None

    res = build_ticket(candidates, target_odds=11, target_selections=5)
    assert res["ok"] is True
    assert res["selections_count"] == 5
    assert res["total_odds"] >= 11 * 0.999
    expected = 1.0
    for s in res["selections"]:
        expected *= s["prob"]
    assert res["estimated_probability"] == pytest.approx(expected, abs=1e-4)
    assert res["honesty"]
    assert res["honesty"]["compact_selections"] == 3
    assert "5 selecții în loc de 3" in res["honesty"]["user_message"]


def test_target_selections_warns_when_not_enough_candidates():
    candidates = [
        {"fixture_id": i, "match": f"M{i} vs X", "market": "1X2", "pick": "1",
         "odds": 2.0, "prob": 0.5}
        for i in range(3)
    ]
    res = build_ticket(candidates, target_odds=5, target_selections=5)
    assert res["selections_count"] == 3
    assert any("5" in w and "3" in w for w in res["warnings"])


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


def test_classic_get_odds_batch_are_ids_pentru_superbet():
    """În mode=classic nu există analyze_matches — id-urile vin din get_odds."""
    ids = agent._fixture_ids_from_tool("get_odds", {"fixture_id": 1550099}, None)
    assert ids == [1550099]
    ids = agent._fixture_ids_from_tool(
        "build_ticket",
        {"candidates": [{"fixture_id": 1}, {"fixture_id": 2}]},
        None,
    )
    assert ids == [1, 2]


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
    assert "LENGTH DISCIPLINE" in prompt
    assert "1500" in prompt
    assert "MAXIMUM one short line per avoided match" in prompt
    assert "SAME MATCH vs TICKET" in prompt
    assert "Bet Builder" in prompt
    assert "one selection per match" in prompt
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


def test_analyst_schema_drops_generic_top_factor_keeps_rest():
    """Un factor generic e eliminat; restul analizei ramane valida.
    Nu se mai ridica ValidationError pe toata analiza."""
    only_generic = analysts.MatchAnalysis.model_validate(
        _analysis_payload(["echipa gazdă e mai bună și în formă"])
    )
    assert only_generic.top_factors == []

    mixed = analysts.MatchAnalysis.model_validate(
        _analysis_payload([
            "echipa gazdă e mai bună și în formă",
            "Gazdele au 4W din 5 acasă",
            "revine golgheterul Burcă",
        ])
    )
    assert mixed.top_factors == ["Gazdele au 4W din 5 acasă", "revine golgheterul Burcă"]


def test_analyst_schema_keeps_midweek_european_and_date_factors():
    """Observatiile de program (meci european / zile de pauza) si datele
    sunt acceptate — nu doar cifra sau nume propriu."""
    ok = analysts.MatchAnalysis.model_validate(
        _analysis_payload([
            "Ambele echipe nu au meciuri europene de mijlocul săptămânii; se profilează duelul pe stabilitate și calitate ofensivă",
            "Kickoff luni 19:30",
        ])
    )
    assert len(ok.top_factors) == 2


def test_extract_json_strips_markdown_and_uses_outer_braces():
    blob = 'nota\n```json\n{"a": 1, "nested": {"b": 2}}\n```\n'
    assert analysts._extract_json(blob) == {"a": 1, "nested": {"b": 2}}


def test_extract_json_repairs_truncated_object():
    obj = analysts._extract_json('{"match": "A vs B", "fixture_id": 1')
    assert obj["match"] == "A vs B"
    assert obj["fixture_id"] == 1


def test_analyst_max_tokens_default_is_4000(monkeypatch):
    monkeypatch.delenv("ANALYST_MAX_TOKENS", raising=False)
    assert analysts.analyst_max_tokens() == 4000


def test_coordinator_must_not_improvise_after_failed_analyses(monkeypatch):
    monkeypatch.setenv("ORCHESTRATION_MODE", "analysts")
    p = prompts.build_system_prompt()
    assert "NEVER compensate for failed analyses" in p
    assert "how many matches could not be analyzed" in p
    assert "preseason-friendly" in p
    assert "target_selections" in p
    assert "honesty.user_message" in p
    assert "bookmakers have not published odds" in p


def test_data_gaps_stringified_json_array_is_accepted():
    """Analistul a dublu-serializat lista; nu retry, parseaza-o."""
    payload = _analysis_payload(["Gazdele au 4W din 5 acasă"])
    payload["data_gaps"] = (
        '[\n  "Sezonul 2026 abia a început; Osasuna nu are statistici oficiale.",\n'
        '  "Lipsesc informații despre formația de start."\n]'
    )
    ok = analysts.MatchAnalysis.model_validate(payload)
    assert len(ok.data_gaps) == 2
    assert "Sezonul 2026" in ok.data_gaps[0]


def test_market_probs_consistency_warns_but_does_not_reject():
    bad = analysts.market_probs_consistency_warnings({
        "home": 0.9, "draw": 0.2, "away": 0.2,
        "over25": 0.8, "under25": 0.1,
        "btts_yes": 0.7, "btts_no": 0.1,
        "dc_home_draw": 0.5,
    })
    assert any("1X2" in w for w in bad)
    assert any("O/U 2.5" in w for w in bad)
    assert any("BTTS" in w for w in bad)
    assert any("DC 1X" in w for w in bad)

    good = analysts.market_probs_consistency_warnings({
        "home": 0.45, "draw": 0.28, "away": 0.27,
        "over25": 0.52, "under25": 0.48,
        "btts_yes": 0.50, "btts_no": 0.50,
        "dc_home_draw": 0.73,
    })
    assert good == []


def test_double_chance_is_derived_from_1x2_not_the_model():
    derived = analysts.derive_double_chance_probs(
        {"home": 0.45, "draw": 0.28, "away": 0.27, "dc_home_draw": 0.10})
    assert derived["dc_home_draw"] == pytest.approx(0.73, abs=0.0001)
    assert derived["dc_draw_away"] == pytest.approx(0.55, abs=0.0001)
    assert derived["dc_home_away"] == pytest.approx(0.72, abs=0.0001)
    prompt = analysts.build_analyst_prompt({
        "double_chance": {"Home/Draw": 1.4},
        "1X2": {"Home": 2.0, "Draw": 3.0, "Away": 4.0},
    })
    _, _, allowed_list = prompt.partition("allowed_prob_keys:")
    assert "dc_home_draw" not in allowed_list
    assert "derived in code from 1X2" in analysts._ANALYST_SYSTEM_PROMPT


async def test_inconsistent_probs_downgrade_confidence_not_fail(no_http, monkeypatch):
    """O/U sau BTTS strâmbe coboară încrederea; 1X2 se normalizează, nu eșuează."""
    await db.init_db()
    await _seed_fixture(1, _today())
    await db.budget_add(_today(), 50)

    blob = json.loads(_valid_analysis_json(1))
    blob["confidence"] = "high"
    blob["market_probs"] = {"home": 0.45, "draw": 0.28, "away": 0.27,
                            "over25": 0.8, "under25": 0.1, "btts_yes": 0.5}

    async def fake_llm(system, user):
        return json.dumps(blob), _fake_usage()

    monkeypatch.setattr(analysts, "_call_analyst_llm", fake_llm)
    res = await analysts.analyze_match(1)
    assert res.get("analysis_failed") is not True
    assert res["confidence"] == "low"


async def test_1x2_sum_over_one_is_normalized_not_failed(no_http, monkeypatch):
    """Cazul din producție: home+draw+away=1.07 — renormalizat, DC din 1X2."""
    await db.init_db()
    await _seed_fixture(1, _today())
    await db.budget_add(_today(), 50)

    blob = json.loads(_valid_analysis_json(1))
    blob["confidence"] = "high"
    blob["market_probs"] = {
        "home": 0.50, "draw": 0.30, "away": 0.27,
        "over25": 0.55, "under25": 0.45, "btts_yes": 0.5,
        "dc_home_draw": 0.40,
    }

    async def fake_llm(system, user):
        return json.dumps(blob), _fake_usage()

    monkeypatch.setattr(analysts, "_call_analyst_llm", fake_llm)
    res = await analysts.analyze_match(1)
    assert res.get("analysis_failed") is not True
    assert res["confidence"] == "high"
    mp = res["market_probs"]
    assert mp["home"] + mp["draw"] + mp["away"] == pytest.approx(1.0, abs=0.001)
    assert mp["dc_home_draw"] == pytest.approx(mp["home"] + mp["draw"], abs=0.0001)
    assert mp["dc_home_draw"] <= 1.0


def test_1x2_normalized_before_double_chance():
    derived = analysts.derive_double_chance_probs(
        {"home": 0.50, "draw": 0.30, "away": 0.27, "dc_home_draw": 0.10})
    assert derived["home"] + derived["draw"] + derived["away"] == pytest.approx(1.0, abs=0.001)
    assert derived["dc_home_draw"] == pytest.approx(
        derived["home"] + derived["draw"], abs=0.0001)
    assert derived["dc_draw_away"] == pytest.approx(
        derived["draw"] + derived["away"], abs=0.0001)
    assert derived["dc_home_away"] == pytest.approx(
        derived["home"] + derived["away"], abs=0.0001)


async def test_inconsistent_double_chance_does_not_downgrade_when_1x2_ok(no_http, monkeypatch):
    """DC greșit de la model e rescris din 1X2 — nu mai degradează analize bune."""
    await db.init_db()
    await _seed_fixture(1, _today())
    await db.budget_add(_today(), 50)

    blob = json.loads(_valid_analysis_json(1))
    blob["confidence"] = "high"
    blob["market_probs"] = {
        "home": 0.45, "draw": 0.28, "away": 0.27,
        "over25": 0.55, "under25": 0.45, "btts_yes": 0.5,
        "dc_home_draw": 0.10, "dc_draw_away": 0.10, "dc_home_away": 0.10,
    }
    blob["best_candidates"] = [{
        "market": "double_chance", "pick": "home/draw", "odds": 1.40,
        "prob": 0.10, "reason": "Gazdele au 4W din 5 acasă (11-3)",
    }]

    async def fake_llm(system, user):
        return json.dumps(blob), _fake_usage()

    monkeypatch.setattr(analysts, "_call_analyst_llm", fake_llm)
    res = await analysts.analyze_match(1)
    assert res.get("analysis_failed") is not True
    assert res["confidence"] == "high"
    assert res["market_probs"]["dc_home_draw"] == pytest.approx(0.73, abs=0.0001)
    assert res["best_candidates"][0]["prob"] == pytest.approx(0.73, abs=0.0001)


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
