"""
Adapter layer pentru API-Football (api-sports.io, v3).
Toate tool-urile agentului trec prin acest modul — SINGURUL care vorbeste
cu API-ul extern.

Design:
- Un singur punct de acces HTTP (_get) cu cache TTL in-memory + BUGET ZILNIC
  persistat in SQLite (api_budget). La MAX_DAILY_API_REQUESTS ridica
  BudgetExhausted; apelantii cad pe baza locala cu o nota onesta de vechime.
  Cache hit-urile NU consuma buget.
- TIMEZONE (bug fix câstigat greu — nu elimina niciodata): toate cererile de
  fixtures folosesc timezone=APP_TIMEZONE (implicit Europe/Bucharest), deci
  date/ore sunt LOCALE Romaniei, niciodata UTC.
- get_fixtures serveste din fixture store-ul local (SQLite, tinut la zi de
  sync.py) cand ziua e in fereastra sincronizata -> zero request-uri HTTP.
- Fiecare functie CONDENSEAZA raspunsul brut intr-un JSON compact pentru LLM.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from collections import OrderedDict, deque
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx

import db

log = logging.getLogger("betmind.football")

BASE_URL = "https://v3.football.api-sports.io"

# Ligile implicite pentru MVP (ID-uri API-Football). Seed pentru tracked_leagues.
DEFAULT_LEAGUES: dict[int, str] = {
    39: "Premier League (Anglia)",
    140: "La Liga (Spania)",
    78: "Bundesliga (Germania)",
    135: "Serie A (Italia)",
    61: "Ligue 1 (Franta)",
    2: "UEFA Champions League",
    3: "UEFA Europa League",
    848: "UEFA Conference League",
    283: "Liga I (Romania)",
    1: "World Cup",
    88: "Eredivisie (Olanda)",
    94: "Primeira Liga (Portugalia)",
}

# TTL-uri de cache (secunde) per tip de date.
TTL = {
    "fixtures": 15 * 60,
    "odds": 15 * 60,
    "injuries": 60 * 60,
    "team_stats": 6 * 3600,
    "standings": 6 * 3600,
    "h2h": 24 * 3600,
    "last_matches": 60 * 60,
    "leagues": 24 * 3600,
    "predictions": 6 * 3600,
}

_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = asyncio.Lock()
# Single-flight: cereri identice (endpoint+params) in zbor partajeaza UN request.
_inflight: dict[str, asyncio.Future] = {}
_requests_remaining: Optional[str] = None  # raportat de API in headere


class FootballDataError(Exception):
    pass


class BudgetExhausted(FootballDataError):
    """Bugetul zilnic de request-uri API a fost atins. Cade pe baza locala."""


NO_ODDS_USER_MSG = (
    "Casele de pariuri nu au publicat încă cote pentru acest meci "
    "(de obicei se deschid cu 2-3 zile înainte)."
)


# ---------------------------------------------------------------------------
# Diagnostic: vizibilitate pe esecurile API-Football (nu schimba comportamentul)
#
# Fara asta, orice esec de la /odds ajunge in data_gaps ca un singur sir de
# text si nu se poate distinge un 429 real de un `errors.rateLimit` primit cu
# status 200, de un timeout sau de bugetul zilnic epuizat.
# ---------------------------------------------------------------------------

# Headerele pe care API-Football le foloseste pentru cele DOUA limite diferite:
# cea zilnica (requests-limit/remaining) si cea pe minut (RateLimit-*).
_RATE_HEADERS = (
    "x-ratelimit-requests-limit",      # cota zilnica a planului
    "x-ratelimit-requests-remaining",  # cat a mai ramas azi
    "x-ratelimit-limit",               # cota PE MINUT
    "x-ratelimit-remaining",           # cat a mai ramas in minutul curent
    "retry-after",
)

_MAX_RECENT_ERRORS = 100
_recent_errors: deque[dict] = deque(maxlen=_MAX_RECENT_ERRORS)

# Ultimele valori raportate de API (actualizate si la succes): asa vedem daca
# ne apropiem de limita pe minut inainte sa inceapa esecurile.
_last_rate_headers: dict = {}

# Statistici per tura de chat: cate apeluri API s-au facut, cate au esuat si
# cu ce fel de eroare. Turn-ul curent se propaga automat in task-urile
# analistilor (asyncio copiaza contextul la crearea task-ului).
_current_turn: ContextVar[Optional[str]] = ContextVar("betmind_api_turn", default=None)
_MAX_TRACKED_TURNS = 50
_turn_stats: "OrderedDict[str, dict]" = OrderedDict()


def set_current_turn(turn_id: Optional[str]) -> None:
    """Marcheaza tura careia i se atribuie apelurile API urmatoare."""
    _current_turn.set(turn_id)


def _turn_bucket() -> Optional[dict]:
    turn_id = _current_turn.get()
    if not turn_id:
        return None
    bucket = _turn_stats.get(turn_id)
    if bucket is None:
        bucket = {"turn_id": turn_id, "api_calls": 0, "cache_hits": 0,
                  "failures": 0, "by_kind": {}, "by_status": {},
                  "duration_s": 0.0, "endpoints": {}}
        _turn_stats[turn_id] = bucket
        while len(_turn_stats) > _MAX_TRACKED_TURNS:
            _turn_stats.popitem(last=False)
    return bucket


def turn_api_stats(turn_id: str) -> dict:
    """Ce s-a intamplat cu API-ul extern in tura data (pentru modul dezvoltator)."""
    bucket = _turn_stats.get(turn_id)
    if not bucket:
        return {"api_calls": 0, "cache_hits": 0, "failures": 0,
                "by_kind": {}, "by_status": {}, "duration_s": 0.0, "endpoints": {}}
    return {
        "api_calls": bucket["api_calls"],
        "cache_hits": bucket["cache_hits"],
        "failures": bucket["failures"],
        "by_kind": dict(bucket["by_kind"]),
        "by_status": dict(bucket["by_status"]),
        "duration_s": round(bucket["duration_s"], 3),
        "endpoints": dict(bucket["endpoints"]),
    }


def _errors_text(errors: Any) -> str:
    if isinstance(errors, dict):
        return " ".join(f"{k}={v}" for k, v in errors.items())
    if isinstance(errors, list):
        return " ".join(str(e) for e in errors)
    return str(errors)


def _looks_like_rate_limit(text: str) -> bool:
    low = text.lower()
    return "ratelimit" in low.replace(" ", "") or "too many requests" in low


def _rate_headers(headers: Any) -> dict:
    """Extrage headerele de ritm. httpx e case-insensitive, deci
    X-RateLimit-Remaining si x-ratelimit-remaining sunt acelasi camp."""
    if headers is None:
        return {}
    out = {}
    for name in _RATE_HEADERS:
        try:
            value = headers.get(name)
        except Exception:
            value = None
        if value is not None:
            out[name.lower()] = value
    # Orice alt header de ritm pe care nu l-am listat (proxy, CDN).
    try:
        items = headers.items()
    except Exception:
        items = []
    for key, value in items:
        low = str(key).lower()
        if value is None or low in out:
            continue
        compact = low.replace("-", "")
        if "ratelimit" in compact or low == "retry-after":
            out[low] = value
    return out


def _note_call(endpoint: str, duration: float, cached: bool = False) -> None:
    bucket = _turn_bucket()
    if bucket is None:
        return
    if cached:
        bucket["cache_hits"] += 1
        return
    bucket["api_calls"] += 1
    bucket["duration_s"] += duration
    bucket["endpoints"][endpoint] = bucket["endpoints"].get(endpoint, 0) + 1


# Cat de zgomotos logam fiecare tip de esec. Bugetul epuizat se repeta la
# FIECARE apel al fiecarui analist (zeci de linii identice) si e deja o stare
# cunoscuta local — ramane in istoric si in /api/health, dar nu inunda logul.
# Esecurile venite chiar de la API raman la WARNING: pe ele le cautam.
_LOG_LEVELS = {
    "budget_exhausted": logging.DEBUG,
    "no_odds_data": logging.INFO,
}


def _record_api_error(endpoint: str, kind: str, status: Optional[int], body: str,
                      headers: Any = None, params: Optional[dict] = None,
                      duration: float = 0.0) -> None:
    """Un singur loc unde esecurile devin vizibile: log structurat + istoric
    scurt in memorie (citit de /api/health)."""
    entry = {
        "at": now_local().isoformat(timespec="seconds"),
        "endpoint": endpoint,
        "kind": kind,
        "status": status,
        "body": (body or "")[:200],
        "headers": _rate_headers(headers),
        "params": dict(params or {}),
        "duration_s": round(duration, 3),
        "turn_id": _current_turn.get(),
    }
    _recent_errors.append(entry)

    bucket = _turn_bucket()
    if bucket is not None:
        bucket["failures"] += 1
        bucket["by_kind"][kind] = bucket["by_kind"].get(kind, 0) + 1
        key = str(status) if status is not None else "none"
        bucket["by_status"][key] = bucket["by_status"].get(key, 0) + 1

    log.log(
        _LOG_LEVELS.get(kind, logging.WARNING),
        "API-Football FAIL endpoint=%s kind=%s status=%s params=%s durata=%.2fs "
        "headers=%s body=%r",
        endpoint, kind, status, entry["params"], duration, entry["headers"], entry["body"],
    )


def recent_api_errors(endpoint: Optional[str] = None, hours: float = 1.0) -> list[dict]:
    """Erorile din ultimele `hours` ore, optional filtrate pe endpoint."""
    cutoff = now_local() - timedelta(hours=hours)
    out = []
    for e in _recent_errors:
        if endpoint and e["endpoint"] != endpoint:
            continue
        try:
            if datetime.fromisoformat(e["at"]) < cutoff:
                continue
        except ValueError:
            pass
        out.append(e)
    return out


def api_errors_grouped(endpoint: Optional[str] = None, hours: float = 1.0) -> dict[str, int]:
    """Erorile grupate pe tip — raspunde la «ce fel de esec e, de fapt?»."""
    grouped: dict[str, int] = {}
    for e in recent_api_errors(endpoint, hours):
        grouped[e["kind"]] = grouped.get(e["kind"], 0) + 1
    return grouped


def last_api_error(endpoint: Optional[str] = None) -> Optional[dict]:
    for e in reversed(_recent_errors):
        if endpoint is None or e["endpoint"] == endpoint:
            return e
    return None


def last_rate_headers() -> dict:
    """Ultimele headere de ritm vazute (si la succes, nu doar la esec) — arata
    cat de aproape suntem de limita PE MINUT a planului."""
    return dict(_last_rate_headers)


def api_max_concurrent() -> int:
    try:
        return max(1, int(os.environ.get("API_MAX_CONCURRENT", "8")))
    except ValueError:
        return 8


def api_http_attempts() -> int:
    try:
        return max(1, int(os.environ.get("API_HTTP_ATTEMPTS", "3")))
    except ValueError:
        return 3


def api_retry_backoff_base() -> float:
    try:
        return max(0.0, float(os.environ.get("API_RETRY_BACKOFF_BASE", "1")))
    except ValueError:
        return 1.0


def rate_limit_per_minute() -> Optional[int]:
    """Limita pe minut aplicata (env). Implicit 240, sub plafonul real ~300."""
    raw = (os.environ.get("API_RATE_LIMIT_PER_MINUTE")
           or os.environ.get("API_FOOTBALL_RATE_LIMIT_PER_MINUTE")
           or "240").strip()
    try:
        n = int(raw)
    except ValueError:
        return 240
    return n if n > 0 else 240


def rate_limiter_active() -> bool:
    """Limitatorul de ritm e aplicat pe toate apelurile HTTP (inclusiv sync)."""
    return True


def _is_odds_endpoint(endpoint: str) -> bool:
    return (endpoint or "").rstrip("/").endswith("/odds")


class HttpGate:
    """Semaphore de concurenta + token bucket pe minut, cu prioritate /odds."""

    def __init__(self) -> None:
        self.concurrent = api_max_concurrent()
        self.rate = max(1, rate_limit_per_minute() or 240)
        self._sem = asyncio.Semaphore(self.concurrent)
        self._lock = asyncio.Lock()
        burst = float(min(self.concurrent, self.rate))
        self._tokens = burst
        self._burst = burst
        self._last = time.monotonic()
        self._rate_per_sec = self.rate / 60.0
        self._odds_waiters = 0

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate_per_sec)

    async def acquire(self, endpoint: str) -> None:
        is_odds = _is_odds_endpoint(endpoint)
        if is_odds:
            async with self._lock:
                self._odds_waiters += 1
        try:
            while True:
                async with self._lock:
                    self._refill()
                    blocked = (not is_odds) and self._odds_waiters > 0
                    if not blocked and self._tokens >= 1.0:
                        self._tokens -= 1.0
                        break
                    if self._tokens < 1.0 and self._rate_per_sec > 0:
                        wait = (1.0 - self._tokens) / self._rate_per_sec
                    else:
                        wait = 0.02
                await asyncio.sleep(max(0.005, min(wait, 0.25)))
            await self._sem.acquire()
        except BaseException:
            if is_odds:
                async with self._lock:
                    self._odds_waiters -= 1
            raise
        else:
            if is_odds:
                async with self._lock:
                    self._odds_waiters -= 1

    def release(self) -> None:
        self._sem.release()


_http_gate: Optional[HttpGate] = None


def reset_http_gate() -> None:
    """Reciteste env (teste)."""
    global _http_gate
    _http_gate = None


def _gate() -> HttpGate:
    global _http_gate
    if _http_gate is None:
        _http_gate = HttpGate()
    return _http_gate


# ---------------------------------------------------------------------------
# Timezone (APP_TIMEZONE) — semantica locala Romania, niciodata UTC
# ---------------------------------------------------------------------------

def app_timezone_name() -> str:
    return os.environ.get("APP_TIMEZONE", "").strip() or "Europe/Bucharest"


def app_timezone() -> ZoneInfo:
    return ZoneInfo(app_timezone_name())


def now_local() -> datetime:
    return datetime.now(app_timezone())


def today_local() -> date:
    return now_local().date()


def max_daily_requests() -> int:
    """
    SINGURA sursa de adevar pentru limita zilnica de request-uri.
    Budget guard-ul din _get() si /api/health citesc AMBELE de aici —
    nu citi MAX_DAILY_API_REQUESTS din env in alta parte.
    """
    try:
        return int(os.environ.get("MAX_DAILY_API_REQUESTS", "100"))
    except ValueError:
        return 100


def sync_window() -> tuple[date, date]:
    """Fereastra acoperita de fixture store: [azi - past, azi + future]."""
    past = int(os.environ.get("SYNC_WINDOW_PAST_DAYS", "7"))
    future = int(os.environ.get("SYNC_WINDOW_FUTURE_DAYS", "14"))
    today = today_local()
    return today - timedelta(days=past), today + timedelta(days=future)


# ---------------------------------------------------------------------------
# HTTP + cache + budget guard
# ---------------------------------------------------------------------------

def _api_key() -> str:
    key = os.environ.get("API_FOOTBALL_KEY", "").strip()
    if not key:
        raise FootballDataError(
            "API_FOOTBALL_KEY lipseste din .env. "
            "Creeaza un cont gratuit pe https://dashboard.api-football.com "
            "si adauga cheia in fisierul .env."
        )
    return key


async def _http_get(endpoint: str, params: dict, headers: dict) -> httpx.Response:
    """Cererea HTTP bruta — separata ca sa poata fi mock-uita in teste."""
    async with httpx.AsyncClient(timeout=25) as client:
        return await client.get(f"{BASE_URL}{endpoint}", params=params, headers=headers)


def _cache_id(endpoint: str, params: dict) -> str:
    return endpoint + "|" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))


async def _get(endpoint: str, params: dict, ttl_key: str) -> Any:
    """
    GET cu cache TTL, single-flight si buget zilnic. Cache hit = zero cost.
    Daca un apel identic e deja in zbor, asteptam rezultatul lui (zero HTTP).
    Ridica BudgetExhausted la limita, FootballDataError la alte probleme.
    """
    cache_id = _cache_id(endpoint, params)

    async with _cache_lock:
        hit = _cache.get(cache_id)
        if hit and hit[0] > time.time():
            _note_call(endpoint, 0.0, cached=True)
            return hit[1]
        existing = _inflight.get(cache_id)
        if existing is not None:
            leader = False
            fut = existing
        else:
            leader = True
            fut = asyncio.get_running_loop().create_future()
            # Marcheaza exceptia ca „citita" ca asyncio sa nu logheze
            # „Future exception was never retrieved" cand liderul o ridica
            # si nimeni nu astepta (sau asteptatorii au primit-o deja).
            fut.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
            _inflight[cache_id] = fut

    if not leader:
        _note_call(endpoint, 0.0, cached=True)
        return await asyncio.shield(fut)

    try:
        payload = await _get_http(endpoint, params, ttl_key, cache_id)
        if not fut.done():
            fut.set_result(payload)
        return payload
    except Exception as e:
        if not fut.done():
            fut.set_exception(e)
        raise
    finally:
        async with _cache_lock:
            if _inflight.get(cache_id) is fut:
                del _inflight[cache_id]


def _retry_delay(attempt: int, retry_after: Optional[str]) -> float:
    delay = api_retry_backoff_base() * (2 ** attempt)
    if delay <= 0:
        return 0.0  # teste: retry instant, ignora Retry-After
    if retry_after:
        try:
            delay = max(delay, min(float(retry_after), 30.0))
        except (TypeError, ValueError):
            pass
    return delay


async def _get_http(endpoint: str, params: dict, ttl_key: str, cache_id: str) -> Any:
    """Cererea HTTP reala (un singur zbor per cache_id), cu ritm + retry pe 429."""
    global _requests_remaining

    limit = max_daily_requests()
    budget_day = today_local().isoformat()
    used = await db.budget_get(budget_day)
    if used >= limit:
        _record_api_error(endpoint, "budget_exhausted", None,
                          f"buget zilnic local: {used}/{limit} folosite azi", None, params)
        raise BudgetExhausted(
            f"Bugetul zilnic de {limit} request-uri API-Football a fost epuizat "
            f"({used} folosite azi). Raspund din baza locala."
        )

    headers = {"x-apisports-key": _api_key()}
    attempts = api_http_attempts()
    last_error: Optional[FootballDataError] = None

    for attempt in range(attempts):
        used = await db.budget_get(budget_day)
        if used >= limit:
            _record_api_error(endpoint, "budget_exhausted", None,
                              f"buget zilnic local: {used}/{limit} folosite azi", None, params)
            raise BudgetExhausted(
                f"Bugetul zilnic de {limit} request-uri API-Football a fost epuizat "
                f"({used} folosite azi). Raspund din baza locala."
            )

        await _gate().acquire(endpoint)
        started = time.monotonic()
        try:
            try:
                resp = await _http_get(endpoint, params, headers)
            except httpx.HTTPError as e:
                kind = "timeout" if isinstance(e, httpx.TimeoutException) else "network_error"
                _record_api_error(endpoint, kind, None, f"{type(e).__name__}: {e}", None, params,
                                  time.monotonic() - started)
                raise FootballDataError(
                    f"[{kind}] Nu am putut contacta API-Football: {e}") from e

            elapsed = time.monotonic() - started
            _note_call(endpoint, elapsed)
            seen = _rate_headers(resp.headers)
            if seen:
                _last_rate_headers.clear()
                _last_rate_headers.update(seen)
                _last_rate_headers["at"] = now_local().isoformat(timespec="seconds")
            await db.budget_add(budget_day, 1)

            outcome, payload_or_err, retryable = _interpret_response(
                resp, endpoint, params, elapsed)
            if outcome == "ok":
                _requests_remaining = resp.headers.get(
                    "x-ratelimit-requests-remaining", _requests_remaining)
                limit_header = resp.headers.get("x-ratelimit-requests-limit")
                if _requests_remaining is not None and limit_header:
                    try:
                        used_now = await db.budget_get(budget_day)
                        used_by_api = int(limit_header) - int(_requests_remaining)
                        if used_by_api > used_now:
                            await db.budget_floor(budget_day, used_by_api)
                    except ValueError:
                        pass
                async with _cache_lock:
                    _cache[cache_id] = (time.time() + TTL[ttl_key], payload_or_err)
                return payload_or_err

            last_error = payload_or_err  # FootballDataError
            if retryable and attempt < attempts - 1:
                delay = _retry_delay(attempt, (resp.headers or {}).get("retry-after"))
                log.warning(
                    "API-Football rate limit, retry %s/%s in %.1fs endpoint=%s",
                    attempt + 1, attempts, delay, endpoint,
                )
            else:
                rec = getattr(last_error, "_retry_record", None)
                if rec is not None:
                    _record_api_error(*rec)
                raise last_error
        finally:
            _gate().release()

        if retryable and attempt < attempts - 1:
            await asyncio.sleep(delay)

    raise last_error or FootballDataError("API-Football: esuat dupa retry-uri")


def _interpret_response(resp: httpx.Response, endpoint: str, params: dict,
                        elapsed: float) -> tuple[str, Any, bool]:
    """('ok', payload, False) | ('err', FootballDataError, retryable)."""
    if resp.status_code != 200:
        kind = {429: "http_429_rate_limit", 403: "http_403_forbidden",
                499: "http_499"}.get(resp.status_code, f"http_{resp.status_code}")
        retryable = resp.status_code == 429
        if not retryable:
            _record_api_error(endpoint, kind, resp.status_code, resp.text,
                              resp.headers, params, elapsed)
        else:
            # Inregistram doar daca e ultima incercare — apelantul decide.
            pass
        err = FootballDataError(
            f"[{kind}] API-Football a raspuns cu status {resp.status_code}: {resp.text[:200]}")
        if retryable:
            # Pasam kind pe exceptie prin mesaj; inregistram la abandon.
            err._retry_kind = kind  # type: ignore[attr-defined]
            err._retry_record = (endpoint, kind, resp.status_code, resp.text,
                                 resp.headers, params, elapsed)  # type: ignore[attr-defined]
        return ("err", err, retryable)

    try:
        data = resp.json()
    except Exception as e:
        _record_api_error(endpoint, "invalid_json", resp.status_code, resp.text,
                          resp.headers, params, elapsed)
        raise

    errors = data.get("errors")
    if errors:
        text = _errors_text(errors)
        extra = data.get("message")
        if extra:
            text = f"{text} | message={extra}"
        retryable = _looks_like_rate_limit(text)
        kind = "rate_limit_in_body" if retryable else "api_errors_in_body"
        err = FootballDataError(f"[{kind}] API-Football a returnat erori: {errors}")
        if retryable:
            err._retry_kind = kind  # type: ignore[attr-defined]
            err._retry_record = (endpoint, kind, resp.status_code, text,
                                 resp.headers, params, elapsed)  # type: ignore[attr-defined]
        else:
            _record_api_error(endpoint, kind, resp.status_code, text,
                              resp.headers, params, elapsed)
        return ("err", err, retryable)

    return ("ok", data.get("response", []), False)


def requests_remaining() -> Optional[str]:
    return _requests_remaining


async def requests_used_today() -> int:
    return await db.budget_get(today_local().isoformat())


# ---------------------------------------------------------------------------
# Statusuri -> grupe (upcoming / live / finished / other)
# ---------------------------------------------------------------------------

_STATUS_UPCOMING = {"NS", "TBD"}
_STATUS_LIVE = {"1H", "HT", "2H", "ET", "BT", "P", "INT", "LIVE"}
_STATUS_FINISHED = {"FT", "AET", "PEN"}


def status_group(status: str) -> str:
    if status in _STATUS_UPCOMING:
        return "upcoming"
    if status in _STATUS_LIVE:
        return "live"
    if status in _STATUS_FINISHED:
        return "finished"
    return "other"  # PST, CANC, ABD, AWD, WO, SUSP...


def _parse_fixture(f: dict) -> dict:
    """Raspuns brut API -> dict in forma tabelei `fixtures` (ore locale)."""
    fx = f.get("fixture", {}) or {}
    league = f.get("league", {}) or {}
    teams = f.get("teams", {}) or {}
    goals = f.get("goals", {}) or {}
    kickoff = fx.get("date") or ""  # deja in APP_TIMEZONE (param timezone)
    status = (fx.get("status") or {}).get("short", "")
    country = league.get("country")
    league_name = league.get("name") or ""
    if country:
        league_name = f"{league_name} ({country})"
    return {
        "fixture_id": fx.get("id"),
        "league_id": league.get("id"),
        "league_name": league_name,
        "season": league.get("season"),
        "date_local": kickoff[:10],
        "time_local": kickoff[11:16],
        "kickoff_iso": kickoff,
        "status": status,
        "status_group": status_group(status),
        "home_id": (teams.get("home") or {}).get("id"),
        "home_name": (teams.get("home") or {}).get("name"),
        "away_id": (teams.get("away") or {}).get("id"),
        "away_name": (teams.get("away") or {}).get("name"),
        "goals_home": goals.get("home"),
        "goals_away": goals.get("away"),
    }


_WEEKDAYS_RO = ["luni", "marți", "miercuri", "joi", "vineri", "sâmbătă", "duminică"]


def weekday_ro(date_local: str) -> Optional[str]:
    """Ziua saptamanii in romana pentru o data locala (pt. afisare 'sâmbătă 19:30')."""
    try:
        return _WEEKDAYS_RO[date.fromisoformat(date_local).weekday()]
    except (ValueError, IndexError):
        return None


def _fixture_out(p: dict) -> dict:
    """Forma compacta trimisa LLM-ului (chei identice pt. randuri DB si live)."""
    gh, ga = p.get("goals_home"), p.get("goals_away")
    return {
        "fixture_id": p["fixture_id"],
        "date": p["date_local"],
        "weekday": weekday_ro(p["date_local"]),
        "time": p["time_local"],
        "kickoff": p["kickoff_iso"],
        "status": p["status"],
        "status_group": p["status_group"],
        "league": p["league_name"],
        "league_id": p["league_id"],
        "season": p["season"],
        "home": {"id": p["home_id"], "name": p["home_name"]},
        "away": {"id": p["away_id"], "name": p["away_name"]},
        "score": f"{gh}-{ga}" if gh is not None and ga is not None else None,
    }


async def fetch_day(day: str) -> list[dict]:
    """
    O zi completa de fixtures (toate ligile), in APP_TIMEZONE — UN request.
    Folosita de sync si de fallback-ul live din get_fixtures.
    """
    raw = await _get("/fixtures", {"date": day, "timezone": app_timezone_name()}, "fixtures")
    return [_parse_fixture(f) for f in raw]


# ---------------------------------------------------------------------------
# Tools expuse agentului
# ---------------------------------------------------------------------------

async def list_leagues(search: str) -> list[dict]:
    """Cauta o liga dupa nume/tara. Folosit cand liga nu e urmarita deja."""
    raw = await _get("/leagues", {"search": search}, "leagues")
    return _league_summaries(raw)


def _league_summaries(raw: list[dict]) -> list[dict]:
    out = []
    for item in raw[:10]:
        lg, country = item.get("league", {}), item.get("country", {})
        seasons = item.get("seasons", [])
        current = next((s["year"] for s in seasons if s.get("current")), None)
        out.append({
            "league_id": lg.get("id"),
            "name": lg.get("name"),
            "type": lg.get("type"),
            "country": country.get("name"),
            "current_season": current,
        })
    return out


def _staleness_minutes(synced_at: Optional[str]) -> Optional[int]:
    if not synced_at:
        return None
    try:
        then = datetime.fromisoformat(synced_at)
        if then.tzinfo is None:
            then = then.replace(tzinfo=app_timezone())
        return int((now_local() - then).total_seconds() // 60)
    except ValueError:
        return None


async def get_fixtures(date_from: str, date_to: Optional[str] = None,
                       league_ids: Optional[list[int]] = None) -> dict:
    """
    Meciurile din intervalul [date_from, date_to] (max 7 zile), filtrate pe ligi.

    Zilele din fereastra sincronizata se servesc din fixture store-ul local
    (source: local_db, ZERO request-uri HTTP). In afara ferestrei sau pentru
    zile inca nesincronizate -> API live prin budget guard, apoi upsert in DB.
    La buget epuizat -> ce avem in DB + nota onesta de vechime.
    """
    start = datetime.strptime(date_from, "%Y-%m-%d").date()
    end = datetime.strptime(date_to, "%Y-%m-%d").date() if date_to else start
    if end < start:
        start, end = end, start
    if (end - start).days > 6:
        end = start + timedelta(days=6)

    tracked = await db.get_tracked_leagues()
    if not tracked:
        tracked = dict(DEFAULT_LEAGUES)
    wanted = set(league_ids) if league_ids else set(tracked.keys())
    # DB-ul contine doar ligile urmarite; alte ligi cerute explicit -> live.
    db_can_serve = wanted <= set(tracked.keys())

    win_start, win_end = sync_window()
    days = [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]
    sync_info = await db.day_sync_info(days)

    fixtures: list[dict] = []
    day_meta: dict[str, dict] = {}
    sources: set[str] = set()
    budget_exhausted = False

    for day_str in days:
        d = date.fromisoformat(day_str)
        synced_at = sync_info.get(day_str)
        in_window = win_start <= d <= win_end

        if in_window and db_can_serve and synced_at:
            rows = await db.get_fixtures_for_days([day_str], sorted(wanted))
            fixtures.extend(rows)
            age_min = _staleness_minutes(synced_at)
            day_meta[day_str] = {
                "source": "local_db",
                "last_synced_at": synced_at,
                "stale": bool(age_min is not None and age_min > 60),
            }
            sources.add("local_db")
            continue

        try:
            parsed = await fetch_day(day_str)
            synced_now = now_local().isoformat(timespec="seconds")
            for p in parsed:
                if p["league_id"] in tracked:
                    await db.upsert_fixture(p, synced_now)
            await db.mark_day_synced(day_str, synced_now)
            fixtures.extend(p for p in parsed if p["league_id"] in wanted)
            day_meta[day_str] = {"source": "live_api", "last_synced_at": synced_now, "stale": False}
            sources.add("live_api")
        except BudgetExhausted as e:
            budget_exhausted = True
            rows = await db.get_fixtures_for_days([day_str], sorted(wanted))
            fixtures.extend(rows)
            day_meta[day_str] = {
                "source": "local_db",
                "last_synced_at": synced_at,
                "stale": True,
                "note": str(e),
            }
            sources.add("local_db")

    fixtures.sort(key=lambda x: x.get("kickoff_iso") or "")
    out = [_fixture_out(p) for p in fixtures]

    matches_per_day = {d: 0 for d in days}
    for f in out:
        if f["date"] in matches_per_day:
            matches_per_day[f["date"]] += 1

    if sources == {"local_db"}:
        source = "local_db"
    elif sources == {"live_api"}:
        source = "live_api"
    else:
        source = "mixed"

    result = {
        "count": len(out),
        "source": source,
        "timezone": app_timezone_name(),
        "matches_per_day": matches_per_day,
        "days": day_meta,
        "fixtures": out[:120],
        "note": ("Toate datele si orele sunt LOCALE Romania (APP_TIMEZONE) — nu le converti. "
                 "status_group: upcoming=recomandabil, live/finished/other=nu recomanda."),
        "api_requests_remaining_today": requests_remaining(),
    }
    if budget_exhausted:
        result["budget_exhausted"] = True
        result["note"] += (" ATENTIE: bugetul API de azi e epuizat; datele vin din baza locala "
                           "si pot fi vechi — spune-i userului onest varsta datelor.")
    return result


async def get_fixture_changes(date_from: Optional[str] = None,
                              date_to: Optional[str] = None) -> dict:
    """Amanari/reprogramari detectate de sincronizare (din fixture_changes)."""
    today = today_local()
    if not date_from:
        date_from = (today - timedelta(days=3)).isoformat()
    if not date_to:
        date_to = today.isoformat()

    rows = await db.get_changes(date_from, date_to)
    changes = []
    for r in rows:
        match = f"{r['home_name']}–{r['away_name']}"
        old, new = r["old_value"], r["new_value"]
        if r["field"] == "status" and new == "PST":
            meaning = "postponed (amanat)"
        elif r["field"] in ("kickoff_iso", "time_local", "date_local"):
            meaning = "rescheduled (reprogramat)"
        else:
            meaning = "status change"
        changed_at = r["changed_at"] or ""
        changes.append({
            "fixture_id": r["fixture_id"],
            "match": match,
            "league": r["league_name"],
            "match_date": r["date_local"],
            "field": r["field"],
            "old_value": old,
            "new_value": new,
            "meaning": meaning,
            "changed_at": changed_at,
            "summary": f"{match}: {old}→{new} ({meaning}) detectat {changed_at[:10]} la {changed_at[11:16]}",
        })
    return {
        "count": len(changes),
        "date_from": date_from,
        "date_to": date_to,
        "changes": changes,
        "note": "Schimbari detectate de sincronizarea locala; orele sunt locale Romania.",
    }


async def track_league(search_or_id: str) -> dict:
    """
    Adauga o competitie in tracked_leagues (rezolvata prin /leagues).
    Cu nume ambiguu returneaza candidatii, ca agentul sa aleaga league_id exact.
    """
    s = str(search_or_id).strip()
    if not s:
        return {"error": "search_or_id gol."}

    params: dict[str, Any] = {"id": int(s)} if s.isdigit() else {"search": s}
    raw = await _get("/leagues", params, "leagues")
    candidates = _league_summaries(raw)
    if not candidates:
        return {"error": f"Nu am gasit nicio competitie pentru '{s}' in API-Football."}

    if len(candidates) > 1 and not s.isdigit():
        return {
            "multiple_matches": candidates[:8],
            "note": ("Mai multe competitii se potrivesc. Alege-o pe cea corecta si "
                     "cheama din nou track_league cu league_id-ul exact."),
        }

    lg = candidates[0]
    name = f"{lg['name']} ({lg['country']})" if lg.get("country") else (lg.get("name") or "")
    await db.add_tracked_league(int(lg["league_id"]), name, added_by="user")
    return {
        "tracked": True,
        "league_id": lg["league_id"],
        "name": name,
        "current_season": lg.get("current_season"),
        "note": "Liga e acum urmarita: intra in sincronizarea locala si in get_fixtures implicit.",
    }


async def get_team_last_matches(team_id: int, count: int = 6) -> list[dict]:
    """Ultimele N meciuri ale echipei, cu rezultat din perspectiva ei."""
    count = max(1, min(count, 10))
    raw = await _get("/fixtures",
                     {"team": team_id, "last": count, "timezone": app_timezone_name()},
                     "last_matches")
    out = []
    for f in raw:
        teams, goals = f.get("teams", {}), f.get("goals", {})
        home = teams.get("home", {})
        is_home = home.get("id") == team_id
        gf = goals.get("home") if is_home else goals.get("away")
        ga = goals.get("away") if is_home else goals.get("home")
        if gf is None or ga is None:
            result = "?"
        else:
            result = "W" if gf > ga else ("L" if gf < ga else "D")
        opponent = teams.get("away", {}) if is_home else teams.get("home", {})
        out.append({
            "date": (f.get("fixture", {}).get("date") or "")[:10],
            "competition": f.get("league", {}).get("name"),
            "venue": "home" if is_home else "away",
            "opponent": opponent.get("name"),
            "opponent_id": opponent.get("id"),
            "score": f"{goals.get('home')}-{goals.get('away')}",
            "result": result,
            "goals_for": gf,
            "goals_against": ga,
            "both_scored": bool(goals.get("home") and goals.get("away")) if None not in (goals.get("home"), goals.get("away")) else None,
        })
    return out


async def get_team_statistics(team_id: int, league_id: int, season: int) -> dict:
    """Statistici agregate pe sezon: forma, goluri acasa/deplasare, clean sheets."""
    raw = await _get("/teams/statistics",
                     {"team": team_id, "league": league_id, "season": season},
                     "team_stats")
    if not raw:
        return {"error": "Fara statistici pentru combinatia echipa/liga/sezon."}
    g = raw.get("goals", {})
    fixtures = raw.get("fixtures", {})

    def _avg(side: str, direction: str) -> Any:
        return ((g.get(direction) or {}).get("average") or {}).get(side)

    return {
        "team": (raw.get("team") or {}).get("name"),
        "form_last_matches": raw.get("form"),  # ex: "WWDLW"
        "played": (fixtures.get("played") or {}),
        "wins": (fixtures.get("wins") or {}),
        "draws": (fixtures.get("draws") or {}),
        "loses": (fixtures.get("loses") or {}),
        "avg_goals_scored": {"home": _avg("home", "for"), "away": _avg("away", "for"), "total": _avg("total", "for")},
        "avg_goals_conceded": {"home": _avg("home", "against"), "away": _avg("away", "against"), "total": _avg("total", "against")},
        "clean_sheets": raw.get("clean_sheet"),
        "failed_to_score": raw.get("failed_to_score"),
    }


async def get_h2h(team1_id: int, team2_id: int, last: int = 6) -> list[dict]:
    """Istoricul direct dintre doua echipe."""
    raw = await _get("/fixtures/headtohead",
                     {"h2h": f"{team1_id}-{team2_id}", "last": max(1, min(last, 10)),
                      "timezone": app_timezone_name()},
                     "h2h")
    out = []
    for f in raw:
        teams, goals = f.get("teams", {}), f.get("goals", {})
        out.append({
            "date": (f.get("fixture", {}).get("date") or "")[:10],
            "competition": f.get("league", {}).get("name"),
            "home": teams.get("home", {}).get("name"),
            "away": teams.get("away", {}).get("name"),
            "score": f"{goals.get('home')}-{goals.get('away')}",
        })
    return out


def _injuries_pack_from_raw(raw: list, team_id: Optional[int] = None) -> dict:
    """Aceeasi forma pe care o vede analistul, indiferent daca raw-ul a venit
    de la /injuries?team= sau de la /injuries?league= (filtrat pe echipa)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    players: dict[str, dict] = {}
    for item in raw:
        if team_id is not None:
            item_team = ((item.get("team") or {}).get("id"))
            if item_team != team_id:
                continue
        fx_date = (item.get("fixture") or {}).get("date")
        if fx_date:
            try:
                when = datetime.fromisoformat(fx_date.replace("Z", "+00:00"))
                if when < cutoff:
                    continue
            except ValueError:
                pass
        p = item.get("player") or {}
        name = p.get("name")
        if not name:
            continue
        players[name] = {
            "player": name,
            "type": p.get("type"),      # ex: "Missing Fixture"
            "reason": p.get("reason"),  # ex: "Knee Injury", "Suspended"
        }
    return {
        "count": len(players),
        "injuries": list(players.values())[:25],
        "note": "Date din ultimele ~30 zile; verifica importanta jucatorilor cu get_team_last_matches/stiri.",
    }


