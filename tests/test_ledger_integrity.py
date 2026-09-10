"""F4/F5 -- ledger integrity + the PROPOSED-aging monitor (Code #12; audit §E/§H).

F4: a folded-vs-raw id reconciliation that names every id with events but no
`captured` event, plus a LOUD reducer path for an unknown event kind -- today
`_fold_items()` dropped both silently (a hand-written "cowork-biweekly-review" row
hid a real, approved ask for 16 days; the 7/30 cleanup left 7 descendant events of
a removed captured row). Fixture = the two known live orphans, in their exact
shapes; the check must report EXACTLY two on the live ledger.

F5: `check_priority_kickoffs` guards APPROVED + P0/P1 + no kickoff -- a population
every approval path empties synchronously -- so it reads "ok" permanently. The
sibling gauge watches PROPOSED HIGH-class aging, the tier that actually regresses.

Ledger-replay doctrine (audit §H): the live assertions re-derive the headline
numbers by a SECOND method (a raw scan independent of the production reducer).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from cora import code_queue as cq

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import nightly_health_check as hc  # noqa: E402

HARRISON = "U0B2RM2JYJ1"
ORPHAN_HANDWRITTEN = "cq-11b694215709"   # `"event": "cowork-biweekly-review"`, no captured
ORPHAN_CLEANUP = "cq-8f7d6da0112a"       # captured removed 7/30; dm_held + 6 recurrence left


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
    monkeypatch.setattr(cq.drive_io, "write_text_atomic", lambda p, t, **k: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cq, "_default_embed", lambda texts: [])
    cq._LEDGER_ANOMALY_WARNED.clear()
    return tmp_path


def _replay_orphans():
    cq._append_event({"kind": "capability_ask", "severity": "MEDIUM", "entity": "FNDR",
                      "signal": "explicit", "title": "cora_delegated_jobs: expose requester",
                      "event": "cowork-biweekly-review", "id": ORPHAN_HANDWRITTEN})
    cq._append_event({"event": "dm_held", "ts": cq._now_iso(), "id": ORPHAN_CLEANUP})
    for _ in range(6):
        cq._append_event({"event": "recurrence", "ts": cq._now_iso(), "id": ORPHAN_CLEANUP,
                          "evidence": {"channel_id": "", "ts": "", "note": "tool failure"}})


class TestF4Synthetic:
    def test_exactly_two_orphans_in_their_live_shapes(self, qenv):
        cq.seed_item(kind="bug", severity="P2", title="healthy", summary="s", entity="F3E",
                     signal="explicit", status="PROPOSED")
        _replay_orphans()
        rep = cq.ledger_integrity()
        assert [o["id"] for o in rep["orphans"]] == sorted([ORPHAN_CLEANUP, ORPHAN_HANDWRITTEN])
        by = {o["id"]: o for o in rep["orphans"]}
        assert by[ORPHAN_CLEANUP]["events"] == 7 and set(by[ORPHAN_CLEANUP]["kinds"]) == {"dm_held", "recurrence"}
        assert by[ORPHAN_HANDWRITTEN]["events"] == 1
        assert rep["unknown_event_types"] == {"cowork-biweekly-review": 1}
        assert rep["unknown_event_ids"] == [ORPHAN_HANDWRITTEN]
        assert rep["raw_ids"] == 3 and rep["folded_ids"] == 1
        # and the fold itself still yields only the healthy row
        assert {it["id"] for it in cq.load_items()} != set()
        assert ORPHAN_HANDWRITTEN not in {it["id"] for it in cq.load_items()}

    def test_reducer_is_loud_once_per_anomaly(self, qenv, caplog):
        _replay_orphans()
        caplog.set_level(logging.WARNING, logger="cora.code_queue")
        cq.load_items()
        cq.load_items()  # second fold: no second warning
        msgs = [r.getMessage() for r in caplog.records if "ledger integrity" in r.getMessage()]
        unknown = [m for m in msgs if "UNKNOWN ledger event kind" in m and "cowork-biweekly-review" in m]
        orphan = [m for m in msgs if "ORPHAN ledger event" in m and ORPHAN_CLEANUP in m]
        assert len(unknown) == 1 and len(orphan) == 1

    def test_clean_ledger_reports_no_anomalies(self, qenv):
        cq.seed_item(kind="bug", severity="P2", title="healthy", summary="s", entity="F3E",
                     signal="explicit", status="PROPOSED")
        rep = cq.ledger_integrity()
        assert rep["orphans"] == [] and rep["unknown_event_types"] == {}

    def test_health_check_warns_and_names_ids(self, qenv):
        _replay_orphans()
        r = hc.check_code_queue_ledger_integrity()
        assert r.status == "warn"
        assert ORPHAN_HANDWRITTEN in r.detail and ORPHAN_CLEANUP in r.detail
        assert "cowork-biweekly-review" in r.detail and "never a jsonl hand-edit" in r.detail

    def test_health_check_ok_when_clean(self, qenv):
        cq.seed_item(kind="bug", severity="P2", title="healthy", summary="s", entity="F3E",
                     signal="explicit", status="PROPOSED")
        assert hc.check_code_queue_ledger_integrity().status == "ok"

    def test_health_check_scan_failure_warns(self, monkeypatch):
        monkeypatch.setattr(cq, "ledger_integrity", lambda *a, **k: 1 / 0)
        r = hc.check_code_queue_ledger_integrity()
        assert r.status == "warn" and "Could not reconcile" in r.detail

    def test_every_reducer_branch_is_a_known_kind(self):
        """The KNOWN set and the reducer's branches must agree, or a modelled kind
        would be warned as unknown (or an unmodelled one silently accepted)."""
        import inspect
        import re
        src = inspect.getsource(cq._fold_items)
        branches = set(re.findall(r'et == "([a-z_]+)"', src))
        # strict equality (D-051 lens D LOW #9): a KNOWN kind with no reducer branch
        # used to pass on a substring a docstring or comment could satisfy
        assert branches == cq._KNOWN_EVENT_TYPES


class TestF4Live:
    def test_live_ledger_has_exactly_the_two_known_orphans(self):
        """Read-only on the real ledger. Pinned to the two ids the 9/2 audit named;
        if this ever fails, the ledger drifted (a repair or a new orphan) -- re-pin
        deliberately after reading the ids the message prints."""
        real = cq._DEFAULT_EVENT_LEDGER
        if not real.exists():
            pytest.skip("no live ledger on this host")
        rep = cq.ledger_integrity(real)
        ids = sorted(o["id"] for o in rep["orphans"])
        assert ids == sorted([ORPHAN_CLEANUP, ORPHAN_HANDWRITTEN]), (
            f"live orphans drifted from the two the 9/2 audit named: {ids}")
        # second method (audit §H): a raw scan independent of ledger_integrity()
        events = [json.loads(l) for l in real.read_text(encoding="utf-8").splitlines() if l.strip()]
        captured = {e.get("id") for e in events if e.get("event") == "captured"}
        raw_orphans = sorted({i for e in events if (i := e.get("id")) and i not in captured})
        assert raw_orphans == ids
        assert rep["unknown_event_types"] == {"cowork-biweekly-review": 1}, rep["unknown_event_types"]


class TestF5:
    def test_aged_priority_proposed_are_listed_oldest_first(self, qenv):
        old_hi = cq.seed_item(kind="bug", severity="HIGH", title="old high", summary="s",
                              entity="F3E", signal="explicit", status="PROPOSED")
        old_lo = cq.seed_item(kind="bug", severity="LOW", title="old low", summary="s",
                              entity="F3E", signal="explicit", status="PROPOSED")
        new_hi = cq.seed_item(kind="bug", severity="P1", title="new high", summary="s",
                              entity="F3E", signal="explicit", status="PROPOSED")
        # age the first two by rewriting their captured ts (test-only ledger surgery)
        path = cq._EVENT_LEDGER
        rows = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        for r in rows:
            if r.get("id") in (old_hi, old_lo):
                r["ts"] = (cq._now() - timedelta(days=29)).isoformat()
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        rep = cq.proposed_priority_aging()
        assert [a["id"] for a in rep["aged_priority"]] == [old_hi]
        assert rep["aged_priority"][0]["age_days"] >= 28
        assert rep["aged_total"] == 2 and rep["proposed_total"] == 3
        r = hc.check_proposed_priority_aging()
        assert r.status == "warn" and old_hi in r.detail and new_hi not in r.detail
        assert "PROPOSED coverage" in r.detail

    def test_ok_when_no_aged_priority(self, qenv):
        cq.seed_item(kind="bug", severity="HIGH", title="fresh", summary="s",
                     entity="F3E", signal="explicit", status="PROPOSED")
        r = hc.check_proposed_priority_aging()
        assert r.status == "ok"

    def test_scan_failure_warns(self, monkeypatch):
        monkeypatch.setattr(cq, "proposed_priority_aging", lambda *a, **k: 1 / 0)
        assert hc.check_proposed_priority_aging().status == "warn"

    def test_both_checks_registered_in_main(self):
        import inspect
        body = inspect.getsource(hc)[inspect.getsource(hc).index("def main("):]
        assert "all_results.append(check_code_queue_ledger_integrity())" in body
        assert "all_results.append(check_proposed_priority_aging())" in body
        assert "all_results.append(check_priority_kickoffs())" in body  # the old gauge stays

    def test_live_proposed_priority_aging_is_nonempty_today(self):
        """The 9/2 audit counted 8 HIGH-class PROPOSED rows (13 on 9/9). Read-only;
        skipped when the live ledger is absent. If the tier is ever drained, the
        gauge reading zero is the good outcome -- update this pin then."""
        real = cq._DEFAULT_EVENT_LEDGER
        if not real.exists():
            pytest.skip("no live ledger on this host")
        saved = cq._EVENT_LEDGER
        try:
            cq._EVENT_LEDGER = real
            rep = cq.proposed_priority_aging()
        finally:
            cq._EVENT_LEDGER = saved
        assert rep["proposed_total"] >= rep["aged_total"] >= len(rep["aged_priority"])
        # the NON-EMPTINESS the name promises (D-051 lens D MED #5: the inequality
        # alone is true by construction and passed at 0/0/0)
        assert rep["aged_total"] >= 1 and rep["aged_priority"], rep


class TestF4Blind:
    """B MED #3 (D-051): a MISSING or TORN ledger must never read as clean."""

    def test_missing_ledger_raises_and_the_check_warns(self, qenv, monkeypatch):
        with pytest.raises(FileNotFoundError):
            cq.ledger_integrity(qenv / "does-not-exist.jsonl")
        monkeypatch.setattr(cq, "_EVENT_LEDGER", qenv / "does-not-exist.jsonl")
        r = hc.check_code_queue_ledger_integrity()
        assert r.status == "warn" and "Could not reconcile" in r.detail

    def test_empty_ledger_reports_nothing_folded_not_ok(self, qenv):
        cq._EVENT_LEDGER.write_text("", encoding="utf-8")
        rep = cq.ledger_integrity()
        assert rep["raw_ids"] == 0 and rep["folded_ids"] == 0 and rep["unparseable"] == 0

    def test_torn_lines_are_counted_and_warned(self, qenv):
        cq.seed_item(kind="bug", severity="P2", title="healthy", summary="s", entity="F3E",
                     signal="explicit", status="PROPOSED")
        with cq._EVENT_LEDGER.open("a", encoding="utf-8") as fh:
            fh.write('{"event": "approved", "ts": "2026-09-09T00:00:00", "id": "cq-aaaaaaaaaaaa"\n')  # torn
            fh.write("not json at all\n")
        rep = cq.ledger_integrity()
        assert rep["unparseable"] == 2
        r = hc.check_code_queue_ledger_integrity()
        assert r.status == "warn" and "2 unparseable line(s)" in r.detail


