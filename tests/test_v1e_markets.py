"""
Acceptanta V1-E — acoperire piete, convingere analitica, diversitate bilete.

(a) get_odds pe un payload cu 15 piete / 6 case → DC, AH, HT prezente;
    correct-score exclus; n_books>1; best_odd >= avg_odd; chei legacy intacte.
(b) validatorul respinge o cheie market_probs absenta din pachetul de cote.
(c) ranking: la cote si prob identice, edge+confidence mai mari intra primele;
    probabilitatea afisata = produsul RAW (boost-ul nu se scurge).
(d) diversitate: 5 aceeasi familie + 4 alta → cel mult max_same per familie,
    tinta tot atinsa.
(e) compat: cheile legacy se rezolva; testele vechi de bilet raman verzi.
"""

from __future__ import annotations

import math

import pytest

import analysts
import db
import football_data as fd
import prompts
import agent
from ticket_builder import build_ticket, market_family
from tests.test_v1b import _seed_fixture, _today


# ---------------------------------------------------------------------------
# Payload API-Football: 15 piete, 6 case
# ---------------------------------------------------------------------------

_MARKETS_15 = [
    ("Match Winner", [
        ("Home", 1.80), ("Draw", 3.60), ("Away", 4.20)]),
    ("Double Chance", [
        ("Home/Draw", 1.25), ("Draw/Away", 1.90), ("Home/Away", 1.28)]),
    ("Goals Over/Under", [
        ("Over 1.5", 1.28), ("Under 1.5", 3.60),
        ("Over 2.5", 1.85), ("Under 2.5", 1.95),
        ("Over 3.5", 3.10), ("Under 3.5", 1.35)]),
    ("Both Teams Score", [("Yes", 1.80), ("No", 1.95)]),
    ("Asian Handicap", [
        ("Home -0.5", 1.90), ("Away +0.5", 1.90)]),
    ("Home Team Total Goals", [
        ("Over 1.5", 1.70), ("Under 1.5", 2.10)]),
    ("Away Team Total Goals", [
        ("Over 0.5", 1.45), ("Under 0.5", 2.70)]),
    ("Home Team Score a Goal", [("Yes", 1.15), ("No", 5.50)]),
    ("First Half Winner", [
        ("Home", 2.40), ("Draw", 2.20), ("Away", 3.80)]),
    ("Goals Over/Under First Half", [
        ("Over 0.5", 1.40), ("Under 0.5", 2.80),
        ("Over 1.5", 2.60), ("Under 1.5", 1.48)]),
    ("Both Teams Score - First Half", [("Yes", 3.40), ("No", 1.30)]),
    ("First Team To Score", [("Home", 1.70), ("Away", 2.20), ("None", 9.00)]),
    ("HT/FT Double", [("Home/Home", 3.10), ("Draw/Draw", 5.50)]),
    ("Result/Total Goals", [("Home/Over 2.5", 3.20), ("Home/Under 2.5", 4.10)]),
    ("Exact Score", [("1-0", 7.50), ("2-1", 8.00), ("1-1", 6.50)]),  # deny
]


def _book(bid: int, name: str, drift: float) -> dict:
    """O casa: aceleasi 15 piete, cote usor deplasate ca sa existe avg ≠ best."""
    bets = []
    for i, (mname, values) in enumerate(_MARKETS_15):
        bets.append({
            "id": i + 1,
            "name": mname,
            "values": [
                {"value": v, "odd": f"{odd * drift:.2f}"} for v, odd in values
            ],
        })
    return {"id": bid, "name": name, "bets": bets}


def _six_bookmakers() -> list[dict]:
    # Bet365 (id=8) e casa preferata / reference.
    return [
        _book(8, "Bet365", 1.00),
        _book(6, "Bwin", 1.03),
        _book(11, "1xBet", 1.06),
        _book(1, "10Bet", 0.98),
        _book(21, "Unibet", 1.08),
        _book(30, "William Hill", 1.02),
    ]


# ---------------------------------------------------------------------------
# (a) get_odds
# ---------------------------------------------------------------------------

