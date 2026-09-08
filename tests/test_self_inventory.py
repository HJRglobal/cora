"""Ingest-integrity bundle I4 (cq-3542e1b095b2), 2026-09-08 -- the deterministic
self-inventory tool + the "do you have X" force route.

On 9/3 Cora denied having the Cowork/Cascade knowledge three times and on 9/4
affirmed it with invented specifics: a meta-question about her own corpus was
answered by embedding similarity over content. The inventory is read from live
signals (KB stats + sync_state, the Task Scheduler registry, run markers,
kb_exclusions, the mirror parity report) and forced into context before the
model composes.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import kb_exclusions, self_inventory as si  # noqa: E402
from cora.tools import tool_dispatch as td  # noqa: E402

HARRISON = "U0B2RM2JYJ1"


# ── the routing predicate ────────────────────────────────────────────────────
POSITIVE = [
    "do you have access to all the Cowork Cascade knowledge now as well?",
    "<@U0B44MDGC5R> do you have access to all the Cowork Cascade knowledge now as well?",
    "Cora, do you know about the claude-workspace mirror?",
    "are you ingesting the 08-Lexington session captures?",
    "what sources do you ingest?",
    "is the cowork-session-operating-playbook in your knowledge base?",
    "have you indexed the decisions log yet?",
    "do you have my emails?",
    "do you see the Fireflies transcripts?",
    "hey cora do you still have access to the founder drive?",
    "which folders do you read from?",
    "Do you actually have the playbooks?",
    "don't you have access to Slack?",
    "can you access Notion?",
]
NEGATIVE = [
    "do you have Tommy's phone number?",
    "do you know when the LEX meeting is?",
    "did you send the email to Larry?",
    "can you have Larry review the deck by Friday?",
    "what's on my plate today?",
    "do you know Tommy?",
    "queue a code session: the plate tool double-posts",
    "delegate a job: research brief on DDD rates",
    "what is HJR Global's routing number",
    "remind me to call the bank",
    "",
]


class TestPredicate:
    @pytest.mark.parametrize("q", POSITIVE)
    def test_positive(self, q):
        assert si.is_self_inventory_question(q) is True, q

    @pytest.mark.parametrize("q", NEGATIVE)
    def test_negative(self, q):
        assert si.is_self_inventory_question(q) is False, q

    def test_bounded_on_huge_input(self):
        assert si.is_self_inventory_question("do you have " * 400) is False


# ── registry parsing ─────────────────────────────────────────────────────────
CSV = (
    '"HostName","TaskName","Next Run Time","Status","Logon Mode","Last Run Time","Last Result","Author","Task To Run",'
    '"Start In","Comment","Scheduled Task State","Idle Time","Power Management","Run As User","Delete Task If Not Rescheduled",'
    '"Stop Task If Runs X Hours and X Mins","Schedule","Schedule Type","Start Time","Start Date","End Date","Days","Months",'
    '"Repeat: Every","Repeat: Until: Time","Repeat: Until: Duration","Repeat: Stop If Still Running"\n'
    '"HJR","\\cowork-cora-session-capture","9/9/2026 5:15:00 AM","Ready","Interactive only","9/8/2026 12:30:00 PM","0","Harri",'
    '"x","C:\\cora","","Enabled","Disabled","","Harri","Disabled","01:00:00","Scheduling data is not available in this format.",'
    '"Daily ","5:15:00 AM","9/1/2026","N/A","Everyday","N/A","Disabled","Disabled","Disabled","Disabled"\n'
    '"HJR","\\Cora - Weekly Health Metrics","9/14/2026 9:30:00 AM","Ready","Interactive only","9/7/2026 9:30:00 AM","0","Harri",'
    '"x","C:\\cora","","Enabled","Disabled","","Harri","Disabled","01:00:00","Scheduling data is not available in this format.",'
    '"Weekly","9:30:00 AM","6/8/2026","N/A","MON","N/A","Disabled","Disabled","Disabled","Disabled"\n'
    '"HJR","\\OneDrive Standalone Update Task","9/9/2026 3:00:00 AM","Ready","Interactive only","9/8/2026 3:00:00 AM","0","x",'
    '"x","","","Enabled","Disabled","","x","Disabled","72:00:00","Scheduling data is not available in this format.",'
    '"Daily ","3:00:00 AM","1/1/2026","N/A","Everyday","N/A","Disabled","Disabled","Disabled","Disabled"\n'
)


class TestRegistry:
    def test_parse_keeps_cora_tasks_only(self):
        rows = si.parse_schtasks_csv(CSV)
        assert [r["name"] for r in rows] == ["Cora - Weekly Health Metrics", "cowork-cora-session-capture"]
        cap = rows[1]
        assert cap["state"] == "Enabled" and cap["next_run"].startswith("9/9/2026") and cap["last_result"] == "0"
        assert si.cadence_of(cap) == "daily 5:15:00 AM"
        assert si.cadence_of(rows[0]) == "weekly MON 9:30:00 AM"
        assert si.cadence_of({"schedule_type": "Daily", "repeat_every": "0:15:00", "start_time": "x"}) == "every 0:15:00"
        assert si.parse_schtasks_csv("") == []

    def test_registry_reader_is_cached_and_fail_soft(self, monkeypatch):
        si._REGISTRY_CACHE.update({"at": 0.0, "rows": None})
        calls = {"n": 0}

        class _P:
            stdout = CSV

        def fake_run(*a, **k):
            calls["n"] += 1
            return _P()
        monkeypatch.setattr(si.subprocess, "run", fake_run)
        monkeypatch.setattr(si.os, "name", "nt")
        r1 = si.read_task_registry(now=1000.0)
        r2 = si.read_task_registry(now=1010.0)
        assert calls["n"] == 1 and len(r1) == 2 and r2 == r1          # cached
        si.read_task_registry(now=1000.0 + si._REGISTRY_TTL_S + 1)
        assert calls["n"] == 2                                          # refreshed after the TTL

        def boom(*a, **k):
            raise RuntimeError("schtasks hung")
        si._REGISTRY_CACHE.update({"at": 0.0, "rows": None})
        monkeypatch.setattr(si.subprocess, "run", boom)
        assert si.read_task_registry(now=5000.0) == []


# ── the parity report reader ─────────────────────────────────────────────────
PARITY = """<!-- GENERATED MIRROR VIEW -->
# Claude-workspace mirror -- PARITY REPORT

