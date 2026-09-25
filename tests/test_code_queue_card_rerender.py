"""Code #15 S5 (cq-2d26f131091e) -- a press re-renders ONLY the pressed row.

9/21: the Monday menu went out as ONE DM message with 21 actions rows (6 APPROVED
Stage rows, 7 PROPOSED Queue rows, 8 stale STAGED Keep rows). Harrison pressed 29
times; every press executed and was THREADED, the message was never edited, and all
21 rows kept live buttons although 20 were decided. The antecedent is D-051 lesson
48 / f2f9733 (8/24 C4: the knowledge-review emoji path never called chat_update).

Contract under test:
  * code_queue.rerender_card_blocks is PURE and keyed on block_id + button values;
    "decided" = R14-9's _QS_DECISION_EVENTS/_first_decision_after bound at the card
    ts (the card and cora_queue_status agree by construction); a resolved row's
    actions block becomes ONE context block (cq_done_...) with a static label + AZ
    time and no title / prompt path / reason; every other block is untouched;
  * app._cq_ack_in_message re-renders under _CQ_CARD_RENDER_LOCK (held across the
    ledger read + chat_update only) and STILL threads the outcome; the menu's own
    fallback text is preserved; a single capture card is consumed as before;
  * Park / Dismiss-w/-note modal submits fetch the card by its EXACT ts first;
  * a repeat Keep on the same card is a no-op (per-card idempotency).
The menus here are built by the REAL build_weekly_menu and posted by the REAL
maybe_send_weekly_menu -- block_ids and button values come from the builder.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

import cora.app as app_module
from cora import code_queue as cq

HARRISON = "U0B2RM2JYJ1"
MENU_921_TS = "1789999254.143349"          # the real 9/21 menu message ts (2026-09-21T14:00:54Z)
LEX_RAW_TITLE = "Quibblewick footer rebuild"   # distinctive: a raw-title leak would show
LEX_RAW_PATH_TOKEN = "quibblewick-footer"

# The 9/21 menu's 21 carded rows (ids only, from data/state/code-queue-menu-runs.jsonl).
APPROVED_921 = ["cq-4f3fcc9b7096", "cq-be90cea867c3", "cq-74e6b20d5d3d", "cq-b93e2abf2922",
                "cq-e9ef3f581d60"]
FLOOR_HELD = "cq-f880ce946bb6"            # APPROVED, no evidence -> the Stage press is refused
HIGH_921 = ["cq-90568f0b1222", "cq-d9d0c92cc797", "cq-22b84598aee8", "cq-5f44ce934aeb",
            "cq-17fe76f5ab91"]            # PROPOSED HIGH -> Queue auto-stages (approve_auto)
AGED_921 = ["cq-0e9971a5d047", "cq-505a37b1c4b7"]   # PROPOSED P3, aged >= 14d
STALE_921 = ["cq-ad0e4becafce", "cq-7ebfc2f9c014", "cq-4d5c88973e7f", "cq-f3bfa4e9ca5b",
             "cq-4e03929147dd", "cq-23824a6bc4a0", "cq-cfb701db248b", "cq-3442ddffdea7"]

# The 29 presses, in order, from the bot log (cora-2026-09-19.log.2026-09-21 l.536-604):
# (action, id, outcome). Press 1 is the in-flight double-tap; 14-20 the re-presses.
S, Q, K = cq.ACTION_STAGE, cq.ACTION_APPROVE, cq.ACTION_KEEP
REPLAY_921 = [
    (S, "cq-4f3fcc9b7096", "noop"), (S, "cq-4f3fcc9b7096", "staged"),
    (S, "cq-be90cea867c3", "staged"), (S, "cq-74e6b20d5d3d", "staged"),
    (S, FLOOR_HELD, "no_evidence"), (S, "cq-b93e2abf2922", "staged"),
    (S, "cq-e9ef3f581d60", "staged"),
    (Q, "cq-90568f0b1222", "approved"), (Q, "cq-0e9971a5d047", "approved"),
    (Q, "cq-d9d0c92cc797", "approved"), (Q, "cq-22b84598aee8", "approved"),
    (Q, "cq-5f44ce934aeb", "approved"), (Q, "cq-17fe76f5ab91", "approved"),
    (S, "cq-4f3fcc9b7096", "noop"),
    (Q, "cq-90568f0b1222", "noop"), (Q, "cq-d9d0c92cc797", "noop"),
    (Q, "cq-22b84598aee8", "noop"), (Q, "cq-5f44ce934aeb", "noop"),
    (Q, "cq-17fe76f5ab91", "noop"), (Q, "cq-0e9971a5d047", "noop"),
    (Q, "cq-505a37b1c4b7", "approved"),
] + [(K, cid, "kept") for cid in STALE_921]


# ── fixtures ─────────────────────────────────────────────────────────────────

class FakeClient:
    """Records every Slack call. chat_postMessage without thread_ts is the menu
    post (returns ``menu_ts``); chat_update can be slowed; conversations_history
    returns whatever ``history`` yields."""

    def __init__(self, menu_ts: str | None = None):
        self.menu_ts = menu_ts
        self.posts: list[dict] = []
        self.updates: list[dict] = []
        self.history_calls: list[dict] = []
        self.history = None
        self._lock = threading.Lock()

    def conversations_open(self, users=None):
        return {"channel": {"id": "D123"}}

    def chat_postMessage(self, **kw):
        with self._lock:
            self.posts.append(copy.deepcopy(kw))
        if kw.get("thread_ts"):
            return {"ts": "1.1"}
        return {"ts": self.menu_ts or f"{time.time() - 1:.6f}"}

    def chat_update(self, **kw):
        with self._lock:
            self.updates.append(copy.deepcopy(kw))
        return {"ok": True}

    def conversations_history(self, **kw):
        self.history_calls.append(dict(kw))
        return self.history(**kw) if callable(self.history) else (self.history or {"messages": []})

    def chat_postEphemeral(self, **kw):
        self.posts.append({"ephemeral": True, **kw})

    @property
    def threads(self):
        return [p for p in self.posts if p.get("thread_ts")]


@pytest.fixture
def qenv(tmp_path, monkeypatch):
    monkeypatch.setattr(cq, "_EVENT_LEDGER", tmp_path / "code-session-queue.jsonl")
    monkeypatch.setattr(cq, "_FINGERPRINT_LEDGER", tmp_path / "fp.jsonl")
    monkeypatch.setattr(cq, "_SIGNALS_LEDGER", tmp_path / "sig.jsonl")
    monkeypatch.setattr(cq, "_MENU_RUNS_LEDGER", tmp_path / "menu-runs.jsonl")
    monkeypatch.setattr(cq, "_NOTES_DIR", tmp_path / "_notes")
    monkeypatch.setenv("FOUNDER_OS_ROOT", str(tmp_path / "founder-os"))
    monkeypatch.setenv("CORA_CODE_QUEUE", "live")
    monkeypatch.setattr(cq, "HARRISON_ID", HARRISON)
    monkeypatch.setattr(cq, "_SYNC", True)
    monkeypatch.setattr(cq.drive_io, "write_text_atomic", lambda p, t, **k: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setattr(cq, "_default_embed", lambda texts: [])
    cq._STAGING_INFLIGHT.clear()

    def _gen(items, slug=None, meta_out=None, **_kw):
        # the path carries a LEX-looking token so a leaked path is detectable
        return f"C:/notes/{LEX_RAW_PATH_TOKEN}-{items[0]['id']}.md"
    monkeypatch.setattr(cq, "generate_kickoff_prompt", _gen)
    yield tmp_path
    cq._STAGING_INFLIGHT.clear()


def _iso(days_ago: float = 0.0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _cap(cid, *, status="PROPOSED", severity="P2", entity="F3E", signal="explicit",
         summary="an explicit ask", title=None, sub=None, ts=None, evidence=None):
    return {"event": "captured", "id": cid, "ts": ts or _iso(1), "status": status,
            "title": title or f"fixture {cid}", "summary": summary, "entity": entity,
            "signal": signal, "kind": "bug", "severity": severity,
            "subsystem_guess": sub if sub is not None else f"sub-{cid}",
            "evidence": evidence or []}


def _write(rows):
    with cq._EVENT_LEDGER.open("a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _seed_921():
    """The live 9/21 shape: 6 APPROVED singles (one floor-held), 7 PROPOSED (5 HIGH +
    2 aged), 8 stale STAGED -- exactly the 21 action rows the allocator carded."""
    rows = [_cap(c, status="APPROVED") for c in APPROVED_921]
    rows.append(_cap(FLOOR_HELD, status="APPROVED", signal="passive", summary=""))
    rows += [_cap(c, severity="HIGH") for c in HIGH_921]
    rows += [_cap(c, severity="P3", ts=_iso(20)) for c in AGED_921]
    for c in STALE_921:
        rows.append(_cap(c, status="APPROVED", ts=_iso(30)))
        rows.append({"event": "staged", "id": c, "ts": _iso(20), "prompt_path": f"/p/{c}.md",
                     "bundle_id": ""})
    _write(rows)


def _post_menu(client):
    assert cq.maybe_send_weekly_menu(client_factory=lambda: client) is True
    post = client.posts[0]
    return post["blocks"], post["text"], cq.latest_menu_run()["message_ts"]


def _row(blocks, cid):
    """The row block for one id (actions while live, context once resolved)."""
    for b in blocks:
        bid = str(b.get("block_id") or "")
        if bid.endswith("_" + cid) and (bid.startswith(cq._CARD_ROW_PREFIXES)
                                        or bid.startswith(cq._CARD_DONE_PREFIX)):
            return b
    raise AssertionError(f"no row for {cid}")


def _row_text(blocks, cid):
    return _row(blocks, cid)["elements"][0]["text"]


def _resolved_ids(blocks):
    out = []
    for b in blocks:
        m = cq._CARD_DONE_ID_RE.match(str(b.get("block_id") or ""))
        if m and b.get("type") == "context":
            out.append(m.group(1))
    return out


def _body(blocks, text, ts, value, action_id=None):
    return {"actions": [{"value": value, "action_id": action_id or ""}],
            "user": {"id": HARRISON}, "channel": {"id": "D123"},
            "message": {"ts": ts, "text": text, "blocks": copy.deepcopy(blocks)}}


def _press(client, blocks, text, ts, action_id, cid):
    app_module._handle_code_queue_button(_body(blocks, text, ts, cid, action_id), client, action_id)


def _current(client, original):
    return client.updates[-1]["blocks"] if client.updates else original


def _ledger_sha():
    return hashlib.sha256(cq._EVENT_LEDGER.read_bytes()).hexdigest()


def _shape_ok(before, after):
    assert len(after) == len(before) and len(after) <= 50
    bids = [b["block_id"] for b in after if b.get("block_id")]
    assert len(bids) == len(set(bids)) and all(len(x) <= 255 for x in bids)


def _history_for(client, text, ts, original):
    client.history = lambda **kw: {"messages": [
        {"ts": ts, "text": text, "blocks": copy.deepcopy(_current(client, original))}]}


def _park(client, ts, cid, reason="waiting on the vendor"):
    fut = (cq._now().date() + timedelta(days=30)).isoformat()
    view = {"private_metadata": json.dumps({"cq_id": cid, "dm_channel": "D123", "dm_ts": ts}),
            "state": {"values": {"cq_park_reason": {"v": {"value": reason}},
                                 "cq_park_until": {"v": {"selected_date": fut}},
                                 "cq_park_event": {"v": {"value": ""}}}}}
    app_module.handle_cq_park_submit(lambda **k: None, {"user": {"id": HARRISON}}, client, view)


def _dismiss(client, ts, cid, note="answered in decisions.md; no build needed"):
    view = {"private_metadata": json.dumps({"cq_id": cid, "dm_channel": "D123", "dm_ts": ts}),
            "state": {"values": {"cq_dismiss_note": {"v": {"value": note}}}}}
    app_module.handle_cq_dismiss_submit(lambda **k: None, {"user": {"id": HARRISON}}, client, view)


# ── 1. every button re-renders exactly its own row ───────────────────────────

class TestEachButtonRerendersItsRow:
    @pytest.mark.parametrize("kind", ["stage", "queue_high", "keep", "park", "dismiss_note"])
    def test_one_press_one_update_only_that_row(self, qenv, kind):
        _seed_921()
        client = FakeClient()
        blocks, text, ts = _post_menu(client)
        cid = {"stage": APPROVED_921[0], "queue_high": HIGH_921[0], "keep": STALE_921[0],
               "park": APPROVED_921[1], "dismiss_note": AGED_921[0]}[kind]
        if kind in ("park", "dismiss_note"):
            _history_for(client, text, ts, blocks)
            (_park if kind == "park" else _dismiss)(client, ts, cid)
            assert client.history_calls[-1] == {"channel": "D123", "latest": ts, "oldest": ts,
                                                 "inclusive": True, "limit": 1}
        else:
            action = {"stage": cq.ACTION_STAGE, "queue_high": cq.ACTION_APPROVE,
                      "keep": cq.ACTION_KEEP}[kind]
            _press(client, blocks, text, ts, action, cid)
        assert len(client.updates) == 1
        up = client.updates[0]
        new = up["blocks"]
        assert up["channel"] == "D123" and up["ts"] == ts
        assert up["text"] == text                      # the menu's own fallback text
        _shape_ok(blocks, new)
        pressed = _row(new, cid)
        assert pressed["type"] == "context" and pressed["block_id"].startswith("cq_done_")
        assert _resolved_ids(new) == [cid]
        for old_b, new_b in zip(blocks, new):          # every other block byte-identical
            if old_b is not _row(blocks, cid):
                assert json.dumps(old_b, sort_keys=True) == json.dumps(new_b, sort_keys=True)
        # the outcome is STILL threaded under the menu
        assert client.threads and client.threads[-1]["thread_ts"] == ts
        label = pressed["elements"][0]["text"]
        want = {"stage": "📝 Prompt staged", "queue_high": "✅ Queued (APPROVED) → 📝 Prompt staged",
                "keep": "🔁 Kept (x1)", "park": "⏸ Parked", "dismiss_note": "🗑️ Dismissed"}[kind]
        assert label.startswith(want) and label.endswith(" AZ")

    def test_the_queue_high_press_keeps_the_loud_kickoff_failure_text(self, qenv, monkeypatch):
        """Threading is not suppressed on a state change: the approve path's 'kickoff
        did NOT generate' clause must reach Harrison even though the row re-renders."""
        _seed_921()
        monkeypatch.setattr(cq, "generate_kickoff_prompt", lambda items, **kw: None)
        client = FakeClient()
        blocks, text, ts = _post_menu(client)
        _press(client, blocks, text, ts, cq.ACTION_APPROVE, HIGH_921[1])
        assert "did NOT generate" in client.threads[-1]["text"]
        assert _row_text(client.updates[-1]["blocks"], HIGH_921[1]).startswith("✅ Queued (APPROVED)")


