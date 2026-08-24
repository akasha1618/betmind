"""Apeluri Anthropic rezistente la diferente de SDK (parametri nesuportati)."""

from __future__ import annotations

import inspect
import logging
import re
from typing import Any, Callable

log = logging.getLogger("betmind.llm")

_UNEXPECTED_KW = re.compile(r"unexpected keyword argument ['\"](\w+)['\"]")


def supported_kwargs(fn: Callable, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Pastreaza doar argumentele acceptate de semnatura lui `fn`."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return dict(kwargs)
    params = sig.parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return dict(kwargs)
    return {k: v for k, v in kwargs.items() if k in params}


async def messages_create(client: Any, **kwargs: Any) -> Any:
    """client.messages.create, ignorand parametrii pe care SDK-ul nu ii are.

    Un argument optional (ex. temperature) nu are voie sa pice toata tura.
    """
    create = client.messages.create
    pending = supported_kwargs(create, kwargs)
    last_error: TypeError | None = None
    for _ in range(8):
        try:
            return await create(**pending)
        except TypeError as e:
            last_error = e
            name = _unexpected_kwarg_name(e)
            if not name or name not in pending:
                raise
            log.warning("SDK Anthropic nu accepta %s — reiau fara el", name)
            pending = {k: v for k, v in pending.items() if k != name}
    raise last_error or TypeError("messages.create: parametri incompatibili")


def _unexpected_kwarg_name(exc: TypeError) -> str | None:
    m = _UNEXPECTED_KW.search(str(exc))
    return m.group(1) if m else None
