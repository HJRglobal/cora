"""Unit tests for team_learning.py — write-back, correction capture, approval queue."""

import time
from pathlib import Path

import pytest

from cora.team_learning import (
    APPROVAL_CHANNEL,
    build_approval_card,
    is_correction,
    lookup_by_approval_ts,
    parse_note,
    pending_stats,
    resolve_contribution,
    set_approval_msg,
    store_contribution,
)


# ── Fixture: redirect DB to a temp file ──────────────────────────────────────

@pytest.fixture(autouse=True)
def patch_db_path(tmp_path, monkeypatch):
    """Redirect team_learning's KB_DB_PATH to an in-memory temp file."""
    import cora.team_learning as tl
    db = tmp_path / "test_contributions.db"
    monkeypatch.setattr(tl, "_KB_DB_PATH", db)
    yield


# ── parse_note() ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("msg,expected", [
    ("note: Shaun Hawkins is the LLC operations lead", "Shaun Hawkins is the LLC operations lead"),
    ("NOTE: BCB ingredient deadline is May 27",       "BCB ingredient deadline is May 27"),
    ("Note:  Extra spaces at start   ",               "Extra spaces at start"),
    ("Hey note: this is a note",                       "this is a note"),
    ("@Cora note: Justin runs the books",              "Justin runs the books"),
])
def test_parse_note_valid(msg, expected):
    result = parse_note(msg)
    assert result == expected


@pytest.mark.parametrize("msg", [
    "what's the tagline?",
    "show me my tasks",
    "noteworthy update — not a note command",
    "",
    "noted, will do",
])
def test_parse_note_invalid(msg):
    assert parse_note(msg) is None


def test_parse_note_multiline():
    msg = "note: First line\nSecond line\nThird line"
    result = parse_note(msg)
    assert result is not None
    assert "First line" in result


# ── is_correction() ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "actually, that's not right — the launch is June 15th",
    "Correction: Micah Kessler, not Micah Williams",
    "That's wrong — Sandy Patel holds 25%, not sold the entity",
    "To clarify, the lease expires June 30, not July 30",
    "Just to clarify: Tessa is part-time, not departed",
    "Small correction: provider type 15 applies to LTS",
    "Quick correction here — not quite right",
    "Not quite right, the BCB deposit is 50%",
    "That's not accurate — Alex is in Asana",
])
def test_is_correction_true(text):
    assert is_correction(text) is True


@pytest.mark.parametrize("text", [
    "what's the cash position?",
    "show me my tasks",
    "great, thanks",
    "can you clarify the LTS revalidation timeline?",  # question, not correction
    "I need to actually go do that",  # "actually" mid-sentence, not a lead
    "",
])
def test_is_correction_false(text):
    assert is_correction(text) is False


# ── store_contribution() and lookup_by_approval_ts() ─────────────────────────

def test_store_and_lookup_basic():
    cid = store_contribution(
        kind="note",
        entity="F3E",
        channel_id="C123",
        channel_name="f3e-leadership",
        author="U456",
        content="Sandy Patel holds 25% of LLA",
        original_ts="1234567890.000100",
    )
    assert len(cid) == 36  # UUID format

    # Set approval message
    set_approval_msg(cid, "9876543210.000200", "Capproval")

    # Lookup by approval ts
    contrib = lookup_by_approval_ts("9876543210.000200")
    assert contrib is not None
    assert contrib["contribution_id"] == cid
    assert contrib["entity"] == "F3E"
    assert contrib["author"] == "U456"
    assert contrib["kind"] == "note"
    assert contrib["status"] == "pending"


def test_lookup_nonexistent_ts_returns_none():
    result = lookup_by_approval_ts("0000000000.000000")
    assert result is None


def test_lookup_resolved_contribution_returns_none():
    cid = store_contribution(
        kind="correction",
        entity="OSN",
        channel_id="C789",
        channel_name="osn-leadership",
        author="U111",
        content="The breakeven is $240K/mo not $172K",
        original_ts="1111111111.000100",
    )
    set_approval_msg(cid, "2222222222.000200", "Capproval")
    resolve_contribution(cid, "approved")

    # After resolving, lookup should return None (status != 'pending')
    result = lookup_by_approval_ts("2222222222.000200")
    assert result is None


def test_resolve_contribution_approved():
    cid = store_contribution(
        kind="note",
        entity="LEX",
        channel_id="Clex",
        channel_name="llc-leadership",
        author="U222",
        content="Jen Mortensen handles AHCCCS billing",
        original_ts="3333333333.000100",
    )
    resolve_contribution(cid, "approved")
    stats = pending_stats()
    assert stats.get("approved", 0) >= 1


def test_resolve_contribution_declined():
    cid = store_contribution(
        kind="note",
        entity="BDM",
        channel_id="Cbdm",
        channel_name="bdm-leadership",
        author="U333",
        content="Larry Stone handles all BDM production",
        original_ts="4444444444.000100",
    )
    resolve_contribution(cid, "declined")
    stats = pending_stats()
    assert stats.get("declined", 0) >= 1


# ── pending_stats() ───────────────────────────────────────────────────────────

def test_pending_stats_empty():
    assert pending_stats() == {}


def test_pending_stats_mixed():
    for i in range(3):
        store_contribution(
            kind="note", entity="F3E", channel_id="C1",
            channel_name="f3e", author=f"U{i}", content=f"Note {i}",
            original_ts=f"{i}.000",
        )
    stats = pending_stats()
    assert stats.get("pending", 0) == 3


# ── build_approval_card() ─────────────────────────────────────────────────────

def test_build_approval_card_note():
    card = build_approval_card(
        kind="note",
        entity="F3E",
        channel_name="f3e-leadership",
        author="U456",
        content="F3 Pure tagline: Real energy for real life.",
        contribution_id="abcdef12-0000-0000-0000-000000000000",
    )
    assert "📝 Team Note" in card
    assert "abcdef12" in card
    assert "F3E" in card
    assert "f3e-leadership" in card
    assert "✅" in card
    assert "❌" in card


def test_build_approval_card_correction():
    card = build_approval_card(
        kind="correction",
        entity="LEX",
        channel_name="llc-ops",
        author="U789",
        content="Correction: Micah Kessler (not Williams) is the OSN co-guarantor",
        contribution_id="deadbeef-0000-0000-0000-000000000000",
    )
    assert "🔄 Correction" in card
    assert "deadbeef" in card


def test_build_approval_card_truncates_long_content():
    long_content = "x" * 1000
    card = build_approval_card(
        kind="note",
        entity="OSN",
        channel_name="osn",
        author="U999",
        content=long_content,
        contribution_id="aaaabbbb-0000-0000-0000-000000000000",
    )
    # Content is capped at 800 chars in the card
    assert len(card) < 2000


# ── APPROVAL_CHANNEL constant ─────────────────────────────────────────────────

def test_approval_channel_is_hjrg_leadership():
    assert APPROVAL_CHANNEL == "hjrg-leadership"
