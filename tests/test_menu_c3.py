"""C3 -- Monday menu EXTENDED (Code #12; audit F2/F6/F8).

Contract under test:
  * PROPOSED coverage: rows aged >= 14d and/or P0/P1-class appear on the card --
    the first rows actionable (Queue / Park / Dismiss w/ note), the rest LISTED,
    nothing dropped, the card never exceeds Slack's 50-block ceiling;
  * a Keep tap is counted and rendered; at KEEP_CAP the row renders in a distinct
    state with no Keep button and a further Keep is a no-op;
  * park-with-trigger: reason + (date and/or event); a PARKED row leaves every
    section until its date passes, then resurfaces as due; an event-park is listed
    count-only; PHI-screened; Harrison-only;
  * dismiss-with-evidence records the reason on the row;
  * every maybe_send_weekly_menu call writes ONE artifact row (sent + carried, or
    why not) -- to the redirected ledger, never the live one;
  * the app.py modal handlers are registered.
"""

from __future__ import annotations

import inspect
import json
import uuid
from datetime import timedelta

import pytest

from cora import code_queue as cq

HARRISON = "U0B2RM2JYJ1"


class FakeClient:
    def __init__(self):
        self.posts = []

    def conversations_open(self, users=None):
        return {"channel": {"id": "D123"}}

    def chat_postMessage(self, **kw):
        self.posts.append(kw)
        return {"ts": "1000.1"}


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
    return tmp_path


def _seed(title, status="PROPOSED", severity="P3", kind="bug", sub=""):
    # seed_item fuzzy-dedups same-signal titles at ratio >= 0.85 ("prop 10" ~ "prop 11"),
    # so every fixture title carries a random suffix to stay a distinct row.
    return cq.seed_item(kind=kind, severity=severity, title=f"{title} {uuid.uuid4().hex}",
                        summary="s", entity="F3E", signal="explicit", status=status,
                        subsystem_guess=sub)


def _age(qenv, ids, days):
    """Test-only ledger surgery: push the captured ts of `ids` back by `days`."""
    path = cq._EVENT_LEDGER
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
    for r in rows:
        if r.get("event") == "captured" and r.get("id") in ids:
            r["ts"] = (cq._now() - timedelta(days=days)).isoformat()
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def _actions(blocks, action_id):
    return [e for b in blocks if b.get("type") == "actions"
            for e in b["elements"] if e.get("action_id") == action_id]


class TestProposedCoverage:
    def test_34_aged_plus_8_high_are_carried_nothing_dropped(self, qenv):
        aged = [_seed(f"aged {i}") for i in range(34)]
        _age(qenv, set(aged), 20)
        high = [_seed(f"high {i}", severity="HIGH") for i in range(8)]
        fresh_low = _seed("fresh low", severity="LOW")  # neither aged nor priority: NOT covered
        carried = {}
        text, blocks = cq.build_weekly_menu(carried_out=carried)
        covered = set(carried["proposed_actionable"]) | set(carried["proposed_listed"])
        assert len(covered) + carried["proposed_overflow"] == 42
        assert fresh_low not in covered
        assert carried["proposed_aged"] == 34 and carried["proposed_priority"] == 8
        # priority rows come first and are actionable
        # the 8 P0/P1-class rows fill the 8 actionable slots; the aged rows are listed
        # (as many whole lines as one block holds) and the rest counted as overflow
        assert set(carried["proposed_actionable"]) == set(high)
        assert set(carried["proposed_listed"]) <= set(aged)
        assert len(carried["proposed_listed"]) + carried["proposed_overflow"] == 34
        assert len(carried["proposed_listed"]) >= 20
        assert len(blocks) <= 50 and carried["blocks"] == len(blocks)
        assert "PROPOSED coverage" in text and "nothing else surfaces these" in text
        assert "other" not in text.lower()  # the no-kitchen-sink pin stays honest
        assert len(_actions(blocks, cq.ACTION_APPROVE)) == len(carried["proposed_actionable"])
        assert len(_actions(blocks, cq.ACTION_PARK)) >= len(carried["proposed_actionable"])

    def test_block_budget_holds_with_a_full_card(self, qenv):
        # 12 approved bundles-worth of singletons + many stale + many proposed
        for i in range(12):
            _seed(f"appr {i}", status="APPROVED", sub=f"sub{i}")
        stale = [_seed(f"stale {i}", status="APPROVED", sub=f"st{i}") for i in range(6)]
        for cid in stale:
            cq._append_event({"event": "staged", "ts": (cq._now() - timedelta(days=20)).isoformat(),
                              "id": cid, "prompt_path": "/p", "bundle_id": f"solo-{cid}"})
        for i in range(30):
            _seed(f"prop {i}", severity="HIGH")
        carried = {}
        _text, blocks = cq.build_weekly_menu(carried_out=carried)
        assert len(blocks) <= 50
        assert carried["proposed_overflow"] + len(carried["proposed_listed"]) + \
            len(carried["proposed_actionable"]) == 30

    def test_no_proposed_no_section(self, qenv):
        _seed("appr", status="APPROVED")
        carried = {}
        text, _blocks = cq.build_weekly_menu(carried_out=carried)
        assert "PROPOSED coverage" not in text and carried["proposed_actionable"] == []

    def test_proposed_only_ledger_still_builds_a_card(self, qenv):
        _seed("lonely high", severity="P1")
        assert cq.build_weekly_menu() is not None


