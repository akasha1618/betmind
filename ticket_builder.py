"""
Constructia deterministic-matematica a biletului.

Problema: atinge cota tinta maximizand probabilitatea totala a biletului.
- probabilitatea biletului = produsul probabilitatilor selectiilor
- cota biletului         = produsul cotelor selectiilor

Ranking (V1-E): value_ratio × (1 + EDGE_WEIGHT × max(edge, 0)) × confidence.
Boost-ul decide DOAR ce intra pe bilet — probabilitatea afisata ramane
produsul brut al `prob`, niciodata scorul umflat.
"""

from __future__ import annotations

import itertools
import math
import os
import re
from collections import Counter
from typing import Any, Optional

_CONF_MULT = {"high": 1.15, "medium": 1.0, "low": 0.85}
_CLOSE_SCORE = 0.05  # 5% — la scoruri apropiate preferam liga/ora diferite


def edge_weight() -> float:
    try:
        return float(os.environ.get("EDGE_WEIGHT", "2.0"))
    except ValueError:
        return 2.0


def max_same_market_family_default() -> int:
    try:
        return max(1, int(os.environ.get("MAX_SAME_MARKET_FAMILY", "2")))
    except ValueError:
        return 2


def _norm_prob(p: float) -> float:
    return min(max(float(p), 0.01), 0.97)


def market_family(market: str, pick: str = "") -> str:
    """Familia de piata a unei selectii (diversitate)."""
    m = (market or "").strip().lower()
    p = (pick or "").strip().lower()

    if any(x in m for x in ("1x2_ht", "over_under_ht", "btts_ht", "first_to_score",
                             "htft", "first half", "1st half", "ht/ft")):
        return "half-time"
    if m.endswith("_ht") or m.startswith("ht_"):
        return "half-time"

    if any(x in m for x in ("double_chance", "double chance", "dc_")):
        return "double_chance"
    if p in ("1x", "x2", "12", "home/draw", "draw/away", "home/away"):
        return "double_chance"

    if any(x in m for x in ("asian", "handicap", "ah_")):
        return "handicap"

    if any(x in m for x in ("team_total", "team_scores", "team to score",
                             "team total", "score a goal")):
        return "team-based"

    if any(x in m for x in ("btts", "both teams", "gg")):
        return "btts"

    if any(x in m for x in ("over", "under", "over_under")):
        return "goals"

    if any(x in m for x in ("1x2", "match winner", "full time")):
        return "result"
    if p in ("1", "x", "2", "home", "draw", "away"):
        return "result"
    return "other"


def _kickoff_slot(kickoff: Any) -> str:
    s = str(kickoff or "")
    if "T" in s:
        return s[:13]  # YYYY-MM-DDTHH
    return s[:16]


def _league_of(item: dict) -> Any:
    return item.get("league") or item.get("league_name") or item.get("league_id")


def _fixture_key(item: dict) -> Any:
    return item.get("fixture_id") or item.get("match")


def _ranking_score(item: dict) -> float:
    odds, prob = item["odds"], item["prob"]
    value_ratio = math.log(odds) / (-math.log(prob))
    edge = max(float(item.get("edge") or 0.0), 0.0)
    conf = _CONF_MULT.get(str(item.get("confidence") or "medium").lower(), 1.0)
    return value_ratio * (1.0 + edge_weight() * edge) * conf


def _ticket_log_odds(items: list[dict]) -> float:
    return sum(math.log(i["odds"]) for i in items)


def _apply_diversity_swaps(picked: list[dict], unused: list[dict],
                           target_log: float, max_same: int) -> tuple[list[dict], list[str]]:
    """Daca o familie depaseste plafonul, inlocuieste cea mai slaba selectie
    cu cel mai bun candidat dintr-o familie sub-reprezentata, pastrand tinta."""
    warnings: list[str] = []
    picked = list(picked)
    unused = list(unused)

    def counts() -> Counter:
        return Counter(market_family(s.get("market", ""), s.get("pick", "")) for s in picked)

    while True:
        c = counts()
        overflow = [f for f, n in c.items() if n > max_same]
        if not overflow:
            break
        fam = overflow[0]
        in_fam = [s for s in picked if market_family(s.get("market", ""), s.get("pick", "")) == fam]
        weakest = min(in_fam, key=lambda x: x["_score"])
        under = {f for f, n in c.items() if n < max_same}
        # si familiile care inca nu sunt pe bilet
        for u in unused:
            under.add(market_family(u.get("market", ""), u.get("pick", "")))
        under.discard(fam)

        used = {_fixture_key(s) for s in picked if s is not weakest}
        candidates = [
            u for u in unused
            if _fixture_key(u) not in used
            and market_family(u.get("market", ""), u.get("pick", "")) in under
        ]
        candidates.sort(key=lambda x: x["_score"], reverse=True)

        swapped = False
        for cand in candidates:
            trial = [s for s in picked if s is not weakest] + [cand]
            if _ticket_log_odds(trial) >= target_log:
                picked.remove(weakest)
                picked.append(cand)
                unused.remove(cand)
                unused.append(weakest)
                swapped = True
                break
        if not swapped:
            warnings.append(
                f"Nu am putut reduce familia '{fam}' sub {max_same} selectii "
                f"fara sa pierd cota tinta — pastrez biletul cum e."
            )
            break
    return picked, warnings


