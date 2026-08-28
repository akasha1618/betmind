#!/usr/bin/env python3
"""
OddsPapi — versiune AI-READY, rezistenta la esecul /markets, testeaza 10
meciuri DIFERITE de azi.

DE CE EXISTA SCRIPTUL ASTA
---------------------------
Testarea manuala anterioara a scos la iveala o problema serioasa:
raspunsul brut de la /odds foloseste chei UUID opace pentru fiecare
"outcome" (varianta de pariu). Ghicitul sensului din marimea cotei a dus
la o eroare confirmata pe teren (Bayern-Stuttgart, GG Da/Nu inversate).

CE FACE SCRIPTUL ASTA
-----------------------
1. Incearca sa descarce /markets (cu cache local + 3 reincercari), ca sa
   stie sensul real al fiecarui outcome. NU se opreste daca esueaza —
   continua testul de cote, dar eticheteaza clar orice piata al carei
   sens nu a putut fi confirmat.
2. Selecteaza 10 meciuri DIFERITE care incep azi, din ligile tintite —
   nu repeta aceleasi 5-6 meciuri ca la rularile anterioare.
3. Pentru fiecare, verificare de sanitate: suma probabilitatilor implicite
   (1/cota) trebuie sa fie in [0.85, 1.20]. Daca nu, piata e marcata
   NEFOLOSITA, indiferent daca sensul era confirmat sau nu.
4. Fiecare linie de output e ori OK, ori NEFOLOSITA — niciodata ambiguu.

Rulare:
  ODDSPAPI_KEY=... in .env
  python oddspapi_v5.py
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

ODDS_COOLDOWN_S = 0.55
_ultimul_apel_odds = 0.0

LIGI_TINTA = [
    ("Premier League", "England"),
    ("LaLiga", "Spain"),
    ("Serie A", "Italy"),
    ("Bundesliga", "Germany"),
    ("Ligue 1", "France"),
    ("Superliga", "Romania"),
]

SUMA_MIN, SUMA_MAX = 0.85, 1.20
NR_MECIURI_DE_TESTAT = 10

if API_KEY == "PUNE_CHEIA_AICI":
    sys.exit("Pune cheia in .env ca ODDSPAPI_KEY=...")


def get(path, params=None, respect_odds_cooldown=False, incercari=1):
    global _ultimul_apel_odds
    ultima_eroare = None
    for incercare in range(1, incercari + 1):
        if respect_odds_cooldown:
            asteapta = ODDS_COOLDOWN_S - (time.monotonic() - _ultimul_apel_odds)
            if asteapta > 0:
                time.sleep(asteapta)
        p = {"apiKey": API_KEY}
        p.update(params or {})
        try:
            r = httpx.get(f"{BASE}{path}", params=p, timeout=45)
        except httpx.HTTPError as e:
            ultima_eroare = f"eroare retea: {e}"
            time.sleep(1.0 * incercare)
            continue
        if respect_odds_cooldown:
            _ultimul_apel_odds = time.monotonic()

        if r.status_code == 429 and respect_odds_cooldown:
            try:
                retry_ms = r.json().get("error", {}).get("retryMs", 600)
            except Exception:
                retry_ms = 600
            time.sleep((retry_ms / 1000.0) + 0.05)
            continue

        if r.status_code == 500:
            # eroare temporara de server — merita reincercat, nu abandonat
            ultima_eroare = f"HTTP 500: {r.text[:200]}"
            if incercare < incercari:
                time.sleep(1.5 * incercare)  # backoff crescator: 1.5s, 3s...
                continue
            return None, ultima_eroare

        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {r.text[:300]}"
        try:
            return r.json(), None
        except Exception as e:
            return None, f"raspuns non-JSON: {e}"
    return None, ultima_eroare or "esuat dupa reincercari"


def as_list(x):
    if isinstance(x, list):
        return x
    if isinstance(x, dict):
        for k in ("data", "results", "items"):
            if isinstance(x.get(k), list):
                return x[k]
    return []


def liga_e_tinta(tournament_name, category_name):
    tn, cn = (tournament_name or "").strip().lower(), (category_name or "").strip().lower()
    return any(tn == t.lower() and cn == c.lower() for t, c in LIGI_TINTA)


# =========================================================================
# PASUL A: sensul piețelor din /markets — CU RETRY si DEGRADARE ELEGANTA.
# Daca esueaza complet, scriptul CONTINUA (nu se opreste), dar orice piata
# va fi marcata explicit ca "sens neconfirmat" in loc sa fie ghicita.
# =========================================================================
print("=" * 78)
print("  PASUL A — sensul oficial al piețelor din /markets (cu retry)")
print("=" * 78)

CACHE_MARKETS = "oddspapi_markets_cache.json"
CACHE_MAX_VECHIME_ORE = 24 * 7

sens_piata = {}
markets_disponibile = False

mk_raw = None
if os.path.exists(CACHE_MARKETS):
    varsta_ore = (time.time() - os.path.getmtime(CACHE_MARKETS)) / 3600
    if varsta_ore < CACHE_MAX_VECHIME_ORE:
        with open(CACHE_MARKETS, encoding="utf-8") as fh:
            mk_raw = json.load(fh)
        print(f"  Citit din cache local (vechime {varsta_ore:.1f}h) — zero apel API")
        markets_disponibile = True

if mk_raw is None:
    print("  Cache lipsa/expirat — cer /markets (pana la 3 incercari, cu pauza intre ele)...")
    mk_raw, mk_err = get("/markets", {"sportId": SPORT_FOOTBALL}, incercari=3)
    if mk_err:
        print(f"  ✗ /markets indisponibil dupa 3 incercari: {mk_err}")
        if os.path.exists(CACHE_MARKETS):
            print("  -> Folosesc cache-ul vechi (mai bun decat nimic).")
            with open(CACHE_MARKETS, encoding="utf-8") as fh:
                mk_raw = json.load(fh)
            markets_disponibile = True
        else:
            print("  -> CONTINUAM FARA sensuri confirmate. Toate piețele vor fi")
            print("     marcate 'sens neconfirmat' — testul de cote tot ruleaza,")
            print("     doar ca nu poate afirma ce inseamna fiecare outcome.")
            mk_raw = []
    else:
        with open(CACHE_MARKETS, "w", encoding="utf-8") as fh:
            json.dump(mk_raw, fh, ensure_ascii=False)
        print("  Salvat in cache local pentru rularile viitoare")
        markets_disponibile = True

if markets_disponibile:
    for m in as_list(mk_raw):
        if not isinstance(m, dict):
            continue
        mid = str(m.get("marketId") or m.get("id") or "")
        nume_piata = m.get("marketName") or m.get("name") or f"market {mid}"
        outcomes_doc = m.get("outcomes") or m.get("selections") or []
        mapare = {}
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
        sens_piata[mid] = {"nume": nume_piata, "outcomes": mapare}

    piete_cu_sens = sum(1 for v in sens_piata.values() if v["outcomes"])
    print(f"  Piete documentate: {len(sens_piata)}  |  cu mapare completa: {piete_cu_sens}")
    if len(sens_piata) and piete_cu_sens == 0 and as_list(mk_raw):
        print("  ⚠ structura /markets nu se potriveste cu parsarea asteptata. Exemplu brut:")
        print("    " + json.dumps(as_list(mk_raw)[0], ensure_ascii=False)[:500])


def eticheta_outcome(market_id, outcome_key):
    """None daca sensul nu poate fi confirmat — NU se ghiceste in locul lui."""
    info = sens_piata.get(str(market_id))
    if not info:
        return None
    return info["outcomes"].get(str(outcome_key))


# =========================================================================
# PASUL B: 10 meciuri DIFERITE care incep AZI, din ligile tintite.
# =========================================================================
print("\n" + "=" * 78)
print(f"  PASUL B — pana la {NR_MECIURI_DE_TESTAT} meciuri diferite, azi/maine")
print("=" * 78)

azi = date.today()
maine = azi + timedelta(days=1)
fx, err = get("/fixtures", {
    "sportId": SPORT_FOOTBALL,
    "from": azi.isoformat(),
    "to": maine.isoformat(),        # azi + maine, ca sa nu picam in 404 daca
                                    # meciurile de azi s-au jucat deja pana
                                    # la ora la care rulam scriptul
    "hasOdds": "true",
}, incercari=2)
if err:
    sys.exit(f"✗ {err}")

fixtures = as_list(fx)
relevante = [f for f in fixtures
            if liga_e_tinta(f.get("tournamentName", ""), f.get("categoryName", ""))]
acum = datetime.now(timezone.utc)


def porneste_in_viitor(f):
    try:
        return datetime.fromisoformat(str(f["startTime"]).replace("Z", "+00:00")) > acum
    except Exception:
        return False


viitoare = sorted((f for f in relevante if porneste_in_viitor(f)), key=lambda f: f["startTime"])

# Deduplicam pe perechea de echipe, ca sa nu testam accidental acelasi meci
# de doua ori (poate aparea cu markets diferite in feed).
vazute, unice = set(), []
for f in viitoare:
    cheie = (f.get("participant1Name"), f.get("participant2Name"), f.get("startTime"))
    if cheie not in vazute:
        vazute.add(cheie)
        unice.append(f)

print(f"  Meciuri azi/maine in ligile tintite: {len(unice)} (din {len(fixtures)} total in feed)")
de_testat = unique_slice = unice[:NR_MECIURI_DE_TESTAT]
if len(de_testat) < NR_MECIURI_DE_TESTAT:
    print(f"  Nota: doar {len(de_testat)} meciuri disponibile azi in ligile tintite"
          f" (nu {NR_MECIURI_DE_TESTAT}) — e ok, testam ce exista.")

# =========================================================================
# PASUL C: cote de la Superbet RO, cu SENS + VERIFICARE DE SANITATE
# =========================================================================
print("\n" + "=" * 78)
print(f"  PASUL C — cote {CASA} pentru {len(de_testat)} meciuri")
print("  Fiecare piata: OK (sens confirmat + suma normala) sau NEFOLOSITA")
print("=" * 78)

ok_total, nefolosite_total = 0, 0

for f in de_testat:
    p1, p2 = f.get("participant1Name", "?"), f.get("participant2Name", "?")
    liga, tara = f.get("tournamentName", "?"), f.get("categoryName", "?")
    fid = f.get("fixtureId")

    print("\n" + "-" * 78)
    print(f"MECI: {p1}  vs  {p2}   [{liga} — {tara}]   fixtureId={fid}")

    odds, oerr = get("/odds",
                     {"fixtureId": fid, "bookmakers": CASA, "oddsFormat": "decimal", "verbosity": 3},
                     respect_odds_cooldown=True, incercari=2)
    if oerr:
        print(f"  eroare API: {oerr}")
        continue

    bk_odds = (odds or {}).get("bookmakerOdds", {})
    if CASA not in bk_odds:
        print(f"  {CASA} nu are cote pentru acest meci")
        continue

    bk = bk_odds[CASA]
    markets = bk.get("markets", {})

    for mid, m in markets.items():
        info_piata = sens_piata.get(str(mid))
        nume_piata = info_piata["nume"] if info_piata else f"market necunoscut {mid}"

        randuri = []
        for oid, o in (m.get("outcomes") or {}).items():
            for pid, pl in (o.get("players") or {}).items():
                cota = pl.get("price")
                eticheta = eticheta_outcome(mid, oid)
                randuri.append((eticheta, cota, oid))

        if not randuri:
            continue

        cote_valide = [c for _, c, _ in randuri if isinstance(c, (int, float)) and c > 1.0]
        suma_prob = sum(1.0 / c for c in cote_valide) if cote_valide else 0.0
        suma_ok = SUMA_MIN <= suma_prob <= SUMA_MAX
        toate_etichetate = all(e is not None for e, _, _ in randuri)

        if toate_etichetate and suma_ok:
            status = "OK — sens confirmat, suma probabilitati normala"
            ok_total += 1
        elif not toate_etichetate:
            status = "NEFOLOSITA — sens neconfirmat pentru cel putin un outcome"
            nefolosite_total += 1
        else:
            status = f"NEFOLOSITA — suma probabilitati {suma_prob:.2f} anormala"
            nefolosite_total += 1

        print(f"\n    {nume_piata}  [{status}]")
        print(f"      suma probabilitati implicite: {suma_prob:.3f}  (normal: {SUMA_MIN}-{SUMA_MAX})")
        for eticheta, cota, oid in randuri:
            afisaj = eticheta if eticheta else f"SENS NECUNOSCUT (id brut: {oid})"
            print(f"      {afisaj:<40} cota={cota}")

print("\n" + "=" * 78)
print(f"  REZUMAT: {ok_total} piete OK, {nefolosite_total} NEFOLOSITE")
print("  De folosit in aplicatie DOAR piete marcate OK.")
print("=" * 78)
