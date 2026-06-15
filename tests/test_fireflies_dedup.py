"""Tests for Fireflies duplicate-meeting dedup at KB ingest (2026-06-14).

Multiple attendees' notetakers capture the SAME meeting -> near-identical
transcripts with different ids. We collapse them keyed on (meeting_link,
start_time) within +/-5 min (title+participant fallback), keeping the most
complete copy, with a ledger that makes re-runs idempotent.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.connectors import fireflies_connector as ffc


def _t(tid, *, title="Weekly Sync", date_ts, link=None, n_sentences=5,
       attendees=None, action_items="do a thing"):
    sentences = [{"index": i, "speaker_name": "A", "text": f"line {i}"} for i in range(n_sentences)]
    return {
        "id": tid,
        "title": title,
        "date": date_ts,
        "meeting_link": link,
        "duration": 1800,
        "summary": {"overview": "ov", "action_items": action_items},
        "sentences": sentences,
        "meeting_attendees": attendees or [{"displayName": "A", "email": "a@x.com"}],
    }


BASE = 1_780_000_000  # fixed epoch (seconds) for deterministic windows


# ---------------------------------------------------------------------------
# Keys + completeness
# ---------------------------------------------------------------------------

class TestKeysAndCompleteness:
    def test_link_key_used_when_present(self):
        t = _t("a", date_ts=BASE, link="https://zoom.us/j/abc")
        assert ffc._meeting_dedup_key(t) == ("link", "https://zoom.us/j/abc")

    def test_title_fallback_when_no_link(self):
        t = _t("a", title="Ops Review", date_ts=BASE,
               attendees=[{"displayName": "A", "email": "A@X.com"}])
        key = ffc._meeting_dedup_key(t)
        assert key[0] == "title"
        assert key[1] == "ops review"
        assert key[2] == frozenset({"a@x.com"})

    def test_completeness_orders_by_sentences(self):
        small = _t("a", date_ts=BASE, n_sentences=2)
        big = _t("b", date_ts=BASE, n_sentences=200)
        assert ffc._transcript_completeness(big) > ffc._transcript_completeness(small)


# ---------------------------------------------------------------------------
# Dedup core
# ---------------------------------------------------------------------------

class TestDedup:
    def test_same_link_same_time_collapses_keep_most_complete(self):
        a = _t("a", date_ts=BASE, link="L1", n_sentences=3)
        b = _t("b", date_ts=BASE + 120, link="L1", n_sentences=99)  # within 5 min, more complete
        winners, ledger, collapsed = ffc._dedup_transcripts([a, b], {})
        assert collapsed == 1
        assert [w["id"] for w in winners] == ["b"]
        # ledger records 'a' as collapsed under the canonical 'b'
        assert any("a" in e.get("collapsed_ids", []) for e in ledger.values())

    def test_different_day_same_title_both_kept(self):
        a = _t("a", title="Weekly Sync", date_ts=BASE)
        b = _t("b", title="Weekly Sync", date_ts=BASE + 2 * 86400)  # 2 days later
        winners, ledger, collapsed = ffc._dedup_transcripts([a, b], {})
        assert collapsed == 0
        assert sorted(w["id"] for w in winners) == ["a", "b"]

    def test_outside_tolerance_window_both_kept(self):
        a = _t("a", date_ts=BASE, link="L1")
        b = _t("b", date_ts=BASE + 600, link="L1")  # 10 min apart > tolerance
        winners, _, collapsed = ffc._dedup_transcripts([a, b], {})
        assert collapsed == 0
        assert sorted(w["id"] for w in winners) == ["a", "b"]

    def test_title_fallback_collapses(self):
        a = _t("a", title="Ops", date_ts=BASE, n_sentences=2,
               attendees=[{"displayName": "A", "email": "a@x.com"}])
        b = _t("b", title="Ops", date_ts=BASE + 60, n_sentences=50,
               attendees=[{"displayName": "A", "email": "a@x.com"}])
        winners, _, collapsed = ffc._dedup_transcripts([a, b], {})
        assert collapsed == 1
        assert [w["id"] for w in winners] == ["b"]

    def test_tiebreak_smallest_id(self):
        """Equal completeness -> deterministic smallest-id canonical."""
        a = _t("zzz", date_ts=BASE, link="L1", n_sentences=5)
        b = _t("aaa", date_ts=BASE + 30, link="L1", n_sentences=5)
        winners, _, collapsed = ffc._dedup_transcripts([a, b], {})
        assert collapsed == 1
        assert [w["id"] for w in winners] == ["aaa"]

    def test_idempotent_no_resurrection(self):
        """Re-running with the prior ledger never resurrects a dropped copy."""
        a = _t("a", date_ts=BASE, link="L1", n_sentences=3)
        b = _t("b", date_ts=BASE + 120, link="L1", n_sentences=99)
        winners1, ledger1, _ = ffc._dedup_transcripts([a, b], {})
        assert [w["id"] for w in winners1] == ["b"]
        # second run with both transcripts again + the persisted ledger
        winners2, ledger2, collapsed2 = ffc._dedup_transcripts([a, b], ledger1)
        ids2 = [w["id"] for w in winners2]
        assert "a" not in ids2          # dropped copy not resurrected
        assert ids2 == ["b"]            # canonical still kept (upsert is idempotent)

    def test_single_transcript_passes_through(self):
        a = _t("a", date_ts=BASE, link="L1")
        winners, ledger, collapsed = ffc._dedup_transcripts([a], {})
        assert [w["id"] for w in winners] == ["a"]
        assert collapsed == 0
        assert ledger == {}  # nothing collapsed -> no ledger entry

    def test_transcripts_without_id_ignored(self):
        a = _t("a", date_ts=BASE, link="L1")
        bad = {"title": "no id", "date": BASE, "meeting_link": "L2"}
        winners, _, _ = ffc._dedup_transcripts([a, bad], {})
        assert [w["id"] for w in winners] == ["a"]


# ---------------------------------------------------------------------------
# Ledger persistence
# ---------------------------------------------------------------------------

class TestLedgerIO:
    def test_read_missing_returns_empty(self, tmp_path):
        with patch.object(ffc, "_DEDUP_LEDGER_PATH", tmp_path / "missing.json"):
            assert ffc._read_dedup_ledger() == {}

    def test_round_trip(self, tmp_path):
        path = tmp_path / "ledger.json"
        with patch.object(ffc, "_DEDUP_LEDGER_PATH", path):
            ffc._write_dedup_ledger({"k": {"canonical_id": "x", "collapsed_ids": ["y"], "updated": 1}})
            assert ffc._read_dedup_ledger()["k"]["canonical_id"] == "x"

    def test_corrupt_ledger_returns_empty(self, tmp_path):
        path = tmp_path / "ledger.json"
        path.write_text("not json", encoding="utf-8")
        with patch.object(ffc, "_DEDUP_LEDGER_PATH", path):
            assert ffc._read_dedup_ledger() == {}

    def test_cap_keeps_most_recent(self, tmp_path):
        path = tmp_path / "ledger.json"
        ledger = {f"k{i}": {"canonical_id": str(i), "collapsed_ids": ["c"], "updated": i}
                  for i in range(ffc._DEDUP_LEDGER_MAX + 50)}
        with patch.object(ffc, "_DEDUP_LEDGER_PATH", path):
            ffc._write_dedup_ledger(ledger)
            loaded = ffc._read_dedup_ledger()
        assert len(loaded) == ffc._DEDUP_LEDGER_MAX
        # the most-recent (highest 'updated') entries are kept
        assert f"k{ffc._DEDUP_LEDGER_MAX + 49}" in loaded


# ---------------------------------------------------------------------------
# backfill() end-to-end (dedup applied before yield)
# ---------------------------------------------------------------------------

class TestBackfillDedup:
    def test_backfill_yields_one_per_meeting(self, tmp_path):
        from datetime import datetime, timezone
        a = _t("a", date_ts=BASE, link="L1", n_sentences=3, title="F3 Weekly Review")
        b = _t("b", date_ts=BASE + 120, link="L1", n_sentences=99, title="F3 Weekly Review")
        ledger_path = tmp_path / "ledger.json"

        with (
            patch.object(ffc, "_DEDUP_LEDGER_PATH", ledger_path),
            patch.object(ffc, "_graphql_query", return_value={"transcripts": [a, b]}),
        ):
            docs1 = list(ffc.backfill(datetime(2020, 1, 1, tzinfo=timezone.utc)))
            # second run: same transcripts re-returned, ledger persisted in between
            docs2 = list(ffc.backfill(datetime(2020, 1, 1, tzinfo=timezone.utc)))

        assert [d.source_id for d in docs1] == ["b"]   # canonical only
        assert [d.source_id for d in docs2] == ["b"]   # idempotent: no resurrection of 'a'
