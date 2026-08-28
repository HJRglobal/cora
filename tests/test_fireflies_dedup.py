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
        "duration": 30,   # MINUTES (Fireflies unit), not seconds
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
# Multi-organizer: one meeting captured by two notetakers -> DIFFERENT links (WS13)
# ---------------------------------------------------------------------------

class TestMultiOrganizerDedup:
    def test_dedup_keys_includes_both_link_and_title(self):
        t = _t("a", title="Ops", date_ts=BASE, link="https://zoom/A",
               attendees=[{"email": "a@x.com"}])
        keys = ffc._meeting_dedup_keys(t)
        assert ("link", "https://zoom/a") in keys
        assert any(k[0] == "title" for k in keys)

    def test_different_links_same_title_collapses(self):
        a = _t("a", title="13 WCF Review", date_ts=BASE, link="https://zoom/A", n_sentences=3,
               attendees=[{"email": "x@hjr.com"}, {"email": "y@hjr.com"}])
        b = _t("b", title="13 WCF Review", date_ts=BASE + 90, link="https://meet/B", n_sentences=80,
               attendees=[{"email": "x@hjr.com"}, {"email": "y@hjr.com"}])
        winners, _, collapsed = ffc._dedup_transcripts([a, b], {})
        assert collapsed == 1
        assert [w["id"] for w in winners] == ["b"]  # most complete kept

    def test_different_links_same_title_outside_window_both_kept(self):
        a = _t("a", title="13 WCF Review", date_ts=BASE, link="https://zoom/A",
               attendees=[{"email": "x@hjr.com"}])
        b = _t("b", title="13 WCF Review", date_ts=BASE + 1200, link="https://meet/B",
               attendees=[{"email": "x@hjr.com"}])  # 20 min apart > tolerance
        winners, _, collapsed = ffc._dedup_transcripts([a, b], {})
        assert collapsed == 0
        assert sorted(w["id"] for w in winners) == ["a", "b"]

    def test_different_links_different_titles_not_merged(self):
        a = _t("a", title="F3 Weekly", date_ts=BASE, link="https://zoom/A",
               attendees=[{"email": "x@hjr.com"}])
        b = _t("b", title="OSN Recon", date_ts=BASE + 60, link="https://meet/B",
               attendees=[{"email": "z@hjr.com"}])
        winners, _, collapsed = ffc._dedup_transcripts([a, b], {})
        assert collapsed == 0
        assert sorted(w["id"] for w in winners) == ["a", "b"]

    def test_empty_title_no_attendees_no_link_never_merges(self):
        a = {"id": "a", "title": "", "date": BASE, "meeting_link": None,
             "summary": {"overview": "", "action_items": ""}, "sentences": [], "meeting_attendees": []}
        b = {"id": "b", "title": "", "date": BASE + 30, "meeting_link": None,
             "summary": {"overview": "", "action_items": ""}, "sentences": [], "meeting_attendees": []}
        winners, _, collapsed = ffc._dedup_transcripts([a, b], {})
        assert collapsed == 0   # degenerate ("solo", id) keys never cross-match
        assert sorted(w["id"] for w in winners) == ["a", "b"]

    def test_different_links_same_title_outside_tight_window_not_merged(self):
        """Two DIFFERENT meetings, same generic title + same attendees, different
        links, 4 min apart (inside +/-5min but OUTSIDE the +/-3min title-merge
        window) -> must NOT collapse (a title-only cross-link match needs tight
        time coincidence; else one meeting's transcript would be silently dropped)."""
        a = _t("a", title="Weekly Sync", date_ts=BASE, link="https://zoom/A",
               attendees=[{"email": "x@hjr.com"}, {"email": "y@hjr.com"}])
        b = _t("b", title="Weekly Sync", date_ts=BASE + 240, link="https://meet/B",
               attendees=[{"email": "x@hjr.com"}, {"email": "y@hjr.com"}])
        winners, _, collapsed = ffc._dedup_transcripts([a, b], {})
        assert collapsed == 0
        assert sorted(w["id"] for w in winners) == ["a", "b"]

    def test_no_transitive_bridge_via_borrowed_link(self):
        """A(L1,Weekly) and B(L2,Weekly) collapse via the title key (tight window);
        a third transcript C(L2, a DIFFERENT title) must NOT be pulled in via a
        'borrowed' L2 key — cluster keys are the anchor's only (no accumulation)."""
        a = _t("a", title="Weekly Sync", date_ts=BASE, link="https://zoom/L1", n_sentences=3,
               attendees=[{"email": "x@hjr.com"}])
        b = _t("b", title="Weekly Sync", date_ts=BASE + 60, link="https://zoom/L2", n_sentences=99,
               attendees=[{"email": "x@hjr.com"}])
        c = _t("c", title="OSN Recon", date_ts=BASE + 120, link="https://zoom/L2",
               attendees=[{"email": "z@hjr.com"}])
        winners, _, collapsed = ffc._dedup_transcripts([a, b, c], {})
        assert collapsed == 1                     # only B collapses into A
        assert "c" in [w["id"] for w in winners]  # C never bridged in
        assert sorted(w["id"] for w in winners) == ["b", "c"]  # B is most complete


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


