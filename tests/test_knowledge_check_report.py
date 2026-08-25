"""C11 (cq-affac22a9723): knowledge-check participation, actually delivered.

THE SEED'S CORE PREMISE IS OVERTURNED. It says participation is "structurally
unmeasurable" because the 7-day answer TTL erases the evidence, and that the
weekly report "spec'd 8/11 was never built". The first half is wrong and it
changes the whole shape of the work.

There IS a complete append-only per-user event log --
data/state/knowledge-check-events.jsonl, 290 rows covering every pilot day from
2026-08-11 -- and `participation_stats` / `participation_report` ALREADY fold it
into exactly the per-user asked / answered / confirmed / skipped / no-response
breakdown the slice asks for. The 7-day TTL applies only to
`expire_stale_answers`, which sweeps kc-entry BLOCKS out of the known-answers
files; it never touches the event log, and nothing else prunes it either
(compact_logs globs data/*.jsonl NON-recursively, so data/state/ never matches at
any size). The seed reasoned from the known-answers ARTIFACT and never looked at
the module's own state.

So this is "render and deliver", not "start recording, then report". Two things
were genuinely missing, and both are here:

  IT WENT TO A LOG FILE. The only consumer of participation_report is the
  runner's `--report` flag, which log.info()s the lines and returns. No DM, no
  --days flag (so it always used the 30-day default, wrong for a weekly report),
  no scheduled task. The setup script's own help text says "Participation report
  (feeds Hannah's Monday audit): --report" -- a manual command standing in for
  the automation.

  THE PER-PERSON LINE DROPPED THE ANSWER. participation_stats computes
  no_response / no_confirm / user_skipped per person and the rendered line
  printed none of them -- so the one surface a reader sees could not answer "who
  is not participating", which is the entire question the report exists for.

RECIPIENT IS HANNAH, per the 2026-08-11 addendum Harrison locked ("DMs Hannah
directly (not a channel post)"). She owns training readiness and runs the Monday
audit this feeds.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from cora import knowledge_check as kc

_SCRIPTS = str(Path(__file__).resolve().parents[1] / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

HANNAH = "U0B3AEQS0NB"


# ── the per-person line answers the question ────────────────────────────────

def test_the_per_person_line_carries_the_non_participation_columns(monkeypatch):
    monkeypatch.setattr(kc, "participation_stats", lambda **kw: {
        "since": "2026-08-17",
        "totals": {"asked": 6, "answered": 1, "confirmed": 1, "user_skipped": 2,
                   "no_response": 3, "no_confirm": 0, "pool_exhausted": 4,
                   "held_collision": 0, "failed": 0, "reserved_never_sent": 0},
        "people": {"U1": {"name": "Shaun Hawkins", "entity": "LEX-LLC",
                          "asked": 3, "answered": 0, "confirmed": 0,
                          "user_skipped": 2, "no_response": 1, "no_confirm": 0,
                          "pool_exhausted": 3}},
    })
    line = [l for l in kc.participation_report(days=7) if "Shaun" in l][0]
    for token in ("asked  3", "answered  0", "confirmed  0", "skipped  2",
                  "no-response  1", "no-confirm  0", "pool-exhausted  3"):
        assert token in line, f"{token!r} missing from {line!r}"


def test_the_report_runs_against_the_live_event_log():
    """The log is not TTL'd and the pilot's full history is queryable -- the fact
    the seed's premise turned on."""
    lines = kc.participation_report(days=7)
    assert lines and lines[0].startswith("Knowledge check -- last 7d")
    assert any("asked" in l for l in lines)


# ── delivery ────────────────────────────────────────────────────────────────

