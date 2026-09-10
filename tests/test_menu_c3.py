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
        # the no-kitchen-sink pin: no BUNDLE row is themed "other" (D-051 lens D LOW
        # #13: a bare substring reddened on 'another'/'others' in ordinary copy)
        import re as _re
        assert not _re.search(r"^- \*other\*", text, _re.M | _re.I)
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
        fut = (cq._now().date() + timedelta(days=30)).isoformat()
        o, msg = cq.park_item(cid, HARRISON, "waiting on the Q4 budget", until=fut)
        assert o == "parked" and f"until {fut}" in msg
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
        sections = "\n".join(b["text"]["text"] for b in blocks if b.get("type") == "section")
        assert "Tessa grants project admin" in sections and "review by" in sections
        assert not [e for e in _actions(blocks, cq.ACTION_APPROVE) if e["value"] == cid]

    def test_park_is_phi_screened_and_refuses_terminal(self, qenv):
        cid = _seed("park me", status="APPROVED")
        fut = (cq._now().date() + timedelta(days=30)).isoformat()
        o, msg = cq.park_item(cid, HARRISON, "waiting on Bob Smith's billing authorization",
                              until=fut)
        assert o == "error" and "PHI" in msg
        cq._append_event({"event": "shipped", "ts": cq._now_iso(), "id": cid, "bundle_id": "b"})
        assert cq.park_item(cid, HARRISON, "x", until=fut)[0] == "noop"

    def test_park_is_bounded_and_an_event_park_gets_a_review_horizon(self, qenv):
        """D-051 lens B HIGH #2: a 2099 park was accepted and an event-only park never
        became due -- one tap could silence a recurring P0 ask indefinitely."""
        cid = _seed("park me", status="APPROVED")
        o, msg = cq.park_item(cid, HARRISON, "x", until="2099-01-01")
        assert o == "error" and f"at most {cq.PARK_MAX_DAYS} days" in msg
        too_far = (cq._now().date() + timedelta(days=cq.PARK_MAX_DAYS + 1)).isoformat()
        assert cq.park_item(cid, HARRISON, "x", until=too_far)[0] == "error"
        o, msg = cq.park_item(cid, HARRISON, "blocked on Tessa", trigger_event="Tessa grants admin")
        rec = cq.get_item(cid)
        horizon = (cq._now().date() + timedelta(days=cq.PARK_MAX_DAYS)).isoformat()
        assert o == "parked" and rec["park_until"] == horizon and rec["park_horizon"] is True
        assert f"review by {horizon}" in msg and "review horizon" in msg
        errors = cq.validate_park("", "2099-01-01", "")
        assert set(errors) == {"cq_park_reason", "cq_park_until"}
        assert cq.validate_park("why", "", "") == {"cq_park_event": cq.validate_park("why", "", "")["cq_park_event"]}
        assert cq.validate_park("why", "", "an event") == {}

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

    def test_artifact_ledger_is_redirected_by_the_suite(self, tmp_path):
        """The conftest autouse redirect covers the new write path (a suite run must
        never write into data/state/code-queue-menu-runs.jsonl)."""
        assert cq._MENU_RUNS_LEDGER == tmp_path / "code-queue-menu-runs.jsonl"  # THIS test's tmp


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
        fut = (cq._now().date() + timedelta(days=30)).isoformat()
        view = {"private_metadata": json.dumps({"cq_id": cid, "dm_channel": "D1", "dm_ts": "9.9"}),
                "state": {"values": {"cq_park_reason": {"v": {"value": "await budget"}},
                                     "cq_park_until": {"v": {"selected_date": fut}},
                                     "cq_park_event": {"v": {"value": ""}}}}}
        ack = MagicMock()
        app_module.handle_cq_park_submit(ack, {"user": {"id": HARRISON}}, client, view)
        ack.assert_called_once_with()
        assert cq.get_item(cid)["status"] == "PARKED"
        kw = client.chat_postMessage.call_args.kwargs
        assert kw["channel"] == "D1" and kw["thread_ts"] == "9.9" and "Parked" in kw["text"]

    def test_park_submit_validation_errors_render_in_the_modal(self, qenv):
        """D-051 lens F MED #3: an invalid submit used to ack, close the modal and
        thread nothing legible; the errors now come back in the modal."""
        import cora.app as app_module
        from unittest.mock import MagicMock
        cid = _seed("submit", status="APPROVED")
        client = MagicMock()
        view = {"private_metadata": json.dumps({"cq_id": cid, "dm_channel": "D1", "dm_ts": "9.9"}),
                "state": {"values": {"cq_park_reason": {"v": {"value": ""}},
                                     "cq_park_until": {"v": {"selected_date": "2099-01-01"}},
                                     "cq_park_event": {"v": {"value": ""}}}}}
        ack = MagicMock()
        app_module.handle_cq_park_submit(ack, {"user": {"id": HARRISON}}, client, view)
        assert ack.call_count == 1
        kw = ack.call_args.kwargs
        assert kw["response_action"] == "errors" and set(kw["errors"]) == {"cq_park_reason", "cq_park_until"}
        assert cq.get_item(cid)["status"] == "APPROVED"
        client.chat_postMessage.assert_not_called()
        # dismiss modal: an empty note errors in the modal too
        dview = {"private_metadata": json.dumps({"cq_id": cid, "dm_channel": "D1", "dm_ts": "9.9"}),
                 "state": {"values": {"cq_dismiss_note": {"v": {"value": "  "}}}}}
        ack2 = MagicMock()
        app_module.handle_cq_dismiss_submit(ack2, {"user": {"id": HARRISON}}, client, dview)
        assert ack2.call_args.kwargs["response_action"] == "errors"
        assert cq.get_item(cid)["status"] == "APPROVED"


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


