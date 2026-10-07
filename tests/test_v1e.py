"""
Teste de acceptanta V1-E (control asupra conversatiei + transparenta cost):
(a) Mod avansat ales din interfata, per cerere, indiferent de .env
(b) Premium: cand restrictia e activa, cererea "advanced" cade elegant pe
    modul standard si utilizatorul e anuntat
(c) Editarea unui mesaj trimis rescrie conversatia din acel punct
(d) Oprirea raspunsului pastreaza ce s-a scris, fara perechi tool rupte
(e) Costul turei (cu tot cu istoric) ajunge in SSE si la /api/usage/{turn_id}
(f) Titlu automat pentru conversatie, generat o singura data
(g) Limbaj curat: fara emoji si fara nume interne de instrumente
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

import agent
import db
import football_data as fd
import pricing
import prompts
import titles
from tests.test_v1b import _FakeStream, _msg, _text, _tool
from tests.test_v1d import _client, _recording_anthropic, _sse_events


def _tool_names(kwargs: dict) -> set[str]:
    return {t["name"] for t in kwargs["tools"]}


# ---------------------------------------------------------------------------
# (a) + (b) mod avansat per cerere & restrictia Premium
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("env_mode", ["classic", "analysts"])
async def test_advanced_mode_is_chosen_per_request(no_http, monkeypatch, env_mode):
    """Comutatorul din interfata bate valoarea din .env, in ambele sensuri."""
    import main
    main.SESSIONS.clear()
    await db.init_db()
    monkeypatch.setenv("ORCHESTRATION_MODE", env_mode)

    captured: list = []
    script = [_msg([_text("gata")], "end_turn"), _msg([_text("gata")], "end_turn")]
    monkeypatch.setattr(agent, "AsyncAnthropic", _recording_anthropic(script, captured))

    async with _client(main) as client:
        adv = _sse_events((await client.post("/api/chat", json={
            "message": "bilet cota 5", "user_key": "u1", "mode": "advanced",
        })).text)
        std = _sse_events((await client.post("/api/chat", json={
            "message": "bilet cota 5", "user_key": "u1", "mode": "standard",
        })).text)

    assert "analyze_matches" in _tool_names(captured[0])
    assert "analyze_matches" not in _tool_names(captured[1])
    assert adv[0]["mode"] == "analysts" and std[0]["mode"] == "classic"
    assert adv[0]["premium_required"] is False
    adv_usage = next(e for e in adv if e["type"] == "usage")
    std_usage = next(e for e in std if e["type"] == "usage")
    assert adv_usage["mode"] == "analysts" and std_usage["mode"] == "classic"
    assert std_usage["calls_detail"][0]["role"] == "coordinator"
    assert std_usage["calls_detail"][0]["mode"] == "classic"

    # Promptul urmeaza acelasi mod ca tool-urile.
    assert "analyze_matches" in captured[0]["system"][0]["text"]
    assert "analyze_matches" not in captured[1]["system"][0]["text"]


async def test_advanced_mode_falls_back_to_standard_when_premium_required(no_http, monkeypatch):
    import main
    main.SESSIONS.clear()
    await db.init_db()
    monkeypatch.setenv("PREMIUM_GATING", "true")
    monkeypatch.setenv("ORCHESTRATION_MODE", "analysts")

    captured: list = []
    monkeypatch.setattr(agent, "AsyncAnthropic",
                        _recording_anthropic([_msg([_text("ok")], "end_turn")], captured))

    async with _client(main) as client:
        events = _sse_events((await client.post("/api/chat", json={
            "message": "bilet cota 5", "user_key": "u1", "mode": "advanced",
        })).text)
        cfg = (await client.get("/api/config")).json()

    # Cererea nu esueaza: raspunde in modul standard si semnaleaza restrictia.
    assert events[0]["premium_required"] is True
    assert events[0]["mode"] == "classic"
    assert "analyze_matches" not in _tool_names(captured[0])
    assert cfg["premium_gating"] is True and cfg["premium_active"] is False
    assert cfg["request_limits_enabled"] is False


async def test_premium_gating_is_off_by_default(no_http, monkeypatch):
    """Implicit nu blocam nimic: abonamentele nu sunt inca active."""
    import main
    monkeypatch.delenv("PREMIUM_GATING", raising=False)
    monkeypatch.delenv("REQUEST_LIMITS_ENABLED", raising=False)
    async with _client(main) as client:
        cfg = (await client.get("/api/config")).json()
    assert cfg["premium_gating"] is False
    assert cfg["request_limits_enabled"] is False


# ---------------------------------------------------------------------------
# (c) editarea unui mesaj deja trimis
# ---------------------------------------------------------------------------

async def test_editing_a_message_rewrites_conversation_from_that_point(no_http, monkeypatch):
    import main
    main.SESSIONS.clear()
    await db.init_db()

    captured: list = []
    script = [
        _msg([_text("Prima variantă.")], "end_turn"),
        _msg([_text("A doua întrebare.")], "end_turn"),
        _msg([_text("Variantă nouă.")], "end_turn"),
    ]
    monkeypatch.setattr(agent, "AsyncAnthropic", _recording_anthropic(script, captured))

    async with _client(main) as client:
        conv = _sse_events((await client.post("/api/chat", json={
            "message": "bilet cota 5", "user_key": "u1",
        })).text)[0]["conversation_id"]
        await client.post("/api/chat", json={
            "message": "și unul cu cota 10?", "conversation_id": conv, "user_key": "u1",
        })
        assert await db.count_messages(conv) == 4

        # Utilizatorul editeaza PRIMUL mesaj: tot ce urmeaza dispare.
        await client.post("/api/chat", json={
            "message": "bilet cota 8", "conversation_id": conv, "user_key": "u1",
            "edit_from_index": 0,
        })
        detail = (await client.get(f"/api/conversations/{conv}")).json()

    assert [(m["role"], m["text"]) for m in detail["messages"]] == [
        ("user", "bilet cota 8"),
        ("assistant", "Variantă nouă."),
    ]
    # Modelul primeste conversatia rescrisa, fara urme din varianta veche.
    sent = json.dumps(captured[-1]["messages"], ensure_ascii=False)
    assert "bilet cota 8" in sent
    assert "cota 5" not in sent and "cota 10" not in sent

    # Titlul se reface dupa editarea primului mesaj.
    assert (await db.get_conversation(conv))["title"] == "bilet cota 8"


async def test_editing_a_later_message_keeps_earlier_turns(no_http, monkeypatch):
    import main
    main.SESSIONS.clear()
    await db.init_db()

    captured: list = []
    script = [_msg([_text(f"R{i}")], "end_turn") for i in range(3)]
    monkeypatch.setattr(agent, "AsyncAnthropic", _recording_anthropic(script, captured))

    async with _client(main) as client:
        conv = _sse_events((await client.post("/api/chat", json={
            "message": "prima", "user_key": "u1"})).text)[0]["conversation_id"]
        await client.post("/api/chat", json={
            "message": "a doua", "conversation_id": conv, "user_key": "u1"})
        await client.post("/api/chat", json={
            "message": "a doua, corectata", "conversation_id": conv,
            "user_key": "u1", "edit_from_index": 1})
        detail = (await client.get(f"/api/conversations/{conv}")).json()

    assert [m["text"] for m in detail["messages"]] == [
        "prima", "R0", "a doua, corectata", "R2"]


# ---------------------------------------------------------------------------
# (d) oprirea raspunsului
# ---------------------------------------------------------------------------

def test_stopping_keeps_partial_text_and_never_leaves_a_broken_tool_pair():
    """Butonul Stop: pastram ce s-a scris pe ecran, dar niciodata un mesaj
    care cere un instrument fara raspunsul lui (ar bloca tura urmatoare)."""
    import main

    # 1. Oprit in timp ce scria text (nimic nu apucase sa fie salvat).
    history = [{"role": "user", "content": "bilet cota 5"}]
    main._finalize_interrupted(history, "Caut meciurile de azi…")
    assert history[-1] == {"role": "assistant", "content": [
        {"type": "text", "text": "Caut meciurile de azi…"}]}

    # 2. Oprit imediat dupa ce a cerut un instrument: cererea orfana dispare,
    #    dar textul scris inainte ramane.
    history = [
        {"role": "user", "content": "bilet cota 5"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Mă uit la cote."},
            {"type": "tool_use", "id": "t1", "name": "get_odds", "input": {}}]},
    ]
    main._finalize_interrupted(history, "Mă uit la cote.")
    assert len(history) == 2
    assert history[1]["content"] == [{"type": "text", "text": "Mă uit la cote."}]
    assert "tool_use" not in json.dumps(history)

    # 3. Tura completa: nimic nu se modifica.
    done = [{"role": "user", "content": "salut"},
            {"role": "assistant", "content": [{"type": "text", "text": "Salut!"}]}]
    snapshot = json.loads(json.dumps(done))
    main._finalize_interrupted(done, "Salut!")
    assert done == snapshot


async def test_interrupted_turn_is_persisted_and_next_turn_starts_clean(no_http, monkeypatch):
    """Dupa o oprire, conversatia se reia normal: istoricul salvat e valid."""
    import main
    main.SESSIONS.clear()
    await db.init_db()

    captured: list = []
    monkeypatch.setattr(agent, "AsyncAnthropic",
                        _recording_anthropic([_msg([_text("Continuăm.")], "end_turn")], captured))

    conv = "conv-stop"
    now = fd.now_local().isoformat(timespec="seconds")
    await db.ensure_conversation(conv, "u1", "bilet", now)
    history = [{"role": "user", "content": "bilet cota 5"},
               {"role": "assistant", "content": [
                   {"type": "text", "text": "Caut meciurile…"},
                   {"type": "tool_use", "id": "t1", "name": "get_fixtures", "input": {}}]}]
    await main._persist_turn(conv, history, 0, "Caut meciurile…", False, "bilet cota 5", "t")

    stored = await db.get_messages(conv)
    assert [m["role"] for m in stored] == ["user", "assistant"]
    assert "tool_use" not in json.dumps(stored)

    async with _client(main) as client:
        await client.post("/api/chat", json={
            "message": "continuăm", "conversation_id": conv, "user_key": "u1"})
    assert [m["role"] for m in captured[-1]["messages"]] == ["user", "assistant", "user"]


# ---------------------------------------------------------------------------
# (e) costul turei
# ---------------------------------------------------------------------------

def test_cost_math_and_unknown_model_is_flagged():
    rows = [
        {"model": "claude-sonnet-4-6", "input_tokens": 1_000_000,
         "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0},
        {"model": "claude-sonnet-4-6", "input_tokens": 0,
         "output_tokens": 100_000, "cache_read_tokens": 1_000_000,
         "cache_write_tokens": 0},
    ]
    s = pricing.summarize(rows)
    # 3$ (intrare) + 1.5$ (iesire) + 0.30$ (citire din cache)
    assert s["cost_usd"] == pytest.approx(4.80)
    assert s["calls"] == 2 and s["prices_exact"] is True
    assert s["models"][0]["model"] == "claude-sonnet-4-6"

    # Cache-ul citit e de 10x mai ieftin decat acelasi volum de intrare.
    cached = pricing.cost_of("claude-sonnet-4-6", 0, 0, cache_read_tokens=1_000_000)
    fresh = pricing.cost_of("claude-sonnet-4-6", 1_000_000, 0)
    assert fresh == pytest.approx(cached * 10)

    unknown = pricing.summarize([{"model": "model-viitor", "input_tokens": 1000,
                                  "output_tokens": 0}])
    assert unknown["prices_exact"] is False
    assert unknown["cost_usd"] > 0


def test_summarize_breaks_down_role_mode_and_each_call():
    rows = [
        {"model": "claude-sonnet-4-6", "role": "coordinator", "mode": "classic",
         "label": "runda 1 · tool_use", "input_tokens": 6, "output_tokens": 200,
         "cache_read_tokens": 0, "cache_write_tokens": 85_700},
        {"model": "claude-sonnet-4-6", "role": "coordinator", "mode": "classic",
         "label": "runda 2 · end_turn", "input_tokens": 0, "output_tokens": 3600,
         "cache_read_tokens": 121_300, "cache_write_tokens": 0},
        {"model": "claude-haiku-4-5", "role": "title", "mode": None,
         "label": "titlu conversație", "input_tokens": 400, "output_tokens": 20,
         "cache_read_tokens": 0, "cache_write_tokens": 0},
    ]
    s = pricing.summarize(rows)
    assert s["mode"] == "classic"
    assert s["calls"] == 3
    assert len(s["calls_detail"]) == 3
    assert s["calls_detail"][0]["label"] == "runda 1 · tool_use"
    assert s["calls_detail"][0]["cost_usd"] == pytest.approx(
        pricing.cost_of("claude-sonnet-4-6", 6, 200, 0, 85_700))
    roles = {r["role"]: r for r in s["by_role"]}
    assert roles["coordinator"]["calls"] == 2
    assert roles["title"]["calls"] == 1
    assert roles["coordinator"]["cost_usd"] > roles["title"]["cost_usd"]


async def test_turn_cost_includes_history_and_is_exposed_to_dev_mode(no_http, monkeypatch):
    """Costul intrebarii = toate apelurile turei; tokenii de intrare includ
    deja istoricul retrimis modelului."""
    import main
    main.SESSIONS.clear()
    await db.init_db()

    script = [
        _msg([_tool("t1", "get_fixtures", {"date": "2026-08-23"})], "tool_use"),
        _msg([_text("Gata.")], "end_turn"),
    ]
    monkeypatch.setattr(agent, "AsyncAnthropic", _recording_anthropic(script, []))
    monkeypatch.setattr(agent, "_execute_tool",
                        lambda *a, **k: _async_value({"ok": True}))

    async with _client(main) as client:
        events = _sse_events((await client.post("/api/chat", json={
            "message": "bilet cota 5", "user_key": "u1"})).text)
        usage = [e for e in events if e["type"] == "usage"]
        assert len(usage) == 1
        turn_id = events[0]["turn_id"]
        assert usage[0]["turn_id"] == turn_id

        api = (await client.get(f"/api/usage/{turn_id}")).json()
        sent = (await client.get(f"/api/usage/{turn_id}/payload/0")).json()
        system = (await client.get(f"/api/usage/{turn_id}/payload/system")).json()
        missing = await client.get(f"/api/usage/{turn_id}/payload/9")

    # Dev mode poate deschide exact ce a primit Sonnet ca tool_result.
    assert sent["tool"] == "get_fixtures"
    assert sent["content"] == agent._tool_result_json("get_fixtures", {"ok": True})
    assert sent["full_args"] == {"date": "2026-08-23"}
    assert system["content"] == agent.build_system_prompt(api["mode"])
    assert missing.status_code == 404
    assert "content" not in usage[0]["tools"]["calls"][0]
    assert usage[0]["tools"]["prefix"]["system"]["tokens"] > 100

    # Doua apeluri catre model in aceeasi tura, un singur cost raportat.
    assert usage[0]["calls"] == 2
    assert usage[0]["input_tokens"] == 200 and usage[0]["output_tokens"] == 100
    assert usage[0]["cost_usd"] == pytest.approx(
        pricing.cost_of(agent.MODEL, 200, 100))
    assert api["cost_usd"] == usage[0]["cost_usd"]
    assert api["calls"] == 2
    assert usage[0]["latency_s"] >= 0
    assert api["latency_s"] == pytest.approx(usage[0]["latency_s"])
    assert usage[0]["mode"] in ("classic", "analysts")
    assert api["mode"] == usage[0]["mode"]
    assert len(usage[0]["calls_detail"]) == 2
    assert all(c["role"] == "coordinator" for c in usage[0]["calls_detail"])
    assert usage[0]["calls_detail"][0]["label"]
    assert {r["role"] for r in usage[0]["by_role"]} == {"coordinator"}
    # Ce date de tool-uri a citit Sonnet: get_fixtures, intrat în runda 2.
    tools = usage[0]["tools"]
    assert [t["tool"] for t in tools["by_tool"]] == ["get_fixtures"]
    assert tools["calls"][0]["round"] == 2
    assert tools["calls"][0]["tokens"] >= 1
    assert api["tools"] == tools


async def test_dev_mode_latency_tracks_wall_clock(no_http, monkeypatch):
    """Timpul de raspuns e de la startul turei pana la mesajul final."""
    import main
    main.SESSIONS.clear()
    await db.init_db()

    async def slow_turn(*args, **kwargs):
        await asyncio.sleep(0.08)
        yield {"type": "delta", "text": "gata"}
        yield {"type": "done"}

    monkeypatch.setattr(agent, "run_turn", slow_turn)

    async with _client(main) as client:
        events = _sse_events((await client.post("/api/chat", json={
            "message": "salut", "user_key": "u-lat"})).text)
        usage = [e for e in events if e["type"] == "usage"]
        assert usage and usage[0]["latency_s"] >= 0.07
        turn_id = events[0]["turn_id"]
        api = (await client.get(f"/api/usage/{turn_id}")).json()
        assert api["latency_s"] >= 0.07


def _async_value(value):
    async def _coro():
        return value
    return _coro()


async def test_cost_stays_available_after_reload_via_message_turn_id(no_http, monkeypatch):
    """Modul dezvoltator pornit mai tarziu (sau dupa reincarcare) trebuie sa
    poata arata costul mesajelor deja afisate: fiecare raspuns isi tine
    turn_id-ul."""
    import main
    main.SESSIONS.clear()
    await db.init_db()

    monkeypatch.setattr(agent, "AsyncAnthropic", _recording_anthropic(
        [_msg([_text("Primul")], "end_turn"), _msg([_text("Al doilea")], "end_turn")], []))

    async with _client(main) as client:
        e1 = _sse_events((await client.post("/api/chat", json={
            "message": "prima", "user_key": "u1"})).text)
        conv, turn1 = e1[0]["conversation_id"], e1[0]["turn_id"]
        e2 = _sse_events((await client.post("/api/chat", json={
            "message": "a doua", "conversation_id": conv, "user_key": "u1"})).text)
        turn2 = e2[0]["turn_id"]

        msgs = (await client.get(f"/api/conversations/{conv}")).json()["messages"]
        costs = [(await client.get(f"/api/usage/{t}")).json() for t in (turn1, turn2)]

    assert turn1 != turn2
    assistant = [m for m in msgs if m["role"] == "assistant"]
    assert [m["turn_id"] for m in assistant] == [turn1, turn2]
    # Fiecare tura are costul ei, deci interfata poate reconstrui tot ecranul.
    assert all(c["calls"] == 1 and c["cost_usd"] > 0 for c in costs)


# ---------------------------------------------------------------------------
# (f) titlu automat
# ---------------------------------------------------------------------------

def test_clean_title_trims_quotes_and_length():
    assert titles.clean_title('  "Bilet cota 5 pe azi"  ') == "Bilet cota 5 pe azi"
    assert titles.clean_title("Titlu\nrand doi") == "Titlu"
    assert len(titles.clean_title("x" * 200)) == titles.MAX_TITLE_LEN


async def test_conversation_gets_an_automatic_title_once(no_http, monkeypatch):
    await db.init_db()
    monkeypatch.setenv("AUTO_TITLE_ENABLED", "true")

    calls: list = []

    async def fake_llm(payload):
        calls.append(payload)
        return "Bilet cota 5 pentru azi", SimpleNamespace(input_tokens=40, output_tokens=8)

    monkeypatch.setattr(titles, "_call_llm", fake_llm)

    conv = "conv-title"
    now = fd.now_local().isoformat(timespec="seconds")
    # Titlul provizoriu = inceputul primului mesaj (V1-D).
    await db.ensure_conversation(conv, "u1", "vreau un bilet cu cota 5 din", now)

    await titles.maybe_title_conversation(conv, "vreau un bilet cu cota 5 din meciurile de azi",
                                          "Iată biletul propus…", "turn-1")
    assert (await db.get_conversation(conv))["title"] == "Bilet cota 5 pentru azi"
    assert "meciurile de azi" in calls[0]

    # A doua tura nu mai regenereaza titlul.
    await titles.maybe_title_conversation(conv, "și cu cota 10?", "Sigur.", "turn-2")
    assert len(calls) == 1
    assert (await db.get_conversation(conv))["title"] == "Bilet cota 5 pentru azi"

    # Costul titlului intra in aceeasi tura, ca sa apara in modul dezvoltator.
    assert pricing.summarize(await db.usage_for_turn("turn-1"))["calls"] == 1


async def test_untitled_conversations_get_titled_when_history_is_listed(no_http, monkeypatch):
    """Conversatiile mai vechi (dinainte de titluri automate, sau cu prima tura
    oprita) primesc titlu la deschiderea istoricului, nu raman cu query-ul brut."""
    import main
    await db.init_db()
    monkeypatch.setenv("AUTO_TITLE_ENABLED", "true")

    async def fake_llm(payload):
        return "Meciuri Serie A azi", SimpleNamespace(input_tokens=30, output_tokens=6)

    monkeypatch.setattr(titles, "_call_llm", fake_llm)

    conv = "conv-vechi"
    now = fd.now_local().isoformat(timespec="seconds")
    await db.ensure_conversation(conv, "u-vechi", "ce meciuri sunt azi in Serie A?", now)
    await db.append_messages(conv, [
        {"role": "user", "content": "ce meciuri sunt azi in Serie A?"},
        {"role": "assistant", "content": [{"type": "text", "text": "Azi sunt 4 meciuri."}]},
    ], now)

    async with _client(main) as client:
        first = (await client.get("/api/conversations",
                                  params={"user_key": "u-vechi"})).json()
        assert first["conversations"][0]["title"] == "ce meciuri sunt azi in Serie A?"
        for task in list(main._BACKGROUND):   # lasam titlul de fundal sa termine
            await task
        second = (await client.get("/api/conversations",
                                   params={"user_key": "u-vechi"})).json()

    assert second["conversations"][0]["title"] == "Meciuri Serie A azi"


async def test_title_is_generated_even_if_the_first_turn_was_stopped(no_http, monkeypatch):
    """Prima tura oprita nu lasa conversatia fara titlu: se incearca din nou
    la tura urmatoare."""
    import main
    main.SESSIONS.clear()
    await db.init_db()
    monkeypatch.setenv("AUTO_TITLE_ENABLED", "true")

    async def fake_llm(payload):
        return "Bilet cota 5 azi", SimpleNamespace(input_tokens=30, output_tokens=6)

    monkeypatch.setattr(titles, "_call_llm", fake_llm)
    monkeypatch.setattr(agent, "AsyncAnthropic",
                        _recording_anthropic([_msg([_text("Gata.")], "end_turn")], []))

    conv = "conv-oprit"
    now = fd.now_local().isoformat(timespec="seconds")
    await db.ensure_conversation(conv, "u1", "bilet cota 5 din meciurile de azi", now)
    await db.append_messages(conv, [{"role": "user", "content": "bilet cota 5 din meciurile de azi"}], now)

    async with _client(main) as client:
        await client.post("/api/chat", json={
            "message": "continuăm", "conversation_id": conv, "user_key": "u1"})
        for task in list(main._BACKGROUND):
            await task

    assert (await db.get_conversation(conv))["title"] == "Bilet cota 5 azi"


async def test_auto_title_failure_keeps_the_provisional_title(no_http, monkeypatch):
    await db.init_db()
    monkeypatch.setenv("AUTO_TITLE_ENABLED", "true")

    async def boom(payload):
        raise RuntimeError("model indisponibil")

    monkeypatch.setattr(titles, "_call_llm", boom)

    conv = "conv-title-fail"
    now = fd.now_local().isoformat(timespec="seconds")
    await db.ensure_conversation(conv, "u1", "primul mesaj", now)
    await titles.maybe_title_conversation(conv, "primul mesaj", "raspuns")
    assert (await db.get_conversation(conv))["title"] == "primul mesaj"


async def test_auto_title_can_be_disabled(no_http, monkeypatch):
    await db.init_db()
    monkeypatch.setenv("AUTO_TITLE_ENABLED", "false")

    async def fail(payload):
        raise AssertionError("nu ar trebui apelat modelul")

    monkeypatch.setattr(titles, "_call_llm", fail)
    assert await titles.generate_title("ceva") == ""


# ---------------------------------------------------------------------------
# (g) limbaj curat: fara emoji, fara nume interne de instrumente
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["analysts", "classic"])
def test_prompt_forbids_dev_jargon_but_keeps_emoji(monkeypatch, mode):
    monkeypatch.setenv("ORCHESTRATION_MODE", mode)
    prompt = prompts.build_system_prompt()

    assert "NO INTERNAL JARGON" in prompt
    # Exemplul negativ vizeaza exact greseala raportata de utilizator.
    assert "algoritmul build_ticket optimizează" in prompt
    assert "sistemul care compune biletul" in prompt
    # Emoji-urile RAMAN: increderea se arata cu stele, ca la inceput.
    assert "⭐⭐⭐" in prompt
    assert "NO EMOJI" not in prompt
    # Bold-ul stricat ("**text **") nu se randeaza — modelul e avertizat.
    assert "MARKDOWN HYGIENE" in prompt


def test_llm_tool_payload_keeps_decision_fields_and_drops_duplicates():
    """Sonnet vede aceleași meciuri/cote, fără duplicate care umflă cache-ul."""
    fat_odds = {
        "bookmaker": "Bet365",
        "1X2": {"Home": 1.80},
        "over_under": {"Over 2.5": 1.90},
        "btts": {"Yes": 1.85},
        "double_chance": {"Home/Draw": 1.25},
        "markets": [{
            "key": "1x2",
            "name": "Match Winner",
            "outcomes": [{
                "value": "Home", "avg_odd": 1.82, "best_odd": 1.90,
                "n_books": 6, "reference_odd": 1.80, "display_odd": 1.70,
                "best_bookmaker": "Unibet", "display_bookmaker": "Superbet",
                "odds_label": "1.70",
            }],
        }],
        "truncated": False,
    }
    slim = agent.compact_tool_result("get_odds", fat_odds)
    market = slim["markets"]["1x2"]
    # [avg_odd, display_odd, best_odd] — best rămâne când diferă cu ≥ 0.05.
    assert market["o"]["Home"] == [1.82, 1.70, 1.90]
    assert market["n"] == 6
    assert slim["row"] == agent.ODDS_ROW_FORMAT
    assert "1X2" not in slim  # legacy e duplicat față de markets
    dumped = agent._tool_result_json("get_odds", fat_odds)
    for gone in ("odds_label", "reference_odd", "display_bookmaker", "Superbet",
                 "Match Winner", "best_bookmaker"):
        assert gone not in dumped
    assert len(dumped) < len(json.dumps(fat_odds, ensure_ascii=False)) / 2
    sections = agent._sections_breakdown("get_odds", dumped)
    assert [s["key"] for s in sections] == ["1x2"] and sections[0]["outcomes"] == 1

    close = {"markets": [{"key": "btts", "outcomes": [
        {"value": "Yes", "avg_odd": 1.80, "display_odd": 1.83, "best_odd": 1.85}]}]}
    assert agent.compact_tool_result("get_odds", close)["markets"]["btts"]["o"]["Yes"] == [1.80, 1.83]

    fat_fx = {
        "count": 2, "listed": 2, "by_league": {"PL (id 39)": 2},
        "source": "local_db",
        "timezone": "Europe/Bucharest",
        "matches_per_day": {"2026-10-11": 2},
        "api_requests_remaining_today": 42,
        "note": "Toate datele si orele sunt LOCALE Romania " * 8,
        "days": {"2026-10-11": {"source": "local_db", "stale": False,
                                "last_synced_at": "2026-10-07T12:00:00"}},
        "fixtures": [{
            "fixture_id": 1, "date": "2026-10-11", "weekday": "sâmbătă",
            "time": "14:30", "kickoff": "2026-10-11T14:30:00+03:00",
            "status": "NS", "status_group": "upcoming",
            "league": "Premier League", "league_id": 39, "season": 2026,
            "round": "Regular Season - 8",
            "home": {"id": 42, "name": "Arsenal"},
            "away": {"id": 63, "name": "Leeds"},
            "score": None,
        }],
    }
    fx = agent.compact_tool_result("get_fixtures", fat_fx)
    assert fx["count"] == 2 and len(fx["fixtures"]) == 1
    # [fixture_id, weekday, kickoff, league_id, season, home_id, home, away_id, away]
    assert fx["fixtures"][0] == [1, "sâmbătă", "2026-10-11 14:30", 39, 2026,
                                 42, "Arsenal", 63, "Leeds"]
    assert fx["leagues"] == {"39": "Premier League"}
    assert fx["row"] == agent.FIXTURE_ROW_FORMAT
    assert "note" not in fx and "matches_per_day" not in fx
    assert "stale_days" not in fx

    finished = dict(fat_fx["fixtures"][0], fixture_id=2, status="FT",
                    status_group="finished", score="2-1")
    fx2 = agent.compact_tool_result("get_fixtures", dict(fat_fx, fixtures=[finished]))
    assert fx2["fixtures"][0][-1] == "FT"  # status doar pentru meciuri care nu sunt upcoming

    big = dict(fat_fx, fixtures=fat_fx["fixtures"] * 40)
    old_style = json.dumps(big["fixtures"], ensure_ascii=False, separators=(",", ":"))
    new_style = json.dumps(agent.compact_tool_result("get_fixtures", big)["fixtures"],
                           ensure_ascii=False, separators=(",", ":"))
    assert len(new_style) < len(old_style) / 2


def test_classic_prompt_batches_research_in_one_step(monkeypatch):
    monkeypatch.setenv("ORCHESTRATION_MODE", "classic")
    prompt = prompts.build_system_prompt()
    assert "ONE batch" in prompt
    assert "analyze_matches" not in prompt


@pytest.mark.parametrize("mode", ["classic", "analysts"])
def test_prompt_builds_the_ticket_in_one_call(mode):
    """Fără reconstruiri pentru «varietate»: fiecare rundă refacturează conversația."""
    prompt = prompts.build_system_prompt(mode)
    assert "BUILD THE TICKET IN ONE CALL" in prompt
    assert "Rebuild AT MOST ONCE" in prompt
    assert "{_BUILD_ONCE}" not in prompt


def test_logs_redact_api_keys_in_urls():
    import logging
    from logging_config import RedactSecretsFilter

    record = logging.LogRecord(
        "httpx", logging.INFO, __file__, 1, 'HTTP Request: %s %s "%s"',
        ("GET", "https://api.oddspapi.io/v4/odds?apiKey=6c183127-secret&fixtureId=id1",
         "HTTP/1.1 200 OK"), None)
    RedactSecretsFilter().filter(record)
    msg = record.getMessage()
    assert "6c183127-secret" not in msg
    assert "apiKey=***" in msg and "fixtureId=id1" in msg


def test_prompt_mode_argument_overrides_environment(monkeypatch):
    monkeypatch.setenv("ORCHESTRATION_MODE", "classic")
    assert "analyze_matches" in prompts.build_system_prompt("analysts")
    assert "analyze_matches" not in prompts.build_system_prompt("classic")
    # Fara argument ramane comportamentul din .env (compatibilitate V1-B/C).
    assert "analyze_matches" not in prompts.build_system_prompt()


def test_old_tool_results_are_stubbed_before_the_current_turn():
    """Follow-up-ul nu retrimite JSON-ul de 50k tokeni din tura anterioara."""
    fat = json.dumps({"fixtures": [{"fixture_id": i} for i in range(120)]})
    history = [
        {"role": "user", "content": "bilet azi"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Caut…"},
            {"type": "tool_use", "id": "t1", "name": "get_fixtures", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": fat},
        ]},
        {"role": "assistant", "content": [{"type": "text", "text": "Nu sunt meciuri azi."}]},
        {"role": "user", "content": "atunci pe saptamana viitoare"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t2", "name": "get_fixtures", "input": {}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t2", "content": '{"count":3}'},
        ]},
    ]
    compact = agent.compact_history_for_llm(history)
    old = compact[2]["content"][0]["content"]
    assert old == agent._COMPACTED_TOOL_RESULT
    assert "fixture_id" not in old
    # Tura curenta (dupa ultimul mesaj user-text) ramane intreaga.
    assert compact[6]["content"][0]["content"] == '{"count":3}'
    # Istoricul original nu e mutat — persistarea pastreaza datele.
    assert "fixture_id" in history[2]["content"][0]["content"]

    _, _, msgs = agent._with_cache_markers("SYS", agent.build_tools("classic"), history)
    assert msgs[2]["content"][0]["content"] == agent._COMPACTED_TOOL_RESULT
    assert msgs[-1]["content"][-1]["cache_control"] == {"type": "ephemeral"}


def test_fixture_list_round_robins_leagues_instead_of_dumping_120():
    rows = []
    for lid, n in ((39, 40), (140, 40), (283, 40)):
        for i in range(n):
            rows.append({
                "fixture_id": lid * 100 + i,
                "league_id": lid,
                "status_group": "upcoming",
                "kickoff": f"2026-10-13T{18 + i % 4}:00:00+03:00",
            })
    listed = fd.prioritize_fixtures_for_llm(rows, cap=12)
    assert len(listed) == 12
    by_lg = {}
    for f in listed:
        by_lg[f["league_id"]] = by_lg.get(f["league_id"], 0) + 1
    assert by_lg[39] == 4 and by_lg[140] == 4 and by_lg[283] == 4