class TestKeepCountAndCap:
    def _stale(self, qenv):
        cid = _seed("stale", status="APPROVED")
        cq._append_event({"event": "staged", "ts": (cq._now() - timedelta(days=20)).isoformat(),
                          "id": cid, "prompt_path": "/p", "bundle_id": f"solo-{cid}"})
        return cid

    def test_keep_is_counted_and_rendered(self, qenv):
        cid = self._stale(qenv)
        o, msg = cq.process_queue_action(cq.ACTION_KEEP, cid, HARRISON)
        assert o == "kept" and "x1" in msg and "1 Keep left" in msg
        assert cq.get_item(cid)["keep_count"] == 1
        # a Keep resets the staleness clock (the row leaves the stale section for 14d)
        carried = {}
        cq.build_weekly_menu(carried_out=carried)
        assert cid not in carried["stale_staged"]

    def test_second_keep_reaches_the_cap_and_renders_distinctly(self, qenv):
        cid = self._stale(qenv)
        cq.process_queue_action(cq.ACTION_KEEP, cid, HARRISON)
        o, msg = cq.process_queue_action(cq.ACTION_KEEP, cid, HARRISON)
        assert o == "kept" and "last Keep" in msg
        # age the last_touch back so the row is stale again
        path = cq._EVENT_LEDGER
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        for r in rows:
            if r.get("event") == "kept":
                r["ts"] = (cq._now() - timedelta(days=15)).isoformat()
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        text, blocks = cq.build_weekly_menu()
        assert "KEPT x2 (capped)" in text
        assert not [e for e in _actions(blocks, cq.ACTION_KEEP) if e["value"] == cid]
        assert [e for e in _actions(blocks, cq.ACTION_PARK) if e["value"] == cid]
        o3, msg3 = cq.process_queue_action(cq.ACTION_KEEP, cid, HARRISON)
        assert o3 == "noop" and "capped" in msg3
        assert cq.get_item(cid)["keep_count"] == 2

    def test_uncapped_stale_row_still_offers_keep(self, qenv):
        cid = self._stale(qenv)
        _text, blocks = cq.build_weekly_menu()
        assert [e for e in _actions(blocks, cq.ACTION_KEEP) if e["value"] == cid]