async def test_get_odds_aggregates_all_books_and_keeps_legacy_keys(fake_http):
    await db.init_db()
    fake_http.response_payload = [{"bookmakers": _six_bookmakers()}]

    out = await fd.get_odds(9999)

    assert "error" not in out
    # Legacy — neschimbate ca forma, din casa preferata.
    assert out["bookmaker"] == "Bet365"
    assert set(out["1X2"]) >= {"Home", "Draw", "Away"}
    assert "Over 2.5" in out["over_under"]
    assert "Yes" in out["btts"]
    assert "Home/Draw" in out["double_chance"]

    keys = {m["key"] for m in out["markets"]}
    assert "double_chance" in keys
    assert "asian_handicap" in keys
    assert "1x2_ht" in keys or "over_under_ht" in keys
    assert "correct_score" not in keys
    assert "exact_score" not in keys
    assert not any("correct" in k or "exact_score" in k for k in keys)
    # Marcatorii si exact score nu intra, oricum ar fi slug-uite.
    assert all("goalscorer" not in (m.get("name") or "").lower()
               for m in out["markets"])

    # Agregare: mai multe case, best >= avg, reference de la Bet365.
    sample = next(m for m in out["markets"] if m["key"] == "1x2")
    home = next(o for o in sample["outcomes"] if o["value"] == "Home")
    assert home["n_books"] > 1
    assert home["best_odd"] >= home["avg_odd"]
    assert home["reference_odd"] is not None
    assert home["best_bookmaker"]

    assert out["truncated"] is False
    assert len(out["markets"]) <= 25
    # Un singur request HTTP — nu cerem cote per casa.
    assert len(fake_http.calls) == 1
    assert fake_http.calls[0][0] == "/odds"


def test_aggregate_odds_accepts_numeric_outcome_values():
    """API-Football trimite uneori 'Home' ca 1 sau handicapul ca -0.5 (int/float).
    Asta crăpa agregarea pe .strip() și modelul vedea 'eroare' deși HTTP era 200."""
    books = [
        {"id": 8, "name": "Bet365", "bets": [
            {"name": "Match Winner", "values": [
                {"value": 1, "odd": "1.70"},
                {"value": "Draw", "odd": "3.80"},
                {"value": 2, "odd": "5.00"},
            ]},
            {"name": "Asian Handicap", "values": [
                {"value": -0.5, "odd": "1.90"},
                {"value": 0.5, "odd": "1.90"},
            ]},
            {"name": "Exact Score", "values": [{"value": "1-0", "odd": "7.00"}]},
        ]},
        {"id": 21, "name": "Unibet", "bets": [
            {"name": "Match Winner", "values": [
                {"value": "Home", "odd": "1.75"},
                {"value": "Draw", "odd": "3.70"},
                {"value": "Away", "odd": "4.90"},
            ]},
            {"name": "Asian Handicap", "values": [
                {"value": "Home -0.5", "odd": "1.95"},
                {"value": "Away +0.5", "odd": "1.85"},
            ]},
        ]},
    ]
    out = fd.aggregate_odds(books)
    assert "error" not in out
    keys = {m["key"] for m in out["markets"]}
    assert "1x2" in keys and "asian_handicap" in keys
    assert "exact_score" not in keys
    home = next(o for m in out["markets"] if m["key"] == "1x2"
                for o in m["outcomes"] if o["value"] == "Home")
    assert home["n_books"] >= 1
    assert home["best_odd"] >= home["avg_odd"]


def test_normalize_market_name_mapping_and_deny_list():
    assert fd.normalize_market_name("Match Winner") == "1x2"
    assert fd.normalize_market_name("Full Time Result") == "1x2"
    assert fd.normalize_market_name("Goals Over/Under") == "over_under"
    assert fd.normalize_market_name("Both Teams Score") == "btts"
    assert fd.normalize_market_name("Double Chance") == "double_chance"
    assert fd.normalize_market_name("Asian Handicap") == "asian_handicap"
    assert fd.normalize_market_name("Home Team Total Goals") == "team_total_home"
    assert fd.normalize_market_name("Away Team Total Goals") == "team_total_away"
    assert fd.normalize_market_name("Home Team Score a Goal") == "team_scores_home"
    assert fd.normalize_market_name("HT/FT Double") == "htft"
    assert fd.normalize_market_name("First Half Winner") == "1x2_ht"
    assert fd.normalize_market_name("Goals Over/Under First Half") == "over_under_ht"
    assert fd.normalize_market_name("First Team To Score") == "first_to_score"
    assert fd.normalize_market_name("Both Teams Score - First Half") == "btts_ht"
    assert fd.normalize_market_name("Result/Total Goals") == "combo_result_goals"
    # Deny
    assert fd.normalize_market_name("Exact Score") is None
    assert fd.normalize_market_name("Correct Score") is None
    assert fd.normalize_market_name("Anytime Goal Scorer") is None
    # Necunoscut dar permis → slug, nu drop.
    assert fd.normalize_market_name("Odd/Even") == "odd_even"


