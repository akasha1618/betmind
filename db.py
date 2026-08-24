"""
Stratul de persistenta BetMind — SQLite prin aiosqlite.

Contine fixture store-ul local (V1-A), bugetul zilnic de API si jurnalul de
sincronizare. Toate functiile deschid o conexiune scurta per operatie (SQLite
local, WAL) — simplu, sigur intre task-uri asyncio si usor de testat.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path
from typing import Any, Optional

import aiosqlite

log = logging.getLogger("betmind.db")

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS fixtures(
    fixture_id     INTEGER PRIMARY KEY,
    league_id      INTEGER,
    league_name    TEXT,
    season         INTEGER,
    date_local     TEXT,
    time_local     TEXT,
    kickoff_iso    TEXT,
    status         TEXT,
    status_group   TEXT,
    home_id        INTEGER,
    home_name      TEXT,
    away_id        INTEGER,
    away_name      TEXT,
    goals_home     INTEGER,
    goals_away     INTEGER,
    last_synced_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_fixtures_date ON fixtures(date_local);

CREATE TABLE IF NOT EXISTS fixture_changes(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture_id INTEGER NOT NULL,
    changed_at TEXT NOT NULL,
    field      TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT
);

CREATE TABLE IF NOT EXISTS sync_log(
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at        TEXT,
    finished_at       TEXT,
    dates_synced      TEXT,
    api_requests_used INTEGER,
    ok                INTEGER,
    error             TEXT
);

CREATE TABLE IF NOT EXISTS api_budget(
    day           TEXT PRIMARY KEY,
    requests_used INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS tracked_leagues(
    league_id INTEGER PRIMARY KEY,
    name      TEXT,
    added_by  TEXT,
    active    INTEGER NOT NULL DEFAULT 1
);

-- Acoperirea sincronizarii per zi: stim CAND a fost adusa ultima data ziua X,
-- chiar daca in ziua respectiva nu joaca nicio liga urmarita.
CREATE TABLE IF NOT EXISTS sync_days(
    day            TEXT PRIMARY KEY,
    last_synced_at TEXT
);

-- V1-B: analizele produse de agentii Match Analyst (JSON validat sau
-- inregistrare de esec {"analysis_failed": true}).
CREATE TABLE IF NOT EXISTS analyses(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    fixture_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    model      TEXT,
    json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_analyses_fixture ON analyses(fixture_id, id);

-- V1-B: jurnal de cost LLM (coordinator + analisti).
-- V1-C: coloane de prompt caching (cache_read/cache_write).
CREATE TABLE IF NOT EXISTS usage_log(
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    turn_id            TEXT,
    model              TEXT,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    cache_read_tokens  INTEGER,
    cache_write_tokens INTEGER,
    created_at         TEXT
);

-- V1-C: biletele prezentate utilizatorului (alimenteaza track record-ul V2).
CREATE TABLE IF NOT EXISTS tickets(
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT,
    created_at      TEXT,
    target_odds     REAL,
    total_odds      REAL,
    est_probability REAL,
    risk_level      TEXT,
    status          TEXT NOT NULL DEFAULT 'open'
);

CREATE TABLE IF NOT EXISTS ticket_selections(
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  INTEGER NOT NULL,
    fixture_id INTEGER,
    market     TEXT,
    pick       TEXT,
    odds       REAL,
    prob       REAL,
    confidence TEXT,
    result     TEXT
);

-- V1-D: conversatii persistente + feedback.
-- tickets/feedback pastreaza conversation_id ca coloana simpla (fara FK):
-- stergerea unei conversatii NU sterge biletele/feedback-ul (analytics V2).
-- title_auto = 1 dupa ce titlul a fost generat automat (o singura data).
CREATE TABLE IF NOT EXISTS conversations(
    id         TEXT PRIMARY KEY,
    user_key   TEXT,
    title      TEXT,
    created_at TEXT,
    updated_at TEXT,
    title_auto INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_key, updated_at);

-- turn_id leaga mesajul de apelurile din usage_log => cost per intrebare.
CREATE TABLE IF NOT EXISTS messages(
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role            TEXT NOT NULL,
    content_json    TEXT NOT NULL,
    created_at      TEXT,
    turn_id         TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS feedback(
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT,
    ticket_id       INTEGER,
    message_ref     TEXT NOT NULL,
    rating          TEXT NOT NULL CHECK(rating IN ('up','down')),
    comment         TEXT,
    created_at      TEXT,
    UNIQUE(conversation_id, message_ref)
);
"""

