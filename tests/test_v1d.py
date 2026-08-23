"""
Teste de acceptanta V1-D (Feedback + Persistenta conversatiilor & Istoric):
(a) restart de server -> lista de conversatii si mesajele supravietuiesc,
    chatul se reia cu context
(b) POST /api/feedback persista, iar re-ratarea actualizeaza acelasi rand
(c) contract SSE neschimbat: fara conversation_id -> serverul creeaza una
    si o intoarce in primul eveniment "meta"; clientii vechi (session_id)
    functioneaza neschimbat
+ Addition 11: get_my_tickets — bilet salvat intr-o conversatie anterioara
  e listat corect intr-una noua (e2e mock-uit)
+ Addition 12: istoricul incarcat ajunge INTACT la model (tool results
  incluse), fara duplicare
+ Addition 13: plafon MAX_STORED_MESSAGES cu taiere sigura pe perechi
  tool_use/tool_result; stergerea conversatiei NU sterge bilete/feedback
"""

from __future__ import annotations

import json

import httpx
import pytest

import agent
import db
import football_data as fd
import prompts
from tests.test_v1b import _FakeStream, _msg, _seed_fixture, _text, _tool


def _now_iso() -> str:
    return fd.now_local().isoformat(timespec="seconds")


def _recording_anthropic(script: list, captured: list):
    """Fake AsyncAnthropic care inregistreaza kwargs-urile fiecarui apel."""
    class _Messages:
        def stream(self, **kwargs):
            captured.append(kwargs)
            return _FakeStream(script.pop(0))

    class _Client:
        def __init__(self, **kwargs):
            self.messages = _Messages()

    return _Client


def _sse_events(body: str) -> list[dict]:
    return [json.loads(line[6:]) for line in body.split("\n\n")
            if line.startswith("data: ")]


def _finished_ok(events: list[dict]) -> bool:
    """Tura s-a incheiat curat. Dupa 'done' pot urma evenimente aditive
    (ex. 'usage' pentru modul dezvoltator), ignorate de clientii vechi."""
    return any(e["type"] == "done" for e in events) and \
        not any(e["type"] == "error" for e in events)


def _client(main_module) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=main_module.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


# ---------------------------------------------------------------------------
# (a) + (c): meta event, persistenta, restart, reluare cu context
# ---------------------------------------------------------------------------

async def test_conversation_survives_restart_and_resumes_with_context(no_http, monkeypatch):
    import main
    main.SESSIONS.clear()
    await db.init_db()

    captured: list = []
    script = [
        _msg([_text("Salut! Cu ce te ajut azi?")], "end_turn"),
        _msg([_text("Sigur, continuăm de unde am rămas.")], "end_turn"),
    ]
    monkeypatch.setattr(agent, "AsyncAnthropic", _recording_anthropic(script, captured))

    async with _client(main) as client:
        resp = await client.post("/api/chat", json={
            "message": "salut, ce meciuri sunt azi?", "user_key": "u1",
        })
        events = _sse_events(resp.text)

        # (c) fara conversation_id -> serverul creeaza una si o intoarce
        # in PRIMUL eveniment SSE, de tip meta; restul contractului neschimbat.
        assert events[0]["type"] == "meta"
        conv_id = events[0]["conversation_id"]
        assert conv_id
        assert any(e["type"] == "delta" for e in events)
        assert _finished_ok(events)

        # "restart de server": cache-ul din memorie dispare, DB-ul ramane.
        main.SESSIONS.clear()

        listing = (await client.get("/api/conversations",
                                    params={"user_key": "u1"})).json()
        assert [c["id"] for c in listing["conversations"]] == [conv_id]
        assert listing["conversations"][0]["title"] == "salut, ce meciuri sunt azi?"

        detail = (await client.get(f"/api/conversations/{conv_id}")).json()
        assert [(m["role"], m["text"]) for m in detail["messages"]] == [
            ("user", "salut, ce meciuri sunt azi?"),
            ("assistant", "Salut! Cu ce te ajut azi?"),
        ]

        # Reluare: modelul primeste istoricul complet incarcat din DB.
        resp2 = await client.post("/api/chat", json={
            "message": "continuăm?", "conversation_id": conv_id, "user_key": "u1",
        })
        events2 = _sse_events(resp2.text)
        assert events2[0]["type"] == "meta"
        assert events2[0]["conversation_id"] == conv_id

    sent = captured[-1]["messages"]
    assert len(sent) == 3  # user + assistant din DB + noul mesaj user
    assert sent[0] == {"role": "user", "content": "salut, ce meciuri sunt azi?"}
    assert sent[1]["role"] == "assistant"
    assert "continuăm?" in json.dumps(sent[2], ensure_ascii=False)

    # Titlul NU se schimba la mesajele urmatoare (ramane primul mesaj).
    conv = await db.get_conversation(conv_id)
    assert conv["title"] == "salut, ce meciuri sunt azi?"
    assert await db.count_messages(conv_id) == 4


