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


def _token_bucket() -> dict[str, Any]:
    return {
        "calls": 0, "input_tokens": 0, "output_tokens": 0,
        "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0,
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Agrega randuri din usage_log intr-un raport de cost.
    Fiecare rand: model, input_tokens, output_tokens, cache_read_tokens,
    cache_write_tokens; optional role, mode, label.
    """
    per_model: dict[str, dict[str, Any]] = {}
    per_role: dict[str, dict[str, Any]] = {}
    calls_detail: list[dict[str, Any]] = []
    exact = True
    turn_mode: Optional[str] = None
    for i, r in enumerate(rows, 1):
        model = r.get("model") or "necunoscut"
        _, known = prices_for(model)
        exact = exact and known
        cost = cost_of(model, r.get("input_tokens"), r.get("output_tokens"),
                       r.get("cache_read_tokens"), r.get("cache_write_tokens"))
        detail = {
            "seq": i,
            "role": r.get("role") or None,
            "mode": r.get("mode") or None,
            "label": r.get("label") or None,
            "model": model,
            "input_tokens": int(r.get("input_tokens") or 0),
            "output_tokens": int(r.get("output_tokens") or 0),
            "cache_read_tokens": int(r.get("cache_read_tokens") or 0),
            "cache_write_tokens": int(r.get("cache_write_tokens") or 0),
            "cost_usd": round(cost, 6),
        }
        calls_detail.append(detail)
        if turn_mode is None and detail["mode"]:
            turn_mode = detail["mode"]

        m = per_model.setdefault(model, {"model": model, **_token_bucket()})
        role_key = detail["role"] or "necunoscut"
        role_row = per_role.setdefault(role_key, {"role": role_key, **_token_bucket()})
        for bucket in (m, role_row):
            bucket["calls"] += 1
            for field in ("input_tokens", "output_tokens", "cache_read_tokens",
                          "cache_write_tokens"):
                bucket[field] += detail[field]
            bucket["cost_usd"] += cost

    models = [{**m, "cost_usd": round(m["cost_usd"], 6)} for m in per_model.values()]
    models.sort(key=lambda m: m["cost_usd"], reverse=True)
    by_role = [{**r, "cost_usd": round(r["cost_usd"], 6)} for r in per_role.values()]
    by_role.sort(key=lambda r: r["cost_usd"], reverse=True)

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
        "by_role": by_role,
        "calls_detail": calls_detail,
        "mode": turn_mode,
    }
