"""C1 -- evidence-attached kickoffs + the EVIDENCE FLOOR (Code #12, cq-b6f2f4825ffb).

Fixture = the four kickoffs the 2026-09-07 Monday menu generated with an EMPTY
evidence field, replayed in the shapes the floor READS (evidence / summary / signal /
seeded / reporter -- titles abbreviated, classifier fields omitted; D-051 lens D LOW #12):
  cq-00648c3d162d  deflection, evidence [{channel D0B4CTD3B09, ts "", note <raw ask>}]
  cq-651e6783994f  capability, evidence [{channel "", ts "", note "#fndr"}]  (Harrison's QUESTION)
  cq-17f7595b8c45  LEX deflection, evidence [{channel C0B3RHB3XPU, ts ""}]
  cq-405d735c0143  LEX capability, evidence [{channel "", ts ""}]
The floor must refuse exactly these four on replay and pass any item carrying a
permalink (channel_id + ts) or an explicit seed body.

VERIFY-FIRST record (read the ledger, not the kickoff): cq-651e6783994f carries an
`approved` event at 2026-09-03T23:38:41Z, three minutes after its DM card -- the
only writer of that event is the Queue button, so the APPROVED came from a tap,
not from the capture path. What the capture path CAN guarantee, and now pins, is
that a passive capture mints PROPOSED (the founder fast-path lives only in the
explicit tool); the floor is what keeps such an item from becoming a kickoff.
"""

from __future__ import annotations

import pytest

from cora import code_queue as cq

HARRISON = "U0B2RM2JYJ1"


@pytest.fixture
def qenv(tmp_path, monkeypatch):
    monkeypatch.setattr(cq, "_EVENT_LEDGER", tmp_path / "code-session-queue.jsonl")
    monkeypatch.setattr(cq, "_FINGERPRINT_LEDGER", tmp_path / "fp.jsonl")
    monkeypatch.setattr(cq, "_SIGNALS_LEDGER", tmp_path / "sig.jsonl")
    monkeypatch.setattr(cq, "_NOTES_DIR", tmp_path / "_notes")
    monkeypatch.setenv("FOUNDER_OS_ROOT", str(tmp_path / "founder-os"))
    monkeypatch.setenv("CORA_CODE_QUEUE", "log")
    monkeypatch.setattr(cq, "HARRISON_ID", HARRISON)
    monkeypatch.setattr(cq, "_SYNC", True)

    def _plain_write(path, text, **kw):
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text, encoding="utf-8")
    monkeypatch.setattr(cq.drive_io, "write_text_atomic", _plain_write)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setattr(cq, "_default_embed", lambda texts: [])
    cq._STAGING_INFLIGHT.clear()
    return tmp_path


# The four 9/7 rows, as captured (status APPROVED = after the tap; evidence as stored).
FIXTURE_ROWS = [
    {"id": "cq-00648c3d162d", "kind": "bug", "severity": "P2", "entity": "FNDR",
     "signal": "deflection", "title": "Card that was previously visible is no longer displayed",
     "summary": "Card that was previously visible is no longer displayed",
     "representative": "I don't see any card anymore. Surface it again",
     "evidence": [{"channel_id": "D0B4CTD3B09", "ts": "",
                   "note": "I don't see any card anymore. Surface it again"}]},
    {"id": "cq-651e6783994f", "kind": "feature", "severity": "P3", "entity": "FNDR",
     "signal": "capability",
     "title": "do you have access to all the Cowork Cascade knowledge now as well?",
     "summary": "do you have access to all the Cowork Cascade knowledge now as well?",
     "representative": "do you have access to all the Cowork Cascade knowledge now as well?",
     "evidence": [{"channel_id": "", "ts": "", "note": "#fndr"}]},
    {"id": "cq-17f7595b8c45", "kind": "feature", "severity": "P2", "entity": "LEX",
     "signal": "deflection", "title": "[LEX build ask -- details withheld]", "summary": "",
     "representative": "", "evidence": [{"channel_id": "C0B3RHB3XPU", "ts": ""}]},
    {"id": "cq-405d735c0143", "kind": "feature", "severity": "P3", "entity": "LEX",
     "signal": "capability", "title": "[LEX build ask -- details withheld]", "summary": "",
     "representative": "", "evidence": [{"channel_id": "", "ts": ""}]},
]


def _replay(row: dict, status: str = "APPROVED") -> str:
    rec = dict(row)
    rec.update({"ts": cq._now_iso(), "status": status, "count": 1, "reporter": HARRISON,
                "fingerprint": "f" * 40})
    cq._append_event({"event": "captured", **rec})
    return rec["id"]