# ── 2. nine cards pressed in order ───────────────────────────────────────────

class TestNinePressesInOrder:
    NINE = [(cq.ACTION_APPROVE, AGED_921[1])] + [(cq.ACTION_KEEP, c) for c in STALE_921]

    def test_after_press_k_exactly_k_rows_are_resolved(self, qenv):
        _seed_921()
        client = FakeClient()
        blocks, text, ts = _post_menu(client)
        for k, (action, cid) in enumerate(self.NINE, start=1):
            _press(client, _current(client, blocks), text, ts, action, cid)
            cur = _current(client, blocks)
            assert len(_resolved_ids(cur)) == k
            assert cid in _resolved_ids(cur)
            _shape_ok(blocks, cur)
        assert len(client.updates) == 9

    def test_a_stale_client_converges_to_the_same_render(self, qenv):
        _seed_921()
        fresh, stale = FakeClient(), FakeClient()
        blocks, text, ts = _post_menu(fresh)
        for action, cid in self.NINE:
            _press(fresh, _current(fresh, blocks), text, ts, action, cid)
        final_fresh = fresh.updates[-1]["blocks"]
        # a client that never refreshed: every body carries the ORIGINAL snapshot
        _press(stale, blocks, text, ts, cq.ACTION_KEEP, STALE_921[0])   # a noop re-press
        assert json.dumps(stale.updates[-1]["blocks"]) == json.dumps(final_fresh)


