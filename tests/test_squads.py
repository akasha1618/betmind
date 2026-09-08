"""Lot actual, transferuri recente, lookup jucator — fara nume din memorie."""

from __future__ import annotations

from datetime import date, timedelta

import prompts
import football_data as fd
import analysts
import agent


def test_player_name_matches_relaxed():
    assert fd.player_name_matches("Haaland", "Erling Haaland")
    assert fd.player_name_matches("erling haaland", "Haaland")
    assert fd.player_name_matches("Burcă", "Burca")
    assert not fd.player_name_matches("Messi", "Cristiano Ronaldo")
    assert not fd.player_name_matches("John Smith", "John Doe")


def test_squad_pack_from_api_shape():
    raw = [{
        "team": {"id": 50, "name": "Manchester City"},
        "players": [
            {"id": 1100, "name": "Ederson", "age": 32, "number": 31, "position": "Goalkeeper"},
            {"id": 1101, "name": "Erling Haaland", "age": 25, "number": 9, "position": "Attacker"},
            {"id": 1102, "name": "", "position": "Midfielder"},
        ],
    }]
    pack = fd._squad_pack(raw, 50)
    assert pack["count"] == 2
    assert pack["team"] == "Manchester City"
    assert pack["players"][1]["name"] == "Erling Haaland"
    assert pack["players"][1]["pos"] == "A"
    assert pack["players"][1]["age"] == 25
    assert pack["players"][0]["pos"] == "G"
    assert "no" not in pack["players"][0]
    assert "no" not in pack["players"][1]


def test_is_reserve_side_matches_academy_suffixes_not_other_clubs():
    assert fd.is_reserve_side("Real Madrid", "Real Madrid II")
    assert fd.is_reserve_side("Real Madrid", "Real Madrid III")
    assert fd.is_reserve_side("Real Madrid II", "Real Madrid III")
    assert fd.is_reserve_side("FC Barcelona", "FC Barcelona B")
    assert fd.is_reserve_side("Bayern Munich", "Bayern Munich II")
    assert fd.is_reserve_side("Chelsea", "Chelsea U21")
    assert fd.is_reserve_side("Chelsea", "Chelsea U18")
    assert fd.is_reserve_side("Juventus", "Juventus Next Gen")
    assert not fd.is_reserve_side("Real Madrid II", "Real Madrid")
    assert not fd.is_reserve_side("Real Madrid", "Real Madrid")
    assert not fd.is_reserve_side("Real Madrid", "Real Madrid Women")
    assert not fd.is_reserve_side("Inter", "Inter Miami")
    assert not fd.is_reserve_side("Manchester United", "West Ham United")
    assert not fd.is_reserve_side("Sporting CP", "Sporting Braga")


def test_european_season_july_june():
    assert fd.european_season(date(2026, 9, 8)) == 2026
    assert fd.european_season(date(2027, 2, 1)) == 2026
    assert fd.european_season(date(2026, 7, 1)) == 2026
    assert fd.european_season(date(2026, 6, 30)) == 2025


def test_select_first_team_keeps_injured_and_apps_drops_academy_only():
    """Criteriu vs lot oficial RM 2026/27 (realmadrid.com, 10.08.2026)."""
    players = [
        {"id": 730, "name": "T. Courtois", "age": 33, "pos": "G"},
        {"id": 568427, "name": "I. Voloshyn", "age": 19, "pos": "G"},
        {"id": 341640, "name": "Raúl Asencio", "age": 22, "pos": "D"},
        {"id": 284300, "name": "Álvaro Fernández", "age": 23, "pos": "D"},
        {"id": 330436, "name": "David Jiménez", "age": 21, "pos": "D"},
        {"id": 443595, "name": "Jesús Fortea", "age": 18, "pos": "D"},
        {"id": 313167, "name": "Manuel Ángel", "age": 21, "pos": "M"},
        {"id": 509470, "name": "Thiago Pitarch", "age": 18, "pos": "M"},
        {"id": 377122, "name": "Endrick", "age": 19, "pos": "A"},
        {"id": 386872, "name": "Sergio Mestre", "age": 20, "pos": "G"},
    ]
    appearances = {730: 4, 284300: 2}
    injured = {341640, 509470, 377122}
    reserve = {443595, 386872}
    kept = {p["name"] for p in fd.select_first_team_players(
        players, appearances, injured, reserve)}
    assert kept == {
        "T. Courtois", "Raúl Asencio", "Álvaro Fernández",
        "Thiago Pitarch", "Endrick",
    }
    assert "I. Voloshyn" not in kept
    assert "David Jiménez" not in kept
    assert "Manuel Ángel" not in kept
    assert "Jesús Fortea" not in kept
    assert "Sergio Mestre" not in kept