# ---------------------------------------------------------------------------
# (b) validator market_probs
# ---------------------------------------------------------------------------

def _odds_pack_1x2_ou25() -> dict:
    return {
        "bookmaker": "Bet365",
        "1X2": {"Home": "1.80", "Draw": "3.50", "Away": "4.20"},
        "over_under": {"Over 2.5": "1.90", "Under 2.5": "1.90"},
        "markets": [
            {"key": "1x2", "name": "Match Winner", "outcomes": [
                {"value": "Home", "avg_odd": 1.82, "best_odd": 1.90,
                 "best_bookmaker": "Unibet", "n_books": 6, "reference_odd": 1.80},
                {"value": "Draw", "avg_odd": 3.50, "best_odd": 3.60,
                 "best_bookmaker": "Bwin", "n_books": 6, "reference_odd": 3.50},
                {"value": "Away", "avg_odd": 4.10, "best_odd": 4.30,
                 "best_bookmaker": "1xBet", "n_books": 6, "reference_odd": 4.20},
            ]},
            {"key": "over_under", "name": "Goals Over/Under", "outcomes": [
                {"value": "Over 2.5", "avg_odd": 1.88, "best_odd": 1.95,
                 "best_bookmaker": "Unibet", "n_books": 5, "reference_odd": 1.90},
                {"value": "Under 2.5", "avg_odd": 1.92, "best_odd": 2.00,
                 "best_bookmaker": "Bwin", "n_books": 5, "reference_odd": 1.90},
            ]},
        ],
        "truncated": False,
    }


def test_validator_rejects_prob_for_market_absent_from_odds_pack():
    odds = _odds_pack_1x2_ou25()
    allowed = analysts.allowed_prob_keys(odds)
    assert "home" in allowed and "over25" in allowed
    assert "over35" not in allowed
    assert "btts_yes" not in allowed

    analysts.validate_market_probs({"home": 0.5, "over25": 0.55}, odds)  # ok

    with pytest.raises(ValueError, match="over35"):
        analysts.validate_market_probs({"home": 0.5, "over35": 0.30}, odds)

    # Fara pachet de cote nu blocam — analiza poate continua onest.
    analysts.validate_market_probs({"home": 0.4}, None)
    analysts.validate_market_probs({"home": 0.4}, {"error": "fara cote"})


def test_enrich_candidates_computes_edge_from_avg_odd():
    analysis = {
        "confidence": "high",
        "best_candidates": [
            {"market": "1X2", "pick": "Home", "odds": 1.80, "prob": 0.62,
             "reason": "4W din 5 acasa"},
        ],
    }
    analysts.enrich_candidates(analysis, _odds_pack_1x2_ou25())
    c = analysis["best_candidates"][0]
    assert c["avg_odds"] == 1.82
    assert c["implied_prob"] == pytest.approx(1 / 1.82, abs=0.002)
    assert c["edge"] == pytest.approx(0.62 - 1 / 1.82, abs=0.002)
    assert c["best_bookmaker"] == "Unibet"
    assert c["confidence"] == "high"


# ---------------------------------------------------------------------------
# (c) ranking + onestitate
# ---------------------------------------------------------------------------

def test_higher_conviction_ranks_first_and_boost_does_not_leak_into_probability():
    shared = {"odds": 2.0, "prob": 0.50}
    high = {"fixture_id": 1, "match": "A vs B", "market": "Over 2.5",
            "pick": "Over 2.5", "edge": 0.12, "confidence": "high",
            "implied_prob": 0.38, **shared}
    bland = {"fixture_id": 2, "match": "C vs D", "market": "1X2",
             "pick": "1", "edge": 0.0, "confidence": "medium",
             "implied_prob": 0.50, **shared}

    # O singura selectie: intra cea cu edge + confidence.
    one = build_ticket([bland, high], target_odds=2.0)
    assert one["ok"] and one["selections_count"] == 1
    assert one["selections"][0]["fixture_id"] == 1
    assert one["estimated_probability"] == pytest.approx(0.50, abs=0.0001)

    # Ambele pe bilet: probabilitatea = 0.5 × 0.5, NU scorul umflat.
    both = build_ticket([bland, high], target_odds=3.5)
    assert {s["fixture_id"] for s in both["selections"]} == {1, 2}
    assert both["estimated_probability"] == pytest.approx(0.25, abs=0.0001)
    # Cotele raportate raman cele brute.
    assert both["total_odds"] == pytest.approx(4.0, abs=0.02)
    assert all(s["odds"] == 2.0 and s["prob"] == 0.5 for s in both["selections"])


