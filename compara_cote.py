#!/usr/bin/env python3
"""
OddsPapi — versiune corectata:
  1. Filtrare STRICTA pe (tournamentName, categoryName) — nu doar pe numele
     ligii, care se repeta in multe tari (Premier League exista si in Feroe,
     Bundesliga si in Austria, Ligue 1 si in Algeria/Tunisia etc.)
  2. Respecta cooldown-ul documentat de 500ms intre apeluri catre /odds
     (rate limit e per-request, nu pe minut), cu retry automat pe 429.

Rulare:
  ODDSPAPI_KEY=... in .env
  python oddspapi_v3.py
"""

import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import httpx

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

API_KEY = os.environ.get("ODDSPAPI_KEY", "").strip() or "PUNE_CHEIA_AICI"
BASE = "https://api.oddspapi.io/v4"
SPORT_FOOTBALL = 10
CASA = "superbet.ro"

# Cooldown documentat pe /odds: 500ms. Punem 0.55s marja de siguranta.
ODDS_COOLDOWN_S = 0.55
_ultimul_apel_odds = 0.0

# Perechi STRICTE (tournamentName, categoryName) — ambele trebuie sa se
# potriveasca, altfel prindem ligi omonime din alte tari.
LIGI_TINTA = [
    ("Premier League", "England"),
    ("La Liga", "Spain"),
    ("LaLiga", "Spain"),          # unele feed-uri folosesc denumirea comerciala
    ("Serie A", "Italy"),
    ("Bundesliga", "Germany"),
    ("Ligue 1", "France"),
    ("Champions League", "International Clubs"),
    ("Europa League", "International Clubs"),
    ("Conference League", "International Clubs"),
    ("Superliga", "Romania"),      # NU "Superliga" simplu -> exista si in Danemarca
    ("Liga I", "Romania"),         # denumire alternativa posibila in feed
]

if API_KEY == "PUNE_CHEIA_AICI":
    sys.exit("Pune cheia in .env ca ODDSPAPI_KEY=...")


def get(path, params=None, respect_odds_cooldown=False):
    """GET generic. Daca respect_odds_cooldown=True, asteapta cooldown-ul
    de 500ms fata de ultimul apel catre /odds si reincearca o data pe 429."""
    global _ultimul_apel_odds

    if respect_odds_cooldown:
        asteapta = ODDS_COOLDOWN_S - (time.monotonic() - _ultimul_apel_odds)
        if asteapta > 0:
            time.sleep(asteapta)

    p = {"apiKey": API_KEY}
    p.update(params or {})
    r = httpx.get(f"{BASE}{path}", params=p, timeout=45)

    if respect_odds_cooldown:
        _ultimul_apel_odds = time.monotonic()

    if r.status_code == 429 and respect_odds_cooldown:
        # citim retryMs din corp si asteptam exact atat, plus o marja mica
        try:
            retry_ms = r.json().get("error", {}).get("retryMs", 600)
        except Exception:
            retry_ms = 600
        time.sleep((retry_ms / 1000.0) + 0.05)
        r = httpx.get(f"{BASE}{path}", params=p, timeout=45)
        _ultimul_apel_odds = time.monotonic()

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


def liga_e_tinta(tournament_name: str, category_name: str) -> bool:
    """Potrivire STRICTA pe perechea (turneu, tara) — case-insensitive,
    dar ambele campuri trebuie sa se potriveasca simultan."""
    tn = (tournament_name or "").strip().lower()
    cn = (category_name or "").strip().lower()
    for t_tinta, c_tinta in LIGI_TINTA:
        if tn == t_tinta.lower() and cn == c_tinta.lower():
            return True
    return False


print("=" * 78)
print("  OddsPapi v3 — filtrare stricta (turneu, tara) + cooldown 500ms")
print("=" * 78)

# --- 1. Toate fixture-urile cu cote, urmatoarele 5 zile -----------------
azi = date.today()
fx, err = get("/fixtures", {
    "sportId": SPORT_FOOTBALL,
    "from": azi.isoformat(),
    "to": (azi + timedelta(days=5)).isoformat(),
    "hasOdds": "true",
})
if err:
    sys.exit(f"✗ {err}")

fixtures = as_list(fx)
print(f"\n[1] Total meciuri cu cote (5 zile): {len(fixtures)}")