# Migratii aditive pentru baze create inainte de V1-C (CREATE IF NOT EXISTS
# nu adauga coloane la tabele existente).
_MIGRATIONS: dict[str, list[str]] = {
    "usage_log": [
        "ALTER TABLE usage_log ADD COLUMN cache_read_tokens INTEGER",
        "ALTER TABLE usage_log ADD COLUMN cache_write_tokens INTEGER",
    ],
    "conversations": [
        "ALTER TABLE conversations ADD COLUMN title_auto INTEGER NOT NULL DEFAULT 0",
    ],
    "messages": [
        "ALTER TABLE messages ADD COLUMN turn_id TEXT",
    ],
}

# Campurile pe care le urmarim pentru amanari/reprogramari.
WATCHED_FIELDS = ("status", "date_local", "time_local", "kickoff_iso")

_initialized_paths: set[str] = set()


def db_path() -> str:
    """Calea DB-ului. Prioritate: DB_PATH explicit; apoi DATA_DIR (sau /data pe
    Railway — volumul persistent); altfel data/betmind.db pentru dev local."""
    explicit = os.environ.get("DB_PATH", "").strip()
    if explicit:
        return explicit
    data_dir = os.environ.get("DATA_DIR", "").strip()
    if not data_dir and os.environ.get("RAILWAY_ENVIRONMENT", "").strip():
        data_dir = "/data"
    if data_dir:
        return str(Path(data_dir) / "betmind.db")
    return "data/betmind.db"


async def _connect() -> aiosqlite.Connection:
    path = db_path()
    parent = Path(path).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    if path not in _initialized_paths:
        await conn.executescript(_SCHEMA)
        for statement in [s for stmts in _MIGRATIONS.values() for s in stmts]:
            try:
                await conn.execute(statement)
            except aiosqlite.OperationalError:
                pass  # coloana exista deja
        await conn.commit()
        _initialized_paths.add(path)
    return conn


def _db_dir_diagnostics() -> str:
    """Context pentru esecurile de tipul «unable to open database file»: cine
    ruleaza procesul si cum arata directorul in care ar trebui scrisa baza.

    Cazul clasic in productie: volumul montat peste director apartine lui root,
    iar aplicatia ruleaza non-root — vezi docker-entrypoint.sh.
    """
    path = Path(db_path())
    directory = path.parent if str(path.parent) not in ("", ".") else Path(".")
    uid = getattr(os, "geteuid", lambda: "n/a")()
    gid = getattr(os, "getegid", lambda: "n/a")()
    parts = [f"db_path={path}", f"dir={directory}", f"uid={uid}", f"gid={gid}"]
    try:
        info = directory.stat()
        parts.append(f"dir_mode={stat.filemode(info.st_mode)}")
        parts.append(f"dir_owner={info.st_uid}:{info.st_gid}")
    except OSError as e:
        parts.append(f"dir_stat_error={e}")
    parts.append(f"dir_exists={directory.exists()}")
    parts.append(f"dir_writable={os.access(directory, os.W_OK)}")
    if path.exists():
        parts.append(f"file_writable={os.access(path, os.W_OK)}")
    return " ".join(parts)


async def init_db() -> None:
    """Creeaza schema si seed-uieste tracked_leagues din DEFAULT_LEAGUES."""
    from football_data import DEFAULT_LEAGUES  # import lazy — evita ciclul

    directory = Path(db_path()).parent
    if str(directory) not in ("", ".") and directory.exists() and not os.access(directory, os.W_OK):
        log.error("Directorul bazei de date nu e scriabil. %s", _db_dir_diagnostics())

    try:
        conn = await _connect()
    except Exception:
        log.error("Nu pot deschide baza de date. %s", _db_dir_diagnostics())
        raise
    try:
        for lid, name in DEFAULT_LEAGUES.items():
            await conn.execute(
                "INSERT OR IGNORE INTO tracked_leagues(league_id, name, added_by, active) "
                "VALUES(?, ?, 'default', 1)",
                (lid, name),
            )
        await conn.commit()
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Fixtures + change detection
# ---------------------------------------------------------------------------

