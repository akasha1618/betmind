"""
BetMind — server FastAPI.

Endpoints:
  GET    /                        -> UI-ul de chat (static/index.html)
  POST   /api/chat                -> raspuns SSE (streaming); primul eveniment
                                     este {"type":"meta","conversation_id":...}
  GET    /api/conversations       -> lista conversatiilor unui user_key
  GET    /api/conversations/{id}  -> mesajele unei conversatii (pentru redare)
  DELETE /api/conversations/{id}  -> sterge conversatia + mesajele ei
  POST   /api/feedback            -> rating pozitiv/negativ + comentariu per mesaj
  GET    /api/config              -> ce poate face interfata (mod, Premium, limite)
  GET    /api/usage/{turn_id}     -> costul unei ture (modul dezvoltator)
  GET    /api/health              -> status chei/model, pentru debugging rapid

Porneste cu:  python main.py   (sau: uvicorn main:app --reload)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from dotenv import load_dotenv

load_dotenv()  # inainte de importurile care citesc env

from logging_config import setup_logging

log = setup_logging()

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse,
                               Response, StreamingResponse)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import agent
import analysts
import auth
import db
import football_data as fd
import pricing
import sync
import titles

BASE_DIR = Path(__file__).parent
DEBUG = os.environ.get("DEBUG", "").strip().lower() in ("1", "true", "yes")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # V1-F: avertismente clare (nu crash) pentru configuratia de productie.
    if not auth.gate_enabled():
        log.warning("ACCESS_PASSWORD nesetat — aplicația rulează DESCHISĂ "
                    "(ok pentru dev local, nu pentru un URL public).")
    if not auth.session_secret_set():
        log.warning("SESSION_SECRET nesetat — folosesc un secret temporar per "
                    "proces; sesiunile de login NU supraviețuiesc unui restart.")
    await db.init_db()
    sync_task: asyncio.Task | None = None
    if sync.sync_enabled():
        # Non-blocking: hot sync imediat + bucla periodica ruleaza in fundal.
        sync_task = asyncio.create_task(sync.sync_loop(), name="betmind-sync")
        log.info("Background sync pornit (SYNC_ENABLED=true)")
    else:
        log.info("Background sync dezactivat (SYNC_ENABLED=false)")
    yield
    if sync_task:
        sync_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await sync_task


app = FastAPI(title="BetMind", lifespan=lifespan)


# --- V1-F: poarta de acces cu parola comuna -------------------------------
# Middleware ASGI pur (nu BaseHTTPMiddleware): nu atinge fluxul SSE al
# /api/chat, doar decide inainte de rutare daca cererea trece sau nu.

_GATE_EXEMPT_PATHS = {"/login", "/api/login", "/api/health", "/favicon.ico"}


def _gate_exempt(path: str) -> bool:
    return path in _GATE_EXEMPT_PATHS or path.startswith("/static/")


class AccessGateMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not auth.gate_enabled() or _gate_exempt(scope["path"]):
            return await self.app(scope, receive, send)
        request = Request(scope)
        if auth.verify_session_cookie(request.cookies.get(auth.SESSION_COOKIE)):
            return await self.app(scope, receive, send)
        if scope["path"].startswith("/api/"):
            response = JSONResponse({"detail": "Autentificare necesară."}, status_code=401)
        else:
            response = RedirectResponse("/login", status_code=302)
        await response(scope, receive, send)


app.add_middleware(AccessGateMiddleware)

# V1-D: cache write-through peste DB — conversation_id -> mesaje (format Claude
# API). Sursa de adevar e SQLite (tabelele conversations/messages); dict-ul doar
# evita recitirea istoricului la fiecare mesaj. La restart se reincarca din DB.
SESSIONS: dict[str, list[dict]] = {}
MAX_HISTORY_MESSAGES = 60

# Task-uri detasate (titluri automate): pastram referinta ca sa nu le curete GC.
_BACKGROUND: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    """Porneste o corutina in fundal, fara sa blocheze raspunsul."""
    task = asyncio.create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


def max_stored_messages() -> int:
    """Addition 13: plafonul de mesaje stocate per conversatie."""
    try:
        return int(os.environ.get("MAX_STORED_MESSAGES", "200"))
    except ValueError:
        return 200


def premium_gating() -> bool:
    """Daca e ON, Advanced Mode si limitele de request-uri cer cont Premium.
    Implicit OFF — abonamentele nu sunt inca active."""
    return os.environ.get("PREMIUM_GATING", "").strip().lower() in ("1", "true", "yes")


def request_limits_enabled() -> bool:
    """Limitarea numarului de intrebari per utilizator — deocamdata oprita."""
    return os.environ.get("REQUEST_LIMITS_ENABLED", "").strip().lower() in ("1", "true", "yes")


class ChatRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None
    user_key: Optional[str] = None
    # Compatibilitate cu clientii vechi: {session_id, message}. Daca lipseste
    # conversation_id, session_id devine id-ul conversatiei (continuitate).
    session_id: Optional[str] = None
    # "advanced" (analiza in paralel) sau "standard"; lipsa = valoarea din .env.
    mode: Optional[str] = None
    # Editarea unui mesaj deja trimis: se sterge tot de la al N-lea mesaj al
    # utilizatorului incolo, apoi conversatia continua cu textul nou.
    edit_from_index: Optional[int] = None


class FeedbackRequest(BaseModel):
    conversation_id: str
    message_ref: str
    rating: Literal["up", "down"]
    comment: Optional[str] = None
    ticket_id: Optional[int] = None


class LoginRequest(BaseModel):
    password: str


@app.get("/login")
async def login_page(request: Request):
    """Pagina cu parola de acces. Daca poarta e oprita sau esti deja logat,
    nu are sens sa o vezi — mergi direct la chat."""
    if (not auth.gate_enabled()
            or auth.verify_session_cookie(request.cookies.get(auth.SESSION_COOKIE))):
        return RedirectResponse("/", status_code=302)
    return FileResponse(BASE_DIR / "static" / "login.html",
                        headers={"Cache-Control": "no-store"})


@app.post("/api/login")
async def api_login(body: LoginRequest, request: Request):
    if not auth.gate_enabled():
        return {"ok": True}  # poarta oprita: nimic de deblocat
    ip = auth.client_ip(request)
    if auth.login_rate_limited(ip):
        raise HTTPException(status_code=429,
                            detail="Prea multe încercări. Reîncearcă peste ~15 minute.")
    if not auth.password_matches(body.password):
        auth.record_failed_login(ip)
        raise HTTPException(status_code=401, detail="Parolă greșită.")
    auth.clear_failed_logins(ip)
    response = JSONResponse({"ok": True})
    response.set_cookie(
        auth.SESSION_COOKIE, auth.make_session_cookie(),
        max_age=auth.SESSION_MAX_AGE_SECONDS,
        httponly=True, secure=True, samesite="lax", path="/",
    )
    return response


def _disk_writable() -> bool:
    """Test-scriere in directorul DB-ului — confirma rapid ca volumul e montat."""
    directory = Path(db.db_path()).parent
    probe = directory / f".writable-{uuid.uuid4().hex[:8]}"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


@app.get("/api/health")
async def health():
    last = await db.latest_sync()
    return {
        "ok": True,
        "debug": DEBUG,
        # V1-F: verificarea rapida a deploy-ului (volum montat? poarta activa?)
        "env": "production" if (os.environ.get("RAILWAY_ENVIRONMENT", "").strip()
                                or os.environ.get("DATA_DIR", "").strip()) else "local",
        "db_path": db.db_path(),
        "disk_writable": _disk_writable(),
        "access_gate_enabled": auth.gate_enabled(),
        "model": agent.MODEL,
        "anthropic_key_set": bool(os.environ.get("ANTHROPIC_API_KEY", "").strip()),
        "anthropic_base_url": os.environ.get("ANTHROPIC_BASE_URL", "").strip() or "https://api.anthropic.com",
        "api_football_key_set": bool(os.environ.get("API_FOOTBALL_KEY", "").strip()),
        "api_football_requests_remaining_today": fd.requests_remaining(),
        "active_sessions": len(SESSIONS),
        # V1-A: metadata fixture store & sync
        "sync_enabled": sync.sync_enabled(),
        "last_sync_at": last["finished_at"] if last else None,
        "sync_ok": bool(last["ok"]) if last else None,
        "api_requests_used_today": await fd.requests_used_today(),
        "budget_limit": fd.max_daily_requests(),
        "tracked_leagues_count": await db.count_tracked_leagues(),
        "fixtures_in_db": await db.count_fixtures(),
        "timezone": fd.app_timezone_name(),
        # V1-D: persistenta conversatiilor + feedback
        "conversations_count": await db.count_conversations(),
        "feedback_count": await db.count_feedback(),
        # Diagnostic API-Football: de ce esueaza cererile de cote in productie.
        "odds_errors_last_hour": fd.api_errors_grouped("/odds", hours=1),
        "api_errors_last_hour": fd.api_errors_grouped(hours=1),
        "last_odds_error": fd.last_api_error("/odds"),
        "last_api_error": fd.last_api_error(),
        "rate_limit_per_minute": fd.rate_limit_per_minute(),
        "rate_limiter_active": fd.rate_limiter_active(),
        "last_rate_headers": fd.last_rate_headers(),
        "max_parallel_analysts": analysts.max_parallel_analysts(),
        "odds_cache_entries": sum(1 for k in fd._cache if k.startswith("/odds")),
        "cache_entries": len(fd._cache),
    }


@app.get("/api/debug/anthropic")
async def debug_anthropic():
    """Smoke test Anthropic — disponibil doar cu DEBUG=1 in .env."""
    if not DEBUG:
        raise HTTPException(status_code=404, detail="Setează DEBUG=1 în .env pentru a activa endpoint-ul de debug.")
    log.info("Running Anthropic ping test")
    result = await agent.ping_anthropic()
    log.info("Anthropic ping result: %s", result)
    return result


def _sanitize_loaded_history(msgs: list[dict]) -> list[dict]:
    """La incarcarea din DB: daca ultimul mesaj e un assistant cu tool_use fara
    tool_result (tura intrerupta), il scoatem ca sa nu trimitem modelului o
    pereche rupta."""
    while msgs:
        last = msgs[-1]
        if (last["role"] == "assistant" and isinstance(last["content"], list)
                and any(b.get("type") == "tool_use" for b in last["content"])):
            msgs.pop()
        else:
            break
    return msgs


def _finalize_interrupted(history: list[dict], partial_text: str) -> None:
    """Repara istoricul dupa o tura oprita de utilizator (butonul Stop).

    Ce s-a scris pe ecran se pastreaza, dar niciodata o pereche rupta: un
    mesaj assistant cu cerere de tool fara raspunsul ei ar bloca tura urmatoare.
    """
    if (history and history[-1]["role"] == "assistant"
            and isinstance(history[-1]["content"], list)
            and any(b.get("type") == "tool_use" for b in history[-1]["content"])):
        history.pop()
    if partial_text.strip() and (not history or history[-1]["role"] != "assistant"):
        history.append({"role": "assistant",
                        "content": [{"type": "text", "text": partial_text.strip()}]})


async def _persist_turn(conv_id: str, history: list[dict], start_len: int,
                        partial_text: str, first_turn: bool, user_text: str,
                        turn_id: str) -> None:
    """Write-through dupa tura: mesaje noi -> DB, plafon, titlu automat."""
    try:
        _finalize_interrupted(history, partial_text)
        ts = fd.now_local().isoformat(timespec="seconds")
        new_msgs = history[start_len:]
        if new_msgs:
            await db.append_messages(conv_id, new_msgs, ts, turn_id)
        await db.touch_conversation(conv_id, ts)
        await db.trim_conversation_messages(conv_id, max_stored_messages())
    except Exception:
        log.exception("Persistarea conversatiei %s a esuat", conv_id)

    # Titlul automat: se incearca la fiecare tura cat timp conversatia inca are
    # titlul provizoriu (prima tura oprita sau esuata nu ramane fara titlu).
    reply = ""
    for m in history[start_len:]:
        if m["role"] == "assistant" and isinstance(m["content"], list):
            reply += "".join(b.get("text", "") for b in m["content"]
                             if isinstance(b, dict) and b.get("type") == "text")
    if not first_turn:
        first_user, first_reply = await db.first_exchange(conv_id)
        if first_user:
            user_text, reply = first_user, first_reply
    # Detasat: titlul se genereaza dupa ce raspunsul a plecat, ca sa nu tina
    # conexiunea deschisa nici macar o secunda in plus.
    _spawn(titles.maybe_title_conversation(conv_id, user_text, reply, turn_id))


@app.post("/api/chat")
async def chat(req: ChatRequest):
    text = req.message.strip()
    user_key = (req.user_key or req.session_id or "anon").strip() or "anon"
    conv_id = (req.conversation_id or req.session_id or "").strip() or uuid.uuid4().hex
    turn_id = uuid.uuid4().hex

    # Advanced Mode: cerut din interfata, dar respectand eventuala restrictie
    # Premium (implicit dezactivata — vezi PREMIUM_GATING).
    requested_mode = analysts.normalize_mode(req.mode)
    premium_required = bool(requested_mode == "analysts" and premium_gating())
    active_mode = "classic" if premium_required else requested_mode

    log.info("Chat request conv=%s user=%s mode=%s message_len=%d preview=%r",
             conv_id, user_key, active_mode or "env", len(text), text[:80])
    if not text:
        async def _empty():
            yield _sse({"type": "error", "message": "Mesajul este gol."})
        return StreamingResponse(_empty(), media_type="text/event-stream")

    now = fd.now_local().isoformat(timespec="seconds")

    # Editarea unui mesaj trimis: conversatia se rescrie de la acel punct.
    if req.edit_from_index is not None and req.edit_from_index >= 0:
        await db.truncate_from_user_message(conv_id, req.edit_from_index)
        SESSIONS.pop(conv_id, None)
        if req.edit_from_index == 0:
            # Primul mesaj s-a schimbat -> titlul se regenereaza.
            await db.set_conversation_title(conv_id, text[:60], auto=False)

    # Titlul se seteaza doar la creare (primele 60 caractere din primul mesaj);
    # dupa prima tura e inlocuit de unul generat automat.
    await db.ensure_conversation(conv_id, user_key, text[:60], now)

    history = SESSIONS.get(conv_id)
    if history is None:
        history = _sanitize_loaded_history(await db.get_messages(conv_id))
        SESSIONS[conv_id] = history

    # Contextul trimis modelului ramane plafonat: taiem de la inceput, dar
    # niciodata in mijlocul unei perechi tool_use/tool_result.
    if len(history) > MAX_HISTORY_MESSAGES:
        cut = len(history) - MAX_HISTORY_MESSAGES
        while cut < len(history):
            m = history[cut]
            if m["role"] == "user" and isinstance(m["content"], str):
                break
            cut += 1
        del history[:cut]

    history.append({"role": "user", "content": text})
    await db.append_messages(conv_id, [{"role": "user", "content": text}], now)
    first_turn = len(history) == 1

    async def event_stream():
        # Diagnostic: apelurile API-Football din aceasta tura (inclusiv cele
        # facute de analistii din task-uri paralele) se contorizeaza pe turn_id.
        fd.set_current_turn(turn_id)
        # Contract SSE compatibil: eveniment nou aditiv "meta" (clientii vechi
        # ignora tipurile necunoscute).
        yield _sse({"type": "meta", "conversation_id": conv_id, "turn_id": turn_id,
                    "mode": active_mode or analysts.orchestration_mode(),
                    "premium_required": premium_required})
        start_len = len(history)
        partial: list[str] = []
        try:
            async for event in agent.run_turn(history, conversation_id=conv_id,
                                              user_key=user_key, mode=active_mode,
                                              turn_id=turn_id):
                if event.get("type") == "delta":
                    partial.append(event.get("text", ""))
                elif event.get("type") == "error":
                    log.error("Chat error event: %s", event.get("message"))
                yield _sse(event)
            # Costul turei (pentru modul dezvoltator din interfata).
            with contextlib.suppress(Exception):
                yield _sse({"type": "usage", "turn_id": turn_id,
                            **pricing.summarize(await db.usage_for_turn(turn_id)),
                            "api": fd.turn_api_stats(turn_id)})
        except Exception as e:
            log.exception("Chat stream failed")
            yield _sse({"type": "error", "message": f"Eroare de server: {type(e).__name__}: {e}"})
        finally:
            # Write-through, protejat de anulare: daca utilizatorul apasa Stop,
            # conexiunea moare, dar ce s-a produs pana atunci tot se salveaza.
            task = asyncio.create_task(_persist_turn(
                conv_id, history, start_len, "".join(partial),
                first_turn, text, turn_id))
            with contextlib.suppress(Exception):
                await asyncio.shield(task)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _render_messages(msgs: list[dict]) -> list[dict]:
    """Mesajele vizibile pentru UI: user text + textul mesajelor assistant
    (blocurile tool_use/tool_result nu se afiseaza). `turn_id` merge mai
    departe ca sa poata fi afisat costul intrebarii in modul dezvoltator."""
    out: list[dict] = []
    for m in msgs:
        content = m["content"]
        if m["role"] == "user" and isinstance(content, str):
            out.append({"role": "user", "text": content})
        elif m["role"] == "assistant" and isinstance(content, list):
            text = "".join(b.get("text", "") for b in content
                           if isinstance(b, dict) and b.get("type") == "text").strip()
            if text:
                out.append({"role": "assistant", "text": text,
                            "turn_id": m.get("turn_id")})
    return out


# Cate conversatii fara titlu generat rezolvam la o singura listare (apeluri
# ieftine, in fundal — evitam o rafala cand istoricul e lung).
_TITLE_BACKFILL_PER_REQUEST = 3


@app.get("/api/conversations")
async def list_conversations(user_key: str = ""):
    rows = await db.list_conversations((user_key or "anon").strip() or "anon")
    # Conversatiile mai vechi (create inainte de titlurile automate sau ramase
    # cu titlul provizoriu) primesc unul in fundal; apare la urmatoarea listare.
    if titles.auto_title_enabled():
        pending = [r for r in rows if not r.get("title_auto")][:_TITLE_BACKFILL_PER_REQUEST]
        for r in pending:
            _spawn(_backfill_title(r["id"]))
    return {"conversations": rows}


async def _backfill_title(conversation_id: str) -> None:
    user_text, reply = await db.first_exchange(conversation_id)
    if user_text:
        await titles.maybe_title_conversation(conversation_id, user_text, reply)


@app.get("/api/conversations/{conversation_id}")
async def get_conversation(conversation_id: str):
    conv = await db.get_conversation(conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversația nu există.")
    msgs = await db.get_messages(conversation_id, with_turn_id=True)
    return {
        "conversation_id": conv["id"],
        "title": conv["title"],
        "created_at": conv["created_at"],
        "updated_at": conv["updated_at"],
        "messages": _render_messages(msgs),
    }


@app.delete("/api/conversations/{conversation_id}")
async def delete_conversation(conversation_id: str):
    # Sterge conversatia + mesajele; biletele si feedback-ul raman (analytics).
    await db.delete_conversation(conversation_id)
    SESSIONS.pop(conversation_id, None)
    return {"ok": True}


@app.get("/api/config")
async def config():
    """Ce poate face interfata: modul implicit, starea Premium, limitele."""
    return {
        "default_mode": analysts.orchestration_mode(),
        "premium_gating": premium_gating(),
        "premium_active": False,        # abonamentele nu sunt inca implementate
        "request_limits_enabled": request_limits_enabled(),
    }


@app.get("/api/usage/{turn_id}")
async def usage(turn_id: str):
    """Costul unei ture (toate apelurile Claude cu acelasi turn_id) plus ce s-a
    intamplat cu API-Football in tura respectiva (diagnostic pentru modul dev)."""
    rows = await db.usage_for_turn(turn_id)
    return {"turn_id": turn_id, **pricing.summarize(rows),
            "api": fd.turn_api_stats(turn_id)}


@app.post("/api/feedback")
async def feedback(req: FeedbackRequest):
    row = await db.upsert_feedback(
        req.conversation_id, req.message_ref, req.rating,
        req.comment, req.ticket_id,
        fd.now_local().isoformat(timespec="seconds"),
    )
    return {"ok": True, "feedback_id": row["id"], "rating": row["rating"]}


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@app.get("/")
async def index():
    # no-store: dupa fiecare modificare a interfetei, reincarcarea aduce
    # varianta noua (altfel browserul poate servi un UI vechi din cache).
    return FileResponse(BASE_DIR / "static" / "index.html",
                        headers={"Cache-Control": "no-store"})


@app.get("/favicon.ico")
async def favicon():
    """Evită 404-ul din browser — nu e legat de erorile Anthropic."""
    return Response(status_code=204)


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


def _startup_report():
    print("=" * 56)
    print("  BetMind — recomandări AI pentru bilete de fotbal")
    print("=" * 56)
    for key, hint in [
        ("ANTHROPIC_API_KEY", "https://console.anthropic.com/settings/keys"),
        ("API_FOOTBALL_KEY", "https://dashboard.api-football.com"),
    ]:
        status = "OK" if os.environ.get(key, "").strip() else f"LIPSĂ  -> obține de la {hint}"
        print(f"  {key:<20} {status}")
    print(f"  Model Claude: {agent.MODEL}")
    print(f"  Buget API zilnic: {fd.max_daily_requests()} request-uri (MAX_DAILY_API_REQUESTS)")
    print(f"  Debug logging: {'ON (DEBUG=1)' if DEBUG else 'OFF — setează DEBUG=1 în .env'}")
    print("  Deschide http://localhost:" + os.environ.get("PORT", "8000"))
    print("=" * 56)


if __name__ == "__main__":
    _startup_report()
    uvicorn.run("main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
