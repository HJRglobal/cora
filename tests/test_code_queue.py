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
    # Embedding dedup is OFF by default (stubbed -> [] -> fail-soft None): keeps the
    # suite network-free. Tests that exercise the semantic layer override _default_embed.
    monkeypatch.setattr(cq, "_default_embed", lambda texts: [])
    # Reset the module-global reservations/pending so they can't leak across tests.
    cq._DM_RESERVE.update({"date": None, "n": 0})
    cq._STAGING_INFLIGHT.clear()
    try:
        import cora.tools.tool_dispatch as _td
        _td._PENDING_CODE_QUEUE.clear()
    except Exception:  # noqa: BLE001
        pass
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
    monkeypatch.setattr(cq.phi_guard, "is_phi_risk_person_linked",
                        lambda t: True)
    rec = {"kind": "bug", "severity": "P2", "title": "x", "summary": "y",
           "entity": "F3E", "signal": "tool_error", "representative": "x",
           "evidence": [], "reporter": "U1"}
    assert cq._capture(dict(rec)) is None
    assert cq.load_items() == []


def test_lex_representative_not_persisted(qenv):
    rec = {"kind": "feature", "severity": "P3", "title": "lts scheduler tool",
           "summary": "generic", "entity": "LEX-LTS", "signal": "phrase",
           "representative": "cora should add an LTS scheduler",
           "evidence": [{"channel_id": "C1", "ts": ""}], "reporter": "U1"}
    cid = cq._capture(dict(rec))
    assert cq.get_item(cid)["representative"] == ""  # redacted in the event record
    fps = cq._read_jsonl(cq._FINGERPRINT_LEDGER)
    assert all(f.get("representative") == "" for f in fps if f.get("id") == cid)


def _counting_classifier(counter):
    def _c(_m, _e):
        counter["n"] += 1
        return {"kind": "bug", "severity": "P2", "summary": "x"}
    return _c


def test_message_signal_phi_dropped_preclassify(qenv, monkeypatch):
    counter = {"n": 0}
    monkeypatch.setattr(cq, "classify_candidate", _counting_classifier(counter))
    monkeypatch.setattr(cq.phi_guard, "is_phi_risk_person_linked",
                        lambda t: True)
    cq.capture_message_signal("cora should track patient billing authorization",
                              "LEX", "C1", "lex", "U1")
    assert cq.load_items() == [] and counter["n"] == 0  # classifier never called


def test_phi_check_error_fails_closed(qenv, monkeypatch):
    def _boom(_t):
        raise RuntimeError("phi check exploded")
    monkeypatch.setattr(cq.phi_guard, "is_phi_risk_person_linked", _boom)
    rec = {"kind": "bug", "severity": "P2", "title": "x", "summary": "y",
           "entity": "F3E", "signal": "tool_error", "representative": "x",
           "evidence": [], "reporter": "U1"}
    assert cq._capture(dict(rec)) is None  # fail-closed -> drop


def test_phi_tripping_subsystem_guess_blanked_at_capture(qenv, monkeypatch):
    """D-051 2026-07-31: subsystem_guess egresses via mixed-bundle prompt-path
    slugs (_affinity_key -> _bundle_theme -> _slug), where the LEX prompt_path
    redaction can't reach co-bundled non-LEX items -- so a PHI-tripping value is
    blanked at capture (item survives, hint dropped)."""
    monkeypatch.setattr(cq.phi_guard, "is_any_phi_request",
                        lambda t: "billing authorization" in t)
    rec = {"kind": "bug", "severity": "P2", "title": "tool crashed", "summary": "boom",
           "entity": "F3E", "signal": "tool_error", "representative": "tool",
           "subsystem_guess": "Bob Smith billing authorization",
           "evidence": [], "reporter": "U1"}
    cid = cq._capture(dict(rec))
    assert cid is not None                      # the item itself survives
    assert cq.get_item(cid)["subsystem_guess"] == ""

    clean = dict(rec, title="shopify sync loses inventory rows",
                 representative="a completely different shopify sync defect",
                 subsystem_guess="shopify inventory")
    cid2 = cq._capture(clean)
    assert cid2 != cid                          # genuinely distinct item
    assert cq.get_item(cid2)["subsystem_guess"] == "shopify inventory"


def test_phi_tripping_subsystem_guess_blanked_in_seed_lane(qenv, monkeypatch):
    """Same screen on the seed lane (verify-pass follow-up): seed_item is
    caller-supplied via the MCP cora_code_queue_seed tool and bypasses _capture,
    so it must screen subsystem_guess itself (falls back to the entity code)."""
    monkeypatch.setattr(cq.phi_guard, "is_any_phi_request",
                        lambda t: "SECRETSUB" in t)
    cid = cq.seed_item(kind="bug", severity="P2", title="clean title",
                       summary="clean summary", entity="F3E", signal="explicit",
                       status="PROPOSED", subsystem_guess="SECRETSUB detail")
    assert cid is not None
    assert cq.get_item(cid)["subsystem_guess"] == "F3E"  # blanked -> entity fallback


def test_lex_prompt_path_redacted_in_read_layer(qenv):
    """D-051 2026-07-31: a staged LEX item's prompt_path (filename embeds the
    title slug) is replaced with the fixed placeholder in the read-layer view --
    and stays TRUTHY so process_queue_action's staging idempotency holds."""
    rec = {"kind": "feature", "severity": "P3", "title": "lts scheduler tool",
           "summary": "generic", "entity": "LEX-LTS", "signal": "phrase",
           "representative": "x", "evidence": [], "reporter": "U1"}
    cid = cq._capture(dict(rec))
    cq._append_event({"event": "staged", "ts": cq._now_iso(), "id": cid,
                      "prompt_path": "_notes/cora-code-prompt-raw-title-slug.md"})
    item = cq.get_item(cid)
    assert item["prompt_path"] == cq._LEX_REDACTED_PROMPT_PATH
    assert "raw-title-slug" not in cq.render_backlog_text()


def test_explicit_lex_redacts_representative_and_evidence_at_rest(qenv):
    # 1i: the explicit write path builds rec with a RAW request in both the
    # representative AND the evidence note; _capture must redact BOTH for LEX before
    # anything is persisted (never a raw-LEX-at-rest window in the ledger).
    cid, outcome = cq.queue_explicit(
        "U9", "LEX-LTS", "C1", "cora should retrieve DDD service definitions", False)
    assert outcome in ("ok", "held") and cid
    item = cq.get_item(cid)
    assert item["representative"] == ""                 # redacted at rest
    assert item["evidence"][0].get("note") is None      # pointer-only (LEX)
    # The fingerprint ledger likewise stores no raw LEX representative.
    fps = cq._read_jsonl(cq._FINGERPRINT_LEDGER)
    assert all(f.get("representative") == "" for f in fps if f.get("id") == cid)


