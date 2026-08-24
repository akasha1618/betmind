"""Bug-uri iOS: viewport, font 16px, enterkeyhint, reconectare, fara autofocus."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
LOGIN = (ROOT / "static" / "login.html").read_text(encoding="utf-8")


def test_viewport_does_not_block_manual_zoom():
    for html in (INDEX, LOGIN):
        assert 'name="viewport"' in html
        assert "width=device-width" in html
        assert "initial-scale=1" in html
        assert "viewport-fit=cover" in html
        assert "user-scalable=no" not in html
        assert "maximum-scale=1" not in html


def test_inputs_are_at_least_16px():
    """Safari iOS zoomează automat pe câmpuri sub 16px și nu revine."""
    compact = INDEX.replace(" ", "")
    assert "textarea,input,select{font-size:16px}" in compact
    assert "font:16px/1.4inherit" in compact
    edit_css = INDEX.split(".editbox textarea")[1][:220]
    assert "font:16px/1.4 inherit" in edit_css
    assert "font:15px" not in edit_css
    assert "font:16px/1.4 inherit" in LOGIN
    assert "autofocus" not in LOGIN.lower()


def test_composer_sends_from_mobile_keyboard():
    assert 'id="composer"' in INDEX
    assert 'enterkeyhint="send"' in INDEX
    assert "isSendKey" in INDEX
    assert 'addEventListener("submit"' in INDEX


def test_no_autofocus_on_main_composer():
    assert 'id="input"' in INDEX
    assert "autofocus" not in INDEX.lower().split('id="input"')[1][:200]
    assert "maybeFocusInput" in INDEX
    assert "isMobileUi" in INDEX
    assert "function maybeFocusInput() { if (!isMobileUi()) input.focus(); }" in INDEX


def test_visibilitychange_reconnects_inflight_turn():
    assert 'addEventListener("visibilitychange"' in INDEX
    assert "Reconectez…" in INDEX
    assert "/api/turns/" in INDEX
    assert "inflight.reconnect" in INDEX