async def upsert_fixture(fx: dict[str, Any], synced_at: str) -> list[tuple[str, Any, Any]]:
    """
    Insereaza/actualizeaza un meci. Returneaza lista de (field, old, new)
    pentru campurile urmarite care s-au schimbat; fiecare devine un rand
    in fixture_changes (NS→PST = amanat, kickoff diferit = reprogramat).
    """
    conn = await _connect()
    try:
        cur = await conn.execute(
            "SELECT * FROM fixtures WHERE fixture_id = ?", (fx["fixture_id"],)
        )
        existing = await cur.fetchone()

        changes: list[tuple[str, Any, Any]] = []
        if existing is not None:
            for field in WATCHED_FIELDS:
                old, new = existing[field], fx.get(field)
                if new is not None and old != new:
                    changes.append((field, old, new))
                    await conn.execute(
                        "INSERT INTO fixture_changes(fixture_id, changed_at, field, old_value, new_value) "
                        "VALUES(?, ?, ?, ?, ?)",
                        (fx["fixture_id"], synced_at, field,
                         None if old is None else str(old), str(new)),
                    )

        await conn.execute(
            """
            INSERT INTO fixtures(fixture_id, league_id, league_name, season,
                                 date_local, time_local, kickoff_iso, status, status_group,
                                 home_id, home_name, away_id, away_name,
                                 goals_home, goals_away, last_synced_at)
            VALUES(:fixture_id, :league_id, :league_name, :season,
                   :date_local, :time_local, :kickoff_iso, :status, :status_group,
                   :home_id, :home_name, :away_id, :away_name,
                   :goals_home, :goals_away, :last_synced_at)
            ON CONFLICT(fixture_id) DO UPDATE SET
                league_id=excluded.league_id, league_name=excluded.league_name,
                season=excluded.season, date_local=excluded.date_local,
                time_local=excluded.time_local, kickoff_iso=excluded.kickoff_iso,
                status=excluded.status, status_group=excluded.status_group,
                home_id=excluded.home_id, home_name=excluded.home_name,
                away_id=excluded.away_id, away_name=excluded.away_name,
                goals_home=excluded.goals_home, goals_away=excluded.goals_away,
                last_synced_at=excluded.last_synced_at
            """,
            {**fx, "last_synced_at": synced_at},
        )
        await conn.commit()
        return changes
    finally:
        await conn.close()


async def get_fixtures_for_days(days: list[str],
                                league_ids: Optional[list[int]] = None) -> list[dict]:
    if not days:
        return []
    conn = await _connect()
    try:
        query = (
            f"SELECT * FROM fixtures WHERE date_local IN ({','.join('?' * len(days))})"
        )
        params: list[Any] = list(days)
        if league_ids:
            query += f" AND league_id IN ({','.join('?' * len(league_ids))})"
            params += list(league_ids)
        query += " ORDER BY kickoff_iso"
        cur = await conn.execute(query, params)
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()


async def team_display_name(team_id: int) -> Optional[str]:
    """Numele echipei din fixture store (prima aparitie)."""
    conn = await _connect()
    try:
        cur = await conn.execute(
            "SELECT CASE WHEN home_id = ? THEN home_name ELSE away_name END AS name "
            "FROM fixtures WHERE home_id = ? OR away_id = ? LIMIT 1",
            (team_id, team_id, team_id),
        )
        row = await cur.fetchone()
        return row["name"] if row else None
    finally:
        await conn.close()


async def league_display_name(league_id: int) -> Optional[str]:
    conn = await _connect()
    try:
        cur = await conn.execute(
            "SELECT name FROM tracked_leagues WHERE league_id = ?", (league_id,))
        row = await cur.fetchone()
        if row and row["name"]:
            return row["name"]
        cur = await conn.execute(
            "SELECT league_name FROM fixtures WHERE league_id = ? LIMIT 1", (league_id,))
        row = await cur.fetchone()
        return row["league_name"] if row else None
    finally:
        await conn.close()


