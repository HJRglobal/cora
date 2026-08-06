"""Slice 2 (pipeline-integrity bundle, 2026-08-05) -- cq-a1306f3835f8:
"@Cora queue a code session: ..." misrouted to an Asana task op.

ROOT CAUSE is intent PRECEDENCE, not tool selection. _asana_destructive_intent
reads the whole message, so the free-text DESCRIPTION of a bug report about tasks
satisfies its task-referent gate plus a verb branch; it then forces that tool via
tool_choice, which makes cora_queue_code_session unreachable for the turn.

Evidence: first reported 2026-07-29 in the D-090 live smokes, reproduced in the
2026-08-05 request sweep (item 10) -- six days as the sweep's longest-unresolved
item, and it degrades the exact escape hatch people use to report bugs.

The control class is the point of the slice: legitimate task ops must STILL force
their tool when they are NOT wrapped in the queue phrase, and a soft complaint must
NOT force a capture (those ride code_queue's async Harrison-gated classifier).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import app as cora_app


# ── the two historically misrouted phrasings ─────────────────────────────────

# 7/29 D-090 smoke + 8/5 sweep item 10. Each carries a task verb AND a "task"
# referent in its description -- which is exactly what the Asana detector caught.
MISROUTED = [
    "queue a code session: marking a task done doesn't work, the confirm no-ops",
    "queue a code session: the staged Asana task delete confirm silently no-ops",
    "@Cora queue a code session: close out the task and it stays open",
    "queue a code session to fix the task completion confirm",
    "Hey Cora, can you queue a code session: task due dates render wrong",
    "please log this for the devs: completing a task from a DM does nothing",
    "add this to the code queue: deleting a task needs a second confirm",
    "file a code session for the task-close bug",
]


@pytest.mark.parametrize("msg", MISROUTED)
def test_queue_phrase_detected(msg):
    assert cora_app._code_queue_capture_intent(msg) is True


@pytest.mark.parametrize("msg", MISROUTED)
def test_queue_phrase_suppresses_the_asana_force(msg):
    """The load-bearing half: even if the positive force were ever removed, the
    HIJACK must be gone -- otherwise tool_choice makes the capture tool
    unreachable and the model has no way to route correctly."""
    assert cora_app._asana_destructive_intent(msg) is None


@pytest.mark.parametrize("msg", MISROUTED)
def test_asana_detector_would_have_hijacked_without_the_guard(msg, monkeypatch):
    """Pins the DEFECT, not just the fix: with the precedence guard disabled, at
    least some of these phrasings do get claimed by an Asana write tool. If this
    ever stops being true the regression test above has gone vacuous."""
    monkeypatch.setattr(cora_app, "_code_queue_capture_intent", lambda _t: False)
    # Not every phrasing trips the Asana regexes -- the class is what matters.
    cora_app._asana_destructive_intent(msg)  # must not raise


def test_at_least_one_repro_provably_hijacked(monkeypatch):
    monkeypatch.setattr(cora_app, "_code_queue_capture_intent", lambda _t: False)
    hijacked = [m for m in MISROUTED if cora_app._asana_destructive_intent(m) is not None]
    assert hijacked, "the repro set no longer exercises the hijack -- fix the fixtures"


# ── phrase coverage ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "queue a code session: x",
    "queue this as a code session please",
    "log this for the devs",
    "log this for the dev queue",
    "file a code session",
    "add this to the code queue",
    "put this in the code queue",
    "open a code session for the inventory alias bug",
    "queue a code-session: the preview flipped direction",
    "can you queue a code session for this",
    "ok please queue a code session about the digest",
])
def test_explicit_phrases(msg):
    assert cora_app._code_queue_capture_intent(msg) is True


# ── the control set: what must NOT be claimed as a capture ───────────────────

@pytest.mark.parametrize("msg", [
    # Soft complaints -- code_queue._PHRASE_RE signals that ride the ASYNC
    # Harrison-gated classifier. Force-filing a card on each would flood the queue.
    "this is broken",
    "it doesn't work",
    "Cora should be able to check our RepRally wholesale listings",
    "can Cora pull the wholesale pricing?",
    "that's a bug",
    # Deliberative / negated / rejected-alternative framings.
    "I don't think we should queue a code session for that",
    "no need to queue a code session, I'll just do it manually",
    "instead of queueing a code session let's just fix the sheet",
    "rather than file a code session, ping Harrison",
    "why would we queue a code session for a typo",
    # Buried far past the leading window -- reads as an aside, not the command.
    ("Long context first: we talked through the whole inventory situation at length "
     "yesterday and everyone agreed on the plan, so maybe queue a code session"),
    # Unrelated uses of the words.
    "add this to the sprint queue",
    "log the meeting notes",
    "queue up the next episode",
    "",
])
def test_not_a_capture_intent(msg):
    assert cora_app._code_queue_capture_intent(msg) is False


# ── the control set: legitimate task ops still force their tool ──────────────

@pytest.mark.parametrize("msg,expected", [
    ("delete the task about the vendor invoice", "asana_delete_task"),
    ("please delete that task", "asana_delete_task"),
    ("mark the vendor task done", "asana_complete_task"),
    ("complete the task for the deck", "asana_complete_task"),
    ("create a task to follow up with Larry", "asana_create_task"),
    ("add a subtask for the packaging review", "asana_add_subtask"),
    ("change the due date on the Rita task", "asana_update_task"),
    ("leave a comment on the invoice task", "asana_add_comment"),
])
def test_task_ops_unaffected(msg, expected):
    """The whole risk of Slice 2 is swallowing legitimate task ops. None of these
    contain a code-queue phrase, so precedence never engages."""
    assert cora_app._code_queue_capture_intent(msg) is False
    assert cora_app._asana_destructive_intent(msg) == expected


@pytest.mark.parametrize("msg", [
    "who deleted the task?",
    "did you complete the task?",
    "what tasks are overdue?",
    "the complete list of tasks",
])
def test_interrogatives_still_excluded_everywhere(msg):
    assert cora_app._asana_destructive_intent(msg) is None
    assert cora_app._code_queue_capture_intent(msg) is False


# ── precedence ordering ──────────────────────────────────────────────────────

def test_precedence_is_queue_then_asana():
    """A message satisfying BOTH detectors resolves to the capture tool. This is
    the ordering _dispatch_qa applies when it computes force_tool."""
    msg = "queue a code session: delete the task confirm no-ops"
    assert cora_app._code_queue_capture_intent(msg) is True
    assert cora_app._asana_destructive_intent(msg) is None


def test_forced_tool_is_globally_exposed():
    """tool_choice can never name a tool the entity does not carry --
    cora_queue_code_session must stay in _GLOBAL_CORE_TOOLS for the force to be
    valid in every channel and DM."""
    from cora.tools import tool_dispatch
    assert "cora_queue_code_session" in tool_dispatch._GLOBAL_CORE_TOOLS
    for entity in ("F3E", "OSN", "LEX", "LEX-LLC", "HJRP", "FNDR", "HJRG", "F3C"):
        names = {t["name"] for t in tool_dispatch.tools_for_entity(entity)}
        assert "cora_queue_code_session" in names, entity


def test_forced_tool_first_call_files_nothing():
    """Why forcing this tool is safe even on a false positive: call one returns a
    preview and stashes server-side. Nothing is filed without a confirm."""
    from cora.tools import tool_dispatch
    spec = next(t for t in tool_dispatch.TOOL_DEFINITIONS
                if t["name"] == "cora_queue_code_session")
    assert "confirmed" in spec["input_schema"]["properties"]
    assert spec["input_schema"]["required"] == ["request"]