async def get_injuries(team_id: int, season: int) -> dict:
    """Accidentari/suspendari recente (ultimele ~30 zile) pentru o echipa."""
    raw = await _get("/injuries", {"team": team_id, "season": season}, "injuries")
    return _injuries_pack_from_raw(raw, team_id)


async def get_league_injuries_by_team(league_id: int, season: int) -> dict[int, dict]:
    """UN apel /injuries?league=&season= , grupat pe team_id. Aceeasi forma
    per echipa ca get_injuries — ca assemble_data_pack sa nu schimbe pachetul."""
    raw = await _get("/injuries", {"league": league_id, "season": season}, "injuries")
    teams: set[int] = set()
    for item in raw:
        tid = (item.get("team") or {}).get("id")
        if tid is not None:
            teams.add(tid)
    return {tid: _injuries_pack_from_raw(raw, tid) for tid in teams}


async def get_standings(league_id: int, season: int) -> list[dict]:
    """Clasamentul unei ligi."""
    raw = await _get("/standings", {"league": league_id, "season": season}, "standings")
    if not raw:
        return []
    table = ((raw[0].get("league") or {}).get("standings") or [[]])[0]
    out = []
    for row in table:
        out.append({
            "rank": row.get("rank"),
            "team": (row.get("team") or {}).get("name"),
            "team_id": (row.get("team") or {}).get("id"),
            "points": row.get("points"),
            "played": (row.get("all") or {}).get("played"),
            "goal_diff": row.get("goalsDiff"),
            "form": row.get("form"),
        })
    return out


