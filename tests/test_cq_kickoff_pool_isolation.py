"""D-051 Code #15 round 2, lens#r2-0 (MEDIUM): a pooled code-queue press must never
outlive its test's isolation, and nothing under pytest may write the REAL Founder-OS
backlog or kickoff folder.

The incident (2026-09-25 04:54Z): a scratch test pressed the real handle_cq_stage with
a gated generator. Its bodies ran on app._CQ_KICKOFF_POOL (process-global, s6#0) and
resumed AFTER the test's monkeypatch undo, so they appended an orphan `staged` event
to a real data/state/code-session-queue.jsonl and re-rendered the LIVE
G:\\...\\code-session-backlog.md as "0 item(s)". code_queue's own leak guard
(_backlog_write_would_leak) reads "not a test" once _EVENT_LEDGER is restored.

  * the conftest autouse fixture gives every test its own cq-kickoff pool and drains
    it BEFORE monkeypatch undoes anything (TestPooledBodyCannotOutliveItsTest);
  * code_queue refuses the real backlog / kickoff target whenever pytest is loaded,
    whatever state the redirects are in (TestRealFounderOsBeltUnderPytest).

Nothing here reaches a real writer: the pooled probe body only READS module state,
and every belt test replaces drive_io.write_text_atomic with a recorder.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import cora.app as app_module
from cora import code_queue as cq

HARRISON = "U0B2RM2JYJ1"


def _press(value="cq-bbbbbbbbbbbb"):
    return {"actions": [{"value": value}], "user": {"id": HARRISON},
            "channel": {"id": "D1"},
            "message": {"ts": "9.9", "blocks": [
                {"type": "section", "text": {"type": "mrkdwn", "text": "card"}},
                {"type": "actions", "block_id": "a0", "elements": []}]}}


class TestPooledBodyCannotOutliveItsTest:
    """The ordering proof. The pooled body blocks until the CLASS-scoped probe's
    teardown releases it (bounded at 1s so a drained pool never deadlocks), i.e.
    until after this test's whole function-scoped teardown, monkeypatch undo
    included, unless something drains the pool first. Without the conftest drain
    the body wakes on the shared pool after the undo, reads the REAL default ledger
    constant and a missing env var, and the probe fails. With it, the per-test
    pool's shutdown(wait=True) holds teardown until the body has finished while
    this test's redirects are still in place."""

    @pytest.fixture(scope="class")
    def probe(self):
        rec: dict = {"release": threading.Event(), "done": threading.Event()}
        yield rec
        # Runs after the test's function-scoped teardown (monkeypatch undo included).
        finished_before_undo = rec["done"].is_set()
        rec["release"].set()
        rec["done"].wait(10)           # never leave the probe thread behind
        assert finished_before_undo, (
            "a pooled code-queue body was still pending after its test's teardown "
            "-- it would have run against the REAL ledger / Founder-OS paths")
        assert rec.get("seen_ledger") == rec["expected_ledger"], rec.get("seen_ledger")
        assert rec.get("seen_env") == "inside", rec.get("seen_env")

    def test_a_pooled_stage_body_finishes_inside_this_tests_redirects(
            self, probe, monkeypatch):
        monkeypatch.setenv("CQ_POOL_PROBE", "inside")
        # the autouse _LEDGER_CONSTS redirect -- the constant the incident's body wrote
        expected = cq._EVENT_LEDGER
        assert Path(expected).resolve() != cq._DEFAULT_EVENT_LEDGER.resolve()
        probe["expected_ledger"] = expected

        def _probe_body(body, client, action_id):
            try:
                probe["release"].wait(1.0)
                probe["seen_ledger"] = cq._EVENT_LEDGER
                probe["seen_env"] = os.environ.get("CQ_POOL_PROBE")
                probe["thread"] = threading.current_thread().name
            finally:
                probe["done"].set()

        # handle_cq_stage -> _submit_code_queue_button captures this at submit time
        monkeypatch.setattr(app_module, "_handle_code_queue_button", _probe_body)
        ack = MagicMock()
        app_module.handle_cq_stage(ack, _press(), MagicMock())
        ack.assert_called_once_with()
        # really pooled: the listener returned while the body is still blocked
        assert not probe["done"].is_set()