def test_seed_item_lex_evidence_pointer_only(qenv):
    # 1i fix: seed_item previously persisted summary[:200] raw as the evidence note.
    # For a LEX seed it must now be pointer-only + representative redacted (matches the
    # capture path). This is what makes the 1h LEX-DDD seed evidence-pointer-only.
    cid = cq.seed_item(
        kind="feature", severity="P2",
        title="LEX-LLC DDD service-definition retrieval (RSP/HAH/ATC)",
        summary="alias/glossary layer + verified re-chunk + Notion sync",
        entity="LEX", signal="explicit", status="APPROVED")
    item = cq.get_item(cid)
    assert item["status"] == "APPROVED"
    assert item["representative"] == ""
    assert item["evidence"][0].get("note") is None      # pointer-only, no raw text


def test_seed_item_phi_title_refused(qenv, monkeypatch):
    # D-051 fix: a PHI-tripping seed is REFUSED outright (mirrors _capture) -- the seed
    # title/summary persist raw + egress via code-session-backlog.md, so a PHI seed must
    # never be stored, not merely stored-with-scrubbed-evidence.
    monkeypatch.setattr(cq.phi_guard, "is_phi_risk_person_linked",
                        lambda t: True)
    cid = cq.seed_item(
        kind="bug", severity="P2", title="f3e widget", summary="contains phi text",
        entity="F3E", signal="explicit", status="APPROVED")
    assert cid is None
    assert cq.load_items() == []


def test_seed_item_phi_check_error_fails_closed(qenv, monkeypatch):
    def _boom(_t):
        raise RuntimeError("phi check exploded")
    monkeypatch.setattr(cq.phi_guard, "is_phi_risk_person_linked", _boom)
    assert cq.seed_item(kind="bug", severity="P2", title="x", summary="y",
                        entity="F3E", signal="explicit", status="APPROVED") is None


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
    cid, outcome = cq.queue_explicit("U0B2RM2JYJ1", "F3E", "C1", "add a TikTok voucher check", True)
    assert outcome == "ok"
    assert cq.get_item(cid)["status"] == "APPROVED"


def test_teammate_proposed(qenv):
    cid, outcome = cq.queue_explicit("U9", "F3E", "C1", "the tiktok digest misses vouchers", False)
    assert outcome == "ok"
    assert cq.get_item(cid)["status"] == "PROPOSED"


def test_explicit_throttle_teammate_over_cap_is_held_not_dropped(qenv):
    # A teammate is capped at EXPLICIT_THROTTLE_PER_DAY explicit files/day, but a
    # CONFIRMED ask over the cap must NEVER vanish (1g): it is captured with dm_held
    # (no immediate card) and surfaces via the overflow flush.
    distinct = [
        "add a tiktok voucher check to the digest",
        "fix the rangeme status refresh timing",
        "build an osn franchise thread pulse tool",
    ]
    assert len(distinct) == cq.EXPLICIT_THROTTLE_PER_DAY
    for req in distinct:
        cid, outcome = cq.queue_explicit("U9", "F3E", "C1", req, False)
        assert outcome == "ok" and cid
    over = "create a lex audit dashboard view"
    cid, outcome = cq.queue_explicit("U9", "F3E", "C1", over, False)
    assert outcome == "held"                      # over quota -> held, not dropped
    item = cq.get_item(cid)
    assert item is not None                        # the confirmed ask was NOT lost
    assert item["status"] == "PROPOSED"
    assert item.get("dm_held") is True
    # It is in the flushable set (surfaces on the next knowledge-review run).
    assert cid in {it["id"] for it in cq.load_items()
                   if it.get("dm_held") and not it.get("dm_flushed")}


def test_founder_is_throttle_exempt(qenv):
    # Harrison IS the approval gate -> never capped. Well past the cap, still APPROVED
    # + not held (1g). Distinct requests so fuzzy dedup doesn't collapse them.
    reqs = [
        "add a tiktok voucher check to the digest",
        "fix the rangeme status refresh timing",
        "build an osn franchise thread pulse tool",
        "wire a ddd service-definition retrieval alias layer",
        "add a cash-flow pulse export for hjrp leases",
    ]
    for req in reqs:
        cid, outcome = cq.queue_explicit("U0B2RM2JYJ1", "F3E", "C1", req, True)
        assert outcome == "ok" and cid
        item = cq.get_item(cid)
        assert item["status"] == "APPROVED"
        assert not item.get("dm_held")


def test_explicit_empty_and_dropped_outcomes(qenv):
    # Blank request -> empty; nothing filed.
    assert cq.queue_explicit("U9", "F3E", "C1", "   ", False) == (None, "empty")


def test_explicit_reask_after_dismiss_resurfaces(qenv):
    # D-051 finding A: a confirmed EXPLICIT ask must NEVER silently merge into a CLOSED
    # item (the finding-6 invariant). Re-asking an exact match of a DISMISSED item mints a
    # FRESH item instead of a dead-end recurrence.
    cid1, o1 = cq.queue_explicit("U0B2RM2JYJ1", "F3E", "C1",
                                 "build a tiktok voucher digest tool", True)
    assert o1 == "ok"
    assert cq.process_queue_action(cq.ACTION_DISMISS, cid1, "U0B2RM2JYJ1")[0] == "dismissed"
    assert cq.get_item(cid1)["status"] == "DISMISSED"
    cid2, o2 = cq.queue_explicit("U0B2RM2JYJ1", "F3E", "C1",
                                 "build a tiktok voucher digest tool", True)
    assert cid2 != cid1                            # not a recurrence onto the dismissed item
    assert cq.get_item(cid2)["status"] == "APPROVED"   # resurfaced, actionable


def test_non_explicit_still_dedups_onto_closed(qenv):
    # The A-fix is EXPLICIT-only: other signals keep dedup-onto-any so a resolved bug
    # recurrence doesn't spam a new item. A friction signal matching a dismissed item is a
    # recurrence (same id).
    fp = "vendor cox billing flood needs a make filter"
    id1 = cq._capture({"kind": "feature", "severity": "P3", "title": "cox flood",
                       "summary": "s", "entity": "F3E", "signal": "friction",
                       "representative": fp, "evidence": [], "reporter": "U1"})
    cq._append_event({"event": "dismissed", "ts": cq._now_iso(), "id": id1})
    id2 = cq._capture({"kind": "feature", "severity": "P3", "title": "cox flood",
                       "summary": "s", "entity": "F3E", "signal": "friction",
                       "representative": fp, "evidence": [], "reporter": "U1"})
    assert id2 == id1                              # recurrence onto the dismissed item (unchanged)


def test_explicit_over_cap_dedup_reports_ok_not_held(qenv):
    # D-051 finding C: an over-cap ask that dedups onto the asker's OWN still-open item is
    # a recurrence (rides the existing card) -> report "ok", NOT a false "held / digest"
    # promise (the item carries no dm_held flag and would never be flushed).
    first = "add a tiktok voucher check to the digest"
    for req in (first, "fix the rangeme status refresh timing", "build an osn thread pulse"):
        assert cq.queue_explicit("U9", "F3E", "C1", req, False)[1] == "ok"
    cid, outcome = cq.queue_explicit("U9", "F3E", "C1", first, False)   # over cap + exact dedup
    assert outcome == "ok"
    assert not cq.get_item(cid).get("dm_held")


