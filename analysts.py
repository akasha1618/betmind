"""
V1-B — Match Analysts: un pool de agenti Claude (model mic, ANALYST_MODEL),
fiecare analizeaza in profunzime UN meci, in paralel, si intoarce JSON strict
(validat cu pydantic). Coordinatorul discuta si planifica; biletul ramane
determinist (ticket_builder).

Pipeline per meci:
  assemble_data_pack (cod pur, fara LLM) -> analyze_match (UN apel Claude,
  JSON validat; invalid -> 1 retry -> inregistrare analysis_failed) ->
  persistare in tabela `analyses`.

analyze_matches ruleaza analistii cu asyncio + Semaphore(MAX_PARALLEL_ANALYSTS)
si emite progres pe masura ce fiecare analiza se termina (pentru SSE).
O analiza recenta (ANALYSIS_REUSE_MINUTES) e refolosita fara apel LLM nou.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from typing import Any, AsyncGenerator, Literal, Optional

from anthropic import AsyncAnthropic
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

import db
import football_data as fd
from llm_compat import messages_create

log = logging.getLogger("betmind.analysts")

_EUROPEAN_LEAGUE_IDS = [2, 3, 848]  # UCL, UEL, UECL

# Pachete comune de liga (standings + injuries), setate inainte de analisti.
# assemble_data_pack le citeste; in afara lui analyze_matches raman apelurile
# individuale (tool-uri classic / un singur meci).
_league_packs: ContextVar[Optional[dict]] = ContextVar("betmind_league_packs", default=None)


# ---------------------------------------------------------------------------
# Config (citita din env la fiecare apel — testabil, fara restart)
# ---------------------------------------------------------------------------

def orchestration_mode() -> str:
    """'analysts' (implicit, fluxul nou) sau 'classic' (single-agent, fallback)."""
    mode = os.environ.get("ORCHESTRATION_MODE", "").strip().lower()
    return mode if mode in ("analysts", "classic") else "analysts"


def normalize_mode(value: Optional[str]) -> Optional[str]:
    """Traduce alegerea din interfata in modul de orchestrare.

    'advanced'/'analysts' -> analiza in paralel; 'standard'/'classic' -> fluxul
    single-agent. Orice altceva (sau lipsa) inseamna "foloseste ce zice .env".
    """
    v = (value or "").strip().lower()
    if v in ("advanced", "analysts"):
        return "analysts"
    if v in ("standard", "classic", "basic"):
        return "classic"
    return None


def analyst_model() -> str:
    return os.environ.get("ANALYST_MODEL", "").strip() or "claude-haiku-4-5-20251001"


def max_parallel_analysts() -> int:
    try:
        return max(1, int(os.environ.get("MAX_PARALLEL_ANALYSTS", "5")))
    except ValueError:
        return 5


def analyst_max_tokens() -> int:
    # Schema V1-E (market_probs + candidates + top_factors/angle in romana)
    # depaseste ~1500 tokeni; la 1500 raspunsul e trunchiat (stop_reason=max_tokens)
    # si json.loads pica cu "Expecting ',' delimiter" in jurul char 2400-3200.
    try:
        return int(os.environ.get("ANALYST_MAX_TOKENS", "4000"))
    except ValueError:
        return 4000


def analyst_timeout_seconds() -> float:
    try:
        return float(os.environ.get("ANALYST_TIMEOUT_SECONDS", "60"))
    except ValueError:
        return 60.0


def analysis_reuse_minutes() -> int:
    try:
        return int(os.environ.get("ANALYSIS_REUSE_MINUTES", "180"))
    except ValueError:
        return 180


# ---------------------------------------------------------------------------
# Schema stricta a analizei (pydantic)
# ---------------------------------------------------------------------------

def _parse_json_list(value: Any) -> Any:
    """Claude pune uneori o lista ca string JSON ('[\"a\", \"b\"]').
    Parseaza-o; un string obisnuit devine lista cu un singur element."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    if not isinstance(value, str):
        return value
    s = value.strip()
    if not s:
        return []
    if s[0] in "[{":
        try:
            parsed = json.loads(s)
        except json.JSONDecodeError:
            return [value]
        if isinstance(parsed, list):
            return parsed
        return [parsed] if parsed is not None else []
    return [value]


def market_probs_consistency_warnings(probs: dict) -> list[str]:
    """Avertismente (nu erori) cand piețele corelate nu suma ~1 sau DC ≠ 1X2."""
    warnings: list[str] = []
    if not isinstance(probs, dict):
        return warnings

    def _check_sum(keys: tuple[str, ...], label: str, expected: float = 1.0) -> None:
        present = [(k, float(probs[k])) for k in keys if k in probs]
        if len(present) < len(keys):
            return
        total = sum(v for _, v in present)
        if abs(total - expected) > 0.05:
            warnings.append(
                f"{label}: {'+'.join(k for k, _ in present)}={total:.3f} "
                f"(așteptat {expected:.2f}, abatere {abs(total - expected):.3f})"
            )

    def _check_eq(left_keys: tuple[str, ...], right_key: str, label: str) -> None:
        if right_key not in probs or any(k not in probs for k in left_keys):
            return
        left = sum(float(probs[k]) for k in left_keys)
        right = float(probs[right_key])
        if abs(left - right) > 0.05:
            warnings.append(
                f"{label}: {'+'.join(left_keys)}={left:.3f} vs {right_key}={right:.3f}"
            )

    _check_sum(("home", "draw", "away"), "1X2")
    for over_k, under_k, tag in (
        ("over15", "under15", "O/U 1.5"),
        ("over25", "under25", "O/U 2.5"),
        ("over35", "under35", "O/U 3.5"),
        ("over05_ht", "under05_ht", "O/U 0.5 HT"),
        ("over15_ht", "under15_ht", "O/U 1.5 HT"),
    ):
        _check_sum((over_k, under_k), tag)
    _check_sum(("btts_yes", "btts_no"), "BTTS")
    _check_sum(("btts_ht_yes", "btts_ht_no"), "BTTS HT")
    _check_eq(("home", "draw"), "dc_home_draw", "1X vs DC 1X")
    _check_eq(("draw", "away"), "dc_draw_away", "X2 vs DC X2")
    _check_eq(("home", "away"), "dc_home_away", "12 vs DC 12")
    return warnings


