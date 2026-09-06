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
    assert pack["players"][0]["pos"] == "G"


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


async def test_assemble_pack_includes_squad(fake_http):
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
    fake_http.response_payload = [{
        "team": {"id": 50, "name": "City"},
        "players": [{"id": 1, "name": "Ederson", "position": "Goalkeeper", "number": 31}],
    }]
    pack = await analysts.assemble_data_pack(501)
    assert "squad" in pack["home"]
    assert "recent_transfers" in pack["home"]
    assert "lineups" in pack
    assert any(e == "/players/squads" for e, _ in fake_http.calls)


def test_prompts_forbid_naming_players_from_memory():
    p = prompts.build_system_prompt("analysts")
    assert "lookup_player" in p
    assert "get_team_squad" in p
    assert "PLAYERS" in p
    a = analysts._ANALYST_SYSTEM_PROMPT
    assert "squad.players" in a
    assert "do not mention them" in a.lower() or "If the name is not in the pack" in a


def test_player_tools_registered_in_both_modes(monkeypatch):
    monkeypatch.setenv("ORCHESTRATION_MODE", "classic")
    names = {t["name"] for t in agent.build_tools()}
    assert "get_team_squad" in names
    assert "lookup_player" in names
    monkeypatch.setenv("ORCHESTRATION_MODE", "analysts")
    names = {t["name"] for t in agent.build_tools("analysts")}
    assert "get_team_squad" in names and "lookup_player" in names