# --- 2. Diagnostic: ce categoryName reale exista pentru fiecare tournamentName
# tintit, ca sa vedem daca denumirile noastre se potrivesc exact -----------
print("\n[2] Verificare denumiri reale in feed (pentru fiecare turneu tintit):")
nume_turnee_vazute = {}
for f in fixtures:
    tn = f.get("tournamentName", "")
    cn = f.get("categoryName", "")
    key = tn.strip().lower()
    if any(key == t.lower() for t, _ in LIGI_TINTA):
        nume_turnee_vazute.setdefault(tn, set()).add(cn)

for t_tinta, c_tinta in LIGI_TINTA:
    gasite = None
    for tn, cats in nume_turnee_vazute.items():
        if tn.lower() == t_tinta.lower():
            gasite = cats
            break
    if gasite is None:
        print(f"  ✗ '{t_tinta}' — nu apare deloc in fereastra de 5 zile")
    elif t_tinta.lower() in [c.lower() for c in gasite] or c_tinta in gasite:
        print(f"  ✓ '{t_tinta}' / '{c_tinta}' — confirmat")
    else:
        print(f"  ! '{t_tinta}' — apare, dar cu alte categoryName: {sorted(gasite)}"
              f"  (asteptat: '{c_tinta}')")

# --- 3. Filtrare STRICTA pe perechea (turneu, tara) ----------------------
relevante = [f for f in fixtures
            if liga_e_tinta(f.get("tournamentName", ""), f.get("categoryName", ""))]
print(f"\n[3] Meciuri dupa filtrare stricta (turneu+tara): {len(relevante)}")

acum = datetime.now(timezone.utc)


def porneste_in_viitor(f):
    try:
        st = datetime.fromisoformat(str(f["startTime"]).replace("Z", "+00:00"))
        return st > acum
    except Exception:
        return False


viitoare = sorted((f for f in relevante if porneste_in_viitor(f)),
                  key=lambda f: f["startTime"])
print(f"    din care inca neincepute: {len(viitoare)}")

if not viitoare:
    sys.exit("Niciun meci viitor gasit in ligile tintite — verifica sectiunea [2] de mai sus.")

# --- 4. Cote de la superbet.ro, cu cooldown respectat --------------------
print(f"\n[4] Cote de la {CASA} pentru primele 6 meciuri viitoare (cu pauza {ODDS_COOLDOWN_S}s intre apeluri)")

MARKETS_INTERES = {
    "101": "Rezultat Final (1X2)",
    "1010": "Ambele echipe marcheaza",
}

esuate = 0
reusite = 0

for f in viitoare[:6]:
    p1, p2 = f.get("participant1Name", "?"), f.get("participant2Name", "?")
    liga, tara = f.get("tournamentName", "?"), f.get("categoryName", "?")
    fid = f.get("fixtureId")

    print("\n" + "-" * 78)
    print(f"MECI: {p1}  vs  {p2}   [{liga} — {tara}]")
    print(f"  start: {f.get('startTime')}   fixtureId: {fid}")

    odds, oerr = get("/odds",
                     {"fixtureId": fid, "bookmakers": CASA, "oddsFormat": "decimal", "verbosity": 3},
                     respect_odds_cooldown=True)
    if oerr:
        print(f"  ✗ {oerr}")
        esuate += 1
        continue

    bk_odds = (odds or {}).get("bookmakerOdds", {})
    if CASA not in bk_odds:
        print(f"  ✗ {CASA} nu are cote pentru acest meci")
        continue

    reusite += 1
    bk = bk_odds[CASA]
    print(f"  ★ {CASA} — activa: {bk.get('bookmakerIsActive')}")
    if bk.get("fixturePath"):
        print(f"    link: {bk['fixturePath']}")

    markets = bk.get("markets", {})
    for mid, eticheta in MARKETS_INTERES.items():
        m = markets.get(mid)
        if not m:
            continue
        linii = []
        ultima = None
        for oid, o in (m.get("outcomes") or {}).items():
            for pid, pl in (o.get("players") or {}).items():
                nume_out = pl.get("bookmakerOutcomeId") or oid
                linii.append(f"{nume_out}={pl.get('price')}")
                ultima = pl.get("changedAt") or ultima
        if linii:
            print(f"    {eticheta}: {'  '.join(linii)}   (actualizat: {ultima})")

print("\n" + "=" * 78)
print(f"  REZUMAT: {reusite} meciuri cu cote reusite, {esuate} esuate (rate limit sau alta eroare)")
print("  Zero meciuri din alte tari ar trebui sa apara mai sus.")
print("=" * 78)
