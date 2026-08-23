"""
Titluri automate pentru conversatii (ca in ChatGPT).

Dupa prima tura, un model mic rezuma schimbul intr-un titlu scurt in romana.
Ruleaza in fundal, dupa ce raspunsul a fost livrat — nu intarzie niciodata
chatul. Daca apelul esueaza sau e dezactivat, ramane titlul provizoriu
(primele caractere din primul mesaj), deci nu se pierde nimic.
"""

from __future__ import annotations

import logging
import os

from anthropic import AsyncAnthropic

import db
import football_data as fd

log = logging.getLogger("betmind.titles")

MAX_TITLE_LEN = 60

# Conversatii cu titlul in curs de generare — doua liste cerute in acelasi
# timp nu trebuie sa plateasca acelasi apel de doua ori.
_IN_FLIGHT: set[str] = set()

_SYSTEM = (
    "Esti un generator de titluri pentru un chat despre pariuri pe fotbal. "
    "Primesti inceputul unei conversatii si raspunzi EXCLUSIV cu titlul ei, "
    "in romana: 3-6 cuvinte, concret (echipe, cota, perioada, daca apar), "
    "fara ghilimele, fara punct final, fara emoji, fara nicio explicatie.\n\n"
    "Titlul descrie SUBIECTUL discutiei, niciodata sarcina ta si niciodata "
    "asistentul care raspunde.\n\n"
    "Exemple:\n"
    "UTILIZATOR: vreau un bilet cu cota 16 pe saptamana viitoare, risc mic\n"
    "ASISTENT: Iata biletul propus pentru weekend...\n"
    "Titlu: Bilet cota 16 risc mic\n\n"
    "UTILIZATOR: salut, cine esti?\n"
    "ASISTENT: Sunt BetMind, analist de pariuri...\n"
    "Titlu: Salut si prezentare"
)


def auto_title_enabled() -> bool:
    return os.environ.get("AUTO_TITLE_ENABLED", "true").strip().lower() not in (
        "0", "false", "no")


def title_model() -> str:
    return (os.environ.get("TITLE_MODEL", "").strip()
            or os.environ.get("ANALYST_MODEL", "").strip()
            or "claude-haiku-4-5-20251001")


def clean_title(raw: str) -> str:
    """Prima linie, fara prefixe/ghilimele/punct final, taiata la cuvant."""
    title = (raw or "").strip().splitlines()[0] if (raw or "").strip() else ""
    title = title.strip().strip('"\'“”„ ').strip()
    for prefix in ("Titlu:", "Titlul:", "Title:"):
        if title.lower().startswith(prefix.lower()):
            title = title[len(prefix):].strip()
    title = title.strip('"\'“”„ ').rstrip(".").strip()
    if len(title) > MAX_TITLE_LEN:  # taiem la ultimul cuvant intreg
        title = title[:MAX_TITLE_LEN].rsplit(" ", 1)[0].rstrip(",;:-") or title[:MAX_TITLE_LEN]
    return title


async def _call_llm(user_content: str):
    """Un apel scurt la modelul de titluri. Separat ca sa fie mock-uibil."""
    client = AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip())
    msg = await client.messages.create(
        model=title_model(),
        max_tokens=32,
        system=_SYSTEM,
        messages=[{"role": "user", "content": user_content}],
    )
    return "".join(b.text for b in msg.content if b.type == "text"), msg.usage


async def generate_title(user_message: str, assistant_text: str = "",
                         turn_id: str = "") -> str:
    """Titlul propus pentru conversatie ('' daca nu se poate genera)."""
    if not auto_title_enabled() or not os.environ.get("ANTHROPIC_API_KEY", "").strip():
        return ""
    payload = f"UTILIZATOR: {user_message[:600]}"
    if assistant_text:
        payload += f"\nASISTENT: {assistant_text[:400]}"
    payload += "\nTitlu:"
    try:
        text, usage = await _call_llm(payload)
    except Exception:
        log.warning("Nu am putut genera titlul conversatiei", exc_info=True)
        return ""
    try:  # costul titlului intra in aceeasi tura, ca sa apara in modul dev
        await db.add_usage(turn_id or "title", title_model(),
                           getattr(usage, "input_tokens", None),
                           getattr(usage, "output_tokens", None),
                           fd.now_local().isoformat(timespec="seconds"))
    except Exception:
        pass
    return clean_title(text)


async def maybe_title_conversation(conversation_id: str, user_message: str,
                                   assistant_text: str = "", turn_id: str = "") -> None:
    """Genereaza si salveaza titlul O SINGURA data (la prima tura).

    Se apeleaza ca task de fundal, dupa livrarea raspunsului — orice eroare
    ramane aici, chatul nu e afectat.
    """
    if conversation_id in _IN_FLIGHT or not user_message.strip():
        return
    _IN_FLIGHT.add(conversation_id)
    try:
        conv = await db.get_conversation(conversation_id)
        if not conv or conv.get("title_auto"):
            return
        title = await generate_title(user_message, assistant_text, turn_id)
        if title:
            await db.set_conversation_title(conversation_id, title, auto=True)
    except Exception:
        log.warning("Titlu automat esuat pentru %s", conversation_id, exc_info=True)
    finally:
        _IN_FLIGHT.discard(conversation_id)
