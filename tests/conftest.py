"""Fixture-uri comune: mediu izolat (DB temporar, buget mic, fara HTTP real)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Optional

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))  # radacina proiectului

import db  # noqa: E402
import football_data as fd  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """DB temporar per test + config determinist + cache HTTP golit."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("MAX_DAILY_API_REQUESTS", "50")
    monkeypatch.setenv("APP_TIMEZONE", "Europe/Bucharest")
    monkeypatch.setenv("SYNC_ENABLED", "false")
    monkeypatch.setenv("SYNC_WINDOW_PAST_DAYS", "7")
    monkeypatch.setenv("SYNC_WINDOW_FUTURE_DAYS", "14")
    monkeypatch.setenv("API_FOOTBALL_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    # Titlul automat face un apel LLM propriu: implicit oprit in teste, pornit
    # explicit acolo unde e testat (cu LLM-ul simulat).
    monkeypatch.setenv("AUTO_TITLE_ENABLED", "false")
    # V1-F: poarta de acces oprita implicit (testele ei o pornesc explicit);
    # neutralizam si detectarea mediului de productie, indiferent de .env local.
    monkeypatch.setenv("ACCESS_PASSWORD", "")
    monkeypatch.setenv("SESSION_SECRET", "")
    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    monkeypatch.delenv("TRUST_PROXY_HEADERS", raising=False)
    fd._cache.clear()
    fd._cache_lock = asyncio.Lock()  # lock nou — loop nou per test
    fd._requests_remaining = None
    # Diagnosticul API (istoric erori + statistici per tura) e la nivel de modul:
    # fara golire, testele s-ar vedea unele pe altele.
    fd._recent_errors.clear()
    fd._turn_stats.clear()
    fd._last_rate_headers.clear()
    fd.set_current_turn(None)
    yield


def raw_fixture(fixture_id: int = 1001, league_id: int = 39,
                kickoff: str = "2026-08-22T19:30:00+03:00", status: str = "NS",
                home: tuple[int, str] = (10, "NEC"),
                away: tuple[int, str] = (20, "Excelsior"),
                goals: tuple = (None, None)) -> dict:
    """Payload brut in formatul API-Football /fixtures."""
    return {
        "fixture": {"id": fixture_id, "date": kickoff, "status": {"short": status}},
        "league": {"id": league_id, "name": "Premier League", "country": "England",
                   "season": 2026},
        "teams": {"home": {"id": home[0], "name": home[1]},
                  "away": {"id": away[0], "name": away[1]}},
        "goals": {"home": goals[0], "away": goals[1]},
    }


class FakeHTTP:
    """Inlocuitor pentru fd._http_get: raspunsuri programate + contor de apeluri."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.response_payload: list[dict] = []
        self.headers: dict[str, str] = {}
        # Pentru testele de diagnostic: status non-200 sau corp cu `errors`.
        self.status_code: int = 200
        self.errors: Any = []
        self.text_body: Optional[str] = None

    async def __call__(self, endpoint: str, params: dict, headers: dict) -> httpx.Response:
        self.calls.append((endpoint, dict(params)))
        if self.text_body is not None:
            return httpx.Response(self.status_code, text=self.text_body, headers=self.headers)
        return httpx.Response(
            self.status_code,
            json={"errors": self.errors, "response": self.response_payload},
            headers=self.headers,
        )


@pytest.fixture
def fake_http(monkeypatch) -> FakeHTTP:
    fake = FakeHTTP()
    monkeypatch.setattr(fd, "_http_get", fake)
    return fake


@pytest.fixture
def no_http(monkeypatch):
    """Orice request HTTP real pica testul."""
    async def _boom(endpoint, params, headers):
        raise AssertionError(f"HTTP request neasteptat: {endpoint} {params}")
    monkeypatch.setattr(fd, "_http_get", _boom)