# ---------------------------------------------------------------------------
# Schema pydantic
# ---------------------------------------------------------------------------


class CandidateSelection(BaseModel):
    model_config = ConfigDict(extra="ignore")
    market: str
    pick: str
    odds: float
    prob: float = Field(ge=0.0, le=1.0)
    reason: str
    implied_prob: Optional[float] = None
    edge: Optional[float] = None
    best_bookmaker: Optional[str] = None
    avg_odds: Optional[float] = None


class MatchAnalysis(BaseModel):
    model_config = ConfigDict(extra="ignore")
    fixture_id: int
    match: str
    kickoff: str  # "YYYY-MM-DDTHH:MM", ora locala Romania
    market_probs: dict[str, float]
    best_candidates: list[CandidateSelection]
    top_factors: list[str]
    angle: str
    data_gaps: list[str] = []
    confidence: Literal["high", "medium", "low"]

    @field_validator("market_probs")
    @classmethod
    def _probs_in_unit_interval(cls, v: dict) -> dict[str, float]:
        if not isinstance(v, dict):
            raise ValueError("market_probs trebuie sa fie un obiect cheie→probabilitate")
        out: dict[str, float] = {}
        for key, p in v.items():
            try:
                fp = float(p)
            except (TypeError, ValueError) as e:
                raise ValueError(f"market_probs[{key!r}] nu e un numar") from e
            if not 0.0 <= fp <= 1.0:
                raise ValueError(f"market_probs[{key!r}]={fp} in afara lui [0, 1]")
            out[str(key)] = fp
        return out

    @field_validator("data_gaps", "best_candidates", mode="before")
    @classmethod
    def _coerce_list_fields(cls, v):
        return _parse_json_list(v)

    @field_validator("top_factors", mode="before")
    @classmethod
    def _max_five_factors(cls, v):
        parsed = _parse_json_list(v)
        return list(parsed)[:5] if isinstance(parsed, list) else parsed

    @field_validator("top_factors")
    @classmethod
    def _factors_must_be_specific(cls, v: list[str]) -> list[str]:
        """Pastreaza factorii cu cifra, nume propriu, data, sau un fapt de
        program din pachet (meci european de mijlocul saptamanii, zile de
        pauza). Un factor generic e ELIMINAT — nu invalideaza toata analiza."""
        kept: list[str] = []
        for factor in v:
            if not isinstance(factor, str) or not factor.strip():
                continue
            if _factor_is_specific(factor):
                kept.append(factor)
            else:
                log.warning("Analist: top_factor generic eliminat: %r", factor)
        return kept


_DATE_WORD = re.compile(
    r"\b(?:0?[1-9]|[12]\d|3[01])[./-](?:0?[1-9]|1[0-2])"
    r"|\b(?:20\d{2})\b"
    r"|\b(?:ian(?:uarie)?|feb(?:ruarie)?|mar(?:tie)?|apr(?:ilie)?|"
    r"iun(?:ie)?|iul(?:ie)?|aug(?:ust)?|sep(?:tembrie)?|oct(?:ombrie)?|"
    r"nov(?:embrie)?|dec(?:embrie)?|"
    r"january|february|march|april|june|july|august|september|"
    r"october|november|december)\b"
    r"|\bmai\s+\d|\d+\s+mai\b"  # luna, nu adverbul "mai buna"
    r"|\b(?:luni|marti|marți|miercuri|joi|vineri|sambata|sâmbătă|"
    r"duminica|duminică|monday|tuesday|wednesday|thursday|friday|"
    r"saturday|sunday)\b",
    re.I,
)
_PACK_GROUNDED = re.compile(
    r"europen|midweek|mijlocul\s+s[aă]pt[aă]m[aâ]n|"
    r"zile\s+de\s+(?:pauz|refacere)|days?\s+(?:of\s+)?rest|"
    r"nou[- ]promovat",
    re.I,
)


def _factor_is_specific(factor: str) -> bool:
    """Cifra, nume propriu (cuvant capitalizat dupa primul), data, sau
    fapt de program din pachet (meci european / zile de pauza)."""
    if any(ch.isdigit() for ch in factor):
        return True
    words = factor.split()
    if any(w[:1].isupper() for w in words[1:]):
        return True
    if _DATE_WORD.search(factor) or _PACK_GROUNDED.search(factor):
        return True
    return False


