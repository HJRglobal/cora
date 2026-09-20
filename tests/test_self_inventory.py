"""Ingest-integrity bundle I4 (cq-3542e1b095b2), 2026-09-08 -- the deterministic
self-inventory tool + the "do you have X" force route, as remediated by the D-051
lens-E / lens-F review the same day.

On 9/3 Cora denied having the Cowork/Cascade knowledge three times and on 9/4
affirmed it with invented specifics: a meta-question about her own corpus was
answered by embedding similarity over content. The inventory is read from live
signals (KB stats + sync_state, the Task Scheduler registry, run markers,
kb_exclusions, the mirror parity report, the channel's tool list) and forced into
context before the model composes.

The routing predicate is a grammar for SOURCE questions: the measured lens-E
corpus below is the contract -- every acceptance phrasing forces, every content /
live-system / write turn does not.
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
    # the original acceptance set
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
    "can you access Notion?",                            # a KB door (source=notion), no live tool
    # the lens-E misses (17 of 28 acceptance phrasings failed the first cut)
    "Cora do u have the cowork stuff",
    "does your knowledge include the mirror?",
    "have you got the playbooks?",
    "you don't have the LEX captures do you",
    "you don't have the LEX captures, do you?",
    "what are your sources?",
    "what's in your knowledge base?",
    "where does your knowledge come from?",
    "is the cowork cascade material available to you?",
    "have you been given the Cowork Cascade knowledge?",
    "Which of the Cowork Cascade docs can you see?",
    "Cora \u2014 do you have the cowork cascade knowledge?",
    "Cora -- do you have the cowork cascade knowledge?",
    "Cora... do you have the cowork cascade knowledge?",
    "Cora! do you have the cowork cascade knowledge?",
    "Cora, quick question: do you have the cowork cascade knowledge?",
    "hey cora, quick q: do you have the cowork cascade knowledge?",
    "is the Q3 P&L in your knowledge base?",           # FINANCIAL-classified meta-question still routes
    # Code #13 slice 1 routing half (cq-2e02f1fd0f65; ruled 2026-09-10): live
    # connectors / tools / files JOIN the force -- REVERSES the lens-E #2 choice
    # (the 9/10 transcript shows the model denying tools it had). The inventory
    # renders them as live connectors, NOT sources, and names the door.
    "do you have access to HubSpot?",
    "can you access HubSpot?",
    "do you have access to Asana?",
    "do you have access to QBO for OSN?",
    "do you have access to the web?",
    "can you see my calendar?",
    "are you able to reach Shopify?",
    "can you read the files in that folder?",               # via the folder SOURCE noun (_P_HAVE_KNOW)
    "Cora, can you connect to Deposco?",
    # AD-2 clause-end shapes that MUST still route: bounded adverbial, entity
    # qualifier, two connectors, no punctuation at all, a following line.
    "do you have access to HubSpot at all?",
    "can you access HubSpot or Asana?",
    "can you access hubspot",
    "can you access hubspot\nthanks",
]
NEGATIVE = [
    # ordinary asks the first cut hijacked (27 of 50 realistic role-based asks)
    "are you tracking the Gotham deal?",
    "are you monitoring #f3e-sales?",
    "are you reading this?",
    "are you connected right now?",
    "have you seen the Sprouts contract?",
    "have you read the Q3 board deck?",
    "what data do you have on the Kroger account?",
    "what knowledge do you have of the Gotham deal?",
    "what channels do you post in?",
    "do you have the notes from Tuesday's sales meeting?",
    "did you see the emails about the Whole Foods reset?",
    "do you have the EVV docs for live-in caregivers?",          # the D-046 LEX use case: retrieval
    "do you have the fireflies transcript from the Gotham call?",
    "do you have the slack thread where Matt approved the PO?",
    # CONTENT questions about a live system stay with retrieval + the tool list
    # (the Code #13 routing half admits "can you access <connector>", not this)
    "do you have Tommy's calendar for Friday?",
    "do you know the Shopify inventory for the 12-pack?",
    "what's in the Asana project for Pure Launch?",
    "can you pull my HubSpot deals?",                    # an ASK for the tool, not a meta-question
    # D-051 Code #13 review AD-2: the connector noun must END the clause -- a
    # connector followed by an object clause is a TOOL-USE ask (calendar / QBO /
    # Asana / HubSpot / Shopify tool, or a web search), never the inventory dump.
    # The first cut FORCED all eleven of these to cora_self_inventory, and since a
    # forced tool sets web_gate_skip, the web was withheld on the explicit web asks.
    "can you use the web to find F3's competitor pricing?",
    "will you use the web search to find the venue address?",
    "Can you use web search to check F3's Amazon rank?",
    "can you see my calendar for Friday?",
    "can you query QBO for OSN's Q2 revenue?",
    "can you open the Asana task for the Sprouts appeal?",
    "could you read the files in the Founder-OS folder and summarize them?",
    "can you hit HubSpot and tell me which deals are stalled?",
    "can you pull from Shopify the 12-pack inventory?",
    "can you see the spreadsheet Justin sent?",
    "can you get into HubSpot and update the deal stage?",  # a write intent the imperative strip cannot see
    # imperative writes are never meta-questions (lens E #1)
    "Complete the 'send Larry the deck' task -- the deck is in your knowledge base right",
    "Mark the Sprouts task done since the appeal letter is in your memory now",
    "Delete the task 'confirm Cora has Drive access'. It was created because we weren't sure whether the folders were in your index",
    "create a task for Tommy to check whether the Kroger files are in your sources",
    "DM Tommy that the Q3 numbers are in your knowledge base",
    "remember that the Sprouts folder is in your sources now",
    "draft an email to Larry saying the deck is in your knowledge base",
    # the original negatives
    "do you have Tommy's phone number?",
    "do you know when the LEX meeting is?",
    "did you send the email to Larry?",
    "can you have Larry review the deck by Friday?",
    "what's on my plate today?",
    "do you know Tommy?",
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

    def test_no_redos(self):
        import time
        for adversarial in ("is " * 666, "do you have " * 170, "cora" + " " * 1990, "which of the " * 150,
                            "you don't have " * 130, "in your " * 240):
            t0 = time.perf_counter()
            si.is_self_inventory_question(adversarial[:2000])
            assert time.perf_counter() - t0 < 0.05, adversarial[:20]

    def test_connector_clause_end_growth_shape_is_linear(self):
        """AD-2 growth-shape pin for the new clause-END tail (every new regex gets one):
        a connector followed by whitespace / an unterminated qualifier / repeated
        'or' / repeated 'and the web' must fail fast, and the forced route must
        still fire on the bare meta-question the tail exists to admit."""
        import time
        for adversarial in ("can you access hubspot" + " " * 1990,
                            "can you access hubspot for " + "x" * 1900,
                            "can you " + "still " * 300 + "access hubspot?",
                            "are you able to reach shopify" + " or " * 400,
                            "can you access the web" + " and the web" * 150):
            t0 = time.perf_counter()
            si.is_self_inventory_question(adversarial[:2000])
            assert time.perf_counter() - t0 < 0.05, adversarial[:30]
        assert si.is_self_inventory_question("can you access hubspot" + " " * 1900) is True   # whitespace then EOL
        assert si.is_self_inventory_question("can you access hubspot" + " " * 1900 + "x") is False


# ── registry parsing ─────────────────────────────────────────────────────────
_HEADER = (
    '"HostName","TaskName","Next Run Time","Status","Logon Mode","Last Run Time","Last Result","Author","Task To Run",'
    '"Start In","Comment","Scheduled Task State","Idle Time","Power Management","Run As User","Delete Task If Not Rescheduled",'
    '"Stop Task If Runs X Hours and X Mins","Schedule","Schedule Type","Start Time","Start Date","End Date","Days","Months",'
    '"Repeat: Every","Repeat: Until: Time","Repeat: Until: Duration","Repeat: Stop If Still Running"\n'
)


def _row(name, next_run, last_run, sched_type, start, days):
    return (f'"HJR","\\{name}","{next_run}","Ready","Interactive only","{last_run}","0","Harri",'
            f'"x","C:\\cora","","Enabled","Disabled","","Harri","Disabled","01:00:00","Scheduling data is not available in this format.",'
            f'"{sched_type}","{start}","9/1/2026","N/A","{days}","N/A","Disabled","Disabled","Disabled","Disabled"\n')


CSV = (
    _HEADER
    + _row("cowork-cora-session-capture", "9/9/2026 5:15:00 AM", "9/8/2026 12:30:00 PM", "Daily ", "5:15:00 AM", "Everyday")
    + _HEADER   # the verbose listing repeats the header per task
    + _row("cowork-cora-session-capture", "9/9/2026 12:30:00 PM", "9/8/2026 12:30:00 PM", "Daily ", "12:30:00 PM", "Everyday")
    + _HEADER
    + _row("Cora - Weekly Health Metrics", "9/14/2026 9:30:00 AM", "9/7/2026 9:30:00 AM", "Weekly", "9:30:00 AM", "MON")
    + _HEADER
    + _row("Cora - Asana Hygiene Nudges", "9/9/2026 6:30:00 AM", "9/8/2026 6:30:00 AM", "Daily ", "6:30:00 AM", "Everyday")
    + _HEADER
    + _row("Cora - Email Attachment Filer", "9/8/2026 4:00:00 PM", "9/8/2026 12:00:00 PM", "Daily ", "12:00:00 AM", "Everyday")
    + _HEADER
    + _row("cowork-cora-claude-mirror", "9/9/2026 3:45:00 AM", "9/8/2026 3:45:00 AM", "Daily ", "3:45:00 AM", "Everyday")
    + _HEADER
    + _row("OneDrive Standalone Update Task", "9/9/2026 3:00:00 AM", "9/8/2026 3:00:00 AM", "Daily ", "3:00:00 AM", "Everyday")
    + _HEADER
    + _row("Decorator Update Task", "9/9/2026 3:00:00 AM", "9/8/2026 3:00:00 AM", "Daily ", "3:00:00 AM", "Everyday")
)


class TestRegistry:
    def test_parse_keeps_cora_tasks_only_and_folds_triggers(self):
        rows = si.parse_schtasks_csv(CSV)
        assert [r["name"] for r in rows] == [
            "Cora - Asana Hygiene Nudges", "Cora - Email Attachment Filer", "Cora - Weekly Health Metrics",
            "cowork-cora-claude-mirror", "cowork-cora-session-capture",
        ]                                                              # no "Decorator", no OneDrive
        cap = rows[-1]
        assert cap["triggers"] == "2" and cap["next_run"].startswith("9/9/2026 5:15")
        assert si.cadence_of(cap) == "daily 5:15:00 AM + 12:30:00 PM"    # both triggers, one row
        assert si.cadence_of(rows[2]) == "weekly MON 9:30:00 AM"
        assert si.cadence_of({"schedule_type": "Daily", "repeat_every": "0:15:00", "start_time": "x"}) == "every 0:15:00"
        assert si.parse_schtasks_csv("") == []

    def test_ingest_lane_classification(self):
        for name in ("cowork-cora-kb-sync-gmail", "Cora - Drive Sweep", "cowork-cora-founders-os-sweep",
                     "cowork-cora-session-capture", "cowork-cora-claude-mirror", "Cora - Email Attachment Filer",
                     "cowork-cora-inventory-state-sync", "Cora - LEX Dump Folder Sync", "cowork-cora-channel-sweep"):
            assert si.is_ingest_task(name), name
        for name in ("Cora - Asana Hygiene Nudges", "Cora - Revops Sweep", "cowork-cora-decision-capture",
                     "cowork-cora-fireflies-coverage", "cowork-cora-completion-sweep", "Cora - Weekly Health Metrics",
                     "cowork-cora-backup", "Cora - Meeting Action Capture", "cowork-cora-daily-briefing"):
            assert not si.is_ingest_task(name), name

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
        assert calls["n"] == 1 and len(r1) == 5 and r2 == r1          # cached
        si.read_task_registry(now=1000.0 + si._REGISTRY_TTL_S + 1)
        assert calls["n"] == 2                                          # refreshed after the TTL

        def boom(*a, **k):
            raise RuntimeError("schtasks hung")
        si._REGISTRY_CACHE.update({"at": 0.0, "rows": None})
        monkeypatch.setattr(si.subprocess, "run", boom)
        assert si.read_task_registry(now=5000.0) == []
        si._REGISTRY_CACHE.update({"at": 0.0, "rows": None})


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
NOW = 1788900000.0
H = 3600.0


class _KB:
    def stats(self):
        return {"total_chunks": 661973,
                "by_source": {"gmail": 300000, "drive_sweep": 200000, "static_md": 50000, "slack": 40000, "user_note": 3},
                "by_entity": {"LEX": 250000, "FNDR": 150000, "F3E": 120000}}

    def list_sync_states(self):
        return {"static_md": (int(NOW - 9 * H), int(NOW - 9 * H)),
                "drive_sweep_harrison@hjrglobal.com": (int(NOW - 3 * H), None),
                "founders_os_F3E_1rOC": (int(NOW - 2320 * H), None),        # 97 days: STALE, not "fresh"
                "fireflies": (int(NOW - 12 * H), None)}


def _registry():
    return si.parse_schtasks_csv(CSV)


def _markers():
    return {"cowork-cora-session-capture": {"ts": "2026-09-08T12:30:00+00:00", "ok": True, "outputs": 7}}


def _gmail():
    # the live file holds epoch INTS per mailbox (lens F #14 -- the first fixture used strings)
    return {"harrison@hjrglobal.com": int(NOW - 5 * H), "tommy@f3energy.com": int(NOW - 6 * H),
            "stale@lexingtonservices.com": int(NOW - 200 * H)}


def _parity():
    return {"available": True, "generated": "2026-09-08T10:45:06Z", "path": r"G:\My Drive\HJR-Founder-OS\_shared\claude-workspace-mirror\PARITY-REPORT.md",
            "classes": {"skills": {"mirrored": "11", "quarantined": "1"}}, "quarantined_total": 32}


def _connectors(entity):
    return ["HubSpot CRM (live deals / contacts)", "Asana (live tasks)", "Web search / fetch (gated per turn)"]


def _inv(detail, entity="F3E", **over):
    kw = dict(kb=_KB(), kb_lock=threading.Lock(), detail=detail, entity=entity, registry_reader=_registry,
              markers_reader=_markers, gmail_reader=_gmail, parity_reader=_parity, connectors_reader=_connectors, now=NOW)
    kw.update(over)
    return si.build_inventory(**kw)


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
        assert "cowork-cora-session-capture | Enabled | daily 5:15:00 AM + 12:30:00 PM" in text
        assert "Cora - Weekly Health Metrics | Enabled | weekly MON 9:30:00 AM" in text
        assert "outputs=7" in text                                   # run marker overlay
        assert "harrison@hjrglobal.com" in text                      # founder scope shows mailboxes

    def test_doors_counts_sync_and_watermark_freshness(self):
        inv = _inv(detail=True)
        doors = {d["source"]: d for d in inv["doors"]}
        assert doors["gmail"]["chunks"] == 300000
        assert doors["static_md"]["newest_sync"] == "9h ago" and doors["static_md"]["watermarks"] == 1
        assert doors["drive_sweep"]["watermarks"] == 2               # per-user + founders_os keys
        assert doors["drive_sweep"]["watermarks_fresh"] == 1         # the 97-day founders_os key is NOT fresh
        assert doors["drive_sweep"]["watermarks_stale_keys"] == ["founders_os_F3E_1rOC"]
        assert doors["drive_sweep"]["newest_sync"] == "3h ago"
        assert "user_note" in doors and "owner-only" in doors["user_note"]["note"]
        assert inv["by_entity"]["LEX"] == 250000 and inv["gmail_accounts"] == 3
        assert inv["gmail_newest_sync"] == "5h ago" and inv["gmail_stale_accounts"] == 1
        text = si.render_inventory(inv)
        assert "[1/2 fresh; 1 STALE]" in text and "STALE (older than 48h): founders_os_F3E_1rOC" in text
        assert "3 mailboxes tracked, newest sync 5h ago, 1 STALE" in text

    def test_live_connectors_rendered_as_not_sources(self):
        text = si.render_inventory(_inv(detail=False))
        assert "LIVE TOOL CONNECTORS in this channel (NOT knowledge-base sources" in text
        assert "HubSpot CRM (live deals / contacts)" in text and "Asana (live tasks)" in text
        assert "answered YES from this list, never 'not in my sources'" in text
        assert "never 'not in my sources'" in si.REPLY_FORMAT
        inv = _inv(detail=False, connectors_reader=lambda e: [])
        assert "tool list unavailable this turn" in si.render_inventory(inv)

    def test_live_tool_families_from_the_real_tool_map(self):
        f3e = si.live_tool_families("F3E")
        assert any("HubSpot" in f for f in f3e) and any("Shopify" in f for f in f3e) and any("Asana" in f for f in f3e)
        lex = si.live_tool_families("LEX-LLC")
        assert not any("HubSpot" in f for f in lex)                 # LEX never gets HubSpot (Tier-1 doctrine)
        assert any("Asana" in f for f in lex) and any("Web search" in f for f in lex)
        assert any("QuickBooks" in f for f in si.live_tool_families("FNDR"))

    def test_non_founder_scope_hides_identities_paths_and_non_ingest_tasks(self):
        text = si.render_inventory(_inv(detail=False, entity="F3E"))
        assert "harrison@hjrglobal.com" not in text and "tommy@f3energy.com" not in text
        assert "drive_sweep_harrison" not in text and "founders_os_F3E_1rOC" not in text
        assert "cowork-cora-session-capture" in text                 # an ingest lane
        assert "Cora - Email Attachment Filer" in text               # an ingest lane the first cut missed
        assert "Cora - Weekly Health Metrics" not in text            # not an ingest lane
        assert "Cora - Asana Hygiene Nudges" not in text             # a WRITER, not an ingest lane
        assert "founder-level" in text
        # no repo / Drive file paths, and only the asker's partition size
        assert "gmail-thread-watermarks.json" not in text and "monitored-email-accounts.yaml" not in text
        assert r"G:\My Drive" not in text
        assert "this channel's: F3E 120,000" in text and "LEX 250,000" not in text and "FNDR 150,000" not in text
        sub = si.render_inventory(_inv(detail=False, entity="LEX-LLC"))
        assert "LEX 250,000" in sub and "F3E 120,000" not in sub      # a sub-entity sees its parent partition

    def test_non_founder_scope_never_names_an_excluded_store_or_the_nda_project(self):
        # the EXISTENCE of "capital-raise (HIGHLY CONFIDENTIAL)" or a personal store is
        # itself founder-level; a channel asker gets kinds + a count, never names or ids --
        # and never the NDA'd project's token (lens E #7: "COPA" reached every channel twice)
        text = si.render_inventory(_inv(detail=False))
        for fid, label in kb_exclusions.KB_EXCLUDED_FOLDER_LABELS.items():
            assert fid not in text, fid
            assert label not in text, label
        for word in ("CONFIDENTIAL", "oneamerica", "capital-raise", "copa", "COPA", "travel-points"):
            assert word not in text, word
        assert f"{len(kb_exclusions.KB_EXCLUDED_FOLDER_IDS)} Drive folders are excluded by design" in text
        assert "2 PC backup roots" in text
        founder = si.render_inventory(_inv(detail=True))
        assert "(token: `copa`)" in founder                           # the founder scope names it

    def test_reply_format_and_pointer_live(self):
        for detail in (True, False):
            text = si.render_inventory(_inv(detail))
            assert si.REPLY_FORMAT in text and si.COWORK_CASCADE_POINTER in text
            assert "PARITY-REPORT.md generated 2026-09-08T10:45:06Z" in text
            assert "not in my sources" in text
        assert r"G:\My Drive" in si.render_inventory(_inv(True))       # founder scope: the path
        assert "path founder-level" in si.render_inventory(_inv(False))

    def test_pointer_goes_dark_when_the_doors_show_no_live_signal(self):
        # lens E #10: the first cut rendered "YES by two doors" as a constant
        inv = si.build_inventory(kb=None, detail=True, registry_reader=lambda: [], markers_reader=lambda: {},
                                 gmail_reader=lambda: {}, parity_reader=lambda: {"available": False, "error": "gone", "path": "P"},
                                 connectors_reader=lambda e: [], now=NOW)
        assert inv["cowork_cascade_live"] is False
        text = si.render_inventory(inv)
        assert "YES by two doors" not in text
        assert "NO live signal for the static_md door" in text and "claude-workspace mirror (parity report unreadable" in text
        assert "do NOT affirm with specifics, do NOT deny" in text
        # static door present + mirror TASK registered (parity unreadable) -> still live
        inv2 = _inv(detail=True, parity_reader=lambda: {"available": False, "error": "x", "path": "P"})
        assert inv2["cowork_cascade_live"] is True and si.COWORK_CASCADE_POINTER in si.render_inventory(inv2)
        # static door present, no mirror task, parity unreadable -> dark on the mirror only
        inv3 = _inv(detail=True, registry_reader=lambda: [], parity_reader=lambda: {"available": False, "error": "x", "path": "P"})
        assert inv3["cowork_cascade_live"] is False and "NO live signal for the claude-workspace mirror" in si.render_inventory(inv3)

    def test_kb_unavailable_and_registry_unavailable_are_stated_not_invented(self):
        inv = si.build_inventory(kb=None, detail=True, registry_reader=lambda: [], markers_reader=lambda: {},
                                 gmail_reader=lambda: {}, parity_reader=lambda: {"available": False, "error": "gone", "path": "P"},
                                 connectors_reader=lambda e: [])
        text = si.render_inventory(inv)
        assert "knowledge base unavailable" in text
        assert "live registry unavailable this turn" in text
        assert "not readable this turn" in text
        assert len(inv["excluded_folders"]) == len(kb_exclusions.KB_EXCLUDED_FOLDER_IDS)

    def test_readers_that_raise_are_absorbed(self):
        def boom(*a, **k):
            raise RuntimeError("x")
        inv = si.build_inventory(kb=_KB(), detail=True, registry_reader=boom, markers_reader=boom,
                                 gmail_reader=boom, parity_reader=boom, connectors_reader=boom)
        assert inv["tasks"] == [] and inv["parity"]["available"] is False and inv["live_connectors"] == []

    def test_default_readers_resolve_at_call_time(self, monkeypatch):
        # lens F #4: the first cut bound the readers as default arguments, so a
        # monkeypatch on the module was inert and the "unit" test spawned schtasks
        seen = {}
        monkeypatch.setattr(si, "read_task_registry", lambda: seen.setdefault("reg", True) and [])
        monkeypatch.setattr(si, "read_gmail_watermarks", lambda: {"PATCHED@example.com": int(NOW - H)})
        monkeypatch.setattr(si, "read_parity_report", lambda: {"available": False, "error": "patched", "path": "P"})
        monkeypatch.setattr(si, "live_tool_families", lambda e: ["PATCHED-CONNECTOR"])
        monkeypatch.setattr(si.run_marker, "latest_by_task", lambda: {})
        inv = si.build_inventory(kb=_KB(), detail=True, now=NOW)
        text = si.render_inventory(inv)
        assert seen.get("reg") is True
        assert "PATCHED@example.com" in text and "PATCHED-CONNECTOR" in text and "patched" in text


# ── wiring ───────────────────────────────────────────────────────────────────
class TestWiring:
    def test_tool_is_defined_dispatched_global_and_timed(self):
        names = [t["name"] for t in td.TOOL_DEFINITIONS]
        assert names.count("cora_self_inventory") == 1
        defn = next(t for t in td.TOOL_DEFINITIONS if t["name"] == "cora_self_inventory")
        assert defn["input_schema"]["properties"] == {}
        assert "Read-only" in defn["description"] and "NEVER evidence of absence" in defn["description"]
        assert "NOT for live systems" in defn["description"] and "NOT for content questions" in defn["description"]
        assert callable(td._TOOL_FUNCTIONS["cora_self_inventory"])
        assert "cora_self_inventory" in td._GLOBAL_CORE_TOOLS
        assert td._TOOL_TIMEOUTS["cora_self_inventory"] == 20

    @pytest.mark.parametrize("entity", ["FNDR", "F3E", "LEX", "LEX-LLC", "OSN", "HJRPROD", "WIDGETS-INC"])
    def test_exposed_to_every_entity(self, entity):
        assert "cora_self_inventory" in {t["name"] for t in td.tools_for_entity(entity)}

    def test_tool_function_founder_vs_channel_scope(self, monkeypatch):
        # every reader stubbed (resolved at call time now) -- no schtasks, no G:
        monkeypatch.setattr(td, "_notes_kb", lambda: (_KB(), threading.Lock()))
        monkeypatch.setattr(si, "read_task_registry", _registry)
        monkeypatch.setattr(si, "read_gmail_watermarks", lambda: {"PATCHED@example.com": int(NOW - H)})
        monkeypatch.setattr(si, "read_parity_report", lambda: {"available": False, "error": "n/a", "path": "P"})
        monkeypatch.setattr(si, "live_tool_families", lambda e: [f"CONNECTORS-FOR-{e}"])
        monkeypatch.setattr(si.run_marker, "latest_by_task", lambda: {})
        founder = td._TOOL_FUNCTIONS["cora_self_inventory"](HARRISON, "F3E", {})
        channel = td._TOOL_FUNCTIONS["cora_self_inventory"]("U0B3VGWJTMJ", "F3E", {})
        assert "PINNED EXCLUSIONS" in founder and "PATCHED@example.com" in founder
        assert "PINNED EXCLUSIONS" in channel and "PATCHED@example.com" not in channel
        assert "CONNECTORS-FOR-F3E" in founder and "CONNECTORS-FOR-F3E" in channel   # the asker's entity is passed
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
        # LAST in the chain: below code-queue, delegate, staged-write AND Asana (lens E #1)
        src = Path(app.__file__).read_text(encoding="utf-8")
        body = src[src.index("def _dispatch_qa("):]
        i_queue = body.index("_code_queue_capture_intent(user_message)")
        i_deleg = body.index("_delegate_work_intent(user_message)")
        i_staged = body.index("force_tool = _staged_write_force_tool(user_message)")
        i_asana = body.index("force_tool = _asana_destructive_intent(user_message)")
        i_inv = body.index('force_tool = "cora_self_inventory"')
        assert i_queue < i_deleg < i_staged < i_asana < i_inv
        # gated on the same predicate the cache-read bypass uses, and never on a grant turn
        assert "inventory_turn = bool(user_id) and retrieval_grant is None and _self_inventory_force(user_message) is not None" in body
        assert "and not web_intent and not inventory_turn:" in body
        assert "if force_tool is None and inventory_turn:" in body

    def test_explicit_commands_win_and_writes_are_never_stolen(self, app):
        q = "queue a code session: do you have access to the Drive folder? the tool double-posts"
        assert app._code_queue_capture_intent(q)          # the command matches ...
        assert app._self_inventory_force(q) is None       # ... and the meta shape yields to it by construction
        for msg, tool in [
            ("Complete the 'send Larry the deck' task -- the deck is in your knowledge base right", "asana_complete_task"),
            ("remember that the Sprouts folder is in your sources now", "cora_remember"),
            ("DM Tommy that the Q3 numbers are in your knowledge base", "slack_send_dm"),
        ]:
            assert app._self_inventory_force(msg) is None, msg
            assert (app._staged_write_force_tool(msg) or app._asana_destructive_intent(msg)) == tool, msg


def test_list_sync_states_on_a_real_kb(tmp_path):
    from cora.knowledge_base.store import KnowledgeBase
    kb = KnowledgeBase(tmp_path / "kb.db")
    try:
        kb.set_sync_state("static_md", 100, 90)
        kb.set_sync_state("drive_sweep_x@y.com", 200)
        assert kb.list_sync_states() == {"drive_sweep_x@y.com": (200, None), "static_md": (100, 90)}
    finally:
        kb.close()
