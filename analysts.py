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
from contextvars import ContextVar
from datetime import date, datetime, timedelta
from typing import Any, AsyncGenerator, Literal, Optional

from anthropic import AsyncAnthropic
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

import db
import football_data as fd

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
    try:
        return int(os.environ.get("ANALYST_MAX_TOKENS", "1500"))
    except ValueError:
        return 1500


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

    @field_validator("top_factors", mode="before")
    @classmethod
    def _max_five_factors(cls, v):
        return list(v)[:5] if isinstance(v, (list, tuple)) else v

    @field_validator("top_factors")
    @classmethod
    def _factors_must_be_specific(cls, v: list[str]) -> list[str]:
        """Fiecare factor trebuie sa contina o cifra/scor/data SAU o entitate
        numita (nume propriu, aproximat printr-un cuvant capitalizat care nu
        deschide propozitia). Generalitatile sunt respinse -> retry."""
        for factor in v:
            has_digit = any(ch.isdigit() for ch in factor)
            has_named_entity = any(w[:1].isupper() for w in factor.split()[1:])
            if not (has_digit or has_named_entity):
                raise ValueError(
                    f"top_factor generic, fara cifra/scor/data/jucator numit: {factor!r}"
                )
        return v


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


async def prefetch_league_packs(fixture_ids: list[int]) -> dict:
    """O singura pereche /standings + /injuries per (liga, sezon) din shortlist."""
    keys: set[tuple[int, int]] = set()
    for fid in fixture_ids:
        fx = await db.get_fixture(fid)
        if not fx:
            continue
        lid, season = fx.get("league_id"), fx.get("season")
        if lid and season:
            keys.add((int(lid), int(season)))

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
    return packs


async def assemble_data_pack(fixture_id: int) -> dict:
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

    shared = (_league_packs.get() or {}).get((league_id, season)) if league_id and season else None

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

    common = [
        safe("home_last_matches", fd.get_team_last_matches(home_id, 6)),
        safe("away_last_matches", fd.get_team_last_matches(away_id, 6)),
        safe("home_season_stats", fd.get_team_statistics(home_id, league_id, season)),
        safe("away_season_stats", fd.get_team_statistics(away_id, league_id, season)),
        safe("h2h", fd.get_h2h(home_id, away_id, 6)),
        safe("odds", fd.get_odds(fixture_id)),
        safe("predictions", fd.get_predictions(fixture_id)),
    ]
    if shared is None:
        (home_last, away_last, home_stats, away_stats, home_inj, away_inj,
         h2h, standings, odds, predictions) = await asyncio.gather(
            common[0], common[1], common[2], common[3],
            safe("home_injuries", fd.get_injuries(home_id, season)),
            safe("away_injuries", fd.get_injuries(away_id, season)),
            common[4],
            safe("standings", fd.get_standings(league_id, season)),
            common[5], common[6],
        )
    else:
        (home_last, away_last, home_stats, away_stats,
         h2h, odds, predictions) = await asyncio.gather(*common)

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
- top_factors: max 5; EACH must contain at least one concrete number, score, date, or named player from the pack (schema-enforced — entries without one are rejected). No generic claims.
- BANNED generic phrases (never use, in any language): "echipă de calitate", "echipă superioară/consacrată", "formă bună" without numbers, "meci deschis", "tradițional cu goluri", "meci de tempo ridicat", "outsider clar" without the odds, "favorită clară" without numbers. If a claim cannot be grounded in pack data, DROP it.
- angle: ONE non-obvious connection grounded in pack data: schedule congestion (days_since_last_match), midweek European game, stakes/table context, promoted side, key absence chain. One or two sentences, in Romanian.
- The API-Football predictions block is one signal among many — never copy it as your conclusion.
- data_gaps: copy the pack's gaps that actually limited you, plus any you noticed. Lower your confidence accordingly.
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


async def _call_analyst_llm(system: str, user_content: str) -> tuple[str, Any]:
    """UN apel Claude (analyst). Separat ca sa fie mock-uibil in teste."""
    client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip())
    msg = await client.messages.create(
        model=analyst_model(),
        max_tokens=analyst_max_tokens(),
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    return text, msg.usage


def _extract_json(text: str) -> dict:
    """Scoate obiectul JSON din raspuns (tolerant la garduri de cod/prefixe)."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("Raspunsul analistului nu contine JSON.")
    return json.loads(text[start:end + 1])


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


async def analyze_match(fixture_id: int, turn_id: Optional[str] = None) -> dict:
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

    pack = await assemble_data_pack(fixture_id)
    now = fd.now_local().isoformat(timespec="seconds")
    if pack.get("error"):
        failed = {"fixture_id": fixture_id, "analysis_failed": True, "error": pack["error"]}
        await db.add_analysis(fixture_id, now, analyst_model(), json.dumps(failed, ensure_ascii=False))
        return failed

    user_content = json.dumps(pack, ensure_ascii=False, default=str)
    last_error = ""
    for attempt in (1, 2):  # al doilea = singurul retry permis
        try:
            text, usage = await _call_analyst_llm(build_analyst_prompt(pack.get("odds")),
                                                 user_content)
            await _log_usage(turn_id, usage)
            raw = _extract_json(text)
            validate_market_probs(raw.get("market_probs") or {}, pack.get("odds"))
            analysis = MatchAnalysis.model_validate(raw).model_dump()
            analysis["fixture_id"] = fixture_id  # nu lasam analistul sa-l schimbe
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
            log.warning("Analist fixture=%s: %s", fixture_id, last_error)
        except Exception as e:
            last_error = f"Apel analist esuat (incercarea {attempt}): {type(e).__name__}: {e}"
            log.warning("Analist fixture=%s: %s", fixture_id, last_error)

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
                return await asyncio.wait_for(analyze_match(fid, turn_id), timeout)
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
                 "mentioneaza onest data_gaps si confidence scazut."),
    }
    if failed:
        summary["failed_fixtures"] = [
            {"fixture_id": r["fixture_id"], "error": r.get("error", "necunoscut")} for r in failed
        ]
        summary["note"] += (" Meciurile esuate se sar cu O linie onesta in raspuns "
                            "(nu inventa analize pentru ele).")
    yield ("result", summary)


async def analyze_matches(fixture_ids: list[int], max_matches: int = 15,
                          turn_id: Optional[str] = None) -> dict:
    """Varianta fara streaming (teste / utilizare programatica)."""
    result: dict = {}
    async for ev in analyze_matches_events(fixture_ids, max_matches, turn_id):
        if ev[0] == "result":
            result = ev[1]
    return result
