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


async def test_get_team_squad_strips_players_also_on_reserve_team(fake_http):
    """API amestecă Castilla în lotul 541; scoatem id-urile de pe Real Madrid II."""
    fake_http.payload_for["/teams"] = [
        {"team": {"id": 541, "name": "Real Madrid"}},
        {"team": {"id": 9575, "name": "Real Madrid II"}},
        {"team": {"id": 22142, "name": "Real Madrid III"}},
        {"team": {"id": 9999, "name": "Real Madrid Women"}},
    ]
    fake_http.squads_by_team = {
        541: [{
            "team": {"id": 541, "name": "Real Madrid"},
            "players": [
                {"id": 730, "name": "T. Courtois", "number": 1, "position": "Goalkeeper"},
                {"id": 762, "name": "Vinícius Júnior", "number": 7, "position": "Midfielder"},
                {"id": 386872, "name": "Sergio Mestre", "number": 26, "position": "Goalkeeper"},
                {"id": 443595, "name": "Jesús Fortea", "number": 2, "position": "Defender"},
            ],
        }],
        9575: [{
            "team": {"id": 9575, "name": "Real Madrid II"},
            "players": [
                {"id": 386872, "name": "Sergio Mestre", "number": 1, "position": "Goalkeeper"},
                {"id": 443595, "name": "Jesús Fortea", "number": 2, "position": "Defender"},
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
    assert names == {"T. Courtois", "Vinícius Júnior"}
    assert squad["count"] == 2
    squad_calls = [(e, p) for e, p in fake_http.calls if e == "/players/squads"]
    assert {"team": 541} in [p for _, p in squad_calls]
    assert {"team": 9575} in [p for _, p in squad_calls]
    # Women nu e filială — nu cerem lotul.
    assert not any(p.get("team") == 9999 for _, p in squad_calls)


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
    squad = await fd.get_team_squad(50)
    assert squad["count"] == 1
    assert squad["players"][0]["name"] == "Erling Haaland"
    assert any(e == "/players/squads" for e, _ in fake_http.calls)

    fake_http.response_payload = [{
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
