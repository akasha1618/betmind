"""Coordonatorul nu mai livreaza silentios un raspuns taiat la max_tokens."""

from __future__ import annotations

import pytest

import agent
from tests.test_v1b import _fake_anthropic, _msg, _text
from tests.test_v1d import _recording_anthropic


@pytest.fixture(autouse=True)
def _clear_streak():
    agent._max_tokens_streak.clear()
    yield
    agent._max_tokens_streak.clear()


def test_coordinator_max_tokens_default_is_model_max(monkeypatch):
    monkeypatch.delenv("MAX_TOKENS", raising=False)
    assert agent.max_tokens() == 128_000
    assert agent.max_tokens() == agent._MAX_TOKENS_CEILING


def test_coordinator_max_tokens_reads_env(monkeypatch):
    monkeypatch.setenv("MAX_TOKENS", "64000")
    assert agent.max_tokens() == 64000
    monkeypatch.setenv("MAX_TOKENS", "2048")
    assert agent.max_tokens() == 2048
    monkeypatch.setenv("MAX_TOKENS", "999999")
    assert agent.max_tokens() == agent._MAX_TOKENS_CEILING


async def test_max_tokens_continues_once_and_concatenates(no_http, monkeypatch):
    await _init()
    captured: list = []
    script = [
        _msg([_text("Prima jumătate ")], "max_tokens"),
        _msg([_text("și continuarea.")], "end_turn"),
    ]
    monkeypatch.setattr(agent, "AsyncAnthropic", _recording_anthropic(script, captured))

    events = []
    messages = [{"role": "user", "content": "bilet cotă 5"}]
    async for ev in agent.run_turn(messages, conversation_id="conv-cont"):
        events.append(ev)

    assert events[-1] == {"type": "done"}
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "Prima jumătate " in text
    assert "și continuarea." in text
    assert agent._TRUNCATED_USER_MSG not in text
    assert len(captured) == 2
    assert captured[0]["max_tokens"] == agent.max_tokens()
    continue_msgs = [
        m for m in messages
        if m["role"] == "user" and isinstance(m.get("content"), list)
        and any(b.get("text") == agent._CONTINUE_PROMPT for b in m["content"]
                if isinstance(b, dict))
    ]
    assert continue_msgs, "mesajul de continuare trebuie sa fie in istoric"
    assert _max_tokens_streak_for("conv-cont") == 0


async def test_max_tokens_after_continue_tells_the_user(no_http, monkeypatch):
    await _init()
    script = [
        _msg([_text("AAAA")], "max_tokens"),
        _msg([_text("BBBB")], "max_tokens"),
    ]
    monkeypatch.setattr(agent, "AsyncAnthropic", _fake_anthropic(script))

    events = []
    messages = [{"role": "user", "content": "bilet"}]
    async for ev in agent.run_turn(messages, conversation_id="conv-cut"):
        events.append(ev)

    assert events[-1] == {"type": "done"}
    deltas = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "AAAA" in deltas
    assert "BBBB" in deltas
    assert agent._TRUNCATED_USER_MSG in deltas
    assert any(e.get("label") == "Răspunsul a fost prea lung…" for e in events
               if e["type"] == "status")
    assert _max_tokens_streak_for("conv-cut") == 1
    # Istoricul nu pretinde ca raspunsul taiat e complet: avertismentul e lipit.
    last_assistant = [m for m in messages if m["role"] == "assistant"][-1]
    joined = "".join(b.get("text", "") for b in last_assistant["content"]
                     if isinstance(b, dict))
    assert agent._TRUNCATED_USER_MSG in joined


async def test_third_consecutive_max_tokens_aborts_without_retrying(no_http, monkeypatch):
    await _init()
    captured: list = []

    def _two_truncations():
        return [
            _msg([_text("lung1")], "max_tokens"),
            _msg([_text("lung2")], "max_tokens"),
        ]

    # Doua ture taiate (cate 2 apeluri: initial + continue).
    script = _two_truncations() + _two_truncations()
    monkeypatch.setattr(agent, "AsyncAnthropic", _recording_anthropic(script, captured))

    async def _run(cid: str, text: str) -> list:
        evs = []
        msgs = [{"role": "user", "content": text}]
        async for ev in agent.run_turn(msgs, conversation_id=cid):
            evs.append(ev)
        return evs

    await _run("conv-loop", "bilet 13 meciuri")
    await _run("conv-loop", "Reușești?")
    assert len(captured) == 4
    assert _max_tokens_streak_for("conv-loop") == 2

    # A treia tura: zero apeluri Claude, mesaj scurt de restrangere.
    third = await _run("conv-loop", "Aștept")
    assert len(captured) == 4
    assert third[-1] == {"type": "done"}
    text = "".join(e["text"] for e in third if e["type"] == "delta")
    assert agent._LOOP_ABORT_MSG in text
    assert _max_tokens_streak_for("conv-loop") == 0


def _max_tokens_streak_for(cid: str) -> int:
    return agent._max_tokens_streak.get(cid, 0)


async def _init():
    import db
    await db.init_db()