# ── 3. the 9/21 incident, replayed ───────────────────────────────────────────

class TestReplay921:
    def _replay(self, client, blocks, text, ts, *, stale_client):
        outcomes = []
        for n, (action, cid, _want) in enumerate(REPLAY_921, start=1):
            if n == 1:
                cq._STAGING_INFLIGHT.add(cid)   # the 08:20:15 double-tap lost the reservation
            body_blocks = blocks if stale_client else _current(client, blocks)
            with mock.patch.object(app_module.log, "info") as info:
                _press(client, body_blocks, text, ts, action, cid)
            line = [c for c in info.call_args_list if c.args and "code-queue button" in c.args[0]]
            outcomes.append(line[-1].args[-1])
            if n == 1:
                cq._STAGING_INFLIGHT.discard(cid)
        return outcomes

    @pytest.mark.parametrize("stale_client", [False, True], ids=["fresh_client", "stale_client"])
    def test_29_presses_resolve_20_rows_and_leave_the_floor_held_row_buttoned(self, qenv, stale_client):
        _seed_921()
        client = FakeClient(menu_ts=MENU_921_TS)
        blocks, text, ts = _post_menu(client)
        assert ts == MENU_921_TS
        assert sum(1 for b in blocks if b.get("type") == "actions") == 21
        outcomes = self._replay(client, blocks, text, ts, stale_client=stale_client)
        assert outcomes == [w for _a, _c, w in REPLAY_921]
        assert {o: outcomes.count(o) for o in set(outcomes)} == {
            "staged": 5, "no_evidence": 1, "approved": 7, "kept": 8, "noop": 8}
        final = client.updates[-1]["blocks"]
        _shape_ok(blocks, final)
        assert len(_resolved_ids(final)) == 20
        held = _row(final, FLOOR_HELD)
        assert held["type"] == "actions" and len(held["elements"]) == 3
        assert json.dumps(held) == json.dumps(_row(blocks, FLOOR_HELD))
        # every press is still answered in the thread (29 replies, as on 9/21)
        assert len(client.threads) == 29
        assert all(u["text"] == text for u in client.updates)
        # the card and cora_queue_status agree by construction (R14-9's predicate)
        after = cq.card_ts_to_dt(ts)
        events = cq._events_by_id()
        decided = {c for c in APPROVED_921 + [FLOOR_HELD] + HIGH_921 + AGED_921 + STALE_921
                   if cq._first_decision_after(events.get(c, []), after) is not None}
        assert decided == set(_resolved_ids(final))
        if not stale_client:
            assert len(client.updates) == 20              # one update per newly decided row