async def get_fixture(fixture_id: int) -> Optional[dict]:
    conn = await _connect()
    try:
        cur = await conn.execute("SELECT * FROM fixtures WHERE fixture_id = ?", (fixture_id,))
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await conn.close()


async def last_finished_fixture_before(team_id: int, date_local: str) -> Optional[dict]:
    """Ultimul meci TERMINAT al echipei inainte de o data (pentru zile de pauza)."""
    conn = await _connect()
    try:
        cur = await conn.execute(
            "SELECT * FROM fixtures "
            "WHERE (home_id = ? OR away_id = ?) AND date_local < ? AND status_group = 'finished' "
            "ORDER BY date_local DESC LIMIT 1",
            (team_id, team_id, date_local),
        )
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await conn.close()


async def team_fixtures_in_leagues(team_id: int, date_from: str, date_to: str,
                                   league_ids: list[int],
                                   exclude_fixture_id: Optional[int] = None) -> list[dict]:
    """Meciurile echipei in anumite ligi, intr-un interval de zile (local)."""
    conn = await _connect()
    try:
        query = (
            "SELECT * FROM fixtures "
            "WHERE (home_id = ? OR away_id = ?) AND date_local BETWEEN ? AND ? "
            f"AND league_id IN ({','.join('?' * len(league_ids))})"
        )
        params: list[Any] = [team_id, team_id, date_from, date_to, *league_ids]
        if exclude_fixture_id is not None:
            query += " AND fixture_id != ?"
            params.append(exclude_fixture_id)
        cur = await conn.execute(query, params)
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()


async def count_fixtures() -> int:
    conn = await _connect()
    try:
        cur = await conn.execute("SELECT COUNT(*) AS n FROM fixtures")
        row = await cur.fetchone()
        return int(row["n"])
    finally:
        await conn.close()


async def get_changes(date_from: str, date_to: str, limit: int = 100) -> list[dict]:
    """Schimbarile detectate (join cu numele echipelor), cele mai noi primele."""
    conn = await _connect()
    try:
        cur = await conn.execute(
            """
            SELECT c.id, c.fixture_id, c.changed_at, c.field, c.old_value, c.new_value,
                   f.home_name, f.away_name, f.league_name, f.date_local, f.time_local
            FROM fixture_changes c
            JOIN fixtures f ON f.fixture_id = c.fixture_id
            WHERE substr(c.changed_at, 1, 10) BETWEEN ? AND ?
            ORDER BY c.changed_at DESC
            LIMIT ?
            """,
            (date_from, date_to, limit),
        )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Acoperirea sincronizarii per zi
# ---------------------------------------------------------------------------

async def mark_day_synced(day: str, synced_at: str) -> None:
    conn = await _connect()
    try:
        await conn.execute(
            "INSERT INTO sync_days(day, last_synced_at) VALUES(?, ?) "
            "ON CONFLICT(day) DO UPDATE SET last_synced_at=excluded.last_synced_at",
            (day, synced_at),
        )
        await conn.commit()
    finally:
        await conn.close()


async def day_sync_info(days: list[str]) -> dict[str, str]:
    """{zi: last_synced_at} doar pentru zilele deja sincronizate."""
    if not days:
        return {}
    conn = await _connect()
    try:
        cur = await conn.execute(
            f"SELECT day, last_synced_at FROM sync_days WHERE day IN ({','.join('?' * len(days))})",
            days,
        )
        return {r["day"]: r["last_synced_at"] for r in await cur.fetchall()}
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Buget zilnic API
# ---------------------------------------------------------------------------

async def budget_get(day: str) -> int:
    conn = await _connect()
    try:
        cur = await conn.execute("SELECT requests_used FROM api_budget WHERE day = ?", (day,))
        row = await cur.fetchone()
        return int(row["requests_used"]) if row else 0
    finally:
        await conn.close()


async def budget_add(day: str, n: int = 1) -> int:
    """Incrementeaza contorul zilei si returneaza noua valoare."""
    conn = await _connect()
    try:
        await conn.execute(
            "INSERT INTO api_budget(day, requests_used) VALUES(?, ?) "
            "ON CONFLICT(day) DO UPDATE SET requests_used = requests_used + excluded.requests_used",
            (day, n),
        )
        await conn.commit()
        cur = await conn.execute("SELECT requests_used FROM api_budget WHERE day = ?", (day,))
        row = await cur.fetchone()
        return int(row["requests_used"])
    finally:
        await conn.close()