class TestPark:
    def test_park_requires_reason_and_trigger(self, qenv):
        cid = _seed("park me", status="APPROVED")
        assert cq.park_item(cid, HARRISON, "")[0] == "error"
        assert cq.park_item(cid, HARRISON, "waiting on Q4 budget")[0] == "error"
        assert cq.park_item(cid, HARRISON, "x", until="next tuesday")[0] == "error"
        assert cq.park_item(cid, "U_TOMMY", "x", until="2026-10-01")[0] == "not_authorized"
        assert cq.get_item(cid)["status"] == "APPROVED"

    def test_park_with_date_leaves_the_card_until_due(self, qenv):
        cid = _seed("park me", status="APPROVED")
        o, msg = cq.park_item(cid, HARRISON, "waiting on the Q4 budget", until="2099-01-01")
        assert o == "parked" and "until 2099-01-01" in msg
        rec = cq.get_item(cid)
        assert rec["status"] == "PARKED" and rec["park_reason"] == "waiting on the Q4 budget"
        carried = {}
        cq.build_weekly_menu(carried_out=carried)
        assert cid not in carried["approved"] and cid in carried["parked_waiting"]
        assert cid not in carried["parked_due"]

    def test_due_park_resurfaces_with_actions(self, qenv):
        cid = _seed("park me", status="APPROVED")
        cq.park_item(cid, HARRISON, "wait for the vendor", until="2026-01-01")
        carried = {}
        text, blocks = cq.build_weekly_menu(carried_out=carried)
        assert cid in carried["parked_due"]
        assert "park trigger reached (2026-01-01): wait for the vendor" in text
        assert [e for e in _actions(blocks, cq.ACTION_APPROVE) if e["value"] == cid]

    def test_event_park_is_listed_count_only(self, qenv):
        cid = _seed("park me", status="APPROVED")
        o, _ = cq.park_item(cid, HARRISON, "blocked on Tessa", trigger_event="Tessa grants project admin")
        assert o == "parked"
        carried = {}
        text, blocks = cq.build_weekly_menu(carried_out=carried)
        assert cid in carried["parked_waiting"]
        assert "Parked (1), waiting on a trigger" in text
        assert "Tessa grants project admin" in "\n".join(
            b["text"]["text"] for b in blocks if b.get("type") == "section")
        assert not [e for e in _actions(blocks, cq.ACTION_APPROVE) if e["value"] == cid]

    def test_park_is_phi_screened_and_refuses_terminal(self, qenv):
        cid = _seed("park me", status="APPROVED")
        o, msg = cq.park_item(cid, HARRISON, "waiting on Bob Smith's billing authorization",
                              until="2099-01-01")
        assert o == "error" and "PHI" in msg
        cq._append_event({"event": "shipped", "ts": cq._now_iso(), "id": cid, "bundle_id": "b"})
        assert cq.park_item(cid, HARRISON, "x", until="2099-01-01")[0] == "noop"

    def test_park_on_a_proposed_row_then_requeue(self, qenv):
        cid = _seed("proposed park", severity="P1")
        cq.park_item(cid, HARRISON, "later", until="2026-01-01")
        o, _ = cq.process_queue_action(cq.ACTION_APPROVE, cid, HARRISON)
        assert o == "approved" and cq.get_item(cid)["status"] in ("APPROVED", "STAGED")


class TestDismissWithEvidence:
    def test_reason_is_recorded(self, qenv):
        cid = _seed("dismiss me")
        o, msg = cq.dismiss_with_evidence(cid, HARRISON, "answered 9/4 in decisions.md; no build needed")
        assert o == "dismissed"
        rec = cq.get_item(cid)
        assert rec["status"] == "DISMISSED" and "answered 9/4" in rec["dismiss_reason"]
        assert cq.dismiss_with_evidence(cid, HARRISON, "again")[0] == "noop"

    def test_requires_a_note_and_is_phi_screened(self, qenv):
        cid = _seed("dismiss me")
        assert cq.dismiss_with_evidence(cid, HARRISON, "")[0] == "error"
        assert cq.dismiss_with_evidence(cid, HARRISON, "Bob Smith's billing authorization")[0] == "error"
        assert cq.dismiss_with_evidence(cid, "U_TOMMY", "x")[0] == "not_authorized"
        assert cq.get_item(cid)["status"] == "PROPOSED"