# ---------------------------------------------------------------------------
# (d) diversitate
# ---------------------------------------------------------------------------

def test_diversity_caps_same_family_and_still_reaches_target():
    results = [
        {"fixture_id": 10 + i, "match": f"R{i} vs X", "market": "1X2", "pick": "1",
         "odds": 1.80, "prob": 0.60, "edge": 0.15, "confidence": "high",
         "league": "Serie A", "kickoff": f"2026-08-23T1{i}:00"}
        for i in range(5)
    ]
    goals = [
        {"fixture_id": 20 + i, "match": f"G{i} vs Y", "market": "Over 1.5",
         "pick": "Over 1.5", "odds": 1.80, "prob": 0.60, "edge": 0.0,
         "confidence": "medium", "league": "Ligue 1",
         "kickoff": f"2026-08-24T1{i}:00"}
        for i in range(4)
    ]
    # 1.80^4 ≈ 10.5; tinta 5 cere ~3-4 selectii. Fara constrangere, greedy
    # ar lua doar rezultate (edge mai mare).
    res = build_ticket(results + goals, target_odds=5.0, max_same_market_family=2)
    assert res["ok"] is True
    assert res["reached_target"] is True
    families = [market_family(s["market"], s["pick"]) for s in res["selections"]]
    from collections import Counter
    counts = Counter(families)
    assert all(n <= 2 for n in counts.values()), counts
    assert res["total_odds"] >= 5.0 * 0.999
    # Onestitate: produsul RAW.
    raw = math.exp(sum(math.log(s["prob"]) for s in res["selections"]))
    assert res["estimated_probability"] == pytest.approx(raw, abs=0.0001)


def test_market_family_mapping():
    assert market_family("1X2", "1") == "result"
    assert market_family("Over 2.5", "Over 2.5") == "goals"
    assert market_family("Over 1.5", "Over 1.5") == "goals"
    assert market_family("GG", "Yes") == "btts"
    assert market_family("Double Chance", "1X") == "double_chance"
    assert market_family("Asian Handicap", "Home -0.5") == "handicap"
    assert market_family("team_total_home", "Over 1.5") == "team-based"
    assert market_family("1x2_ht", "Home") == "half-time"


# ---------------------------------------------------------------------------
# (e) compat + prompt
# ---------------------------------------------------------------------------

def test_legacy_odds_keys_still_resolve_without_markets_array():
    """Un pachet vechi (doar 1X2/over_under/…) tot produce chei permise."""
    old = {
        "bookmaker": "Bet365",
        "1X2": {"Home": "1.70", "Draw": "3.80", "Away": "5.00"},
        "over_under": {"Over 2.5": "1.85", "Under 2.5": "1.95", "Over 1.5": "1.25"},
        "btts": {"Yes": "1.80", "No": "2.00"},
        "double_chance": {"Home/Draw": "1.22"},
    }
    keys = analysts.allowed_prob_keys(old)
    assert {"home", "draw", "away", "over25", "under25", "over15",
            "btts_yes", "dc_home_draw"} <= keys


@pytest.mark.parametrize("mode", ["analysts", "classic"])
def test_prompt_covers_market_mix_edge_and_safe_alternative(monkeypatch, mode):
    monkeypatch.setenv("ORCHESTRATION_MODE", mode)
    p = prompts.build_system_prompt()
    assert "două rezultate finale" in p or "șansă dublă" in p
    assert "cea mai bună din piață" in p or "media pieței" in p
    assert "piața dă 61%" in p
    assert "over 1.5" in p.lower() or "șansă dublă" in p
    # Biletul sigur nu e doar 1X2 scurt.
    assert "safe" in p.lower() or "sigur" in p.lower()


def test_analyst_prompt_asks_for_three_families_and_edge():
    p = analysts._ANALYST_SYSTEM_PROMPT
    assert "at least 3" in p
    assert "double chance" in p.lower()
    assert "EDGE" in p or "edge" in p
    assert "allowed_prob_keys" in p
    assert "ROMANIAN" in p


# ---------------------------------------------------------------------------
# Status animat: ce date se preiau, pentru cine (fara nume de API)
# ---------------------------------------------------------------------------

