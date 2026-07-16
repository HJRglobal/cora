"""Slice 2: the bot request path + in-process tools survive a G: mount outage.

These assert the load-bearing invariant of the 2026-07-16 hardening: a
drive_io.DriveUnavailable on any in-process G: read degrades gracefully and NEVER
propagates into the request handler (which is what froze the process on 2026-07-15).

The "heartbeat keeps beating while a G: read is stuck" guarantee is proven at the
primitive level by test_drive_io.py::test_hung_op_does_not_freeze_other_threads --
the heartbeat thread touches only C: and a hung G: read is parked on a disposable
worker with the GIL released, so an independent thread keeps making progress.
"""

from __future__ import annotations

import types

import pytest

# Import from `cora` (not `src.cora`) so the module objects here are IDENTICAL to the
# ones the production code imports (`from cora import ...`) -- critical, because the
# two import paths are distinct module trees, so a DriveUnavailable raised from the
# `src.cora` tree would NOT be caught by code that catches the `cora`-tree class.
from cora import context_loader, drive_io
from cora.tools import person_dossier, tool_dispatch


@pytest.fixture(autouse=True)
def _clean_state():
    drive_io.reset_state_for_tests()
    context_loader._cache.clear()
    yield
    context_loader._cache.clear()
    drive_io.reset_state_for_tests()


def _raise_unavailable(*_a, **_k):
    raise drive_io.DriveUnavailable("simulated G: outage")


# ── context_loader degradation ───────────────────────────────────────────────

def test_build_serves_degraded_when_no_cache_and_mount_gone(monkeypatch):
    """No cache + mount gone => minimal degraded static context, no exception."""
    monkeypatch.setattr(context_loader.drive_io, "exists", _raise_unavailable)
    monkeypatch.setattr(context_loader.drive_io, "read_text", _raise_unavailable)
    monkeypatch.setattr(context_loader.drive_io, "stat_mtime", _raise_unavailable)

    out = context_loader._load_static_context("F3E")
    assert out == context_loader._DEGRADED_STATIC_CONTEXT
    # A degraded result must NOT be cached (so the next request re-attempts G:).
    assert "F3E" not in context_loader._cache


def test_serves_fresh_cache_when_mtime_check_hits_outage(monkeypatch):
    """A fresh cache entry is served even if the per-request mtime check hits the
    outage -- the hot path never touches a dead mount when it has a valid cache."""
    import time
    context_loader._cache["F3E"] = ("CACHED BRIEF", time.monotonic(), 123.0)
    monkeypatch.setattr(context_loader, "_known_answers_mtime", _raise_unavailable)

    assert context_loader._load_static_context("F3E") == "CACHED BRIEF"


def test_serves_stale_cache_when_build_hits_outage(monkeypatch):
    """An EXPIRED cache entry is still served (stale-ok) when a rebuild hits the
    outage -- better a slightly-stale brief than a dead request."""
    import time
    # cached_at far in the past => TTL expired => rebuild attempted.
    context_loader._cache["F3E"] = ("STALE BRIEF", time.monotonic() - 100_000, None)
    # mtime check succeeds (mount "up" for the stat) but returns a changed value so
    # validity fails and we proceed to build...
    monkeypatch.setattr(context_loader, "_known_answers_mtime", lambda e: 999.0)
    # ...and the build hits the outage.
    monkeypatch.setattr(context_loader, "_build_static_context", _raise_unavailable)

    assert context_loader._load_static_context("F3E") == "STALE BRIEF"


def test_load_context_parts_survives_outage(monkeypatch):
    """The actual request-path entry point (app.py calls this) must return normally
    under a G: outage -- never raise into the handler."""
    monkeypatch.setattr(context_loader.drive_io, "exists", _raise_unavailable)
    monkeypatch.setattr(context_loader.drive_io, "read_text", _raise_unavailable)
    monkeypatch.setattr(context_loader.drive_io, "stat_mtime", _raise_unavailable)

    static_text, kb_text = context_loader.load_context_parts(
        "F3E", query="anything", skip_kb=True
    )
    assert static_text == context_loader._DEGRADED_STATIC_CONTEXT
    assert kb_text == ""


def test_healthy_build_still_works_through_drive_io(monkeypatch):
    """Happy-path invariant: with the mount up, the static context is built from the
    G: reads exactly as before (now routed through drive_io)."""
    monkeypatch.setattr(context_loader.drive_io, "exists", lambda *a, **k: True)
    monkeypatch.setattr(
        context_loader.drive_io, "stat_mtime", lambda *a, **k: 42.0
    )

    def _fake_read(path, **_k):
        p = str(path)
        if p.endswith("HJR-Founder-OS/CLAUDE.md") or p.endswith("HJR-Founder-OS\\CLAUDE.md"):
            return "FOUNDER BRIEF\n# Current State of the World\nvolatile"
        return "F3E ENTITY BRIEF"

    monkeypatch.setattr(context_loader.drive_io, "read_text", _fake_read)

    out = context_loader._load_static_context("F3E")
    assert "F3E ENTITY BRIEF" in out
    assert "FOUNDER BRIEF" in out
    # And it cached.
    assert "F3E" in context_loader._cache


# ── in-process tools degrade, never hang/crash ───────────────────────────────

def test_fndr_open_decisions_transient_message_on_outage(monkeypatch):
    # The tool does `from cora import drive_io` locally -> patching cora.drive_io
    # (the object we imported here) hits exactly the read it calls.
    monkeypatch.setattr(drive_io, "read_text", _raise_unavailable)
    out = tool_dispatch._tool_fndr_open_decisions("U0B2RM2JYJ1", "FNDR", {})
    assert "briefly unavailable" in out.lower()


def test_person_dossier_writeback_skips_on_outage(monkeypatch):
    """write-back returns False (skip) on a G: outage rather than hanging/raising;
    the synthesized reply is still returned to the user by the caller."""
    monkeypatch.setattr(person_dossier.drive_io, "exists", _raise_unavailable)
    fake_p = types.SimpleNamespace(
        dossier_filename="test-person.md", slug="test-person", name="Test Person"
    )
    assert person_dossier.write_back(fake_p, "some synthesized body") is False