class TestArtifact:
    def test_send_records_carried_ids(self, qenv):
        cid = _seed("appr", status="APPROVED")
        client = FakeClient()
        assert cq.maybe_send_weekly_menu(client_factory=lambda: client) is True
        rows = [json.loads(l) for l in (qenv / "menu-runs.jsonl").read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 1 and rows[0]["sent"] is True and rows[0]["message_ts"] == "1000.1"
        assert rows[0]["approved"] == [cid] and rows[0]["blocks"] == len(client.posts[0]["blocks"])

    def test_nothing_to_show_still_writes_a_row(self, qenv):
        assert cq.maybe_send_weekly_menu(client_factory=lambda: FakeClient()) is False
        rows = [json.loads(l) for l in (qenv / "menu-runs.jsonl").read_text(encoding="utf-8").splitlines()]
        assert rows[0]["sent"] is False and rows[0]["reason"] == "nothing_to_show"

    def test_not_live_writes_a_row(self, qenv, monkeypatch):
        monkeypatch.setenv("CORA_CODE_QUEUE", "log")
        assert cq.maybe_send_weekly_menu(client_factory=lambda: FakeClient()) is False
        rows = [json.loads(l) for l in (qenv / "menu-runs.jsonl").read_text(encoding="utf-8").splitlines()]
        assert rows[0]["reason"] == "not_live"

    def test_artifact_ledger_is_redirected_by_the_suite(self):
        """The conftest autouse redirect covers the new write path (a suite run must
        never write into data/state/code-queue-menu-runs.jsonl)."""
        assert cq._MENU_RUNS_LEDGER.name == "code-queue-menu-runs.jsonl"
        assert "pytest" in str(cq._MENU_RUNS_LEDGER).lower() or "tmp" in str(cq._MENU_RUNS_LEDGER).lower()


class TestModalsAndWiring:
    def test_modal_views_carry_the_pointer(self, qenv):
        cid = _seed("modal")
        v = cq.park_modal_view(cid, "D1", "9.9")
        assert v["callback_id"] == cq.VIEW_PARK_SUBMIT
        assert json.loads(v["private_metadata"]) == {"cq_id": cid, "dm_channel": "D1", "dm_ts": "9.9"}
        ids = [b.get("block_id") for b in v["blocks"] if b.get("type") == "input"]
        assert ids == ["cq_park_reason", "cq_park_until", "cq_park_event"]
        d = cq.dismiss_modal_view(cid, "D1", "9.9")
        assert d["callback_id"] == cq.VIEW_DISMISS_SUBMIT
        assert [b.get("block_id") for b in d["blocks"] if b.get("type") == "input"] == ["cq_dismiss_note"]

    def test_app_handlers_registered_and_gated(self):
        import cora.app as app_module
        src = inspect.getsource(app_module)
        for token in ("@app.action(code_queue.ACTION_PARK)", "@app.view(code_queue.VIEW_PARK_SUBMIT)",
                      "@app.action(code_queue.ACTION_DISMISS_NOTE)",
                      "@app.view(code_queue.VIEW_DISMISS_SUBMIT)"):
            assert token in src, token
        opener = inspect.getsource(app_module._open_cq_modal)
        assert "actor_id != code_queue.HARRISON_ID" in opener
        submit = inspect.getsource(app_module.handle_cq_park_submit)
        assert 'selected_date' in submit and "code_queue.park_item(" in submit

    def test_park_submit_flows_to_park_item(self, qenv):
        import cora.app as app_module
        from unittest.mock import MagicMock
        cid = _seed("submit", status="APPROVED")
        client = MagicMock()
        view = {"private_metadata": json.dumps({"cq_id": cid, "dm_channel": "D1", "dm_ts": "9.9"}),
                "state": {"values": {"cq_park_reason": {"v": {"value": "await budget"}},
                                     "cq_park_until": {"v": {"selected_date": "2099-01-01"}},
                                     "cq_park_event": {"v": {"value": ""}}}}}
        app_module.handle_cq_park_submit(lambda: None, {"user": {"id": HARRISON}}, client, view)
        assert cq.get_item(cid)["status"] == "PARKED"
        kw = client.chat_postMessage.call_args.kwargs
        assert kw["channel"] == "D1" and kw["thread_ts"] == "9.9" and "Parked" in kw["text"]


class TestLiveReadOnly:
    def test_live_ledger_menu_fits_slack_and_covers_proposed(self):
        real = cq._DEFAULT_EVENT_LEDGER
        if not real.exists():
            pytest.skip("no live ledger on this host")
        saved = cq._EVENT_LEDGER
        try:
            cq._EVENT_LEDGER = real
            carried = {}
            built = cq.build_weekly_menu(carried_out=carried)
        finally:
            cq._EVENT_LEDGER = saved
        assert built is not None
        _text, blocks = built
        assert len(blocks) <= 50
        assert carried["trimmed"] == 0  # the allocator, not the guillotine, kept it under 50
        assert carried["proposed_aged"] + carried["proposed_priority"] > 0
        # the section C3 exists for is VISIBLE on the live card, buttons included
        assert len(carried["proposed_actionable"]) >= 1
        ids = [b.get("block_id") for b in blocks if b.get("type") == "actions"]
        assert len(ids) == len(set(ids))  # Slack rejects duplicate block_ids
        assert all(len(b["text"]["text"]) <= 3000 for b in blocks if b.get("type") == "section")

    def test_stale_rows_cannot_crowd_out_proposed_coverage(self, qenv):
        """The live-ledger defect replayed: 24 stale STAGED rows + 13 priority PROPOSED
        rows. Before the allocator the card built 54 blocks and the PROPOSED section
        was trimmed off; now every section gets its slots first and nothing is trimmed."""
        for i in range(24):
            cid = _seed(f"stale {i}", status="APPROVED", sub=f"s{i}")
            cq._append_event({"event": "staged", "ts": (cq._now() - timedelta(days=20)).isoformat(),
                              "id": cid, "prompt_path": "/p", "bundle_id": f"solo-{cid}"})
        for i in range(13):
            _seed(f"hi {i}", severity="HIGH")
        carried = {}
        _text, blocks = cq.build_weekly_menu(carried_out=carried)
        assert len(blocks) <= 50 and carried["trimmed"] == 0
        assert len(carried["proposed_actionable"]) == 8
        assert len(carried["stale_actionable"]) == 8 and len(carried["stale_listed"]) == 16
        assert len(carried["proposed_listed"]) == 5 and carried["proposed_overflow"] == 0

    def test_listing_never_truncates_mid_line(self, qenv):
        # long, fully random titles (a shared prefix would fuzzy-dedup at 0.85)
        ids = [cq.seed_item(kind="bug", severity="HIGH", title=uuid.uuid4().hex * 3, summary="s",
                            entity="F3E", signal="explicit", status="PROPOSED") for _ in range(60)]
        assert len(set(ids)) == 60
        carried = {}
        _text, blocks = cq.build_weekly_menu(carried_out=carried)
        listing = [b for b in blocks if b.get("type") == "section"
                   and b["text"]["text"].startswith("*Also PROPOSED")][0]["text"]["text"]
        assert len(listing) <= 3000
        assert carried["proposed_overflow"] > 0  # 52 long lines cannot fit one block
        assert listing.rstrip().endswith("nothing dropped_")
        assert f"+{carried['proposed_overflow']} more" in listing
        assert len(carried["proposed_listed"]) + carried["proposed_overflow"] + \
            len(carried["proposed_actionable"]) == 60
        assert all(cid in listing for cid in carried["proposed_listed"])  # every listed id is whole
        assert not any(cid in listing for cid in ids
                       if cid not in carried["proposed_listed"] + carried["proposed_actionable"])
