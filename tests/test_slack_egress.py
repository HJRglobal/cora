"""Tests for the single sanitizing egress boundary (Phase 2.1 / B1).

Post-2026-06-17 design: the boundary is a NARROW universal SAFETY layer (mojibake
repair + bare-URL/GID/long-ID redaction) that never flattens markdown/emoji or
collapses whitespace -- so it can run on EVERY send (including proactive
code-fenced / fixed-width tables and emoji-bearing cards) without mangling layout.
Conversational voice-flattening lives in reply_formatter.format_reply (applied
inline on the interactive Q&A path only), not here.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cora import slack_egress  # noqa: E402


# ── sanitize_text: SAFETY redactions ─────────────────────────────────────────
def test_sanitize_redacts_bare_drive_url():
    out = slack_egress.sanitize_text("Filed to https://drive.google.com/file/d/abc123 today")
    assert "drive.google.com" not in out
    assert "Filed to" in out and "today" in out


def test_sanitize_preserves_sanctioned_link():
    msg = "See <https://drive.google.com/file/d/abc|the contract> for detail"
    out = slack_egress.sanitize_text(msg)
    assert "<https://drive.google.com/file/d/abc|the contract>" in out  # token preserved


def test_sanitize_redacts_gid_and_long_id():
    out = slack_egress.sanitize_text("task gid 1204525779609669 and id 9876543210987654")
    assert "1204525779609669" not in out
    assert "9876543210987654" not in out


def test_sanitize_repairs_mojibake():
    out = slack_egress.sanitize_text("Portfolio cash â€” steady â€¢ on track")
    assert "â€" not in out
    assert "—" in out and "•" in out


# ── sanitize_text: structure-PRESERVING (the 2026-06-17 regression fix) ──────
def test_sanitize_preserves_code_fenced_aligned_table():
    table = (
        "```\n"
        "Entity        Ending Cash\n"
        "F3 Energy           $1,680\n"
        "UFL                 $2,244\n"
        "```"
    )
    out = slack_egress.sanitize_text(table)
    assert out == table  # fences + fixed-width alignment untouched


def test_sanitize_preserves_emoji_and_markdown():
    card = "*[Known Answer]* 🔴 `HIGH`\nSome fact\n\n👍 Approve · 👎 Dismiss"
    out = slack_egress.sanitize_text(card)
    assert out == card  # emoji, bold, backticks, blank line all survive


def test_sanitize_does_not_strip_emdash_or_collapse_whitespace():
    # An ops alert may legitimately use an em-dash and aligned spacing.
    msg = "Sync failed — retry queued     (col-aligned)"
    out = slack_egress.sanitize_text(msg)
    assert "—" in out
    assert "     " in out  # whitespace not collapsed


def test_sanitize_none_and_empty_passthrough():
    assert slack_egress.sanitize_text(None) is None
    assert slack_egress.sanitize_text("") == ""
    assert slack_egress.sanitize_text(123) == 123


# ── repair_mojibake ──────────────────────────────────────────────────────────
def test_repair_mojibake_em_dash():
    out = slack_egress.repair_mojibake("Week of 5-29 â€” steady")
    assert "â€”" not in out and "—" in out


def test_repair_mojibake_idempotent_on_clean():
    clean = "Clean text — with a real em dash and • bullet"
    assert slack_egress.repair_mojibake(clean) == clean


# ── install_egress_sanitizer + wrapper ───────────────────────────────────────
def test_install_is_idempotent_and_patches_real_client():
    assert slack_egress.install_egress_sanitizer() is True
    from slack_sdk.web.client import WebClient
    for name in slack_egress._SEND_METHODS:
        method = getattr(WebClient, name)
        assert getattr(method, "_cora_egress_wrapped", False), f"{name} not wrapped"


def test_wrapper_sanitizes_text():
    captured = {}

    def fake_original(self, *args, **kwargs):
        captured.update(kwargs)
        return {"ok": True}

    wrapped = slack_egress._make_wrapper(fake_original)
    wrapped(object(), channel="C1", text="Filed https://drive.google.com/x today")
    assert "drive.google.com" not in captured["text"]
    assert captured["channel"] == "C1"


def test_wrapper_sanitizes_markdown_text():
    captured = {}

    def fake_original(self, *args, **kwargs):
        captured.update(kwargs)
        return {"ok": True}

    wrapped = slack_egress._make_wrapper(fake_original)
    wrapped(object(), channel="C1", markdown_text="see https://drive.google.com/x")
    assert "drive.google.com" not in captured["markdown_text"]


def test_wrapper_preserves_table_text():
    captured = {}

    def fake_original(self, *args, **kwargs):
        captured.update(kwargs)
        return {"ok": True}

    wrapped = slack_egress._make_wrapper(fake_original)
    table = "```\nA    B\n1    2\n```"
    wrapped(object(), channel="C1", text=table)
    assert captured["text"] == table


def test_wrapper_no_text_passes_through():
    captured = {}

    def fake_original(self, *args, **kwargs):
        captured.update(kwargs)
        return {"ok": True}

    wrapped = slack_egress._make_wrapper(fake_original)
    wrapped(object(), channel="C1", blocks=[{"type": "section"}])
    assert "text" not in captured
    assert captured["blocks"] == [{"type": "section"}]


# ── AsyncWebClient guard (B3) ─────────────────────────────────────────────────
import pytest  # noqa: E402


def test_async_guard_is_noop_when_async_absent_and_sync_install_holds():
    # Absence-safe: install must still return True and the sync wrapper must hold
    # whether or not slack_sdk's async client / aiohttp is importable.
    assert slack_egress.install_egress_sanitizer() is True
    from slack_sdk.web.client import WebClient
    assert getattr(WebClient.chat_postMessage, "_cora_egress_wrapped", False)
    # Calling the guard directly must never raise, even with aiohttp absent.
    slack_egress._guard_async_webclient()


def test_async_guard_is_idempotent():
    # Calling install / the guard repeatedly does not change behavior or raise.
    slack_egress._guard_async_webclient()
    slack_egress._guard_async_webclient()
    assert slack_egress.install_egress_sanitizer() is True


def test_async_webclient_construction_is_forbidden():
    # Positive guard: if the async client IS importable (aiohttp installed), the
    # guard must make construction raise. Skipped in the current venv (no aiohttp).
    try:
        from slack_sdk.web.async_client import AsyncWebClient
    except Exception:  # pragma: no cover -- aiohttp not installed in this env
        pytest.skip("AsyncWebClient unavailable (aiohttp not installed)")
    slack_egress._guard_async_webclient()
    with pytest.raises(RuntimeError, match="sync-only"):
        AsyncWebClient(token="xoxb-test")