# ---------------------------------------------------------------------------
# Vocabular dinamic de piete (din pachetul de cote, nu o schema fixa)
# ---------------------------------------------------------------------------

_BASELINE_OUTCOME_KEYS = {
    ("1x2", "home"): "home",
    ("1x2", "draw"): "draw",
    ("1x2", "away"): "away",
    ("over_under", "over 1.5"): "over15",
    ("over_under", "over 2.5"): "over25",
    ("over_under", "under 2.5"): "under25",
    ("over_under", "over 3.5"): "over35",
    ("btts", "yes"): "btts_yes",
    ("double_chance", "home/draw"): "dc_home_draw",
    ("double_chance", "draw/away"): "dc_draw_away",
    ("double_chance", "home/away"): "dc_home_away",
    ("1x2_ht", "home"): "ht_home",
    ("1x2_ht", "draw"): "ht_draw",
    ("1x2_ht", "away"): "ht_away",
    ("over_under_ht", "over 0.5"): "over05_ht",
    ("over_under_ht", "over 1.5"): "over15_ht",
    ("first_to_score", "home"): "first_score_home",
    ("first_to_score", "away"): "first_score_away",
    ("first_to_score", "none"): "first_score_none",
    ("btts_ht", "yes"): "btts_ht_yes",
}


def _slug(s: str) -> str:
    out = []
    for ch in (s or "").lower():
        out.append(ch if ch.isalnum() else "_")
    return "".join(out).strip("_") or "x"


def _prob_key_for(market_key: str, value: str) -> str:
    mapped = _BASELINE_OUTCOME_KEYS.get((market_key, (value or "").strip().lower()))
    if mapped:
        return mapped
    if market_key == "asian_handicap":
        return "ah_" + _slug(value)
    if market_key.startswith("team_total_"):
        return market_key + "_" + _slug(value)
    if market_key.startswith("team_scores"):
        return market_key if market_key != "team_scores" else "team_scores_" + _slug(value)
    if market_key == "htft":
        return "htft_" + _slug(value)
    if market_key == "first_to_score":
        return "first_score_" + _slug(value)
    if market_key.endswith("_ht"):
        return _slug(f"{market_key}_{value}")
    return _slug(f"{market_key}_{value}")


def allowed_prob_keys(odds: Optional[dict]) -> Optional[set[str]]:
    """Cheile permise in market_probs, derivate din cotele reale.

    None = nu putem verifica (fara pachet de cote). set() = orice cheie e invalida.
    """
    if not odds or odds.get("error"):
        return None
    keys: set[str] = set()
    for m in odds.get("markets") or []:
        mk = m.get("key") or ""
        for o in m.get("outcomes") or []:
            keys.add(_prob_key_for(mk, o.get("value") or ""))
    # Compat: pachete vechi, doar cheile top-level.
    if odds.get("1X2"):
        keys.update({"home", "draw", "away"})
    if odds.get("over_under"):
        ou = {str(k).lower() for k in odds["over_under"]}
        if any("over 1.5" in k for k in ou):
            keys.add("over15")
        if any("over 2.5" in k for k in ou):
            keys.add("over25")
        if any("under 2.5" in k for k in ou):
            keys.add("under25")
        if any("over 3.5" in k for k in ou):
            keys.add("over35")
    if odds.get("btts"):
        keys.add("btts_yes")
    if odds.get("double_chance"):
        keys.update({"dc_home_draw", "dc_draw_away", "dc_home_away"})
    return keys


def validate_market_probs(probs: dict, odds: Optional[dict]) -> None:
    """Respinge o probabilitate pentru o piata care nu exista in pachetul de cote."""
    allowed = allowed_prob_keys(odds)
    if allowed is None:
        return
    unknown = [k for k in (probs or {}) if k not in allowed]
    if unknown:
        raise ValueError(
            f"market_probs contine piete fara cote in pachet: {unknown}. "
            f"Permise: {sorted(allowed)}"
        )


def _find_outcome(odds: Optional[dict], market: str, pick: str) -> Optional[dict]:
    if not odds or odds.get("error"):
        return None
    key = fd.normalize_market_name(market) or (market or "").lower()
    pick_n = fd._normalize_outcome(key, pick) if key else (pick or "")
    for m in odds.get("markets") or []:
        if m.get("key") != key and m.get("key") != (market or "").lower():
            continue
        for o in m.get("outcomes") or []:
            if (o.get("value") or "").lower() in {
                (pick or "").lower(), pick_n.lower(),
            }:
                return o
    # Legacy top-level
    legacy = {"1x2": "1X2", "over_under": "over_under", "btts": "btts",
              "double_chance": "double_chance"}
    block = odds.get(legacy.get(key, ""))
    if isinstance(block, dict):
        for val, odd in block.items():
            if str(val).lower() == (pick or "").lower():
                try:
                    fo = float(odd)
                except (TypeError, ValueError):
                    return None
                return {"value": val, "avg_odd": fo, "best_odd": fo,
                        "best_bookmaker": odds.get("bookmaker"), "n_books": 1,
                        "reference_odd": fo}
    return None


