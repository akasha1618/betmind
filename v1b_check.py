#!/usr/bin/env python3
"""
Verificare V1-B pe date REALE: pachetul de date al analistului + un analist
rulat cap-coada + persistenta + cost.

Ruleaza din folderul betmind (serverul poate fi oprit sau pornit, indiferent):
    python v1b_check.py             -> test complet (~6-10 req API + 1 apel Haiku, ~centi)
    python v1b_check.py --counts    -> doar contoarele analyses/usage_log (pt. testul de reuse)

Daca primesti erori de import sau de nume de functii/coloane, schema lui Cursor
difera putin de spec — trimite-mi eroarea exacta si ajustez scriptul.
"""
import asyncio
import inspect
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()
DB = os.environ.get("DB_PATH", "data/betmind.db")
TZ = os.environ.get("APP_TIMEZONE", "Europe/Bucharest").strip() or "Europe/Bucharest"

OK, BAD, WARN = "✓", "✗", "!"
issues = []


def mark(ok, label, detail="", warn_only=False):
    sym = OK if ok else (WARN if warn_only else BAD)
    if not ok and not warn_only:
        issues.append(label)
    print(f"  {sym} {label}" + (f" — {detail}" if detail else ""))


def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def table_count(con, table, where="1=1", args=()):
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}", args).fetchone()[0]
    except sqlite3.OperationalError as e:
        return f"(tabel indisponibil: {e})"


def print_counts():
    con = db()
    print("=== Contoare DB ===")
    print(f"  analyses total: {table_count(con, 'analyses')}")
    try:
        rows = con.execute(
            "SELECT fixture_id, created_at, model FROM analyses ORDER BY created_at DESC LIMIT 5"
        ).fetchall()
        for r in rows:
            print(f"    fixture {r['fixture_id']}  {r['created_at']}  [{r['model']}]")
    except sqlite3.OperationalError:
        pass
    print(f"  usage_log total: {table_count(con, 'usage_log')}")
    try:
        rows = con.execute(
            """SELECT model, SUM(input_tokens) i, SUM(output_tokens) o, COUNT(*) n
               FROM usage_log GROUP BY model"""
        ).fetchall()
        for r in rows:
            print(f"    {r['model']}: {r['n']} apeluri, in={r['i']}, out={r['o']}")
    except sqlite3.OperationalError:
        pass
    con.close()


async def maybe_await(x):
    return await x if inspect.isawaitable(x) else x


def find_key(d, *needles):
    """Gaseste o cheie care contine oricare din fragmentele date (tolerant la nume)."""
    for k in d.keys():
        lk = k.lower()
        if any(n in lk for n in needles):
            return k
    return None


