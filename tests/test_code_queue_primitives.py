"""Slice 0 (pipeline-integrity bundle, 2026-08-05) -- the three ledger primitives
the queue reconciliation needed, plus the severity-vocabulary normalizer.

Why these exist: reconciling live queue state (re-rate a priority, attach a dated
real-world example, record a kickoff prompt authored outside the generator) had NO
public API, so the only route was a hand-edit of the append-only event ledger --
which the standing loop forbids. Each primitive appends an event; the fold applies it.

The normalizer (`canonical_severity` / `is_priority_severity`) is the fix for the
vocabulary split found in Slice-4 verify-first: the Haiku classifier emits P0-P3
while every seed_item caller passes HIGH/MEDIUM/LOW, and seed_item never validated,
so a HIGH item was invisible to a bare `in ("P0","P1")` priority test.
"""

from __future__ import annotations

import pytest

from cora import code_queue as cq

from test_code_queue import qenv  # noqa: F401  -- shared isolation fixture


HARRISON = "U0B2RM2JYJ1"
NOT_HARRISON = "U0B3AEJCYGP"


# ─────────────────────────────────────────────────────────────────────────────
# Severity vocabulary
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,expected", [
    ("P0", "P0"), ("P1", "P1"), ("P2", "P2"), ("P3", "P3"),
    ("p1", "P1"), (" P1 ", "P1"),
    ("HIGH", "P1"), ("high", "P1"),
    ("MEDIUM", "P2"), ("MED", "P2"), ("NORMAL", "P2"),
    ("LOW", "P3"), ("MINOR", "P3"),
    ("CRITICAL", "P0"), ("URGENT", "P0"),
    ("", ""), (None, ""), ("banana", ""), ("P4", ""),
])
def test_canonical_severity(raw, expected):
    assert cq.canonical_severity(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("P0", True), ("P1", True), ("HIGH", True), ("high", True),
    ("CRITICAL", True), ("URGENT", True),
    ("P2", False), ("P3", False), ("MEDIUM", False), ("LOW", False),
    ("", False), (None, False), ("banana", False),
])
def test_is_priority_severity(raw, expected):
    """The load-bearing predicate: "HIGH" must read as P1-class. A bare
    `in ("P0","P1")` test is exactly what dropped HIGH items on the floor."""
    assert cq.is_priority_severity(raw) is expected


def test_unknown_severity_is_never_silently_a_priority():
    """Fail-safe direction: an unrecognized vocabulary must degrade to
    not-priority (no phantom kickoff generation), never to priority."""
    for bogus in ("SEV1", "blocker", "??", "0"):
        assert cq.canonical_severity(bogus) == ""
        assert cq.is_priority_severity(bogus) is False


# ─────────────────────────────────────────────────────────────────────────────
# set_severity
# ─────────────────────────────────────────────────────────────────────────────
def test_set_severity_rerates_via_ledger(qenv):  # noqa: F811
    cid = cq.seed_item(kind="bug", severity="MEDIUM", title="misroute", summary="s",
                       entity="FNDR", signal="explicit", status="PROPOSED")
    outcome, _msg = cq.set_severity(cid, HARRISON, "HIGH")
    assert outcome == "edited"
    assert cq.get_item(cid)["severity"] == "HIGH"
    # Stored AS GIVEN -- canonicalizing live rows in place would rewrite the
    # priorities rendered in the backlog and the Monday menu.
    assert cq.is_priority_severity(cq.get_item(cid)["severity"]) is True


def test_set_severity_accepts_both_ladders(qenv):  # noqa: F811
    cid = cq.seed_item(kind="bug", severity="P3", title="t", summary="s",
                       entity="FNDR", signal="explicit", status="PROPOSED")
    assert cq.set_severity(cid, HARRISON, "P1")[0] == "edited"
    assert cq.get_item(cid)["severity"] == "P1"
    assert cq.set_severity(cid, HARRISON, "medium")[0] == "edited"
    assert cq.get_item(cid)["severity"] == "MEDIUM"


