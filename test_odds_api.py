#!/usr/bin/env python3
"""
Verifica ce case de pariuri ofera The Odds API pentru fotbal — cu accent pe cele din Romania.

Pasi:
  1. Creeaza cont gratuit pe https://the-odds-api.com/#get-access (500 req/luna, fara card)
  2. Pune cheia mai jos sau in .env ca ODDS_API_KEY=...
  3. python test_odds_api.py

Consuma ~6-10 requesturi din cota lunara.
"""

import os
import sys
from collections import Counter

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

API_KEY = os.environ.get("ODDS_API_KEY", "").strip() or "PUNE_CHEIA_AICI"
BASE = "https://api.the-odds-api.com/v4"

# Casele active in Romania pe care le cautam (potrivire pe fragment de nume)
RO_BOOKIES = ["superbet", "betano", "unibet", "fortuna", "netbet",
              "casa pariurilor", "publicwin", "maxbet", "winner", "efortuna"]

# Regiuni de interogat. "eu" e cea relevanta pentru RO; le testam pe toate ca sa vedem diferenta.
REGIONS = ["eu", "uk"]



# Competitii de test (cheile The Odds API)
LEAGUES = [
    ("soccer_epl", "Premier League"),
    ("soccer_uefa_champs_league", "UEFA Champions League"),
    ("soccer_spain_la_liga", "La Liga"),
    ("soccer_romania_liga_1", "Liga I Romania"),   # poate sa nu existe — verificam
]

if API_KEY == "PUNE_CHEIA_AICI":
    sys.exit("Pune cheia in .env (ODDS_API_KEY=...) sau direct in script.")


def get(path, params=None):
    p = {"apiKey": API_KEY}
    p.update(params or {})
    r = httpx.get(f"{BASE}{path}", params=p, timeout=30)
    remaining = r.headers.get("x-requests-remaining")
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:200]}", remaining
    return r.json(), None, remaining


print("=" * 70)
print("  The Odds API — verificare acoperire case de pariuri (fotbal)")
print("=" * 70)

# --- 1. Ce competitii de fotbal exista ---------------------------------
print("\n[1] Competitii de fotbal disponibile")
sports, err, rem = get("/sports")
if err:
    sys.exit(f"  ✗ {err}")

soccer = [s for s in sports if s.get("group") == "Soccer" and s.get("active")]
print(f"  {len(soccer)} competitii de fotbal active. Cele care contin 'romania':")
ro_leagues = [s for s in soccer if "romania" in s["key"].lower() or "romania" in s["title"].lower()]
for s in (ro_leagues or []):
    print(f"    ✓ {s['key']}  —  {s['title']}")
if not ro_leagues:
    print("    ✗ nicio competitie din Romania (Liga I nu e acoperita)")

print(f"\n  Exemple de competitii europene disponibile:")
for s in soccer[:12]:
    print(f"    {s['key']:<42} {s['title']}")
print(f"  ... (total {len(soccer)})")

# --- 2. Ce bookmakeri apar per liga/regiune -----------------------------
all_bookies = Counter()
ro_found = {}

for key, name in LEAGUES:
    if not any(s["key"] == key for s in soccer):
        print(f"\n[2] {name}: competitia '{key}' NU exista in API — sarim")
        continue
    for region in REGIONS:
        data, err, rem = get(f"/sports/{key}/odds",
                             {"regions": region, "markets": "h2h", "oddsFormat": "decimal"})
        if err:
            print(f"\n[2] {name} / regiune '{region}': {err}")
            continue
        if not data:
            print(f"\n[2] {name} / regiune '{region}': niciun meci disponibil acum")
            continue

        bookies = Counter()
        for match in data:
            for b in match.get("bookmakers", []):
                bookies[b["title"]] += 1
                all_bookies[b["title"]] += 1
                low = b["title"].lower()
                for ro in RO_BOOKIES:
                    if ro in low:
                        ro_found.setdefault(b["title"], set()).add(f"{name}/{region}")

        print(f"\n[2] {name} / regiune '{region}' — {len(data)} meciuri, {len(bookies)} case:")
        for title, cnt in bookies.most_common():
            mark = " ★ RO" if any(ro in title.lower() for ro in RO_BOOKIES) else ""
            print(f"    {title:<30} (in {cnt} meciuri){mark}")

# --- 3. Ce piete ofera pentru un meci -----------------------------------
print("\n[3] Piete disponibile (test pe Premier League, regiune eu)")
data, err, rem = get("/sports/soccer_epl/odds",
                     {"regions": "eu", "markets": "h2h,totals,spreads,btts",
                      "oddsFormat": "decimal"})
if err:
    print(f"  ! {err}")
elif data:
    markets = set()
    for m in data[:3]:
        for b in m.get("bookmakers", []):
            for mk in b.get("markets", []):
                markets.add(mk["key"])
    print(f"  Piete returnate: {sorted(markets) or '(niciuna)'}")
    m0 = data[0]
    print(f"  Exemplu: {m0['home_team']} vs {m0['away_team']}")
    for b in m0.get("bookmakers", [])[:3]:
        h2h = next((mk for mk in b["markets"] if mk["key"] == "h2h"), None)
        if h2h:
            vals = ", ".join(f"{o['name']}={o['price']}" for o in h2h["outcomes"])
            print(f"    {b['title']:<22} actualizat: {b.get('last_update')}  |  {vals}")

# --- REZUMAT ------------------------------------------------------------
print("\n" + "=" * 70)
print("  REZUMAT")
print("=" * 70)
if ro_found:
    print("  ✓ Case din Romania gasite:")
    for title, where in ro_found.items():
        print(f"      {title}  —  in: {', '.join(sorted(where))}")
else:
    print("  ✗ NICIO casa din Romania (Superbet/Betano/Unibet RO etc.) in rezultate.")
    print("    Atentie: 'Unibet' generic poate fi versiunea internationala, nu unibet.ro.")

print(f"\n  Toate casele intalnite ({len(all_bookies)}):")
for title, cnt in all_bookies.most_common():
    print(f"      {title}")

print(f"\n  Requesturi ramase luna asta: {rem}")
print("=" * 70)
