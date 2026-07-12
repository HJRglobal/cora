"""Fold 2 (2026-07-10): daily-briefing crash VISIBILITY.

The 7/10 catch-up fire died with host LastTaskResult 32212 and left no trace
(run_start with no run_end, no dated log). These tests cover the added
visibility: a per-day FileHandler and a top-level crash catch that logs the
traceback and drops a run_crash audit line before exiting nonzero.

The single-instance lock already existed and is NOT re-added here (verified
2026-07-10: the 7/9 'triple run_start' was dev/probe traffic, not a double-fire).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import run_daily_briefing as rdb  # noqa: E402


@pytest.fixture()
def clean_root_handlers():
    """Snapshot + restore root logger handlers so an attached FileHandler never
    leaks into (and holds a file open for) the rest of the session."""
    root = logging.getLogger()
    before = list(root.handlers)
    yield
    for h in list(root.handlers):
        if h not in before:
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass


# ── dated FileHandler ─────────────────────────────────────────────────────────

def test_attach_creates_dated_log_and_captures(clean_root_handlers, monkeypatch, tmp_path):
    monkeypatch.setattr(rdb, "_DATED_LOG_DIR", tmp_path)
    path = rdb._attach_dated_file_handler()
    assert path is not None
    assert path.parent == tmp_path
    assert path.name.startswith("daily-briefing-") and path.name.endswith(".log")
    rdb.log.warning("crash-visibility-probe-line")
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:
            pass
    assert path.exists()
    assert "crash-visibility-probe-line" in path.read_text(encoding="utf-8")


def test_attach_is_idempotent(clean_root_handlers, monkeypatch, tmp_path):
    monkeypatch.setattr(rdb, "_DATED_LOG_DIR", tmp_path)
    root = logging.getLogger()
    n0 = len(root.handlers)
    rdb._attach_dated_file_handler()
    rdb._attach_dated_file_handler()
    added = len(root.handlers) - n0
    assert added == 1  # second call is a no-op


def test_attach_fail_soft_returns_none(clean_root_handlers, monkeypatch):
    # A directory that can't be made (mkdir raises) must not break the run.
    class _Boom:
        def mkdir(self, *a, **k):
            raise OSError("nope")
    monkeypatch.setattr(rdb, "_DATED_LOG_DIR", _Boom())
    assert rdb._attach_dated_file_handler() is None


# ── crash catch (run_crash audit + nonzero exit) ───────────────────────────────

def test_run_crash_is_caught_and_audited(monkeypatch):
    captured: list[dict] = []
    monkeypatch.setattr(rdb, "_write_audit", lambda entries: captured.extend(entries))

    def _boom(args, mode):
        raise RuntimeError("kaboom in _run")
    monkeypatch.setattr(rdb, "_run", _boom)

    # --dry-run bypasses the run lock, so no lockfile side effects.
    rc = rdb.main(["--dry-run"])
    assert rc == 1
    events = [e.get("event") for e in captured]
    assert "run_crash" in events
    crash = next(e for e in captured if e.get("event") == "run_crash")
    assert crash["mode"] == "review_driven"
    assert "traceback" in crash["error"].lower() or "exception" in crash["error"].lower()


def test_normal_return_code_not_masked(monkeypatch):
    """A clean _run return (e.g. 2 = partial) must pass through unchanged, not be
    swallowed by the crash catch."""
    monkeypatch.setattr(rdb, "_write_audit", lambda entries: None)
    monkeypatch.setattr(rdb, "_run", lambda args, mode: 2)
    assert rdb.main(["--dry-run"]) == 2


# ── F-14a (2026-07-12): the per-user synthesis call is BOUNDED ────────────────
def test_synthesize_bounds_the_anthropic_call(monkeypatch):
    """0xC000013A was an external SIGKILL because a stalled, UNTIMED messages.create
    ran past the task limit. The call must now carry a hard timeout so a stall fails
    fast (the per-user try/except then fails soft + the budget check binds)."""
    import types

    captured = {}

    class _FakeMessages:
        def create(self, **kwargs):
            captured["create_timeout"] = kwargs.get("timeout")
            msg = types.SimpleNamespace()
            block = types.SimpleNamespace(text="ok briefing")
            msg.content = [block]
            return msg

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            captured["client_timeout"] = kwargs.get("timeout")
            self.messages = _FakeMessages()

    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.Anthropic = _FakeClient
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)

    rec = rdb.RoleRecord(slack_id="U1", name="Test User", role="ops", entity="FNDR")
    monkeypatch.setattr(rdb, "_compose_sections", lambda r: "sections")
    monkeypatch.setattr(rdb, "_query_user_chunks", lambda name, first: [])

    out = rdb.build_user_briefing(rec, api_key="sk-test", today_str="2026-07-12")
    assert out == "ok briefing"
    assert captured["client_timeout"] == rdb._ANTHROPIC_TIMEOUT_S
    assert captured["create_timeout"] == rdb._ANTHROPIC_TIMEOUT_S
