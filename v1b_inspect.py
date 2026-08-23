#!/usr/bin/env python3
"""
Diagnostic V1-B: citeste ce e REAL in DB si in pachetul de date, ca sa stabilim
daca ✗-urile din v1b_check sunt doar nume diferite de chei (fals-negativ) sau
un bug real de validare.

Ruleaza:  python v1b_inspect.py
(read-only pe analize; asambleaza un singur pachet de date — cateva requesturi, majoritatea din cache)
"""
import asyncio
import inspect
import json
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()
DB = os.environ.get("DB_PATH", "data/betmind.db")
TZ = os.environ.get("APP_TIMEZONE", "Europe/Bucharest").strip() or "Europe/Bucharest"


def deep_get(obj, key):
    """Cauta o cheie oriunde in structura (recursiv)."""
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = deep_get(v, key)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = deep_get(v, key)
            if r is not None:
                return r
    return None


def tree(x, indent=4, depth=3):
    """Afiseaza structura (chei, tipuri, marimi) pana la adancimea data."""
    pad = " " * indent
    if isinstance(x, dict):
        for k, v in x.items():
            if isinstance(v, (dict, list)):
                print(f"{pad}{k}: {type(v).__name__}[{len(v)}]")
                if depth > 1:
                    tree(v, indent + 4, depth - 1)
            else:
                s = str(v)
                print(f"{pad}{k}: {s[:70] + '…' if len(s) > 70 else s}")
    elif isinstance(x, list) and x:
        print(f"{pad}(primul din {len(x)} elemente:)")
        tree(x[0], indent + 4, depth - 1)


async def maybe_await(x):
    return await x if inspect.isawaitable(x) else x


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    # ---------------- A. Ce e salvat REAL in `analyses` ----------------
    print("=" * 68)
    print("A. Ultimele 2 analize salvate in DB (adevarul din coloana json)")
    print("=" * 68)
    rows = con.execute(
        "SELECT fixture_id, created_at, model, json FROM analyses ORDER BY created_at DESC LIMIT 2"
    ).fetchall()
    for r in rows:
        print(f"\n--- fixture {r['fixture_id']}  ({r['created_at']}, {r['model']}) ---")
        try:
            payload = json.loads(r["json"])
        except Exception as e:
            print(f"  json invalid in DB: {e}\n  RAW: {str(r['json'])[:400]}")
            continue

        print("  STRUCTURA:")
        tree(payload, indent=4, depth=2)

        print("\n  VERIFICARI-CHEIE (cautate oriunde in structura):")
        mp = deep_get(payload, "market_probs")
        bc = deep_get(payload, "best_candidates")
        tf = deep_get(payload, "top_factors")
        ang = deep_get(payload, "angle")
        conf = deep_get(payload, "confidence")
        err = deep_get(payload, "error") or deep_get(payload, "analysis_failed")
        print(f"    market_probs:    {mp if mp else 'LIPSESTE'}")
        print(f"    best_candidates: {len(bc) if isinstance(bc, list) else 'LIPSESTE'}"
              + (f"  (ex: {json.dumps(bc[0], ensure_ascii=False)[:100]}…)" if isinstance(bc, list) and bc else ""))
        print(f"    top_factors:     {len(tf) if isinstance(tf, list) else 'LIPSESTE'}")
        if isinstance(tf, list):
            for f in tf[:3]:
                print(f"        • {str(f)[:100]}")
        print(f"    angle:           {str(ang)[:110] if ang else 'LIPSESTE'}")
        print(f"    confidence:      {conf if conf else 'LIPSESTE'}")
        print(f"    eroare/failed:   {str(err)[:150] if err else '(nu)'}")

        if mp and isinstance(bc, list) and bc:
            print("  => VERDICT rand: IMPLEMENTAREA E BUNA — checker-ul meu citea la nivelul gresit.")
        elif err:
            print("  => VERDICT rand: analiza a ESUAT real — de raportat lui Cursor cu eroarea de mai sus.")
        else:
            print("  => VERDICT rand: structura neasteptata — trimite-mi output-ul integral.")

    # ---------------- B. Structura reala a pachetului de date ----------------
    print("\n" + "=" * 68)
    print("B. Structura reala a unui data pack proaspat (home/away desfacute)")
    print("=" * 68)
    now = datetime.now(ZoneInfo(TZ))
    row = con.execute(
        """SELECT fixture_id, home_name, away_name FROM fixtures
           WHERE status IN ('NS','TBD') AND date_local >= ?
           ORDER BY date_local, time_local LIMIT 1""",
        (now.date().isoformat(),),
    ).fetchone()
    con.close()
    if not row:
        print("  (niciun meci NS viitor in DB)")
        return
    print(f"  Meci: {row['home_name']} - {row['away_name']} (id={row['fixture_id']})\n")
    import analysts
    pack = asyncio.run(maybe_await(analysts.assemble_data_pack(row["fixture_id"])))
    tree(pack, indent=2, depth=3)

    print("\n  Cautare adanca a campurilor calculate:")
    for key in ("days_since_last_match", "days_rest", "midweek_european_game", "midweek_european"):
        v = deep_get(pack, key)
        if v is not None:
            print(f"    {key}: {v}")
    print("=" * 68)


if __name__ == "__main__":
    main()
