"""
V1-F — poarta de acces cu parola comuna (testare privata).

Fara conturi de utilizator: o singura parola (env ACCESS_PASSWORD) deschide
aplicatia; fiecare browser isi pastreaza user_key-ul propriu din localStorage,
deci istoricul ramane separat per tester.

Sesiunea e un cookie HTTP-only semnat (BETMIND_SESSION), valabil 30 de zile:
    v1.<expira_la_epoch>.<hmac_sha256(SESSION_SECRET, "v1.<expira_la>")>
Nu stocam nimic pe server — verificarea e doar semnatura + termenul.

Daca ACCESS_PASSWORD nu e setat, aplicatia ruleaza deschis (dev local).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time

SESSION_COOKIE = "BETMIND_SESSION"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 3600  # 30 de zile

LOGIN_MAX_ATTEMPTS = 10          # incercari gresite permise...
LOGIN_WINDOW_SECONDS = 15 * 60   # ...per IP, in aceasta fereastra

# IP -> momentele incercarilor esuate (in-memory: suficient pentru 2-5 testeri;
# se goleste la restart, ceea ce e acceptabil pentru un rate-limit de login).
_failed_logins: dict[str, list[float]] = {}

# Fallback cand SESSION_SECRET lipseste: secret aleator per proces. Login-ul
# functioneaza, dar sesiunile nu supravietuiesc unui restart — de aceea
# main.py logheaza un avertisment clar la pornire.
_ephemeral_secret = secrets.token_hex(32)


def access_password() -> str:
    return os.environ.get("ACCESS_PASSWORD", "").strip()


def gate_enabled() -> bool:
    """Poarta e activa doar daca ACCESS_PASSWORD e setat (dev local = deschis)."""
    return bool(access_password())


def session_secret_set() -> bool:
    return bool(os.environ.get("SESSION_SECRET", "").strip())


def _secret() -> bytes:
    return (os.environ.get("SESSION_SECRET", "").strip() or _ephemeral_secret).encode()


def trust_proxy_headers() -> bool:
    return os.environ.get("TRUST_PROXY_HEADERS", "").strip().lower() in ("1", "true", "yes")


def password_matches(candidate: str) -> bool:
    return secrets.compare_digest(candidate.encode(), access_password().encode())


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()


def make_session_cookie(now: float | None = None) -> str:
    expires_at = int(now if now is not None else time.time()) + SESSION_MAX_AGE_SECONDS
    payload = f"v1.{expires_at}"
    return f"{payload}.{_sign(payload)}"


def verify_session_cookie(value: str | None, now: float | None = None) -> bool:
    if not value:
        return False
    parts = value.split(".")
    if len(parts) != 3 or parts[0] != "v1" or not parts[1].isdigit():
        return False
    payload = f"{parts[0]}.{parts[1]}"
    if not hmac.compare_digest(_sign(payload), parts[2]):
        return False
    return int(parts[1]) > (now if now is not None else time.time())


def client_ip(request) -> str:
    """IP-ul clientului; in spatele unui proxy (Railway) citim X-Forwarded-For
    doar daca TRUST_PROXY_HEADERS=true — altfel header-ul poate fi falsificat."""
    if trust_proxy_headers():
        forwarded = request.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def login_rate_limited(ip: str, now: float | None = None) -> bool:
    now = now if now is not None else time.time()
    window = [t for t in _failed_logins.get(ip, []) if now - t < LOGIN_WINDOW_SECONDS]
    if window:
        _failed_logins[ip] = window
    else:
        _failed_logins.pop(ip, None)
    return len(window) >= LOGIN_MAX_ATTEMPTS


def record_failed_login(ip: str, now: float | None = None) -> None:
    _failed_logins.setdefault(ip, []).append(now if now is not None else time.time())


def clear_failed_logins(ip: str | None = None) -> None:
    """La login reusit (sau in teste): resetam contorul IP-ului / totul."""
    if ip is None:
        _failed_logins.clear()
    else:
        _failed_logins.pop(ip, None)