async def get_predictions(fixture_id: int) -> dict:
    """
    Predictiile API-Football pentru un meci (procente 1/X/2, sfat, comparatie
    forma/atac/aparare). UN semnal intre altele — niciodata raspunsul final.
    """
    raw = await _get("/predictions", {"fixture": fixture_id}, "predictions")
    if not raw:
        return {"error": "Fara predictii pentru acest meci."}
    item = raw[0]
    p = item.get("predictions") or {}
    comparison = item.get("comparison") or {}
    return {
        "percent": p.get("percent"),          # ex: {"home":"45%","draw":"30%","away":"25%"}
        "advice": p.get("advice"),
        "winner": (p.get("winner") or {}).get("name"),
        "win_or_draw": p.get("win_or_draw"),
        "under_over": p.get("under_over"),
        "comparison": {
            "form": comparison.get("form"),
            "att": comparison.get("att"),
            "def": comparison.get("def"),
            "total": comparison.get("total"),
        },
        "note": "Semnal orientativ de la API-Football; nu-l trata drept adevar final.",
    }


_PREFERRED_BOOKMAKERS = [8, 6, 11, 1]  # Bet365, apoi fallback-uri (referinta pentru media/ranking)
# Case observate live pe /odds (aug 2026) care operează și în România,
# în ordinea cerută de produs. Doar acestea — restul listei RO nu apare în API.
_RO_PREFERRED_BOOKS: tuple[tuple[int, str], ...] = (
    (34, "superbet"),
    (32, "betano"),
    (16, "unibet"),
)
# Chei legacy (V1): pastrate neschimbate pentru clientii existenti.
_LEGACY_MARKETS = {"Match Winner": "1X2", "Goals Over/Under": "over_under",
                   "Both Teams Score": "btts", "Double Chance": "double_chance"}