def test_status_helpers_format_dates_and_lists():
    assert agent._human_day("2026-08-29") == "29 aug"
    assert agent._join_ro(["Liverpool – Forest", "Juve – Parma"]) == \
        "Liverpool – Forest și Juve – Parma"
    assert "încă 2" in agent._join_ro(["A", "B", "C", "D", "E", "F"], limit=4)


async def test_status_label_names_the_match_and_the_data(no_http):
    await db.init_db()
    await _seed_fixture(1557383, _today(), "16:30",
                        home=(40, "Liverpool"), away=(65, "Nottingham Forest"))
    label = await agent.status_label("get_odds", {"fixture_id": 1557383})
    assert "Liverpool" in label and "Nottingham Forest" in label
    assert "cotele" in label
    assert "API" not in label and "get_odds" not in label and "Bet365" not in label

    form = await agent.status_label("get_team_last_matches",
                                    {"team_id": 40, "count": 6})
    assert "Liverpool" in form and "ultimele 6" in form

    built = await agent.status_label("build_ticket", {
        "candidates": [{}, {}, {}], "target_odds": 10,
    })
    assert "3 variante" in built and "10" in built
    assert "build_ticket" not in built


async def test_status_label_fixtures_uses_human_dates(no_http):
    label = await agent.status_label("get_fixtures", {
        "date_from": "2026-08-29", "date_to": "2026-08-30",
    })
    assert "29 aug" in label and "30 aug" in label
    assert "programul" in label


def test_prompt_asks_for_concrete_step_messages():
    p = prompts.build_system_prompt()
    assert "Name the matches" in p
    assert "Never mention APIs" in p


# ---------------------------------------------------------------------------
# Casa de pariuri lângă cotă (afișare; construcția biletului neschimbată)
# ---------------------------------------------------------------------------

def test_format_odds_label_omits_house_when_name_missing():
    assert fd.format_odds_label(1.85, "Superbet") == "1.85"
    assert fd.format_odds_label(1.9, "  Superbet ") == "1.90"
    assert fd.format_odds_label(1.85, "Bet365") == "1.85"
    assert fd.format_odds_label(1.85, "Betano") == "1.85"
    assert fd.format_odds_label(1.85, None) == "1.85"
    assert fd.format_odds_label(1.85, "") == "1.85"
    assert fd.format_odds_label(1.85, "?") == "1.85"


def test_pick_display_quote_prefers_romanian_house_from_same_tuple():
    quotes = [
        (8, "Bet365", 1.90),
        (34, "Superbet", 1.70),
        (16, "Unibet", 1.88),
    ]
    odd, name = fd.pick_display_quote(quotes)
    assert odd == 1.70 and name == "Superbet"


def test_pick_display_quote_falls_back_to_best_when_no_ro_house():
    quotes = [
        (8, "Bet365", 1.80),
        (11, "1xBet", 1.92),
        (4, "Pinnacle", 1.85),
    ]
    odd, name = fd.pick_display_quote(quotes)
    assert odd == 1.92 and name == "1xBet"


def test_pick_display_quote_no_name_means_no_attribution():
    quotes = [(99, "", 2.10), (8, "Bet365", 1.80)]
    odd, name = fd.pick_display_quote(quotes)
    # Bet365 e RO? Nu. Best e 2.10 la casa fără nume → cota fără atribuire.
    assert odd == 2.10 and name is None


async def test_displayed_odd_and_bookmaker_are_the_same_api_pair(fake_http):
    """(a) Superbet 1.70 Home — afișarea e exact perechea din obiectul API."""
    await db.init_db()
    books = [
        _book(8, "Bet365", 1.00),          # Home 1.80
        _book(11, "1xBet", 1.10),          # Home 1.98 — cea mai bună
        _book(34, "Superbet", 0.9444),     # Home ~1.70
        _book(16, "Unibet", 1.05),
    ]
    fake_http.response_payload = [{"bookmakers": books}]
    out = await fd.get_odds(4242)
    home = next(o for m in out["markets"] if m["key"] == "1x2"
                for o in m["outcomes"] if o["value"] == "Home")
    assert home["display_odd"] == pytest.approx(1.70, abs=0.01)
    assert home["display_bookmaker"] == "Superbet"
    assert home["odds_label"] == fd.format_odds_label(
        home["display_odd"], home["display_bookmaker"])
    assert "Superbet" not in (home["odds_label"] or "")
    assert home["odds_label"] == "1.70"
    assert home["best_odd"] == pytest.approx(1.98, abs=0.01)
    assert home["best_bookmaker"] == "1xBet"
    # Construcția (reference) rămâne Bet365.
    assert home["reference_odd"] == pytest.approx(1.80, abs=0.01)