async def test_legacy_session_id_clients_keep_working(no_http, monkeypatch):
    """(c) Clientii vechi trimit doar {session_id, message}: continuitatea
    conversatiei se pastreaza, iar meta intoarce acelasi id."""
    import main
    main.SESSIONS.clear()
    await db.init_db()

    script = [
        _msg([_text("Prima tură.")], "end_turn"),
        _msg([_text("A doua tură.")], "end_turn"),
    ]
    monkeypatch.setattr(agent, "AsyncAnthropic", _recording_anthropic(script, []))

    async with _client(main) as client:
        e1 = _sse_events((await client.post("/api/chat", json={
            "session_id": "legacy-1", "message": "prima"})).text)
        e2 = _sse_events((await client.post("/api/chat", json={
            "session_id": "legacy-1", "message": "a doua"})).text)

    assert e1[0]["conversation_id"] == "legacy-1"
    assert e2[0]["conversation_id"] == "legacy-1"
    assert _finished_ok(e1) and _finished_ok(e2)
    assert await db.count_messages("legacy-1") == 4
    assert (await db.get_conversation("legacy-1"))["user_key"] == "legacy-1"


# ---------------------------------------------------------------------------
# (b) feedback: persistare + re-ratarea actualizeaza acelasi rand
# ---------------------------------------------------------------------------

async def test_feedback_persists_and_rerating_updates_row(no_http):
    import main
    await db.init_db()

    async with _client(main) as client:
        r1 = await client.post("/api/feedback", json={
            "conversation_id": "conv-f", "message_ref": "assistant-2",
            "rating": "up",
        })
        assert r1.status_code == 200
        fid = r1.json()["feedback_id"]

        r2 = await client.post("/api/feedback", json={
            "conversation_id": "conv-f", "message_ref": "assistant-2",
            "rating": "down", "comment": "prea riscant pentru mine",
        })
        assert r2.status_code == 200
        assert r2.json()["feedback_id"] == fid  # ACELASI rand, actualizat

    assert await db.count_feedback() == 1
    conn = await db._connect()
    try:
        cur = await conn.execute("SELECT * FROM feedback")
        row = dict(await cur.fetchone())
    finally:
        await conn.close()
    assert row["rating"] == "down"
    assert row["comment"] == "prea riscant pentru mine"

    # Alt mesaj din aceeasi conversatie = rand separat.
    other = await db.upsert_feedback("conv-f", "assistant-5", "up", None, None, _now_iso())
    assert other["id"] != fid
    assert await db.count_feedback() == 2


async def test_health_reports_conversation_and_feedback_counts(no_http):
    import main
    await db.init_db()
    await db.ensure_conversation("conv-h", "u1", "titlu", _now_iso())
    await db.upsert_feedback("conv-h", "assistant-0", "up", None, None, _now_iso())

    async with _client(main) as client:
        body = (await client.get("/api/health")).json()
    assert body["conversations_count"] == 1
    assert body["feedback_count"] == 1


async def test_delete_conversation_endpoint(no_http, monkeypatch):
    import main
    main.SESSIONS.clear()
    await db.init_db()
    now = _now_iso()
    await db.ensure_conversation("conv-d", "u1", "de sters", now)
    await db.append_messages("conv-d", [{"role": "user", "content": "salut"}], now)
    main.SESSIONS["conv-d"] = [{"role": "user", "content": "salut"}]

    async with _client(main) as client:
        assert (await client.delete("/api/conversations/conv-d")).status_code == 200
        assert (await client.get("/api/conversations/conv-d")).status_code == 404

    assert await db.get_conversation("conv-d") is None
    assert await db.count_messages("conv-d") == 0
    assert "conv-d" not in main.SESSIONS


# ---------------------------------------------------------------------------
# Addition 11: get_my_tickets — bilet dintr-o conversatie veche, listat in una noua
# ---------------------------------------------------------------------------

