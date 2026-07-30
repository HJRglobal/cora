"""Read-only MCP server (src/cora/mcp_server.py) — the founder-scoped read surface.

Covers the D-051 load-bearing invariants:
  * read-only BY CONSTRUCTION (mode=ro; writes physically raise; no write tool),
  * KB access goes through store.search (in-SQL user_note exclusion) — never
    search_user_notes / search_owned,
  * founder-surface PHI scrub PARITY with context_loader (LEX scrub / non-LEX
    withhold), treated as non-custodian,
  * entity-mapping parity with the founder retrieval path,
  * decisions_search filters to the static_md founder TOM + decisions.md,
  * health() is read-only (never restarts), and
  * every content-bearing result carries the prompt-injection provenance framing.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")

import cora.context_loader as cl  # noqa: E402
import cora.mcp_server as mcp_server  # noqa: E402
from cora.knowledge_base.store import KnowledgeBase, SearchResult  # noqa: E402

_PHI_TEXT = (
    "Client Bob Smith's care plan needs review. Shaun Hawkins will follow up. "
    "DOB 03/15/1990."
)


def _result(content: str, *, source: str = "asana", entity: str = "LEX",
            title: str = "t", source_id: str = "s1", distance: float = 0.2,
            deep_link: str = "") -> SearchResult:
    return SearchResult(
        chunk_id="c1", source=source, source_id=source_id, entity=entity,
        title=title, content=content, deep_link=deep_link,
        date_modified=None, distance=distance,
    )


def _patch_staff(monkeypatch, names=("Shaun Hawkins",)):
    monkeypatch.setattr(
        cl.org_roles, "all_roles",
        lambda: [SimpleNamespace(name=n) for n in names],
    )


class _FakeKB:
    """Records the last search() kwargs; returns canned results. Raises on any
    write method so a test can prove the surface never mutates."""

    def __init__(self, results):
        self._results = list(results)
        self.calls: list[dict] = []

    def search(self, query, **kwargs):
        self.calls.append({"query": query, **kwargs})
        return list(self._results)

    def search_user_notes(self, *a, **k):  # pragma: no cover - must never be called
        raise AssertionError("mcp_server must not call search_user_notes")

    def search_owned(self, *a, **k):  # pragma: no cover
        raise AssertionError("mcp_server must not call search_owned")


def _wire(monkeypatch, results):
    fake = _FakeKB(results)
    monkeypatch.setattr(mcp_server, "_get_ro_kb", lambda: fake)
    return fake


# ── A. Read-only BY CONSTRUCTION ─────────────────────────────────────────────
def test_open_readonly_blocks_every_write(tmp_path):
    db = tmp_path / "kb.db"
    KnowledgeBase(db).close()  # create + init schema (writable)

    ro = KnowledgeBase.open_readonly(db)
    assert ro.read_only is True
    # Reads work.
    assert isinstance(ro.stats().get("total_chunks", 0), int)
    # Every write raises on the mode=ro handle — the path does not exist.
    for label, fn in [
        ("CREATE", lambda: ro._conn.execute("CREATE TABLE _p(x)")),
        ("INSERT", lambda: ro._conn.execute(
            "INSERT INTO knowledge_chunks(chunk_id,source,source_id,entity,content,ingested_at)"
            " VALUES('z','s','s','FNDR','x',1)")),
        ("set_checkpoint", lambda: ro.set_checkpoint("_p", {"x": 1})),
        ("set_sync_state", lambda: ro.set_sync_state("_p", 1)),
    ]:
        with pytest.raises(sqlite3.OperationalError):
            fn()
    ro.close()


def test_open_readonly_missing_db_raises(tmp_path):
    with pytest.raises(sqlite3.OperationalError):
        KnowledgeBase.open_readonly(tmp_path / "nope.db")


def test_no_write_tool_is_exposed():
    names = {s["name"] for s in mcp_server._TOOL_SPECS}
    assert names == {
        "cora_kb_search", "cora_decisions_search", "cora_known_answers",
        "cora_code_queue", "cora_health",
    }
    forbidden = ("create", "update", "delete", "set", "write", "upsert", "remove", "add")
    for n in names:
        assert not any(tok in n.lower() for tok in forbidden), n


def test_module_never_references_write_methods():
    src = Path(mcp_server.__file__).read_text(encoding="utf-8")
    for banned in ("search_user_notes", "search_owned", "upsert_documents",
                   "set_checkpoint", "delete_user_note"):
        assert banned not in src, f"mcp_server must not reference {banned}"


# ── B. Founder-surface PHI scrub PARITY (non-custodian) ──────────────────────
def test_kb_search_lex_is_scrubbed_like_founder_path(monkeypatch):
    _patch_staff(monkeypatch)
    _wire(monkeypatch, [_result(_PHI_TEXT, entity="LEX",
                                title="Bob Smith Care Plan",
                                deep_link="<https://x|Bob Smith Care Plan>")])
    out = mcp_server.kb_search("care plan", entity="LEX", limit=5)
    assert out["count"] == 1
    r = out["results"][0]
    assert "Bob Smith" not in r["content"]
    assert "1990" not in r["content"]
    assert "Shaun Hawkins" in r["content"]        # staff preserved
    assert r["title"] == "LEX knowledge base entry"  # citation neutralized
    assert r["deep_link"] == ""
    # Parity: the same scrub helper the founder path uses.
    _patch_staff(monkeypatch)
    direct = cl._apply_lex_phi_scrub([_result(_PHI_TEXT, entity="LEX")])
    assert "Bob Smith" not in direct[0].content


def test_kb_search_lex_subentity_scopes_and_scrubs(monkeypatch):
    _patch_staff(monkeypatch)
    fake = _wire(monkeypatch, [_result(_PHI_TEXT, entity="LEX")])
    out = mcp_server.kb_search("q", entity="LEX-LLC", limit=4)
    call = fake.calls[-1]
    assert call["entity"] == "LEX"              # mapped to parent
    assert call["sub_entity"] == "LEX-LLC"      # strict sub-entity scoping
    assert call["include_fndr"] is False        # sub-entity firewall
    assert out["results"][0]["title"] == "LEX knowledge base entry"


def test_kb_search_nonlex_phi_is_withheld(monkeypatch):
    _patch_staff(monkeypatch)
    _wire(monkeypatch, [_result(_PHI_TEXT, source="gmail", entity="F3E")])
    out = mcp_server.kb_search("q", entity="F3E", limit=4)
    assert out["count"] == 0  # LEX-PHI mis-tagged under F3E -> withheld


def test_kb_search_nonlex_ordinary_prose_passes(monkeypatch):
    _patch_staff(monkeypatch)
    body = "The F3 Energy Walmart launch ships in Q3; margins hold at 42 percent."
    _wire(monkeypatch, [_result(body, source="slack", entity="F3E", title="F3E note")])
    out = mcp_server.kb_search("launch", entity="F3E", limit=4)
    assert out["count"] == 1
    assert "Walmart" in out["results"][0]["content"]


# ── C. Entity-mapping parity + scoping knobs ─────────────────────────────────
@pytest.mark.parametrize("entity,exp_kb,exp_sub,exp_fndr", [
    (None, "FNDR", None, True),
    ("FNDR", "FNDR", None, True),
    ("F3E", "F3E", None, True),
    ("OSNGF", "OSN", None, True),
    ("HJRP-1337", "HJRP", None, True),
    ("F3", "F3E", None, True),
    ("LEX", "LEX", None, True),
    ("LEX-LTS", "LEX", "LEX-LTS", False),
])
def test_kb_search_entity_mapping_matches_founder_path(monkeypatch, entity, exp_kb, exp_sub, exp_fndr):
    _patch_staff(monkeypatch)
    fake = _wire(monkeypatch, [])
    mcp_server.kb_search("q", entity=entity, limit=3)
    call = fake.calls[-1]
    assert call["entity"] == exp_kb
    assert call["sub_entity"] == exp_sub
    assert call["include_fndr"] is exp_fndr


def test_kb_search_distance_gate(monkeypatch):
    _patch_staff(monkeypatch)
    _wire(monkeypatch, [
        _result("near", source="slack", entity="F3E", title="a", distance=0.5),
        _result("far", source="slack", entity="F3E", title="b",
                distance=cl._KB_MAX_DISTANCE + 0.5),
    ])
    out = mcp_server.kb_search("q", entity="F3E", limit=8)
    assert out["count"] == 1
    assert out["results"][0]["content"] == "near"


def test_kb_search_limit_is_clamped(monkeypatch):
    _patch_staff(monkeypatch)
    fake = _wire(monkeypatch, [])
    mcp_server.kb_search("q", entity="F3E", limit=999)
    assert fake.calls[-1]["k"] == mcp_server._MAX_LIMIT
    mcp_server.kb_search("q", entity="F3E", limit=None)
    assert fake.calls[-1]["k"] == mcp_server._DEFAULT_LIMIT
    mcp_server.kb_search("q", entity="F3E", limit=0)
    assert fake.calls[-1]["k"] == 1


def test_kb_search_uses_search_not_notes(monkeypatch):
    # _FakeKB.search_user_notes / search_owned raise; reaching them fails the test.
    _patch_staff(monkeypatch)
    fake = _wire(monkeypatch, [_result("x", source="slack", entity="F3E", title="a")])
    mcp_server.kb_search("q", entity="F3E")
    assert fake.calls, "kb.search must be the query path"


def test_kb_search_empty_query_errors(monkeypatch):
    out = mcp_server.kb_search("   ", entity="F3E")
    assert "error" in out and out["results"] == []


def test_kb_search_no_kb_is_graceful(monkeypatch):
    monkeypatch.setattr(mcp_server, "_get_ro_kb", lambda: None)
    out = mcp_server.kb_search("q", entity="F3E")
    assert out["count"] == 0
    assert "unavailable" in out["text"].lower()


# ── D. decisions_search filter ───────────────────────────────────────────────
def test_decisions_search_filters_to_founder_tom_and_decisions(monkeypatch):
    _patch_staff(monkeypatch)
    _wire(monkeypatch, [
        _result("tom body", source="static_md", entity="FNDR",
                source_id="CLAUDE.md", title="Claude"),
        _result("dec body", source="static_md", entity="FNDR",
                source_id="memory\\decisions.md", title="Decisions"),
        _result("other md", source="static_md", entity="FNDR",
                source_id="02-F3-Energy\\CLAUDE.md", title="F3E brief"),
        _result("slack msg", source="slack", entity="FNDR",
                source_id="x", title="chatter"),
        _result("proj decisions", source="static_md", entity="FNDR",
                source_id="06-HJR-Properties/rogers-ranch/memory/decisions.md",
                title="RR decisions"),
    ])
    out = mcp_server.decisions_search("anything", limit=8)
    titles = {r["title"] for r in out["results"]}
    # founder CLAUDE.md + any *decisions.md, but NOT a project sub-CLAUDE.md or slack.
    assert "Claude" in titles
    assert "Decisions" in titles
    assert "RR decisions" in titles
    assert "F3E brief" not in titles
    assert "chatter" not in titles


def test_decisions_search_searches_fndr(monkeypatch):
    _patch_staff(monkeypatch)
    fake = _wire(monkeypatch, [])
    mcp_server.decisions_search("q", limit=5)
    assert fake.calls[-1]["entity"] == "FNDR"


# ── E. known_answers ─────────────────────────────────────────────────────────
def _patch_ka(monkeypatch, *, exists=True, text="## Known facts\n\nHQ is Phoenix.",
              raise_unavailable=False):
    def _exists(path, **k):
        return exists

    def _read(path, **k):
        if raise_unavailable:
            raise cl.drive_io.DriveUnavailable("G: gone")
        return text
    monkeypatch.setattr(mcp_server.drive_io, "exists", _exists)
    monkeypatch.setattr(mcp_server.drive_io, "read_text", _read)


def test_known_answers_valid_entity(monkeypatch):
    _patch_ka(monkeypatch)
    out = mcp_server.known_answers("F3E")
    assert out["found"] is True
    assert "Phoenix" in out["content"]
    assert out["file"] == "f3e.md"


def test_known_answers_lex_subentity_excluded(monkeypatch):
    _patch_ka(monkeypatch)
    out = mcp_server.known_answers("LEX-LLC")
    assert out["found"] is False
    assert "No known-answers surface" in out["message"]


def test_known_answers_lex_gm_allowed(monkeypatch):
    _patch_ka(monkeypatch)
    out = mcp_server.known_answers("LEX")
    assert out["found"] is True
    assert out["file"] == "lex.md"


def test_known_answers_unknown_entity(monkeypatch):
    out = mcp_server.known_answers("NOPE")
    assert out["found"] is False


def test_known_answers_drive_unavailable_is_graceful(monkeypatch):
    _patch_ka(monkeypatch, raise_unavailable=True)
    out = mcp_server.known_answers("F3E")
    assert out["found"] is False
    assert "unavailable" in out["message"].lower()


# ── F. code_queue_view ───────────────────────────────────────────────────────
def test_code_queue_view_returns_rendered_backlog(monkeypatch):
    from cora import code_queue
    monkeypatch.setattr(code_queue, "render_backlog_text", lambda: "# Cora Code-Session Backlog\n")
    out = mcp_server.code_queue_view()
    assert "Cora Code-Session Backlog" in out["text"]
    assert out["text"].startswith("[Cora code-session backlog")


# ── G. health() is READ-ONLY (never restarts) ────────────────────────────────
def test_health_is_read_only_and_never_restarts(monkeypatch):
    from cora import health_endpoint
    monkeypatch.setattr(health_endpoint, "heartbeat_age_seconds", lambda *a, **k: 42.0)
    monkeypatch.setattr(mcp_server, "_read_uptime_from_log", lambda: 1234)

    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(stdout="cowork-cora-service|Running|0\n")
    monkeypatch.setattr(mcp_server.subprocess, "run", _fake_run)

    out = mcp_server.health()
    assert out["alive"] is True
    assert out["uptime_seconds"] == 1234
    # The only subprocess it may run is a read-only query — never a restart verb.
    joined = " ".join(" ".join(map(str, c)) for c in calls)
    for verb in ("/Run", "/End", "Start-ScheduledTask", "Stop-ScheduledTask",
                 "Restart", "schtasks /Run"):
        assert verb not in joined


def test_health_stale_heartbeat_reported_not_acted(monkeypatch):
    from cora import health_endpoint
    monkeypatch.setattr(health_endpoint, "heartbeat_age_seconds", lambda *a, **k: 9999.0)
    monkeypatch.setattr(mcp_server, "_read_uptime_from_log", lambda: None)
    monkeypatch.setattr(mcp_server, "_read_task_last_results", lambda: {})
    out = mcp_server.health()
    assert out["alive"] is False  # stale, but reported (no restart path exists here)


def test_health_task_query_is_read_only():
    # The PowerShell query never mutates: only Get-* verbs.
    src = Path(mcp_server.__file__).read_text(encoding="utf-8")
    # locate the query string
    assert "Get-ScheduledTask" in src and "Get-ScheduledTaskInfo" in src
    assert "Start-ScheduledTask" not in src
    assert "Stop-ScheduledTask" not in src
    assert "schtasks" not in src.lower() or "/run" not in src.lower()


# ── H. provenance framing on every content-bearing result ────────────────────
def test_provenance_framing_present(monkeypatch):
    _patch_staff(monkeypatch)
    _wire(monkeypatch, [_result("body", source="slack", entity="F3E", title="a")])
    assert mcp_server.kb_search("q", entity="F3E")["text"].startswith("[Cora read-only KB")
    _wire(monkeypatch, [])
    assert mcp_server.decisions_search("q")["text"].startswith("[Cora read-only KB")
    _patch_ka(monkeypatch)
    assert mcp_server.known_answers("F3E")["text"].startswith("[Cora known-answers")


# ── I. _clamp_limit ──────────────────────────────────────────────────────────
def test_clamp_limit():
    assert mcp_server._clamp_limit(None) == mcp_server._DEFAULT_LIMIT
    assert mcp_server._clamp_limit(3) == 3
    assert mcp_server._clamp_limit(999) == mcp_server._MAX_LIMIT
    assert mcp_server._clamp_limit(0) == 1
    assert mcp_server._clamp_limit("nope") == mcp_server._DEFAULT_LIMIT


# ── J. server build (needs the mcp SDK installed) ────────────────────────────
def test_build_server_ok():
    pytest.importorskip("mcp")
    srv = mcp_server.build_server()
    assert srv is not None
    assert len(mcp_server._TOOL_SPECS) == 5