def _opt_int(value: Any) -> Optional[int]:
    if value is None or value is False:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def parse_selection_request(text: str) -> dict[str, int]:
    """Extrage din mesajul userului cate selectii vrea pe bilet.

    «bilet cu cota 5» NU e numar de selectii. «5 selecții» / «mai multe
    meciuri» da. Daca nu e nimic, dict gol — comportamentul greedy ramane.
    """
    if not text or not str(text).strip():
        return {}
    t = str(text).lower()
    numbered = re.search(
        r"\b(\d{1,2})\s*(?:selec(?:ț|t)i[ei]?|meciuri|picks?|leg(?:ă|a)turi)\b",
        t,
    )
    if not numbered:
        numbered = re.search(
            r"\bbilet(?:ul)?\s+cu\s+(\d{1,2})\s*(?:selec|meci)",
            t,
        )
    if numbered:
        n = int(numbered.group(1))
        if 2 <= n <= 15:
            return {"target_selections": n}
    if re.search(
        r"mai multe meciuri|mai multe selec|prea pu(?:ț|t)ine|"
        r"prea scurt|mai lung pe bilet",
        t,
    ):
        return {"min_selections": 5}
    return {}


def _pick_n_near_target(pool: list[dict], n: int, target_odds: float) -> list[dict]:
    """Cele mai bune N selectii cu cota >= tinta, cat mai aproape de tinta."""
    if n >= len(pool):
        return list(pool)
    target = max(float(target_odds), 1.02)
    best: Optional[tuple] = None
    chosen: list[dict] = list(pool[:n])
    for combo in itertools.combinations(pool, n):
        odds = math.exp(_ticket_log_odds(combo))
        score_sum = sum(x["_score"] for x in combo)
        if odds >= target * 0.999:
            key = (0, odds - target, -score_sum)
        else:
            key = (1, target - odds, -score_sum)
        if best is None or key < best:
            best = key
            chosen = list(combo)
    return chosen


