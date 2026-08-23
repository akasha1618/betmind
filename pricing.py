"""
Costul apelurilor Claude — folosit de modul dezvoltator din UI.

Preturile sunt in USD per 1 MILION de tokeni si sunt grupate pe familie de
model (potrivire dupa subsir in numele modelului, ca sa supravietuiasca
schimbarilor de versiune). Daca apare un model necunoscut, folosim tariful
implicit si marcam rezultatul ca aproximativ.

Nota: tokenii de intrare ai unei ture includ DEJA tot istoricul retrimis
modelului, deci suma pe turn_id = costul real al intrebarii, cu tot cu context.
"""

from __future__ import annotations

from typing import Any, Optional

# (input, output, cache_write, cache_read) — USD / 1M tokeni
_PRICES: dict[str, tuple[float, float, float, float]] = {
    "opus": (15.0, 75.0, 18.75, 1.50),
    "sonnet": (3.0, 15.0, 3.75, 0.30),
    "haiku": (1.0, 5.0, 1.25, 0.10),
}
_DEFAULT = _PRICES["sonnet"]


def prices_for(model: str) -> tuple[tuple[float, float, float, float], bool]:
    """Tarifele modelului + daca sunt cunoscute exact."""
    name = (model or "").lower()
    for key, price in _PRICES.items():
        if key in name:
            return price, True
    return _DEFAULT, False


def cost_of(model: str, input_tokens: Optional[int], output_tokens: Optional[int],
            cache_read_tokens: Optional[int] = None,
            cache_write_tokens: Optional[int] = None) -> float:
    (p_in, p_out, p_write, p_read), _ = prices_for(model)
    return (
        (input_tokens or 0) * p_in
        + (output_tokens or 0) * p_out
        + (cache_write_tokens or 0) * p_write
        + (cache_read_tokens or 0) * p_read
    ) / 1_000_000


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Agrega randuri din usage_log intr-un raport de cost.
    Fiecare rand: model, input_tokens, output_tokens, cache_read_tokens,
    cache_write_tokens.
    """
    per_model: dict[str, dict[str, Any]] = {}
    exact = True
    for r in rows:
        model = r.get("model") or "necunoscut"
        _, known = prices_for(model)
        exact = exact and known
        m = per_model.setdefault(model, {
            "model": model, "calls": 0, "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0,
        })
        m["calls"] += 1
        for field in ("input_tokens", "output_tokens", "cache_read_tokens",
                      "cache_write_tokens"):
            m[field] += int(r.get(field) or 0)
        m["cost_usd"] += cost_of(model, r.get("input_tokens"), r.get("output_tokens"),
                                 r.get("cache_read_tokens"), r.get("cache_write_tokens"))

    models = []
    for m in per_model.values():
        models.append({**m, "cost_usd": round(m["cost_usd"], 6)})
    models.sort(key=lambda m: m["cost_usd"], reverse=True)

    total_cost = sum(m["cost_usd"] for m in models)
    return {
        "calls": sum(m["calls"] for m in models),
        "input_tokens": sum(m["input_tokens"] for m in models),
        "output_tokens": sum(m["output_tokens"] for m in models),
        "cache_read_tokens": sum(m["cache_read_tokens"] for m in models),
        "cache_write_tokens": sum(m["cache_write_tokens"] for m in models),
        "cost_usd": round(total_cost, 6),
        "prices_exact": exact,
        "models": models,
    }