async def budget_floor(day: str, used: int) -> None:
    """Ridica contorul la cel putin `used` (cross-check cu headerul de rate limit)."""
    conn = await _connect()
    try:
        await conn.execute(
            "INSERT INTO api_budget(day, requests_used) VALUES(?, ?) "
            "ON CONFLICT(day) DO UPDATE SET requests_used = MAX(requests_used, excluded.requests_used)",
            (day, used),
        )
        await conn.commit()
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Ligi urmarite
# ---------------------------------------------------------------------------

async def get_tracked_leagues() -> dict[int, str]:
    """Ligile active urmarite: {league_id: name}."""
    conn = await _connect()
    try:
        cur = await conn.execute(
            "SELECT league_id, name FROM tracked_leagues WHERE active = 1"
        )
        return {int(r["league_id"]): r["name"] for r in await cur.fetchall()}
    finally:
        await conn.close()


async def add_tracked_league(league_id: int, name: str, added_by: str = "user") -> None:
    conn = await _connect()
    try:
        await conn.execute(
            "INSERT INTO tracked_leagues(league_id, name, added_by, active) VALUES(?, ?, ?, 1) "
            "ON CONFLICT(league_id) DO UPDATE SET active = 1, name = excluded.name",
            (league_id, name, added_by),
        )
        await conn.commit()
    finally:
        await conn.close()


async def count_tracked_leagues() -> int:
    conn = await _connect()
    try:
        cur = await conn.execute("SELECT COUNT(*) AS n FROM tracked_leagues WHERE active = 1")
        row = await cur.fetchone()
        return int(row["n"])
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Analize (V1-B) + jurnal de cost LLM
# ---------------------------------------------------------------------------

async def add_analysis(fixture_id: int, created_at: str, model: str, json_text: str) -> None:
    conn = await _connect()
    try:
        await conn.execute(
            "INSERT INTO analyses(fixture_id, created_at, model, json) VALUES(?, ?, ?, ?)",
            (fixture_id, created_at, model, json_text),
        )
        await conn.commit()
    finally:
        await conn.close()


async def latest_analysis(fixture_id: int) -> Optional[dict]:
    conn = await _connect()
    try:
        cur = await conn.execute(
            "SELECT * FROM analyses WHERE fixture_id = ? ORDER BY id DESC LIMIT 1",
            (fixture_id,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await conn.close()


async def count_analyses(fixture_id: Optional[int] = None) -> int:
    conn = await _connect()
    try:
        if fixture_id is None:
            cur = await conn.execute("SELECT COUNT(*) AS n FROM analyses")
        else:
            cur = await conn.execute("SELECT COUNT(*) AS n FROM analyses WHERE fixture_id = ?",
                                     (fixture_id,))
        row = await cur.fetchone()
        return int(row["n"])
    finally:
        await conn.close()


async def add_usage(turn_id: str, model: str, input_tokens: Optional[int],
                    output_tokens: Optional[int], created_at: str,
                    cache_read_tokens: Optional[int] = None,
                    cache_write_tokens: Optional[int] = None) -> None:
    conn = await _connect()
    try:
        await conn.execute(
            "INSERT INTO usage_log(turn_id, model, input_tokens, output_tokens, "
            "cache_read_tokens, cache_write_tokens, created_at) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (turn_id, model, input_tokens, output_tokens,
             cache_read_tokens, cache_write_tokens, created_at),
        )
        await conn.commit()
    finally:
        await conn.close()


async def save_ticket(conversation_id: Optional[str], ticket: dict,
                      risk_level: Optional[str] = None, created_at: str = "") -> int:
    """Persista un bilet prezentat + selectiile lui. Returneaza ticket_id."""
    conn = await _connect()
    try:
        cur = await conn.execute(
            "INSERT INTO tickets(conversation_id, created_at, target_odds, total_odds, "
            "est_probability, risk_level, status) VALUES(?, ?, ?, ?, ?, ?, 'open')",
            (conversation_id, created_at, ticket.get("target_odds"),
             ticket.get("total_odds"), ticket.get("estimated_probability"), risk_level),
        )
        ticket_id = cur.lastrowid
        for sel in ticket.get("selections", []):
            await conn.execute(
                "INSERT INTO ticket_selections(ticket_id, fixture_id, market, pick, "
                "odds, prob, confidence, result) VALUES(?, ?, ?, ?, ?, ?, ?, NULL)",
                (ticket_id, sel.get("fixture_id"), sel.get("market"), sel.get("pick"),
                 sel.get("odds"), sel.get("prob"), sel.get("confidence")),
            )
        await conn.commit()
        return int(ticket_id)
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Conversatii persistente + feedback (V1-D)
# ---------------------------------------------------------------------------

async def ensure_conversation(conversation_id: str, user_key: str,
                              title: str, now: str) -> None:
    """Creeaza conversatia daca nu exista; altfel doar actualizeaza updated_at.

    Titlul si user_key NU se suprascriu la conflict (titlul = primele 60 de
    caractere din PRIMUL mesaj al utilizatorului).
    """
    conn = await _connect()
    try:
        await conn.execute(
            "INSERT INTO conversations(id, user_key, title, created_at, updated_at) "
            "VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at",
            (conversation_id, user_key, title[:60], now, now),
        )
        await conn.commit()
    finally:
        await conn.close()


async def get_conversation(conversation_id: str) -> Optional[dict]:
    conn = await _connect()
    try:
        cur = await conn.execute("SELECT * FROM conversations WHERE id = ?",
                                 (conversation_id,))
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await conn.close()


async def touch_conversation(conversation_id: str, now: str) -> None:
    conn = await _connect()
    try:
        await conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?",
                           (now, conversation_id))
        await conn.commit()
    finally:
        await conn.close()