def build_ticket(candidates: list[dict[str, Any]], target_odds: float,
                 max_selections: int = 15,
                 excluded_fixture_ids: Optional[list[int]] = None,
                 max_same_market_family: Optional[int] = None,
                 prefer_league_diversity: bool = True,
                 min_selections: Optional[int] = None,
                 target_selections: Optional[int] = None) -> dict[str, Any]:
    warnings: list[str] = []
    excluded = set(excluded_fixture_ids or [])
    max_same = (max_same_market_family if max_same_market_family is not None
                else max_same_market_family_default())

    # 1) Validare + imbogatire candidati (fixture-urile excluse ies din start)
    valid: list[dict] = []
    for c in candidates:
        if excluded and c.get("fixture_id") in excluded:
            continue
        try:
            odds = float(c["odds"])
            prob = _norm_prob(c["prob"])
        except (KeyError, TypeError, ValueError):
            warnings.append(f"Candidat ignorat (odds/prob invalide): {c.get('match', '?')}")
            continue
        if odds <= 1.01:
            warnings.append(f"Candidat ignorat (cota <= 1.01): {c.get('match', '?')}")
            continue
        item = dict(c)
        item["odds"], item["prob"] = odds, prob
        if item.get("implied_prob") is None:
            item["implied_prob"] = round(1.0 / odds, 3)
        else:
            try:
                item["implied_prob"] = round(float(item["implied_prob"]), 3)
            except (TypeError, ValueError):
                item["implied_prob"] = round(1.0 / odds, 3)
        if item.get("edge") is None:
            item["edge"] = round(prob - float(item["implied_prob"]), 3)
        else:
            try:
                item["edge"] = round(float(item["edge"]), 3)
            except (TypeError, ValueError):
                item["edge"] = round(prob - float(item["implied_prob"]), 3)
        if prob > float(item["implied_prob"]) * 1.4:
            item["flag"] = "estimare mult peste piata - verifica"
        item["_score"] = _ranking_score(item)
        valid.append(item)

    if not valid:
        return {"ok": False, "error": "Niciun candidat valid.", "warnings": warnings}

    # 2) Maxim o selectie per meci: pastram cea cu cel mai bun scor (convingere)
    best_per_fixture: dict[Any, dict] = {}
    leftovers: list[dict] = []
    for item in valid:
        key = _fixture_key(item)
        prev = best_per_fixture.get(key)
        if prev is None or item["_score"] > prev["_score"]:
            if prev is not None:
                leftovers.append(prev)
            best_per_fixture[key] = item
        else:
            leftovers.append(item)
    pool = sorted(best_per_fixture.values(), key=lambda x: x["_score"], reverse=True)

    target_n = _opt_int(target_selections)
    min_n = _opt_int(min_selections)
    if target_n is not None:
        target_n = min(max(2, target_n), max_selections)
    if min_n is not None:
        min_n = min(max(2, min_n), max_selections)
    constrained = target_n is not None or min_n is not None

    compact_snapshot: Optional[dict] = None
    if constrained:
        compact_snapshot = build_ticket(
            candidates, target_odds, max_selections, excluded_fixture_ids,
            max_same_market_family, prefer_league_diversity,
        )

    target_log = math.log(max(float(target_odds), 1.02))
    picked: list[dict] = []
    used: set = set()
    remaining = list(pool)

    if target_n is not None:
        n = min(target_n, len(pool))
        if n < target_n:
            warnings.append(
                f"Ai cerut {target_n} selecții, dar am doar {len(pool)} candidați "
                f"valizi (câte unu per meci). Construiesc biletul cu {n}."
            )
        picked = _pick_n_near_target(pool, n, target_odds)
        used = {_fixture_key(x) for x in picked}
        remaining = [x for x in pool if x not in picked]
        if min_n and len(picked) < min_n:
            remaining.sort(key=lambda x: x["_score"], reverse=True)
            for item in remaining:
                if len(picked) >= min_n:
                    break
                picked.append(item)
    else:
        # 3) Greedy pana atingem cota tinta. La scoruri la 5%, preferam o liga
        #    si un interval orar diferite de ce e deja pe bilet.
        while remaining and _ticket_log_odds(picked) < target_log and len(picked) < max_selections:
            remaining = [x for x in remaining if _fixture_key(x) not in used]
            if not remaining:
                break
            remaining.sort(key=lambda x: x["_score"], reverse=True)
            best = remaining[0]
            close = [x for x in remaining if x["_score"] >= best["_score"] * (1.0 - _CLOSE_SCORE)]
            if prefer_league_diversity and len(close) > 1 and picked:
                leagues = {_league_of(s) for s in picked}
                slots = {_kickoff_slot(s.get("kickoff")) for s in picked}

                def _div(x: dict) -> tuple:
                    lg, sl = _league_of(x), _kickoff_slot(x.get("kickoff"))
                    return (1 if lg and lg not in leagues else 0,
                            1 if sl and sl not in slots else 0,
                            x["_score"])

                chosen = max(close, key=_div)
            else:
                chosen = best
            picked.append(chosen)
            used.add(_fixture_key(chosen))
            remaining = [x for x in remaining if x is not chosen]

        if min_n:
            remaining = [x for x in pool if x not in picked]
            remaining.sort(key=lambda x: x["_score"], reverse=True)
            for item in remaining:
                if len(picked) >= min_n or len(picked) >= max_selections:
                    break
                picked.append(item)
                used.add(_fixture_key(item))
            if len(picked) < min_n:
                warnings.append(
                    f"Ai cerut cel puțin {min_n} selecții, dar am doar {len(picked)} "
                    f"candidați valizi. Construiesc biletul cu ce avem."
                )

        if not constrained:
            # 4) Trim: scoatem selectiile care nu mai sunt necesare (pastrand tinta),
            #    incepand cu cele mai slabe ca scor.
            for item in sorted(list(picked), key=lambda x: x["_score"]):
                if _ticket_log_odds(picked) - math.log(item["odds"]) >= target_log:
                    picked.remove(item)

            # 5) Diversitate: cel mult max_same din aceeasi familie, daca se poate
            #    pastrand cota tinta. Swap din pool-ul nefolosit (si leftovers).
            unused = [x for x in pool + leftovers if x not in picked]
            picked, swap_warns = _apply_diversity_swaps(
                picked, unused, target_log, max_same)
            warnings.extend(swap_warns)

    total_odds = math.exp(_ticket_log_odds(picked)) if picked else 0.0
    # Onestitate: produsul RAW al prob, niciodata scorul cu edge/confidence.
    est_prob = math.exp(sum(math.log(i["prob"]) for i in picked)) if picked else 0.0
    reached = total_odds >= float(target_odds) * 0.999

    if not reached:
        warnings.append(
            f"Cu candidatii primiti am ajuns doar la cota {total_odds:.2f} "
            f"din tinta {float(target_odds):.2f}. Adauga mai multi candidati sau meciuri."
        )

    honesty = None
    if constrained and compact_snapshot and compact_snapshot.get("ok"):
        compact_n = compact_snapshot.get("selections_count") or 0
        compact_p = float(compact_snapshot.get("estimated_probability") or 0)
        if compact_n and len(picked) > compact_n:
            honesty = {
                "compact_selections": compact_n,
                "compact_probability": round(compact_p, 4),
                "requested_selections": target_n or min_n,
                "actual_selections": len(picked),
                "actual_probability": round(est_prob, 4),
                "user_message": (
                    f"Cu {len(picked)} selecții în loc de {compact_n}, probabilitatea "
                    f"trece de la {compact_p:.0%} la ~{est_prob:.0%}."
                ),
            }

    picked.sort(key=lambda x: x.get("kickoff") or "")
    selections = [{k: v for k, v in i.items() if not k.startswith("_")} for i in picked]

    note = ("estimated_probability = sansa estimata ca TOT biletul sa fie castigator. "
            "Prezinta-o onest utilizatorului.")
    if honesty:
        note += (" Utilizatorul a cerut mai multe selecții decât minimul pentru cota "
                 "țintă — spune-i clar cum scade probabilitatea (honesty.user_message). "
                 "Nu refuza și nu ascunde.")

    return {
        "ok": True,
        "excluded_fixture_ids": sorted(excluded),
        "reached_target": reached,
        "target_odds": round(float(target_odds), 2),
        "total_odds": round(total_odds, 2),
        "selections_count": len(selections),
        "estimated_probability": round(est_prob, 4),
        "implied_probability_of_ticket": round(1.0 / total_odds, 4) if total_odds > 0 else None,
        "selections": selections,
        "warnings": warnings,
        "min_selections": min_n,
        "target_selections": target_n,
        "honesty": honesty,
        "note": note,
    }