# ── 4. a stale / expired card ────────────────────────────────────────────────

class TestStaleCard:
    def test_a_week_old_menu_resolves_every_row_decided_since_on_one_press(self, qenv):
        _seed_921()
        client = FakeClient(menu_ts=f"{time.time() - 7 * 86400:.6f}")
        blocks, text, ts = _post_menu(client)
        # decided since by typed verb / script -- no press on this card at all
        for cid in APPROVED_921[:3]:
            assert cq.process_queue_action(cq.ACTION_STAGE, cid, HARRISON)[0] == "staged"
        assert cq.process_queue_action(cq.ACTION_DISMISS, AGED_921[0], HARRISON)[0] == "dismissed"
        _press(client, blocks, text, ts, cq.ACTION_KEEP, STALE_921[0])
        assert set(_resolved_ids(client.updates[-1]["blocks"])) == \
            set(APPROVED_921[:3] + [AGED_921[0], STALE_921[0]])

    def test_a_decision_before_the_card_ts_does_not_count(self, qenv):
        _seed_921()
        client = FakeClient()
        blocks, text, ts = _post_menu(client)
        t = cq.card_ts_to_dt(ts)
        _write([{"event": "kept", "id": STALE_921[1], "ts": (t - timedelta(seconds=5)).isoformat()},
                {"event": "approved", "id": FLOOR_HELD, "ts": (t - timedelta(days=7)).isoformat()}])
        new, summary = cq.rerender_card_blocks(blocks, ts)
        assert summary["changed"] is False and summary["reason"] == "nothing_new"
        assert _row(new, STALE_921[1])["type"] == "actions"
        _write([{"event": "kept", "id": STALE_921[1], "ts": t.isoformat()}])   # AT the ts counts
        new, summary = cq.rerender_card_blocks(blocks, ts)
        assert summary["changed"] is True and _resolved_ids(new) == [STALE_921[1]]

    def test_an_id_absent_from_the_ledger_renders_not_in_the_queue(self, qenv):
        _seed_921()
        client = FakeClient()
        blocks, _text, ts = _post_menu(client)
        ghost = "cq-0000deadbeef"
        blocks = copy.deepcopy(blocks)
        row = _row(blocks, APPROVED_921[0])
        row["block_id"] = f"cq_single_{ghost}"
        for e in row["elements"]:
            e["value"] = ghost
        new, summary = cq.rerender_card_blocks(blocks, ts)
        assert summary["changed"] and _row_text(new, ghost) == "Not in the queue ledger"

    def test_an_unparseable_card_ts_is_no_edit_and_a_warning(self, qenv, caplog):
        _seed_921()
        client = FakeClient()
        blocks, text, _ts = _post_menu(client)
        caplog.set_level(logging.WARNING, logger=cq.log.name)
        for bad in ("", "garbage", "0", "-5.1", "1789999254.1234567", "nan", "9" * 13):
            new, summary = cq.rerender_card_blocks(blocks, bad)
            assert summary["changed"] is False and new == blocks, bad
        assert any("unparseable card ts" in r.getMessage() for r in caplog.records)
        client2 = FakeClient()
        _press(client2, blocks, text, "garbage", cq.ACTION_KEEP, STALE_921[0])
        assert client2.updates == []

    def test_an_empty_ledger_never_strips_the_menu(self, qenv, caplog):
        _seed_921()
        client = FakeClient()
        blocks, _text, ts = _post_menu(client)
        cq._EVENT_LEDGER.write_text("", encoding="utf-8")
        caplog.set_level(logging.WARNING, logger=cq.log.name)
        new, summary = cq.rerender_card_blocks(blocks, ts)
        assert summary["changed"] is False and new == blocks
        assert any("ledger read empty" in r.getMessage() for r in caplog.records)


