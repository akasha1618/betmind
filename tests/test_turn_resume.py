"""Tura continua dupa deconectarea clientului; reconectarea reia evenimentele."""

from __future__ import annotations

import asyncio
import json

import agent
import db
import football_data as fd
import main
import turns
from tests.test_v1d import _client, _sse_events


async def test_turn_survives_client_disconnect_and_resume(monkeypatch):
    """Fara consumator SSE, tura tot ajunge la capat si se salveaza.

    httpx.ASGITransport buffer-uiește tot body-ul, deci deconectarea se
    simulează prin lipsa unui subscriber — exact ce face Safari în background.
    """
    main.SESSIONS.clear()
    await db.init_db()
    started = asyncio.Event()
    go = asyncio.Event()

    async def slow_turn(*args, **kwargs):
        yield {"type": "delta", "text": "prima "}
        started.set()
        await go.wait()
        yield {"type": "delta", "text": "a doua"}
        yield {"type": "done"}

    monkeypatch.setattr(agent, "run_turn", slow_turn)

    conv_id = "conv-mobile"
    turn_id = "turn-mobile"
    user_key = "u-mobile"
    now = fd.now_local().isoformat(timespec="seconds")
    await db.ensure_conversation(conv_id, user_key, "salut din tren", now)
    history = [{"role": "user", "content": "salut din tren"}]
    await db.append_messages(conv_id, history, now)

    turn = turns.hub.create(turn_id, conv_id, user_key)
    turn.task = asyncio.create_task(main._produce_turn(
        turn, history, start_len=1, first_turn=True, user_text="salut din tren",
        active_mode=None, premium_required=False, conv_id=conv_id,
        user_key=user_key, turn_id=turn_id,
    ))
    await asyncio.wait_for(started.wait(), 5)
    assert not turn.done
    go.set()
    await asyncio.wait_for(turn.task, 5)
    assert turn.done
    assert not turn.cancel_requested

    events = list(turn.events)
    deltas = "".join(e.get("text", "") for e in events if e.get("type") == "delta")
    assert "prima" in deltas and "a doua" in deltas
    assert any(e.get("type") == "done" for e in events)

    msgs = await db.get_messages(conv_id)
    assistant = [m for m in msgs if m["role"] == "assistant"]
    assert assistant, "raspunsul trebuia salvat in DB fara client conectat"
    blob = json.dumps(assistant[-1]["content"], ensure_ascii=False)
    assert "a doua" in blob

    async with _client(main) as client:
        r2 = await client.get(
            f"/api/turns/{turn_id}/stream?after=0&user_key={user_key}")
        replay = _sse_events(r2.text)
        replay_deltas = "".join(
            e.get("text", "") for e in replay if e.get("type") == "delta")
        assert "a doua" in replay_deltas
        assert any(e.get("type") == "done" for e in replay)


async def test_finished_turn_replays_from_db_after_hub_evicted(monkeypatch):
    main.SESSIONS.clear()
    await db.init_db()

    async def quick_turn(*args, **kwargs):
        yield {"type": "delta", "text": "bilet gata"}
        yield {"type": "done"}

    monkeypatch.setattr(agent, "run_turn", quick_turn)

    async with _client(main) as client:
        resp = await client.post("/api/chat", json={"message": "bilet", "user_key": "u2"})
        events = _sse_events(resp.text)
        meta = next(e for e in events if e["type"] == "meta")
        turn_id = meta["turn_id"]
        turns.hub.clear()

        r2 = await client.get(f"/api/turns/{turn_id}/stream?after=0&user_key=u2")
        replay = _sse_events(r2.text)
        snaps = [e for e in replay if e.get("type") == "snapshot"]
        assert snaps and "bilet gata" in snaps[0]["text"]
        assert any(e.get("type") == "done" for e in replay)


async def test_resume_rejects_other_user(monkeypatch):
    main.SESSIONS.clear()
    await db.init_db()

    async def quick_turn(*args, **kwargs):
        yield {"type": "delta", "text": "secret"}
        yield {"type": "done"}

    monkeypatch.setattr(agent, "run_turn", quick_turn)

    async with _client(main) as client:
        resp = await client.post("/api/chat", json={"message": "x", "user_key": "owner"})
        turn_id = next(e["turn_id"] for e in _sse_events(resp.text) if e["type"] == "meta")
        denied = await client.get(f"/api/turns/{turn_id}/stream?user_key=intrus")
        assert denied.status_code == 404
