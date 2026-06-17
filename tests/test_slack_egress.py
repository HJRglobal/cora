"""Tests for the single sanitizing egress boundary (Phase 2.1 / B1)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cora import slack_egress  # noqa: E402


# ── sanitize_text ──────────────────────────────────────────────────────────────
def test_sanitize_non_verbatim_applies_voice_contract():
    out = slack_egress.sanitize_text("**Bold** answer — done :tada:")
    assert "**" not in out
    assert "—" not in out and "-" in out
    assert ":tada:" not in out
    assert "Bold answer" in out


def test_sanitize_non_verbatim_flattens_markdown_list():
    out = slack_egress.sanitize_text("Items:\n- one\n- two")
    assert "\n- " not in out
    assert "one" in out and "two" in out


def test_sanitize_non_verbatim_redacts_drive_url():
    out = slack_egress.sanitize_text("See https://docs.google.com/spreadsheets/d/abc123 for detail")
    assert "docs.google.com" not in out


def test_sanitize_verbatim_preserves_financial_table():
    table = "| Entity | Ending Cash |\n|---|---|\n| F3E | $1,680 |\n| LEX | $99,807 |"
    out = slack_egress.sanitize_text(table, verbatim=True)
    # Verbatim tables survive un-mangled (pipes + separator row intact).
    assert out == table


def test_sanitize_verbatim_still_repairs_mojibake():
    bad = "Portfolio cash â€” steady"  # mojibake em-dash
    out = slack_egress.sanitize_text(bad, verbatim=True)
    assert "â€" not in out
    assert "—" in out  # repaired to a real em-dash (verbatim leaves it as-is)


def test_sanitize_none_and_empty_passthrough():
    assert slack_egress.sanitize_text(None) is None
    assert slack_egress.sanitize_text("") == ""
    assert slack_egress.sanitize_text(123) == 123  # non-str unchanged


# ── repair_mojibake ──────────────────────────────────────────────────────────
def test_repair_mojibake_em_dash():
    bad = "Week of 5-29 â€” portfolio steady"
    out = slack_egress.repair_mojibake(bad)
    assert "â€”" not in out
    assert "—" in out


def test_repair_mojibake_bullet():
    # The run_reconciliation nudge mojibake: bullet rendered as "a-bullet".
    bad = "â€¢ task one"
    out = slack_egress.repair_mojibake(bad)
    assert "•" in out  # real bullet


def test_repair_mojibake_idempotent_on_clean():
    clean = "Clean text — with a real em dash and • bullet"
    assert slack_egress.repair_mojibake(clean) == clean


# ── redact_named_sources ─────────────────────────────────────────────────────
def test_redact_sheet_identifier():
    out = slack_egress.redact_named_sources("Pulled from the Standing ACTUALS sheet today")
    assert "Standing ACTUALS" not in out


def test_redact_cf_summary():
    out = slack_egress.redact_named_sources("The CF_SUMMARY tab shows $X")
    assert "CF_SUMMARY" not in out and "CF SUMMARY" not in out


def test_redact_financial_attribution_phrase():
    out = slack_egress.redact_named_sources("Ending cash is $99,807 per QuickBooks")
    assert "QuickBooks" not in out
    assert "$99,807" in out


def test_redact_leaves_operational_tools_alone():
    msg = "I created an Asana task and posted to Slack; check your Gmail."
    assert slack_egress.redact_named_sources(msg) == msg


def test_redact_bare_app_name_left_but_logged(caplog):
    # A bare standalone "QuickBooks" (no attributive preposition) is intentionally
    # left to the prompt; redacting it blind reads worse than the leak. It logs.
    import logging
    with caplog.at_level(logging.WARNING):
        out = slack_egress.redact_named_sources("QuickBooks shows a discrepancy.")
    assert "QuickBooks" in out
    assert any("named_source_survived" in r.message for r in caplog.records)


# ── install_egress_sanitizer + wrapper ───────────────────────────────────────
def test_install_is_idempotent_and_patches_real_client():
    # cora/__init__.py already installed it on import; calling again is a no-op.
    assert slack_egress.install_egress_sanitizer() is True
    from slack_sdk.web.client import WebClient
    for name in slack_egress._SEND_METHODS:
        method = getattr(WebClient, name)
        assert getattr(method, "_cora_egress_wrapped", False), f"{name} not wrapped"


def test_wrapper_sanitizes_text_and_pops_verbatim():
    captured = {}

    def fake_original(self, *args, **kwargs):
        captured.update(kwargs)
        captured["args"] = args
        return {"ok": True}

    wrapped = slack_egress._make_wrapper(fake_original)
    wrapped(object(), channel="C1", text="**Bold** — done", cora_verbatim=False)
    # text sanitized, cora_verbatim never reaches the SDK
    assert "**" not in captured["text"]
    assert "—" not in captured["text"]
    assert "cora_verbatim" not in captured
    assert captured["channel"] == "C1"


def test_wrapper_verbatim_leaves_table_raw():
    captured = {}

    def fake_original(self, *args, **kwargs):
        captured.update(kwargs)
        return {"ok": True}

    wrapped = slack_egress._make_wrapper(fake_original)
    table = "| A | B |\n|---|---|\n| 1 | 2 |"
    wrapped(object(), channel="C1", text=table, cora_verbatim=True)
    assert captured["text"] == table
    assert "cora_verbatim" not in captured


def test_wrapper_no_text_passes_through():
    captured = {}

    def fake_original(self, *args, **kwargs):
        captured.update(kwargs)
        return {"ok": True}

    wrapped = slack_egress._make_wrapper(fake_original)
    wrapped(object(), channel="C1", blocks=[{"type": "section"}])
    assert "text" not in captured  # untouched when no text kwarg
    assert captured["blocks"] == [{"type": "section"}]