_LEGACY_OU_LINES = ("Over 1.5", "Under 1.5", "Over 2.5", "Under 2.5",
                    "Over 3.5", "Under 3.5")
_MAX_MARKETS_PER_FIXTURE = 25
_MAX_OUTCOMES = 12
_OU_KEEP = {
    "Over 0.5", "Under 0.5", "Over 1.5", "Under 1.5", "Over 2.5", "Under 2.5",
    "Over 3.5", "Under 3.5", "Over 4.5", "Under 4.5",
}
_DENY_MARKET_SUBSTR = (
    "correct score", "exact score",
    "goalscorer", "goal scorer",
    "method of victory", "winning method",
)
_EXACT_MARKET_KEYS = {
    "match winner": "1x2",
    "full time result": "1x2",
    "1x2": "1x2",
    "goals over/under": "over_under",
    "both teams score": "btts",
    "both teams to score": "btts",
    "double chance": "double_chance",
    "asian handicap": "asian_handicap",
    "home team total goals": "team_total_home",
    "away team total goals": "team_total_away",
    "total - home": "team_total_home",
    "total - away": "team_total_away",
    "home team score a goal": "team_scores_home",
    "away team score a goal": "team_scores_away",
    "ht/ft double": "htft",
    "ht/ft": "htft",
    "first half winner": "1x2_ht",
    "half time result": "1x2_ht",
    "goals over/under first half": "over_under_ht",
    "goals over/under - first half": "over_under_ht",
    "both teams score - first half": "btts_ht",
    "both teams score first half": "btts_ht",
    "both teams to score - first half": "btts_ht",
    "first team to score": "first_to_score",
    "result/total goals": "combo_result_goals",
    "match winner/total goals": "combo_result_goals",
    "result total goals": "combo_result_goals",
}
# Prioritate la taierea la 25: rezultate si goluri inainte de pauza.
_MARKET_PRIORITY = {
    "1x2": 0,
    "double_chance": 1,
    "over_under": 2,
    "btts": 3,
    "asian_handicap": 4,
    "team_total_home": 5,
    "team_total_away": 5,
    "team_scores_home": 6,
    "team_scores_away": 6,
    "team_scores": 6,
    "combo_result_goals": 7,
    "1x2_ht": 8,
    "over_under_ht": 9,
    "btts_ht": 10,
    "first_to_score": 11,
    "htft": 12,
}