def test_transfer_ledger_drops_stale_departures_not_old_arrivals():
    """Bellingham plecat în 2023: ultima mutare e out → scos, indiferent de vechime."""
    dortmund = 165
    raw = [{
        "player": {"id": 129718, "name": "J. Bellingham"},
        "transfers": [
            {"date": "2023-06-14", "type": "€103M",
             "teams": {"in": {"id": 541, "name": "Real Madrid"},
                       "out": {"id": 165, "name": "Borussia Dortmund"}}},
            {"date": "2020-07-20", "type": "€25M",
             "teams": {"in": {"id": 165, "name": "Borussia Dortmund"},
                       "out": {"id": 746, "name": "Birmingham"}}},
        ],
    }, {
        "player": {"id": 99, "name": "Old Signing"},
        "transfers": [
            {"date": "2019-07-01", "type": "Free",
             "teams": {"in": {"id": 165, "name": "Borussia Dortmund"},
                       "out": {"id": 1, "name": "Elsewhere"}}},
        ],
    }]
    left_ids, left_names, arrivals = fd.transfer_ledger(raw, dortmund)
    assert 129718 in left_ids
    assert any(fd.player_name_matches("Bellingham", n) for n in left_names)
    assert all(a["name"] != "Old Signing" for a in arrivals)

    season_in = date(fd.european_season(), 7, 15).isoformat()
    raw.append({
        "player": {"id": 42, "name": "New Striker"},
        "transfers": [{
            "date": season_in, "type": "€10M",
            "teams": {"in": {"id": 165, "name": "Borussia Dortmund"},
                      "out": {"id": 50, "name": "City"}},
        }],
    })
    _, _, arrivals = fd.transfer_ledger(raw, dortmund)
    assert any(a["id"] == 42 for a in arrivals)


def test_apply_transfer_ledger_bellingham_and_returnee():
    players = [
        {"id": 129718, "name": "J. Bellingham", "age": 22, "pos": "M"},
        {"id": 1, "name": "Gregor Kobel", "age": 28, "pos": "G"},
        {"id": 2, "name": "Jude Nickname", "age": 22, "pos": "M"},
    ]
    left_ids, left_names, arrivals = {129718}, ["J. Bellingham"], []
    kept = fd.apply_transfer_ledger(players, left_ids, left_names, arrivals)
    names = {p["name"] for p in kept}
    assert "J. Bellingham" not in names
    assert "Gregor Kobel" in names
    # still at club this season despite an old out in the dump
    kept_apps = fd.apply_transfer_ledger(
        players, {129718}, ["J. Bellingham"], [], appearances={129718: 3})
    assert any(p["id"] == 129718 for p in kept_apps)

    returned = fd.apply_transfer_ledger(
        [{"id": 8, "name": "Loan Back", "age": 24, "pos": "D"}],
        set(), [],
        [{"id": 8, "name": "Loan Back", "pos": "?", "age": None}],
    )
    assert len(returned) == 1

    missing_arrival = fd.apply_transfer_ledger(
        [{"id": 1, "name": "Kobel", "age": 28, "pos": "G"}],
        set(), [],
        [{"id": 42, "name": "New Striker", "pos": "A", "age": 22}],
    )
    assert {p["name"] for p in missing_arrival} == {"Kobel", "New Striker"}

    # Fratele rămas la club (Jobe) nu e scos doar pentru că Jude e tot „J. Bellingham”.
    jobe_kept = fd.apply_transfer_ledger(
        [{"id": 326757, "name": "J. Bellingham", "age": 20, "pos": "M"},
         {"id": 1, "name": "Kobel", "age": 28, "pos": "G"}],
        {129718}, ["J. Bellingham"], [],
    )
    assert {p["id"] for p in jobe_kept} == {326757, 1}