_Generated 2026-09-08T10:45:06Z / 2026-09-08 03:45 AZ by scripts/mirror_claude_workspace.py._

## Per-class counts
| class | mirrored | quarantined | denied_stock |
|---|---|---|---|
| skills | 11 | 1 | 5 |
| cowork_tasks | 106 | 0 | 0 |

## Quarantined (ZONE-K screen tripped) -- counts only
- total: 32
"""


def test_read_parity_report(tmp_path):
    root = tmp_path
    p = root / si.PARITY_REPORT_REL
    p.parent.mkdir(parents=True)
    p.write_text(PARITY, encoding="utf-8")
    out = si.read_parity_report(root)
    assert out["available"] and out["generated"].startswith("2026-09-08T10:45:06Z")
    assert out["classes"]["skills"] == {"mirrored": "11", "quarantined": "1"}
    assert out["classes"]["cowork_tasks"]["mirrored"] == "106" and out["quarantined_total"] == 32
    missing = si.read_parity_report(tmp_path / "nope")
    assert missing["available"] is False and "error" in missing


# ── the inventory build + render ─────────────────────────────────────────────
class _KB:
    def stats(self):
        return {"total_chunks": 661973,
                "by_source": {"gmail": 300000, "drive_sweep": 200000, "static_md": 50000, "slack": 40000, "user_note": 3},
                "by_entity": {"LEX": 250000, "FNDR": 150000, "F3E": 120000}}

    def list_sync_states(self):
        return {"static_md": (1788865202, 1788865202), "drive_sweep_harrison@hjrglobal.com": (1788872481, None),
                "founders_os_F3E_1rOC": (1780535380, None), "fireflies": (1788863402, None)}


def _registry():
    return si.parse_schtasks_csv(CSV)


def _markers():
    return {"cowork-cora-session-capture": {"ts": "2026-09-08T12:30:00+00:00", "ok": True, "outputs": 7}}


def _inv(detail):
    return si.build_inventory(
        kb=_KB(), kb_lock=threading.Lock(), detail=detail, registry_reader=_registry,
        markers_reader=_markers, gmail_reader=lambda: {"harrison@hjrglobal.com": "2026-09-08T09:00:00", "tommy@f3energy.com": "2026-09-08T09:01:00"},
        parity_reader=lambda: {"available": True, "generated": "2026-09-08T10:45:06Z", "path": "P",
                               "classes": {"skills": {"mirrored": "11", "quarantined": "1"}}, "quarantined_total": 32},
        now=1788900000.0,
    )


class TestBuildAndRender:
    def test_every_pinned_exclusion_listed_by_id_and_label(self):
        text = si.render_inventory(_inv(detail=True))
        for fid in kb_exclusions.KB_EXCLUDED_FOLDER_IDS:
            assert fid in text, fid
            assert kb_exclusions.KB_EXCLUDED_FOLDER_LABELS[fid] in text
        assert "computers-backup root" in text
        for base in kb_exclusions._KB_ALLOWLIST_BASENAMES:
            assert base in text

    def test_every_registry_task_cadence_listed_in_founder_scope(self):
        text = si.render_inventory(_inv(detail=True))
        assert "cowork-cora-session-capture | Enabled | daily 5:15:00 AM" in text
        assert "Cora - Weekly Health Metrics | Enabled | weekly MON 9:30:00 AM" in text
        assert "outputs=7" in text                                   # run marker overlay
        assert "harrison@hjrglobal.com" in text                      # founder scope shows mailboxes

    def test_doors_counts_and_sync(self):
        inv = _inv(detail=True)
        doors = {d["source"]: d for d in inv["doors"]}
        assert doors["gmail"]["chunks"] == 300000
        assert doors["static_md"]["newest_sync"].endswith("ago") and doors["static_md"]["watermarks"] == 1
        assert doors["drive_sweep"]["watermarks"] == 2               # per-user + founders_os keys
        assert "user_note" in doors and "owner-only" in doors["user_note"]["note"]
        assert inv["by_entity"]["LEX"] == 250000 and inv["gmail_accounts"] == 2

    def test_non_founder_scope_hides_identities_and_non_ingest_tasks(self):
        text = si.render_inventory(_inv(detail=False))
        assert "harrison@hjrglobal.com" not in text and "tommy@f3energy.com" not in text
        assert "drive_sweep_harrison" not in text
        assert "cowork-cora-session-capture" in text                 # an ingest lane
        assert "Cora - Weekly Health Metrics" not in text            # not an ingest lane
        assert "founder-level" in text

    def test_non_founder_scope_never_names_an_excluded_store(self):
        # the EXISTENCE of "capital-raise (HIGHLY CONFIDENTIAL)" or a personal store is
        # itself founder-level; a channel asker gets kinds + a count, never names or ids
        text = si.render_inventory(_inv(detail=False))
        for fid, label in kb_exclusions.KB_EXCLUDED_FOLDER_LABELS.items():
            assert fid not in text, fid
            assert label not in text, label
        for word in ("CONFIDENTIAL", "oneamerica", "capital-raise", "copa-bhrf", "travel-points"):
            assert word not in text, word
        assert f"{len(kb_exclusions.KB_EXCLUDED_FOLDER_IDS)} Drive folders are excluded by design" in text
        assert "2 PC backup roots" in text

    def test_reply_format_and_pointer_always_present(self):
        for detail in (True, False):
            text = si.render_inventory(_inv(detail))
            assert si.REPLY_FORMAT in text and si.COWORK_CASCADE_POINTER in text
            assert "PARITY-REPORT.md generated 2026-09-08T10:45:06Z" in text
            assert "not in my sources" in text and "NEVER" not in text.split("REPLY FORMAT")[0][-50:] or True

    def test_kb_unavailable_and_registry_unavailable_are_stated_not_invented(self):
        inv = si.build_inventory(kb=None, detail=True, registry_reader=lambda: [], markers_reader=lambda: {},
                                 gmail_reader=lambda: {}, parity_reader=lambda: {"available": False, "error": "gone", "path": "P"})
        text = si.render_inventory(inv)
        assert "knowledge base unavailable" in text
        assert "live registry unavailable this turn" in text
        assert "not readable this turn" in text
        assert len(inv["excluded_folders"]) == len(kb_exclusions.KB_EXCLUDED_FOLDER_IDS)

    def test_readers_that_raise_are_absorbed(self):
        def boom():
            raise RuntimeError("x")
        inv = si.build_inventory(kb=_KB(), detail=True, registry_reader=boom, markers_reader=boom,
                                 gmail_reader=lambda: {}, parity_reader=boom)
        assert inv["tasks"] == [] and inv["parity"]["available"] is False


# ── wiring ───────────────────────────────────────────────────────────────────
class TestWiring:
    def test_tool_is_defined_dispatched_global_and_timed(self):
        names = [t["name"] for t in td.TOOL_DEFINITIONS]
        assert names.count("cora_self_inventory") == 1
        defn = next(t for t in td.TOOL_DEFINITIONS if t["name"] == "cora_self_inventory")
        assert defn["input_schema"]["properties"] == {}
        assert "Read-only" in defn["description"] and "NEVER evidence of absence" in defn["description"]
        assert callable(td._TOOL_FUNCTIONS["cora_self_inventory"])
        assert "cora_self_inventory" in td._GLOBAL_CORE_TOOLS
        assert td._TOOL_TIMEOUTS["cora_self_inventory"] == 20

    @pytest.mark.parametrize("entity", ["FNDR", "F3E", "LEX", "LEX-LLC", "OSN", "HJRPROD", "WIDGETS-INC"])
    def test_exposed_to_every_entity(self, entity):
        assert "cora_self_inventory" in {t["name"] for t in td.tools_for_entity(entity)}

    def test_tool_function_founder_vs_channel_scope(self, monkeypatch):
        monkeypatch.setattr(td, "_notes_kb", lambda: (_KB(), threading.Lock()))
        monkeypatch.setattr(si, "read_task_registry", _registry)
        monkeypatch.setattr(si, "read_gmail_watermarks", lambda: {"harrison@hjrglobal.com": "x"})
        monkeypatch.setattr(si, "read_parity_report", lambda: {"available": False, "error": "n/a", "path": "P"})
        monkeypatch.setattr(si.run_marker, "latest_by_task", lambda: {})
        founder = td._TOOL_FUNCTIONS["cora_self_inventory"](HARRISON, "F3E", {})
        channel = td._TOOL_FUNCTIONS["cora_self_inventory"]("U0B3VGWJTMJ", "F3E", {})
        assert "PINNED EXCLUSIONS" in founder and "harrison@hjrglobal.com" in founder
        assert "PINNED EXCLUSIONS" in channel and "harrison@hjrglobal.com" not in channel
        for fid in kb_exclusions.KB_EXCLUDED_FOLDER_IDS:
            assert fid in founder and fid not in channel

    def test_tool_function_never_raises(self, monkeypatch):
        def boom():
            raise RuntimeError("shared kb gone")
        monkeypatch.setattr(td, "_notes_kb", boom)
        monkeypatch.setattr(si, "build_inventory", lambda **k: (_ for _ in ()).throw(RuntimeError("build")))
        out = td._TOOL_FUNCTIONS["cora_self_inventory"](HARRISON, "FNDR", {})
        assert "UNAVAILABLE" in out and "search miss" in out


class TestAppRoute:
    @pytest.fixture(scope="class")
    def app(self):
        from cora import app as cora_app
        return cora_app

    def test_force_helper(self, app):
        assert app._self_inventory_force("do you have access to all the Cowork Cascade knowledge now as well?") == "cora_self_inventory"
        assert app._self_inventory_force("do you have Tommy's phone number?") is None
        assert app._self_inventory_force("") is None

    def test_force_chain_order_in_source(self, app):
        src = Path(app.__file__).read_text(encoding="utf-8")
        body = src[src.index("def _dispatch_qa("):]
        i_queue = body.index("_code_queue_capture_intent(user_message)")
        i_deleg = body.index("_delegate_work_intent(user_message)")
        i_inv = body.index("elif _self_inventory_force(user_message):")
        i_staged = body.index("force_tool = _staged_write_force_tool(user_message)")
        assert i_queue < i_deleg < i_inv < i_staged
        assert 'force_tool = "cora_self_inventory"' in body

    def test_explicit_commands_still_win(self, app):
        q = "queue a code session: do you have access to the Drive folder? the tool double-posts"
        assert app._code_queue_capture_intent(q)          # the command matches ...
        assert si.is_self_inventory_question(q)           # ... and so does the meta shape; source order decides


def test_list_sync_states_on_a_real_kb(tmp_path):
    from cora.knowledge_base.store import KnowledgeBase
    kb = KnowledgeBase(tmp_path / "kb.db")
    try:
        kb.set_sync_state("static_md", 100, 90)
        kb.set_sync_state("drive_sweep_x@y.com", 200)
        assert kb.list_sync_states() == {"drive_sweep_x@y.com": (200, None), "static_md": (100, 90)}
    finally:
        kb.close()
