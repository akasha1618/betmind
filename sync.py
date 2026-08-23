"""
Sincronizarea de fundal a fixture store-ului local (V1-A).

Ruleaza ca task asyncio pornit din lifespan-ul FastAPI. Tine baza locala la zi
pentru fereastra [azi - SYNC_WINDOW_PAST_DAYS, azi + SYNC_WINDOW_FUTURE_DAYS]:
  - zilele HOT (azi + maine) se reimprospateaza la SYNC_HOT_MINUTES;
  - restul (WARM) la SYNC_WARM_HOURS.
UN request /fixtures?date=X&timezone=... per zi sincronizata; se filtreaza pe
tracked_leagues si se upsert-eaza cu detectie de schimbari (amanari etc.).
La buget scazut, zilele HOT au prioritate; BudgetExhausted opreste ciclul onest.

CLI:  python -m sync --once   (o trecere completa, apoi iese)
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import timedelta
from typing import Optional

import db
import football_data as fd

log = logging.getLogger("betmind.sync")

_CHECK_INTERVAL_SECONDS = 60  # cat de des verificam ce zile sunt scadente


def sync_enabled() -> bool:
    return os.environ.get("SYNC_ENABLED", "true").strip().lower() in ("1", "true", "yes")


def _hot_minutes() -> int:
    return int(os.environ.get("SYNC_HOT_MINUTES", "30"))


def _warm_hours() -> int:
    return int(os.environ.get("SYNC_WARM_HOURS", "6"))


def _window_days() -> tuple[list[str], list[str]]:
    """(zile_hot, zile_warm) in fereastra de sincronizare. Hot = azi + maine."""
    win_start, win_end = fd.sync_window()
    today = fd.today_local()
    hot, warm = [], []
    d = win_start
    while d <= win_end:
        if d in (today, today + timedelta(days=1)):
            hot.append(d.isoformat())
        else:
            warm.append(d.isoformat())
        d += timedelta(days=1)
    # Warm: intai zilele viitoare apropiate (mai valoroase pt. recomandari),
    # apoi trecutul, cel mai recent primul.
    today_iso = today.isoformat()
    future = sorted(d for d in warm if d > today_iso)
    past = sorted((d for d in warm if d < today_iso), reverse=True)
    return hot, future + past


def _is_due(last_synced_at: Optional[str], max_age_seconds: int) -> bool:
    if not last_synced_at:
        return True
    try:
        from datetime import datetime
        then = datetime.fromisoformat(last_synced_at)
        if then.tzinfo is None:
            then = then.replace(tzinfo=fd.app_timezone())
        return (fd.now_local() - then).total_seconds() > max_age_seconds
    except ValueError:
        return True


async def sync_day(day: str) -> int:
    """
    Sincronizeaza o zi: UN request API, filtrare pe ligile urmarite, upsert cu
    detectie de schimbari. Returneaza numarul de schimbari detectate.
    Ridica BudgetExhausted / FootballDataError mai departe.
    """
    parsed = await fd.fetch_day(day)
    tracked = await db.get_tracked_leagues()
    synced_at = fd.now_local().isoformat(timespec="seconds")

    n_changes = 0
    for p in parsed:
        if p["league_id"] not in tracked:
            continue
        changes = await db.upsert_fixture(p, synced_at)
        for field, old, new in changes:
            log.info("Fixture change %s (%s–%s): %s %s -> %s",
                     p["fixture_id"], p["home_name"], p["away_name"], field, old, new)
        n_changes += len(changes)

    await db.mark_day_synced(day, synced_at)
    return n_changes


async def run_sync_cycle(force: bool = False) -> dict:
    """
    O trecere: sincronizeaza zilele scadente (hot intai — au prioritate la
    buget scazut). Scrie un rand in sync_log. Returneaza un rezumat.
    """
    started_at = fd.now_local().isoformat(timespec="seconds")
    hot, warm = _window_days()
    info = await db.day_sync_info(hot + warm)

    due: list[str] = []
    if force:
        due = hot + warm
    else:
        due += [d for d in hot if _is_due(info.get(d), _hot_minutes() * 60)]
        due += [d for d in warm if _is_due(info.get(d), _warm_hours() * 3600)]

    synced: list[str] = []
    requests_used = 0
    ok, error = True, None

    for day in due:
        try:
            await sync_day(day)
            synced.append(day)
            requests_used += 1
        except fd.BudgetExhausted as e:
            ok = len(synced) > 0  # partial e tot un esec doar daca n-am facut nimic
            error = str(e)
            log.warning("Sync oprit — buget epuizat dupa %d zile: %s", len(synced), e)
            break
        except fd.FootballDataError as e:
            ok = False
            error = str(e)
            log.error("Sync a esuat pentru ziua %s: %s", day, e)
            break

    finished_at = fd.now_local().isoformat(timespec="seconds")
    if due:  # nu umplem jurnalul cu cicluri in care nu era nimic de facut
        await db.add_sync_log(started_at, finished_at, synced, requests_used, ok, error)

    return {"due": due, "synced": synced, "api_requests_used": requests_used,
            "ok": ok, "error": error}


async def hot_sync_once() -> None:
    """Sincronizare imediata a zilelor hot la pornire (non-blocking prin task)."""
    hot, _ = _window_days()
    started_at = fd.now_local().isoformat(timespec="seconds")
    synced, ok, error = [], True, None
    for day in hot:
        try:
            await sync_day(day)
            synced.append(day)
        except fd.FootballDataError as e:  # include BudgetExhausted
            ok = len(synced) > 0
            error = str(e)
            log.warning("Hot sync la pornire incomplet: %s", e)
            break
    await db.add_sync_log(started_at, fd.now_local().isoformat(timespec="seconds"),
                          synced, len(synced), ok, error)


async def sync_loop() -> None:
    """Bucla de fundal: hot sync imediat, apoi verificare la ~60s."""
    await db.init_db()
    try:
        await hot_sync_once()
    except Exception:
        log.exception("Hot sync la pornire a esuat")
    while True:
        # V1-F: nicio exceptie dintr-un ciclu nu omoara task-ul — logam si
        # continuam la tick-ul urmator. Fiecare ciclu cu activitate isi
        # raporteaza rezultatul, ca sa fie vizibil in logurile de productie.
        try:
            result = await run_sync_cycle()
            if result["due"]:
                log.info("Ciclu sync: %d/%d zile sincronizate, %d request-uri API, ok=%s%s",
                         len(result["synced"]), len(result["due"]),
                         result["api_requests_used"], result["ok"],
                         f" — {result['error']}" if result["error"] else "")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Ciclul de sincronizare a esuat; reincerc la urmatorul tick")
        await asyncio.sleep(_CHECK_INTERVAL_SECONDS)


async def _main_once() -> None:
    await db.init_db()
    result = await run_sync_cycle(force=True)
    print(f"Sync complet: {len(result['synced'])}/{len(result['due'])} zile, "
          f"{result['api_requests_used']} request-uri API, ok={result['ok']}")
    if result["error"]:
        print(f"Eroare: {result['error']}")


if __name__ == "__main__":
    import argparse

    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(description="BetMind fixture sync")
    parser.add_argument("--once", action="store_true", help="o trecere completa, apoi iesire")
    args = parser.parse_args()

    if args.once:
        asyncio.run(_main_once())
    else:
        asyncio.run(sync_loop())
