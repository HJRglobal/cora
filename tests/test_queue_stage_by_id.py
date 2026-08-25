"""C15 (cq-5414c154b213): a typed stage request minted a junk item, and two leaks.

All three from ONE live exchange, 2026-08-24 15:38.

(b) STAGE-BY-ID. "stage a code-session prompt for cq-f52c6b691127" created a new
    P2 item titled "Retrieve pending code-queue item cq-f52c6b691127 for staging"
    -- a queue entry whose entire content is a request to look at another queue
    entry -- and staged a kickoff for it.

    PREMISE OVERTURNED on the mechanism. The seed blames "the queue-phrase lane";
    the ledger says otherwise: the captured event carries signal="explicit", and
    the phrase lane stamps "phrase" while the deflection lane stamps
    "deflection". _PHRASE_RE does not match this wording, and app.py's
    _CODE_QUEUE_INTENT_RE requires a verb from {queue|log|file|add|put|open} --
    "stage" is not among them, and that regex is a tool-choice precedence
    suppressor, never a seeder. The real path is the cora_queue_code_session TOOL
    -> queue_explicit, which MINTS UNCONDITIONALLY and never inspects the request
    for an id. Fingerprint dedup cannot help either: the key is (signal,
    representative) = ("explicit", raw request text), so two differently-worded
    stage requests naming the SAME id produce two different junk items.

    Also corrected: the kickoff did not "auto-stage". severity is hard-coded P2
    and the auto-kickoff branch fires only for P0/P1; the staged event 16s later
    is Harrison tapping the card's own Stage button.

(c1) WRITE_CONFIRMED LEAK. The same exchange posted "WRITE_CONFIRMED -- post as
    your entire response:" into the founder DM. Three emitters in
    _execute_claimed_code_queue separated the sentinel from the user text with
    ": " instead of a blank line; _strip_write_sentinel splits on "\\n\\n" and
    FAILED OPEN, returning the raw directive.

(c2) EMPTY PROVENANCE. The card rendered "<slack://channel?id=...> ts ``"
    because the guard was an OR over channel_id/ts. Every row minted by
    queue_explicit carries ts="" by construction, so it fired on every explicit
    capture, not just occasionally.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cora import code_queue as cq
from cora.tools import tool_dispatch as td

# The queue's ledger/notes isolation fixture lives with the main queue suite;
# forking it would be a second thing to keep in sync.
from test_code_queue import qenv  # noqa: F401

HARRISON = "U0B2RM2JYJ1"


# ── (b) stage-by-id ─────────────────────────────────────────────────────────

def test_find_cq_id_resolves_only_against_the_ledger(qenv):
    cid = cq.seed_item(kind="bug", severity="P2", title="a real item",
                       summary="s", entity="F3E", signal="tool_error",
                       status="PROPOSED")
    assert cq.find_cq_id(f"stage a prompt for {cid} please") == cid
    assert cq.find_cq_id("stage cq-000000000000") == ""
    assert cq.find_cq_id("no id here at all") == ""
    assert cq.find_cq_id("") == ""


def test_a_request_naming_an_existing_item_does_not_mint(qenv):
    """THE defect. Before this, queue_explicit minted unconditionally."""
    cid = cq.seed_item(kind="bug", severity="P2", title="real work",
                       summary="s", entity="F3E", signal="tool_error",
                       status="PROPOSED")
    before = len(cq.load_items()) if hasattr(cq, "load_items") else None
    got, outcome = cq.queue_explicit(
        HARRISON, "FNDR", "D1",
        f"stage a code-session prompt for {cid}", is_founder=True)
    assert got == cid, "it minted a new item instead of resolving the named one"
    assert outcome.startswith(("staged", "resolved")), outcome
    # and no item was created whose title is the request text
    titles = [i.get("title", "") for i in cq.all_items()] \
        if hasattr(cq, "all_items") else []
    assert not [t for t in titles if "stage a code-session prompt" in t.lower()]
    if before is not None:
        assert len(cq.load_items()) == before


def test_a_normal_request_still_mints(qenv):
    got, outcome = cq.queue_explicit(
        HARRISON, "FNDR", "D1", "Cora should retry failed Drive uploads",
        is_founder=True)
    assert got and got.startswith("cq-")
    assert outcome in ("ok", "held")


def test_stage_by_id_is_harrison_only(qenv):
    cid = cq.seed_item(kind="bug", severity="P2", title="x", summary="s",
                       entity="F3E", signal="tool_error", status="PROPOSED")
    outcome, msg = cq.stage_by_id(cid, "U_SOMEONE_ELSE")
    assert outcome == "not_authorized"


def test_an_unknown_id_points_at_the_button_instead_of_failing_silently(qenv):
    outcome, msg = cq.stage_by_id("cq-000000000000", HARRISON)
    assert outcome == "not_found"
    assert "Stage prompt" in msg, "the refusal must name the path that works"


def test_stage_by_id_delegates_to_the_shared_kickoff_generator(qenv, monkeypatch):
    """Reuse is the point: it inherits the staging reservation, the terminal
    guard and the prompt_path idempotence rather than re-deriving them."""
    cid = cq.seed_item(kind="bug", severity="P2", title="x", summary="s",
                       entity="F3E", signal="tool_error", status="PROPOSED")
    calls = []
    monkeypatch.setattr(cq, "ensure_kickoff_staged",
                        lambda c: (calls.append(c), ("staged", "/tmp/p.md"))[1])
    assert cq.stage_by_id(cid, HARRISON) == ("staged", "/tmp/p.md")
    assert calls == [cid]


# ── (c1) the WRITE_CONFIRMED leak ───────────────────────────────────────────

def test_the_stripper_recovers_a_malformed_payload_instead_of_leaking_it():
    leaked = ("WRITE_CONFIRMED -- post as your entire response: Queued to your "
              "code-session queue (APPROVED).")
    out = td._strip_write_sentinel(leaked)
    assert "WRITE_CONFIRMED" not in out
    assert "post as your entire response" not in out
    assert out.startswith("Queued to your code-session queue")


def test_the_well_formed_contract_is_unchanged():
    assert td._strip_write_sentinel("WRITE_CONFIRMED\n\nQueued.") == "Queued."
    assert td._strip_write_sentinel("ordinary reply") == "ordinary reply"


def test_a_bare_sentinel_is_never_posted_verbatim():
    for raw, expect in (("WRITE_CONFIRMED", "Done."),
                        ("WRITE_BLOCKED", "I couldn't complete that.")):
        out = td._strip_write_sentinel(raw)
        assert "WRITE_" not in out
        assert out == expect


def test_the_directive_regex_cannot_eat_a_user_sentence():
    """Bounded and non-greedy, so a legitimate payload containing a colon
    survives."""
    ok = "WRITE_CONFIRMED\n\nDone: the note is filed."
    assert td._strip_write_sentinel(ok) == "Done: the note is filed."


def test_every_write_confirmed_literal_carries_a_blank_line_separator():
    """Structural guard, the tests/test_no_raw_slack_post.py pattern: the leak
    happened because three literals used ': ' where every other emitter used a
    blank line. A fourth must not be able to drift in.

    Walks the AST rather than the raw text so that prose inside a docstring
    which merely QUOTES the bad form is not treated as an emitter -- this file's
    own module docstring would otherwise flag itself.

    The rule is narrow on purpose: an offender is a literal carrying user-facing
    text on the SAME string after the directive's colon. The widespread
    `["WRITE_CONFIRMED ...:", "", "Done -- ..."]` list-join is correct -- its
    blank line is a separate element and only exists at runtime -- and a guard
    that flagged it would be turned off within a week.
    """
    import ast

    root = Path(__file__).resolve().parents[1] / "src" / "cora"
    offenders = []
    for path in sorted(root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docstrings.add(doc)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            val = node.value
            if val in docstrings:
                continue
            if not val.startswith(("WRITE_CONFIRMED", "WRITE_BLOCKED")):
                continue
            if val in ("WRITE_CONFIRMED", "WRITE_BLOCKED"):
                continue          # a bare token used in a startswith/in check
            if "\n\n" in val:
                continue          # well-formed: sentinel, blank line, user text
            # THE DEFECT SHAPE, precisely: user-facing text sitting on the SAME
            # literal after the directive's colon. A literal that ENDS at the
            # colon hands the user text to a separate element -- the common
            # `["WRITE_CONFIRMED ...:", "", "Done -- ..."]` join, which produces
            # the blank line at runtime and is correct.
            head, sep, tail = val.partition(":")
            if sep and tail.strip():
                offenders.append(f"{path.name}:{node.lineno}: {val[:80]!r}")
    assert not offenders, (
        "WRITE_ sentinel literals with no blank-line separator -- these leak "
        "their model-facing directive to the user:\n  " + "\n  ".join(offenders))


# ── (c2) the empty provenance line ──────────────────────────────────────────

def test_a_row_with_no_ts_renders_no_provenance_line():
    rec = {"id": "cq-abc", "status": "PROPOSED", "kind": "bug", "severity": "P2",
           "entity": "FNDR", "title": "t", "summary": "s",
           "evidence": [{"channel_id": "D0B4CTD3B09", "ts": "", "note": "n"}]}
    text, _blocks = cq.build_item_card(rec)
    assert "slack://channel" not in text
    assert "ts ``" not in text


def test_a_complete_row_still_renders_it():
    rec = {"id": "cq-abc", "status": "PROPOSED", "kind": "bug", "severity": "P2",
           "entity": "FNDR", "title": "t", "summary": "s",
           "evidence": [{"channel_id": "C123", "ts": "1787611109.915539",
                         "note": "n"}]}
    text, _blocks = cq.build_item_card(rec)
    assert "slack://channel?id=C123" in text
    assert "1787611109.915539" in text


def test_the_kickoff_evidence_renderer_has_the_same_guard():
    """Leaving :1443 half-fixed reproduces the identical artifact in every
    generated prompt instead of only on the card."""
    import inspect
    src = inspect.getsource(cq)
    assert "if str(e.get('channel_id') or '').strip()" in src