class TestFloorPredicate:
    @pytest.mark.parametrize("row", FIXTURE_ROWS, ids=[r["id"] for r in FIXTURE_ROWS])
    def test_each_97_row_has_no_evidence(self, row):
        assert cq.has_evidence(row) is False
        assert cq.evidence_permalinks(row) == []

    def test_permalink_passes(self):
        row = dict(FIXTURE_ROWS[0])
        row["evidence"] = [{"channel_id": "D0B4CTD3B09", "ts": "1788270727.411619", "note": "x"}]
        assert cq.has_evidence(row) is True
        assert cq.evidence_permalinks(row) == [
            "https://hjr-global.slack.com/archives/D0B4CTD3B09/p1788270727411619"]

    def test_explicit_seed_body_passes(self, qenv):
        cid = cq.seed_item(kind="bug", severity="P2", title="Seeded finding", summary="the body",
                           entity="F3E", signal="explicit", status="APPROVED")
        assert cq.has_evidence(cq.get_item(cid)) is True

    def test_explicit_tool_request_passes(self):
        row = {"signal": "explicit", "summary": "", "evidence": [
            {"channel_id": "D1", "ts": "", "note": "please add retry to uploads"}]}
        assert cq.has_evidence(row) is True

    def test_explicit_but_blank_everything_fails(self):
        assert cq.has_evidence({"signal": "explicit", "summary": "", "evidence": []}) is False

    @pytest.mark.parametrize("signal", ["tool_error", "friction", "v2b_s5_review_deferred",
                                        "kb_eval_persistent_failure:auto-ka-1"])
    def test_seed_shape_passes_under_any_signal_label(self, qenv, signal):
        """Live census 2026-09-09: seeds carry nine signal labels; the body is the
        evidence whatever the label says."""
        cid = cq.seed_item(kind="bug", severity="P2", title=f"seed {signal}", summary="s",
                           entity="F3E", signal=signal, status="APPROVED")
        rec = cq.get_item(cid)
        assert rec.get("seeded") is True and cq.has_evidence(rec) is True
        legacy = {k: v for k, v in rec.items() if k != "seeded"}  # a pre-stamp row
        assert cq._is_seed_shaped(legacy) is True and cq.has_evidence(legacy) is True

    def test_passive_capture_note_is_not_a_seed_body(self):
        """The 9/7 class: raw trigger text in the note, classifier text in the
        summary -- different strings, no permalink -> not evidence."""
        assert cq._is_seed_shaped(FIXTURE_ROWS[0]) is False
        assert cq._is_seed_shaped(FIXTURE_ROWS[1]) is False

    def test_lex_explicit_ask_passes_on_a_channel_pointer(self):
        """A LEX item's body is redacted at rest -- but an EXPLICIT ask is a human
        typing cora_queue_code_session in a known channel; redaction is Cora's own
        act, not missing provenance (D-051 lens B MED #7: queue_explicit wrote ts=""
        and the LEX scrub drops the note, so every LEX explicit ask was floored)."""
        assert cq.has_evidence({"signal": "explicit", "summary": "", "entity": "LEX",
                                "evidence": [{"channel_id": "C1", "ts": ""}]}) is True
        assert cq.has_evidence({"signal": "explicit", "summary": "", "entity": "LEX",
                                "evidence": [{"channel_id": "C1", "ts": "1.2"}]}) is True
        # no channel, no ts, redacted body: still floored
        assert cq.has_evidence({"signal": "explicit", "summary": "", "entity": "LEX",
                                "evidence": [{"channel_id": "", "ts": ""}]}) is False
        # the passive LEX 9/7 rows (deflection / capability) stay floored
        assert cq.has_evidence(FIXTURE_ROWS[2]) is False and cq.has_evidence(FIXTURE_ROWS[3]) is False

    def test_seeded_flag_without_a_body_is_floored(self):
        """D-051 lens A LOW #10: the flag says a seed wrote it; the floor still needs
        something to build from -- a LEX seed (redacted title, blank summary) has none."""
        assert cq.has_evidence({"seeded": True, "signal": "friction", "summary": "",
                                "title": cq._LEX_REDACTED_TITLE, "evidence": [{"channel_id": "", "ts": ""}]}) is False
        assert cq.has_evidence({"seeded": True, "signal": "friction", "summary": "",
                                "title": "a real title", "evidence": []}) is True

    def test_seed_shape_needs_the_seed_reporter(self):
        """D-051 lens B LOW: a channel-less passive capture whose note equals its
        summary must not pass as a seed -- every legacy seed carries seed_item's reporter."""
        row = {"signal": "deflection", "summary": "Can Cora do X?", "reporter": "U_SOMEONE",
               "evidence": [{"channel_id": "", "ts": "", "note": "Can Cora do X?"}]}
        assert cq._is_seed_shaped(row) is False and cq.has_evidence(row) is False
        assert cq._is_seed_shaped({**row, "reporter": HARRISON}) is True

    def test_permalink_needs_both_halves(self):
        assert cq.slack_permalink("C1", "") == ""
        assert cq.slack_permalink("", "1.2") == ""
        assert cq.slack_permalink("C1", "1788270727.411619") == \
            "https://hjr-global.slack.com/archives/C1/p1788270727411619"