def enrich_candidates(analysis: dict, odds: Optional[dict]) -> dict:
    """Completeaza implied_prob / edge / avg_odds / casa din pachetul de cote.

    Calculat in cod (niciodata de LLM) ca sa nu se fabrice convigerea.
    """
    confidence = analysis.get("confidence") or "medium"
    for c in analysis.get("best_candidates") or []:
        c.setdefault("confidence", confidence)
        found = _find_outcome(odds, c.get("market") or "", c.get("pick") or "")
        try:
            prob = float(c.get("prob"))
        except (TypeError, ValueError):
            continue
        if found and found.get("avg_odd"):
            avg = float(found["avg_odd"])
            implied = 1.0 / avg
            c["avg_odds"] = round(avg, 3)
            c["implied_prob"] = round(implied, 3)
            c["edge"] = round(prob - implied, 3)
            c["best_bookmaker"] = found.get("best_bookmaker")
            if found.get("best_odd"):
                c["best_odd"] = found["best_odd"]
            # Pe bilet folosim cota de referinta (casa preferata) daca exista.
            ref = found.get("reference_odd") or found.get("best_odd")
            if ref and not c.get("odds"):
                c["odds"] = ref
        else:
            try:
                implied = 1.0 / float(c["odds"])
            except (TypeError, ValueError, ZeroDivisionError, KeyError):
                continue
            c.setdefault("implied_prob", round(implied, 3))
            c.setdefault("edge", round(prob - implied, 3))
    return analysis


# ---------------------------------------------------------------------------
# Data pack (cod pur, fara LLM)
# ---------------------------------------------------------------------------

async def _days_since_last_match(team_id: int, kickoff_date: str,
                                 exclude_fixture_id: int) -> Optional[int]:
    row = await db.last_finished_fixture_before(team_id, kickoff_date)
    if not row or row["fixture_id"] == exclude_fixture_id:
        return None
    try:
        return (date.fromisoformat(kickoff_date) - date.fromisoformat(row["date_local"])).days
    except ValueError:
        return None


async def _midweek_european_game(team_id: int, kickoff_date: str,
                                 exclude_fixture_id: int) -> bool:
    try:
        d = date.fromisoformat(kickoff_date)
    except ValueError:
        return False
    rows = await db.team_fixtures_in_leagues(
        team_id,
        (d - timedelta(days=4)).isoformat(),
        (d + timedelta(days=4)).isoformat(),
        _EUROPEAN_LEAGUE_IDS,
        exclude_fixture_id=exclude_fixture_id,
    )
    return bool(rows)


def _league_key(league_id, season) -> Optional[tuple[int, int]]:
    try:
        if league_id is None or season is None:
            return None
        return (int(league_id), int(season))
    except (TypeError, ValueError):
        return None


async def prefetch_league_packs(fixture_ids: list[int]) -> dict:
    """O singura pereche /standings + /injuries per (liga, sezon) din shortlist."""
    keys: set[tuple[int, int]] = set()
    for fid in fixture_ids:
        fx = await db.get_fixture(fid)
        if not fx:
            continue
        key = _league_key(fx.get("league_id"), fx.get("season"))
        if key:
            keys.add(key)

    async def one(lid: int, season: int) -> tuple[tuple[int, int], dict]:
        standings, st_err = None, None
        try:
            standings = await fd.get_standings(lid, season)
        except fd.FootballDataError as e:
            st_err = str(e)
        except Exception as e:
            st_err = f"{type(e).__name__}: {e}"
        injuries_by_team, inj_err = {}, None
        try:
            injuries_by_team = await fd.get_league_injuries_by_team(lid, season)
        except fd.FootballDataError as e:
            inj_err = str(e)
        except Exception as e:
            inj_err = f"{type(e).__name__}: {e}"
        return (lid, season), {
            "standings": standings,
            "standings_error": st_err,
            "injuries_by_team": injuries_by_team,
            "injuries_error": inj_err,
        }

    packs: dict = {}
    if keys:
        for key, pack in await asyncio.gather(*[one(lid, season) for lid, season in keys]):
            packs[key] = pack
    log.info("Prefetch ligi: %d (%s) — /standings+/injuries?league= o data per liga, nu per echipa",
             len(packs),
             ", ".join(f"{lid}/{season}" for lid, season in sorted(packs)) or "nimic")
    return packs