async def test_missing_bookmaker_name_shows_odd_without_attribution(fake_http):
    """(b) Numele casei lipsește → doar cota, nicio casă ghicită."""
    await db.init_db()
    nameless = _book(99, "", 1.20)  # cea mai bună, dar fără nume
    nameless["name"] = None
    fake_http.response_payload = [{"bookmakers": [
        _book(8, "Bet365", 1.00),
        nameless,
    ]}]
    out = await fd.get_odds(4243)
    home = next(o for m in out["markets"] if m["key"] == "1x2"
                for o in m["outcomes"] if o["value"] == "Home")
    assert "display_bookmaker" not in home
    assert "(" not in (home.get("odds_label") or "")
    assert home["odds_label"] == fd.format_odds_label(home["display_odd"], None)
    for banned in ("Superbet", "Betano", "Unibet", "Bet365"):
        assert banned not in (home.get("odds_label") or "")


async def test_ro_priority_then_best_odd_fallback(fake_http):
    """(c) Cu Superbet+Betano → Superbet; fără case RO → cea mai bună cotă."""
    await db.init_db()
    fake_http.response_payload = [{"bookmakers": [
        _book(8, "Bet365", 1.00),
        _book(32, "Betano", 0.95),
        _book(34, "Superbet", 0.90),
        _book(11, "1xBet", 1.20),
    ]}]
    with_ro = await fd.get_odds(1)
    home = next(o for m in with_ro["markets"] if m["key"] == "1x2"
                for o in m["outcomes"] if o["value"] == "Home")
    assert home["display_bookmaker"] == "Superbet"
    assert home["best_bookmaker"] == "1xBet"

    fake_http.response_payload = [{"bookmakers": [
        _book(8, "Bet365", 1.00),
        _book(11, "1xBet", 1.15),
        _book(4, "Pinnacle", 1.05),
    ]}]
    no_ro = await fd.get_odds(2)
    home2 = next(o for m in no_ro["markets"] if m["key"] == "1x2"
                 for o in m["outcomes"] if o["value"] == "Home")
    assert home2["display_bookmaker"] == "1xBet"
    assert home2["display_odd"] == home2["best_odd"]
    assert home2["odds_label"] == fd.format_odds_label(
        home2["display_odd"], "1xBet")


def test_build_ticket_does_not_change_odds_when_passing_display_fields():
    """Logica de construcție rămâne pe `odds`; eticheta e doar afișare."""
    res = build_ticket([{
        "fixture_id": 1, "match": "A vs B", "market": "1X2", "pick": "1",
        "odds": 1.85, "prob": 0.55,
        "display_odd": 1.70, "display_bookmaker": "Superbet",
        "odds_label": "1.70 (Superbet)",
    }], target_odds=1.8)
    assert res["ok"]
    sel = res["selections"][0]
    assert sel["odds"] == 1.85
    assert sel["odds_label"] == "1.70 (Superbet)"
    assert sel["display_bookmaker"] == "Superbet"
    assert res["total_odds"] == pytest.approx(1.85, abs=0.001)


def test_enrich_copies_odds_label_from_the_same_outcome():
    analysis = {
        "confidence": "medium",
        "best_candidates": [
            {"market": "1X2", "pick": "Home", "odds": 1.80, "prob": 0.55,
             "reason": "4W din 5 acasă"},
        ],
    }
    pack = _odds_pack_1x2_ou25()
    pack["markets"][0]["outcomes"][0].update({
        "display_odd": 1.70, "display_bookmaker": "Superbet",
        "odds_label": "1.70",
    })
    analysts.enrich_candidates(analysis, pack)
    c = analysis["best_candidates"][0]
    assert c["odds_label"] == "1.70"
    assert c["display_bookmaker"] == "Superbet"
    assert c["display_odd"] == 1.70
    assert c["odds"] == 1.80  # neschimbat — construcția biletului
    assert c["best_bookmaker"] == "Unibet"


@pytest.mark.parametrize("mode", ["analysts", "classic"])
def test_prompt_forbids_inventing_bookmaker_names(monkeypatch, mode):
    monkeypatch.setenv("ORCHESTRATION_MODE", mode)
    p = prompts.build_system_prompt()
    assert "ODDS & BOOKMAKER HONESTY" in p
    assert "odds_label" in p
    assert "NEVER write bookmaker names" in p
    assert "NEVER invent" in p

