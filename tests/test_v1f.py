"""
V1-F — poarta de acces cu parola comuna + pregatirea de deploy.

Acceptanta:
  (a) cu ACCESS_PASSWORD setat: / -> redirect la /login, /api/chat -> 401;
      dupa login corect chat-ul functioneaza normal;
  (b) fara ACCESS_PASSWORD: totul merge ca azi (dev local neafectat);
  (c) incercarile cu parola gresita sunt rate-limited;
  (d) health raporteaza disk_writable si db_path-ul rezolvat;
  (+) cookie falsificat/expirat respins, IP din X-Forwarded-For doar cu
      TRUST_PROXY_HEADERS=true, rezolvarea DB_PATH/DATA_DIR.
"""

from __future__ import annotations

import httpx
import pytest

import auth
import db

PASSWORD = "parola-secreta-test"


@pytest.fixture
def gated(monkeypatch):
    """Porneste poarta de acces cu o parola si un secret cunoscute."""
    monkeypatch.setenv("ACCESS_PASSWORD", PASSWORD)
    monkeypatch.setenv("SESSION_SECRET", "un-secret-suficient-de-lung-pentru-teste")
    auth.clear_failed_logins()
    yield
    auth.clear_failed_logins()


def make_client(app) -> httpx.AsyncClient:
    # base_url https: cookie-ul de sesiune e Secure, altfel clientul nu l-ar retrimite.
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test")


async def do_login(client: httpx.AsyncClient, password: str = PASSWORD) -> httpx.Response:
    return await client.post("/api/login", json={"password": password})


# ---------------------------------------------------------------- (a) gate on

async def test_unauthenticated_html_redirects_to_login(gated, no_http):
    import main
    async with make_client(main.app) as client:
        resp = await client.get("/")
    assert resp.status_code == 302
    assert resp.headers["location"] == "/login"


async def test_unauthenticated_api_gets_401(gated, no_http):
    import main
    async with make_client(main.app) as client:
        chat = await client.post("/api/chat", json={"message": "salut"})
        convs = await client.get("/api/conversations", params={"user_key": "u1"})
    assert chat.status_code == 401
    assert convs.status_code == 401


async def test_login_page_and_health_stay_open(gated, no_http):
    import main
    await db.init_db()
    async with make_client(main.app) as client:
        login = await client.get("/login")
        health = await client.get("/api/health")
    assert login.status_code == 200
    assert "Parola de acces" in login.text
    assert health.status_code == 200


async def test_correct_login_unlocks_app(gated, no_http):
    import main
    await db.init_db()
    async with make_client(main.app) as client:
        resp = await do_login(client)
        assert resp.status_code == 200
        cookie = resp.headers.get("set-cookie", "")
        assert auth.SESSION_COOKIE in cookie
        assert "HttpOnly" in cookie and "Secure" in cookie and "lax" in cookie.lower()

        # Cookie-ul retinut de client deblocheaza si HTML-ul, si API-ul.
        index = await client.get("/")
        convs = await client.get("/api/conversations", params={"user_key": "u1"})
        login_page = await client.get("/login")
    assert index.status_code == 200
    assert convs.status_code == 200
    assert login_page.status_code == 302  # deja logat -> inapoi la chat


async def test_wrong_password_rejected(gated, no_http):
    import main
    async with make_client(main.app) as client:
        resp = await do_login(client, "parola-gresita")
    assert resp.status_code == 401


async def test_tampered_or_garbage_cookie_rejected(gated, no_http):
    import main
    valid = auth.make_session_cookie()
    payload, sig = valid.rsplit(".", 1)
    tampered = f"{payload}.{'0' * len(sig)}"
    for bad in (tampered, "v1.123.abc", "gunoaie", ""):
        async with make_client(main.app) as client:
            client.cookies.set(auth.SESSION_COOKIE, bad, domain="test")
            resp = await client.get("/api/conversations", params={"user_key": "u1"})
        assert resp.status_code == 401, f"cookie acceptat gresit: {bad!r}"


async def test_expired_cookie_rejected(gated):
    # Emis acum 31 de zile -> peste termenul de 30 de zile.
    old = auth.make_session_cookie(now=0)
    assert auth.verify_session_cookie(old, now=auth.SESSION_MAX_AGE_SECONDS + 86_400) is False
    assert auth.verify_session_cookie(auth.make_session_cookie()) is True


# --------------------------------------------------------------- (b) gate off

async def test_without_password_everything_open(no_http):
    import main
    await db.init_db()
    async with make_client(main.app) as client:
        index = await client.get("/")
        convs = await client.get("/api/conversations", params={"user_key": "u1"})
        login_page = await client.get("/login")
        login_api = await do_login(client, "orice")
    assert index.status_code == 200
    assert convs.status_code == 200
    assert login_page.status_code == 302  # poarta oprita -> direct la chat
    assert login_api.status_code == 200   # nimic de deblocat


# -------------------------------------------------------------- (c) rate limit

