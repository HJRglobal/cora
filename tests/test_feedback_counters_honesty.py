"""C5 (cq-7bac8008b140): two counters pinned at 0, for two different reasons.

CORRECTIONS -- 0 for THIRTEEN consecutive weekly runs (6/01..8/24) while its two
siblings in the same message fluctuated. Not a key mismatch: the reader filters
`signal_type == "correction"` and the writer writes exactly that literal, to the
same file. The counter is 0 because its only production writer -- app.py's Path 1
-- sits inside @app.event("message") in the CHANNEL branch, and channel `message`
events do not reach this Slack app. The Event Subscriptions bot_events list is
configured separately from OAuth scopes, is invisible to the token, and cannot be
changed from this repo (D-138..145). Measured: 0 occurrences of "team_learning:
correction detected" across 1,374 log files, against 1,490 "app_mention routed".
So the code fix here is NOT to the counter -- it is to stop a structural zero
reading as "Cora is never wrong", and to fix the second defect stacked behind the
first so the lane works the day the subscription lands.

AUTO-FIXED -- premise overturned. The counter is NOT unwired: check_heartbeat has
two writers that fire _restart_cora on a >300s stale heartbeat, and the heartbeat
file parses fine, so the branch is reachable. It has simply never triggered (82
of 82 logged runs report "0 fixed"). The real defect is a DOCUMENTED auto-fix
with no implementation: the module docstring advertised "Any scheduled task in
state Running for >2h -> mark stuck, restart", and no such code has ever existed
-- while `Running` was scored OK by the classifier, so a wedged task was
invisible. That is not hypothetical; the same file records a live 8/19 incident
("State=Running with no process behind it") and notes that such a task blocks
every subsequent trigger.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from cora import team_learning as tl

_SCRIPTS = str(Path(__file__).resolve().parents[1] / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import nightly_health_check as nhc  # noqa: E402


# ── the latent defect behind the corrections counter ────────────────────────

@pytest.mark.parametrize("body", [
    "actually, that's the old address",
    "actually that is wrong",
    "to clarify, the MOQ is 12",
])
def test_a_correction_addressed_to_cora_is_detected(body):
    """Every pattern in _CORRECTION_PATTERNS is ^-anchored, and the W1-01
    "@mentions Cora" skip deliberately sits AFTER Path 1, so a correction aimed
    at Cora arrives as "<@U...> actually ..." and every anchored pattern missed
    it. Latent while the event is undelivered -- which is exactly why it had to
    be fixed now rather than discovered later."""
    from cora.app import _MENTION_RE
    raw = f"<@U0B2RM2JYJ1> {body}"
    assert tl.is_correction(raw) is False, "premise gone: patterns no longer anchored"
    assert tl.is_correction(_MENTION_RE.sub("", raw).strip()) is True


def test_path1_strips_the_mention_before_testing():
    src = (Path(__file__).resolve().parents[1]
           / "src" / "cora" / "app.py").read_text(encoding="utf-8")
    assert "_correction_text = _MENTION_RE.sub(\"\", text).strip() or text" in src
    assert "if team_learning.is_correction(_correction_text):" in src


def test_the_logged_correction_text_is_the_stripped_one():
    """The stored correction should not carry a raw <@U...> token -- the same
    class the mention-resolution work already fixed on every other card."""
    src = (Path(__file__).resolve().parents[1]
           / "src" / "cora" / "app.py").read_text(encoding="utf-8")
    assert "correction_text=_correction_text," in src


# ── the report explains its structural zero ─────────────────────────────────

def test_a_zero_correction_count_carries_its_explanation():
    import run_feedback_health_report as rfh
    src = Path(rfh.__file__).read_text(encoding="utf-8")
    assert "if total_corrections == 0:" in src
    assert "NOT evidence" in src
    assert "Event Subscriptions" in src


# ── the stuck-task detection the docstring promised ─────────────────────────

def test_a_task_stuck_in_running_is_surfaced():
    _c, warn, ok = nhc._classify_task_states(
        {"cowork-cora-x": "Running", "cowork-cora-y": "Running"},
        set(), set(), {"cowork-cora-x"})
    assert ok == 1
    assert len(warn) == 1
    assert "stuck in Running" in warn[0]
    assert "schtasks /End" in warn[0], "the warning must name the remedy"


def test_a_briefly_running_task_is_still_ok():
    _c, warn, ok = nhc._classify_task_states(
        {"cowork-cora-x": "Running"}, set(), set(), set())
    assert ok == 1 and warn == []


def test_the_always_on_service_is_never_called_stuck():
    """cowork-cora-service is SUPPOSED to be Running for weeks."""
    _c, warn, ok = nhc._classify_task_states(
        {"cowork-cora-service": "Running"}, set(), {"cowork-cora-service"},
        {"cowork-cora-service"})
    assert ok == 1 and warn == []


def test_stuck_detection_parses_a_real_schtasks_listing():
    listing = (
        "Folder: \\\r\n"
        "HostName:                             DESKTOP\r\n"
        "TaskName:                             \\cowork-cora-wedged\r\n"
        "Last Run Time:                        8/24/2026 6:05:02 AM\r\n"
        "Status:                               Running\r\n"
    )
    with patch.object(nhc.subprocess, "run") as run:
        run.return_value.stdout = listing
        stuck = nhc._stuck_running_tasks(
            ["cowork-cora-wedged"],
            now=datetime(2026, 8, 24, 15, 45))
    assert stuck == {"cowork-cora-wedged"}


def test_a_recent_start_is_not_stuck():
    listing = ("TaskName:  \\cowork-cora-busy\r\n"
               "Last Run Time:  8/24/2026 3:00:00 PM\r\n")
    with patch.object(nhc.subprocess, "run") as run:
        run.return_value.stdout = listing
        assert nhc._stuck_running_tasks(
            ["cowork-cora-busy"], now=datetime(2026, 8, 24, 15, 45)) == set()


@pytest.mark.parametrize("stdout", ["", "garbage", "Last Run Time:  never\r\n"])
def test_unparseable_output_never_reports_stuck(stdout):
    """Fail-soft in the safe direction: a false "your task is wedged" sends
    Harrison to an elevated shell for nothing."""
    with patch.object(nhc.subprocess, "run") as run:
        run.return_value.stdout = stdout
        assert nhc._stuck_running_tasks(["cowork-cora-x"]) == set()


def test_a_failing_query_never_reports_stuck():
    with patch.object(nhc.subprocess, "run", side_effect=OSError("boom")):
        assert nhc._stuck_running_tasks(["cowork-cora-x"]) == set()


def test_no_running_tasks_makes_no_calls():
    with patch.object(nhc.subprocess, "run") as run:
        assert nhc._stuck_running_tasks([]) == set()
        run.assert_not_called()


def test_the_docstring_no_longer_claims_an_autofix_that_does_not_exist():
    src = Path(nhc.__file__).read_text(encoding="utf-8")
    head = src[:src.index("import ")]
    auto = head[head.index("Auto-fixes (applied immediately"):]
    auto = auto[:auto.index("Detections that WARN")]
    # The Auto-fixes list must claim ONLY what the code actually does.
    assert "heartbeat" in auto
    assert "scheduled task" not in auto.lower(),         "the docstring is advertising a scheduled-task auto-fix again"
    assert "Detections that WARN rather than auto-fix" in head
