"""
Bucla agentului: Claude + tool use, cu streaming.

Un singur agent orchestrator care primeste mesajul userului, decide singur ce
tool-uri sa apeleze (fixtures, statistici, cote...), executa, analizeaza si
raspunde. Genereaza evenimente pentru SSE:
  {"type": "delta",  "text": "..."}      - bucata de text de afisat
  {"type": "status", "label": "..."}     - ce face agentul acum (tool call)
  {"type": "done"}                        - tura s-a terminat
  {"type": "error",  "message": "..."}   - eroare afisabila userului
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from collections import defaultdict
from typing import Any, AsyncGenerator, Optional

from anthropic import AsyncAnthropic, APIStatusError

log = logging.getLogger("betmind.agent")

import analysts
import db
import football_data as fd
from prompts import build_system_prompt
from ticket_builder import build_ticket

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
MAX_TOKENS = int(os.environ.get("MAX_TOKENS", "4096"))
MAX_AGENT_ITERATIONS = 25

# ---------------------------------------------------------------------------
# Definitiile tool-urilor (JSON Schema pentru Claude)
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "name": "get_fixtures",
        "description": ("Lista meciurilor dintr-un interval de zile (max 7), optional filtrata pe ligi. "
                        "Returneaza fixture_id, echipe cu id-uri, liga, sezon, ora. "
                        "Fara league_ids foloseste ligile implicite de top."),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD, optional (implicit = date_from)"},
                "league_ids": {"type": "array", "items": {"type": "integer"},
                               "description": "ID-uri de liga API-Football, optional"},
            },
            "required": ["date_from"],
        },
    },
    {
        "name": "get_fixture_changes",
        "description": ("Meciuri amanate sau reprogramate recent, detectate de sincronizarea locala: "
                        "schimbari de status (ex. NS→PST = amanat) sau de ora de start (reprogramat). "
                        "Util cand userul intreaba de amanari sau inainte de a recomanda un meci dubios."),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "YYYY-MM-DD, implicit acum 3 zile"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD, implicit azi"},
            },
            "required": [],
        },
    },
    {
        "name": "track_league",
        "description": ("Adauga o competitie in lista de ligi urmarite (sincronizate local). "
                        "Foloseste cand userul cere o competitie care nu e in lista urmarita "
                        "(ex. DFB Pokal, Cupa Romaniei, Supercupa). Accepta nume de cautat sau "
                        "league_id exact (ca string). Nu ghici niciodata ID-uri de liga."),
        "input_schema": {
            "type": "object",
            "properties": {
                "search_or_id": {"type": "string",
                                 "description": "nume competitie (ex. 'DFB Pokal') sau league_id exact"},
            },
            "required": ["search_or_id"],
        },
    },
    {
        "name": "list_leagues",
        "description": "Cauta o liga dupa nume sau tara ca sa-i afli league_id si sezonul curent.",
        "input_schema": {
            "type": "object",
            "properties": {"search": {"type": "string", "description": "ex: 'Superliga', 'Turkey'"}},
            "required": ["search"],
        },
    },
    {
        "name": "get_team_last_matches",
        "description": "Ultimele N meciuri ale unei echipe: adversar, scor, acasa/deplasare, rezultat W/D/L, ambele au marcat.",
        "input_schema": {
            "type": "object",
            "properties": {
                "team_id": {"type": "integer"},
                "count": {"type": "integer", "description": "1-10, implicit 6"},
            },
            "required": ["team_id"],
        },
    },
    {
        "name": "get_team_statistics",
        "description": "Statistici de sezon pentru o echipa intr-o liga: forma, victorii/egaluri, medii de goluri acasa/deplasare, clean sheets.",
        "input_schema": {
            "type": "object",
            "properties": {
                "team_id": {"type": "integer"},
                "league_id": {"type": "integer"},
                "season": {"type": "integer", "description": "anul in care a inceput sezonul, ex 2025"},
            },
            "required": ["team_id", "league_id", "season"],
        },
    },
    {
        "name": "get_h2h",
        "description": "Istoricul direct (head-to-head) dintre doua echipe.",
        "input_schema": {
            "type": "object",
            "properties": {
                "team1_id": {"type": "integer"},
                "team2_id": {"type": "integer"},
                "last": {"type": "integer", "description": "cate meciuri, implicit 6"},
            },
            "required": ["team1_id", "team2_id"],
        },
    },
    {
        "name": "get_injuries",
        "description": "Accidentari si suspendari recente (~30 zile) pentru o echipa.",
        "input_schema": {
            "type": "object",
            "properties": {
                "team_id": {"type": "integer"},
                "season": {"type": "integer"},
            },
            "required": ["team_id", "season"],
        },
    },
    {
        "name": "get_standings",
        "description": "Clasamentul unei ligi: pozitie, puncte, golaveraj, forma.",
        "input_schema": {
            "type": "object",
            "properties": {
                "league_id": {"type": "integer"},
                "season": {"type": "integer"},
            },
            "required": ["league_id", "season"],
        },
    },
    {
        "name": "get_odds",
        "description": ("Cotele pre-match pentru un meci, agregate pe toate casele: "
                        "1X2, sansa dubla, over/under, GG, handicap asiatic, totaluri de echipa, "
                        "piete de pauza etc. Foloseste avg_odd / best_odd / n_books; "
                        "cheile legacy 1X2/over_under/btts/double_chance raman."),
        "input_schema": {
            "type": "object",
            "properties": {"fixture_id": {"type": "integer"}},
            "required": ["fixture_id"],
        },
    },
    {
        "name": "build_ticket",
        "description": ("Construieste deterministic biletul optim din candidatii dati, ca sa atinga cota tinta "
                        "cu probabilitate maxima. Trimite TOTI candidatii promitatori (poti trimite mai multi decat "
                        "vor intra in bilet); functia alege combinatia. Max o selectie per meci."),
        "input_schema": {
            "type": "object",
            "properties": {
                "candidates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "fixture_id": {"type": "integer"},
                            "match": {"type": "string", "description": "ex: 'Roma vs Bayern'"},
                            "market": {"type": "string", "description": "ex: '1X2', 'Over 2.5', 'GG'"},
                            "pick": {"type": "string", "description": "ex: '1', 'Over 2.5', 'Yes'"},
                            "odds": {"type": "number"},
                            "prob": {"type": "number", "description": "probabilitatea ta estimata, 0-1"},
                            "kickoff": {"type": "string", "description": "ISO datetime, optional"},
                            "league": {"type": "string", "description": "numele ligii, optional"},
                            "reason": {"type": "string", "description": "motivatia scurta, optional"},
                            "confidence": {"type": "string", "enum": ["high", "medium", "low"],
                                           "description": "increderea analizei pentru acest meci, optional"},
                            "edge": {"type": "number",
                                     "description": "prob − implied_prob (din avg_odd), optional"},
                            "implied_prob": {"type": "number"},
                            "avg_odds": {"type": "number"},
                            "best_bookmaker": {"type": "string"},
                            "best_odd": {"type": "number"},
                        },
                        "required": ["fixture_id", "match", "market", "pick", "odds", "prob"],
                    },
                },
                "target_odds": {"type": "number"},
                "max_selections": {"type": "integer", "description": "implicit 15"},
                "excluded_fixture_ids": {
                    "type": "array", "items": {"type": "integer"},
                    "description": ("fixture_id-uri EXCLUSE. Completeaza-l DOAR cand userul "
                                    "cere explicit scoaterea unui meci ('scoate X'). O intrebare "
                                    "despre o selectie ('de ce X?') NU e cerere de scoatere."),
                },
                "risk_level": {"type": "string", "enum": ["sigur", "mediu", "riscant"],
                               "description": "nivelul de risc cerut/asumat, optional"},
            },
            "required": ["candidates", "target_odds"],
        },
    },
    {
        "name": "get_my_tickets",
        "description": ("Biletele recomandate anterior ACESTUI utilizator (salvate automat la "
                        "fiecare bilet prezentat): data, cota tinta/totala, risc, status si "
                        "selectiile cu meciuri, piete si cote. Foloseste-l OBLIGATORIU cand "
                        "userul intreaba ce bilete i-ai dat / biletul de ieri — raspunde doar "
                        "din datele reale returnate."),
        "input_schema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer",
                         "description": "cate zile in urma sa caute, implicit 7 (max 60)"},
            },
            "required": [],
        },
    },
]

# V1-B: tool-ul de orchestrare — inregistrat DOAR cu ORCHESTRATION_MODE=analysts.
ANALYZE_MATCHES_TOOL: dict = {
    "name": "analyze_matches",
    "description": ("Analizeaza in paralel un lot de meciuri (un agent analist per meci): "
                    "fiecare intoarce probabilitati pe piete, 2-4 selectii candidate cu cote reale, "
                    "factori concreti, un unghi non-evident si lipsurile de date. "
                    "Foloseste-l dupa shortlist (12-15 meciuri upcoming) si construieste apoi "
                    "biletul din best_candidates cu build_ticket. Rezultatele recente sunt "
                    "refolosite automat (cache), deci e ieftin sa-l rechemi pentru follow-up-uri."),
    "input_schema": {
        "type": "object",
        "properties": {
            "fixture_ids": {"type": "array", "items": {"type": "integer"},
                            "description": "fixture_id-urile meciurilor de analizat"},
            "max_matches": {"type": "integer", "description": "plafon, implicit 15"},
        },
        "required": ["fixture_ids"],
    },
}


def build_tools(mode: str | None = None) -> list[dict]:
    """Tool-urile inregistrate pentru tura curenta.

    `mode` suprascrie ORCHESTRATION_MODE pentru o singura cerere (comutatorul
    Advanced Mode din interfata); fara el se foloseste valoarea din .env.
    """
    tools = list(TOOLS)
    if (mode or analysts.orchestration_mode()) == "analysts":
        tools.append(ANALYZE_MATCHES_TOOL)
    return tools


# Etichete de rezerva (fara argumente). Statusul real se construieste in
# status_label() cu numele meciurilor/echipelor si ce date se preiau.
STATUS_LABELS = {
    "get_fixtures": "Caut programul de meciuri din perioada cerută…",
    "get_fixture_changes": "Verific amânările și reprogramările…",
    "track_league": "Adaug competiția în lista urmărită…",
    "list_leagues": "Caut competiția…",
    "get_team_last_matches": "Mă uit la ultimele meciuri — rezultate și goluri…",
    "get_team_statistics": "Iau statisticile de sezon: goluri, formă acasă/deplasare…",
    "get_h2h": "Compar întâlnirile directe dintre cele două echipe…",
    "get_injuries": "Verific cine lipsește: accidentări și suspendări…",
    "get_standings": "Consult clasamentul — loc, puncte, formă recentă…",
    "get_odds": "Citesc cotele: rezultat, goluri, ambele marchează, șansă dublă…",
    "build_ticket": "Compun biletul din variantele strânse — mix de piețe și cota țintă…",
    "get_my_tickets": "Îți scot biletele recomandate recent…",
    "analyze_matches": "Analizez meciurile: formă, cote, accidentări și H2H…",
}

_MONTHS_RO = ("", "ian", "feb", "mar", "apr", "mai", "iun",
              "iul", "aug", "sep", "oct", "nov", "dec")


def _human_day(value: Any) -> str:
    s = str(value or "")[:10]
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        try:
            return f"{int(s[8:10])} {_MONTHS_RO[int(s[5:7])]}"
        except (ValueError, IndexError):
            return s
    return s


def _join_ro(parts: list[str], limit: int = 4) -> str:
    clean = [p for p in parts if p]
    extra = len(clean) - limit
    shown = clean[:limit]
    if not shown:
        return ""
    if extra > 0:
        shown.append(f"încă {extra}")
    if len(shown) == 1:
        return shown[0]
    return ", ".join(shown[:-1]) + " și " + shown[-1]


async def _match_label(fixture_id: Any) -> str:
    try:
        fx = await db.get_fixture(int(fixture_id))
    except (TypeError, ValueError):
        return ""
    if not fx:
        return ""
    return f"{fx['home_name']} – {fx['away_name']}"


async def _team_label(team_id: Any) -> str:
    try:
        return await db.team_display_name(int(team_id)) or ""
    except (TypeError, ValueError):
        return ""


async def _league_label(league_id: Any) -> str:
    try:
        lid = int(league_id)
    except (TypeError, ValueError):
        return ""
    name = await db.league_display_name(lid)
    if name:
        return name
    return fd.DEFAULT_LEAGUES.get(lid, "")


async def status_label(name: str, args: Optional[dict] = None,
                       index: int = 0, total: int = 0) -> str:
    """Textul din animația de status: ce date se preiau, pentru cine.

    Fara nume de API-uri sau de unelte. Argumentele sunt cele trimise de model.
    """
    args = args or {}
    progress = f" ({index}/{total})" if total > 1 and index else ""

    if name == "get_odds":
        match = await _match_label(args.get("fixture_id"))
        who = f" pentru {match}" if match else ""
        return (f"Citesc cotele{progress}{who}: rezultat, goluri, ambele marchează, "
                f"șansă dublă și handicap.")

    if name == "get_team_last_matches":
        team = await _team_label(args.get("team_id"))
        n = args.get("count") or 6
        who = f" ale lui {team}" if team else ""
        return f"Mă uit la ultimele {n} meciuri{who}{progress} — rezultate și goluri marcate/primite."

    if name == "get_team_statistics":
        team = await _team_label(args.get("team_id"))
        league = await _league_label(args.get("league_id"))
        who = f" ale lui {team}" if team else " de sezon"
        where = f" în {league}" if league else ""
        return f"Iau statisticile{who}{where}{progress}: goluri, formă acasă/deplasare."

    if name == "get_injuries":
        team = await _team_label(args.get("team_id"))
        who = f" la {team}" if team else ""
        return f"Verific cine lipsește{who}{progress}: accidentări și suspendări."

    if name == "get_h2h":
        a = await _team_label(args.get("team1_id") or args.get("team_id"))
        b = await _team_label(args.get("team2_id"))
        pair = f" {a} – {b}" if a and b else ""
        return f"Compar întâlnirile directe{pair}{progress}."

    if name == "get_standings":
        league = await _league_label(args.get("league_id"))
        where = f" din {league}" if league else ""
        return f"Consult clasamentul{where}{progress} — loc, puncte, formă recentă."

    if name == "get_fixtures":
        period = args.get("date") or ""
        if args.get("date_from") or args.get("date_to"):
            a, b = _human_day(args.get("date_from")), _human_day(args.get("date_to"))
            period = f"{a} – {b}" if a and b else (a or b)
        elif period:
            period = _human_day(period)
        league = await _league_label(args.get("league_id"))
        where = f" din {league}" if league else " din ligile urmărite"
        when = f" pe {period}" if period else ""
        return f"Caut programul de meciuri{when}{where}."

    if name == "get_fixture_changes":
        a, b = _human_day(args.get("date_from")), _human_day(args.get("date_to"))
        when = f" între {a} și {b}" if a and b else ""
        return f"Verific dacă s-a amânat sau s-a mutat vreun meci{when}."

    if name == "build_ticket":
        n = len(args.get("candidates") or [])
        target = args.get("target_odds")
        bits = []
        if n:
            bits.append(f"din {n} variante")
        if target:
            bits.append(f"țintă cotă {target:g}" if isinstance(target, (int, float))
                        else f"țintă cotă {target}")
        head = "Compun biletul" + ((" " + ", ".join(bits)) if bits else "")
        return head + " — aleg piețele cu cel mai bun raport și evit să se repete același tip de pariu."

    if name == "get_my_tickets":
        days = args.get("days") or 7
        return f"Îți scot biletele recomandate din ultimele {days} zile."

    if name == "list_leagues":
        q = (args.get("search") or "").strip()
        return f"Caut competiția „{q}”…" if q else "Caut competiția…"

    if name == "track_league":
        q = str(args.get("search_or_id") or args.get("search") or "").strip()
        return f"Adaug „{q}” la competițiile urmărite…" if q else STATUS_LABELS["track_league"]

    if name == "analyze_matches":
        ids = args.get("fixture_ids") or []
        names = []
        for fid in ids[:6]:
            names.append(await _match_label(fid))
        listed = _join_ro(names)
        n = len(ids) or 1
        who = f": {listed}" if listed else f" {n} meciuri"
        return f"Analizez{who} — formă, cote, accidentări și H2H."

    return STATUS_LABELS.get(name, "Lucrez…")


async def _batch_status(name: str, blocks: list) -> str:
    """O linie de deschidere cand modelul cere acelasi tip de date pentru mai multi."""
    if name == "get_odds":
        names = [await _match_label((b.input or {}).get("fixture_id")) for b in blocks]
        listed = _join_ro(names)
        extra = f": {listed}" if listed else ""
        return (f"Citesc cotele pentru {len(blocks)} meciuri{extra} "
                f"— rezultat, goluri, ambele marchează, șansă dublă.")
    if name == "get_team_last_matches":
        names = [await _team_label((b.input or {}).get("team_id")) for b in blocks]
        listed = _join_ro(names)
        extra = f" ale lui {listed}" if listed else ""
        return f"Mă uit la ultimele meciuri{extra} — rezultate și goluri."
    if name == "get_injuries":
        names = [await _team_label((b.input or {}).get("team_id")) for b in blocks]
        listed = _join_ro(names)
        extra = f" la {listed}" if listed else ""
        return f"Verific cine lipsește{extra}: accidentări și suspendări."
    if name == "get_team_statistics":
        names = [await _team_label((b.input or {}).get("team_id")) for b in blocks]
        listed = _join_ro(names)
        extra = f" pentru {listed}" if listed else ""
        return f"Iau statisticile de sezon{extra}: goluri și formă."
    return ""


async def _get_my_tickets(user_key: str | None, days: Any) -> dict:
    """Addition 11: biletele salvate ale utilizatorului curent, cu selectii
    si numele meciurilor din fixture store (join local, zero apeluri API)."""
    from datetime import timedelta

    try:
        days_n = max(1, min(int(days), 60)) if days is not None else 7
    except (TypeError, ValueError):
        days_n = 7
    if not user_key:
        return {"count": 0, "tickets": [],
                "note": "Nu am putut identifica utilizatorul — nu exista bilete de afisat."}
    cutoff = (fd.now_local() - timedelta(days=days_n)).isoformat(timespec="seconds")
    rows = await db.get_user_tickets(user_key, cutoff)
    tickets = []
    for t in rows:
        selections = []
        for s in t["selections"]:
            if s.get("home_name") and s.get("away_name"):
                match = f"{s['home_name']} – {s['away_name']}"
            else:
                match = f"meci #{s.get('fixture_id')}"
            selections.append({
                "match": match,
                "date": s.get("date_local"),
                "time": s.get("time_local"),
                "market": s.get("market"),
                "pick": s.get("pick"),
                "odds": s.get("odds"),
            })
        tickets.append({
            "ticket_id": t["id"],
            "created_at": t["created_at"],
            "target_odds": t["target_odds"],
            "total_odds": t["total_odds"],
            "risk_level": t["risk_level"],
            "status": t["status"],
            "selections": selections,
        })
    return {"count": len(tickets), "days": days_n, "tickets": tickets,
            "note": ("Acestea sunt TOATE biletele salvate in perioada ceruta. "
                     "Daca lista e goala, spune onest ca nu exista bilete salvate "
                     "in acest interval — nu inventa.")}


async def _execute_tool(name: str, args: dict[str, Any],
                        conversation_id: str | None = None,
                        user_key: str | None = None) -> Any:
    """Ruleaza tool-ul cerut. Erorile devin payload {'error': ...} pentru agent."""
    try:
        if name == "get_fixtures":
            return await fd.get_fixtures(args["date_from"], args.get("date_to"), args.get("league_ids"))
        if name == "get_fixture_changes":
            return await fd.get_fixture_changes(args.get("date_from"), args.get("date_to"))
        if name == "track_league":
            return await fd.track_league(args["search_or_id"])
        if name == "list_leagues":
            return await fd.list_leagues(args["search"])
        if name == "get_team_last_matches":
            return await fd.get_team_last_matches(args["team_id"], args.get("count", 6))
        if name == "get_team_statistics":
            return await fd.get_team_statistics(args["team_id"], args["league_id"], args["season"])
        if name == "get_h2h":
            return await fd.get_h2h(args["team1_id"], args["team2_id"], args.get("last", 6))
        if name == "get_injuries":
            return await fd.get_injuries(args["team_id"], args["season"])
        if name == "get_standings":
            return await fd.get_standings(args["league_id"], args["season"])
        if name == "get_odds":
            return await fd.get_odds(args["fixture_id"])
        if name == "build_ticket":
            result = build_ticket(args["candidates"], args["target_odds"],
                                  args.get("max_selections", 15),
                                  args.get("excluded_fixture_ids"))
            # V1-C: fiecare bilet prezentat se persista (alimenteaza track
            # record-ul V2). ticket_id e referinta interna — promptul interzice
            # mentionarea stocarii catre user.
            if result.get("ok") and result.get("selections"):
                try:
                    result["ticket_id"] = await db.save_ticket(
                        conversation_id, result, args.get("risk_level"),
                        fd.now_local().isoformat(timespec="seconds"),
                    )
                except Exception:
                    log.exception("Nu am putut persista biletul")
            return result
        if name == "get_my_tickets":
            return await _get_my_tickets(user_key, args.get("days"))
        return {"error": f"Tool necunoscut: {name}"}
    except fd.FootballDataError as e:
        return {"error": str(e)}
    except Exception as e:  # nu lasam o exceptie sa omoare tura
        return {"error": f"Eroare la executarea tool-ului {name}: {type(e).__name__}: {e}"}


def _api_error_body(e: APIStatusError) -> str:
    """Extrage corpul erorii Anthropic pentru logging (fara chei sensibile)."""
    try:
        if e.body and isinstance(e.body, dict):
            err = e.body.get("error", e.body)
            if isinstance(err, dict):
                parts = [err.get("type"), err.get("message")]
                return " — ".join(p for p in parts if p) or json.dumps(err, ensure_ascii=False)
            return str(err)
        return getattr(e.response, "text", "") or ""
    except Exception:
        return ""


def _user_facing_api_error(e: APIStatusError) -> str:
    detail = _api_error_body(e)
    detail_lower = detail.lower()
    if e.status_code == 401:
        return "Cheia ANTHROPIC_API_KEY este invalidă. Verifică valoarea din .env."
    if e.status_code == 404:
        hint = (
            f"Modelul «{MODEL}» nu a fost găsit la Anthropic. "
            "Verifică CLAUDE_MODEL din .env (ex: claude-sonnet-4-20250514, claude-3-5-sonnet-20241022)."
        )
        return f"{hint} Detaliu API: {detail}" if detail else hint
    if e.status_code == 429:
        return "Am atins limita de rate a Claude API. Așteaptă puțin și încearcă din nou."
    if e.status_code == 400 and ("credit" in detail_lower or "billing" in detail_lower or "balance" in detail_lower):
        return (
            "Contul Anthropic nu are credit suficient. "
            "Adaugă fonduri pe https://console.anthropic.com/settings/billing și încearcă din nou."
        )
    base = f"Claude API a răspuns cu o eroare ({e.status_code})."
    return f"{base} {detail}" if detail else base


def _serializable_content(message) -> list[dict]:
    """Blocurile de continut ale asistentului, in forma JSON-safe pentru istoric."""
    blocks: list[dict] = []
    for block in message.content:
        if block.type == "text":
            blocks.append({"type": "text", "text": block.text})
        elif block.type == "tool_use":
            blocks.append({"type": "tool_use", "id": block.id, "name": block.name, "input": block.input})
    return blocks


def _with_cache_markers(system_prompt: str, tools: list[dict],
                        messages: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """
    V1-C: prompt caching Anthropic (cache_control ephemeral) pe prefixul stabil:
    system prompt, definitiile de tool-uri si istoricul de mesaje pana la
    ultimul bloc. Urmatorul apel (iteratie sau tura noua) reciteste prefixul
    din cache in loc sa-l re-factureze integral. Istoricul original NU e mutat
    — markerii se pun pe copii.
    """
    system = [{"type": "text", "text": system_prompt,
               "cache_control": {"type": "ephemeral"}}]

    tools_marked = [dict(t) for t in tools]
    if tools_marked:
        tools_marked[-1] = {**tools_marked[-1], "cache_control": {"type": "ephemeral"}}

    msgs = list(messages)
    if msgs:
        last = dict(msgs[-1])
        content = last["content"]
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        else:
            content = [dict(b) for b in content]
        content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}
        last["content"] = content
        msgs[-1] = last
    return system, tools_marked, msgs


async def run_turn(messages: list[dict],
                   conversation_id: str | None = None,
                   user_key: str | None = None,
                   mode: str | None = None,
                   turn_id: str | None = None) -> AsyncGenerator[dict, None]:
    """
    Ruleaza o tura completa de agent peste istoricul `messages`
    (lista e mutata in-place cu ce produce agentul). Genereaza evenimente SSE.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        yield {"type": "error",
               "message": ("Lipsește ANTHROPIC_API_KEY din fișierul .env. "
                           "Creează o cheie pe https://console.anthropic.com/settings/keys "
                           "și repornește aplicația.")}
        return

    base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip() or "https://api.anthropic.com"
    log.info(
        "Anthropic call start model=%s max_tokens=%s messages=%d base_url=%s key_prefix=%s…",
        MODEL, MAX_TOKENS, len(messages), base_url, api_key[:12],
    )

    client = AsyncAnthropic(api_key=api_key)
    system_prompt = build_system_prompt(mode)
    tools = build_tools(mode)
    turn_id = turn_id or uuid.uuid4().hex

    try:
        for iteration in range(MAX_AGENT_ITERATIONS):
            log.debug("Anthropic stream iteration=%d", iteration + 1)
            system_c, tools_c, messages_c = _with_cache_markers(system_prompt, tools, messages)
            async with client.messages.stream(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system_c,
                tools=tools_c,
                messages=messages_c,
            ) as stream:
                async for event in stream:
                    if event.type == "text":
                        yield {"type": "delta", "text": event.text}
                final = await stream.get_final_message()

            log.info(
                "Anthropic response stop_reason=%s input_tokens=%s output_tokens=%s",
                final.stop_reason,
                final.usage.input_tokens if final.usage else "?",
                final.usage.output_tokens if final.usage else "?",
            )
            if final.usage:
                try:
                    await db.add_usage(
                        turn_id, MODEL, final.usage.input_tokens,
                        final.usage.output_tokens,
                        fd.now_local().isoformat(timespec="seconds"),
                        cache_read_tokens=getattr(final.usage, "cache_read_input_tokens", None),
                        cache_write_tokens=getattr(final.usage, "cache_creation_input_tokens", None),
                    )
                except Exception:
                    log.exception("Nu am putut scrie usage_log (coordinator)")

            messages.append({"role": "assistant", "content": _serializable_content(final)})

            if final.stop_reason != "tool_use":
                yield {"type": "done"}
                return

            tool_results = []
            tool_blocks = [b for b in final.content if b.type == "tool_use"]
            batch_of: dict[str, list] = defaultdict(list)
            for b in tool_blocks:
                batch_of[b.name].append(b)
            announced: set[str] = set()

            for block in tool_blocks:
                log.info("Tool call: %s args=%s", block.name, json.dumps(block.input or {}, ensure_ascii=False)[:500])

                if block.name == "analyze_matches":
                    # Batch de analisti in paralel: emitem progres granular pe SSE
                    # (UX: niciodata un apel lung si mut).
                    args = block.input or {}
                    ids = args.get("fixture_ids") or []
                    max_m = args.get("max_matches", 15)
                    n = min(len(ids), max(1, max_m)) or 1
                    names = _join_ro([await _match_label(fid) for fid in ids[:n]])
                    who = f": {names}" if names else ""
                    yield {"type": "status",
                           "label": f"Analizez {n} meciuri în paralel{who} — formă, cote, accidentări și H2H."}
                    result: Any = {"error": "analyze_matches nu a produs rezultat."}
                    try:
                        async for ev in analysts.analyze_matches_events(ids, max_m, turn_id):
                            if ev[0] == "progress":
                                done_n, total_n = ev[1], ev[2]
                                just = ev[3] if len(ev) > 3 else ""
                                tail = f" — gata {just}" if just else ""
                                yield {"type": "status",
                                       "label": (f"Analizez {total_n} meciuri în paralel"
                                                 f"{tail}… ({done_n}/{total_n} gata)")}
                            else:
                                result = ev[1]
                    except Exception as e:
                        log.exception("analyze_matches a esuat")
                        result = {"error": f"Analiza in paralel a esuat: {type(e).__name__}: {e}"}
                else:
                    siblings = batch_of.get(block.name) or [block]
                    if len(siblings) > 1 and block.name not in announced:
                        announced.add(block.name)
                        intro = await _batch_status(block.name, siblings)
                        if intro:
                            yield {"type": "status", "label": intro}
                    idx = siblings.index(block) + 1 if len(siblings) > 1 else 0
                    yield {"type": "status",
                           "label": await status_label(block.name, block.input or {},
                                                       index=idx, total=len(siblings))}
                    result = await _execute_tool(block.name, block.input or {},
                                                 conversation_id, user_key)
                log.debug("Tool result %s: %s", block.name, json.dumps(result, ensure_ascii=False)[:500])
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                })
            messages.append({"role": "user", "content": tool_results})

        yield {"type": "error",
               "message": "Am atins limita de pași pentru această cerere. Încearcă o cerere mai simplă."}
    except APIStatusError as e:
        body = _api_error_body(e)
        request_id = e.response.headers.get("request-id", "?") if e.response else "?"
        log.error(
            "Anthropic APIStatusError status=%s request_id=%s model=%s body=%s",
            e.status_code, request_id, MODEL, body or e.body,
        )
        yield {"type": "error", "message": _user_facing_api_error(e)}
    except Exception as e:
        log.exception("Unexpected error in run_turn")
        yield {"type": "error", "message": f"Eroare neașteptată: {type(e).__name__}: {e}"}


async def ping_anthropic() -> dict:
    """Apel minimal la Anthropic — folosit de /api/debug/anthropic."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return {"ok": False, "error": "ANTHROPIC_API_KEY lipsește din .env"}

    client = AsyncAnthropic(api_key=api_key)
    try:
        msg = await client.messages.create(
            model=MODEL,
            max_tokens=16,
            messages=[{"role": "user", "content": "Reply with exactly: ok"}],
        )
        text = next((b.text for b in msg.content if b.type == "text"), "")
        return {
            "ok": True,
            "model": MODEL,
            "response": text,
            "input_tokens": msg.usage.input_tokens if msg.usage else None,
            "output_tokens": msg.usage.output_tokens if msg.usage else None,
        }
    except APIStatusError as e:
        return {
            "ok": False,
            "model": MODEL,
            "status_code": e.status_code,
            "error": _user_facing_api_error(e),
            "api_body": _api_error_body(e),
            "request_id": e.response.headers.get("request-id") if e.response else None,
        }
    except Exception as e:
        log.exception("ping_anthropic failed")
        return {"ok": False, "model": MODEL, "error": f"{type(e).__name__}: {e}"}