def test_the_report_dms_hannah_and_nobody_else(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(kc, "enabled", lambda: True)
    client = MagicMock()
    client.conversations_open.return_value = {"channel": {"id": "D_HANNAH"}}
    assert kc.post_participation_report(["a line"],
                                        _client_factory=lambda: client) is True
    assert client.conversations_open.call_args.kwargs["users"] == [HANNAH]
    assert client.chat_postMessage.call_args.kwargs["channel"] == "D_HANNAH"


def test_there_is_no_recipient_parameter():
    """A parameter is how a report about named individuals' participation becomes
    a report to anyone. Same contract strategy_memo pins for its Harrison-only
    memo."""
    import inspect
    sig = inspect.signature(kc.post_participation_report)
    assert set(sig.parameters) == {"lines", "dry_run", "_client_factory"}
    src = inspect.getsource(kc.post_participation_report)
    assert "HANNAH_SLACK_USER_ID" in src


def test_dry_run_sends_nothing(monkeypatch):
    monkeypatch.setattr(kc, "enabled", lambda: True)
    client = MagicMock()
    assert kc.post_participation_report(["x"], dry_run=True,
                                        _client_factory=lambda: client) is False
    client.chat_postMessage.assert_not_called()


def test_a_disabled_pilot_sends_nothing(monkeypatch):
    """`dry` must mean no sends anywhere -- the same reasoning promote() applies
    -- and `off` is the documented kill switch."""
    monkeypatch.setattr(kc, "enabled", lambda: False)
    client = MagicMock()
    assert kc.post_participation_report(["x"],
                                        _client_factory=lambda: client) is False
    client.chat_postMessage.assert_not_called()


def test_an_empty_report_is_not_posted(monkeypatch):
    monkeypatch.setattr(kc, "enabled", lambda: True)
    client = MagicMock()
    assert kc.post_participation_report([], _client_factory=lambda: client) is False
    client.chat_postMessage.assert_not_called()


def test_a_slack_failure_never_raises(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setattr(kc, "enabled", lambda: True)
    client = MagicMock()
    client.conversations_open.side_effect = RuntimeError("boom")
    assert kc.post_participation_report(["x"],
                                        _client_factory=lambda: client) is False


# ── the runner ──────────────────────────────────────────────────────────────

def test_the_weekly_default_is_seven_days_not_thirty():
    """The runner's existing --report silently uses the 30-day default, which
    would have made week-over-week movement invisible."""
    import run_knowledge_check_report as r
    captured = {}
    import cora.knowledge_check as _kc
    orig = _kc.participation_report

    def _spy(days=30, today=None):
        captured["days"] = days
        return ["line"]

    _kc.participation_report = _spy
    try:
        assert r.main(["--dry-run"]) == 0
    finally:
        _kc.participation_report = orig
    assert captured["days"] == 7


def test_the_report_is_logged_even_when_it_is_not_sent(caplog):
    """A paused pilot must stay visible in the log rather than going silent."""
    import run_knowledge_check_report as r
    with caplog.at_level("INFO"):
        r.main(["--dry-run", "--days", "7"])
    assert any("Knowledge check -- last 7d" in rec.getMessage()
               for rec in caplog.records)


def test_the_report_script_cannot_ask_anyone_anything():
    """The whole reason it is a separate script: its Monday 07:20 trigger sits an
    hour BEFORE the 08:05 ask run, and a regression that let it ask would fire
    the day's DMs early and make the real run skip everyone as already-handled."""
    src = (Path(__file__).resolve().parents[1]
           / "scripts" / "run_knowledge_check_report.py").read_text(encoding="utf-8")
    for forbidden in ("send_check", "post_check", "reserve", "run_daily", "ask_"):
        assert forbidden not in src, forbidden


def test_the_task_script_is_ascii_only():
    """D-016: PowerShell 5.1 reads UTF-8 as Windows-1252."""
    raw = (Path(__file__).resolve().parents[1] / "deployment"
           / "setup-knowledge-check-report-task.ps1").read_bytes()
    assert all(b < 128 for b in raw)


def test_the_task_uses_the_venv_python_and_a_distinct_minute():
    """D-005, plus: the weekly health metric alarms on two tasks sharing one
    clock time inside 03:00-09:00."""
    text = (Path(__file__).resolve().parents[1] / "deployment"
            / "setup-knowledge-check-report-task.ps1").read_text(encoding="utf-8")
    assert ".venv\\Scripts\\python.exe" in text
    assert "uv run" not in text
    assert '$HourMin    = "07:20"' in text