# ── 5. a second press is a no-op and shows the same state ────────────────────

class TestSecondPress:
    def test_stage_twice(self, qenv):
        _seed_921()
        client = FakeClient()
        blocks, text, ts = _post_menu(client)
        _press(client, blocks, text, ts, cq.ACTION_STAGE, APPROVED_921[0])
        first = client.updates[-1]["blocks"]
        sha = _ledger_sha()
        # a refreshed client (the row is already resolved in its copy)
        _press(client, first, text, ts, cq.ACTION_STAGE, APPROVED_921[0])
        assert "Already staged" in client.threads[-1]["text"]
        assert _ledger_sha() == sha and len(client.updates) == 1
        again, summary = cq.rerender_card_blocks(first, ts)
        assert summary["changed"] is False and json.dumps(again) == json.dumps(first)   # fixpoint
        # a stale client (pre-render body): noop, no write, the SAME render
        _press(client, blocks, text, ts, cq.ACTION_STAGE, APPROVED_921[0])
        assert _ledger_sha() == sha
        assert json.dumps(client.updates[-1]["blocks"]) == json.dumps(first)

    def test_queue_twice(self, qenv):
        _seed_921()
        client = FakeClient()
        blocks, text, ts = _post_menu(client)
        _press(client, blocks, text, ts, cq.ACTION_APPROVE, AGED_921[0])
        first, sha = client.updates[-1]["blocks"], _ledger_sha()
        _press(client, blocks, text, ts, cq.ACTION_APPROVE, AGED_921[0])
        assert client.threads[-1]["text"] == "Already queued."
        assert _ledger_sha() == sha and json.dumps(client.updates[-1]["blocks"]) == json.dumps(first)

    def test_keep_twice_on_the_same_card_is_a_noop(self, qenv):
        _seed_921()
        client = FakeClient()
        blocks, text, ts = _post_menu(client)
        _press(client, blocks, text, ts, cq.ACTION_KEEP, STALE_921[0])
        first, sha = client.updates[-1]["blocks"], _ledger_sha()
        assert cq.get_item(STALE_921[0])["keep_count"] == 1
        _press(client, blocks, text, ts, cq.ACTION_KEEP, STALE_921[0])
        assert client.threads[-1]["text"] == "Already kept on this card (x1)."
        assert cq.get_item(STALE_921[0])["keep_count"] == 1
        assert _ledger_sha() == sha and json.dumps(client.updates[-1]["blocks"]) == json.dumps(first)

    def test_dismiss_note_twice(self, qenv):
        _seed_921()
        client = FakeClient()
        blocks, text, ts = _post_menu(client)
        _history_for(client, text, ts, blocks)
        _dismiss(client, ts, AGED_921[1])
        first, sha = client.updates[-1]["blocks"], _ledger_sha()
        client.history = lambda **kw: {"messages": [{"ts": ts, "text": text,
                                                     "blocks": copy.deepcopy(blocks)}]}
        _dismiss(client, ts, AGED_921[1])
        assert client.threads[-1]["text"] == "Already dismissed."
        assert _ledger_sha() == sha and json.dumps(client.updates[-1]["blocks"]) == json.dumps(first)


