"""R2 email egress guard: block classes 1-7 / warn class 8 per design section 7,
plus the D-051 lens-6 adversarial strings (guard-lexicon gaps)."""

from __future__ import annotations

import pytest

from cora import phi_guard
from cora.revops import email_egress_guard as guard


def _classes(result):
    return {b["class"] for b in result.blocks}


def _warn_classes(result):
    return {w["class"] for w in result.warns}


# ------------------------------------------------------------ class 1: dashes

def test_em_dash_blocks_anywhere():
    r = guard.check_email("Hi Josh \u2014 circling back!", workstream="Retail", entity="F3E")
    assert "em_dash" in _classes(r)


def test_em_dash_inside_template_variable_value_blocks():
    """Lens 6: an em-dash smuggled in via a rendered variable still blocks."""
    body = "Hi " + "Jos\u2014h" + ",\n\nJust circling back."
    r = guard.check_email(body)
    assert "em_dash" in _classes(r)


def test_em_dash_in_quoted_reply_still_blocks():
    r = guard.check_email("Fresh text\n> their quoted line \u2014 with a dash")
    assert "em_dash" in _classes(r)


def test_en_dash_warns_not_blocks():
    r = guard.check_email("Monday\u2013Friday works")
    assert "en_dash" in _warn_classes(r)
    assert "em_dash" not in _classes(r)


def test_clean_text_passes():
    r = guard.check_email(
        "Hi Josh,\n\nJust circling back on this one. Any update?\n\nThanks!\nHarrison",
        workstream="Retail",
        entity="F3E",
    )
    assert r.ok and not r.warns


# ----------------------------------------------------- class 2: health claims

def test_health_claims_block_on_f3e():
    r = guard.check_email("F3 helps treat anxiety and works as a sleep aid.", entity="F3E")
    assert "health_claims" in _classes(r)


def test_health_claims_ignored_outside_f3e():
    r = guard.check_email("The claims process for the disease rider", entity="HJRG")
    assert "health_claims" not in _classes(r)


def test_health_claims_in_quoted_customer_text_do_not_block():
    """Lens 6: claims words inside quoted customer text."""
    body = (
        "Happy to clarify our labeling.\n"
        "> your ad said it cures anxiety and prevents disease"
    )
    r = guard.check_email(body, entity="F3E")
    assert "health_claims" not in _classes(r)


# ------------------------------------------------------- class 3: NSF context

def test_nsf_with_energy_context_passes():
    r = guard.check_email("F3 Energy is NSF Certified for Sport.", entity="F3E")
    assert "nsf_context" not in _classes(r)


def test_nsf_without_energy_context_blocks():
    r = guard.check_email("Our products are NSF certified.", entity="F3E")
    assert "nsf_context" in _classes(r)


def test_nsf_near_pure_blocks():
    r = guard.check_email("F3 Pure energy line is NSF Certified for Sport.", entity="F3E")
    assert "nsf_context" in _classes(r)


# ---------------------------------------------------- class 4: press figures

def test_press_raise_figure_blocks():
    r = guard.check_email(
        "We just closed a $2M raise at a strong valuation.", workstream="Press"
    )
    assert "press_figures" in _classes(r)


def test_press_benign_figure_passes():
    r = guard.check_email("Cans retail around $3 each.", workstream="Press")
    assert "press_figures" not in _classes(r)


def test_raise_outside_press_does_not_block():
    """Lens 6: 'raise' in a benign supplier context must NOT block outside Press."""
    r = guard.check_email(
        "Can you raise the MOQ to 5000 units at $0.42 per can?", workstream="Suppliers"
    )
    assert "press_figures" not in _classes(r)


# ----------------------------------------------------- class 5: founded 2022

def test_founded_2022_blocks():
    for text in ("We were founded in 2022.", "founded 2022"):
        assert "founded_2022" in _classes(guard.check_email(text))


def test_founded_2023_passes():
    assert "founded_2022" not in _classes(guard.check_email("founded in 2023"))


# --------------------------------------------------------------- class 6: PHI

def test_phi_blocks(monkeypatch):
    monkeypatch.setattr(phi_guard, "is_any_phi", lambda text: True)
    r = guard.check_email("anything")
    assert "phi" in _classes(r)


def test_phi_guard_error_fails_closed(monkeypatch):
    def boom(text):
        raise RuntimeError("phi crashed")

    monkeypatch.setattr(phi_guard, "is_any_phi", boom)
    r = guard.check_email("anything")
    assert "phi" in _classes(r)


def test_guard_crash_fails_closed(monkeypatch):
    monkeypatch.setattr(guard, "_strip_quoted_lines", None)  # TypeError inside
    r = guard.check_email("anything")
    assert not r.ok
    assert "guard_error" in _classes(r)


# ------------------------------------------------- class 7: internal paths

@pytest.mark.parametrize(
    "text",
    [
        r"see G:\My Drive\HJR-Founder-OS\file.xlsx",
        "notes at computer://open/something",
        "ping me on slack://channel?id=C123",
        "https://docs.google.com/spreadsheets/d/abc123",
        "https://qbo.intuit.com/app/deeplink",
    ],
)
def test_internal_refs_block(text):
    assert "internal_refs" in _classes(guard.check_email(text))


def test_external_counterparty_urls_pass():
    r = guard.check_email("Details at https://www.sprouts.com/vendors are helpful.")
    assert "internal_refs" not in _classes(r)


# ------------------------------------------------- class 8: retail price WARN

def test_non_canonical_retail_price_warns():
    r = guard.check_email("We can do $21.99 per case.", workstream="Retail")
    assert "retail_price" in _warn_classes(r)
    assert r.ok  # WARN, not BLOCK


def test_canonical_retail_price_no_warn():
    r = guard.check_email("Tier pricing is $25.15 / $22.19 / $18.50.", workstream="Retail")
    assert "retail_price" not in _warn_classes(r)


def test_price_outside_retail_no_warn():
    r = guard.check_email("Freight came to $412.77.", workstream="Suppliers")
    assert "retail_price" not in _warn_classes(r)
