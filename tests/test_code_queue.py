"""Tests for the code-session queue core (src/cora/code_queue.py).

Covers: flag mapping, ledger fold + status transitions, fingerprint dedup
(exact/fuzzy/recurrence-threads-not-new-card), PHI matrix (LEX pointers-only,
is_phi_risk drop, non-LEX note kept), classifier fail-closed + knowledge routing,
DM cap + overflow, founder fast-path, process_queue_action Harrison gate +
idempotency, P1 approve stages a prompt, S1 timeout counter-gate + crash-immediate,
S6 friction cross-registration, backlog render idempotent + fail-soft, prompt
filename KB-excluded, capture fail-soft, weekly menu.
"""

from __future__ import annotations

import json

import pytest

from cora import code_queue as cq


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────
class FakeClient:
    """Records Slack calls; conversations_open + chat_postMessage + history."""

    def __init__(self):
        self.posts: list[dict] = []
        self._ts = 1000

    def conversations_open(self, users=None):
        return {"channel": {"id": "D123"}}

    def chat_postMessage(self, **kwargs):
        self._ts += 1
        kwargs["ts"] = f"{self._ts}.0"
        self.posts.append(kwargs)
        return {"ts": kwargs["ts"]}

    def conversations_history(self, **kwargs):
        return {"messages": [{"text": self.history_text}]}

    history_text = "the answer is 42"


@pytest.fixture
def qenv(tmp_path, monkeypatch):
    """Isolate all ledgers + notes + backlog to tmp; run capture inline; live flag."""
    monkeypatch.setattr(cq, "_EVENT_LEDGER", tmp_path / "code-session-queue.jsonl")
    monkeypatch.setattr(cq, "_FINGERPRINT_LEDGER", tmp_path / "code-queue-fingerprints.jsonl")
    monkeypatch.setattr(cq, "_SIGNALS_LEDGER", tmp_path / "code-queue-signals.jsonl")
    monkeypatch.setattr(cq, "_NOTES_DIR", tmp_path / "_notes")
    monkeypatch.setenv("FOUNDER_OS_ROOT", str(tmp_path / "founder-os"))
    monkeypatch.setenv("CORA_CODE_QUEUE", "live")
    monkeypatch.setenv("HARRISON_SLACK_USER_ID", "U0B2RM2JYJ1")
    monkeypatch.setattr(cq, "HARRISON_ID", "U0B2RM2JYJ1")
    monkeypatch.setattr(cq, "_SYNC", True)
    # Decouple from drive_io mount semantics: write the backlog straight to tmp.
    backlog_writes: dict[str, str] = {}

    def _plain_write(path, text, **kw):
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text, encoding="utf-8")
        backlog_writes["last"] = text

    monkeypatch.setattr(cq.drive_io, "write_text_atomic", _plain_write)
    # No ANTHROPIC key by default -> classifier fail-closed / generator skeleton.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    return {"tmp": tmp_path, "backlog": backlog_writes}


# ─────────────────────────────────────────────────────────────────────────────
# Flag mapping
# ─────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("val,expected", [
    ("off", "off"), ("log", "log"), ("live", "live"),
    ("LIVE", "live"), (" live ", "live"),
    ("bogus", "off"), ("", "off"),
])
def test_flag_mapping(monkeypatch, val, expected):
    monkeypatch.setenv("CORA_CODE_QUEUE", val)
    assert cq.code_queue_level() == expected


def test_flag_missing_defaults_off(monkeypatch):
    monkeypatch.delenv("CORA_CODE_QUEUE", raising=False)
    assert cq.code_queue_level() == "off"