# ── 6. the in-flight double-tap ──────────────────────────────────────────────

class TestInFlight:
    def test_the_loser_writes_nothing_and_the_winner_resolves_the_row(self, qenv, monkeypatch):
        _seed_921()
        entered, release = threading.Event(), threading.Event()

        def _slow_gen(items, slug=None, meta_out=None, **_kw):
            entered.set()
            assert release.wait(5)
            return f"C:/notes/{items[0]['id']}.md"
        monkeypatch.setattr(cq, "generate_kickoff_prompt", _slow_gen)
        client = FakeClient()
        blocks, text, ts = _post_menu(client)
        cid = APPROVED_921[0]
        a = threading.Thread(target=_press, args=(client, blocks, text, ts, cq.ACTION_STAGE, cid))
        a.start()
        assert entered.wait(5)
        _press(client, blocks, text, ts, cq.ACTION_STAGE, cid)          # tap B, mid-generation
        assert client.threads[-1]["text"].startswith("Already staging")
        assert client.updates == []                                   # nothing decided yet
        release.set()
        a.join(5)
        assert _row(client.updates[-1]["blocks"], cid)["type"] == "context"
        staged = [e for e in cq._events_by_id()[cid] if e["event"] == "staged"]
        assert len(staged) == 1


# ── 7. concurrency: the render lock ──────────────────────────────────────────

class _SpyLock:
    """Wraps the render lock: signals when press-B reaches it (then blocks on the
    real lock, or passes straight through a no-op one)."""

    def __init__(self, inner, b_at_lock):
        self.inner, self.b_at_lock = inner, b_at_lock

    def __enter__(self):
        if threading.current_thread().name == "press-B":
            self.b_at_lock.set()
        return self.inner.__enter__()

    def __exit__(self, *exc):
        return self.inner.__exit__(*exc)


class TestRenderLock:
    def _race(self, qenv, monkeypatch, lock):
        """Press A renders (only A decided) and then stalls INSIDE the lock span;
        press B writes, renders and updates. With the lock, B is held at the lock
        until A's update lands, so B's render (A and B) is applied last; with a
        no-op lock B finishes first and A's stale render is applied last."""
        _seed_921()
        a_rendered, b_done, b_at_lock = threading.Event(), threading.Event(), threading.Event()
        real_lock = not isinstance(lock, contextlib.nullcontext)
        monkeypatch.setattr(app_module, "_CQ_CARD_RENDER_LOCK", _SpyLock(lock, b_at_lock))
        client = FakeClient()
        blocks, text, ts = _post_menu(client)
        real = cq.rerender_card_blocks

        def _wrapped(blk, card_ts):
            out = real(blk, card_ts)
            if threading.current_thread().name == "press-A":
                a_rendered.set()
                # real lock: stall until B is blocked on it; no-op lock: until B is done
                assert (b_at_lock if real_lock else b_done).wait(10)
            return out
        monkeypatch.setattr(cq, "rerender_card_blocks", _wrapped)
        a = threading.Thread(name="press-A", target=_press,
                             args=(client, blocks, text, ts, cq.ACTION_APPROVE, AGED_921[0]))
        a.start()
        assert a_rendered.wait(5)

        def _b():
            _press(client, blocks, text, ts, cq.ACTION_APPROVE, AGED_921[1])
            b_done.set()
        b = threading.Thread(name="press-B", target=_b)
        b.start()
        a.join(15)
        b.join(15)
        assert not a.is_alive() and not b.is_alive()
        assert len(client.updates) == 2
        return set(_resolved_ids(client.updates[-1]["blocks"]))

    def test_the_last_applied_render_carries_both_rows(self, qenv, monkeypatch):
        assert self._race(qenv, monkeypatch, threading.Lock()) == set(AGED_921)

    def test_without_the_lock_the_same_interleaving_loses_a_row(self, qenv, monkeypatch):
        """The control: proves the race is real and that the lock is what closes it."""
        assert self._race(qenv, monkeypatch, contextlib.nullcontext()) == {AGED_921[0]}


# ── 8. LEX ───────────────────────────────────────────────────────────────────

