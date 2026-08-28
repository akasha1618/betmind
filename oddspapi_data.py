"""OddsPapi — cote Superbet România (superbet.ro) pentru meciurile din shortlist.

Modul de producție portat din oddspapi_v4.py (logica validată manual pe date
reale). Reguli moștenite ca atare, NU reinventate aici:

- Sensul fiecărui outcome (Acasă/Egal/Oaspete, Da/Nu…) vine EXCLUSIV din
  /markets. Cheile din /odds sunt UUID-uri opace; ghicitul sensului din
  mărimea cotei a produs o eroare confirmată pe teren (GG Da/Nu inversate).
  `rezolva_sens_outcome` întoarce None când sensul nu e documentat — fără
  niciun fallback.
- Verificare de sanitate pe fiecare piață: suma probabilităților implicite
  (1/cotă) trebuie să fie în [0.85, 1.20]. În afara intervalului, piața e
  NEFOLOSITĂ, chiar dacă sensul era confirmat.
- Cooldown de 500ms (documentat de furnizor) între apeluri consecutive la
  /odds, serializat printr-un lock asincron global pe proces; retry pe 429
  citind retryMs din corpul erorii.
- Cache persistent (DB) pentru /markets, reîmprospătat săptămânal, cu
  degradare elegantă pe cache-ul vechi dacă API-ul pică temporar.

Rolul modulului în aplicație: API-Football rămâne sursa pentru TOT
(fixtures, statistici, formă, cote de rezervă). Aici suprascriem cotele
piețelor pe care le analizăm deja (1X2, over/under, GG, șansă dublă,
handicap, totaluri pe echipă, pauză, primul gol…) cu cele Superbet RO —
mai proaspete — și atașăm linkul direct către meci pe superbet.ro. Nu
adăugăm piețe noi (corners, cartonașe, marcatori, scor corect). Orice
incertitudine (potrivire de meci nesigură, sens neconfirmat, sumă anormală,
timeout) => fallback TĂCUT pe cotele API-Football, fără link.

Fără ODDSPAPI_KEY în mediu, modulul e complet inert (enabled() == False).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx

import db
import football_data as fd

log = logging.getLogger("betmind.oddspapi")

BASE = "https://api.oddspapi.io/v4"
SPORT_FOOTBALL = 10
CASA = "superbet.ro"                # cheia bookmakerului în răspunsul /odds
BOOKMAKER_DISPLAY = "Superbet"      # numele afișat pe bilet + textul linkului

ODDS_COOLDOWN_S = 0.55              # 500ms documentat + marjă
SUMA_MIN, SUMA_MAX = 0.85, 1.20     # sanitate: suma 1/cotă pe o piață

MARKETS_CACHE_KEY = "oddspapi_markets_football"
MARKETS_MAX_AGE_H = 24 * 7          # /markets se schimbă foarte rar: refresh săptămânal
FIXTURES_TTL_S = 15 * 60            # feed-ul de fixtures OddsPapi, cache în memorie
KICKOFF_TOLERANCE_S = 3 * 3600      # ±3 ore la potrivirea meciurilor
HTTP_TIMEOUT_S = 12.0
# Cât mai așteptăm cererea Superbet DUPĂ ce restul pachetului de date e gata.
# /odds e serializat (cooldown 500ms), deci un shortlist de 10–13 meciuri
# umple coada ~6–8s; 3s tăia linkurile de pe majoritatea rândurilor.
WAIT_BUDGET_S = 10.0
SB_TTL_S = 15 * 60                  # cache Superbet per fixture, cât feed-ul

# Piețe pe care le analizăm (API-Football). Overlay Superbet doar pe acestea.
# Corners / cartonașe / marcatori / scor corect rămân în afară.
_ANALYZED_KEYS = {
    "1x2", "over_under", "btts", "double_chance",
    "asian_handicap", "team_total_home", "team_total_away",
    "team_scores_home", "team_scores_away", "team_scores",
    "1x2_ht", "over_under_ht", "btts_ht",
    "first_to_score", "odd_even", "htft",
}


def api_key() -> str:
    return os.environ.get("ODDSPAPI_KEY", "").strip()


def enabled() -> bool:
    return bool(api_key())


# ---------------------------------------------------------------------------
# HTTP + coada rate-limitată pentru /odds (portat din oddspapi_v4.get)
# ---------------------------------------------------------------------------

_odds_lock = asyncio.Lock()   # serializează /odds la nivel de proces (per cheie)
_last_odds_call = 0.0


async def _http_get(url: str, params: dict) -> httpx.Response:
    """Singurul punct care atinge rețeaua — testele îl înlocuiesc."""
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_S) as client:
        return await client.get(url, params=params)


async def _get(path: str, params: Optional[dict] = None,
               respect_odds_cooldown: bool = False,
               incercari: int = 1) -> tuple[Optional[Any], Optional[str]]:
    """GET cu semantica validată în v4: cooldown 500ms între TOATE apelurile
    OddsPapi (limita e per cheie, nu per endpoint — /fixtures concurent cu
    /odds dădea 429), retry pe 429 (retryMs din corp) și pe 500 (backoff)."""
    global _last_odds_call
    ultima_eroare = None
    for incercare in range(1, incercari + 1):
        p = {"apiKey": api_key()}
        p.update(params or {})
        try:
            async with _odds_lock:
                asteapta = ODDS_COOLDOWN_S - (time.monotonic() - _last_odds_call)
                if asteapta > 0:
                    await asyncio.sleep(asteapta)
                # Cooldown-ul se măsoară între START-urile cererilor: așa
                # coada nu adaugă și durata HTTP la fiecare pas.
                _last_odds_call = time.monotonic()
                r = await _http_get(f"{BASE}{path}", p)
        except httpx.HTTPError as e:
            ultima_eroare = f"eroare retea: {e}"
            await asyncio.sleep(0.5 * incercare)
            continue

        if r.status_code == 429:
            try:
                retry_ms = r.json().get("error", {}).get("retryMs", 600)
            except Exception:
                retry_ms = 600
            ultima_eroare = "HTTP 429"
            await asyncio.sleep((retry_ms / 1000.0) + 0.05)
            continue

        if r.status_code == 500:
            ultima_eroare = f"HTTP 500: {r.text[:200]}"
            if incercare < incercari:
                await asyncio.sleep(1.0 * incercare)
                continue
            return None, ultima_eroare

        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {r.text[:300]}"
        try:
            return r.json(), None
        except Exception as e:
            return None, f"raspuns non-JSON: {e}"
    return None, ultima_eroare or "esuat dupa reincercari"


def _as_list(x: Any) -> list:
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        for k in ("data", "results", "items"):
            if isinstance(x.get(k), list):
                return x[k]
    return []


# ---------------------------------------------------------------------------
# /markets: sensul oficial al piețelor — cache DB săptămânal + memorie proces
# ---------------------------------------------------------------------------

_markets_mem: Optional[dict[str, dict]] = None
_markets_mem_at = 0.0
_markets_lock = asyncio.Lock()


def _parse_markets(mk_raw: Any) -> dict[str, dict]:
    """{market_id: {"nume": str, "outcomes": {outcome_id: eticheta}}} — ca în v4."""
    sens: dict[str, dict] = {}
    for m in _as_list(mk_raw):
        if not isinstance(m, dict):
            continue
        mid = str(m.get("marketId") or m.get("id") or "")
        nume = m.get("marketName") or m.get("name") or f"market {mid}"
        outcomes_doc = m.get("outcomes") or m.get("selections") or []
        mapare: dict[str, str] = {}
        if isinstance(outcomes_doc, list):
            for o in outcomes_doc:
                if isinstance(o, dict):
                    oid = str(o.get("outcomeId") or o.get("id") or "")
                    onume = o.get("outcomeName") or o.get("name") or o.get("label")
                    if oid and onume:
                        mapare[oid] = onume
        elif isinstance(outcomes_doc, dict):
            for oid, onume in outcomes_doc.items():
                mapare[str(oid)] = onume if isinstance(onume, str) else str(onume)
        handicap = m.get("handicap")
        try:
            handicap = float(handicap) if handicap is not None else None
        except (TypeError, ValueError):
            handicap = None
        if mid:
            sens[mid] = {
                "nume": nume,
                "outcomes": mapare,
                "handicap": handicap,
                "marketType": str(m.get("marketType") or ""),
                "period": str(m.get("period") or ""),
            }
    return sens


async def get_markets_map(force: bool = False) -> dict[str, dict]:
    """Definițiile piețelor, cu cache DB (7 zile) și degradare pe cache vechi."""
    global _markets_mem, _markets_mem_at
    async with _markets_lock:
        if _markets_mem is not None and not force and \
                time.monotonic() - _markets_mem_at < 6 * 3600:
            return _markets_mem

        cached = await db.oddspapi_cache_get(MARKETS_CACHE_KEY)
        mk_raw = None
        if cached and not force:
            try:
                fetched = datetime.fromisoformat(cached["fetched_at"])
                age_h = (datetime.now(timezone.utc) - fetched).total_seconds() / 3600
                if age_h < MARKETS_MAX_AGE_H:
                    mk_raw = json.loads(cached["json"])
            except (ValueError, TypeError, json.JSONDecodeError):
                mk_raw = None

        if mk_raw is None:
            raw, err = await _get("/markets", {"sportId": SPORT_FOOTBALL}, incercari=3)
            if err or raw is None:
                log.warning("OddsPapi /markets indisponibil: %s", err)
                if cached:
                    # degradare elegantă: cache-ul vechi e mai bun decât nimic
                    try:
                        mk_raw = json.loads(cached["json"])
                    except json.JSONDecodeError:
                        mk_raw = []
                else:
                    mk_raw = []
            else:
                mk_raw = raw
                await db.oddspapi_cache_set(
                    MARKETS_CACHE_KEY, json.dumps(raw, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat())

        _markets_mem = _parse_markets(mk_raw)
        _markets_mem_at = time.monotonic()
        return _markets_mem


async def rezolva_sens_outcome(market_id: Any, outcome_id: Any) -> Optional[str]:
    """Eticheta text a unui outcome, DOAR dacă e documentată în /markets.

    None = sens neconfirmat. Interzis orice fallback pe mărimea cotei."""
    info = (await get_markets_map()).get(str(market_id))
    if not info:
        return None
    return info["outcomes"].get(str(outcome_id))


# ---------------------------------------------------------------------------
# Potrivirea meciurilor API-Football <-> OddsPapi (nume normalizate + ±3h)
# ---------------------------------------------------------------------------

# Prefixe/sufixe de club care diferă frecvent între surse.
_NOISE_TOKENS = {
    "fc", "cf", "cs", "csm", "csu", "acs", "afc", "ac", "as", "sc", "ssc",
    "sv", "us", "ud", "cd", "ca", "rc", "fk", "sk", "bk", "nk", "kv", "kaa",
    "rcd", "vfb", "vfl", "tsg", "bsc", "spvgg", "calcio", "club",
    "olympique", "borussia", "le", "de", "the", "sd",
}

# Abrevieri / porecle univoce (token sau nume întreg). Nu punem «inter» —
# ar lipi Inter de Inter Miami. «man» e sigur: Man Utd ≠ Man City rămân
# distincți prin united/city după expand.
_TOKEN_EXPAND = {
    "utd": "united",
    "man": "manchester",
    "nottm": "nottingham",
    "notts": "nottingham",
    "wolves": "wolverhampton",
    "spurs": "tottenham",
    "psg": "paris",
    "hamburger": "hamburg",
}
_NICKNAMES = {
    "psg": "paris saint germain",
    "paris sg": "paris saint germain",
    "psv": "psv eindhoven",
    "spurs": "tottenham",
    "wolves": "wolverhampton",
    "man city": "manchester city",
    "man utd": "manchester united",
    "man united": "manchester united",
    "nottm forest": "nottingham forest",
}


def _norm_team(name: Any) -> str:
    """Nume de echipă comparabil între surse: fără diacritice, prefixe, punctuație."""
    s = unicodedata.normalize("NFKD", str(name or ""))
    s = s.encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    tokens = [t for t in s.split() if t not in _NOISE_TOKENS]
    joined = " ".join(tokens) if tokens else " ".join(s.split())
    return _NICKNAMES.get(joined, joined)


def _team_tokens(name: Any) -> list[str]:
    n = _norm_team(name)
    n = _NICKNAMES.get(n, n)
    raw = [t for t in n.split() if t and not t.isdigit()]
    return [_TOKEN_EXPAND.get(t, t) for t in raw]


def _stem_related(a: str, b: str, min_len: int = 6) -> bool:
    """hamburg ≈ hamburger, tottenham ≈ tottenham. inter (5) ≱ min_len 6."""
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_n = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= min_len and long_n.startswith(short)


def _core_token(tokens: list[str]) -> str:
    """Cel mai lung token; la egalitate, ultimul (Inter Miami → miami, nu inter)."""
    if not tokens:
        return ""
    return max(tokens, key=lambda t: (len(t), tokens.index(t)))


def _teams_match(a: Any, b: Any) -> bool:
    """Potrivire generală de cluburi între surse, nu o listă de cazuri.

    Acceptă prefix/sufix (Tottenham ≈ Tottenham Hotspur), abrevieri
    (Man City ≈ Manchester City), tulpini (Hamburg ≈ Hamburger SV) și
    token-ul distinctiv (Leverkusen ≈ Bayer Leverkusen). Respinge
    conflicte de identitate: Inter ≠ Inter Miami, United ≠ City.
    """
    ta, tb = _team_tokens(a), _team_tokens(b)
    if not ta or not tb:
        return False
    sa, sb = set(ta), set(tb)
    if sa == sb:
        return True

    na, nb = " ".join(ta), " ".join(tb)
    short, long_n = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(short) >= 6 and (
        long_n.startswith(short + " ")
        or long_n.endswith(" " + short)
        or long_n.startswith(short)
    ):
        return True

    # Mulțimea scurtă e inclusă în cea lungă, cu un token destul de
    # distinctiv (≥6) — altfel {inter} ⊆ {inter, miami}.
    if sa < sb or sb < sa:
        smaller = sa if sa < sb else sb
        if smaller and max(len(t) for t in smaller) >= 6:
            return True

    ca, cb = _core_token(ta), _core_token(tb)
    if ca and _stem_related(ca, cb, min_len=4):
        ra = {t for t in sa if t != ca and not _stem_related(t, ca, 4)}
        rb = {t for t in sb if t != cb and not _stem_related(t, cb, 4)}
        if ra and rb and not (ra <= rb or rb <= ra):
            return False
        extra = (ra | rb) - (ra & rb)
        core_len = min(len(ca), len(cb))
        if extra and core_len < 6 and any(len(t) >= 4 for t in extra):
            return False
        return True
    return False


def _name_sim(a: Any, b: Any) -> float:
    """Similaritate 0–1. 1.0 doar când _teams_match e adevărat."""
    if _teams_match(a, b):
        return 1.0
    ta, tb = _team_tokens(a), _team_tokens(b)
    na, nb = " ".join(ta), " ".join(tb)
    if not na or not nb:
        return 0.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    # Bonus doar pe token-uri distinctive (≥6). «inter» comun nu trebuie
    # să apropie Inter de Inter Miami peste pragul de potrivire.
    distinctive = [t for t in (set(ta) & set(tb)) if len(t) >= 6]
    if distinctive:
        ratio = min(0.99, ratio + min(0.25, 0.04 * max(len(t) for t in distinctive)))
    # Secvență-prefix: Tottenham ⊂ Tottenham Hotspur. Inter ⊂ Inter Miami
    # primește bonus doar dacă primul token e ≥6 (Inter are 5).
    if ta == tb[:len(ta)] or tb == ta[:len(tb)]:
        shorter = ta if len(ta) <= len(tb) else tb
        if shorter and len(shorter[0]) >= 6:
            ratio = max(ratio, 0.88)
    return ratio


def _parse_kickoff(value: Any) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        # fixture store ține ora locală a aplicației
        dt = dt.replace(tzinfo=ZoneInfo(os.environ.get("APP_TIMEZONE", "Europe/Bucharest")))
    return dt


_fixtures_cache: dict[str, tuple[float, list]] = {}
_fixtures_lock = asyncio.Lock()


async def _fixtures_feed(date_from: str, date_to: str) -> list[dict]:
    """Feed-ul OddsPapi pe interval de zile. O fereastră care acoperă cererea
    e refolosită — altfel 13 meciuri = 13 GET-uri /fixtures și 429."""
    async with _fixtures_lock:
        now = time.monotonic()
        for key, (ts, items) in _fixtures_cache.items():
            if now - ts >= FIXTURES_TTL_S:
                continue
            try:
                a, b = key.split("|", 1)
            except ValueError:
                continue
            if a <= date_from and date_to <= b:
                return items
        raw, err = await _get("/fixtures", {
            "sportId": SPORT_FOOTBALL,
            "from": date_from,
            "to": date_to,
            "hasOdds": "true",
        }, incercari=3)
        if err or raw is None:
            log.debug("OddsPapi /fixtures indisponibil (%s..%s): %s", date_from, date_to, err)
            for key, (ts, items) in _fixtures_cache.items():
                if now - ts < FIXTURES_TTL_S and items:
                    return items
            return []
        items = _as_list(raw)
        _fixtures_cache[f"{date_from}|{date_to}"] = (time.monotonic(), items)
        return items


async def match_fixture(fx: dict) -> Optional[dict]:
    """Meciul OddsPapi corespunzător unui fixture API-Football, sau None.

    Potrivire pe nume (exact / token / similaritate) + kickoff ±3h.
    0 candidați, 2+ potriviri perfecte sau un câștigător fără marjă =
    potrivire NESIGURĂ => None (fallback tăcut pe API-Football)."""
    kickoff = _parse_kickoff(fx.get("kickoff_iso"))
    if kickoff is None:
        return None
    day = kickoff.astimezone(timezone.utc).date()
    feed = await _fixtures_feed((day - timedelta(days=1)).isoformat(),
                                (day + timedelta(days=1)).isoformat())
    home, away = fx.get("home_name"), fx.get("away_name")
    scored: list[tuple[float, dict]] = []
    for f in feed:
        if not isinstance(f, dict):
            continue
        start = _parse_kickoff(f.get("startTime"))
        if start is None or abs((start - kickoff).total_seconds()) > KICKOFF_TOLERANCE_S:
            continue
        sh = _name_sim(home, f.get("participant1Name"))
        sa = _name_sim(away, f.get("participant2Name"))
        if sh >= 0.70 and sa >= 0.70:
            scored.append((sh * sa, f))
    if not scored:
        log.info("OddsPapi: fără potrivire unică pentru %s vs %s (candidați=%d)",
                 home, away, 0)
        return None
    perfect = [f for s, f in scored if s >= 0.999]
    if len(perfect) == 1:
        return perfect[0]
    if len(perfect) > 1:
        log.info("OddsPapi: fără potrivire unică pentru %s vs %s (candidați=%d)",
                 home, away, len(perfect))
        return None
    scored.sort(key=lambda x: -x[0])
    best, winner = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    if best >= 0.64 and best >= second + 0.12:
        return winner
    log.info("OddsPapi: fără potrivire unică pentru %s vs %s (candidați=%d)",
             home, away, len(scored))
    return None


# ---------------------------------------------------------------------------
# Maparea piețelor/outcome-urilor OddsPapi pe cheile interne analizate
# ---------------------------------------------------------------------------

_DENY_TYPE = ("corner", "booking", "player", "correctscore", "scorer")


def _period_bucket(period: Any) -> Optional[str]:
    """'ft' / 'ht' / None (repriză 2, overtime, sferturi — nu le analizăm)."""
    p = str(period or "").strip().lower()
    if p in ("", "fulltime", "ft"):
        return "ft"
    if p in ("p1", "firsthalf", "1h", "first half"):
        return "ht"
    return None


def _internal_key(info: dict) -> Optional[str]:
    """Cheia internă din marketType+period, cu fallback pe nume (teste / cache vechi)."""
    mtype = str(info.get("marketType") or "").strip().lower()
    name = str(info.get("nume") or "").strip().lower()
    blob = f"{mtype} {name}"
    if any(d in blob for d in _DENY_TYPE):
        return None
    bucket = _period_bucket(info.get("period"))
    if info.get("period") and bucket is None:
        return None  # period explicit dar nerelevant (p2, result…)
    ht = bucket == "ht"

    if mtype == "1x2":
        return "1x2_ht" if ht else "1x2"
    if mtype == "bothteamsscore":
        return "btts_ht" if ht else "btts"
    if mtype == "doublechance":
        return None if ht else "double_chance"
    if mtype == "totals":
        return "over_under_ht" if ht else "over_under"
    if mtype == "spreads":
        return None if ht else "asian_handicap"
    if mtype == "teamtotals-team1":
        return None if ht else "team_total_home"
    if mtype == "teamtotals-team2":
        return None if ht else "team_total_away"
    if mtype == "toscore-team1":
        return None if ht else "team_scores_home"
    if mtype == "toscore-team2":
        return None if ht else "team_scores_away"
    if mtype == "firstgoal":
        return None if ht else "first_to_score"
    if mtype == "oddeven":
        return None if ht else "odd_even"
    if mtype == "halftime-fulltime":
        return None if ht else "htft"
    if mtype:
        return None
    return _key_from_name(name)


def _key_from_name(market_name: str) -> Optional[str]:
    """Fallback când /markets n-are marketType (payload-uri de test, cache vechi)."""
    n = (market_name or "").strip().lower()
    if not n:
        return None
    if any(w in n for w in ("corner", "card", "booking", "scorer", "player",
                            "exact", "correct")):
        return None
    ht = (("first half" in n or "1st half" in n) and "second" not in n)
    if "double chance" in n:
        return None if ht else "double_chance"
    if ("both" in n and "score" in n) or n in ("btts", "gg/ng"):
        return "btts_ht" if ht else "btts"
    if "asian handicap" in n:
        return None if ht else "asian_handicap"
    if "team 1 to score" in n or n == "team 1 to score":
        return None if ht else "team_scores_home"
    if "team 2 to score" in n:
        return None if ht else "team_scores_away"
    if "over under" in n or "over/under" in n or "total goals" in n or n == "total":
        if "team 1" in n:
            return None if ht else "team_total_home"
        if "team 2" in n:
            return None if ht else "team_total_away"
        return "over_under_ht" if ht else "over_under"
    if "first goal" in n or n == "first to score":
        return "first_to_score"
    if "odd even" in n or n in ("odd/even", "odd even"):
        return None if ht else "odd_even"
    if n in ("1x2", "match result", "full time result", "full-time result",
             "match winner", "ft result", "result", "match odds", "winner"):
        return "1x2_ht" if ht else "1x2"
    if "first half result" in n:
        return "1x2_ht"
    if "half time" in n and "full time" in n:
        return "htft"
    return None


_OU_RE = re.compile(r"^(over|under)\s*\(?\s*(\d+(?:[.,]\d+)?)\s*\)?$")


def _ou_line(handicap: Any) -> Optional[str]:
    """1.5 → '1.5'; 2.0 → '2'. None dacă handicapul lipsește."""
    try:
        x = float(handicap)
    except (TypeError, ValueError):
        return None
    if x == int(x):
        return str(int(x))
    return f"{x:.1f}"


def _signed_ah(line: float) -> str:
    """-0.5 → '-0.5'; 0.5 → '+0.5'; 0 → '0' — formatul API-Football."""
    if abs(line) < 1e-9:
        return "0"
    if line == int(line):
        return f"{int(line):+d}"
    s = f"{line:.2f}".rstrip("0").rstrip(".")
    return s if s.startswith("-") else f"+{s}"


def _outcome_value(key: str, label: str, handicap: Any = None) -> Optional[str]:
    """Eticheta OddsPapi (rezolvată din /markets) -> valoarea internă a
    outcome-ului, în formatul folosit de pachetul de cote API-Football.

    Over/Under și AH la OddsPapi sunt o piață per linie (handicap 1.5 / -0.5)
    cu outcome-uri Over/Under sau 1/2 — linia vine din handicap, nu din etichetă.
    """
    l = (label or "").strip().lower()
    if key in ("1x2", "1x2_ht"):
        if l in ("1", "home", "home win"):
            return "Home"
        if l in ("x", "draw", "tie"):
            return "Draw"
        if l in ("2", "away", "away win"):
            return "Away"
        return None
    if key in ("btts", "btts_ht"):
        if l in ("yes", "gg", "both teams to score - yes"):
            return "Yes"
        if l in ("no", "ng", "both teams to score - no"):
            return "No"
        return None
    if key == "double_chance":
        if l in ("1x", "x1", "1/x", "home/draw", "home or draw"):
            return "Home/Draw"
        if l in ("x2", "2x", "x/2", "draw/away", "draw or away"):
            return "Draw/Away"
        if l in ("12", "1/2", "home/away", "home or away"):
            return "Home/Away"
        return None
    if key in ("over_under", "over_under_ht", "team_total_home", "team_total_away"):
        m = _OU_RE.match(l)
        if m:
            return f"{m.group(1).capitalize()} {m.group(2).replace(',', '.')}"
        if l in ("over", "under"):
            line = _ou_line(handicap)
            if line:
                return f"{l.capitalize()} {line}"
        return None
    if key in ("team_scores_home", "team_scores_away", "team_scores"):
        if l in ("yes", "gg"):
            return "Yes"
        if l in ("no", "ng"):
            return "No"
        return None
    if key == "asian_handicap":
        try:
            line = float(handicap)
        except (TypeError, ValueError):
            return None
        if l in ("1", "home"):
            return f"Home {_signed_ah(line)}"
        if l in ("2", "away"):
            return f"Away {_signed_ah(-line)}"
        return None
    if key == "first_to_score":
        if l in ("1", "home"):
            return "Home"
        if l in ("2", "away"):
            return "Away"
        if "no" in l:
            return "None"
        return None
    if key == "odd_even":
        if l in ("odd", "impar"):
            return "Odd"
        if l in ("even", "par"):
            return "Even"
        return None
    if key == "htft":
        parts = l.replace(" ", "").replace("-", "/").upper().split("/")
        if len(parts) != 2:
            return None
        def _side(p: str) -> Optional[str]:
            if p in ("1", "HOME", "H"):
                return "Home"
            if p in ("X", "DRAW", "D"):
                return "Draw"
            if p in ("2", "AWAY", "A"):
                return "Away"
            return None
        a, b = _side(parts[0]), _side(parts[1])
        return f"{a}/{b}" if a and b else None
    return None


def odds_label_with_link(odd: Any, bookmaker: Optional[str],
                         link: Optional[str] = None) -> Optional[str]:
    """Doar numărul. Nicio casă în etichetă — Superbet e butonul separat."""
    return fd.format_odds_label(odd, bookmaker)


def _link_ro(path: Any) -> Optional[str]:
    """fixturePath -> link superbet.ro: doar domeniul se schimbă, calea rămâne."""
    if not path:
        return None
    p = str(path).strip()
    if not p:
        return None
    if p.startswith("http"):
        return p.replace("superbet.com", "superbet.ro")
    return "https://superbet.ro" + (p if p.startswith("/") else "/" + p)


# ---------------------------------------------------------------------------
# Cotele Superbet RO pentru un meci din shortlist
# ---------------------------------------------------------------------------

_sb_mem: dict[int, tuple[float, Optional[dict]]] = {}
_sb_inflight: dict[int, asyncio.Task] = {}
_sb_gate = asyncio.Lock()
_HOUSE_PAREN = re.compile(r"\s*\(\s*[A-Za-z0-9][A-Za-z0-9 .+_-]{0,30}\s*\)")

MAP_NONE_RETRY_S = 6 * 3600   # cât timp nu re-încercăm un fixture marcat «none»
PACK_LINK_MAX_AGE_S = 6 * 3600  # linkul rămâne valid mult după ce cota expiră


def reset_runtime_state() -> None:
    """Cache-uri de proces — testele le golesc ca să nu se scurgă între cazuri."""
    _sb_mem.clear()
    _sb_inflight.clear()
    _fixtures_cache.clear()


def _sb_entry(fid: int) -> Optional[tuple[float, Optional[dict]]]:
    hit = _sb_mem.get(fid)
    if not hit or time.monotonic() - hit[0] >= SB_TTL_S:
        return None
    return hit


def _sb_cached(fid: int) -> Optional[dict]:
    hit = _sb_entry(fid)
    return hit[1] if hit else None


def _sb_to_json(sb: dict) -> str:
    """Pachetul Superbet, serializabil: cheile tuple (piață, valoare) devin
    liste [piață, valoare, cotă]."""
    out = {k: v for k, v in sb.items() if k != "odds"}
    out["odds"] = [[k, v, price] for (k, v), price in (sb.get("odds") or {}).items()]
    return json.dumps(out, ensure_ascii=False)


def _sb_from_json(text: str) -> Optional[dict]:
    try:
        raw = json.loads(text)
        odds = {(str(k), str(v)): float(price) for k, v, price in raw.get("odds") or []}
        raw["odds"] = odds
        return raw
    except Exception:
        return None


def _age_seconds(fetched_at: Any) -> Optional[float]:
    try:
        dt = datetime.fromisoformat(str(fetched_at))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return None


async def _pack_from_db(fid: int) -> Optional[dict]:
    """Snapshot-ul Superbet din DB: cu cote dacă e proaspăt (≤ TTL), doar cu
    link dacă e mai vechi (linkul paginii de meci nu expiră ca o cotă)."""
    try:
        row = await db.superbet_pack_get(fid)
    except Exception:
        return None
    if not row:
        return None
    sb = _sb_from_json(row.get("json") or "")
    if not sb:
        return None
    age = _age_seconds(row.get("fetched_at"))
    if age is None or age > PACK_LINK_MAX_AGE_S:
        return None
    if age > SB_TTL_S:
        sb["odds"] = {}
    return sb


async def _map_store(fid: Optional[int], opp_id: Optional[str],
                     method: str, confidence: float) -> None:
    if fid is None:
        return
    try:
        await db.oddspapi_map_set(fid, opp_id, method, confidence,
                                  datetime.now(timezone.utc).isoformat())
    except Exception as e:
        log.debug("OddsPapi: maparea %s nu s-a putut salva: %s", fid, e)


async def _resolve_opp_id(fx: dict) -> Optional[str]:
    """ID-ul OddsPapi al meciului: întâi maparea persistentă din DB (rezolvată
    o singură dată per fixture), abia apoi potrivirea pe nume."""
    try:
        fid = int(fx.get("fixture_id"))
    except (TypeError, ValueError):
        fid = None
    if fid is not None:
        try:
            row = await db.oddspapi_map_get(fid)
        except Exception:
            row = None
        if row:
            if row.get("opp_fixture_id"):
                return str(row["opp_fixture_id"])
            age = _age_seconds(row.get("created_at"))
            if age is not None and age < MAP_NONE_RETRY_S:
                return None
    opp = await match_fixture(fx)
    opp_id = opp.get("fixtureId") if isinstance(opp, dict) else None
    if opp_id:
        await _map_store(fid, str(opp_id), "name", 1.0)
        return str(opp_id)
    return None


async def superbet_for_fixture(fx: dict) -> Optional[dict]:
    """Pachetul Superbet RO pentru un fixture API-Football, sau None.

    None acoperă TOATE cazurile de incertitudine (cheie lipsă, meci
    nepotrivit, sens neconfirmat, sumă anormală, timeout, orice excepție) —
    apelantul cade tăcut pe cotele API-Football. Rezultatul e cache-uit pe
    fixture_id ca prefetch-ul din analyze_matches și apelul din
    assemble_data_pack să nu dubleze /odds."""
    if not enabled():
        return None
    try:
        fid_raw = fx.get("fixture_id") if isinstance(fx, dict) else None
        try:
            fid = int(fid_raw) if fid_raw is not None else None
        except (TypeError, ValueError):
            fid = None
        if fid is None:
            return await _superbet_for_fixture(fx)
        async with _sb_gate:
            fresh = _sb_entry(fid)
            if fresh is not None:
                return fresh[1]
            task = _sb_inflight.get(fid)
            if task is None:
                task = asyncio.create_task(_superbet_for_fixture(fx))
                _sb_inflight[fid] = task
        try:
            result = await task
        except Exception:
            result = None
        async with _sb_gate:
            _sb_mem[fid] = (time.monotonic(), result)
            if _sb_inflight.get(fid) is task:
                _sb_inflight.pop(fid, None)
        return result
    except Exception as e:
        log.debug("OddsPapi fallback pentru fixture %s: %s", fx.get("fixture_id"), e)
        return None


async def prefetch_for_fixtures(fixture_ids: list[int]) -> None:
    """O singură cerere /fixtures pe tot intervalul, mapare pe tot slate-ul
    (nume → context de slate → LLM), apoi /odds serializat.

    Totul rulează în fundal, în paralel cu analiza meciurilor — nu adaugă
    nimic pe drumul cererii. Mapările și cotele ajung în DB, deci biletul
    le găsește indiferent de timing."""
    if not enabled() or not fixture_ids:
        return
    fxs: list[dict] = []
    days: list = []
    for fid in fixture_ids:
        try:
            fx = await db.get_fixture(int(fid))
        except (TypeError, ValueError):
            continue
        if not fx:
            continue
        fxs.append(fx)
        ko = _parse_kickoff(fx.get("kickoff_iso"))
        if ko is not None:
            days.append(ko.astimezone(timezone.utc).date())
    feed: list[dict] = []
    if days:
        feed = await _fixtures_feed(
            (min(days) - timedelta(days=1)).isoformat(),
            (max(days) + timedelta(days=1)).isoformat(),
        )
    try:
        await _map_slate(fxs, feed)
    except Exception as e:
        log.debug("OddsPapi: maparea pe slate a eșuat: %s", e)
    for fx in fxs:
        asyncio.create_task(superbet_for_fixture(fx))


SLATE_EXACT_TOL_S = 45 * 60      # fereastra strânsă pentru pasul de slate
SLATE_MIN_SIM = 0.45             # similaritate minimă per echipă în pasul de slate


async def _map_slate(fxs: list[dict], feed: list[dict]) -> None:
    """Rezolvă maparea pentru tot shortlist-ul, o singură dată per fixture.

    Trepte: (1) maparea din DB, (2) potrivirea pe nume, (3) contextul de
    slate — candidat unic la aceeași oră, cu nume măcar înrudite, după ce
    scoatem meciurile deja revendicate, (4) LLM pe lot pentru restul.
    Rezultatul (inclusiv «none») e persistat ca să nu repetăm munca."""
    claimed: set[str] = set()
    unmatched: list[dict] = []
    for fx in fxs:
        try:
            fid = int(fx.get("fixture_id"))
        except (TypeError, ValueError):
            continue
        try:
            row = await db.oddspapi_map_get(fid)
        except Exception:
            row = None
        if row and row.get("opp_fixture_id"):
            claimed.add(str(row["opp_fixture_id"]))
            continue
        if row:
            age = _age_seconds(row.get("created_at"))
            if age is not None and age < MAP_NONE_RETRY_S:
                continue
        opp = await match_fixture(fx)
        opp_id = opp.get("fixtureId") if isinstance(opp, dict) else None
        if opp_id:
            await _map_store(fid, str(opp_id), "name", 1.0)
            claimed.add(str(opp_id))
            continue
        unmatched.append(fx)

    still_open: list[tuple[dict, list[dict]]] = []
    for fx in unmatched:
        fid = int(fx["fixture_id"])
        kickoff = _parse_kickoff(fx.get("kickoff_iso"))
        if kickoff is None:
            continue
        home, away = fx.get("home_name"), fx.get("away_name")
        near: list[tuple[float, dict]] = []      # candidați pentru LLM (±3h)
        strong: list[tuple[float, dict]] = []    # candidați pentru pasul de slate
        for f in feed:
            if not isinstance(f, dict):
                continue
            oid = str(f.get("fixtureId") or "")
            if not oid or oid in claimed:
                continue
            start = _parse_kickoff(f.get("startTime"))
            if start is None:
                continue
            dt = abs((start - kickoff).total_seconds())
            if dt > KICKOFF_TOLERANCE_S:
                continue
            sh = _name_sim(home, f.get("participant1Name"))
            sa = _name_sim(away, f.get("participant2Name"))
            near.append((sh * sa, f))
            if dt <= SLATE_EXACT_TOL_S and sh >= SLATE_MIN_SIM and sa >= SLATE_MIN_SIM:
                strong.append((sh * sa, f))
        strong.sort(key=lambda x: -x[0])
        if strong and (len(strong) == 1
                       or strong[0][0] >= strong[1][0] + 0.15):
            opp_id = str(strong[0][1].get("fixtureId"))
            await _map_store(fid, opp_id, "slate", round(strong[0][0], 3))
            claimed.add(opp_id)
            continue
        near.sort(key=lambda x: -x[0])
        still_open.append((fx, [f for _, f in near[:10]]))

    if not still_open:
        return
    try:
        rezolvari = await _llm_match_batch(still_open)
    except Exception as e:
        log.info("OddsPapi: potrivirea LLM a eșuat (%s) — reîncercăm data viitoare", e)
        return
    for fx, _cands in still_open:
        fid = int(fx["fixture_id"])
        opp_id = rezolvari.get(fid)
        if opp_id and opp_id not in claimed:
            await _map_store(fid, opp_id, "llm", 0.9)
            claimed.add(opp_id)
        else:
            # Negăsit nici de LLM: notăm «none» ca să nu repetăm lotul la
            # fiecare cerere; expiră după MAP_NONE_RETRY_S.
            await _map_store(fid, None, "none", 0.0)


async def _llm_match_batch(
        items: list[tuple[dict, list[dict]]]) -> dict[int, Optional[str]]:
    """Un singur apel către modelul mic care decide, pentru fiecare meci
    nepotrivit, care candidat OddsPapi este același meci (sau null).

    Rulează doar în fundal (prefetch), deci nu costă latență pe cerere."""
    if os.environ.get("ODDSPAPI_LLM_MATCH", "1").strip().lower() in ("0", "false", "no"):
        return {}
    if not items or not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return {}
    from anthropic import AsyncAnthropic

    payload = []
    for fx, cands in items:
        payload.append({
            "fixture_id": fx.get("fixture_id"),
            "home": fx.get("home_name"),
            "away": fx.get("away_name"),
            "kickoff_utc": fx.get("kickoff_iso"),
            "league": fx.get("league_name"),
            "candidates": [{
                "id": str(c.get("fixtureId")),
                "home": c.get("participant1Name"),
                "away": c.get("participant2Name"),
                "start_utc": c.get("startTime"),
                "tournament": c.get("tournamentName"),
            } for c in cands],
        })
    prompt = (
        "You match football fixtures between two data providers. Team names "
        "may differ in spelling, language or abbreviation, but it must be the "
        "SAME match (same two clubs, same kickoff). For each fixture pick the "
        "candidate id that is the same match, or null if none clearly is. "
        "Never guess between two plausible candidates.\n"
        "Answer with ONLY a JSON object mapping fixture_id (string) to "
        "candidate id (string) or null.\n\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    model = os.environ.get("ANALYST_MODEL", "").strip() or "claude-haiku-4-5-20251001"
    client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip())
    msg = await asyncio.wait_for(
        client.messages.create(model=model, max_tokens=800, temperature=0,
                               messages=[{"role": "user", "content": prompt}]),
        timeout=20.0)
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    raw = json.loads(m.group(0))
    out: dict[int, Optional[str]] = {}
    valid_ids = {str(c.get("fixtureId")) for _, cands in items for c in cands}
    for k, v in raw.items():
        try:
            fid = int(k)
        except (TypeError, ValueError):
            continue
        opp_id = str(v) if v else None
        # Doar candidați propuși de noi — LLM-ul nu are voie să inventeze ID-uri.
        out[fid] = opp_id if opp_id in valid_ids else None
    return out


def _sb_price_for_pick(sb: dict, market: Any, pick: Any) -> Optional[float]:
    prices = sb.get("odds") or {}
    if not prices:
        return None
    key = fd.normalize_market_name(str(market or "")) or str(market or "").lower()
    pick_n = fd._normalize_outcome(key, pick) if key else str(pick or "")
    want = {str(pick or "").lower(), str(pick_n or "").lower()}
    for (k, v), price in prices.items():
        if k == key and str(v).lower() in want:
            try:
                return float(price)
            except (TypeError, ValueError):
                return None
    return None


async def apply_superbet_to_selections(selections: Optional[list]) -> None:
    """Pune cota + linkul Superbet pe selecțiile biletului, ocolind modelul.

    Modelul uită adesea bookmaker_link și copiază «(Betano)». Aici
    reatașăm după fixture_id + piață/pick, din memoria procesului sau din
    snapshot-ul DB — deci merge și dacă /odds s-a terminat după bilet ori
    procesul a fost repornit între timp."""
    for s in selections or []:
        if not isinstance(s, dict):
            continue
        label = s.get("odds_label")
        if label:
            s["odds_label"] = _HOUSE_PAREN.sub("", str(label)).strip()
        try:
            fid = int(s["fixture_id"])
        except (KeyError, TypeError, ValueError):
            continue
        sb = _sb_cached(fid)
        if not sb:
            sb = await _pack_from_db(fid)
        if not sb:
            continue
        link = str(sb.get("link") or "").strip()
        price = _sb_price_for_pick(sb, s.get("market"), s.get("pick"))
        if price:
            s["odds"] = price
            s["display_odd"] = price
            s["odds_label"] = f"{price:.2f}"
            s["display_bookmaker"] = BOOKMAKER_DISPLAY
            s["bookmaker_name"] = BOOKMAKER_DISPLAY
        if link:
            s["bookmaker_link"] = link
            s["bookmaker_name"] = BOOKMAKER_DISPLAY


async def superbet_links_for_fixtures(fixture_ids: Optional[list] = None) -> list[dict]:
    """Link Superbet pe pagina meciului, indiferent de piața aleasă.

    Folosit când modelul a scris un tabel fără build_ticket (pariuri exotice
    pe un singur meci): butonul duce la Superbet, nu pretinde că numărul e
    cota Superbet a pieței exotice."""
    out: list[dict] = []
    seen: set[int] = set()
    for raw in fixture_ids or []:
        try:
            fid = int(raw)
        except (TypeError, ValueError):
            continue
        if fid in seen:
            continue
        seen.add(fid)
        sb = _sb_cached(fid) or await _pack_from_db(fid)
        if not sb:
            continue
        link = str(sb.get("link") or "").strip()
        if not link:
            continue
        match = ""
        try:
            fx = await db.get_fixture(fid)
        except Exception:
            fx = None
        if fx:
            match = f"{fx.get('home_name') or ''} vs {fx.get('away_name') or ''}".strip()
        if match in ("", "vs"):
            home, away = sb.get("home_name") or "", sb.get("away_name") or ""
            match = f"{home} vs {away}".strip()
        out.append({"fixture_id": fid, "match": match, "link": link,
                    "bookmaker_link": link, "bookmaker_name": BOOKMAKER_DISPLAY})
    return out


async def _superbet_for_fixture(fx: dict) -> Optional[dict]:
    try:
        fid = int(fx.get("fixture_id"))
    except (TypeError, ValueError):
        fid = None

    # Snapshot proaspăt în DB => zero apeluri de rețea (supraviețuiește
    # restartului de proces, spre deosebire de cache-ul din memorie).
    if fid is not None:
        pack = await _pack_from_db(fid)
        if pack and pack.get("odds"):
            return pack

    sens_piata = await get_markets_map()
    opp_id = await _resolve_opp_id(fx)
    if not opp_id:
        return None

    odds, err = await _get(
        "/odds",
        {"fixtureId": opp_id, "bookmakers": CASA,
         "oddsFormat": "decimal", "verbosity": 3},
        respect_odds_cooldown=True, incercari=2)
    if err or not isinstance(odds, dict):
        log.debug("OddsPapi /odds esuat pentru %s: %s", opp_id, err)
        return None

    bk = (odds.get("bookmakerOdds") or {}).get(CASA)
    if not isinstance(bk, dict):
        return None

    link = _link_ro(bk.get("fixturePath") or odds.get("fixturePath"))

    prices: dict[tuple[str, str], float] = {}
    for mid, m in (bk.get("markets") or {}).items():
        info = sens_piata.get(str(mid))
        if not info:
            continue  # piață nedocumentată => sens neconfirmat => nefolosită
        key = _internal_key(info)
        if not key or key not in _ANALYZED_KEYS:
            continue

        randuri: list[tuple[Optional[str], Any]] = []
        for oid, o in (m.get("outcomes") or {}).items():
            for _pid, pl in (o.get("players") or {}).items():
                randuri.append((info["outcomes"].get(str(oid)), pl.get("price")))
        if not randuri:
            continue

        # Regulile validate în v4: TOATE outcome-urile etichetate + suma
        # probabilităților implicite în intervalul normal. Altfel: NEFOLOSITĂ.
        if any(eticheta is None for eticheta, _ in randuri):
            log.debug("OddsPapi: sens neconfirmat pe piata %s (%s) — nefolosita",
                      mid, info["nume"])
            continue
        cote_valide = [c for _, c in randuri if isinstance(c, (int, float)) and c > 1.0]
        if not cote_valide or len(cote_valide) != len(randuri):
            continue
        suma = sum(1.0 / c for c in cote_valide)
        if not (SUMA_MIN <= suma <= SUMA_MAX):
            log.debug("OddsPapi: suma probabilitati %.3f anormala pe piata %s — nefolosita",
                      suma, info["nume"])
            continue

        for eticheta, cota in randuri:
            value = _outcome_value(key, eticheta, info.get("handicap"))
            if value:
                prices[(key, value)] = float(cota)

    if not prices:
        return None
    pack = {
        "bookmaker_name": BOOKMAKER_DISPLAY,
        "bookmaker_key": CASA,
        "link": link,
        "odds": prices,
        "oddspapi_fixture_id": opp_id,
        "home_name": fx.get("home_name"),
        "away_name": fx.get("away_name"),
    }
    if fid is not None:
        try:
            await db.superbet_pack_set(
                fid, _sb_to_json(pack), datetime.now(timezone.utc).isoformat())
        except Exception as e:
            log.debug("OddsPapi: snapshot Superbet %s nesalvat: %s", fid, e)
    return pack


# ---------------------------------------------------------------------------
# Suprascrierea pachetului de cote API-Football + așteptare cu buget de timp
# ---------------------------------------------------------------------------

def overlay_on_odds_pack(odds_pack: Any, sb: Optional[dict]) -> int:
    """Înlocuiește cotele API-Football cu cele Superbet RO pentru piețele
    deja prezente în pachet (cele analizate). Nu adaugă piețe noi. Returnează
    numărul de outcome-uri înlocuite."""
    if not sb or not isinstance(odds_pack, dict) or odds_pack.get("error"):
        return 0
    prices = sb.get("odds") or {}
    if not prices:
        return 0
    name = sb.get("bookmaker_name") or BOOKMAKER_DISPLAY
    link = sb.get("link")

    replaced = 0
    for m in odds_pack.get("markets") or []:
        key = m.get("key")
        for o in m.get("outcomes") or []:
            price = prices.get((key, o.get("value")))
            if not price:
                continue
            price = round(float(price), 3)
            # Cota Superbet devine cota folosită peste tot: implied_prob se
            # recalculează din avg_odd în enrich_candidates, fără alt cod.
            o["avg_odd"] = price
            o["reference_odd"] = price
            o["display_odd"] = price
            o["display_bookmaker"] = name
            o["odds_label"] = odds_label_with_link(price, name, link)
            o["bookmaker_name"] = name
            if link:
                o["bookmaker_link"] = link
            o["odds_source"] = CASA
            replaced += 1

    # Blocurile legacy (1X2/over_under/btts/double_chance) rămân consistente.
    legacy_map = {"1x2": "1X2", "over_under": "over_under",
                  "btts": "btts", "double_chance": "double_chance"}
    for (key, value), price in prices.items():
        block = odds_pack.get(legacy_map.get(key, ""))
        if isinstance(block, dict) and value in block:
            block[value] = round(float(price), 3)

    # Linkul paginii de meci e independent de overlay-ul de cote: pe un tabel
    # cu pariuri exotice (HT/FT, goluri exacte) tot deschidem Superbet.
    if link:
        odds_pack["superbet_link"] = link
        odds_pack["superbet_source"] = CASA
    return replaced


async def wait_result(task: "asyncio.Task", timeout: float = WAIT_BUDGET_S) -> Optional[dict]:
    """Rezultatul task-ului Superbet cu buget strict de timp; la depășire sau
    eroare => None (fallback tăcut). Nu înghite anularea task-ului părinte."""
    try:
        return await asyncio.wait_for(task, timeout=timeout)
    except asyncio.TimeoutError:
        log.debug("OddsPapi: buget de timp depasit (%.1fs) — fallback API-Football", timeout)
        return None
    except Exception as e:
        log.debug("OddsPapi: task esuat (%s) — fallback API-Football", e)
        return None


def _teams_from_match(match: str) -> Optional[tuple[str, str]]:
    parts = re.split(r"\s+vs\.?\s+", str(match or ""), maxsplit=1, flags=re.I)
    if len(parts) != 2:
        return None
    home, away = parts[0].strip(), parts[1].strip()
    return (home, away) if home and away else None


def _team_hits_line(team: str, line: str) -> bool:
    """Potrivire relaxată pe token-uri expandate: Man City ≈ Manchester City,
    Leverkusen în «Elversberg – Leverkusen», Hamburg ≈ Hamburger SV."""
    ta = _team_tokens(team)
    line_toks = _team_tokens(line)
    if not ta or not line_toks:
        return False
    line_join = " ".join(line_toks)
    line_set = set(line_toks)
    team_join = " ".join(ta)
    if team_join in line_join:
        return True

    def present(t: str) -> bool:
        if t in line_set or t in line_join:
            return True
        return any(_stem_related(t, lt, min_len=6) for lt in line_toks)

    if all(present(t) for t in ta):
        return True
    if any(len(t) >= 6 and present(t) for t in ta):
        return True
    # Un token ≥4 (Viseu, Porto, Como): ambele echipe trebuie să lovească
    # linia, deci «city» singur nu leagă un rând greșit.
    return any(len(t) >= 4 and present(t) for t in ta)


def _strip_house_paren_in_cota(line: str) -> str:
    """Scoate «(Betano)», «(Superbet)» etc. din celula Cotă."""
    if "|" not in line or not _HOUSE_PAREN.search(line):
        return line
    parts = line.split("|")
    odd_re = re.compile(r"^\s*\d+[.,]\d+")
    for i, cell in enumerate(parts):
        if i <= 1 or "⭐" in cell:
            continue
        if odd_re.match(_HOUSE_PAREN.sub("", cell)):
            parts[i] = _HOUSE_PAREN.sub("", cell)
    return "|".join(parts)


def _is_md_sep_row(line: str) -> bool:
    cells = [c.strip() for c in line.split("|")]
    cells = [c for c in cells if c]
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", c) for c in cells)


def _cota_col_index(header_line: str) -> Optional[int]:
    parts = header_line.split("|")
    for i, cell in enumerate(parts):
        if re.search(r"cot[aă]", cell, re.I):
            return i
    return None


def _insert_link_in_cota_cell(line: str, link: str,
                              cota_idx: Optional[int] = None) -> str:
    """Adaugă [→ Superbet](url) lângă cota, fără a transforma numărul în link."""
    if "[→ Superbet]" in line or link in line:
        return _strip_house_paren_in_cota(line)
    parts = line.split("|")
    odd_re = re.compile(r"^\s*\d+[.,]\d+")
    idxs = [cota_idx] if cota_idx is not None and 0 <= cota_idx < len(parts) else []
    if not idxs:
        idxs = range(len(parts))
    for i in idxs:
        cell = parts[i]
        if i <= 1 and cota_idx is None:
            continue
        if "⭐" in cell:
            continue
        if cota_idx is not None or odd_re.match(cell):
            if cota_idx is not None and not odd_re.match(_HOUSE_PAREN.sub("", cell)):
                continue
            cleaned = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", cell)
            cleaned = _HOUSE_PAREN.sub("", cleaned)
            cleaned = re.sub(r"\s*→\s*Superbet\s*$", "", cleaned, flags=re.I)
            parts[i] = cleaned.rstrip() + f" [→ Superbet]({link}) "
            return "|".join(parts)
    return line


def inject_bookmaker_links(text: str, selections: Optional[list] = None) -> str:
    """Completează în tabelul markdown linkurile Superbet pe care modelul le-a omis.

    Două potriviri:
      1. rândul conține ambele echipe (bilet clasic, câte un meci pe linie);
      2. titlul de deasupra tabelului conține meciul, rândurile nu (tabel
         exotic pe un singur meci: PIAȚĂ / SELECȚIE / COTĂ) — atunci
         butonul merge pe FIECARE celulă Cotă.
    """
    if not text:
        return text
    lines = [_strip_house_paren_in_cota(line) if "|" in line else line
             for line in text.split("\n")]
    if not selections:
        return "\n".join(lines)

    tables: list[tuple[int, int]] = []
    i = 0
    while i < len(lines):
        if "|" in lines[i]:
            j = i
            while j < len(lines) and "|" in lines[j]:
                j += 1
            if j - i >= 2:
                tables.append((i, j))
            i = j
        else:
            i += 1

    usable: list[tuple[tuple[str, str], str]] = []
    for sel in selections:
        if not isinstance(sel, dict):
            continue
        link = str(sel.get("bookmaker_link") or sel.get("link") or "").strip()
        teams = _teams_from_match(sel.get("match") or "")
        if link and teams:
            usable.append((teams, link))
    if not usable:
        return "\n".join(lines)

    for start, end in tables:
        header = lines[start]
        cota_idx = _cota_col_index(header)
        ctx_from = max(0, start - 4)
        context = "\n".join(lines[ctx_from:start])
        data_rows = [k for k in range(start + 1, end)
                     if not _is_md_sep_row(lines[k])]
        for (home, away), link in usable:
            row_hits = [k for k in data_rows
                        if _team_hits_line(home, lines[k])
                        and _team_hits_line(away, lines[k])]
            heading_hit = (_team_hits_line(home, context)
                           and _team_hits_line(away, context))
            targets = row_hits if row_hits else (data_rows if heading_hit else [])
            for k in targets:
                lines[k] = _insert_link_in_cota_cell(lines[k], link, cota_idx)
    return "\n".join(lines)