SAME_MATCH_MENU_NOTE = (
    "Acestea sunt **opțiuni separate**, nu un bilet combinat. "
    "Pe un bilet clasic casa acceptă **o singură selecție per meci** — "
    "două pariuri din același joc nu se pun împreună. "
    "Excepția e **Bet Builder** (pariu pe același meci): nu toate piețele "
    "se combină, iar cota nu e produsul celor individuale."
)


def _is_md_sep_row(line: str) -> bool:
    cells = [c.strip() for c in line.split("|") if c.strip()]
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c) for c in cells)


def _is_same_match_menu_table(header: str) -> bool:
    """Tabel de meniu (Piață / Selecție), nu bilet (Meci / Pariu)."""
    h = (header or "").lower()
    if re.search(r"pia[tț][aă]|selec", h) and not re.search(r"\bmeci\b", h):
        return True
    return False


def annotate_same_match_menu(text: str) -> str:
    """Dacă răspunsul e un meniu de pariuri pe UN meci, spune clar că nu e bilet."""
    if not text:
        return text
    low = text.lower()
    if "opțiuni separate" in low or "optiuni separate" in low:
        return text
    if "o singură selecție per meci" in low or "o singura selectie per meci" in low:
        return text
    lines = text.split("\n")
    i = 0
    last_end: Optional[int] = None
    while i < len(lines):
        if "|" not in lines[i]:
            i += 1
            continue
        j = i
        while j < len(lines) and "|" in lines[j]:
            j += 1
        if j - i >= 2 and _is_same_match_menu_table(lines[i]):
            data = [k for k in range(i + 1, j) if not _is_md_sep_row(lines[k])]
            if len(data) >= 2:
                last_end = j
        i = j
    if last_end is None:
        return text
    insert_at = last_end
    while insert_at < len(lines) and not lines[insert_at].strip():
        insert_at += 1
    lines.insert(insert_at, "")
    lines.insert(insert_at + 1, SAME_MATCH_MENU_NOTE)
    return "\n".join(lines)