async def test_ticket_from_previous_conversation_listed_in_new_one_e2e(no_http, monkeypatch):
    import main
    main.SESSIONS.clear()
    await db.init_db()
    now = _now_iso()
    day = fd.today_local().isoformat()
    await _seed_fixture(501, day, "19:30", home=(10, "NEC"), away=(20, "Excelsior"))

    # Conversatia A (anterioara) a salvat un bilet pentru user-ul u7.
    await db.ensure_conversation("conv-A", "u7", "biletul de ieri", now)
    tid = await db.save_ticket("conv-A", {
        "target_odds": 5.0, "total_odds": 5.06, "estimated_probability": 0.21,
        "selections": [{"fixture_id": 501, "market": "1X2", "pick": "1",
                        "odds": 2.30, "prob": 0.48}],
    }, risk_level="mediu", created_at=now)

    # Conversatie NOUA, acelasi user: coordonatorul cheama get_my_tickets.
    script = [
        _msg([_tool("t1", "get_my_tickets", {})], "tool_use"),
        _msg([_text("Ieri ți-am recomandat un bilet cu NEC – Excelsior, cotă 5.06.")],
             "end_turn"),
    ]
    monkeypatch.setattr(agent, "AsyncAnthropic", _recording_anthropic(script, []))

    async with _client(main) as client:
        resp = await client.post("/api/chat", json={
            "message": "ce bilete mi-ai dat?", "user_key": "u7",
        })
    events = _sse_events(resp.text)
    new_conv = events[0]["conversation_id"]
    assert new_conv != "conv-A"
    assert _finished_ok(events)

    # Tool result-ul persistat contine biletul REAL din conversatia anterioara.
    msgs = await db.get_messages(new_conv)
    tool_result = next(
        m for m in msgs
        if m["role"] == "user" and isinstance(m["content"], list)
        and m["content"][0].get("tool_use_id") == "t1"
    )
    payload = json.loads(tool_result["content"][0]["content"])
    assert payload["count"] == 1
    ticket = payload["tickets"][0]
    assert ticket["ticket_id"] == tid
    assert ticket["total_odds"] == 5.06
    assert ticket["risk_level"] == "mediu"
    assert ticket["status"] == "open"
    sel = ticket["selections"][0]
    assert sel["match"] == "NEC – Excelsior"  # nume din join-ul cu fixtures
    assert sel["market"] == "1X2" and sel["pick"] == "1" and sel["odds"] == 2.30


async def test_get_my_tickets_scoped_to_user_and_days(no_http):
    await db.init_db()
    now = _now_iso()
    await db.ensure_conversation("conv-B", "alt-user", "alta persoana", now)
    await db.save_ticket("conv-B", {
        "target_odds": 3.0, "total_odds": 3.1, "estimated_probability": 0.3,
        "selections": [{"fixture_id": 1, "market": "1X2", "pick": "1",
                        "odds": 3.1, "prob": 0.3}],
    }, created_at=now)

    # Biletul altui user nu se vede.
    res = await agent._execute_tool("get_my_tickets", {}, None, "u-necunoscut")
    assert res["count"] == 0
    # Fara user_key -> raspuns onest, fara crash.
    res = await agent._execute_tool("get_my_tickets", {}, None, None)
    assert res["count"] == 0 and "tickets" in res
    # User-ul corect il vede.
    res = await agent._execute_tool("get_my_tickets", {"days": 3}, None, "alt-user")
    assert res["count"] == 1 and res["days"] == 3


# ---------------------------------------------------------------------------
# Addition 12: istoricul incarcat ajunge intact la model, fara duplicare
# ---------------------------------------------------------------------------

async def test_loaded_history_passed_intact_to_model_without_duplication(no_http, monkeypatch):
    import main
    main.SESSIONS.clear()
    await db.init_db()
    now = _now_iso()

    stored = [
        {"role": "user", "content": "bilet cota 5 azi"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "Caut meciurile…"},
            {"type": "tool_use", "id": "tu1", "name": "get_fixtures",
             "input": {"date_from": "2026-08-20"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu1",
             "content": "{\"matches\": [], \"source\": \"local_db\"}"},
        ]},
        {"role": "assistant", "content": [{"type": "text", "text": "Gata biletul!"}]},
    ]
    await db.ensure_conversation("conv-r", "u1", "bilet cota 5 azi", now)
    await db.append_messages("conv-r", stored, now)

    captured: list = []
    script = [_msg([_text("Reiau conversația.")], "end_turn")]
    monkeypatch.setattr(agent, "AsyncAnthropic", _recording_anthropic(script, captured))

    async with _client(main) as client:
        resp = await client.post("/api/chat", json={
            "message": "mai e valabil biletul?",
            "conversation_id": "conv-r", "user_key": "u1",
        })
    assert _finished_ok(_sse_events(resp.text))

    sent = captured[0]["messages"]
    # Istoric intact (tool_use + tool_result incluse) + noul mesaj — fara duplicare.
    assert len(sent) == 4 + 1
    assert sent[:4] == stored
    assert sent[4]["role"] == "user"
    assert "mai e valabil biletul?" in json.dumps(sent[4], ensure_ascii=False)

    # In DB nu s-a duplicat nimic: 4 vechi + user nou + assistant nou.
    assert await db.count_messages("conv-r") == 6