class TestLex:
    def test_a_lex_row_carries_neither_the_raw_title_nor_the_prompt_path(self, qenv):
        _write([_cap("cq-00000000aaaa", status="APPROVED", entity="LEX-LLC", title=LEX_RAW_TITLE,
                     summary="Quibblewick summary", evidence=[{"channel_id": "C1", "ts": "1.2"}]),
                _cap("cq-00000000bbbb", status="APPROVED")])
        assert cq._fold_items()["cq-00000000aaaa"]["title"] == LEX_RAW_TITLE   # else proves nothing
        client = FakeClient()
        blocks, text, ts = _post_menu(client)
        _press(client, blocks, text, ts, cq.ACTION_STAGE, "cq-00000000aaaa")
        assert cq.get_item("cq-00000000aaaa")["status"] == "STAGED"
        dumped = json.dumps(client.updates[-1]["blocks"])
        assert "Quibblewick" not in dumped and LEX_RAW_PATH_TOKEN not in dumped
        assert _row_text(client.updates[-1]["blocks"], "cq-00000000aaaa").startswith("📝 Prompt staged")


# ── 9. the single capture card is unchanged ──────────────────────────────────

class TestSingleCaptureCard:
    def test_a_state_change_still_consumes_the_capture_card(self, qenv):
        _write([_cap("cq-00000000cccc")])
        text, blocks = cq.build_item_card(cq.get_item("cq-00000000cccc"))
        assert not cq.is_menu_card(blocks)
        client = FakeClient()
        _press(client, blocks, text, "1789924534.952229", cq.ACTION_APPROVE, "cq-00000000cccc")
        up = client.updates[-1]
        assert not [b for b in up["blocks"] if b.get("type") == "actions"]
        assert up["text"].startswith("✅ Queued") and client.threads == []

    def test_a_menu_down_to_its_last_open_row_is_still_rendered_row_by_row(self, qenv):
        """One actions block left is NOT a single card: the old count-only route
        would consume it and drop every resolved row above."""
        _seed_921()
        client = FakeClient()
        blocks, text, ts = _post_menu(client)
        order = ([(cq.ACTION_STAGE, c) for c in APPROVED_921] + [(cq.ACTION_APPROVE, c) for c in HIGH_921]
                 + [(cq.ACTION_APPROVE, c) for c in AGED_921] + [(cq.ACTION_KEEP, c) for c in STALE_921])
        for action, cid in order:
            _press(client, _current(client, blocks), text, ts, action, cid)
        cur = _current(client, blocks)
        assert [b for b in cur if b.get("type") == "actions"] == [_row(blocks, FLOOR_HELD)]
        _dismiss_via_button = cq.process_queue_action(cq.ACTION_DISMISS, FLOOR_HELD, HARRISON)
        assert _dismiss_via_button[0] == "dismissed"
        _press(client, cur, text, ts, cq.ACTION_STAGE, FLOOR_HELD)   # its last press: a noop
        final = client.updates[-1]["blocks"]
        _shape_ok(blocks, final)
        assert len(_resolved_ids(final)) == 21 and client.updates[-1]["text"] == text

    def test_keep_card_outcomes_thread_and_keep_the_buttons(self, qenv):
        _seed_921()
        client = FakeClient()
        blocks, text, ts = _post_menu(client)
        _press(client, blocks, text, ts, cq.ACTION_STAGE, FLOOR_HELD)
        assert client.updates == [] and "NOT staged" in client.threads[-1]["text"]


# ── 10. modal fetch: the returned ts must equal the card ts ──────────────────

class TestModalFetch:
    def test_a_mismatched_history_ts_is_never_edited(self, qenv, caplog):
        _seed_921()
        client = FakeClient()
        blocks, text, ts = _post_menu(client)
        caplog.set_level(logging.WARNING, logger=app_module.log.name)
        client.history = lambda **kw: {"messages": [{"ts": "1000.0001", "text": "older",
                                                     "blocks": copy.deepcopy(blocks)}]}
        _park(client, ts, APPROVED_921[1])
        assert cq.get_item(APPROVED_921[1])["status"] == "PARKED"
        assert client.updates == []
        assert client.threads[-1]["thread_ts"] == ts and "Parked" in client.threads[-1]["text"]
        assert any("not editing a different message" in r.getMessage() for r in caplog.records)

    def test_a_failed_fetch_falls_back_to_the_thread(self, qenv):
        _seed_921()
        client = FakeClient()
        blocks, _text, ts = _post_menu(client)

        def _boom(**kw):
            raise RuntimeError("history down")
        client.history = _boom
        _dismiss(client, ts, AGED_921[0])
        assert client.updates == [] and "Dismissed with evidence" in client.threads[-1]["text"]

    def test_a_refused_submit_does_not_fetch(self, qenv):
        _seed_921()
        client = FakeClient()
        _blocks, _text, ts = _post_menu(client)
        _park(client, ts, APPROVED_921[1], reason="waiting on Bob Smith's billing authorization")
        assert client.history_calls == [] and "PHI" in client.threads[-1]["text"]


# ── 11. the renderer is pure ─────────────────────────────────────────────────

