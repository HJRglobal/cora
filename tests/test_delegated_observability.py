"""S4/S5 tests: MCP + snapshot observability render ids-not-titles; the
acceptance script pins the 7-tool set; ingest tags _delegated-work/ chunks
bot_authored at the static-sync chokepoint."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

import cora.delegated_work as dw
import cora.mcp_server as mcp_server
import cora.session_snapshots as snaps

_REPO_ROOT = Path(__file__).resolve().parents[1]

SECRET = "hyper secret acquisition brief text"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(dw, "_BOT_LEDGER", tmp_path / "dw-bot.jsonl")
    monkeypatch.setattr(dw, "_RUNNER_LEDGER", tmp_path / "dw-runner.jsonl")
    monkeypatch.setenv("CORA_DELEGATED_WORK", "log")
    yield


def _seed(job_id="dw-obs111111111"):
    dw.append_bot_event({
        "event": "requested", "ts": dw._now_iso(), "job_id": job_id,
        "archetype": "doc_draft", "title": SECRET, "brief": SECRET,
        "requester": "U_X", "requester_name": "X", "entity": "F3E",
        "channel_id": "C_PRIV", "channel_name": "priv", "thread_ts": "",
        "deliverable": "md", "fingerprint": "fp-obs",
    })
    dw.append_bot_event({"event": "queued", "ts": dw._now_iso(), "job_id": job_id})


# ---------------------------------------------------------------------------
# MCP tool
# ---------------------------------------------------------------------------

def test_mcp_tool_registered_seventh():
    names = [s["name"] for s in mcp_server._TOOL_SPECS]
    assert "cora_delegated_jobs" in names
    assert len(names) == 7


def test_mcp_delegated_jobs_renders_ids_never_titles():
    _seed()
    out = mcp_server.delegated_jobs()
    blob = json.dumps(out)
    assert "dw-obs111111111" in blob
    assert SECRET not in blob
    assert "mtd_est_usd" in out
    assert "dw-obs111111111" in out["text"]
    assert SECRET not in out["text"]
    # Code #15 S3: the founder-local MCP view names WHO asked -- id + roster
    # name on the row and in the text, plus the all-jobs per-requester block.
    row = out["recent"][0]
    assert row["requester"] == "U_X" and row["requester_name"] == "X"
    assert out["counts_by_requester"] == {
        "U_X": {"name": "X", "total": 1, "by_state": {"QUEUED": 1}}}
    assert "X (U_X)" in out["text"]
    assert "by requester (ALL jobs" in out["text"]
    assert "titles/briefs never surface here" in out["text"]


def test_mcp_delegated_jobs_failure_enums_never_the_message():
    _seed()
    dw.append_runner_event({"event": "started", "ts": dw._now_iso(),
                            "job_id": "dw-obs111111111"})
    dw.append_runner_event({
        "event": "failed", "ts": dw._now_iso(), "job_id": "dw-obs111111111",
        "failure_class": "content_guard", "guard_class": "non_lex_phi",
        "message": f"artifact tripped the channel content guard: {SECRET}"})
    out = mcp_server.delegated_jobs()
    row = out["recent"][0]
    assert row["failure_class"] == "content_guard"
    assert row["guard_class"] == "non_lex_phi"
    assert "(failure: content_guard/non_lex_phi)" in out["text"]
    blob = json.dumps(out)
    assert SECRET not in blob and "artifact tripped" not in blob


def test_mcp_delegated_jobs_blank_name_renders_bare_id(monkeypatch):
    import cora.org_roles as org_roles

    dw.append_bot_event({
        "event": "requested", "ts": dw._now_iso(), "job_id": "dw-obs222222222",
        "archetype": "doc_draft", "title": SECRET, "brief": SECRET,
        "requester": "U_NONAME", "requester_name": "", "entity": "F3E",
        "channel_id": "C_PRIV", "channel_name": "priv", "thread_ts": "",
        "deliverable": "md", "fingerprint": "fp-obs2",
    })
    monkeypatch.setattr(org_roles, "get_role", lambda uid: None)
    out = mcp_server.delegated_jobs()
    assert "-- U_NONAME" in out["text"]
    assert "(U_NONAME)" not in out["text"]  # no empty "name ()" shape
    assert SECRET not in json.dumps(out)


def test_mcp_requester_display_is_one_line():
    assert mcp_server._requester_display("X", "U_X") == "X (U_X)"
    assert mcp_server._requester_display("", "U_X") == "U_X"
    assert mcp_server._requester_display(None, "") == "?"
    # A ledger value can never inject a line into the one-row-per-line render.
    assert mcp_server._requester_display("Ann\n- fake row", "U_A") == "Ann - fake row (U_A)"


def test_mcp_delegated_jobs_makes_no_slack_call(monkeypatch):
    """The read surface resolves names from the ledger / org_roles roster only:
    the MCP stdio child holds no Slack client and must not grow one."""
    import slack_sdk

    def _no_slack(*a, **k):
        raise AssertionError("the MCP delegated-jobs view must not construct a Slack client")

    monkeypatch.setattr(slack_sdk, "WebClient", _no_slack)
    monkeypatch.setattr(dw, "_default_client_factory", _no_slack)
    _seed()
    out = mcp_server.delegated_jobs()
    assert "error" not in out and out["recent"][0]["requester"] == "U_X"


def test_acceptance_script_expects_seven_tools():
    src = (_REPO_ROOT / "scripts" / "mcp_acceptance_check.py").read_text(encoding="utf-8")
    assert '"cora_delegated_jobs"' in src
    assert src.count('"cora_') >= 7  # the expected set names all seven


def test_plugin_docs_list_the_new_tool():
    readme = (_REPO_ROOT / "deployment" / "cora-tools-plugin" / "README.md").read_text(
        encoding="utf-8")
    assert "cora_delegated_jobs" in readme
    plugin = (_REPO_ROOT / "deployment" / "cora-tools-plugin" / ".claude-plugin"
              / "plugin.json").read_text(encoding="utf-8")
    assert "delegated-work" in plugin


# ---------------------------------------------------------------------------
# Session snapshot
# ---------------------------------------------------------------------------

def test_snapshot_spec_registered_cadence_300():
    spec = next((s for s in snaps._SPECS if s["name"] == "delegated-jobs.json"), None)
    assert spec is not None
    assert spec["cadence"] == 300


def test_snapshot_render_ids_never_titles():
    _seed()
    spec = next(s for s in snaps._SPECS if s["name"] == "delegated-jobs.json")
    payload = spec["render"]()
    blob = json.dumps(payload)
    assert "dw-obs111111111" in blob
    assert SECRET not in blob


def test_snapshot_render_carries_no_requester():
    """Code #15 S3: the requester view is the founder-local MCP tool ONLY. The
    snapshot is mirrored every 300s to G:\\...\\_brain\\_bus\\snapshots\\
    delegated-jobs.json, which is org-readable -- it must keep calling the
    requester-free default (a failed job's guard enum stays off it too)."""
    _seed()
    dw.append_runner_event({"event": "failed", "ts": dw._now_iso(),
                            "job_id": "dw-obs111111111",
                            "failure_class": "content_guard", "guard_class": "non_lex_phi",
                            "message": "artifact tripped the channel content guard: non_lex_phi"})
    spec = next(s for s in snaps._SPECS if s["name"] == "delegated-jobs.json")
    payload = spec["render"]()
    blob = json.dumps(payload)
    assert "dw-obs111111111" in blob
    for needle in ("U_X", '"X"', "requester", "requester_name", "counts_by_requester",
                   "failure_class", "guard_class", "non_lex_phi"):
        assert needle not in blob, needle
    assert list(payload["summary"]["recent"][0].keys()) == [
        "job_id", "archetype", "entity", "state", "requested_at", "est_usd"]


# ---------------------------------------------------------------------------
# S5: ingest tagging at the static-sync chokepoint
# ---------------------------------------------------------------------------

def _load_sync_module():
    spec = importlib.util.spec_from_file_location(
        "incremental_sync_static", _REPO_ROOT / "scripts" / "incremental_sync_static.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_ingest_tags_delegated_work_chunks_bot_authored(tmp_path, monkeypatch):
    sync = _load_sync_module()
    monkeypatch.setattr(sync, "FOUNDER_OS_ROOT", tmp_path)
    dw_dir = tmp_path / "02-F3-Energy" / "_delegated-work" / "2026-08"
    dw_dir.mkdir(parents=True)
    artifact = dw_dir / "2026-08-01_f3e_doc-draft-test-abc123.md"
    artifact.write_text("AI-generated draft content", encoding="utf-8")

    doc = sync.file_to_document(artifact)
    assert doc is not None
    assert doc.metadata.get("bot_authored") is True
    assert doc.entity == "F3E"

    # A sibling non-delegated file must NOT carry the tag.
    normal = tmp_path / "02-F3-Energy" / "notes.md"
    normal.write_text("human-authored note", encoding="utf-8")
    doc2 = sync.file_to_document(normal)
    assert doc2 is not None
    assert "bot_authored" not in doc2.metadata


def test_delegated_work_path_predicate():
    sync = _load_sync_module()
    assert sync.is_delegated_work_path(
        Path("G:/x/02-F3-Energy/_delegated-work/2026-08/a.md"))
    assert not sync.is_delegated_work_path(
        Path("G:/x/02-F3-Energy/delegated-work-notes/a.md"))


def test_delegated_artifacts_survive_the_walk_filters(tmp_path, monkeypatch):
    """The _delegated-work tree must actually INGEST (tagged), not be skipped by
    the swept/internal/archive filters -- the tag is useless if the file never
    lands."""
    sync = _load_sync_module()
    monkeypatch.setattr(sync, "FOUNDER_OS_ROOT", tmp_path)
    p = tmp_path / "09-One-Stop-Nutrition" / "_delegated-work" / "2026-08" / "x.md"
    p.parent.mkdir(parents=True)
    p.write_text("content", encoding="utf-8")
    assert not sync.is_phi_path(p)
    assert not sync.is_swept_path(p)
    assert not sync.is_cora_internal_path(p)
    assert not sync.is_copa_bhrf_path(str(p))
    assert "_archive" not in str(p).lower()
    doc = sync.file_to_document(p)
    assert doc is not None and doc.metadata.get("bot_authored") is True