def test_capture_dm_held_suppresses_card(qenv):
    # dm_held capture persists the item + hold but sends NO immediate DM card.
    fake = FakeClient()
    rec = {
        "kind": "feature", "severity": "P2", "title": "held item",
        "summary": "held item summary", "subsystem_guess": "", "entity": "F3E",
        "signal": "explicit", "representative": "held item summary",
        "evidence": [{"channel_id": "C1", "ts": "", "note": "held item summary"}],
        "reporter": "U9",
    }
    cid = cq._capture(dict(rec), initial_status="PROPOSED", dm_held=True,
                      client_factory=lambda: fake)
    assert cid and cq.get_item(cid).get("dm_held") is True
    assert fake.posts == []                        # no card while held
    # The overflow flush then delivers it as ONE summary DM.
    assert cq.maybe_flush_overflow(client_factory=lambda: fake) == 1
    assert len(fake.posts) == 1


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
    monkeypatch.setattr(cq.phi_guard, "is_phi_risk_person_linked",
                        lambda t: True)
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
# 2026-07-30 incident guard: an isolated (redirected) ledger must never render
# into the REAL, unredirected Founder-OS backlog file
# ─────────────────────────────────────────────────────────────────────────────
def test_backlog_leak_guard_refuses_when_ledger_redirected_but_target_real(
        tmp_path, monkeypatch):
    """The exact 2026-07-30 mismatch: _EVENT_LEDGER points at an isolated tmp
    ledger (as any test/sandbox does) but FOUNDER_OS_ROOT/backlog_path() is
    STILL the real, unredirected default -- render_backlog() must refuse the
    write outright rather than rendering an isolated ledger into the live
    Founder-OS Drive file (item cq-96fdd1850605, a LEX-redacted TEST title,
    rendered into the real G:\\...\\code-session-backlog.md this way). The write
    function itself is patched to RAISE if ever called, so a bug in the guard
    fails this test loudly instead of silently touching the real drive."""
    monkeypatch.setattr(cq, "_EVENT_LEDGER", tmp_path / "isolated-ledger.jsonl")
    monkeypatch.delenv("FOUNDER_OS_ROOT", raising=False)

    def _must_not_be_called(*a, **k):
        raise AssertionError(
            "drive_io.write_text_atomic must NEVER be called in the leak scenario")
    monkeypatch.setattr(cq.drive_io, "write_text_atomic", _must_not_be_called)

    assert cq._backlog_write_would_leak() is True
    assert cq.render_backlog() is False


def test_backlog_leak_guard_does_not_trip_when_fully_isolated(qenv):
    """A properly isolated caller (ledger AND FOUNDER_OS_ROOT both redirected --
    the qenv fixture's normal shape) must NOT trip the guard -- only the
    ledger-redirected-but-target-real MISMATCH does."""
    assert cq._backlog_write_would_leak() is False
    cq.seed_item(kind="bug", severity="P1", title="Beta", summary="s",
                 entity="F3E", signal="tool_error", status="APPROVED")
    assert cq.render_backlog() is True


def test_backlog_leak_guard_does_not_trip_when_ledger_not_redirected(monkeypatch):
    """If _EVENT_LEDGER is (explicitly, for this test) at its real default, the
    guard must not block a write purely because FOUNDER_OS_ROOT differs -- it
    only trips on the redirected-ledger + real-target MISMATCH, never on the
    target alone. (The suite's own autouse fixture normally redirects
    _EVENT_LEDGER for every test -- this test explicitly restores the real
    default to isolate the OTHER half of the guard's condition.)"""
    monkeypatch.setattr(cq, "_EVENT_LEDGER", cq._DEFAULT_EVENT_LEDGER)
    monkeypatch.delenv("FOUNDER_OS_ROOT", raising=False)
    assert cq._backlog_write_would_leak() is False


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


def test_approved_card_has_stage_button(qenv):
    cid = cq.seed_item(kind="feature", severity="P2", title="X", summary="s",
                       entity="F3E", signal="explicit", status="APPROVED")
    _text, blocks = cq.build_item_card(cq.get_item(cid))
    ids = [e["action_id"] for b in blocks if b.get("type") == "actions" for e in b["elements"]]
    assert cq.ACTION_STAGE in ids and cq.ACTION_APPROVE not in ids


# ─────────────────────────────────────────────────────────────────────────────
# Wiring: kb_exclusions allowlist
# ─────────────────────────────────────────────────────────────────────────────
def test_kb_exclusions_allowlist_backlog():
    from pathlib import Path

    from cora import kb_exclusions as ke
    p = Path(r"G:\My Drive\HJR-Founder-OS\_shared\projects\cora\code-session-backlog.md")
    assert ke.is_cora_internal_path(p) is False
    assert ke.is_cora_internal_title("code-session-backlog.md") is False
    assert ke.is_cora_internal_source_id(
        "_shared/projects/cora/code-session-backlog.md") is False
    # A sibling Cora build doc is STILL excluded (allowlist is exact-basename only).
    sib = Path(r"G:\My Drive\HJR-Founder-OS\_shared\projects\cora\x_cora-code-prompt-y.md")
    assert ke.is_cora_internal_path(sib) is True
    assert ke.is_cora_internal_title("x_cora-code-prompt-y.md") is True


# ─────────────────────────────────────────────────────────────────────────────
# Wiring: explicit tool via dispatch + exposure
# ─────────────────────────────────────────────────────────────────────────────
def test_explicit_tool_in_global_core():
    import cora.tools.tool_dispatch as td
    assert "cora_queue_code_session" in td._GLOBAL_CORE_TOOLS
    names = [t["name"] for t in td.tools_for_entity("F3C")]  # lean entity gets it
    assert "cora_queue_code_session" in names


def test_explicit_tool_via_dispatch(qenv, monkeypatch):
    import cora.tools.tool_dispatch as td
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    out = td.dispatch("cora_queue_code_session", {"request": "add a rangeme widget"},
                      "U0B2RM2JYJ1", entity="F3E", channel_id="C1")
    assert "confirmed" in out.lower() and cq.load_items() == []  # staged-write gate
    out2 = td.dispatch("cora_queue_code_session",
                       {"request": "add a rangeme widget", "confirmed": True},
                       "U0B2RM2JYJ1", entity="F3E", channel_id="C1")
    assert "WRITE_CONFIRMED" in out2
    items = cq.load_items()
    assert len(items) == 1 and items[0]["status"] == "APPROVED" and items[0]["signal"] == "explicit"


def test_explicit_tool_off_flag(qenv, monkeypatch):
    import cora.tools.tool_dispatch as td
    monkeypatch.setenv("CORA_CODE_QUEUE", "off")
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    out = td.dispatch("cora_queue_code_session",
                      {"request": "x", "confirmed": True}, "U0B2RM2JYJ1",
                      entity="F3E", channel_id="C1")
    assert "turned off" in out.lower() and cq.load_items() == []


