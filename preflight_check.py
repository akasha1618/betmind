#!/usr/bin/env python3
"""
Pre-flight check dupa upgrade la API-Football PRO.

Ruleaza din folderul betmind (cu venv activat):
    python preflight_check.py

Verifica:
  1. Planul activ si limita zilnica (/status)
  2. Statistici de echipa pe sezonul 2026 (/teams/statistics)
  3. Accidentari pe sezonul 2026 (/injuries)
  4. Predictii pe un meci real viitor (/predictions)

Consuma ~5 requesturi din cota zilnica.
"""

import os
import sys
from datetime import date, timedelta

import httpx
from dotenv import load_dotenv

load_dotenv()

KEY = os.environ.get("API_FOOTBALL_KEY", "").strip()
if not KEY:
    sys.exit("✗ API_FOOTBALL_KEY lipseste din .env — completeaza-l intai.")

BASE = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": KEY}
TZ = os.environ.get("APP_TIMEZONE", "Europe/Bucharest").strip() or "Europe/Bucharest"

PASS, FAIL, WARN = "✓", "✗", "!"
failures = 0


def call(endpoint: str, params: dict | None = None):
    """Returneaza (response, errors). errors=None inseamna OK."""
    r = httpx.get(BASE + endpoint, headers=HEADERS, params=params or {}, timeout=25)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:150]}"
    data = r.json()
    errs = data.get("errors")
    if errs and len(errs) > 0:  # dict sau lista; gol = OK
        return None, errs
    return data.get("response"), None


def report(ok: bool, label: str, detail: str = ""):
    global failures
    mark = PASS if ok else FAIL
    if not ok:
        failures += 1
    print(f"  {mark} {label}" + (f" — {detail}" if detail else ""))


print("=" * 62)
print("  BetMind — verificare pre-flight API-Football PRO")
print("=" * 62)

# ------------------------------------------------------------------ 1. STATUS
print("\n[1/4] Plan si limite (/status)")
resp, err = call("/status")
limit_day = None
if err or not resp:
    report(False, "Nu am putut citi statusul contului", str(err))
else:
    sub = resp.get("subscription", {}) or {}
    req = resp.get("requests", {}) or {}
    plan = sub.get("plan", "?")
    active = sub.get("active", False)
    limit_day = req.get("limit_day")
    used = req.get("current")
    report(bool(active), f"Abonament activ: {active}")
    report(str(plan).lower() != "free", f"Plan: {plan}",
           "trebuie sa fie Pro/Ultra/Mega, NU Free")
    report(bool(limit_day and limit_day >= 7500), f"Limita zilnica: {limit_day} req/zi",
           f"folosite azi: {used}")

# --------------------------------------------------- 2. STATISTICI SEZON 2026
print("\n[2/4] Statistici echipa, sezon 2026 (/teams/statistics)")
# Arsenal (42) in Premier League (39) — sezonul 2026-27 a inceput pe 21 aug
resp, err = call("/teams/statistics", {"team": 42, "league": 39, "season": 2026})
if err:
    report(False, "Endpoint blocat sau eroare", str(err))
elif not resp:
    report(False, "Raspuns gol — planul probabil nu acopera sezonul 2026")
else:
    form = resp.get("form")
    played = ((resp.get("fixtures") or {}).get("played") or {}).get("total")
    report(form is not None, f"Statistici sezon 2026 disponibile",
           f"Arsenal: forma='{form}', meciuri jucate={played}")

# ------------------------------------------------------ 3. ACCIDENTARI 2026
print("\n[3/4] Accidentari, sezon 2026 (/injuries)")
resp, err = call("/injuries", {"league": 39, "season": 2026})
if err:
    report(False, "Endpoint blocat sau eroare", str(err))
else:
    n = len(resp or [])
    report(n > 0, f"Accidentari returnate: {n}",
           "" if n > 0 else "0 poate fi legitim, dar la inceput de sezon PL e improbabil")

# -------------------------------------------------------- 4. PREDICTII
print("\n[4/4] Predictii pe un meci real (/predictions)")
fixture_id, fixture_name = None, None
for offset in (0, 1, 2):
    day = (date.today() + timedelta(days=offset)).isoformat()
    fixtures, err = call("/fixtures", {"date": day, "timezone": TZ})
    if err or not fixtures:
        continue
    for f in fixtures:
        status = ((f.get("fixture") or {}).get("status") or {}).get("short")
        league_id = (f.get("league") or {}).get("id")
        if status in ("NS", "TBD") and league_id in (39, 140, 135, 78, 61, 2, 3, 283, 88, 94):
            fixture_id = (f.get("fixture") or {}).get("id")
            teams = f.get("teams") or {}
            fixture_name = f"{(teams.get('home') or {}).get('name')} vs {(teams.get('away') or {}).get('name')}"
            break
    if fixture_id:
        break

if not fixture_id:
    report(False, "Nu am gasit niciun meci viitor in urmatoarele 3 zile (neasteptat)")
else:
    resp, err = call("/predictions", {"fixture": fixture_id})
    if err or not resp:
        report(False, f"Predictii indisponibile pentru {fixture_name}", str(err))
    else:
        pred = (resp[0].get("predictions") or {}) if resp else {}
        pct = pred.get("percent") or {}
        advice = pred.get("advice")
        report(bool(pct), f"Predictii OK pentru {fixture_name}",
               f"1/X/2 = {pct.get('home')}/{pct.get('draw')}/{pct.get('away')}, advice: '{advice}'")

# ------------------------------------------------------------------ REZUMAT
print("\n" + "=" * 62)
if failures == 0:
    rec = (limit_day - 500) if limit_day else 7000
    print(f"  {PASS} TOATE VERIFICARILE AU TRECUT — poti incepe V1-A in Cursor.")
    print(f"\n  Adauga in .env linia:")
    print(f"      MAX_DAILY_API_REQUESTS={rec}")
    print(f"  (limita ta {limit_day}/zi minus marja de siguranta 500)")
else:
    print(f"  {FAIL} {failures} verificari esuate.")
    print("  Daca planul apare tot 'Free': asteapta 2-3 minute dupa plata,")
    print("  apoi ruleaza din nou. Daca persista, verifica in dashboard ca")
    print("  abonamentul e pe API-FOOTBALL (nu alt sport) si contacteaza suportul.")
print("=" * 62)
