#!/usr/bin/env python3
"""
OddsPapi — cauta cota GG (Ambele echipe marcheaza) la Superbet
pentru meciul Rapid Viena - Hearts.

Scop: comparatie directa cu ce a afisat BetMind (1.77 via API-Football,
instantaneu de la 12:42 UTC) si cu site-ul Superbet (1.73).

Rulare:
  1. In .env:  ODDSPAPI_KEY=cheia_ta
  2. python test_oddspapi_gg.py

Poti schimba echipele si casa in constantele de mai jos.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

API_KEY = os.environ.get("ODDSPAPI_KEY", "").strip() or "PUNE_CHEIA_AICI"
BASE = "https://api.oddspapi.io/v4"
SPORT_FOOTBALL = 10

ECHIPA_1 = "rapid"          # fragment din numele gazdei
ECHIPA_2 = "heart"          # fragment din numele oaspetelui
CASE_CAUTATE = ["superbet", "betano", "unibet"]   # in ordinea preferintei
CUVINTE_GG = ["both teams", "btts", "both to score", "gg"]

if API_KEY == "PUNE_CHEIA_AICI":
    sys.exit("Pune cheia in .env ca ODDSPAPI_KEY=...")


def get(path, params=None):
    p = {"apiKey": API_KEY}
    p.update(params or {})
    try:
        r = httpx.get(f"{BASE}{path}", params=p, timeout=45)
    except httpx.HTTPError as e:
        return None, f"eroare retea: {e}"
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}: {r.text[:300]}"
    try:
        return r.json(), None
    except Exception as e:
        return None, f"raspuns non-JSON: {e}"


def as_list(x):
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        for k in ("data", "results", "items"):
            if isinstance(x.get(k), list):
                return x[k]
    return []


print("=" * 74)
print(f"  OddsPapi — cota GG pentru {ECHIPA_1.title()} vs {ECHIPA_2.title()}")
print("=" * 74)

# --- 1. Numele exact al caselor cautate --------------------------------
print("\n[1] Identific casele de pariuri")
bms, err = get("/bookmakers")
if err:
    sys.exit(f"  ✗ {err}")

bm_names = []
for b in as_list(bms):
    if isinstance(b, dict):
        bm_names.append(b.get("bookmakerName") or b.get("name") or b.get("slug") or "")
    else:
        bm_names.append(str(b))

gasite = {}
for cautat in CASE_CAUTATE:
    potriviri = [n for n in bm_names if cautat in str(n).lower()]
    if potriviri:
        gasite[cautat] = potriviri
        print(f"  ★ '{cautat}' -> {potriviri}")
    else:
        print(f"  ✗ '{cautat}' nu apare in lista celor {len(bm_names)} case")

if not gasite:
    print("\n  Nicio casa cautata nu exista. Continui fara filtru de casa,")
    print("  ca sa vad ce case ofera cote pentru acest meci.")

# --- 2. Gasesc meciul --------------------------------------------------
print("\n[2] Caut meciul in turneele de fotbal")
tours, err = get("/tournaments", {"sportId": SPORT_FOOTBALL})
if err:
    sys.exit(f"  ✗ {err}")
tours = as_list(tours)
print(f"  {len(tours)} turnee de fotbal disponibile")

# participantii ne dau numele echipelor; incercam sa gasim ID-urile
parts, perr = get("/participants", {"sportId": SPORT_FOOTBALL})
nume_dupa_id = {}
if not perr:
    for p in as_list(parts):
        if isinstance(p, dict):
            pid = p.get("participantId") or p.get("id")
            nm = p.get("participantName") or p.get("name")
            if pid is not None and nm:
                nume_dupa_id[pid] = nm
    print(f"  {len(nume_dupa_id)} echipe in dictionar")

# cautam in turneele europene (Conference/Europa/Champions) + toate, la nevoie
prioritare = [t for t in tours if any(k in str(t.get("tournamentName", "")).lower()
              for k in ("conference", "europa", "champions"))]
de_cautat = prioritare + [t for t in tours if t not in prioritare]

meci = None
tournament_gasit = None
for t in de_cautat[:25]:          # limitam ca sa nu ardem cota de requesturi
    tid = t.get("tournamentId")
    fx, ferr = get("/fixtures", {"tournamentIds": tid})
    if ferr:
        continue
    for f in as_list(fx):
        n1 = str(nume_dupa_id.get(f.get("participant1Id"), "")).lower()
        n2 = str(nume_dupa_id.get(f.get("participant2Id"), "")).lower()
        blob = f"{n1} {n2} {json.dumps(f, ensure_ascii=False).lower()}"
        if ECHIPA_1 in blob and ECHIPA_2 in blob:
            meci, tournament_gasit = f, t
            break
    if meci:
        break

if not meci:
    print(f"  ✗ Nu am gasit meciul '{ECHIPA_1}' vs '{ECHIPA_2}'.")
    print("    Posibil sa se fi jucat deja (API-ul listeaza in general meciuri viitoare)")
    print("    sau numele echipelor difera. Incearca alte fragmente in ECHIPA_1/ECHIPA_2.")
    sys.exit(0)

print(f"  ✓ Gasit in: {tournament_gasit.get('tournamentName')} "
      f"({tournament_gasit.get('categoryName')})")
print(f"    fixtureId: {meci.get('fixtureId')}")
print(f"    start: {meci.get('startTime')}")
print(f"    echipe: {nume_dupa_id.get(meci.get('participant1Id'), '?')} vs "
      f"{nume_dupa_id.get(meci.get('participant2Id'), '?')}")

# --- 3. Piete: aflam ce ID are piata GG --------------------------------
print("\n[3] Identific piata GG (Both Teams To Score)")
mk, err = get("/markets", {"sportId": SPORT_FOOTBALL})
gg_market_ids = {}
if not err:
    for m in as_list(mk):
        if not isinstance(m, dict):
            continue
        nume = str(m.get("marketName") or m.get("name") or "").lower()
        if any(k in nume for k in CUVINTE_GG):
            mid = m.get("marketId") or m.get("id")
            gg_market_ids[str(mid)] = m.get("marketName") or m.get("name")
    for mid, nm in gg_market_ids.items():
        print(f"  ★ market {mid}: {nm}")
if not gg_market_ids:
    print("  ! Nu am identificat piata GG din /markets — voi afisa toate pietele.")

# --- 4. Cotele -----------------------------------------------------------
print("\n[4] Cotele pentru acest meci")
params = {"tournamentIds": tournament_gasit.get("tournamentId"), "oddsFormat": "decimal"}
odds, err = get("/odds-by-tournaments", params)
if err:
    sys.exit(f"  ✗ {err}")

fixture_odds = next((f for f in as_list(odds)
                     if f.get("fixtureId") == meci.get("fixtureId")), None)
if not fixture_odds:
    sys.exit("  ✗ Nu exista cote publicate pentru acest meci.")

for bk_name, bk in (fixture_odds.get("bookmakerOdds") or {}).items():
    e_cautata = any(c in str(bk_name).lower() for c in CASE_CAUTATE)
    marcaj = "  ★ CASA CAUTATA" if e_cautata else ""
    markets = bk.get("markets") or {}
    print(f"\n  CASA: {bk_name}{marcaj}   (piete: {len(markets)}, activa: {bk.get('bookmakerIsActive')})")

    for mid, m in markets.items():
        if gg_market_ids and str(mid) not in gg_market_ids:
            continue
        eticheta = gg_market_ids.get(str(mid), f"market {mid}")
        print(f"    {eticheta}:")
        for oid, o in (m.get("outcomes") or {}).items():
            for pl in (o.get("players") or {}).values():
                print(f"      outcome {oid} ({pl.get('bookmakerOutcomeId')}): "
                      f"cota {pl.get('price')}  |  actualizat: {pl.get('changedAt')}")
    if bk.get("fixturePath"):
        print(f"    link direct: {bk['fixturePath'][:100]}")

print("\n" + "=" * 74)
print("  COMPARATIE:")
print("   BetMind (API-Football, instantaneu 12:42 UTC) a afisat: GG Da = 1.77")
print("   Site Superbet (verificat manual, mai tarziu):            GG Da = 1.73")
print("   Uita-te mai sus la cota si la 'actualizat' de la OddsPapi.")
print("=" * 74)