async def test_login_rate_limited_per_ip(gated, no_http):
    import main
    async with make_client(main.app) as client:
        for _ in range(auth.LOGIN_MAX_ATTEMPTS):
            resp = await do_login(client, "gresit")
            assert resp.status_code == 401
        # A 11-a incercare e blocata — chiar si cu parola corecta.
        blocked = await do_login(client, "gresit")
        blocked_correct = await do_login(client)
    assert blocked.status_code == 429
    assert blocked_correct.status_code == 429


async def test_rate_limit_window_expires(gated):
    ip = "10.0.0.1"
    for _ in range(auth.LOGIN_MAX_ATTEMPTS):
        auth.record_failed_login(ip, now=1000.0)
    assert auth.login_rate_limited(ip, now=1000.0) is True
    # Dupa fereastra de 15 minute, incercarile vechi nu mai conteaza.
    assert auth.login_rate_limited(ip, now=1000.0 + auth.LOGIN_WINDOW_SECONDS + 1) is False


async def test_forwarded_for_used_only_with_trust_proxy(gated, no_http, monkeypatch):
    import main

    # Fara TRUST_PROXY_HEADERS: header-ul e ignorat, toate incercarile cad pe
    # acelasi IP de transport -> a 11-a e blocata desi X-Forwarded-For difera.
    async with make_client(main.app) as client:
        for i in range(auth.LOGIN_MAX_ATTEMPTS):
            await client.post("/api/login", json={"password": "gresit"},
                              headers={"X-Forwarded-For": f"1.2.3.{i}"})
        resp = await client.post("/api/login", json={"password": "gresit"},
                                 headers={"X-Forwarded-For": "9.9.9.9"})
    assert resp.status_code == 429

    # Cu TRUST_PROXY_HEADERS=true: fiecare IP forwardat are contorul lui.
    auth.clear_failed_logins()
    monkeypatch.setenv("TRUST_PROXY_HEADERS", "true")
    async with make_client(main.app) as client:
        for i in range(auth.LOGIN_MAX_ATTEMPTS + 1):
            resp_a = await client.post("/api/login", json={"password": "gresit"},
                                       headers={"X-Forwarded-For": "1.1.1.1, 10.0.0.1"})
        resp_b = await client.post("/api/login", json={"password": "gresit"},
                                   headers={"X-Forwarded-For": "2.2.2.2"})
    assert resp_a.status_code == 429   # IP-ul 1.1.1.1 si-a epuizat incercarile
    assert resp_b.status_code == 401   # IP-ul 2.2.2.2 abia incepe


# ------------------------------------------------------------------ (d) health

async def test_health_reports_env_db_path_disk(no_http, tmp_path, monkeypatch):
    import main
    await db.init_db()
    async with make_client(main.app) as client:
        body = (await client.get("/api/health")).json()
    assert body["env"] == "local"
    assert body["db_path"].endswith("test.db")
    assert body["disk_writable"] is True
    assert body["access_gate_enabled"] is False

    monkeypatch.setenv("ACCESS_PASSWORD", PASSWORD)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "vol"))
    async with make_client(main.app) as client:
        body = (await client.get("/api/health")).json()
    assert body["env"] == "production"
    assert body["access_gate_enabled"] is True
    assert body["disk_writable"] is True


# ------------------------------------------------------- rezolvarea caii DB

async def test_db_path_resolution(monkeypatch, tmp_path):
    # DB_PATH explicit are prioritate (asa ruleaza si testele).
    assert db.db_path().endswith("test.db")

    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "vol"))
    assert db.db_path() == str(tmp_path / "vol" / "betmind.db")

    monkeypatch.delenv("DATA_DIR", raising=False)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    assert db.db_path().replace("\\", "/") == "/data/betmind.db"

    monkeypatch.delenv("RAILWAY_ENVIRONMENT", raising=False)
    assert db.db_path() == "data/betmind.db"


async def test_db_directory_created_on_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "adanc" / "nou" / "b.db"))
    await db.init_db()
    assert (tmp_path / "adanc" / "nou" / "b.db").exists()


async def test_db_diagnostics_report_path_uid_and_permissions(tmp_path, monkeypatch):
    """Diagnosticul pentru «unable to open database file» (volum montat de root)
    trebuie sa spuna: ce cale, ce UID si ce permisiuni are directorul."""
    monkeypatch.setenv("DB_PATH", str(tmp_path / "vol" / "betmind.db"))
    (tmp_path / "vol").mkdir()

    report = db._db_dir_diagnostics()

    assert "db_path=" in report and "betmind.db" in report
    assert "uid=" in report and "gid=" in report
    assert "dir_writable=True" in report
    assert "dir_exists=True" in report


async def test_init_db_logs_diagnostics_before_raising(monkeypatch, caplog):
    """Daca deschiderea bazei esueaza, eroarea nu pleaca 'goala': in log ramane
    contextul necesar (cale, UID, permisiuni)."""
    async def _boom():
        raise db.aiosqlite.OperationalError("unable to open database file")

    monkeypatch.setattr(db, "_connect", _boom)

    with caplog.at_level("ERROR", logger="betmind.db"):
        with pytest.raises(db.aiosqlite.OperationalError):
            await db.init_db()

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "Nu pot deschide baza de date" in logged
    assert "db_path=" in logged and "uid=" in logged and "dir_writable=" in logged