def test_squad_display_name_expands_initials():
    assert fd.squad_display_name("J. Bellingham", {
        "firstname": "Jobe Samuel Patrick", "lastname": "Bellingham",
    }) == "Jobe Bellingham"
    assert fd.squad_display_name("T. Courtois", {
        "firstname": "Thibaut", "lastname": "Courtois",
    }) == "Thibaut Courtois"
    assert fd.squad_display_name("G. Kobel", None) == "G. Kobel"


def test_annotate_availability_marks_injured_not_cards_over_injury():
    players = [
        {"id": 26243, "name": "Nico Schlotterbeck", "age": 26, "pos": "D"},
        {"id": 864, "name": "Emre Can", "age": 32, "pos": "M"},
        {"id": 3, "name": "Inacio", "age": 17, "pos": "A"},
        {"id": 4, "name": "Kobel", "age": 28, "pos": "G"},
    ]
    injured = {
        26243: {"reason": "Ankle Injury"},
        864: {"reason": "Knee Injury"},
        3: {"reason": "Red Card"},
    }
    out = {p["id"]: p for p in fd.annotate_availability(players, injured)}
    assert out[26243]["injured"] is True
    assert out[26243]["injury"] == "Ankle Injury"
    assert out[864]["injured"] is True
    assert out[3].get("injured") is None
    assert out[3]["unavailable"] is True
    assert out[4].get("injured") is None


def test_apply_transfer_ledger_keeps_injured_even_if_marked_left():
    players = [
        {"id": 864, "name": "E. Can", "age": 32, "pos": "M"},
        {"id": 129718, "name": "J. Bellingham", "age": 23, "pos": "M"},
    ]
    kept = fd.apply_transfer_ledger(
        players, {864, 129718}, [], [],
        appearances={}, injured_ids={864},
    )
    ids = {p["id"] for p in kept}
    assert 864 in ids
    assert 129718 not in ids