# ─────────────────────────────────────────────────────────────────────────────
# Fold + status transitions
# ─────────────────────────────────────────────────────────────────────────────
def test_fold_status_transitions(qenv):
    cid = cq.seed_item(kind="bug", severity="P2", title="Foo broke", summary="x",
                       entity="F3E", signal="tool_error", status="PROPOSED")
    assert cq.get_item(cid)["status"] == "PROPOSED"
    cq._append_event({"event": "approved", "ts": cq._now_iso(), "id": cid})
    assert cq.get_item(cid)["status"] == "APPROVED"
    cq._append_event({"event": "staged", "ts": cq._now_iso(), "id": cid, "prompt_path": "/p"})
    it = cq.get_item(cid)
    assert it["status"] == "STAGED" and it["prompt_path"] == "/p"
    cq._append_event({"event": "shipped", "ts": cq._now_iso(), "id": cid})
    assert cq.get_item(cid)["status"] == "SHIPPED"


def test_fold_recurrence_increments_count(qenv):
    cid = cq.seed_item(kind="bug", severity="P2", title="Foo", summary="x",
                       entity="F3E", signal="tool_error", status="PROPOSED")
    assert cq.get_item(cid)["count"] == 1
    cq._append_event({"event": "recurrence", "ts": cq._now_iso(), "id": cid})
    cq._append_event({"event": "recurrence", "ts": cq._now_iso(), "id": cid})
    assert cq.get_item(cid)["count"] == 3


# ─────────────────────────────────────────────────────────────────────────────
# Fingerprint dedup
# ─────────────────────────────────────────────────────────────────────────────
def test_dedup_exact_recurrence(qenv, monkeypatch):
    fake = FakeClient()
    rec = {"kind": "bug", "severity": "P2", "title": "tool X crashed", "summary": "boom",
           "entity": "F3E", "signal": "tool_error", "representative": "tool_x",
           "evidence": [{"channel_id": "C1", "ts": ""}], "reporter": "U1"}
    id1 = cq._capture(dict(rec), client_factory=lambda: fake)
    id2 = cq._capture(dict(rec), client_factory=lambda: fake)
    assert id1 == id2
    assert cq.get_item(id1)["count"] == 2
    # first send = a card; second = a THREAD reply, not a new card
    assert len(fake.posts) == 2
    assert "blocks" in fake.posts[0]
    assert fake.posts[1].get("thread_ts") == fake.posts[0]["ts"]
    assert "blocks" not in fake.posts[1]


def test_dedup_fuzzy_same_signal(qenv):
    fake = FakeClient()
    r1 = {"kind": "feature", "severity": "P3", "title": "add rangeme status tool",
          "summary": "", "entity": "F3E", "signal": "phrase",
          "representative": "can cora pull the rangeme status please",
          "evidence": [], "reporter": "U1"}
    r2 = dict(r1, representative="can cora pull the rangeme status pls")
    id1 = cq._capture(dict(r1), client_factory=lambda: fake)
    id2 = cq._capture(dict(r2), client_factory=lambda: fake)
    assert id1 == id2  # fuzzy >= 0.85


def test_dismissed_fingerprint_blocks_reproposal(qenv):
    # A dismissed fingerprint still lives in the fingerprint ledger -> dedups.
    fake = FakeClient()
    rec = {"kind": "bug", "severity": "P2", "title": "z", "summary": "z",
           "entity": "OSN", "signal": "tool_error", "representative": "osn_pulse",
           "evidence": [], "reporter": "U1"}
    id1 = cq._capture(dict(rec), client_factory=lambda: fake)
    cq.process_queue_action(cq.ACTION_DISMISS, id1, "U0B2RM2JYJ1")
    id2 = cq._capture(dict(rec), client_factory=lambda: fake)
    assert id2 == id1  # never re-proposes


# ─────────────────────────────────────────────────────────────────────────────
# PHI matrix
# ─────────────────────────────────────────────────────────────────────────────
def test_lex_evidence_pointers_only(qenv):
    rec = {"kind": "bug", "severity": "P2", "title": "lex tool issue", "summary": "generic",
           "entity": "LEX-LLC", "signal": "tool_error", "representative": "lex_tool",
           "evidence": [{"channel_id": "C9", "ts": "111.0", "note": "client detail text"}],
           "reporter": "U1"}
    cid = cq._capture(dict(rec))
    ev = cq.get_item(cid)["evidence"][0]
    assert ev == {"channel_id": "C9", "ts": "111.0"}  # note stripped