async def main():
    print("=" * 66)
    print_counts()

    # --- alege un meci real viitor din DB ---------------------------------
    now = datetime.now(ZoneInfo(TZ))
    con = db()
    row = con.execute(
        """SELECT fixture_id, date_local, time_local, home_name, away_name, league_name
           FROM fixtures
           WHERE status IN ('NS','TBD') AND date_local >= ?
             AND league_id IN (39,140,135,78,61,283,88,94)
           ORDER BY date_local, time_local LIMIT 1""",
        (now.date().isoformat(),),
    ).fetchone()
    con.close()
    if not row:
        raise SystemExit(f"{BAD} Niciun meci NS viitor in DB — ruleaza sync-ul intai.")
    fid = row["fixture_id"]
    print(f"\n=== Meci de test: {row['home_name']} - {row['away_name']} "
          f"({row['league_name']}, {row['date_local']} {row['time_local']}, id={fid}) ===")

    # --- importa modulul analistilor --------------------------------------
    try:
        import analysts
    except Exception as e:
        raise SystemExit(f"{BAD} Nu pot importa analysts.py: {e}")

    # --- 1) DATA PACK ------------------------------------------------------
    print("\n=== 1. Data pack (assemble_data_pack) ===")
    fn = getattr(analysts, "assemble_data_pack", None)
    if fn is None:
        raise SystemExit(f"{BAD} analysts.assemble_data_pack nu exista — spune-mi numele real.")
    t0 = time.time()
    pack = await maybe_await(fn(fid))
    print(f"  (asamblat in {time.time()-t0:.1f}s; chei top-level: {sorted(list(pack.keys()))[:14]}…)")

    k = find_key(pack, "last_match", "recent", "form_matches")
    if k and isinstance(pack[k], dict):
        h = pack[k].get("home") or []
        a = pack[k].get("away") or []
        mark(len(h) >= 4 and len(a) >= 4, "Ultimele meciuri ambele echipe", f"home={len(h)}, away={len(a)}")
    else:
        mark(bool(k), "Ultimele meciuri", f"cheie gasita: {k}", warn_only=True)

    for label, needles in [
        ("Statistici de sezon", ("statistic", "season_stats", "team_stats")),
        ("Accidentari", ("injur",)),
        ("H2H", ("h2h", "head")),
        ("Clasament", ("standing", "rank")),
        ("Cote", ("odds",)),
    ]:
        k = find_key(pack, *needles)
        val = pack.get(k) if k else None
        nonempty = bool(val) and val not in ({}, [])
        detail = f"{k}: " + (f"{len(val)} elem" if isinstance(val, (list, dict)) else str(val)[:40])
        mark(nonempty, label, detail, warn_only=(label == "Accidentari"))  # 0 accidentari poate fi legitim

    k = find_key(pack, "prediction")
    mark(bool(k and pack.get(k)), "PREDICTIONS (endpoint-ul platit Pro!)",
         f"{k}: {str(pack.get(k))[:70]}…" if k else "LIPSESTE — semnalul Pro nu ajunge la analisti")

    for side in ("home", "away"):
        k = find_key(pack, f"days_since_last_match_{side}", f"days_rest_{side}")
        v = pack.get(k) if k else None
        mark(isinstance(v, int) and 0 <= v <= 40, f"days_since_last_match_{side}", f"= {v}")
        k2 = find_key(pack, f"midweek_european_game_{side}", f"european_{side}")
        mark(k2 is not None and isinstance(pack.get(k2), bool),
             f"midweek_european_game_{side}", f"= {pack.get(k2) if k2 else '?'}")

    gaps = pack.get(find_key(pack, "gap") or "", [])
    print(f"  {WARN} data_gaps declarate: {gaps if gaps else '(niciunul)'}")

    # --- 2) UN ANALIST REAL ------------------------------------------------
    print("\n=== 2. Analist real (analyze_matches pe 1 meci) ===")
    con = db()
    analyses_before = table_count(con, "analyses")
    usage_before = table_count(con, "usage_log")
    con.close()

    runner = getattr(analysts, "analyze_matches", None) or getattr(analysts, "analyze_match", None)
    t0 = time.time()
    try:
        result = await maybe_await(runner([fid]) if runner.__name__ == "analyze_matches" else runner(fid))
    except TypeError:
        # poate cere un emitter — incercam cu unul no-op
        async def _noop(*a, **kw):
            return None
        result = await maybe_await(runner([fid], _noop))
    dur = time.time() - t0
    analysis = result[0] if isinstance(result, list) else result
    print(f"  (analiza in {dur:.1f}s)")

    if not isinstance(analysis, dict) or analysis.get("analysis_failed"):
        mark(False, "Analiza a esuat", str(analysis)[:200])
    else:
        mp = analysis.get("market_probs", {})
        s1x2 = sum(mp.get(x, 0) for x in ("home", "draw", "away"))
        sou = mp.get("over25", 0) + mp.get("under25", 0)
        mark(all(0 <= v <= 1 for v in mp.values()) and len(mp) >= 5, "market_probs valide", str(mp))
        mark(0.9 <= s1x2 <= 1.1, "1X2 se aduna la ~1", f"suma={s1x2:.2f}")
        mark(0.9 <= sou <= 1.1, "over25+under25 ~1", f"suma={sou:.2f}")

        cands = analysis.get("best_candidates", [])
        mark(len(cands) >= 1, "best_candidates prezente", f"{len(cands)} candidati")
        for c in cands:
            implied = 1 / c["odds"] if c.get("odds") else 0
            optimist = c.get("prob", 0) > implied * 1.4
            line = f"{c.get('market')}/{c.get('pick')} @ {c.get('odds')} p={c.get('prob')} (implied {implied:.2f})"
            mark(not optimist, f"candidat sanatos: {line}", "" if not optimist else "prob mult peste piata!", warn_only=True)

        tfs = analysis.get("top_factors", [])
        with_digits = sum(1 for f in tfs if re.search(r"\d", f))
        mark(1 <= len(tfs) <= 5 and with_digits == len(tfs),
             "top_factors specifici (toti contin cifre)",
             f"{with_digits}/{len(tfs)} cu cifre", warn_only=(with_digits >= max(1, len(tfs) - 1)))
        for f in tfs:
            print(f"      • {f}")
        angle = analysis.get("angle", "")
        mark(bool(angle and len(angle) > 15), "angle (conexiunea out-of-the-box)", angle[:110])
        mark(analysis.get("confidence") in ("high", "medium", "low"), "confidence valid",
             analysis.get("confidence", "?"))

    con = db()
    analyses_after = table_count(con, "analyses")
    usage_after = table_count(con, "usage_log")
    rows = []
    try:
        rows = con.execute(
            """SELECT model, input_tokens, output_tokens FROM usage_log
               ORDER BY created_at DESC LIMIT ?""", (max(usage_after - usage_before, 0) or 1,)
        ).fetchall()
    except sqlite3.OperationalError:
        pass
    con.close()

    print("\n=== 3. Persistenta si cost ===")
    mark(isinstance(analyses_after, int) and analyses_after > analyses_before,
         "Analiza persistata in `analyses`", f"{analyses_before} -> {analyses_after}")
    mark(isinstance(usage_after, int) and usage_after > usage_before,
         "usage_log alimentat", f"{usage_before} -> {usage_after}")
    RATES = {"haiku": (1.0, 5.0), "sonnet": (3.0, 15.0)}  # $/MTok in,out — estimativ
    cost = 0.0
    for r in rows:
        rate = next((v for kname, v in RATES.items() if kname in (r["model"] or "").lower()), (2.0, 10.0))
        cost += r["input_tokens"] / 1e6 * rate[0] + r["output_tokens"] / 1e6 * rate[1]
        print(f"      {r['model']}: in={r['input_tokens']}, out={r['output_tokens']}")
    print(f"  Cost estimativ apelul(ele) de analiza: ~${cost:.4f}")

    print("\n" + "=" * 66)
    if issues:
        print(f"  {BAD} DE INVESTIGAT ({len(issues)}): " + "; ".join(issues))
    else:
        print(f"  {OK} V1-B arata solid pe date reale. Treci la testul de aur (classic vs analysts).")
    print("=" * 66)


if __name__ == "__main__":
    if "--counts" in sys.argv:
        print_counts()
    else:
        asyncio.run(main())