# ── fred_joined canonical selection (2026-08-27, cq-ffcf6e4ffe7c) ────────────
# D-247 amendment: when a duplicate pair exists the `fred_joined: true` copy --
# the one the calendar-dispatched bot produced -- is canonical. Guarded by a
# content floor, because on real August data the literal rule would have chosen a
# 0-sentence transcript over an 1,158-sentence one for "Harrison x Finance Weekly".

def _fred(t, value=True):
    """Attach the nested meeting_info shape Fireflies actually returns.

    fred_joined lives on `meeting_info`, NOT at the top level of Transcript
    (verified by live schema introspection 2026-08-27).
    """
    t["meeting_info"] = {"fred_joined": value, "silent_meeting": False}
    return t


class TestFredJoinedCanonical:
    def test_fred_flag_reads_only_literal_true(self):
        assert ffc._fred_joined(_fred(_t("a", date_ts=BASE), True)) is True
        assert ffc._fred_joined(_fred(_t("a", date_ts=BASE), False)) is False
        # None is the COMMON case (~half of live transcripts) and must not read
        # as a claim that the bot joined.
        assert ffc._fred_joined(_fred(_t("a", date_ts=BASE), None)) is False
        assert ffc._fred_joined(_t("a", date_ts=BASE)) is False          # key absent
        assert ffc._fred_joined({"meeting_info": None}) is False          # explicit null

    def test_fred_copy_wins_over_more_complete_copy(self):
        """The headline rule, and the real 'Big D Media' case: on 2026-08-26 the
        completeness rule kept an 8-minute copy over the 20-minute fred copy."""
        fred = _fred(_t("fred", date_ts=BASE, link="L", n_sentences=129,
                        action_items="short"))
        other = _t("other", date_ts=BASE, link="L", n_sentences=129,
                   action_items="a much longer action item list " * 40)
        winner, reason = ffc._pick_canonical([other, fred])
        assert winner["id"] == "fred"
        assert reason == "fred_joined"

    def test_empty_fred_copy_never_beats_a_real_transcript(self):
        """The measured pathological case -- 0 sentences vs 1,158. Selecting the
        fred copy here would silently empty the meeting out of the KB."""
        fred = _fred(_t("fred", date_ts=BASE, link="L", n_sentences=0))
        full = _t("full", date_ts=BASE, link="L", n_sentences=1158)
        winner, reason = ffc._pick_canonical([full, fred])
        assert winner["id"] == "full"
        assert reason == "completeness-over-empty-fred"

    def test_slightly_shorter_fred_copy_still_wins(self):
        """Real clusters sat at 0.89-1.00 sentence ratio; the floor must not fire
        there or the rule would be dead on arrival for the normal case."""
        fred = _fred(_t("fred", date_ts=BASE, link="L", n_sentences=810))
        other = _t("other", date_ts=BASE, link="L", n_sentences=907)
        winner, reason = ffc._pick_canonical([other, fred])
        assert winner["id"] == "fred"
        assert reason == "fred_joined"

    def test_no_fred_copy_falls_back_to_completeness_unchanged(self):
        a = _t("a", date_ts=BASE, link="L", n_sentences=5)
        b = _t("b", date_ts=BASE, link="L", n_sentences=9)
        winner, reason = ffc._pick_canonical([a, b])
        assert winner["id"] == "b"
        assert reason == "completeness"

    def test_multiple_fred_copies_rank_among_themselves(self):
        f1 = _fred(_t("f1", date_ts=BASE, link="L", n_sentences=10))
        f2 = _fred(_t("f2", date_ts=BASE, link="L", n_sentences=40))
        plain = _t("p", date_ts=BASE, link="L", n_sentences=41)
        winner, reason = ffc._pick_canonical([f1, plain, f2])
        assert winner["id"] == "f2"
        assert reason == "fred_joined"

    def test_single_member_cluster_is_returned_as_is(self):
        solo = _fred(_t("solo", date_ts=BASE, link="L", n_sentences=3))
        winner, _ = ffc._pick_canonical([solo])
        assert winner["id"] == "solo"

    def test_dedup_end_to_end_prefers_fred_and_records_reason(self):
        fred = _fred(_t("fred", date_ts=BASE, link="L", n_sentences=100))
        other = _t("other", date_ts=BASE, link="L", n_sentences=140)
        winners, ledger, collapsed = ffc._dedup_transcripts([other, fred], {})
        assert collapsed == 1
        assert [w["id"] for w in winners] == ["fred"]
        entry = next(iter(ledger.values()))
        assert entry["canonical_id"] == "fred"
        assert entry["collapsed_ids"] == ["other"]
        assert entry["reason"] == "fred_joined"

    def test_all_completeness_dimensions_zero_does_not_divide_by_zero(self):
        """Both copies empty: the ratio guard divides by best_sentences, so it must
        be skipped rather than raising. fred still wins; the reason here is
        'fred+completeness' because with equal (zero) content the id tiebreak also
        lands on it, i.e. the two rules agree."""
        fred = _fred(_t("fred", date_ts=BASE, link="L", n_sentences=0))
        other = _t("other", date_ts=BASE, link="L", n_sentences=0)
        winner, reason = ffc._pick_canonical([other, fred])
        assert winner["id"] == "fred"
        assert reason.startswith("fred")

    def test_zero_sentence_guard_still_fires_when_fred_loses_the_tiebreak(self):
        """Same empty-fred hazard, but with an id that sorts AFTER the full copy --
        so the winner cannot come from the tiebreak and the floor is what saves it."""
        fred = _fred(_t("zzz", date_ts=BASE, link="L", n_sentences=0))
        full = _t("aaa", date_ts=BASE, link="L", n_sentences=900)
        winner, reason = ffc._pick_canonical([fred, full])
        assert winner["id"] == "aaa"
        assert reason == "completeness-over-empty-fred"