def test_non_lex_note_kept(qenv):
    rec = {"kind": "feature", "severity": "P3", "title": "f3e wish", "summary": "generic",
           "entity": "F3E", "signal": "phrase", "representative": "f3e wish text",
           "evidence": [{"channel_id": "C1", "ts": "1.0", "note": "please add a widget"}],
           "reporter": "U1"}
    cid = cq._capture(dict(rec))
    assert cq.get_item(cid)["evidence"][0].get("note") == "please add a widget"


def test_phi_summary_drops_item(qenv, monkeypatch):
    monkeypatch.setattr(cq.phi_guard, "is_phi_risk", lambda t: True)
    rec = {"kind": "bug", "severity": "P2", "title": "x", "summary": "y",
           "entity": "F3E", "signal": "tool_error", "representative": "x",
           "evidence": [], "reporter": "U1"}
    assert cq._capture(dict(rec)) is None
    assert cq.load_items() == []


def test_phi_check_error_fails_closed(qenv, monkeypatch):
    def _boom(_t):
        raise RuntimeError("phi check exploded")
    monkeypatch.setattr(cq.phi_guard, "is_phi_risk", _boom)
    rec = {"kind": "bug", "severity": "P2", "title": "x", "summary": "y",
           "entity": "F3E", "signal": "tool_error", "representative": "x",
           "evidence": [], "reporter": "U1"}
    assert cq._capture(dict(rec)) is None  # fail-closed -> drop