def test_set_severity_rejects_unknown_vocabulary(qenv):  # noqa: F811
    cid = cq.seed_item(kind="bug", severity="P2", title="t", summary="s",
                       entity="FNDR", signal="explicit", status="PROPOSED")
    outcome, msg = cq.set_severity(cid, HARRISON, "SEV1")
    assert outcome == "error" and "Unknown severity" in msg
    assert cq.get_item(cid)["severity"] == "P2"  # unchanged


def test_set_severity_idempotent(qenv):  # noqa: F811
    cid = cq.seed_item(kind="bug", severity="HIGH", title="t", summary="s",
                       entity="FNDR", signal="explicit", status="PROPOSED")
    assert cq.set_severity(cid, HARRISON, "HIGH")[0] == "noop"
    assert cq.set_severity(cid, HARRISON, "high")[0] == "noop"


def test_set_severity_harrison_only(qenv):  # noqa: F811
    cid = cq.seed_item(kind="bug", severity="P2", title="t", summary="s",
                       entity="FNDR", signal="explicit", status="PROPOSED")
    outcome, _ = cq.set_severity(cid, NOT_HARRISON, "P0")
    assert outcome == "not_authorized"
    assert cq.get_item(cid)["severity"] == "P2"


def test_set_severity_missing_item(qenv):  # noqa: F811
    assert cq.set_severity("cq-nope", HARRISON, "P1")[0] == "error"


def test_edited_event_without_severity_leaves_it_alone(qenv):  # noqa: F811
    """Back-compat: the pre-existing title/summary edit event carries no
    `severity` key and must not blank the field."""
    cid = cq.seed_item(kind="bug", severity="P1", title="t", summary="s",
                       entity="FNDR", signal="explicit", status="PROPOSED")
    cq.apply_edit(cid, HARRISON, "new title", "new summary")
    it = cq.get_item(cid)
    assert it["severity"] == "P1"
    assert it["title"] == "new title"


# ─────────────────────────────────────────────────────────────────────────────
# append_evidence
# ─────────────────────────────────────────────────────────────────────────────
def test_append_evidence_does_not_bump_count(qenv):  # noqa: F811
    """The whole reason this is not a `recurrence` event: count is what the
    Monday menu ranks on, and attaching an example is not a new occurrence."""
    cid = cq.seed_item(kind="bug", severity="MEDIUM", title="eligibility too loose",
                       summary="s", entity="FNDR", signal="explicit", status="PROPOSED")
    before = cq.get_item(cid)["count"]
    outcome, _ = cq.append_evidence(cid, HARRISON, "2026-08-05: fighter exchange converted")
    assert outcome == "evidence"
    it = cq.get_item(cid)
    assert it["count"] == before
    notes = [e.get("note", "") for e in it["evidence"]]
    assert any("fighter exchange" in n for n in notes)


def test_append_evidence_accumulates(qenv):  # noqa: F811
    cid = cq.seed_item(kind="bug", severity="MEDIUM", title="t", summary="s",
                       entity="FNDR", signal="explicit", status="PROPOSED")
    cq.append_evidence(cid, HARRISON, "first example")
    cq.append_evidence(cid, HARRISON, "second example")
    notes = [e.get("note", "") for e in cq.get_item(cid)["evidence"]]
    assert any("first example" in n for n in notes)
    assert any("second example" in n for n in notes)


def test_append_evidence_phi_refused(qenv, monkeypatch):  # noqa: F811
    cid = cq.seed_item(kind="bug", severity="P2", title="t", summary="s",
                       entity="FNDR", signal="explicit", status="PROPOSED")
    before = len(cq.get_item(cid)["evidence"])
    monkeypatch.setattr(cq.phi_guard, "is_any_phi", lambda t: True)
    outcome, msg = cq.append_evidence(cid, HARRISON, "some clinical text")
    assert outcome == "error" and "PHI" in msg
    assert len(cq.get_item(cid)["evidence"]) == before