async def test_get_team_squad_strips_players_also_on_reserve_team(fake_http):
    """API amestecă Castilla în lotul 541; scoatem id-urile de pe Real Madrid II
    doar dacă n-au jucat / nu sunt accidentați la prima echipă."""
    fake_http.payload_for["/teams"] = [
        {"team": {"id": 541, "name": "Real Madrid"}},
        {"team": {"id": 9575, "name": "Real Madrid II"}},
        {"team": {"id": 22142, "name": "Real Madrid III"}},
        {"team": {"id": 9999, "name": "Real Madrid Women"}},
    ]
    fake_http.payload_for["/players"] = [
        {"player": {"id": 730, "name": "T. Courtois", "age": 33},
         "statistics": [{"games": {"appearences": 4}}]},
        {"player": {"id": 762, "name": "Vinícius Júnior", "age": 25},
         "statistics": [{"games": {"appearences": 4}}]},
    ]
    fake_http.payload_for["/injuries"] = [
        {"team": {"id": 541},
         "player": {"id": 509470, "name": "Thiago Pitarch",
                    "position": "Midfielder", "age": 18}},
        {"team": {"id": 541},
         "player": {"id": 10009, "name": "Rodrygo",
                    "position": "Attacker", "age": 24}},
    ]
    fake_http.payload_for["/transfers"] = []
    fake_http.squads_by_team = {
        541: [{
            "team": {"id": 541, "name": "Real Madrid"},
            "players": [
                {"id": 730, "name": "T. Courtois", "age": 33, "number": 1, "position": "Goalkeeper"},
                {"id": 762, "name": "Vinícius Júnior", "age": 25, "number": 7, "position": "Midfielder"},
                {"id": 386872, "name": "Sergio Mestre", "age": 20, "number": 26, "position": "Goalkeeper"},
                {"id": 443595, "name": "Jesús Fortea", "age": 18, "number": 2, "position": "Defender"},
                {"id": 568427, "name": "I. Voloshyn", "age": 19, "number": None, "position": "Goalkeeper"},
                {"id": 509470, "name": "Thiago Pitarch", "age": 18, "number": 27, "position": "Midfielder"},
            ],
        }],
        9575: [{
            "team": {"id": 9575, "name": "Real Madrid II"},
            "players": [
                {"id": 386872, "name": "Sergio Mestre", "number": 1, "position": "Goalkeeper"},
                {"id": 443595, "name": "Jesús Fortea", "number": 2, "position": "Defender"},
                {"id": 509470, "name": "Thiago Pitarch", "number": 16, "position": "Midfielder"},
            ],
        }],
        22142: [{
            "team": {"id": 22142, "name": "Real Madrid III"},
            "players": [
                {"id": 1, "name": "Some Cadet", "number": 10, "position": "Attacker"},
            ],
        }],
    }
    squad = await fd.get_team_squad(541)
    names = {p["name"] for p in squad["players"]}
    assert names == {"T. Courtois", "Vinícius Júnior", "Thiago Pitarch", "Rodrygo"}
    assert squad["count"] == 4
    squad_calls = [(e, p) for e, p in fake_http.calls if e == "/players/squads"]
    assert {"team": 541} in [p for _, p in squad_calls]
    assert {"team": 9575} in [p for _, p in squad_calls]
    # Women nu e filială — nu cerem lotul.
    assert not any(p.get("team") == 9999 for _, p in squad_calls)


async def test_get_team_squad_drops_player_who_left_years_ago(fake_http):
    fake_http.payload_for["/teams"] = [
        {"team": {"id": 165, "name": "Borussia Dortmund"}},
    ]
    fake_http.payload_for["/players"] = [
        {"player": {"id": 1, "name": "Gregor Kobel", "age": 28},
         "statistics": [{"games": {"appearences": 5}}]},
    ]
    fake_http.payload_for["/injuries"] = []
    fake_http.payload_for["/transfers"] = [{
        "player": {"id": 129718, "name": "J. Bellingham"},
        "transfers": [{
            "date": "2023-06-14", "type": "€103M",
            "teams": {"in": {"id": 541, "name": "Real Madrid"},
                      "out": {"id": 165, "name": "Borussia Dortmund"}},
        }],
    }]
    fake_http.squads_by_team = {
        165: [{
            "team": {"id": 165, "name": "Borussia Dortmund"},
            "players": [
                {"id": 1, "name": "Gregor Kobel", "age": 28, "position": "Goalkeeper"},
                {"id": 129718, "name": "J. Bellingham", "age": 25, "position": "Midfielder"},
            ],
        }],
    }
    squad = await fd.get_team_squad(165)
    names = {p["name"] for p in squad["players"]}
    assert "Gregor Kobel" in names
    assert "J. Bellingham" not in names
    assert any(e == "/transfers" for e, _ in fake_http.calls)