class TestFloorHolds:
    @pytest.mark.parametrize("row", FIXTURE_ROWS, ids=[r["id"] for r in FIXTURE_ROWS])
    def test_replayed_row_is_refused_by_every_auto_path(self, qenv, row):
        cid = _replay(row, status="APPROVED")
        # the single implementation every path shares
        outcome, msg = cq.ensure_kickoff_staged(cid)
        assert outcome == "no_evidence"
        assert cid in msg and f"stage {cid}" in msg and "no Slack permalink" in msg
        # the Stage BUTTON is not the override
        outcome2, msg2 = cq.process_queue_action(cq.ACTION_STAGE, cid, HARRISON)
        assert outcome2 == "no_evidence" and f"stage {cid}" in msg2
        rec = cq.get_item(cid)
        assert rec["status"] == "APPROVED" and not rec.get("prompt_path")
        assert not list((qenv / "founder-os").rglob("*.md"))  # nothing written

    def test_stage_bundle_holds_the_floor(self, qenv):
        """D-051 lens B HIGH #1: the Monday menu's Stage-bundle button bypassed the
        floor entirely -- the same surface and button family as the 9/7 incident."""
        a = _replay(FIXTURE_ROWS[0]); b = _replay(FIXTURE_ROWS[1])
        o, msg = cq.stage_bundle(f"bundle:{a},{b}", HARRISON)
        assert o == "no_evidence" and a in msg and b in msg
        assert cq.get_item(a)["status"] == "APPROVED" and cq.get_item(b)["status"] == "APPROVED"
        # mixed: the evidenced row stages, the floored row is refused BY ID
        seeded = cq.seed_item(kind="bug", severity="P2", title="Seeded with body", summary="the body",
                              entity="FNDR", signal="explicit", status="APPROVED")
        o, msg = cq.stage_bundle(f"bundle:{seeded},{a}", HARRISON)
        assert o == "staged" and "1 refused by the evidence floor" in msg and a in msg
        assert cq.get_item(seeded)["status"] == "STAGED" and cq.get_item(a)["status"] == "APPROVED"

    def test_override_sentence_only_when_the_founder_overrode(self):
        """D-051 lens B HIGH #1: the kickoff used to CLAIM 'staged by the founder's
        override' for every floored item, whoever staged it."""
        plain = "\n".join(cq._evidence_block([FIXTURE_ROWS[1]]))
        assert "EVIDENCE: none on the item" in plain and "attach the thread before firing" in plain
        assert "founder's typed override" not in plain
        overridden = "\n".join(cq._evidence_block([FIXTURE_ROWS[1]], override=True))
        assert "founder's typed override" in overridden
        # a passive LEX capture with a channel but no ts renders NO pointer (the
        # cq-89fdad5f0f86 half-pointer artifact stays out); an EXPLICIT LEX ask
        # renders its channel pointer, never an invented permalink
        lex = "\n".join(cq._evidence_block([FIXTURE_ROWS[2]]))
        assert "C0B3RHB3XPU" not in lex and "permalink:" not in lex
        typed = dict(FIXTURE_ROWS[2], signal="explicit")
        lex2 = "\n".join(cq._evidence_block([typed]))
        assert "channel pointer: <slack://channel?id=C0B3RHB3XPU>" in lex2 and "permalink:" not in lex2

    def test_founder_typed_stage_is_the_deliberate_override(self, qenv):
        cid = _replay(FIXTURE_ROWS[0], status="APPROVED")
        outcome, path = cq.stage_by_id(cid, HARRISON)
        assert outcome == "staged"
        assert cq.get_item(cid)["status"] == "STAGED"
        body = open(path, encoding="utf-8").read()
        assert "## 0. Evidence" in body
        assert "EVIDENCE: none on the item" in body  # the override is named, never hidden
        assert "signal=deflection" in body

    def test_priority_approve_holds_the_floor_and_re_cards(self, qenv):
        row = dict(FIXTURE_ROWS[0])
        row["severity"] = "P1"
        cid = _replay(row, status="PROPOSED")
        outcome, msg = cq.process_queue_action(cq.ACTION_APPROVE, cid, HARRISON)
        assert outcome == "approved"
        assert "Queued (APPROVED)" in msg and "NOT staged" in msg and f"stage {cid}" in msg
        rec = cq.get_item(cid)
        assert rec["status"] == "APPROVED" and not rec.get("prompt_path")

    def test_priority_approve_with_a_permalink_still_auto_stages(self, qenv):
        row = dict(FIXTURE_ROWS[0])
        row.update(severity="P1",
                   evidence=[{"channel_id": "D0B4CTD3B09", "ts": "1788270727.411619", "note": "x"}])
        cid = _replay(row, status="PROPOSED")
        outcome, msg = cq.process_queue_action(cq.ACTION_APPROVE, cid, HARRISON)
        assert outcome == "approved" and "prompt staged" in msg
        assert cq.get_item(cid)["status"] == "STAGED"

    def test_seed_stage_now_passes_the_floor_via_its_body(self, qenv):
        cid = cq.seed_item(kind="bug", severity="HIGH", title="Seeded HIGH", summary="the body",
                           entity="F3E", signal="explicit", status="APPROVED", stage_now=True)
        assert cq.get_item(cid)["status"] == "STAGED"