def test_append_evidence_phi_error_fails_closed(qenv, monkeypatch):  # noqa: F811
    cid = cq.seed_item(kind="bug", severity="P2", title="t", summary="s",
                       entity="FNDR", signal="explicit", status="PROPOSED")
    def _boom(_t):
        raise RuntimeError("screen down")
    monkeypatch.setattr(cq.phi_guard, "is_any_phi", _boom)
    assert cq.append_evidence(cid, HARRISON, "anything")[0] == "error"


def test_append_evidence_lex_refused_without_pointer(qenv):  # noqa: F811
    """A LEX item's note is reduced to a pointer; with no channel/ts to point at
    nothing informative survives, so persisting an empty stub is refused."""
    cid = cq.seed_item(kind="feature", severity="P3", title="lex thing", summary="s",
                       entity="LEX", signal="explicit", status="PROPOSED")
    outcome, msg = cq.append_evidence(cid, HARRISON, "some LEX detail")
    assert outcome == "error" and "pointer" in msg


def test_append_evidence_lex_pointer_only(qenv):  # noqa: F811
    cid = cq.seed_item(kind="feature", severity="P3", title="lex thing", summary="s",
                       entity="LEX", signal="explicit", status="PROPOSED")
    outcome, _ = cq.append_evidence(cid, HARRISON, "some LEX detail",
                                    channel_id="C0LEX", ts="123.45")
    assert outcome == "evidence"
    attached = cq.get_item(cid)["evidence"][-1]
    assert attached["channel_id"] == "C0LEX" and attached["ts"] == "123.45"
    assert "note" not in attached  # the text never persists for a LEX item


def test_append_evidence_harrison_only_and_validated(qenv):  # noqa: F811
    cid = cq.seed_item(kind="bug", severity="P2", title="t", summary="s",
                       entity="FNDR", signal="explicit", status="PROPOSED")
    assert cq.append_evidence(cid, NOT_HARRISON, "x")[0] == "not_authorized"
    assert cq.append_evidence(cid, HARRISON, "   ")[0] == "error"
    assert cq.append_evidence("cq-nope", HARRISON, "x")[0] == "error"


# ─────────────────────────────────────────────────────────────────────────────
# record_staged
# ─────────────────────────────────────────────────────────────────────────────
def test_record_staged_closes_the_external_prompt_loop(qenv):  # noqa: F811
    """The cq-f1236540b61e shape: APPROVED with a kickoff file authored by another
    session. Recording it must move the item to STAGED so the Monday menu stops
    re-offering approved-unstaged work."""
    cid = cq.seed_item(kind="feature", severity="P1", title="intake gap", summary="s",
                       entity="FNDR", signal="explicit", status="APPROVED")
    outcome, _ = cq.record_staged(cid, "G:/x/y/prompt.md", HARRISON)
    assert outcome == "staged"
    it = cq.get_item(cid)
    assert it["status"] == "STAGED" and it["prompt_path"] == "G:/x/y/prompt.md"
    assert it.get("staged_at")


def test_record_staged_idempotent(qenv):  # noqa: F811
    cid = cq.seed_item(kind="feature", severity="P1", title="t", summary="s",
                       entity="FNDR", signal="explicit", status="APPROVED")
    cq.record_staged(cid, "/first.md", HARRISON)
    outcome, msg = cq.record_staged(cid, "/second.md", HARRISON)
    assert outcome == "noop" and "/first.md" in msg
    assert cq.get_item(cid)["prompt_path"] == "/first.md"


def test_record_staged_guards(qenv):  # noqa: F811
    cid = cq.seed_item(kind="feature", severity="P1", title="t", summary="s",
                       entity="FNDR", signal="explicit", status="APPROVED")
    assert cq.record_staged(cid, "/p.md", NOT_HARRISON)[0] == "not_authorized"
    assert cq.record_staged(cid, "   ", HARRISON)[0] == "error"
    assert cq.record_staged("cq-nope", "/p.md", HARRISON)[0] == "error"
    assert cq.get_item(cid)["status"] == "APPROVED"  # untouched
