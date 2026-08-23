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

    # Doua apeluri catre model in aceeasi tura, un singur cost raportat.
    assert usage[0]["calls"] == 2
    assert usage[0]["input_tokens"] == 200 and usage[0]["output_tokens"] == 100
    assert usage[0]["cost_usd"] == pytest.approx(
        pricing.cost_of(agent.MODEL, 200, 100))
    assert api["cost_usd"] == usage[0]["cost_usd"]
    assert api["calls"] == 2


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


def test_prompt_mode_argument_overrides_environment(monkeypatch):
    monkeypatch.setenv("ORCHESTRATION_MODE", "classic")
    assert "analyze_matches" in prompts.build_system_prompt("analysts")
    assert "analyze_matches" not in prompts.build_system_prompt("classic")
    # Fara argument ramane comportamentul din .env (compatibilitate V1-B/C).
    assert "analyze_matches" not in prompts.build_system_prompt()