async def first_exchange(conversation_id: str) -> tuple[str, str]:
    """Primul mesaj al utilizatorului si primul raspuns (text) — pentru titlu."""
    import json as _json
    user_text = assistant_text = ""
    for m in await get_messages(conversation_id):
        if m["role"] == "user" and isinstance(m["content"], str) and not user_text:
            user_text = m["content"]
        elif m["role"] == "assistant" and isinstance(m["content"], list) and not assistant_text:
            assistant_text = "".join(
                b.get("text", "") for b in m["content"]
                if isinstance(b, dict) and b.get("type") == "text")
        if user_text and assistant_text:
            break
    return user_text, assistant_text


async def set_conversation_title(conversation_id: str, title: str,
                                 auto: bool = True) -> None:
    conn = await _connect()
    try:
        await conn.execute(
            "UPDATE conversations SET title = ?, title_auto = ? WHERE id = ?",
            (title[:60], 1 if auto else 0, conversation_id),
        )
        await conn.commit()
    finally:
        await conn.close()


async def list_conversations(user_key: str, limit: int = 50) -> list[dict]:
    """Conversatiile unui utilizator anonim, cele mai recente primele."""
    conn = await _connect()
    try:
        cur = await conn.execute(
            "SELECT id, title, title_auto, created_at, updated_at FROM conversations "
            "WHERE user_key = ? ORDER BY updated_at DESC, created_at DESC LIMIT ?",
            (user_key, limit),
        )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()


async def delete_conversation(conversation_id: str) -> None:
    """Sterge conversatia si mesajele ei. Biletele si feedback-ul RAMAN
    (pastreaza conversation_id ca referinta simpla pentru analytics)."""
    conn = await _connect()
    try:
        await conn.execute("DELETE FROM messages WHERE conversation_id = ?",
                           (conversation_id,))
        await conn.execute("DELETE FROM conversations WHERE id = ?",
                           (conversation_id,))
        await conn.commit()
    finally:
        await conn.close()


async def append_messages(conversation_id: str, msgs: list[dict], now: str,
                          turn_id: str | None = None) -> None:
    """Scrie mesaje (format Claude API: {"role", "content"}) in ordinea data."""
    if not msgs:
        return
    import json as _json
    conn = await _connect()
    try:
        for m in msgs:
            await conn.execute(
                "INSERT INTO messages(conversation_id, role, content_json, created_at, turn_id) "
                "VALUES(?, ?, ?, ?, ?)",
                (conversation_id, m["role"],
                 _json.dumps(m["content"], ensure_ascii=False, default=str), now, turn_id),
            )
        await conn.commit()
    finally:
        await conn.close()