async def test_get_team_squad_keeps_injured_and_expands_names(fake_http):
    fake_http.payload_for["/teams"] = [
        {"team": {"id": 165, "name": "Borussia Dortmund"}},
    ]
    fake_http.payload_for["/players"] = [
        {"player": {"id": 1, "firstname": "Gregor", "lastname": "Kobel", "age": 28},
         "statistics": [{"games": {"appearences": 5}}]},
        {"player": {"id": 326757, "firstname": "Jobe Samuel Patrick",
                    "lastname": "Bellingham", "age": 20},
         "statistics": [{"games": {"appearences": 3}}]},
    ]
    fake_http.payload_for["/injuries"] = [
        {"team": {"id": 165},
         "player": {"id": 26243, "name": "N. Schlotterbeck", "reason": "Ankle Injury"}},
        {"team": {"id": 165},
         "player": {"id": 864, "name": "E. Can", "reason": "Knee Injury"}},
    ]
    fake_http.payload_for["/transfers"] = [{
        "player": {"id": 864, "name": "E. Can"},
        "transfers": [{
            "date": "2018-07-01", "type": "Free",
            "teams": {"in": {"id": 496, "name": "Juventus"},
                      "out": {"id": 40, "name": "Liverpool"}},
        }],
    }]
    fake_http.profiles_by_player = {
        26243: [{"player": {"id": 26243, "firstname": "Nico Cedric",
                            "lastname": "Schlotterbeck"}}],
        864: [{"player": {"id": 864, "firstname": "Emre", "lastname": "Can"}}],
    }
    fake_http.squads_by_team = {
        165: [{
            "team": {"id": 165, "name": "Borussia Dortmund"},
            "players": [
                {"id": 1, "name": "G. Kobel", "age": 28, "position": "Goalkeeper"},
                {"id": 26243, "name": "N. Schlotterbeck", "age": 26, "position": "Defender"},
                {"id": 326757, "name": "J. Bellingham", "age": 20, "position": "Midfielder"},
            ],
        }],
    }
    squad = await fd.get_team_squad(165)
    by_id = {p["id"]: p for p in squad["players"]}
    assert 864 in by_id
    assert 26243 in by_id
    assert by_id[864]["injured"] is True
    assert by_id[864]["injury"] == "Knee Injury"
    assert by_id[26243]["injured"] is True
    assert by_id[26243]["injury"] == "Ankle Injury"
    assert by_id[864]["name"] == "Emre Can"
    assert by_id[26243]["name"] == "Nico Schlotterbeck"
    assert by_id[326757]["name"] == "Jobe Bellingham"
    assert by_id[1]["name"] == "Gregor Kobel"


async def test_get_team_squad_on_b_team_keeps_own_players(fake_http):
    """Lotul filialei nu e golit de jucătorii care mai apar și la prima echipă."""
    fake_http.payload_for["/teams"] = [
        {"team": {"id": 541, "name": "Real Madrid"}},
        {"team": {"id": 9575, "name": "Real Madrid II"}},
        {"team": {"id": 22142, "name": "Real Madrid III"}},
    ]
    fake_http.squads_by_team = {
        9575: [{
            "team": {"id": 9575, "name": "Real Madrid II"},
            "players": [
                {"id": 386872, "name": "Sergio Mestre", "number": 1, "position": "Goalkeeper"},
                {"id": 99, "name": "Cadet III", "number": 8, "position": "Midfielder"},
            ],
        }],
        22142: [{
            "team": {"id": 22142, "name": "Real Madrid III"},
            "players": [
                {"id": 99, "name": "Cadet III", "number": 8, "position": "Midfielder"},
            ],
        }],
    }
    squad = await fd.get_team_squad(9575)
    names = {p["name"] for p in squad["players"]}
    assert "Sergio Mestre" in names
    assert "Cadet III" not in names
    assert not any(p.get("team") == 541 for e, p in fake_http.calls if e == "/players/squads")


