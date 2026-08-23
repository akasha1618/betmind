#!/usr/bin/env python3
"""
Pasul 4 din checklist V1-A — testul de fallback in afara ferestrei de sync.

Cum se foloseste (serverul trebuie sa ruleze in celalalt terminal):
  1.  python step4_check.py            -> face poza "INAINTE"
  2.  intreaba in chat:  "Ce meciuri sunt pe 19 septembrie?"
  3.  python step4_check.py            -> face poza "DUPA" si da verdictul

Alta data de test:  python step4_check.py 2026-09-12
"""
import json
import os
import sys
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

load_dotenv()

TEST_DATE = sys.argv[1] if len(sys.argv) > 1 else "2026-09-19"
DB = os.environ.get("DB_PATH", "data/betmind.db")
PORT = os.environ.get("PORT", "8000")
TZ = os.environ.get("APP_TIMEZONE", "Europe/Bucharest").strip() or "Europe/Bucharest"
PAST = int(os.environ.get("SYNC_WINDOW_PAST_DAYS", "7"))
FUTURE = int(os.environ.get("SYNC_WINDOW_FUTURE_DAYS", "14"))
SNAP = ".step4_snapshot.json"

today = datetime.now(ZoneInfo(TZ)).date()
win_start, win_end = today - timedelta(days=PAST), today + timedelta(days=FUTURE)

# --- citeste starea curenta -------------------------------------------------
try:
    health = httpx.get(f"http://localhost:{PORT}/api/health", timeout=10).json()
except Exception as e:
    raise SystemExit(f"✗ Nu pot citi /api/health — serverul ruleaza? ({e})")
counter = health.get("api_requests_used_today")

con = sqlite3.connect(DB)
rows_test_date = con.execute(
    "SELECT COUNT(*) FROM fixtures WHERE date_local = ?", (TEST_DATE,)
).fetchone()[0]
per_day = con.execute(
    "SELECT date_local, COUNT(*) FROM fixtures GROUP BY date_local ORDER BY date_local"
).fetchall()
con.close()

outside = [(d, n) for d, n in per_day if d and (d < win_start.isoformat() or d > win_end.isoformat())]

print("=" * 62)
print(f"  Fereastra de sync: {win_start} -> {win_end}   (azi: {today})")
print(f"  Data de test:      {TEST_DATE}  "
      + ("[OK, in afara ferestrei]" if TEST_DATE > win_end.isoformat() or TEST_DATE < win_start.isoformat()
         else "[!! e IN fereastra — alege alta data]"))
print(f"  Contor requesturi azi: {counter}")
print(f"  Randuri in DB pentru {TEST_DATE}: {rows_test_date}")

# --- diagnostic: zile din DB aflate in afara ferestrei ----------------------
print("\n  Zile din DB aflate IN AFARA ferestrei de sync:")
if not outside:
    print("    (niciuna)")
else:
    for d, n in outside:
        print(f"    {d}: {n} meciuri")
    if len(outside) >= 10:
        print("    !! Multe zile consecutive in afara ferestrei -> fereastra pare")
        print("       configurata gresit. Cere-i lui Cursor sa verifice SYNC_WINDOW_FUTURE_DAYS.")
    else:
        print("    -> Zile izolate = au fost aduse de fallback-ul live la intrebari")
        print("       anterioare din chat. Configurarea ferestrei e corecta.")

# --- logica inainte/dupa ----------------------------------------------------
snap = None
if os.path.exists(SNAP):
    with open(SNAP, encoding="utf-8") as f:
        snap = json.load(f)
    if snap.get("test_date") != TEST_DATE:
        snap = None  # snapshot pentru alta data — pornim de la zero

if snap is None:
    with open(SNAP, "w", encoding="utf-8") as f:
        json.dump({"taken_at": datetime.now().isoformat(), "counter": counter,
                   "rows": rows_test_date, "test_date": TEST_DATE}, f)
    print("\n" + "=" * 62)
    print("  POZA 'INAINTE' SALVATA.")
    if rows_test_date > 0:
        print(f"  !! {TEST_DATE} EXISTA deja in DB ({rows_test_date} meciuri) —")
        print(f"     testul nu e concludent pe data asta. Ruleaza cu alta zi:")
        print(f"         python step4_check.py 2026-09-12")
    else:
        print(f"  Acum intreaba in chat:  \"Ce meciuri sunt pe {TEST_DATE[8:]} septembrie?\"")
        print("  ...apoi ruleaza din nou:  python step4_check.py")
    print("=" * 62)
    sys.exit(0)

# a doua rulare -> verdict
os.remove(SNAP)
delta = (counter or 0) - (snap.get("counter") or 0)
rows_before, rows_after = snap.get("rows", 0), rows_test_date

print("\n" + "=" * 62)
print(f"  VERDICT PASUL 4  (contor: {snap['counter']} -> {counter}, delta {delta};"
      f" randuri {TEST_DATE}: {rows_before} -> {rows_after})")
ok = rows_before == 0 and rows_after > 0 and 1 <= delta <= 5
if ok:
    print("  ✓ PASS — ziua din afara ferestrei a fost adusa live din API")
    print(f"    ({delta} request{'uri' if delta > 1 else ''}) si salvata in DB ({rows_after} meciuri).")
    if delta > 1:
        print("    Nota: delta >1 e ok daca sync-ul automat a rulat intre poze —")
        print("    verifica in terminalul serverului daca apare un log de sync.")
elif rows_before > 0:
    print("  ! NECONCLUDENT — ziua exista deja in DB dinainte. Repeta cu alta data:")
    print("        python step4_check.py 2026-09-12")
elif rows_after == 0:
    print("  ✗ FAIL — intrebarea din chat nu a adus ziua in DB. Ai pus intrebarea")
    print("    intre cele doua rulari? Daca da, fallback-ul live nu functioneaza.")
elif delta == 0:
    print("  ✗ FAIL — datele au aparut in DB dar contorul nu a crescut:")
    print("    budget guard-ul nu numara requestul de fallback. De raportat lui Cursor.")
else:
    print(f"  ! SUSPECT — delta {delta} e prea mare pentru o singura zi; verifica")
    print("    logurile serverului (posibil sync complet suprapus) si repeta testul.")
print("=" * 62)