class TestAllocatorSynthetic:
    """Synthetic (qenv) allocator / listing pins -- not live reads."""

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


class TestParkedIsNotASilencer:
    """D-051 lens B HIGH #2 + MED #4/#5: a park cannot swallow a re-ask, a recurrence
    is its trigger, Later cannot replace it, and a bundle-less row has no dead
    Mark-shipped button."""

    def test_explicit_reask_on_a_parked_row_mints_a_fresh_row(self, qenv, monkeypatch):
        monkeypatch.setenv("CORA_CODE_QUEUE", "log")
        first, _ = cq.queue_explicit(HARRISON, "F3E", "C1", "please add retry to uploads", True)
        assert cq.park_item(first, HARRISON, "later", until=(cq._now().date() + timedelta(days=10)).isoformat())[0] == "parked"
        second, outcome = cq.queue_explicit(HARRISON, "F3E", "C1", "please add retry to uploads", True)
        assert outcome == "ok" and second != first
        assert cq.get_item(first)["status"] == "PARKED" and cq.get_item(second)["status"] == "APPROVED"

    def test_passive_recurrence_on_a_parked_row_makes_it_due(self, qenv):
        cid = _seed("parked recurring", status="APPROVED")
        cq.park_item(cid, HARRISON, "later", until=(cq._now().date() + timedelta(days=30)).isoformat())
        carried = {}
        cq.build_weekly_menu(carried_out=carried)
        assert cid in carried["parked_waiting"]
        cq._append_event({"event": "recurrence", "ts": cq._now_iso(), "id": cid,
                          "evidence": {"channel_id": "C1", "ts": "1.2", "note": "again"}})
        assert cq.get_item(cid)["parked_recurrences"] == 1
        carried = {}
        text, blocks = cq.build_weekly_menu(carried_out=carried)
        assert cid in carried["parked_due"] and "re-asked x1 since parked" in text
        assert [e for e in _actions(blocks, cq.ACTION_APPROVE) if e["value"] == cid]

    def test_later_refuses_parked_and_terminal_rows(self, qenv):
        cid = _seed("later on parked", severity="P3")
        cq.park_item(cid, HARRISON, "vendor replies", trigger_event="vendor replies")
        o, msg = cq.process_queue_action(cq.ACTION_LATER, cid, HARRISON)
        assert o == "noop" and "PARKED" in msg
        rec = cq.get_item(cid)
        assert rec["status"] == "PARKED" and rec["park_event"] == "vendor replies"
        shipped = _seed("later on shipped", status="APPROVED")
        cq._append_event({"event": "shipped", "ts": cq._now_iso(), "id": shipped, "bundle_id": "b"})
        o, msg = cq.process_queue_action(cq.ACTION_LATER, shipped, HARRISON)
        assert o == "noop" and cq.get_item(shipped)["status"] == "SHIPPED"

    def test_no_mark_shipped_button_without_a_bundle_and_the_refusal_names_the_verb(self, qenv):
        # a stale STAGED row with no bundle (legacy shape) and one staged with a bundle
        legacy = _seed("legacy stale", status="APPROVED")
        cq._append_event({"event": "staged", "ts": (cq._now() - timedelta(days=20)).isoformat(),
                          "id": legacy, "prompt_path": "G:/x.md"})
        bundled = _seed("bundled stale", status="APPROVED", sub="other-sub")
        cq._append_event({"event": "staged", "ts": (cq._now() - timedelta(days=20)).isoformat(),
                          "id": bundled, "prompt_path": "G:/y.md", "bundle_id": "bnd-1"})
        text, blocks = cq.build_weekly_menu()
        ship_values = {e["value"] for e in _actions(blocks, cq.ACTION_MARK_SHIPPED)}
        assert bundled in ship_values and legacy not in ship_values
        assert f"ship {legacy} <bundle-or-branch>" in text
        o, msg = cq.process_queue_action(cq.ACTION_MARK_SHIPPED, legacy, HARRISON)
        assert o == "refused" and "predates bundle linkage" in msg and f"ship {legacy}" in msg
        o2, _ = cq.process_queue_action(cq.ACTION_MARK_SHIPPED, bundled, HARRISON)
        assert o2 == "shipped"

    def test_titles_are_mrkdwn_escaped_and_the_parked_listing_is_whole_line(self, qenv):
        cid = _seed("Fix <b>bold</b> & <i>", status="APPROVED")
        text, blocks = cq.build_weekly_menu()
        assert "&lt;b&gt;bold&lt;/b&gt; &amp; &lt;i&gt;" in text and "<b>" not in text
        # fully random 192-char titles: a shared prefix would fuzzy-dedup at 0.85
        parked = [_seed(uuid.uuid4().hex * 6, status="APPROVED", sub=f"p{i}") for i in range(16)]
        fut = (cq._now().date() + timedelta(days=30)).isoformat()
        for p in parked:
            cq.park_item(p, HARRISON, "later", until=fut)
        carried = {}
        text, blocks = cq.build_weekly_menu(carried_out=carried)
        section = next(b["text"]["text"] for b in blocks if b.get("type") == "section"
                       and b["text"]["text"].startswith("*Parked (16)"))
        assert len(section) <= 3000
        assert "more parked rows in the backlog -- nothing dropped" in section
        assert all(ln.startswith(("*Parked", "•", "_+")) for ln in section.splitlines())  # whole lines only

    def test_carried_artifact_has_a_stable_schema(self, qenv):
        _seed("schema", status="APPROVED")
        carried = {}
        cq.build_weekly_menu(carried_out=carried)
        assert carried["stale_overflow"] == 0 and carried["proposed_overflow"] == 0