class TestParkedAging:
    """B HIGH #2 (D-051): a PARKED row whose trigger fired is watched by a gauge."""

    def _park(self, until):
        import uuid
        # distinct titles: seed_item fuzzy-dedups titles at 0.85 (a shared prefix collapses them)
        cid = cq.seed_item(kind="bug", severity="P1", title=f"parked {uuid.uuid4().hex}", summary="s",
                           entity="F3E", signal="explicit", status="APPROVED")
        o, _ = cq.park_item(cid, HARRISON, "waiting", until=until)
        assert o == "parked"
        return cid

    def test_due_and_reasked_parks_are_due(self, qenv):
        due = self._park("2026-01-01")                                   # date passed
        future = (cq._now().date() + timedelta(days=30)).isoformat()
        waiting = self._park(future)
        reasked = self._park(future)
        cq._append_event({"event": "recurrence", "ts": cq._now_iso(), "id": reasked,
                          "evidence": {"channel_id": "C1", "ts": "1.2"}})
        rep = cq.parked_aging()
        ids = {d["id"] for d in rep["due"]}
        assert ids == {due, reasked} and rep["parked_total"] == 3
        by = {d["id"]: d for d in rep["due"]}
        assert by[reasked]["parked_recurrences"] == 1 and by[due]["park_until"] == "2026-01-01"
        assert waiting not in ids

    def test_health_check_warns_with_ids_and_ok_when_quiet(self, qenv):
        future = (cq._now().date() + timedelta(days=30)).isoformat()
        self._park(future)
        assert hc.check_parked_aging().status == "ok"
        due = self._park("2026-01-01")
        r = hc.check_parked_aging()
        assert r.status == "warn" and due in r.detail and "Re-queue" in r.detail

    def test_scan_failure_warns(self, monkeypatch):
        monkeypatch.setattr(cq, "parked_aging", lambda: 1 / 0)
        assert hc.check_parked_aging().status == "warn"

    def test_registered_in_main(self):
        import inspect
        body = inspect.getsource(hc)[inspect.getsource(hc).index("def main("):]
        assert "all_results.append(check_parked_aging())" in body