async def assemble_data_pack(fixture_id: int, league_packs: Optional[dict] = None) -> dict:
    """
    Aduna TOATE datele pentru un meci (fixture store + API-Football prin
    adapter). Esecurile partiale devin intrari in data_gaps — analiza continua
    onest cu ce exista. Fara niciun apel LLM.

    Daca exista un pachet de liga preincarcat (analyze_matches), clasamentele
    si accidentarile vin de acolo — acelasi continut, fara request-uri duplicate.
    """
    fx = await db.get_fixture(fixture_id)
    if not fx:
        return {"error": f"Meciul {fixture_id} nu exista in fixture store-ul local. "
                         "Cere intai get_fixtures pentru ziua lui (sau track_league daca liga nu e urmarita)."}

    gaps: list[str] = []
    home_id, away_id = fx["home_id"], fx["away_id"]
    league_id, season = fx["league_id"], fx["season"]

    async def safe(name: str, coro):
        try:
            return await coro
        except fd.FootballDataError as e:  # include BudgetExhausted
            gaps.append(f"{name}: {e}")
            return None
        except Exception as e:
            gaps.append(f"{name}: {type(e).__name__}: {e}")
            return None

    packs = league_packs if league_packs is not None else (_league_packs.get() or {})
    key = _league_key(league_id, season)
    shared = packs.get(key) if key else None

    standings = None
    home_inj = away_inj = None
    if shared is not None:
        if shared.get("standings_error"):
            gaps.append(f"standings: {shared['standings_error']}")
        else:
            standings = shared.get("standings")
        if shared.get("injuries_error"):
            gaps.append(f"home_injuries: {shared['injuries_error']}")
            gaps.append(f"away_injuries: {shared['injuries_error']}")
        else:
            by_team = shared.get("injuries_by_team") or {}
            home_inj = by_team.get(home_id) or fd._injuries_pack_from_raw([], home_id)
            away_inj = by_team.get(away_id) or fd._injuries_pack_from_raw([], away_id)

    # Cotele intai: fara ele meciul e inutil pentru bilet, iar burst-ul
    # de statistici le sufocea (rate limit pe /odds).
    odds = await safe("odds", fd.get_odds(fixture_id))

    rest = [
        safe("home_last_matches", fd.get_team_last_matches(home_id, 6)),
        safe("away_last_matches", fd.get_team_last_matches(away_id, 6)),
        safe("home_season_stats", fd.get_team_statistics(home_id, league_id, season)),
        safe("away_season_stats", fd.get_team_statistics(away_id, league_id, season)),
        safe("h2h", fd.get_h2h(home_id, away_id, 6)),
        safe("predictions", fd.get_predictions(fixture_id)),
    ]
    if shared is None:
        (home_last, away_last, home_stats, away_stats, home_inj, away_inj,
         h2h, standings, predictions) = await asyncio.gather(
            rest[0], rest[1], rest[2], rest[3],
            safe("home_injuries", fd.get_injuries(home_id, season)),
            safe("away_injuries", fd.get_injuries(away_id, season)),
            rest[4],
            safe("standings", fd.get_standings(league_id, season)),
            rest[5],
        )
    else:
        (home_last, away_last, home_stats, away_stats,
         h2h, predictions) = await asyncio.gather(*rest)

    def _row(team_id: int) -> Optional[dict]:
        if not standings:
            return None
        return next((r for r in standings if r.get("team_id") == team_id), None)

    kickoff_date = fx["date_local"]
    home_rest = await _days_since_last_match(home_id, kickoff_date, fixture_id)
    away_rest = await _days_since_last_match(away_id, kickoff_date, fixture_id)
    home_euro = await _midweek_european_game(home_id, kickoff_date, fixture_id)
    away_euro = await _midweek_european_game(away_id, kickoff_date, fixture_id)

    return {
        "fixture": {
            "fixture_id": fx["fixture_id"],
            "match": f"{fx['home_name']} vs {fx['away_name']}",
            "kickoff": fx["kickoff_iso"],
            "date": fx["date_local"],
            "time": fx["time_local"],
            "status": fx["status"],
            "status_group": fx["status_group"],
            "league": fx["league_name"],
            "league_id": league_id,
            "season": season,
        },
        "home": {
            "team_id": home_id,
            "name": fx["home_name"],
            "last_matches": home_last,
            "season_stats": home_stats,
            "injuries": home_inj,
            "standings_row": _row(home_id),
            "days_since_last_match": home_rest,
            "midweek_european_game": home_euro,
        },
        "away": {
            "team_id": away_id,
            "name": fx["away_name"],
            "last_matches": away_last,
            "season_stats": away_stats,
            "injuries": away_inj,
            "standings_row": _row(away_id),
            "days_since_last_match": away_rest,
            "midweek_european_game": away_euro,
        },
        "h2h": h2h,
        "odds": odds,
        "predictions": predictions,
        "data_gaps": gaps,
    }


# ---------------------------------------------------------------------------
# Apelul LLM al analistului
# ---------------------------------------------------------------------------