# ─────────────────────────────────────────────────────────────────────────────
# Wiring: S1 dispatch capture (fail-soft, reply-inert)
# ─────────────────────────────────────────────────────────────────────────────
def _boom_tool(uid, entity, inp):
    raise ValueError("kaboom")


def test_dispatch_crash_captures_item(qenv, monkeypatch):
    import cora.tools.tool_dispatch as td
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    monkeypatch.setitem(td._TOOL_FUNCTIONS, "test_boom", _boom_tool)
    out = td.dispatch("test_boom", {}, "U1", entity="F3E", channel_id="C1")
    assert "crashed" in out.lower()
    items = cq.load_items()
    assert len(items) == 1 and items[0]["signal"] == "tool_error" and items[0]["severity"] == "P1"


def _raise_capture(*a, **k):
    raise RuntimeError("capture boom")


def test_dispatch_capture_raise_reply_unchanged(qenv, monkeypatch):
    import cora.tools.tool_dispatch as td
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    monkeypatch.setitem(td._TOOL_FUNCTIONS, "test_boom2", _boom_tool)
    monkeypatch.setattr(cq, "capture_tool_failure", _raise_capture)
    out = td.dispatch("test_boom2", {}, "U1", entity="F3E", channel_id="C1")
    assert "crashed" in out.lower()  # reply unchanged despite capture raising


# ─────────────────────────────────────────────────────────────────────────────
# Wiring: friction S6 cross-registration
# ─────────────────────────────────────────────────────────────────────────────
def test_friction_s6_registers_cora_tool(qenv, monkeypatch, tmp_path):
    from cora import friction_mining as fm
    monkeypatch.setattr(fm, "_backlog_path", lambda: tmp_path / "eff-backlog.md")
    ok, _ = fm.apply_efficiency({"title": "Build a BDM brand-voice check tool",
                                 "entity": "BDM", "recommendation": "extend it",
                                 "route": "cora_tool", "signal_type": "x",
                                 "frequency": "3", "evidence": []})
    assert ok
    items = cq.load_items()
    assert len(items) == 1 and items[0]["signal"] == "friction" and items[0]["status"] == "APPROVED"


def test_friction_s6_skips_make_com(qenv, monkeypatch, tmp_path):
    from cora import friction_mining as fm
    monkeypatch.setattr(fm, "_backlog_path", lambda: tmp_path / "eff-backlog2.md")
    fm.apply_efficiency({"title": "A Make.com scenario", "entity": "F3E",
                         "recommendation": "automate", "route": "make_com",
                         "signal_type": "x", "frequency": "1", "evidence": []})
    assert cq.load_items() == []