class TestProvenanceGoingForward:
    def test_message_signal_records_the_permalink_half(self, qenv, monkeypatch):
        monkeypatch.setattr(cq, "classify_candidate", lambda q, e: {
            "kind": "bug", "severity": "P2", "summary": "Cards vanish", "subsystem_guess": "cards"})
        cq.capture_message_signal("it's broken, the card is gone", "FNDR", "D0B4CTD3B09", "dm",
                                  HARRISON, message_ts="1788270727.411619")
        items = cq.load_items()
        assert len(items) == 1
        ev = items[0]["evidence"][0]
        assert ev["channel_id"] == "D0B4CTD3B09" and ev["ts"] == "1788270727.411619"
        assert cq.has_evidence(items[0]) is True

    def test_capability_ask_records_channel_id_and_ts(self, qenv):
        cq.capture_capability_ask("can you access Klaviyo?", "F3E", "f3e-sales", "U_T",
                                  channel_id="C0B3N5YG1SR", message_ts="1788357868.443679")
        it = cq.load_items()[0]
        assert it["evidence"][0] == {"channel_id": "C0B3N5YG1SR", "ts": "1788357868.443679",
                                     "note": "#f3e-sales"}
        assert cq.has_evidence(it) is True

    def test_founder_passive_question_mints_proposed_never_approved(self, qenv):
        """cq-b6f2f4825ffb's guarantee: the fast-path lives only in queue_explicit."""
        cq.capture_capability_ask("do you have access to all the Cowork Cascade knowledge "
                                  "now as well?", "FNDR", "fndr", HARRISON)
        it = cq.load_items()[0]
        assert it["status"] == "PROPOSED"
        assert cq.has_evidence(it) is False  # name-only note: floor holds until a thread is attached

    def test_tool_failure_records_thread_permalink(self, qenv):
        cq.capture_tool_failure("asana_get_my_tasks", "F3E", "KeyError", "C0B3N5YG1SR", "U_T",
                                False, thread_ts="1788357868.443679")
        it = cq.load_items()[0]
        assert it["evidence"][0]["channel_id"] == "C0B3N5YG1SR"
        assert it["evidence"][0]["ts"] == "1788357868.443679"
        assert cq.has_evidence(it) is True

    def test_kickoff_evidence_block_carries_permalink_seed_text_and_classifier(self, qenv):
        cid = cq.seed_item(kind="bug", severity="HIGH", title="Blog lane dead on scope",
                           summary="articleCreate denied on first fire", entity="F3E",
                           signal="explicit", status="APPROVED")
        cq._append_event({"event": "evidence", "ts": cq._now_iso(), "id": cid,
                          "evidence": {"channel_id": "C0BCUBUDHAR", "ts": "1788053306.806459",
                                       "note": "first live fire 8/31 08:50"}})
        _o, path = cq.stage_by_id(cid, HARRISON)
        body = open(path, encoding="utf-8").read()
        assert "https://hjr-global.slack.com/archives/C0BCUBUDHAR/p1788053306806459" in body
        assert "summary: articleCreate denied on first fire" in body
        assert "signal=explicit" in body and "note: first live fire 8/31 08:50" in body
        assert "EVIDENCE: none" not in body

    def test_call_sites_pass_the_ts(self):
        import inspect
        import cora.app as app_module
        from cora.tools import tool_dispatch as td
        src = inspect.getsource(app_module._dispatch_qa)
        assert src.count("code_queue.capture_message_signal(") == 2
        assert src.count('message_ts=reply_thread_ts or ""') == 2
        dsrc = inspect.getsource(td.dispatch)
        assert dsrc.count("code_queue.capture_tool_failure(") == 2
        assert dsrc.count('thread_ts=str(thread_ts or "")') == 2