_ANALYST_SYSTEM_PROMPT = """You are a football match analyst. You receive ONE match as a JSON data pack (fixture info, both teams' recent matches, season stats, injuries, H2H, standings, bookmaker odds, API-Football predictions, computed rest-days and midweek-European-game flags, and data_gaps).

Output ONLY a single valid JSON object — no prose, no markdown fences — with this schema:
{"fixture_id":int,"match":str,"kickoff":"YYYY-MM-DDTHH:MM",
 "market_probs":{ "<key>": 0-1, ... },
 "best_candidates":[{"market":str,"pick":str,"odds":float,"prob":float,"reason":str}],
 "top_factors":[str],
 "angle":str,
 "data_gaps":[str],
 "confidence":"high|medium|low"}

RULES:
- LANGUAGE: every human-readable string (top_factors, angle, best_candidates[].reason, data_gaps) MUST be written in ROMANIAN — the app speaks Romanian and your exact numbers must survive untranslated. JSON keys, market names ("1X2", "Over 2.5", "GG") and enum values (confidence: high/medium/low) stay in English.
- market_probs is a DYNAMIC map. Emit a probability ONLY for keys listed in the "allowed_prob_keys" block of the user message (those markets actually have odds). A key without odds is rejected. Typical keys when present: home, draw, away, over15, over25, under25, over35, btts_yes, dc_home_draw, dc_draw_away, dc_home_away, plus ah_*, team_total_*, team_scores_*, ht_*, over05_ht, over15_ht, first_score_*, htft_*.
- Probabilities: blend 0.6 × implied-from-odds (1/avg_odd, normalized) + 0.4 × your statistical estimate from the pack. Never drift far above market without a strong stated reason.
- best_candidates: 2-4 selections, ONLY for markets whose real odds appear in the pack (use reference_odd or avg_odd from the pack — never invent). No odds in pack => fewer or zero candidates.
- MARKET FAMILIES: when the pack has at least 3 distinct families (result, goals, btts, double_chance, handicap, team-based, half-time), propose candidates from at least 3 of them. Safe-but-boring markets (double chance, over 1.5, team to score) are first-class options — not leftovers.
- Pick the market where your statistical EDGE over the implied probability (prob − 1/avg_odd) is largest AND best justified by the data — not the market with the highest odds.
- Every candidate's reason MUST name the concrete data points that CREATE the edge: scores, goal averages, named absentees, rest days, H2H dates. "E favorită" is not a reason.
- top_factors: max 5; EACH should contain a number, score, date, named player, OR a pack-grounded schedule fact (midweek European game, rest days). Vague quality claims are dropped one-by-one — they do not fail the whole analysis.
- BANNED generic phrases (never use, in any language): "echipă de calitate", "echipă superioară/consacrată", "formă bună" without numbers, "meci deschis", "tradițional cu goluri", "meci de tempo ridicat", "outsider clar" without the odds, "favorită clară" without numbers. If a claim cannot be grounded in pack data, DROP it.
- angle: ONE non-obvious connection grounded in pack data: schedule congestion (days_since_last_match), midweek European game, stakes/table context, promoted side, key absence chain. One or two sentences, in Romanian.
- The API-Football predictions block is one signal among many — never copy it as your conclusion.
- data_gaps: copy the pack's gaps that actually limited you, plus any you noticed. Lower your confidence accordingly. data_gaps and top_factors MUST be JSON arrays of strings, never a stringified array.
- kickoff is already Romania local time — repeat it as-is, never convert.
- Be honest: thin data => hedged probabilities and "low" confidence."""


def build_analyst_prompt(odds: Optional[dict] = None) -> str:
    """Promptul de sistem + lista concreta de chei permise pentru meciul curent."""
    allowed = allowed_prob_keys(odds)
    if allowed is None:
        extra = ("\n\nallowed_prob_keys: none (no odds in pack). "
                 "Emit market_probs as {} and zero or fewer candidates.")
    elif not allowed:
        extra = ("\n\nallowed_prob_keys: [] — do not emit any market_probs keys.")
    else:
        extra = "\n\nallowed_prob_keys: " + json.dumps(sorted(allowed))
    return _ANALYST_SYSTEM_PROMPT + extra


_ANALYSIS_TOOL = {
    "name": "submit_match_analysis",
    "description": "Return the completed match analysis as structured JSON. Call this once.",
    "input_schema": {
        "type": "object",
        "properties": {
            "fixture_id": {"type": "integer"},
            "match": {"type": "string"},
            "kickoff": {"type": "string"},
            "market_probs": {
                "type": "object",
                "additionalProperties": {"type": "number"},
            },
            "best_candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "market": {"type": "string"},
                        "pick": {"type": "string"},
                        "odds": {"type": "number"},
                        "prob": {"type": "number"},
                        "reason": {"type": "string"},
                    },
                    "required": ["market", "pick", "odds", "prob", "reason"],
                },
            },
            "top_factors": {"type": "array", "items": {"type": "string"}},
            "angle": {"type": "string"},
            "data_gaps": {"type": "array", "items": {"type": "string"}},
            "confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
            },
        },
        "required": [
            "fixture_id", "match", "kickoff", "market_probs",
            "best_candidates", "top_factors", "angle", "confidence",
        ],
    },
}


def analyst_llm_create_kwargs(model: str = "dummy", max_tokens: int = 1,
                              system: str = "s",
                              user_content: str = "x") -> dict:
    """Parametrii exacti trimisi la messages.create — testati fata de SDK."""
    return {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": 0,
        "system": system,
        "messages": [{"role": "user", "content": user_content}],
        "tools": [_ANALYSIS_TOOL],
        "tool_choice": {"type": "tool", "name": "submit_match_analysis"},
    }


async def _call_analyst_llm(system: str, user_content: str) -> tuple[str, Any, str]:
    """UN apel Claude (analyst). Intoarce (text, usage, stop_reason).

    Forteaza JSON prin tool_choice (echivalentul structured output la Claude).
    Mock-urile din teste pot intoarce in continuare (text, usage).
    temperature e optional: SDK-urile vechi nu il au — vezi llm_compat.
    """
    client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip())
    msg = await messages_create(client, **analyst_llm_create_kwargs(
        model=analyst_model(),
        max_tokens=analyst_max_tokens(),
        system=system,
        user_content=user_content,
    ))
    stop_reason = getattr(msg, "stop_reason", None) or ""
    text = ""
    for block in msg.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", "") == "submit_match_analysis":
            inp = getattr(block, "input", None)
            if isinstance(inp, dict):
                text = json.dumps(inp, ensure_ascii=False)
                break
            if isinstance(inp, str) and inp.strip():
                text = inp
                break
    if not text:
        text = "".join(
            getattr(b, "text", "") for b in msg.content
            if getattr(b, "type", None) == "text"
        )
    return text, msg.usage, stop_reason


