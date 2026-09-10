"""C7 -- bundle-linkage HARD GATE (Code #12; ruled 2026-09-02, audit F3).

The 9/2 audit: 10 of 234 items carried a bundle_id (4%), 1 of 115 SHIPPED rows;
the `shipped` event schema was exactly {event, ts, id}; 102 of 115 SHIPPED rows
never passed STAGED (seed-then-ship). "Was this staged prompt captured into a
bundle?" could not be answered from the system of record.

Contract under test:
  * ACTION_MARK_SHIPPED with no bundle reference (neither passed nor on the row)
    is REFUSED -- including the seed-then-ship path;
  * a reconcile-script call passing bundle_id + branch (+ commit) writes them on
    the shipped event and the fold carries them;
  * a Slack tap (no args) ships a row that was staged inside a bundle
    (stage_bundle) or alone (ensure_kickoff_staged / record_staged -> solo-<id>);
  * re-shipping is a no-op; historical rows are NOT back-filled;
  * `reconciled` provenance events fold onto the row instead of being dropped.
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


def _seed(title="x", status="APPROVED", severity="P2"):
    return cq.seed_item(kind="bug", severity=severity, title=title, summary="s",
                        entity="F3E", signal="explicit", status=status)


class TestGate:
    def test_seed_then_ship_without_reference_is_refused(self, qenv):
        cid = _seed(title="seed then ship")
        outcome, msg = cq.process_queue_action(cq.ACTION_MARK_SHIPPED, cid, HARRISON)
        assert outcome == "refused"
        assert "no bundle/branch reference" in msg and "step-7.5" in msg
        assert cq.get_item(cid)["status"] == "APPROVED"  # nothing moved

    def test_reconcile_script_call_carries_bundle_branch_commit(self, qenv):
        cid = _seed(title="reconciled ship")
        outcome, msg = cq.process_queue_action(
            cq.ACTION_MARK_SHIPPED, cid, HARRISON,
            bundle_id="code-12", branch="claude/code-12-queue-metabolism-2026-09-09", commit="abc1234")
        assert outcome == "shipped" and "code-12" in msg and "abc1234" in msg
        rec = cq.get_item(cid)
        assert rec["status"] == "SHIPPED"
        assert rec["bundle_id"] == "code-12"
        assert rec["branch"] == "claude/code-12-queue-metabolism-2026-09-09"
        assert rec["commit"] == "abc1234" and rec.get("shipped_at")
        ev = [e for e in cq._read_jsonl(cq._EVENT_LEDGER) if e.get("event") == "shipped"][0]
        assert {"event", "ts", "id", "bundle_id", "branch", "commit"} <= set(ev)

    def test_tap_ships_a_row_staged_inside_a_bundle(self, qenv):
        ids = [_seed(title=f"b {i}") for i in range(2)]
        outcome, _ = cq.stage_bundle("bundle:" + ",".join(ids), HARRISON)
        assert outcome == "staged"
        bid = cq.get_item(ids[0])["bundle_id"]
        assert bid.startswith("bnd-")
        outcome2, msg2 = cq.process_queue_action(cq.ACTION_MARK_SHIPPED, ids[0], HARRISON)
        assert outcome2 == "shipped" and bid in msg2
        assert cq.get_item(ids[0])["bundle_id"] == bid

    def test_solo_stage_names_its_bundle_so_a_tap_can_ship(self, qenv):
        cid = _seed(title="solo staged")
        outcome, _ = cq.stage_by_id(cid, HARRISON)
        assert outcome == "staged"
        assert cq.get_item(cid)["bundle_id"] == f"solo-{cid}"
        outcome2, msg2 = cq.process_queue_action(cq.ACTION_MARK_SHIPPED, cid, HARRISON)
        assert outcome2 == "shipped" and f"solo-{cid}" in msg2

    def test_record_staged_names_its_bundle(self, qenv):
        cid = _seed(title="external kickoff")
        outcome, _ = cq.record_staged(cid, "G:/notes/hand-written.md", HARRISON)
        assert outcome == "staged"
        assert cq.get_item(cid)["bundle_id"] == f"solo-{cid}"

    def test_explicit_bundle_overrides_the_staging_bundle_on_ship(self, qenv):
        cid = _seed(title="staged solo, shipped by a session")
        cq.stage_by_id(cid, HARRISON)
        cq.process_queue_action(cq.ACTION_MARK_SHIPPED, cid, HARRISON, bundle_id="code-12",
                                branch="claude/x")
        rec = cq.get_item(cid)
        assert rec["bundle_id"] == "code-12" and rec["branch"] == "claude/x"

    def test_reship_is_a_noop(self, qenv):
        cid = _seed(title="ship twice")
        cq.process_queue_action(cq.ACTION_MARK_SHIPPED, cid, HARRISON, bundle_id="code-12")
        outcome, msg = cq.process_queue_action(cq.ACTION_MARK_SHIPPED, cid, HARRISON, bundle_id="code-12")
        assert outcome == "noop" and "Already shipped" in msg
        assert sum(1 for e in cq._read_jsonl(cq._EVENT_LEDGER) if e.get("event") == "shipped") == 1

    def test_legacy_staged_row_without_bundle_is_refused_not_backfilled(self, qenv):
        """A pre-C7 STAGED row (staged event with no bundle_id) has nothing to ship
        with -- the gate refuses and invents nothing (back-fill is out of scope)."""
        cid = _seed(title="legacy staged")
        cq._append_event({"event": "staged", "ts": cq._now_iso(), "id": cid, "prompt_path": "/p"})
        rec = cq.get_item(cid)
        assert rec["status"] == "STAGED" and not rec.get("bundle_id")
        outcome, msg = cq.process_queue_action(cq.ACTION_MARK_SHIPPED, cid, HARRISON)
        assert outcome == "refused"
        # D-051 lens B MED #4: the refusal names the one door such a row has -- there
        # is no reconcile script for a row staged before bundle linkage existed
        assert "predates bundle linkage" in msg and f"ship {cid} <bundle-or-branch>" in msg
        assert cq.get_item(cid)["status"] == "STAGED"

    def test_non_founder_still_not_authorized(self, qenv):
        cid = _seed(title="tommy ships")
        outcome, _ = cq.process_queue_action(cq.ACTION_MARK_SHIPPED, cid, "U_TOMMY", bundle_id="code-12")
        assert outcome == "not_authorized"


class TestReconciledProvenanceFold:
    def test_reconciled_events_fold_onto_the_row(self, qenv):
        cid = _seed(title="ingest-integrity seed")
        cq._append_event({"event": "shipped", "ts": cq._now_iso(), "id": cid})  # legacy shape
        cq._append_event({"event": "reconciled", "ts": cq._now_iso(), "id": cid,
                          "transition": "SHIPPED", "bundle_id": "ingest-integrity-2026-09",
                          "branch": "claude/ingest-integrity-2026-09-08", "commit": "8682ff2"})
        rec = cq.get_item(cid)
        assert rec["status"] == "SHIPPED"
        assert rec["reconciled"][0]["bundle_id"] == "ingest-integrity-2026-09"
        assert rec["bundle_id"] == "ingest-integrity-2026-09"  # filled from the record, never overwritten
        assert rec["commit"] == "8682ff2"

    def test_live_ledger_reconciled_rows_are_readable(self):
        """The 8 `reconciled` provenance rows the 9/8 script wrote fold instead of
        vanishing (read-only on the real ledger; skipped when absent)."""
        real = cq._DEFAULT_EVENT_LEDGER
        if not real.exists():
            pytest.skip("no live ledger on this host")
        evs = cq._read_jsonl(real)
        recon_ids = {e["id"] for e in evs if e.get("event") == "reconciled"}
        if not recon_ids:
            pytest.skip("no reconciled rows in the live ledger")
        items = {}
        # replay the production reducer over the live file without touching it
        import cora.code_queue as mod
        saved = mod._EVENT_LEDGER
        try:
            mod._EVENT_LEDGER = real
            items = mod._fold_items()
        finally:
            mod._EVENT_LEDGER = saved
        for rid in recon_ids:
            if rid in items:
                assert items[rid].get("reconciled"), rid