# ─────────────────────────────────────────────────────────────────────────────
# Classifier
# ─────────────────────────────────────────────────────────────────────────────
def test_classifier_fail_closed_no_key(qenv, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert cq.classify_candidate("cora should do X", "F3E") is None


def test_message_signal_no_key_no_item(qenv):
    cq.capture_message_signal("cora should nudge me when a deal stalls", "F3E",
                              "C1", "f3e-sales", "U1")
    assert cq.load_items() == []  # classifier fail-closed -> nothing queued


def test_knowledge_routed_not_queued(qenv, monkeypatch):
    monkeypatch.setattr(cq, "classify_candidate", lambda m, e: {"kind": "knowledge"})
    routed = {}
    import cora.knowledge_gaps as kg
    monkeypatch.setattr(kg, "log_gap", lambda **kw: routed.update(kw))
    cq.capture_message_signal("can cora tell me the f3e price", "F3E", "C1", "f3e", "U1")
    assert cq.load_items() == []            # never queued
    assert routed.get("question")           # routed to the flywheel


def test_noise_dropped_and_remembered(qenv, monkeypatch):
    monkeypatch.setattr(cq, "classify_candidate", lambda m, e: {"kind": "noise"})
    cq.capture_message_signal("cora should get a coffee lol", "F3E", "C1", "f3e", "U1")
    assert cq.load_items() == []


def test_phrase_becomes_bug_item(qenv, monkeypatch):
    monkeypatch.setattr(cq, "classify_candidate", lambda m, e: {
        "kind": "bug", "severity": "P1", "summary": "inventory tool broken",
        "subsystem_guess": "shopify", "fix_sketch": "fix SKU map"})
    cq.capture_message_signal("the inventory tool is broken", "F3E", "C1", "f3e", "U1")
    items = cq.load_items()
    assert len(items) == 1 and items[0]["kind"] == "bug" and items[0]["signal"] == "phrase"


# ─────────────────────────────────────────────────────────────────────────────
# Founder fast-path / explicit throttle
# ─────────────────────────────────────────────────────────────────────────────
def test_founder_fastpath_approved(qenv):
    cid = cq.queue_explicit("U0B2RM2JYJ1", "F3E", "C1", "add a TikTok voucher check", True)
    assert cq.get_item(cid)["status"] == "APPROVED"


def test_teammate_proposed(qenv):
    cid = cq.queue_explicit("U9", "F3E", "C1", "the tiktok digest misses vouchers", False)
    assert cq.get_item(cid)["status"] == "PROPOSED"


def test_explicit_throttle(qenv):
    # Distinct requests (fuzzy dedup would collapse near-identical ones).
    distinct = [
        "add a tiktok voucher check to the digest",
        "fix the rangeme status refresh timing",
        "build an osn franchise thread pulse tool",
    ]
    assert len(distinct) == cq.EXPLICIT_THROTTLE_PER_DAY
    for req in distinct:
        assert cq.queue_explicit("U9", "F3E", "C1", req, False)
    assert cq.queue_explicit("U9", "F3E", "C1", "create a lex audit dashboard view", False) is None


# ─────────────────────────────────────────────────────────────────────────────
# DM cap + overflow
# ─────────────────────────────────────────────────────────────────────────────
def test_dm_cap_and_overflow(qenv):
    fake = FakeClient()
    for i in range(cq.MAX_DM_PER_DAY + 2):
        cq._capture({"kind": "bug", "severity": "P2", "title": f"issue {i}",
                     "summary": "s", "entity": "F3E", "signal": "tool_error",
                     "representative": f"tool_{i}", "evidence": [], "reporter": "U1"},
                    client_factory=lambda: fake)
    sent = [p for p in fake.posts if "blocks" in p]
    assert len(sent) == cq.MAX_DM_PER_DAY
    held = [it for it in cq.load_items() if it.get("dm_held")]
    assert len(held) == 2
    # overflow flush -> one summary DM, held cleared
    n = cq.maybe_flush_overflow(client_factory=lambda: fake)
    assert n == 2
    assert not [it for it in cq.load_items() if it.get("dm_held") and not it.get("dm_flushed")]


# ─────────────────────────────────────────────────────────────────────────────
# process_queue_action
# ─────────────────────────────────────────────────────────────────────────────
def test_action_harrison_gate(qenv):
    cid = cq.seed_item(kind="bug", severity="P2", title="t", summary="s",
                       entity="F3E", signal="tool_error", status="PROPOSED")
    outcome, _ = cq.process_queue_action(cq.ACTION_APPROVE, cid, "U_NOT_HARRISON")
    assert outcome == "not_authorized"
    assert cq.get_item(cid)["status"] == "PROPOSED"


def test_action_approve_dismiss_later_idempotent(qenv):
    cid = cq.seed_item(kind="feature", severity="P3", title="t", summary="s",
                       entity="F3E", signal="explicit", status="PROPOSED")
    o, _ = cq.process_queue_action(cq.ACTION_APPROVE, cid, "U0B2RM2JYJ1")
    assert o == "approved" and cq.get_item(cid)["status"] == "APPROVED"
    o2, _ = cq.process_queue_action(cq.ACTION_APPROVE, cid, "U0B2RM2JYJ1")
    assert o2 == "noop"
    cid2 = cq.seed_item(kind="bug", severity="P2", title="u", summary="s",
                        entity="F3E", signal="tool_error", status="PROPOSED")
    cq.process_queue_action(cq.ACTION_LATER, cid2, "U0B2RM2JYJ1")
    it = cq.get_item(cid2)
    assert it["status"] == "SNOOZED" and it.get("snooze_until")
    cid3 = cq.seed_item(kind="bug", severity="P2", title="v", summary="s",
                        entity="F3E", signal="tool_error", status="PROPOSED")
    cq.process_queue_action(cq.ACTION_DISMISS, cid3, "U0B2RM2JYJ1")
    assert cq.get_item(cid3)["status"] == "DISMISSED"


def test_p1_approve_stages_prompt(qenv):
    cid = cq.seed_item(kind="bug", severity="P1", title="tool crashed hard",
                       summary="s", entity="F3E", signal="tool_error", status="PROPOSED")
    outcome, msg = cq.process_queue_action(cq.ACTION_APPROVE, cid, "U0B2RM2JYJ1")
    assert outcome == "approved"
    it = cq.get_item(cid)
    assert it["status"] == "STAGED" and it.get("prompt_path")
    from pathlib import Path
    assert Path(it["prompt_path"]).exists()


def test_edit_updates_and_phi_guard(qenv, monkeypatch):
    cid = cq.seed_item(kind="bug", severity="P2", title="old", summary="old s",
                       entity="F3E", signal="tool_error", status="PROPOSED")
    o, _ = cq.apply_edit(cid, "U0B2RM2JYJ1", "new title", "new summary")
    assert o == "edited"
    assert cq.get_item(cid)["title"] == "new title"
    monkeypatch.setattr(cq.phi_guard, "is_phi_risk", lambda t: True)
    o2, _ = cq.apply_edit(cid, "U0B2RM2JYJ1", "phi title", "phi summary")
    assert o2 == "error"  # PHI-tripping edit rejected


# ─────────────────────────────────────────────────────────────────────────────
# S1 tool failure gating
# ─────────────────────────────────────────────────────────────────────────────
def test_s1_timeout_counter_gate(qenv):
    # 2 timeouts -> below threshold, no item
    for _ in range(cq.SILENT_TIMEOUT_THRESHOLD - 1):
        cq.capture_tool_failure("f3e_shopify_set_inventory", "F3E", "TimeoutError",
                                "C1", "U1", True, client_factory=lambda: FakeClient())
    assert cq.load_items() == []
    # 3rd timeout -> item
    cq.capture_tool_failure("f3e_shopify_set_inventory", "F3E", "TimeoutError",
                            "C1", "U1", True, client_factory=lambda: FakeClient())
    items = cq.load_items()
    assert len(items) == 1 and items[0]["severity"] == "P2"


def test_s1_crash_immediate(qenv):
    cq.capture_tool_failure("asana_create_task", "F3E", "ValueError", "C1", "U1",
                            False, client_factory=lambda: FakeClient())
    items = cq.load_items()
    assert len(items) == 1 and items[0]["severity"] == "P1" and items[0]["kind"] == "bug"


def test_s1_crash_no_user_not_carded(qenv):
    cq.capture_tool_failure("asana_create_task", "F3E", "ValueError", "", "",
                            False, client_factory=lambda: FakeClient())
    assert cq.load_items() == []


def test_s1_no_message_text_evidence(qenv):
    cq.capture_tool_failure("asana_create_task", "F3E", "ValueError", "C1", "U1", False)
    ev = cq.load_items()[0]["evidence"][0]
    assert "note" in ev and "no message text" in ev["note"]


# ─────────────────────────────────────────────────────────────────────────────
# S6 friction cross-registration
# ─────────────────────────────────────────────────────────────────────────────
def test_s6_register_from_efficiency(qenv):
    payload = {"title": "Build a BDM brand-voice check tool", "entity": "BDM",
               "recommendation": "extend brand_voice_check to BDM brands", "route": "cora_tool"}
    cid = cq.register_from_efficiency(payload)
    it = cq.get_item(cid)
    assert it["status"] == "APPROVED" and it["signal"] == "friction"
    # idempotent
    cid2 = cq.register_from_efficiency(payload)
    assert cid2 == cid


def test_s6_off_flag_noop(qenv, monkeypatch):
    monkeypatch.setenv("CORA_CODE_QUEUE", "off")
    assert cq.register_from_efficiency({"title": "x", "entity": "BDM", "route": "cora_tool"}) is None


# ─────────────────────────────────────────────────────────────────────────────
# Backlog render
# ─────────────────────────────────────────────────────────────────────────────
def test_backlog_render_content(qenv):
    cq.seed_item(kind="bug", severity="P1", title="Alpha", summary="s",
                 entity="F3E", signal="tool_error", status="APPROVED")
    text = cq.render_backlog_text()
    assert "GENERATED" in text and "Alpha" in text and "## APPROVED" in text


def test_backlog_render_failsoft(qenv, monkeypatch):
    def _raise(*a, **k):
        raise cq.drive_io.DriveUnavailable("mount gone") if hasattr(cq.drive_io, "DriveUnavailable") else RuntimeError("x")
    monkeypatch.setattr(cq.drive_io, "write_text_atomic", _raise)
    assert cq.render_backlog() is False  # never raises


# ─────────────────────────────────────────────────────────────────────────────
# Prompt generator
# ─────────────────────────────────────────────────────────────────────────────
def test_prompt_filename_kb_excluded(qenv):
    cid = cq.seed_item(kind="bug", severity="P1", title="Fix the digest date bug",
                       summary="s", entity="FNDR", signal="explicit", status="APPROVED")
    path = cq.generate_kickoff_prompt([cq.get_item(cid)])
    assert path and path.endswith(".md")
    from pathlib import Path
    from cora import kb_exclusions
    fname = Path(path).name
    assert "cora-code-prompt" in fname
    assert kb_exclusions.is_cora_internal_title(fname) is True  # KB-excluded for free


def test_prompt_skeleton_has_banner(qenv):
    cid = cq.seed_item(kind="bug", severity="P1", title="Widget", summary="s",
                       entity="F3E", signal="tool_error", status="APPROVED")
    path = cq.generate_kickoff_prompt([cq.get_item(cid)])
    from pathlib import Path
    body = Path(path).read_text(encoding="utf-8")
    assert "AUTO-GENERATED DRAFT" in body and "VERIFY-FIRST" in body


# ─────────────────────────────────────────────────────────────────────────────
# Capture fail-soft (guardrail #2)
# ─────────────────────────────────────────────────────────────────────────────
def test_capture_swallows_exceptions(qenv, monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("capture exploded")
    monkeypatch.setattr(cq, "_process_tool_failure", _boom)
    # Must NOT raise even though the worker raises (sync mode).
    cq.capture_tool_failure("t", "F3E", "E", "C1", "U1", False)


def test_off_flag_fully_inert(qenv, monkeypatch):
    monkeypatch.setenv("CORA_CODE_QUEUE", "off")
    cq.capture_tool_failure("asana_create_task", "F3E", "ValueError", "C1", "U1", False)
    cq.capture_message_signal("cora should X", "F3E", "C1", "f3e", "U1")
    assert cq.load_items() == []


# ─────────────────────────────────────────────────────────────────────────────
# Weekly menu
# ─────────────────────────────────────────────────────────────────────────────
def test_weekly_menu_bundles_and_stale(qenv):
    for i in range(4):
        cq.seed_item(kind="feature", severity="P3", title=f"F feature {i}", summary="s",
                     entity="F3E", signal="friction", status="APPROVED", subsystem_guess="shopify")
    cq.seed_item(kind="config", severity="P3", title="known answer add", summary="s",
                 entity="LEX", signal="friction", status="APPROVED")
    built = cq.build_weekly_menu()
    assert built is not None
    text, blocks = built
    assert "Monday menu" in text
    stage_btns = [b for b in blocks if b.get("type") == "actions"
                  and any(e.get("action_id") == cq.ACTION_STAGE for e in b.get("elements", []))]
    assert len(stage_btns) >= 1
    assert "config" in text.lower()


def test_weekly_menu_empty_returns_none(qenv):
    assert cq.build_weekly_menu() is None


def test_stage_bundle(qenv):
    ids = [cq.seed_item(kind="feature", severity="P3", title=f"B {i}", summary="s",
                        entity="F3E", signal="friction", status="APPROVED",
                        subsystem_guess="shopify") for i in range(3)]
    outcome, msg = cq.stage_bundle("bundle:" + ",".join(ids), "U0B2RM2JYJ1")
    assert outcome == "staged"
    for i in ids:
        it = cq.get_item(i)
        assert it["status"] == "STAGED" and it.get("bundle_id")


def test_menu_no_op_when_not_live(qenv, monkeypatch):
    monkeypatch.setenv("CORA_CODE_QUEUE", "log")
    cq.seed_item(kind="feature", severity="P3", title="X", summary="s",
                 entity="F3E", signal="friction", status="APPROVED")
    assert cq.maybe_send_weekly_menu(client_factory=lambda: FakeClient()) is False