def _unpack_llm_result(result: Any) -> tuple[str, Any, str]:
    """Compatibil cu mock-uri care intorc (text, usage) fara stop_reason."""
    if not isinstance(result, tuple):
        raise TypeError(f"_call_analyst_llm a intors {type(result).__name__}, nu tuple")
    text = result[0] if result else ""
    usage = result[1] if len(result) > 1 else None
    stop_reason = result[2] if len(result) > 2 else ""
    return text or "", usage, stop_reason or ""


def _extract_json(text: str) -> dict:
    """Scoate obiectul JSON: elimina garduri markdown, ia de la prima '{'
    pana la ultima '}'. Daca e trunchiat, incearca sa inchida braces."""
    if not text or not str(text).strip():
        raise ValueError("Raspunsul analistului e gol.")
    cleaned = str(text).strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```\s*$", "", cleaned)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1:
        raise ValueError("Raspunsul analistului nu contine JSON.")
    if end <= start:
        repaired = _try_repair_truncated_json(cleaned[start:])
        if repaired is not None:
            return repaired
        raise ValueError("JSON trunchiat (lipsa '}' de inchidere).")
    blob = cleaned[start:end + 1]
    try:
        obj = json.loads(blob)
    except json.JSONDecodeError:
        repaired = _try_repair_truncated_json(blob)
        if repaired is None:
            raise
        obj = repaired
    if not isinstance(obj, dict):
        raise ValueError("JSON-ul analistului nu e un obiect.")
    return obj


def _try_repair_truncated_json(blob: str) -> Optional[dict]:
    """Inchide stringuri/obiecte/liste neterminate (trunchiere max_tokens)."""
    s = blob.rstrip()
    in_str = False
    escape = False
    for ch in s:
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
    if in_str:
        s += '"'
    s = re.sub(r",\s*$", "", s)
    stack: list[str] = []
    in_str = False
    escape = False
    for ch in s:
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in ("}", "]") and stack and stack[-1] == ch:
            stack.pop()
    s += "".join(reversed(stack))
    try:
        obj = json.loads(s)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


async def _log_usage(turn_id: Optional[str], usage: Any) -> None:
    try:
        await db.add_usage(
            turn_id or "analyst",
            analyst_model(),
            getattr(usage, "input_tokens", None),
            getattr(usage, "output_tokens", None),
            fd.now_local().isoformat(timespec="seconds"),
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", None),
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", None),
        )
    except Exception:
        log.exception("Nu am putut scrie usage_log (analist)")


def _analysis_is_fresh(row: dict) -> bool:
    try:
        created = datetime.fromisoformat(row["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=fd.app_timezone())
        age_min = (fd.now_local() - created).total_seconds() / 60
        return age_min <= analysis_reuse_minutes()
    except (ValueError, KeyError, TypeError):
        return False


async def analyze_match(fixture_id: int, turn_id: Optional[str] = None,
                        league_packs: Optional[dict] = None) -> dict:
    """
    Analiza completa a unui meci. Refoloseste o analiza recenta din DB fara
    apel LLM nou. Invalid JSON -> 1 retry -> inregistrare analysis_failed.
    Nu ridica exceptii — esecul e un dict onest {"analysis_failed": true}.
    """
    stored = await db.latest_analysis(fixture_id)
    if stored and _analysis_is_fresh(stored):
        try:
            data = json.loads(stored["json"])
        except (json.JSONDecodeError, TypeError):
            data = None
        if data and not data.get("analysis_failed"):
            data["reused"] = True
            return data

    pack = await assemble_data_pack(fixture_id, league_packs)
    now = fd.now_local().isoformat(timespec="seconds")
    if pack.get("error"):
        failed = {"fixture_id": fixture_id, "analysis_failed": True, "error": pack["error"]}
        await db.add_analysis(fixture_id, now, analyst_model(), json.dumps(failed, ensure_ascii=False))
        return failed

    pack_content = json.dumps(pack, ensure_ascii=False, default=str)
    system = build_analyst_prompt(pack.get("odds"))
    last_error = ""
    prev_text = ""
    prev_stop = ""
    for attempt in (1, 2):  # al doilea = singurul retry permis
        if attempt == 1:
            user_content = pack_content
        else:
            user_content = (
                "PREVIOUS OUTPUT WAS INVALID JSON. Do NOT re-analyse the match. "
                "Fix the JSON only — same conclusions, valid object, no markdown.\n"
                f"stop_reason={prev_stop or 'unknown'}\n"
                f"parse_error={last_error}\n"
                f"previous_output:\n{prev_text}"
            )
        try:
            result = await _call_analyst_llm(system, user_content)
            text, usage, stop_reason = _unpack_llm_result(result)
            prev_text, prev_stop = text, stop_reason
            await _log_usage(turn_id, usage)
            raw = _extract_json(text)
            validate_market_probs(raw.get("market_probs") or {}, pack.get("odds"))
            analysis = MatchAnalysis.model_validate(raw).model_dump()
            analysis["fixture_id"] = fixture_id  # nu lasam analistul sa-l schimbe
            warns = market_probs_consistency_warnings(analysis.get("market_probs") or {})
            if warns:
                log.warning("Analist fixture=%s market_probs inconsistente: %s",
                            fixture_id, "; ".join(warns))
                analysis["confidence"] = "low"
            enrich_candidates(analysis, pack.get("odds"))
            fx = pack.get("fixture") or {}
            for c in analysis.get("best_candidates") or []:
                c.setdefault("league", fx.get("league"))
                c.setdefault("kickoff", analysis.get("kickoff") or fx.get("kickoff"))
                c.setdefault("match", analysis.get("match") or fx.get("match"))
                c.setdefault("fixture_id", fixture_id)
            await db.add_analysis(fixture_id, fd.now_local().isoformat(timespec="seconds"),
                                  analyst_model(), json.dumps(analysis, ensure_ascii=False))
            return analysis
        except (ValidationError, ValueError, json.JSONDecodeError) as e:
            last_error = f"JSON invalid de la analist (incercarea {attempt}): {e}"
            if prev_stop == "max_tokens":
                last_error += " [stop_reason=max_tokens — raspuns trunchiat]"
            log.warning(
                "Analist fixture=%s: %s stop_reason=%s chars=%d",
                fixture_id, last_error, prev_stop or "unknown", len(prev_text),
            )
            log.warning(
                "Analist fixture=%s raspuns brut complet (stop_reason=%s):\n%s",
                fixture_id, prev_stop or "unknown", prev_text,
            )
        except Exception as e:
            last_error = f"Apel analist esuat (incercarea {attempt}): {type(e).__name__}: {e}"
            log.warning("Analist fixture=%s: %s stop_reason=%s",
                        fixture_id, last_error, prev_stop or "unknown")

    failed = {"fixture_id": fixture_id, "analysis_failed": True, "error": last_error}
    await db.add_analysis(fixture_id, fd.now_local().isoformat(timespec="seconds"),
                          analyst_model(), json.dumps(failed, ensure_ascii=False))
    return failed


# ---------------------------------------------------------------------------
# Rularea in paralel + progres pentru SSE
# ---------------------------------------------------------------------------

async def analyze_matches_events(fixture_ids: list[int], max_matches: int = 15,
                                 turn_id: Optional[str] = None
                                 ) -> AsyncGenerator[tuple, None]:
    """
    Ruleaza analistii in paralel (Semaphore MAX_PARALLEL_ANALYSTS) si emite:
      ("progress", done, total) — dupa fiecare analiza terminata;
      ("result", dict)          — rezultatul agregat, la final.
    """
    seen: set[int] = set()
    ids = [f for f in fixture_ids if not (f in seen or seen.add(f))][:max(1, max_matches)]
    total = len(ids)
    if not ids:
        yield ("result", {"error": "Niciun fixture_id de analizat."})
        return

    packs = await prefetch_league_packs(ids)
    token = _league_packs.set(packs)
    sem = asyncio.Semaphore(max_parallel_analysts())
    timeout = analyst_timeout_seconds()

    async def one(fid: int) -> dict:
        async with sem:
            try:
                return await asyncio.wait_for(
                    analyze_match(fid, turn_id, packs), timeout)
            except asyncio.TimeoutError:
                failed = {"fixture_id": fid, "analysis_failed": True,
                          "error": f"Analiza a depasit {int(timeout)}s si a fost oprita."}
                try:
                    await db.add_analysis(fid, fd.now_local().isoformat(timespec="seconds"),
                                          analyst_model(), json.dumps(failed, ensure_ascii=False))
                except Exception:
                    log.exception("Nu am putut persista timeout-ul analistului")
                return failed

    try:
        tasks = [asyncio.create_task(one(fid)) for fid in ids]
        results: list[dict] = []
        done = 0
        for finished in asyncio.as_completed(tasks):
            results.append(await finished)
            done += 1
            just = results[-1].get("match") or ""
            yield ("progress", done, total, just)
    finally:
        _league_packs.reset(token)

    ok = [r for r in results if not r.get("analysis_failed")]
    failed = [r for r in results if r.get("analysis_failed")]
    ok.sort(key=lambda a: a.get("kickoff") or "")

    summary: dict[str, Any] = {
        "requested": total,
        "analyzed": len(ok),
        "failed": len(failed),
        "analyses": ok,
        "note": ("Foloseste best_candidates drept candidati pentru build_ticket "
                 "(include edge, avg_odds, best_bookmaker, confidence, league, kickoff). "
                 "Citeaza top_factors si angle in motivarea selectiilor; "
                 "mentioneaza onest data_gaps si confidence scazut. "
                 "Construiește biletul DOAR din analyses[].best_candidates "
                 "(analizele reușite). NU chema get_odds / get_team_last_matches / "
                 "get_h2h / get_injuries ca sa inlocuiesti o analiza esuata — "
                 "nu improviza din amicale de presezon."),
    }
    if failed:
        summary["failed_fixtures"] = [
            {"fixture_id": r["fixture_id"], "error": r.get("error", "necunoscut")} for r in failed
        ]
        n_fail, n_req = len(failed), total
        summary["note"] += (
            f" {n_fail} din {n_req} meciuri n-au putut fi analizate — "
            "spune-i utilizatorului onest cifra, sari peste ele, si NU inventa "
            "analize / cote / forma pentru ele. Daca ZERO analize au reusit, "
            "nu construi un bilet; spune ca analizele au esuat si ofera retry."
        )
    yield ("result", summary)


async def analyze_matches(fixture_ids: list[int], max_matches: int = 15,
                          turn_id: Optional[str] = None) -> dict:
    """Varianta fara streaming (teste / utilizare programatica)."""
    result: dict = {}
    async for ev in analyze_matches_events(fixture_ids, max_matches, turn_id):
        if ev[0] == "result":
            result = ev[1]
    return result