async def messages_by_turn(turn_id: str) -> list[dict]:
    """Mesajele unei ture (pentru reconectare dupa ce hub-ul in-memory a expirat)."""
    import json as _json
    conn = await _connect()
    try:
        cur = await conn.execute(
            "SELECT conversation_id, role, content_json, turn_id FROM messages "
            "WHERE turn_id = ? ORDER BY id",
            (turn_id,),
        )
        out = []
        for r in await cur.fetchall():
            out.append({
                "conversation_id": r["conversation_id"],
                "role": r["role"],
                "content": _json.loads(r["content_json"]),
                "turn_id": r["turn_id"],
            })
        return out
    finally:
        await conn.close()


async def get_messages(conversation_id: str, with_turn_id: bool = False) -> list[dict]:
    """Istoricul complet al conversatiei, gata de trimis modelului."""
    import json as _json
    conn = await _connect()
    try:
        cur = await conn.execute(
            "SELECT role, content_json, turn_id FROM messages "
            "WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        )
        out = []
        for r in await cur.fetchall():
            msg = {"role": r["role"], "content": _json.loads(r["content_json"])}
            if with_turn_id:
                msg["turn_id"] = r["turn_id"]
            out.append(msg)
        return out
    finally:
        await conn.close()


async def count_messages(conversation_id: str) -> int:
    conn = await _connect()
    try:
        cur = await conn.execute(
            "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ?",
            (conversation_id,))
        row = await cur.fetchone()
        return int(row["n"])
    finally:
        await conn.close()


async def truncate_from_user_message(conversation_id: str, user_index: int) -> int:
    """Sterge tot de la al `user_index`-lea mesaj TEXT al utilizatorului incolo.

    Folosit la editarea unui mesaj deja trimis: conversatia se rescrie de la
    acel punct, exact ca in ChatGPT. Returneaza cate mesaje s-au sters.
    """
    import json as _json
    conn = await _connect()
    try:
        cur = await conn.execute(
            "SELECT id, role, content_json FROM messages "
            "WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        )
        rows = await cur.fetchall()
        seen = -1
        start = None
        for i, r in enumerate(rows):
            if r["role"] == "user" and isinstance(_json.loads(r["content_json"]), str):
                seen += 1
                if seen == user_index:
                    start = i
                    break
        if start is None:
            return 0
        ids = [rows[i]["id"] for i in range(start, len(rows))]
        await conn.execute(
            f"DELETE FROM messages WHERE id IN ({','.join('?' * len(ids))})", ids)
        await conn.commit()
        return len(ids)
    finally:
        await conn.close()


async def usage_for_turn(turn_id: str) -> list[dict]:
    """Randurile de cost ale unei ture (coordinator + analisti + titlu)."""
    conn = await _connect()
    try:
        cur = await conn.execute(
            "SELECT model, input_tokens, output_tokens, cache_read_tokens, "
            "cache_write_tokens, created_at FROM usage_log WHERE turn_id = ? ORDER BY id",
            (turn_id,),
        )
        return [dict(r) for r in await cur.fetchall()]
    finally:
        await conn.close()


async def trim_conversation_messages(conversation_id: str, max_messages: int) -> int:
    """Addition 13: plafoneaza mesajele stocate la max_messages.

    Cand se depaseste plafonul, sterge cel mai vechi bloc de mesaje, dar taie
    DOAR la granita unui mesaj user text (niciodata in mijlocul unei perechi
    tool_use/tool_result — rezultatele de tool sunt mesaje user cu continut
    lista, deci nu pot deveni primul mesaj ramas). Returneaza cate a sters.
    """
    import json as _json
    if max_messages <= 0:
        return 0
    conn = await _connect()
    try:
        cur = await conn.execute(
            "SELECT id, role, content_json FROM messages "
            "WHERE conversation_id = ? ORDER BY id",
            (conversation_id,),
        )
        rows = await cur.fetchall()
        n = len(rows)
        if n <= max_messages:
            return 0
        cut = n - max_messages
        while cut < n:
            r = rows[cut]
            if r["role"] == "user" and isinstance(_json.loads(r["content_json"]), str):
                break
            cut += 1
        if cut >= n:  # nicio granita sigura — nu taiem nimic
            return 0
        ids = [rows[i]["id"] for i in range(cut)]
        await conn.execute(
            f"DELETE FROM messages WHERE id IN ({','.join('?' * len(ids))})", ids)
        await conn.commit()
        return len(ids)
    finally:
        await conn.close()


async def upsert_feedback(conversation_id: str, message_ref: str, rating: str,
                          comment: Optional[str], ticket_id: Optional[int],
                          now: str) -> dict:
    """Un singur rating per mesaj: re-click actualizeaza randul existent."""
    conn = await _connect()
    try:
        await conn.execute(
            """
            INSERT INTO feedback(conversation_id, ticket_id, message_ref, rating,
                                 comment, created_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(conversation_id, message_ref) DO UPDATE SET
                rating = excluded.rating,
                ticket_id = COALESCE(excluded.ticket_id, feedback.ticket_id),
                comment = COALESCE(excluded.comment, feedback.comment),
                created_at = excluded.created_at
            """,
            (conversation_id, ticket_id, message_ref, rating, comment, now),
        )
        await conn.commit()
        cur = await conn.execute(
            "SELECT * FROM feedback WHERE conversation_id = ? AND message_ref = ?",
            (conversation_id, message_ref),
        )
        return dict(await cur.fetchone())
    finally:
        await conn.close()


async def count_conversations() -> int:
    conn = await _connect()
    try:
        cur = await conn.execute("SELECT COUNT(*) AS n FROM conversations")
        return int((await cur.fetchone())["n"])
    finally:
        await conn.close()


async def count_feedback() -> int:
    conn = await _connect()
    try:
        cur = await conn.execute("SELECT COUNT(*) AS n FROM feedback")
        return int((await cur.fetchone())["n"])
    finally:
        await conn.close()


async def get_user_tickets(user_key: str, cutoff_iso: str,
                           limit: int = 20) -> list[dict]:
    """Addition 11: biletele salvate ale unui utilizator (join pe conversatii),
    cu selectiile lor si numele meciurilor din fixture store."""
    conn = await _connect()
    try:
        cur = await conn.execute(
            """
            SELECT t.id, t.conversation_id, t.created_at, t.target_odds,
                   t.total_odds, t.est_probability, t.risk_level, t.status
            FROM tickets t
            JOIN conversations c ON c.id = t.conversation_id
            WHERE c.user_key = ? AND t.created_at >= ?
            ORDER BY t.id DESC LIMIT ?
            """,
            (user_key, cutoff_iso, limit),
        )
        tickets = [dict(r) for r in await cur.fetchall()]
        for t in tickets:
            cur = await conn.execute(
                """
                SELECT s.fixture_id, s.market, s.pick, s.odds, s.prob, s.confidence,
                       s.result, f.home_name, f.away_name, f.date_local, f.time_local
                FROM ticket_selections s
                LEFT JOIN fixtures f ON f.fixture_id = s.fixture_id
                WHERE s.ticket_id = ? ORDER BY s.id
                """,
                (t["id"],),
            )
            t["selections"] = [dict(r) for r in await cur.fetchall()]
        return tickets
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# Jurnal de sincronizare
# ---------------------------------------------------------------------------

async def add_sync_log(started_at: str, finished_at: str, dates_synced: list[str],
                       api_requests_used: int, ok: bool, error: Optional[str] = None) -> None:
    conn = await _connect()
    try:
        await conn.execute(
            "INSERT INTO sync_log(started_at, finished_at, dates_synced, api_requests_used, ok, error) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (started_at, finished_at, ",".join(dates_synced), api_requests_used,
             1 if ok else 0, error),
        )
        await conn.commit()
    finally:
        await conn.close()


async def latest_sync() -> Optional[dict]:
    conn = await _connect()
    try:
        cur = await conn.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1")
        row = await cur.fetchone()
        return dict(row) if row else None
    finally:
        await conn.close()