@pytest.mark.parametrize("mode", ["analysts", "classic"])
def test_prompt_has_past_tickets_and_freshness_rules(monkeypatch, mode):
    monkeypatch.setenv("ORCHESTRATION_MODE", mode)
    prompt = prompts.build_system_prompt()
    # Addition 11: raspunde din date reale, nu inventa si nu nega stocarea.
    assert "get_my_tickets" in prompt
    assert "NEVER claim the system doesn't store recommendations" in prompt
    # Addition 12: la reluare dupa >6h, re-interogheaza si anunta scurt.
    assert "6 hours" in prompt
    assert "refreshed the data" in prompt


# ---------------------------------------------------------------------------
# Addition 13: plafon de stocare + stergere fara cascada spre bilete/feedback
# ---------------------------------------------------------------------------

def _turn(i: int) -> list[dict]:
    """O tura completa: user text, assistant cu tool_use, tool_result, raspuns."""
    return [
        {"role": "user", "content": f"întrebarea {i}"},
        {"role": "assistant", "content": [
            {"type": "text", "text": f"Caut ({i})…"},
            {"type": "tool_use", "id": f"tu{i}", "name": "get_fixtures",
             "input": {"date_from": "2026-08-20"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": f"tu{i}", "content": "{}"},
        ]},
        {"role": "assistant", "content": [{"type": "text", "text": f"răspunsul {i}"}]},
    ]


async def test_stored_messages_capped_without_splitting_tool_pairs(no_http, monkeypatch):
    import main
    await db.init_db()
    now = _now_iso()
    await db.ensure_conversation("conv-t", "u1", "lunga", now)
    for i in (1, 2, 3):
        await db.append_messages("conv-t", _turn(i), now)
    assert await db.count_messages("conv-t") == 12

    # Plafon 6: tinta ar cadea in mijlocul turei 2 — taierea avanseaza pana la
    # granita sigura (mesajul user text al turei 3), fara perechi rupte.
    deleted = await db.trim_conversation_messages("conv-t", 6)
    assert deleted == 8
    remaining = await db.get_messages("conv-t")
    assert len(remaining) == 4
    assert remaining[0] == {"role": "user", "content": "întrebarea 3"}
    # Perechea tool_use/tool_result a turei 3 e completa.
    assert remaining[1]["content"][1]["type"] == "tool_use"
    assert remaining[2]["content"][0]["type"] == "tool_result"

    # Sub plafon: nu sterge nimic.
    assert await db.trim_conversation_messages("conv-t", 200) == 0

    # Env-overridable (implicit 200).
    monkeypatch.setenv("MAX_STORED_MESSAGES", "6")
    assert main.max_stored_messages() == 6
    monkeypatch.delenv("MAX_STORED_MESSAGES")
    assert main.max_stored_messages() == 200


async def test_delete_conversation_keeps_tickets_and_feedback(no_http):
    await db.init_db()
    now = _now_iso()
    await db.ensure_conversation("conv-k", "u1", "cu bilet", now)
    await db.append_messages("conv-k", _turn(1), now)
    tid = await db.save_ticket("conv-k", {
        "target_odds": 5.0, "total_odds": 5.1, "estimated_probability": 0.2,
        "selections": [{"fixture_id": 1, "market": "1X2", "pick": "1",
                        "odds": 5.1, "prob": 0.2}],
    }, created_at=now)
    await db.upsert_feedback("conv-k", "assistant-0", "up", "bun bilet", tid, now)

    await db.delete_conversation("conv-k")

    assert await db.get_conversation("conv-k") is None
    assert await db.count_messages("conv-k") == 0
    # Biletele si feedback-ul RAMAN, cu conversation_id pastrat pentru analytics.
    conn = await db._connect()
    try:
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM tickets WHERE conversation_id = 'conv-k'")
        assert (await cur.fetchone())["n"] == 1
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM feedback WHERE conversation_id = 'conv-k'")
        assert (await cur.fetchone())["n"] == 1
    finally:
        await conn.close()
