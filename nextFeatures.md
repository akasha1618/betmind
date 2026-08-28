Feature: Verificare cote live pe superbet.ro pentru fiecare pick din bilet
Context

Biletul curent e generat din build_ticket cu date de la API-Football (cote posibil învechite/diferite de bookmaker real). Vrem să suprapunem cote live de la OddsPapi (bookmaker superbet) peste fiecare pick, cu link direct spre superbet.ro.

1. Matching fixture API-Football → OddsPapi

Cele două surse au ID-uri complet diferite (fixture_id numeric la API-Football vs fixtureId gen "id1000000872478460" la OddsPapi) — nu există un ID comun. Matching-ul trebuie făcut prin:

nume echipe (participant1/participant2 — normalizate: lowercase, fără diacritice, fără sufixe gen "FC"/"CF")
kickoff time (startTime OddsPapi vs kickoff API-Football), toleranță ±5 minute

Implementează o funcție match_oddspapi_fixture(af_fixture) -> oddspapi_fixture | None care caută în fixture-urile OddsPapi deja sincronizate local (din turneele relevante) o potrivire pe ambele criterii. Dacă nu găsește potrivire, pick-ul rămâne doar cu cotele API-Football (fără buton, fără rând în tabel) — nu bloca tot răspunsul.

2. Mapping piață → market OddsPapi

API-Football folosește piețe gen 1x2, over_under_2.5, btts. OddsPapi le denumește diferit (Full Time Result, Over Under Full Time, Both Teams To Score). Ai nevoie de un mapping explicit:

Pick din bilet	Market OddsPapi de căutat	Observație
1x2	Full Time Result	3 outcome-uri: home/draw/away
over_under_2.5	Over Under Full Time	ATENȚIE: sunt multiple linii (1.5, 2.5, 3.5 etc.) în același market name — trebuie filtrat după linia exactă (2.5), care de obicei apare în bookmakerMarketId sau trebuie dedusă din contextul liniei (verifică payload-ul real, linia nu e mereu explicită în market name — posibil ai nevoie de un query separat cu param de linie)
btts	Both Teams To Score	Yes/No

Scrie funcția asta configurabil, pentru că MVP-ul are și total goals ca piață separată de over 2.5 — verifică dacă se suprapun.

3. Extragere cotă + link

Din fixture-ul OddsPapi matched, extrage:

fixture.bookmakerOdds["superbet"].markets[market_id].outcomes[outcome_id].players[0].price
fixture.bookmakerOdds["superbet"].fixturePath   # ex: https://superbet.com/offer-event/e-13846971

Pentru link: înlocuiește domeniul din fixturePath din superbet.com → superbet.ro (nu modifica path-ul, e cross-domeniu identic — confirmat manual pe 4 meciuri). Fă asta cu un .replace("superbet.com", "superbet.ro") centralizat într-o funcție to_superbet_ro_link(path), ca să fie ușor de schimbat dacă superbet își modifică structura.

4. Output către frontend

Extinde structura de răspuns a ticket-ului (JSON trimis către UI) ca fiecare pick să aibă opțional:

json
{
  "fixture_id": ...,
  "market": "1x2",
  "pick": "Home",
  "odds": 1.28,              // API-Football (existent)
  "superbet_odds": 1.31,     // NOU — nullable, dacă matching a reușit
  "superbet_link": "https://superbet.ro/offer-event/e-13846971"  // NOU — nullable
}
5. UI (asta e treaba ta de design, dar cerințele funcționale):
Lângă fiecare pick din bilet: buton mic "Vezi cota pe superbet.ro" — vizibil DOAR dacă superbet_link există; deschide link în tab nou (target="_blank" rel="noopener").
Sub mesajul cu biletul: tabel separat cu titlu gen "Bilet actualizat cu cote superbet.ro" — aceleași picks, dar cu superbet_odds în loc de odds, și cota totală a biletului recalculată ca produs al cotelor superbet (doar pentru pick-urile care au match; dacă vreun pick nu are match, marchează-l explicit "cotă indisponibilă" în tabel, nu ascunde rândul).
6. Caz de eroare / lipsă matching

Dacă niciun pick din bilet nu are matching OddsPapi (turneu neacoperit, ex. o ligă mai mică), tabelul cu cote superbet nu apare deloc — doar biletul original, fără să afișezi o eroare vizibilă userului.

Un lucru pe care nu-l pot confirma din log-urile pe care mi le-ai dat: cum arată exact filtrarea pe linia de goluri (2.5 vs 1.5 vs 3.5) în payload-ul OddsPapi — output-ul din compara_cote.py arată market name-ul repetat de multe ori fără linia vizibilă în text. Verifică în payload-ul JSON brut (nu în output-ul printat de scriptul tău) unde exact e specificată linia — probabil în bookmakerMarketId sau într-un câmp separat pe outcome — altfel riști să iei cota greșită de Over/Under.