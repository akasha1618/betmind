"""Bug #1: parametrii analistului trebuie sa fie acceptati de SDK-ul instalat."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from anthropic import AsyncAnthropic

import analysts
from llm_compat import messages_create, supported_kwargs


def test_analyst_create_kwargs_match_installed_sdk():
    """Orice parametru trimis la messages.create trebuie sa existe pe SDK.

    Daca producția instalează o versiune mai veche (temperature lipsă),
    acest test pică la pytest — nu în Railway, la fiecare analiză.
    """
    client = AsyncAnthropic(api_key="test-key-not-used")
    sig = inspect.signature(client.messages.create)
    params = sig.parameters
    intended = set(analysts.analyst_llm_create_kwargs())
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return
    unknown = intended - params.keys()
    assert not unknown, (
        f"SDK-ul Anthropic instalat nu acceptă: {sorted(unknown)}. "
        "Fixează versiunea în requirements.txt sau scoate parametrul."
    )


def test_supported_kwargs_drops_unknown_without_var_keyword():
    def create(*, model: str, max_tokens: int):
        pass

    out = supported_kwargs(create, {
        "model": "x", "max_tokens": 1, "temperature": 0, "tools": [],
    })
    assert out == {"model": "x", "max_tokens": 1}


@pytest.mark.asyncio
async def test_messages_create_retries_without_unexpected_kwarg():
    calls = []

    class _Messages:
        async def create(self, **kwargs):
            calls.append(kwargs)
            if "temperature" in kwargs:
                raise TypeError("AsyncMessages.create() got an unexpected keyword argument 'temperature'")
            return SimpleNamespace(content=[], usage=None, stop_reason="end_turn")

    client = SimpleNamespace(messages=_Messages())
    msg = await messages_create(
        client, model="m", max_tokens=8, temperature=0, messages=[],
    )
    assert msg.stop_reason == "end_turn"
    assert "temperature" not in calls[-1]
    assert len(calls) == 2