# ─────────────────────────────────────────────────────────────────────────────
# The code belt: under pytest the real Founder-OS targets are never written
# ─────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def recorder(monkeypatch):
    """drive_io.write_text_atomic -> an in-memory recorder (never a real write, with
    or without the belt under test)."""
    writes: list[str] = []

    def _record(path, text, **kw):
        writes.append(str(path))
    monkeypatch.setattr(cq.drive_io, "write_text_atomic", _record)
    return writes


class TestRealFounderOsBeltUnderPytest:
    def test_render_backlog_refuses_the_real_backlog_in_the_torn_down_state(
            self, monkeypatch, recorder):
        """Exactly the incident's state: _EVENT_LEDGER restored to its real default
        (so _backlog_write_would_leak() is False) and FOUNDER_OS_ROOT unset (so
        backlog_path() is the live G: file). Under pytest the render is refused."""
        monkeypatch.setattr(cq, "_EVENT_LEDGER", cq._DEFAULT_EVENT_LEDGER)
        monkeypatch.delenv("FOUNDER_OS_ROOT", raising=False)
        monkeypatch.setattr(cq, "render_backlog_text", lambda items=None: "x")
        assert cq._backlog_write_would_leak() is False       # the old guard is blind here
        assert cq.render_backlog() is False
        assert recorder == []

    def test_render_backlog_refuses_even_an_explicit_real_root(self, monkeypatch, recorder):
        """The torn-down state with FOUNDER_OS_ROOT set -- to the real root -- is
        refused too: "explicitly set" is not the same as "redirected"."""
        monkeypatch.setattr(cq, "_EVENT_LEDGER", cq._DEFAULT_EVENT_LEDGER)
        monkeypatch.setenv("FOUNDER_OS_ROOT", r"G:\My Drive\HJR-Founder-OS")
        monkeypatch.setattr(cq, "render_backlog_text", lambda items=None: "x")
        assert cq.render_backlog() is False
        assert recorder == []

    def test_render_backlog_still_writes_a_redirected_root(self, tmp_path, monkeypatch,
                                                           recorder):
        monkeypatch.setenv("FOUNDER_OS_ROOT", str(tmp_path / "founder-os"))
        monkeypatch.setattr(cq, "render_backlog_text", lambda items=None: "x")
        assert cq.render_backlog() is True
        assert recorder == [str(cq.backlog_path())]

    def test_kickoff_writer_refuses_the_real_founder_os_notes(self, tmp_path, monkeypatch,
                                                              recorder):
        """A torn-down body that reaches generate_kickoff_prompt would otherwise write
        the prompt into the live Founder-OS _notes. Refused; nothing written."""
        monkeypatch.setattr(cq, "_EVENT_LEDGER", cq._DEFAULT_EVENT_LEDGER)
        monkeypatch.delenv("FOUNDER_OS_ROOT", raising=False)
        monkeypatch.setattr(cq, "_NOTES_DIR", tmp_path / "repo-notes")
        path, mis_homed = cq._write_prompt_file("body", "cora-code-prompt-x.md")
        assert (path, mis_homed) == (None, False)
        assert recorder == []
        assert not (tmp_path / "repo-notes").exists()      # no fallback write either

    def test_kickoff_writer_still_writes_a_redirected_root(self, tmp_path, monkeypatch,
                                                           recorder):
        monkeypatch.setenv("FOUNDER_OS_ROOT", str(tmp_path / "founder-os"))
        path, mis_homed = cq._write_prompt_file("body", "cora-code-prompt-x.md")
        assert mis_homed is False
        assert path == str(cq.founder_os_notes_dir() / "cora-code-prompt-x.md")
        assert recorder == [path]

    def test_the_belt_is_pytest_only(self):
        """Production (the bot, every scheduled script) never imports pytest, so the
        predicate is False there whatever the target. (pytest is out of sys.modules
        only for the one call, restored before anything else runs.)"""
        import sys
        real = cq._real_backlog_path()
        assert cq._real_founder_os_target_under_pytest(real, real) is True
        saved = sys.modules.pop("pytest")
        try:
            outside = cq._real_founder_os_target_under_pytest(real, real)
        finally:
            sys.modules["pytest"] = saved
        assert outside is False
        assert cq._real_founder_os_target_under_pytest(
            Path("C:/elsewhere/code-session-backlog.md"), real) is False