class TestExtendedTranscriptQuery:
    """The extended selection set must never be able to take the nightly KB
    ingest dark -- Fireflies fails the WHOLE query on one unknown field."""

    def test_extended_query_requests_the_capture_lane_fields(self):
        q = ffc._TRANSCRIPTS_QUERY
        assert "cal_id" in q and "calendar_id" in q
        assert "meeting_info" in q and "fred_joined" in q

    def test_legacy_query_is_a_real_stripped_query_not_a_copy(self):
        legacy = ffc._TRANSCRIPTS_QUERY_LEGACY
        assert legacy != ffc._TRANSCRIPTS_QUERY
        for tok in ("cal_id", "calendar_id", "meeting_info", "fred_joined"):
            assert tok not in legacy, f"{tok} survived the strip -- fallback is a no-op"
        # still a usable query
        assert "meeting_link" in legacy and "sentences" in legacy
        assert legacy.count("{") == legacy.count("}")

    def test_validation_error_falls_back_to_legacy(self, monkeypatch):
        monkeypatch.setattr(ffc, "_extended_query_unavailable", False)
        seen: list[str] = []

        def fake(query, variables=None):
            seen.append(query)
            if query is ffc._TRANSCRIPTS_QUERY:
                raise ffc.FirefliesConnectorError(
                    'Fireflies 400: Cannot query field "cal_id" on type "Transcript". '
                    'code: GRAPHQL_VALIDATION_FAILED'
                )
            return {"transcripts": [{"id": "x"}]}

        monkeypatch.setattr(ffc, "_graphql_query", fake)
        out = ffc._query_transcripts({"limit": 1})
        assert out["transcripts"][0]["id"] == "x"
        assert len(seen) == 2 and seen[1] is ffc._TRANSCRIPTS_QUERY_LEGACY
        # sticky: the next call must not re-attempt the extended shape
        seen.clear()
        ffc._query_transcripts({"limit": 1})
        assert seen == [ffc._TRANSCRIPTS_QUERY_LEGACY]

    def test_auth_and_transport_errors_are_re_raised_not_swallowed(self, monkeypatch):
        """A 401 or a timeout is a real outage. Degrading on it would hide the
        outage behind a green, quietly-wrong run."""
        monkeypatch.setattr(ffc, "_extended_query_unavailable", False)
        calls: list[str] = []

        def fake(query, variables=None):
            calls.append(query)
            raise ffc.FirefliesConnectorError("Fireflies 401: Unauthorized")

        monkeypatch.setattr(ffc, "_graphql_query", fake)
        with pytest.raises(ffc.FirefliesConnectorError, match="401"):
            ffc._query_transcripts({"limit": 1})
        assert len(calls) == 1, "must not retry a non-validation failure"
        assert ffc._extended_query_unavailable is False