class TestPurity:
    def test_the_render_writes_nothing(self, qenv, monkeypatch):
        _seed_921()
        client = FakeClient()
        blocks, _text, ts = _post_menu(client)
        cq.process_queue_action(cq.ACTION_KEEP, STALE_921[0], HARRISON)
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in qenv.iterdir() if p.is_file()}
        boom = lambda *a, **k: (_ for _ in ()).throw(AssertionError("write"))  # noqa: E731
        monkeypatch.setattr(cq, "_append_event", boom)
        monkeypatch.setattr(cq, "_append_jsonl", boom)
        snapshot = copy.deepcopy(blocks)
        out1, s1 = cq.rerender_card_blocks(blocks, ts)
        out2, s2 = cq.rerender_card_blocks(copy.deepcopy(blocks), ts)
        assert blocks == snapshot                               # the input is not mutated
        assert json.dumps(out1) == json.dumps(out2) and s1 == s2  # deterministic
        after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in qenv.iterdir() if p.is_file()}
        assert before == after

    def test_rows_are_keyed_on_block_id_and_values_never_on_echoed_text(self, qenv):
        _seed_921()
        client = FakeClient()
        blocks, _text, ts = _post_menu(client)
        cq.process_queue_action(cq.ACTION_KEEP, STALE_921[0], HARRISON)
        echoed = copy.deepcopy(blocks)
        for b in echoed:   # what Slack's read-back does to the text
            if b.get("type") == "section":
                b["text"]["text"] = b["text"]["text"].replace("⏳", ":hourglass_flowing_sand:") \
                    .replace(">", "&gt;")
                b["text"]["verbatim"] = False
            for e in b.get("elements") or []:
                if isinstance(e.get("text"), dict):
                    e["text"]["emoji"] = True
        out, summary = cq.rerender_card_blocks(echoed, ts)
        assert summary["changed"] and _resolved_ids(out) == [STALE_921[0]]
        # the echoed sections pass through verbatim (never regenerated)
        assert [b for b in out if b.get("type") == "section"] == \
            [b for b in echoed if b.get("type") == "section"]

    def test_the_label_map_covers_the_decision_predicate(self):
        assert set(cq._CARD_DECISION_LABELS) == set(cq._QS_DECISION_EVENTS)

    def test_card_ts_is_parsed_exactly(self):
        t = cq.card_ts_to_dt(MENU_921_TS)
        assert t == datetime(2026, 9, 21, 14, 0, 54, 143349, tzinfo=timezone.utc)
        assert cq.card_ts_to_dt("1789999254") == datetime(2026, 9, 21, 14, 0, 54, tzinfo=timezone.utc)


# ── 12. the footer's claims, measured ────────────────────────────────────────

class TestFooterClaims:
    def test_keep_is_idempotent_per_card_not_per_week(self, qenv):
        _write([_cap("cq-00000000dddd", status="APPROVED", ts=_iso(40)),
                {"event": "staged", "id": "cq-00000000dddd", "ts": _iso(30), "prompt_path": "/p"}])
        card_1 = f"{time.time() - 60:.6f}"
        assert cq.process_queue_action(cq.ACTION_KEEP, "cq-00000000dddd", HARRISON,
                                       card_ts=card_1)[0] == "kept"
        o, msg = cq.process_queue_action(cq.ACTION_KEEP, "cq-00000000dddd", HARRISON, card_ts=card_1)
        assert (o, msg) == ("noop", "Already kept on this card (x1).")
        time.sleep(0.01)
        card_2 = f"{time.time():.6f}"                           # next week's menu: a NEW card
        assert cq.process_queue_action(cq.ACTION_KEEP, "cq-00000000dddd", HARRISON,
                                       card_ts=card_2)[0] == "kept"
        # the cap still holds, on any card
        time.sleep(0.01)
        o3, msg3 = cq.process_queue_action(cq.ACTION_KEEP, "cq-00000000dddd", HARRISON,
                                           card_ts=f"{time.time():.6f}")
        assert o3 == "noop" and "capped" in msg3
        assert cq.get_item("cq-00000000dddd")["keep_count"] == 2
        # a typed / scripted Keep (no card) keeps the pre-S5 behaviour: the cap alone
        assert cq.process_queue_action(cq.ACTION_KEEP, "cq-00000000dddd", HARRISON)[0] == "noop"

    def test_a_repeat_park_or_later_still_records_one_more(self, qenv):
        _write([_cap("cq-00000000eeee", status="APPROVED"), _cap("cq-00000000ffff")])
        fut = (cq._now().date() + timedelta(days=30)).isoformat()
        assert cq.park_item("cq-00000000eeee", HARRISON, "r", until=fut)[0] == "parked"
        assert cq.park_item("cq-00000000eeee", HARRISON, "r", until=fut)[0] == "parked"
        assert cq.process_queue_action(cq.ACTION_LATER, "cq-00000000ffff", HARRISON)[0] == "snoozed"
        assert cq.process_queue_action(cq.ACTION_LATER, "cq-00000000ffff", HARRISON)[0] == "snoozed"
        ev = cq._events_by_id()
        assert sum(e["event"] == "parked" for e in ev["cq-00000000eeee"]) == 2
        assert sum(e["event"] == "snoozed" for e in ev["cq-00000000ffff"]) == 2

    def test_the_footer_rides_the_status_read(self, qenv):
        assert cq.QUEUE_STATUS_FOOTER in cq.render_card_status()
        assert "cq-2d26f131091e" in cq.QUEUE_STATUS_FOOTER