# ─────────────────────────────────────────────────────────────────────────────
# Wiring: seed script (dry-run default, idempotent)
# ─────────────────────────────────────────────────────────────────────────────
def _load_seed_module():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts" / "seed_code_queue.py"
    spec = importlib.util.spec_from_file_location("seed_code_queue_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_seed_script_dry_run_default(qenv, monkeypatch):
    mod = _load_seed_module()
    monkeypatch.setattr("sys.argv", ["seed_code_queue.py"])
    assert mod.main() == 0
    assert cq.load_items() == []  # dry-run writes nothing


def test_seed_script_apply_idempotent(qenv, monkeypatch):
    mod = _load_seed_module()
    monkeypatch.setattr("sys.argv", ["seed_code_queue.py", "--apply"])
    mod.main()
    items = cq.load_items()
    assert len(items) == len(mod.SEEDS)
    assert any(it["status"] == "SHIPPED" for it in items)   # #2 shopify
    assert any(it["kind"] == "config" for it in items)      # #3 known-answers
    assert any("phantom" in it["title"].lower() for it in items)  # seed #0
    mod.main()  # re-run
    assert len(cq.load_items()) == len(mod.SEEDS)           # idempotent


# ─────────────────────────────────────────────────────────────────────────────
# D-051 remediation regression tests (all 6 confirmed defects)
# ─────────────────────────────────────────────────────────────────────────────
def _racer_rec():
    return {"kind": "bug", "severity": "P2", "title": "racer", "summary": "s",
            "entity": "F3E", "signal": "tool_error", "representative": "racer_tool",
            "evidence": [], "reporter": "U1"}


def test_dedup_toctou_concurrent_single_item(qenv, monkeypatch):
    # Fix A: concurrent captures of the SAME signal produce exactly ONE item.
    import threading
    monkeypatch.setattr(cq, "_SYNC", False)          # real daemon threads
    monkeypatch.setenv("CORA_CODE_QUEUE", "log")     # no DM network
    threads = [threading.Thread(target=lambda: cq._capture(_racer_rec())) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    items = cq.load_items()
    assert len(items) == 1                            # no duplicate cards/records
    assert items[0]["count"] == 10                   # 1 captured + 9 recurrences


def test_dm_reservation_caps(qenv):
    # Fix B: the reservation guard hands out at most MAX_DM_PER_DAY slots.
    got = [cq._reserve_dm_slot() for _ in range(cq.MAX_DM_PER_DAY + 3)]
    assert got.count(True) == cq.MAX_DM_PER_DAY
    cq._release_dm_slot()                            # freeing one opens a slot
    assert cq._reserve_dm_slot() is True


def test_noise_lex_fingerprint_redacted(qenv, monkeypatch):
    # Fix F: a noise-classified LEX message never lands raw in the fingerprint ledger.
    monkeypatch.setattr(cq, "classify_candidate", lambda m, e: {"kind": "noise"})
    cq.capture_message_signal("cora should get an LTS coffee machine",
                              "LEX-LLC", "C1", "lex", "U1")
    fps = cq._read_jsonl(cq._FINGERPRINT_LEDGER)
    noise = [f for f in fps if f.get("id") == "noise"]
    assert noise and all(f.get("representative") == "" for f in noise)


def test_thumbsdown_lex_signals_hashed(qenv):
    # Fix E: LEX reply text is hashed (not stored raw) in the signals ledger.
    fake = FakeClient()
    fake.history_text = "client Jane Doe DDD authorization status pending"
    cq.capture_thumbsdown("C1", "1.0", "LEX", "U1", client=fake)
    sigs = [s for s in cq._read_jsonl(cq._SIGNALS_LEDGER) if s.get("signal") == "thumbsdown"]
    assert sigs and all(str(s.get("key", "")).startswith("h:") for s in sigs)
    assert all("Jane Doe" not in str(s.get("key", "")) for s in sigs)


def test_approve_stage_idempotent(qenv):
    # Fix D: re-approving a STAGED P1 item is a no-op (no second prompt).
    cid = cq.seed_item(kind="bug", severity="P1", title="crash hard", summary="s",
                       entity="F3E", signal="tool_error", status="PROPOSED")
    cq.process_queue_action(cq.ACTION_APPROVE, cid, "U0B2RM2JYJ1")
    p1 = cq.get_item(cid)["prompt_path"]
    assert cq.get_item(cid)["status"] == "STAGED" and p1
    o, _ = cq.process_queue_action(cq.ACTION_APPROVE, cid, "U0B2RM2JYJ1")
    assert o == "noop" and cq.get_item(cid)["prompt_path"] == p1


def test_stage_bundle_idempotent(qenv):
    # Fix C: re-staging a bundle is a no-op (menu button is re-tappable).
    ids = [cq.seed_item(kind="feature", severity="P3", title=f"B {i}", summary="s",
                        entity="F3E", signal="friction", status="APPROVED",
                        subsystem_guess="shopify") for i in range(2)]
    o, _ = cq.stage_bundle("bundle:" + ",".join(ids), "U0B2RM2JYJ1")
    assert o == "staged"
    o2, msg2 = cq.stage_bundle("bundle:" + ",".join(ids), "U0B2RM2JYJ1")
    assert o2 == "noop" and "already staged" in msg2.lower()


# ══════════════════════════════════════════════════════════════════════════════
# v1.1 hardening -- day-one field defects (2026-07-28)
# ══════════════════════════════════════════════════════════════════════════════

# ── Slice 1a: dedup v2 (normalize / classifier key / embedding paraphrase) ─────
def test_dedup_normalizes_cowork_footer(qenv):
    # Two identical asks differing ONLY by the Cowork "*Sent using* <@..>" footer merge.
    base = "cora should be able to check reprally order status for a customer"
    r1 = {"kind": "feature", "severity": "P3", "title": "reprally check", "summary": "",
          "entity": "F3E", "signal": "phrase", "representative": base,
          "evidence": [], "reporter": "U1"}
    r2 = dict(r1, representative=base + "\n\n*Sent using* <@U0B2RM2JYJ1>")
    id1 = cq._capture(dict(r1))
    id2 = cq._capture(dict(r2))
    assert id1 == id2 and cq.get_item(id1)["count"] == 2


def test_dedup_strips_inline_mentions(qenv):
    r1 = {"kind": "feature", "severity": "P3", "title": "t", "summary": "",
          "entity": "F3E", "signal": "phrase",
          "representative": "cora should ping <@U123> when a deal stalls",
          "evidence": [], "reporter": "U1"}
    r2 = dict(r1, representative="cora should ping <@U999> when a deal stalls")
    assert cq._capture(dict(r1)) == cq._capture(dict(r2))


def test_dedup_classifier_key_cross_signal(qenv):
    # Same (title, subsystem) from the classifier merges even across DIFFERENT signals
    # and different raw text (title-dedup + subsystem key).
    r1 = {"kind": "feature", "severity": "P2", "title": "Add RepRally order-status check",
          "summary": "", "entity": "F3E", "signal": "phrase", "subsystem_guess": "reprally",
          "representative": "can cora check reprally", "evidence": [], "reporter": "U1"}
    r2 = {"kind": "feature", "severity": "P2", "title": "Add RepRally order-status check",
          "summary": "", "entity": "F3E", "signal": "explicit", "subsystem_guess": "reprally",
          "representative": "reprally lookup please", "evidence": [], "reporter": "U2"}
    id1 = cq._capture(dict(r1))
    id2 = cq._capture(dict(r2))
    assert id1 == id2  # merged on classifier key despite different signal + text


def _reprally_embed(texts):
    # Fake embedder: anything mentioning reprally -> one direction, else another.
    return [[1.0, 0.0] if "reprally" in t.lower() else [0.0, 1.0] for t in texts]


def test_dedup_embedding_paraphrase_reprally(qenv, monkeypatch):
    # THE regression: two RepRally paraphrases that (a) are not exact, (b) share no
    # classifier key (no subsystem), and (c) fall below the fuzzy ratio, still MERGE
    # via the embedding layer. Reproduces the day-one double-file cq-5f48.. / cq-3c26...
    monkeypatch.setattr(cq, "_default_embed", _reprally_embed)
    r1 = {"kind": "feature", "severity": "P2",
          "title": "Check RepRally order status", "summary": "",
          "entity": "F3E", "signal": "phrase",
          "representative": "Cora should be able to check RepRally order status for a customer",
          "evidence": [], "reporter": "U1"}
    r2 = {"kind": "feature", "severity": "P2",
          "title": "Where is my RepRally shipment", "summary": "",
          "entity": "F3E", "signal": "phrase",
          "representative": "look up whether a RepRally shipment has actually gone out yet",
          "evidence": [], "reporter": "U2"}
    # Guard the isolation: the deterministic layers must MISS so only embedding merges.
    assert cq.find_fingerprint("phrase", r2["representative"],
                               class_key=cq._class_key(r2["title"], "")) is None
    id1 = cq._capture(dict(r1))
    id2 = cq._capture(dict(r2))
    assert id1 == id2 and cq.get_item(id1)["count"] == 2


def test_embedding_dedup_failsoft(qenv, monkeypatch):
    def _boom(_texts):
        raise RuntimeError("embed exploded")
    monkeypatch.setattr(cq, "_default_embed", _boom)
    r1 = {"kind": "feature", "severity": "P2", "title": "a", "summary": "",
          "entity": "F3E", "signal": "phrase",
          "representative": "cora should build a reprally order tracker widget",
          "evidence": [], "reporter": "U1"}
    r2 = dict(r1, title="b", representative="a tool to see reprally shipment progress live")
    id1 = cq._capture(dict(r1))
    id2 = cq._capture(dict(r2))
    assert id1 != id2  # embedding failed -> no semantic merge, but NO crash


def test_embedding_never_called_for_lex(qenv, monkeypatch):
    calls = {"n": 0}

    def _counting(texts):
        calls["n"] += 1
        return [[1.0, 0.0] for _ in texts]
    monkeypatch.setattr(cq, "_default_embed", _counting)
    for i in range(2):
        cq._capture({"kind": "feature", "severity": "P3", "title": f"lts thing {i}",
                     "summary": "generic", "entity": "LEX-LTS", "signal": "phrase",
                     "representative": f"cora should add an LTS scheduler variant {i}",
                     "evidence": [], "reporter": "U1"})
    assert calls["n"] == 0  # LEX text never embedded (egress guard)


# ── Slice 1b: confirm gate (F-23 parity) via dispatch ──────────────────────────
def _dispatch_cq(monkeypatch, inp, user="U0B2RM2JYJ1", channel_id="C1"):
    import cora.tools.tool_dispatch as td
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    return td.dispatch("cora_queue_code_session", inp, user, entity="F3E", channel_id=channel_id)


def test_cq_confirm_requires_stash(qenv, monkeypatch):
    # A confirmed=True with NO prior unconfirmed call files NOTHING (re-previews).
    out = _dispatch_cq(monkeypatch, {"request": "add a reprally widget", "confirmed": True})
    assert "WRITE_BLOCKED" in out and cq.load_items() == []


def test_cq_confirm_executes_stashed_not_echo(qenv, monkeypatch):
    # Preview stashes req_A; the confirm carries a PARAPHRASE (req_B) -- the STASHED
    # req_A is filed, never the echo.
    _dispatch_cq(monkeypatch, {"request": "Cora should check RepRally order status"})
    assert cq.load_items() == []  # preview files nothing
    out2 = _dispatch_cq(monkeypatch, {"request": "do the reprally thing", "confirmed": True})
    assert "WRITE_CONFIRMED" in out2
    items = cq.load_items()
    assert len(items) == 1
    assert "check RepRally order status" in items[0]["title"]  # stashed text, not the echo


def test_cq_confirm_race_no_duplicate(qenv, monkeypatch):
    # Reproduce the 07:21:53 race: a premature confirmed=True (paraphrase) with no stash
    # -> no item; then the proper preview+confirm -> exactly ONE item.
    _dispatch_cq(monkeypatch, {"request": "check reprally status now", "confirmed": True})
    assert cq.load_items() == []                                   # race confirm filed nothing
    _dispatch_cq(monkeypatch, {"request": "Cora should check RepRally order status"})
    _dispatch_cq(monkeypatch, {"request": "yep", "confirmed": True})
    assert len(cq.load_items()) == 1                               # exactly one item


# ── Slice 1c: stage idempotency under a concurrent double-tap ──────────────────
def _slow_counting_gen(counter):
    import time as _t

    def _g(items, *, slug=None, meta_out=None):
        counter["n"] += 1
        _t.sleep(0.25)  # widen the TOCTOU window
        if meta_out is not None:
            meta_out["mis_homed"] = False
        return f"/tmp/prompt-{counter['n']}.md"
    return _g


def test_stage_single_concurrent_double_tap(qenv, monkeypatch):
    import threading
    calls = {"n": 0}
    monkeypatch.setattr(cq, "generate_kickoff_prompt", _slow_counting_gen(calls))
    cid = cq.seed_item(kind="feature", severity="P2", title="X", summary="s",
                       entity="F3E", signal="explicit", status="APPROVED")
    threads = [threading.Thread(target=lambda: cq.process_queue_action(cq.ACTION_STAGE, cid, "U0B2RM2JYJ1"))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert calls["n"] == 1  # exactly one generator call despite two taps
    staged = [e for e in cq._read_jsonl(cq._EVENT_LEDGER)
              if e.get("event") == "staged" and e.get("id") == cid]
    assert len(staged) == 1
    assert cq.get_item(cid)["status"] == "STAGED"


def test_stage_bundle_concurrent_double_tap(qenv, monkeypatch):
    import threading
    calls = {"n": 0}
    monkeypatch.setattr(cq, "generate_kickoff_prompt", _slow_counting_gen(calls))
    ids = [cq.seed_item(kind="feature", severity="P3", title=f"B {i}", summary="s",
                        entity="F3E", signal="friction", status="APPROVED",
                        subsystem_guess="shopify") for i in range(3)]
    val = "bundle:" + ",".join(ids)
    threads = [threading.Thread(target=lambda: cq.stage_bundle(val, "U0B2RM2JYJ1"))
               for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert calls["n"] == 1
    for i in ids:
        assert cq.get_item(i)["status"] == "STAGED"


# ── Slice 1d: output dir + slug id-suffix + mis-homed fallback ─────────────────
def test_prompt_writes_to_founder_os(qenv):
    cid = cq.seed_item(kind="bug", severity="P1", title="Founder OS home test",
                       summary="s", entity="FNDR", signal="explicit", status="APPROVED")
    path = cq.generate_kickoff_prompt([cq.get_item(cid)])
    assert path is not None
    from pathlib import Path
    norm = str(Path(path)).replace("\\", "/")
    assert "/_shared/projects/cora/_notes/" in norm  # Founder-OS, not repo _notes


def test_prompt_slug_id_suffix_no_collision(qenv):
    # Two DIFFERENT items whose long titles SLUGIFY to the same 48-char prefix (the
    # day-one env-flag-pair clobber) get DISTINCT filenames via the id suffix. Distinct
    # signals keep them distinct items (no fingerprint/fuzzy dedup).
    shared = "the env flag docstring claims read per call needs no restart"
    id1 = cq.seed_item(kind="feature", severity="P2", title=shared + " alpha", summary="a",
                       entity="F3E", signal="phrase", status="APPROVED")
    id2 = cq.seed_item(kind="feature", severity="P2", title=shared + " beta", summary="b",
                       entity="F3E", signal="explicit", status="APPROVED")
    assert id1 != id2  # genuinely two items
    from pathlib import Path
    p1 = Path(cq.generate_kickoff_prompt([cq.get_item(id1)])).name
    p2 = Path(cq.generate_kickoff_prompt([cq.get_item(id2)])).name
    assert cq._slug(shared + " alpha") == cq._slug(shared + " beta")  # slugs DO collide
    assert p1 != p2  # ...but filenames don't (id suffix)
    import re
    assert re.search(r"-[0-9a-f]{6}\.md$", p1) and re.search(r"-[0-9a-f]{6}\.md$", p2)


def test_prompt_mis_homed_on_drive_unavailable(qenv, monkeypatch):
    def _raise(*a, **k):
        raise cq.drive_io.DriveUnavailable("mount gone")
    monkeypatch.setattr(cq.drive_io, "write_text_atomic", _raise)
    cid = cq.seed_item(kind="bug", severity="P2", title="mis-home me", summary="s",
                       entity="F3E", signal="explicit", status="APPROVED")
    outcome, _msg = cq.process_queue_action(cq.ACTION_STAGE, cid, "U0B2RM2JYJ1")
    assert outcome == "staged"
    it = cq.get_item(cid)
    from pathlib import Path
    assert Path(it["prompt_path"]).exists()                      # fell back to repo _notes
    staged = [e for e in cq._read_jsonl(cq._EVENT_LEDGER)
              if e.get("event") == "staged" and e.get("id") == cid]
    assert staged and staged[-1].get("mis_homed") is True         # flagged on the ledger


# ── Slice 1e: bundle grouping (<=4, affinity-only, no kitchen sink, theme slug) ─
def test_bundle_max_four(qenv):
    for i in range(6):
        cq.seed_item(kind="feature", severity="P3", title=f"shop {i}", summary="s",
                     entity="F3E", signal="friction", status="APPROVED", subsystem_guess="shopify")
    _text, blocks = cq.build_weekly_menu()
    bundle_vals = [e["value"] for b in blocks if b.get("type") == "actions"
                   for e in b["elements"] if str(e.get("value", "")).startswith("bundle:")]
    assert len(bundle_vals) == 2  # 6 items -> chunks of 4 + 2
    sizes = sorted(len(v[len("bundle:"):].split(",")) for v in bundle_vals)
    assert sizes == [2, 4] and all(s <= 4 for s in sizes)


def test_bundle_affinity_no_kitchen_sink(qenv):
    for sub in ["ar", "bookings", "dna", "brandvoice", "flywheel"]:
        cq.seed_item(kind="feature", severity="P3", title=f"{sub} thing", summary="s",
                     entity="F3E", signal="friction", status="APPROVED", subsystem_guess=sub)
    text, blocks = cq.build_weekly_menu()
    assert "other" not in text.lower()  # NO kitchen-sink merge
    single_btns = [e for b in blocks if b.get("type") == "actions"
                   for e in b["elements"]
                   if e.get("action_id") == cq.ACTION_STAGE
                   and not str(e.get("value", "")).startswith("bundle:")]
    assert len(single_btns) == 5  # five distinct subsystems -> five singletons


def test_bundle_slug_from_theme(qenv):
    ids = [cq.seed_item(kind="feature", severity="P3", title=t, summary="s",
                        entity="F3E", signal="friction", status="APPROVED",
                        subsystem_guess="reprally")
           for t in ("Alpha widget", "Beta widget")]
    _o, msg = cq.stage_bundle("bundle:" + ",".join(ids), "U0B2RM2JYJ1")
    it = cq.get_item(ids[0])
    from pathlib import Path
    name = Path(it["prompt_path"]).name
    assert "reprally" in name and "alpha" not in name.lower()  # theme slug, not item #1


# ── Slice 1f: supersede + cleanup script ───────────────────────────────────────
def test_supersede_item_merges(qenv):
    winner = cq.seed_item(kind="feature", severity="P2", title="RepRally check A", summary="s",
                          entity="F3E", signal="explicit", status="APPROVED")
    loser = cq.seed_item(kind="feature", severity="P2", title="RepRally check B", summary="s",
                         entity="F3E", signal="phrase", status="PROPOSED")
    assert cq.supersede_item(loser, winner) is True
    assert cq.get_item(loser)["status"] == "SUPERSEDED"
    assert cq.get_item(loser)["superseded_by"] == winner
    assert cq.get_item(winner)["count"] == 2          # recurrence bumped
    assert cq.supersede_item(loser, winner) is False  # idempotent (already superseded)
    assert cq.supersede_item("cq-missing", winner) is False  # missing id -> no-op


def _load_script(name):
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace(".", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cleanup_script_dry_run_then_apply(qenv, monkeypatch):
    mod = _load_script("cleanup_code_queue_dupes_2026-07-28.py")
    w1 = cq.seed_item(kind="feature", severity="P2", title="Win 1", summary="s",
                      entity="F3E", signal="explicit", status="APPROVED")
    l1 = cq.seed_item(kind="feature", severity="P2", title="Lose 1", summary="s",
                      entity="F3E", signal="phrase", status="PROPOSED")
    ship = cq.seed_item(kind="feature", severity="P2", title="Ship me", summary="s",
                        entity="F3E", signal="explicit", status="APPROVED")
    monkeypatch.setattr(mod, "MERGES", [(l1, w1)])
    monkeypatch.setattr(mod, "SHIPS", [ship, "cq-does-not-exist"])
    # dry-run: nothing changes
    monkeypatch.setattr("sys.argv", ["cleanup"])
    assert mod.main() == 0
    assert cq.get_item(l1)["status"] == "PROPOSED"
    assert cq.get_item(ship)["status"] == "APPROVED"
    # apply: merged + shipped (missing SHIP id is skipped gracefully)
    monkeypatch.setattr("sys.argv", ["cleanup", "--apply"])
    assert mod.main() == 0
    assert cq.get_item(l1)["status"] == "SUPERSEDED"
    assert cq.get_item(w1)["count"] == 2
    assert cq.get_item(ship)["status"] == "SHIPPED"


# ── Slice 1d: re-home planner/applier + migration script ───────────────────────
def _seed_mishomed(qenv, name="2026-07-28_fndr_cora-code-prompt-x-abc123.md"):
    """Create a mis-homed prompt file under the repo _notes + a staged event for it."""
    cid = cq.seed_item(kind="bug", severity="P1", title="mishomed", summary="s",
                       entity="FNDR", signal="explicit", status="APPROVED")
    cq._NOTES_DIR.mkdir(parents=True, exist_ok=True)
    src = cq._NOTES_DIR / name
    src.write_text("# a mis-homed prompt\n", encoding="utf-8")
    cq._append_event({"event": "staged", "ts": cq._now_iso(), "id": cid,
                      "prompt_path": str(src)})
    return cid, src


def test_rehome_plan_conservative(qenv):
    cid, src = _seed_mishomed(qenv)
    plan = cq.plan_prompt_rehome()
    assert len(plan) == 1 and plan[0]["id"] == cid
    # A NON-cora-code-prompt file referenced by a staged event is NOT touched.
    other = cq._NOTES_DIR / "unrelated-note.md"
    other.write_text("x", encoding="utf-8")
    cid2 = cq.seed_item(kind="bug", severity="P2", title="other", summary="s",
                        entity="F3E", signal="tool_error", status="APPROVED")
    cq._append_event({"event": "staged", "ts": cq._now_iso(), "id": cid2,
                      "prompt_path": str(other)})
    ids = {p["id"] for p in cq.plan_prompt_rehome()}
    assert cid in ids and cid2 not in ids  # over-deletion guard: basename gate


def test_rehome_apply_moves_and_backfills(qenv):
    cid, src = _seed_mishomed(qenv)
    plan = cq.plan_prompt_rehome()
    done = cq.apply_prompt_rehome(plan)
    from pathlib import Path
    assert done and done[0]["ok"] is True
    assert not src.exists()                                   # repo copy deleted
    new_path = cq.get_item(cid)["prompt_path"]
    norm = str(Path(new_path)).replace("\\", "/")
    assert "/_shared/projects/cora/_notes/" in norm and Path(new_path).exists()
    staged = [e for e in cq._read_jsonl(cq._EVENT_LEDGER)
              if e.get("event") == "staged" and e.get("id") == cid]
    assert staged[-1].get("rehomed") is True


def test_rehome_script_dry_run_no_delete(qenv, monkeypatch):
    mod = _load_script("rehome_code_queue_prompts_2026-07-28.py")
    _cid, src = _seed_mishomed(qenv)
    monkeypatch.setattr("sys.argv", ["rehome"])
    assert mod.main() == 0
    assert src.exists()  # dry-run never deletes


def test_rehome_shared_file_moves_once_backfills_all_rows(qenv):
    """2026-07-31 defect: a bundle stages ONE prompt file for N rows. The old
    per-row copy-then-unlink deleted the shared file on row 1 and
    FileNotFoundError'd rows 2..N (the 5 stale bnd-1fbf2c12 rows)."""
    from pathlib import Path
    cid1, src = _seed_mishomed(qenv, name="2026-07-28_fndr_cora-code-prompt-bundle-a1b2c3.md")
    cid2 = cq.seed_item(kind="feature", severity="P3", title="bundle sibling",
                        summary="s", entity="HJRP", signal="explicit",
                        status="APPROVED")
    cq._append_event({"event": "staged", "ts": cq._now_iso(), "id": cid2,
                      "prompt_path": str(src)})
    plan = cq.plan_prompt_rehome()
    assert {p["id"] for p in plan} == {cid1, cid2}
    done = cq.apply_prompt_rehome(plan)
    assert all(d["ok"] for d in done), done
    assert not src.exists()  # moved exactly once
    for cid in (cid1, cid2):
        new_path = cq.get_item(cid)["prompt_path"]
        assert "/_shared/projects/cora/_notes/" in str(Path(new_path)).replace("\\", "/")
        assert Path(new_path).exists()
        staged = [e for e in cq._read_jsonl(cq._EVENT_LEDGER)
                  if e.get("event") == "staged" and e.get("id") == cid]
        assert staged[-1].get("rehomed") is True


def test_rehome_self_heals_missing_src_when_dst_exists(qenv):
    """The live-damage shape: the shared file was already moved by a pre-fix
    run, so src is gone but the Founder-OS copy exists -- the row gets a
    backfill-only entry (no file ops) and its ledger pointer is fixed."""
    from pathlib import Path
    cid, src = _seed_mishomed(qenv, name="2026-07-28_fndr_cora-code-prompt-healme-d4e5f6.md")
    dst = cq.founder_os_notes_dir() / src.name
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    src.unlink()  # simulate the pre-fix applier having moved-and-deleted it
    plan = cq.plan_prompt_rehome()
    assert len(plan) == 1 and plan[0]["id"] == cid and plan[0].get("backfill_only")
    done = cq.apply_prompt_rehome(plan)
    assert done[0]["ok"] is True
    assert Path(cq.get_item(cid)["prompt_path"]) == dst
    assert dst.exists()


def test_rehome_never_plans_non_staged_rows(qenv):
    """Appending a 'staged' backfill event to a terminal row would RESURRECT it
    via the last-write-wins fold -- the plan must exclude non-STAGED rows (the
    cq-dad80c0011c9 trap)."""
    cid, src = _seed_mishomed(qenv, name="2026-07-28_fndr_cora-code-prompt-term-778899.md")
    winner = cq.seed_item(kind="bug", severity="P2", title="winner", summary="s",
                          entity="FNDR", signal="explicit", status="APPROVED")
    assert cq.supersede_item(cid, winner)
    assert cq.get_item(cid)["status"] == "SUPERSEDED"
    plan = cq.plan_prompt_rehome()
    assert cid not in {p["id"] for p in plan}
    # And even after an apply of the (empty) plan the row stays SUPERSEDED.
    cq.apply_prompt_rehome(plan)
    assert cq.get_item(cid)["status"] == "SUPERSEDED"


# ── D-051 v1.1-review remediation (2 confirmed MEDIUM) ─────────────────────────
def test_seed_item_redacts_lex_representative(qenv):
    # Defect A root cause: seed_item must not persist a raw LEX representative (else it
    # becomes an embedding candidate that egresses to OpenAI).
    cid = cq.seed_item(kind="feature", severity="P3", title="lex_lbhs_ar_aging tool",
                       summary="s", entity="LEX", signal="friction", status="APPROVED")
    assert cq.get_item(cid)["representative"] == ""
    fps = cq._read_jsonl(cq._FINGERPRINT_LEDGER)
    assert all(f.get("representative") == "" for f in fps if f.get("id") == cid)


def test_embedding_never_egresses_lex_candidate(qenv, monkeypatch):
    # Defect A defense-in-depth: even a LEX candidate with a RAW stored rep (legacy row)
    # is never handed to the embedder.
    seen = {"texts": []}

    def _spy(texts):
        seen["texts"].extend(texts)
        return [[0.0, 1.0] for _ in texts]
    monkeypatch.setattr(cq, "_default_embed", _spy)
    # Force a raw LEX rep into the ledger (simulate a legacy/pre-fix row).
    cq._append_event({"event": "captured", "id": "cq-legacylex", "ts": cq._now_iso(),
                      "status": "APPROVED", "count": 1, "entity": "LEX-LLC",
                      "signal": "phrase", "kind": "feature", "severity": "P3",
                      "title": "raw", "summary": "",
                      "representative": "client Jane Doe DDD authorization tracker request"})
    # A non-LEX capture triggers the embedding candidate scan.
    cq._capture({"kind": "feature", "severity": "P3", "title": "osn thing", "summary": "",
                 "entity": "OSN", "signal": "phrase",
                 "representative": "cora should build an osn franchise pulse tool",
                 "evidence": [], "reporter": "U1"})
    assert all("Jane Doe" not in t for t in seen["texts"])  # LEX rep never embedded


def test_dedup_inline_sent_using_not_merged(qenv):
    # Defect B: an inline "sent using X" (no trailing mention) is NOT a footer and must
    # NOT be stripped -- two distinct asks sharing a prefix stay distinct.
    r1 = {"kind": "feature", "severity": "P3", "title": "auto-file receipts amex",
          "summary": "", "entity": "F3E", "signal": "phrase",
          "representative": "cora should auto-file receipts sent using amex card",
          "evidence": [], "reporter": "U1"}
    r2 = {"kind": "feature", "severity": "P3", "title": "auto-file receipts chase",
          "summary": "", "entity": "F3E", "signal": "phrase",
          "representative": "cora should auto-file receipts sent using the chase portal every monday",
          "evidence": [], "reporter": "U1"}
    assert cq._normalize(r1["representative"]) != cq._normalize(r2["representative"])
    id1 = cq._capture(dict(r1))
    id2 = cq._capture(dict(r2))
    assert id1 != id2  # distinct items -- inline "sent using" not over-stripped


def test_footer_strip_still_works_after_anchor(qenv):
    # The genuine Cowork footer (trailing <@..> mention) is STILL stripped post-fix.
    base = "cora should ship a new osn pulse view"
    assert cq._normalize(base + "\n\n*Sent using* <@U0B2RM2JYJ1>") == cq._normalize(base)


# ─────────────────────────────────────────────────────────────────────────────
# Tool-timeout pin (cq-7fb82054ee4a residual)
# ─────────────────────────────────────────────────────────────────────────────
def test_queue_tool_timeout_exceeds_inline_drive_write_budget():
    """cq-7fb82054ee4a: the confirmed explicit path runs an OpenAI embed, a G:
    backlog render (drive_io default timeout 10s), and 2 Slack DM calls INLINE --
    the old 8s tool budget was smaller than a single slow Drive write, yielding
    'Tool timed out' while the abandoned worker usually still filed the item
    (filed-but-reported-failed). The budget must exceed drive_io's per-attempt
    timeout with headroom."""
    from cora import drive_io
    from cora.tools import tool_dispatch as td
    assert td._TOOL_TIMEOUTS["cora_queue_code_session"] >= drive_io.TIMEOUT_SECONDS + 5