async def test_get_team_squad_and_transfers(fake_http):
    today = date.today()
    fake_http.response_payload = [{
        "team": {"id": 50, "name": "Manchester City"},
        "players": [
            {"id": 9, "name": "Erling Haaland", "position": "Attacker", "number": 9},
        ],
    }]
    fake_http.payload_for["/transfers"] = [{
        "player": {"name": "Kalvin Phillips"},
        "transfers": [{
            "date": (today - timedelta(days=10)).isoformat(),
            "type": "Loan",
            "teams": {"in": {"id": 33, "name": "Ipswich"},
                      "out": {"id": 50, "name": "Manchester City"}},
        }],
    }, {
        "player": {"name": "Someone Old"},
        "transfers": [{
            "date": (today - timedelta(days=400)).isoformat(),
            "type": "€20M",
            "teams": {"in": {"id": 99, "name": "Alta"},
                      "out": {"id": 50, "name": "Manchester City"}},
        }],
    }]
    squad = await fd.get_team_squad(50)
    assert squad["count"] == 1
    assert squad["players"][0]["name"] == "Erling Haaland"
    assert any(e == "/players/squads" for e, _ in fake_http.calls)
    assert any(e == "/transfers" for e, _ in fake_http.calls)

    xf = await fd.get_team_transfers(50, days=90)
    names_out = {t["name"] for t in xf["out"]}
    assert "Kalvin Phillips" in names_out
    assert "Someone Old" not in names_out


async def test_lookup_player_on_squad_and_departed(fake_http, monkeypatch):
    async def squad(tid):
        return {"team_id": tid, "team": "City", "count": 1,
                "players": [{"id": 9, "name": "Erling Haaland", "pos": "A", "no": 9}]}

    async def xf(tid, days=90):
        return {"team_id": tid, "since_days": days, "in": [],
                "out": [{"name": "Kalvin Phillips", "date": "2026-08-01",
                         "type": "Loan", "to": "Ipswich"}]}

    monkeypatch.setattr(fd, "get_team_squad", squad)
    monkeypatch.setattr(fd, "get_team_transfers", xf)

    yes = await fd.lookup_player("Haaland", team_id=50)
    assert yes["at_team"] is True
    assert yes["player"]["name"] == "Erling Haaland"

    no = await fd.lookup_player("Phillips", team_id=50)
    assert no["at_team"] is False
    assert no["left"]["to"] == "Ipswich"

    missing = await fd.lookup_player("Messi", team_id=50)
    assert missing["at_team"] is False
    assert "Nu e în lotul actual" in missing["note"]


async def test_assemble_pack_includes_transfers_not_full_squad(fake_http):
    import db
    from tests.conftest import raw_fixture
    from tests.test_v1b import _now_iso, _today

    await db.init_db()
    parsed = fd._parse_fixture(raw_fixture(
        fixture_id=501, league_id=39,
        kickoff=f"{_today()}T19:30:00+03:00",
        home=(50, "City"), away=(33, "United"),
    ))
    await db.upsert_fixture(parsed, _now_iso())
    pack = await analysts.assemble_data_pack(501)
    assert "squad" not in pack["home"]
    assert "squad" not in pack["away"]
    assert "recent_transfers" in pack["home"]
    assert "lineups" in pack
    assert not any(e == "/players/squads" for e, _ in fake_http.calls)
    assert any(e == "/transfers" for e, _ in fake_http.calls)


def test_prompts_forbid_naming_players_from_memory():
    p = prompts.build_system_prompt("analysts")
    assert "lookup_player" in p
    assert "get_team_squad" in p
    assert "PLAYERS" in p
    assert "shirt numbers" in p
    assert "injured" in p
    assert "by_league" in p
    a = analysts._ANALYST_SYSTEM_PROMPT
    assert "squad.players" not in a
    assert "recent_transfers" in a
    assert "do not mention them" in a.lower() or "If the name is not in the pack" in a


def test_player_tools_registered_in_both_modes(monkeypatch):
    monkeypatch.setenv("ORCHESTRATION_MODE", "classic")
    names = {t["name"] for t in agent.build_tools()}
    assert "get_team_squad" in names
    assert "lookup_player" in names
    monkeypatch.setenv("ORCHESTRATION_MODE", "analysts")
    names = {t["name"] for t in agent.build_tools("analysts")}
    assert "get_team_squad" in names and "lookup_player" in names