def slugify_market(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", _as_text(name).lower()).strip("_")
    return s or "unknown"


def _as_text(value: Any) -> str:
    """API-Football trimite uneori valoarea outcome-ului ca int/float (1, -0.5)."""
    if value is None:
        return ""
    return str(value).strip()


def normalize_market_name(name: str) -> Optional[str]:
    """Mapeaza numele furnizorului pe o cheie interna stabila.

    Returneaza None pentru pietele din deny-list (corect score, marcatori…).
    Pietele necunoscute, dar permise, raman ca slug — nu se arunca.
    """
    raw = _as_text(name)
    if not raw:
        return None
    n = raw.lower()
    if any(d in n for d in _DENY_MARKET_SUBSTR):
        return None
    if n in _EXACT_MARKET_KEYS:
        return _EXACT_MARKET_KEYS[n]
    if "ht/ft" in n:
        return "htft"
    if "first team to score" in n or n == "first to score":
        return "first_to_score"
    if ("both teams" in n and "score" in n) and ("first half" in n or "1st half" in n):
        return "btts_ht"
    if ("over/under" in n or "over under" in n) and ("first half" in n or "1st half" in n):
        return "over_under_ht"
    if "first half winner" in n or n == "half time result":
        return "1x2_ht"
    if ("result" in n and "goal" in n) or ("winner" in n and "total" in n and "goal" in n):
        return "combo_result_goals"
    if "asian handicap" in n:
        return "asian_handicap"
    if "double chance" in n:
        return "double_chance"
    if "team to score" in n or "score a goal" in n:
        if "home" in n:
            return "team_scores_home"
        if "away" in n:
            return "team_scores_away"
        return "team_scores"
    if "total" in n and "goal" in n:
        if "home" in n and "away" not in n.split("home")[0]:
            return "team_total_home"
        if "away" in n:
            return "team_total_away"
    if n in ("match winner", "full time result") or n == "1x2":
        return "1x2"
    if "both teams" in n and "score" in n:
        return "btts"
    if "goals over/under" in n or n == "over/under":
        return "over_under"
    return slugify_market(raw)


def _parse_odd(value: Any) -> Optional[float]:
    try:
        odd = float(value)
    except (TypeError, ValueError):
        return None
    return odd if odd > 1.01 else None


def _normalize_outcome(market_key: str, value: Any) -> str:
    v = _as_text(value)
    low = v.lower()
    if market_key in ("1x2", "1x2_ht"):
        if low in ("home", "1", "home win"):
            return "Home"
        if low in ("draw", "x"):
            return "Draw"
        if low in ("away", "2", "away win"):
            return "Away"
    if market_key in ("btts", "btts_ht"):
        if low in ("yes", "gg"):
            return "Yes"
        if low in ("no", "ng"):
            return "No"
    if market_key == "double_chance":
        if low in ("home/draw", "1x", "1/x"):
            return "Home/Draw"
        if low in ("draw/away", "x2", "x/2"):
            return "Draw/Away"
        if low in ("home/away", "12", "1/2"):
            return "Home/Away"
    if market_key == "first_to_score":
        if low in ("home", "1"):
            return "Home"
        if low in ("away", "2"):
            return "Away"
        if "no" in low or low == "none":
            return "None"
    return v


def _market_priority(key: str) -> int:
    if key in _MARKET_PRIORITY:
        return _MARKET_PRIORITY[key]
    if key.startswith("team_total_"):
        return 5
    if key.startswith("team_scores_"):
        return 6
    if key.startswith("combo"):
        return 7
    if key.endswith("_ht") or key.startswith("ht_"):
        return 13
    return 20


def _prune_outcomes(key: str, outcomes: list[dict]) -> list[dict]:
    """Tine liniile utile; pietele cu >12 outcome-uri raman in deny-list."""
    if key in ("over_under", "over_under_ht", "team_total_home", "team_total_away"):
        kept = [o for o in outcomes if o["value"] in _OU_KEEP]
        outcomes = kept or outcomes
    if len(outcomes) > _MAX_OUTCOMES and key == "asian_handicap":
        outcomes = sorted(outcomes, key=lambda o: abs(o["avg_odd"] - 1.90))[:_MAX_OUTCOMES]
    return outcomes


def format_odds_label(odd: Optional[float], bookmaker: Optional[str]) -> Optional[str]:
    """Doar numărul („1.85”). Nicio casă în paranteză — Superbet e butonul
    «→ Superbet», celelalte case nu se afișează pe bilet."""
    if odd is None:
        return None
    try:
        return f"{float(odd):.2f}"
    except (TypeError, ValueError):
        return None


def _real_book_name(name: Any) -> Optional[str]:
    """Numele exact din API; gol / '?' nu e o casă."""
    if not isinstance(name, str):
        return None
    cleaned = name.strip()
    if not cleaned or cleaned == "?":
        return None
    return cleaned


def _quote_name(quote: tuple) -> Optional[str]:
    return _real_book_name(quote[1] if len(quote) > 1 else None)


def pick_display_quote(quotes: list[tuple]) -> tuple[float, Optional[str]]:
    """Alege (cotă, casă) din ACEEAȘI listă de cotații, aceeași preluare.

    Preferă Superbet → Betano → Unibet dacă apar (id sau nume exact din API).
    Altfel: cea mai bună cotă, cu numele casei din același tuplu.
    Fără nume real → (cotă, None) — nu se ghicește casa.
    """
    if not quotes:
        raise ValueError("pick_display_quote: lista de cotații e goală")
    indexed: dict[int, tuple] = {}
    named: dict[str, tuple] = {}
    for q in quotes:
        qid = q[0] if q else None
        qname = _quote_name(q)
        qodd = q[2] if len(q) > 2 else q[-1]
        if isinstance(qid, int) and qid not in indexed:
            indexed[qid] = (qodd, qname)
        if qname:
            named.setdefault(qname.lower(), (qodd, qname))
    for pref_id, pref_key in _RO_PREFERRED_BOOKS:
        if pref_id in indexed:
            odd, name = indexed[pref_id]
            return float(odd), name
        if pref_key in named:
            odd, name = named[pref_key]
            return float(odd), name
    best_odd = max(float(q[2] if len(q) > 2 else q[-1]) for q in quotes)
    for q in quotes:
        odd = float(q[2] if len(q) > 2 else q[-1])
        if odd == best_odd:
            return odd, _quote_name(q)
    return best_odd, None


def _legacy_from_bookmaker(chosen: dict) -> dict[str, Any]:
    """Structura veche (un singur bookmaker, 4 piete) — neschimbata."""
    out: dict[str, Any] = {"bookmaker": chosen.get("name")}
    for bet in chosen.get("bets") or []:
        key = _LEGACY_MARKETS.get(bet.get("name"))
        if not key:
            continue
        values = {v.get("value"): v.get("odd") for v in bet.get("values") or []}
        if key == "over_under":
            values = {k: v for k, v in values.items() if k in _LEGACY_OU_LINES}
        out[key] = values
    return out


def _pick_reference_bookmaker(bookmakers: list[dict]) -> Optional[dict]:
    for pref in _PREFERRED_BOOKMAKERS:
        found = next((b for b in bookmakers if b.get("id") == pref), None)
        if found:
            return found
    return bookmakers[0] if bookmakers else None


def aggregate_odds(bookmakers: list[dict]) -> dict[str, Any]:
    """Agrega cotele tuturor caselor: avg/best/n_books, fara liste brute.

    Zero request-uri extra — /odds intoarce deja toate casele; inainte
    pastram doar una.
    """
    chosen = _pick_reference_bookmaker(bookmakers)
    if not chosen:
        return {"error": "Fara bookmakeri disponibili pentru acest meci."}

    # (market_key, outcome) -> list[(book_id, book_name, odd)]
    buckets: dict[tuple[str, str], list[tuple[Any, Optional[str], float]]] = {}
    labels: dict[str, str] = {}
    for book in bookmakers:
        bname = _real_book_name(book.get("name"))
        bid = book.get("id")
        for bet in book.get("bets") or []:
            raw_name = _as_text(bet.get("name"))
            key = normalize_market_name(raw_name)
            if not key:
                continue
            labels.setdefault(key, raw_name)
            for v in bet.get("values") or []:
                odd = _parse_odd(v.get("odd"))
                if odd is None:
                    continue
                outcome = _normalize_outcome(key, v.get("value"))
                buckets.setdefault((key, outcome), []).append((bid, bname, odd))

    ref_name = _real_book_name(chosen.get("name"))
    markets: list[dict[str, Any]] = []
    for key in {k for k, _ in buckets}:
        outcomes = []
        for (mk, outcome), quotes in buckets.items():
            if mk != key:
                continue
            odds = [o for _, _, o in quotes]
            avg = sum(odds) / len(odds)
            best = max(odds)
            best_quote = next(q for q in quotes if q[2] == best)
            best_book = _quote_name(best_quote)
            display_odd, display_book = pick_display_quote(quotes)
            ref = next((o for _id, n, o in quotes if n == ref_name), None) if ref_name else None
            row: dict[str, Any] = {
                "value": outcome,
                "avg_odd": round(avg, 3),
                "best_odd": round(best, 3),
                "n_books": len(quotes),
                "reference_odd": round(ref, 3) if ref is not None else None,
                "display_odd": round(display_odd, 3),
            }
            if best_book:
                row["best_bookmaker"] = best_book
            if display_book:
                row["display_bookmaker"] = display_book
            row["odds_label"] = format_odds_label(display_odd, display_book)
            outcomes.append(row)
        outcomes = _prune_outcomes(key, outcomes)
        if not outcomes or len(outcomes) > _MAX_OUTCOMES:
            continue
        markets.append({
            "key": key,
            "name": labels.get(key, key),
            "outcomes": sorted(outcomes, key=lambda o: o["value"]),
        })

    markets.sort(key=lambda m: (_market_priority(m["key"]), m["key"]))
    truncated = len(markets) > _MAX_MARKETS_PER_FIXTURE
    if truncated:
        markets = markets[:_MAX_MARKETS_PER_FIXTURE]

    result = _legacy_from_bookmaker(chosen)
    result["markets"] = markets
    result["truncated"] = truncated
    return result


async def get_odds(fixture_id: int) -> dict:
    """Cotele pre-match: cheile legacy (1X2, over_under, btts, double_chance)
    plus `markets` agregat pe toate casele (avg/best/n_books)."""
    raw = await _get("/odds", {"fixture": fixture_id}, "odds")
    if not raw:
        # Nu e o eroare de API, dar in diagnostic trebuie sa se vada distinct:
        # «nu exista cote» arata altfel decat «am fost limitat».
        _record_api_error("/odds", "no_odds_data", 200, "response=[] (fara cote publicate)",
                          None, {"fixture": fixture_id})
        return {"error": NO_ODDS_USER_MSG, "kind": "no_odds_data"}
    books = raw[0].get("bookmakers") or []
    try:
        return aggregate_odds(books)
    except Exception:
        # Nu lasam o exceptie de agregare sa arate ca 'eroare tehnica' in chat
        # daca tot avem cotele de baza de la o casa.
        log.exception("Agregarea cotelor a esuat pentru fixture %s", fixture_id)
        chosen = _pick_reference_bookmaker(books)
        if not chosen:
            return {"error": "Fara bookmakeri disponibili pentru acest meci."}
        out = _legacy_from_bookmaker(chosen)
        out["markets"] = []
        out["truncated"] = False
        out["warning"] = ("Am putut lua cotele de baza, dar nu toate pietele extra. "
                          "Nu e o eroare de retea — biletul poate folosi 1X2 / over / GG / sansa dubla.")
        return out
