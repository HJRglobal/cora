"""Diarization-collapse canary (slice 3, cq-e63feff3a0bf).

A 77-minute multi-party meeting was ingested with 100% single-speaker labels and
nothing noticed. Everything downstream trusts those labels, so a collapsed
transcript does not look like missing data -- it looks like one person having said
everything in the room.

The live corpus turned out to be far worse than the one reported meeting: the
offline pass over stored content flags **126 of 735 meetings** as fully
collapsed. These tests pin the judgement, both preconditions (so the canary stays
worth reading), the retrieval label, the digest line and the ledger.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.connectors import fireflies_diarization as fd  # noqa: E402


def _transcript(labels, attendees=("a@x.com", "b@x.com"), text="something said"):
    return {
        "sentences": [{"index": i, "speaker_name": n, "text": text}
                      for i, n in enumerate(labels)],
        "meeting_attendees": [{"email": e, "displayName": e} for e in attendees],
    }


def _many(name, n):
    return [name] * n


# ── the judgement ────────────────────────────────────────────────────────────

def test_full_collapse_on_a_multiparty_meeting_is_flagged():
    health = fd.assess(_transcript(_many("Harrison Rogers", 300)))
    assert health.collapsed is True
    assert health.top_speaker_share == 1.0
    assert health.speakers == 1
    assert "Harrison Rogers" in health.reason


def test_partial_collapse_above_the_threshold_is_flagged():
    """95%, not 100%: a handful of correctly-split lines in an otherwise
    single-labelled hour is the same defect."""
    labels = _many("A", 98) + _many("B", 2)
    health = fd.assess(_transcript(labels))
    assert health.collapsed is True
    assert health.top_speaker_share == pytest.approx(0.98)


def test_a_healthy_two_way_conversation_is_not_flagged():
    labels = ["A", "B"] * 60
    health = fd.assess(_transcript(labels))
    assert health.collapsed is False
    assert "healthy" in health.reason


def test_just_below_the_threshold_is_not_flagged():
    labels = _many("A", 94) + _many("B", 6)
    health = fd.assess(_transcript(labels))
    assert health.collapsed is False


def test_a_short_exchange_is_never_flagged():
    """One person doing most of the talking in a short call is normal. Without
    the floor the canary would cry on every stand-up."""
    health = fd.assess(_transcript(_many("A", 10)))
    assert health.collapsed is False
    assert "floor" in health.reason


def test_a_genuinely_solo_recording_is_never_flagged():
    """The multi-party precondition. A solo memo IS one speaker."""
    health = fd.assess(_transcript(_many("A", 300), attendees=("a@x.com",)))
    assert health.collapsed is False
    assert "single-speaker is expected" in health.reason


def test_party_count_never_comes_from_the_labels_under_test():
    """If the party count were inferred from speaker labels, a collapsed
    transcript would report 1 party and exempt itself -- the canary would be
    blind precisely when it matters."""
    t = _transcript(_many("A", 300), attendees=("a@x.com", "b@x.com", "c@x.com"))
    assert fd.expected_parties(t) == 3
    assert fd.assess(t).expected_parties == 3


def test_participants_and_attendees_are_unioned_case_insensitively():
    t = _transcript(_many("A", 60), attendees=("A@X.com", "b@x.com"))
    t["participants"] = ["a@x.com", "c@x.com"]
    assert fd.expected_parties(t) == 3


def test_all_unlabelled_sentences_read_as_a_collapse():
    """The ingest formatter substitutes "Speaker" for an empty speaker_name, so a
    wholly unlabelled transcript renders as 100% "Speaker" -- and that IS the
    defect, not a parsing artifact."""
    health = fd.assess(_transcript([""] * 200))
    assert health.collapsed is True
    assert "unlabelled" in health.reason


def test_numbered_placeholder_reads_as_unlabelled():
    health = fd.assess(_transcript(_many("Speaker 1", 200)))
    assert health.collapsed is True
    assert "unlabelled" in health.reason


def test_empty_transcript_is_not_a_collapse():
    health = fd.assess({"sentences": [], "meeting_attendees": []})
    assert health.collapsed is False
    assert health.reason == "no sentences"


def test_sentences_with_no_text_are_not_counted():
    t = _transcript(_many("A", 50))
    for s in t["sentences"][:45]:
        s["text"] = "   "
    health = fd.assess(t)
    assert health.sentences == 5          # only the five with real text
    assert health.collapsed is False      # and that is below the floor


# ── metadata contract ────────────────────────────────────────────────────────

def test_metadata_flags_only_when_collapsed():
    collapsed = fd.assess(_transcript(_many("A", 300))).as_metadata()
    healthy = fd.assess(_transcript(["A", "B"] * 60)).as_metadata()
    assert collapsed["attribution_unreliable"] is True
    assert "attribution_unreliable" not in healthy
    # The measurements ride along either way -- they are how a threshold change
    # can be evaluated later without a re-ingest.
    for meta in (collapsed, healthy):
        assert {"diarization_speakers", "diarization_top_share",
                "diarization_sentences"} <= set(meta)


def test_metadata_never_carries_speaker_names():
    """Metadata is rendered in places content is not; names are content."""
    meta = fd.assess(_transcript(_many("Harrison Rogers", 300))).as_metadata()
    assert "Harrison Rogers" not in json.dumps(meta)
    assert "speaker_counts" not in meta
    # The prose reason carries the name, so it stays in the Harrison-only ledger.
    assert "attribution_reason" not in meta


# ── the offline (stored-content) pass ────────────────────────────────────────

def test_rendered_pass_reads_labels_joined_on_one_line():
    """Measured against the live KB: the chunker collapses the formatter's
    newlines, so a stored chunk holds every `[Name] ..` on ONE line. A
    line-anchored pattern saw exactly one label per chunk -- the right verdict
    for the wrong reason, and the reason the first dry-run reported "49
    sentences" for a 49-chunk meeting."""
    content = " ".join(f"[Harrison Rogers] line {i}." for i in range(200))
    health = fd.assess_rendered(content, expected=4)
    assert health.sentences == 200
    assert health.collapsed is True


def test_rendered_pass_ignores_the_formatters_own_header():
    """`[Fireflies Meeting] <title>` is the formatter's header, not a speaker.
    Live count: exactly 735 across 735 stored meetings."""
    content = ("[Fireflies Meeting] OSN weekly\nDate: 2026-06-01\n\n"
               + " ".join(f"[A] x{i}. [B] y{i}." for i in range(40)))
    health = fd.assess_rendered(content, expected=3)
    assert "Fireflies Meeting" not in health.speaker_counts
    assert health.speakers == 2
    assert health.collapsed is False


def test_rendered_pass_matches_a_healthy_dialogue():
    content = " ".join(f"[A] a{i}. [B] b{i}." for i in range(60))
    assert fd.assess_rendered(content, expected=2).collapsed is False


def test_rendered_pass_on_a_summary_only_chunk_is_inert():
    content = ("[Fireflies Meeting] Julian x F3 Energy\nDate: 2026-06-01\n\n"
               "Overview:\n- **Partnership Strategy:** flexible sponsorships.\n")
    health = fd.assess_rendered(content, expected=4)
    assert health.sentences == 0
    assert health.collapsed is False


def test_rendered_pass_is_linear_on_a_bracket_heavy_string():
    """No backtracking surface: bounded name class, body taken by slicing to the
    next match. Three ReDoS findings in one prior session make this worth a pin."""
    import time
    content = "[" * 40000 + "A] text"
    start = time.monotonic()
    fd.assess_rendered(content, expected=3)
    assert time.monotonic() - start < 2.0


# ── the ledger + digest ──────────────────────────────────────────────────────

@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    from cora.connectors import fireflies_connector as fc
    monkeypatch.setattr(fc, "DIARIZATION_LEDGER_PATH", tmp_path / "diar.jsonl")
    return fc


def _write_flag(fc, ts, transcript_id="t1", title="OSN weekly", top_share=1.0):
    row = {"ts": ts, "transcript_id": transcript_id, "title": title,
           "entity": "OSN", "meeting_ts": 1780000000, "sentences": 900,
           "speakers": 1, "top_share": top_share, "expected_parties": 5,
           "reason": "collapsed"}
    with fc.DIARIZATION_LEDGER_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def test_ledger_records_a_flag_and_reads_it_back(ledger):
    fc = ledger
    health = fd.assess(_transcript(_many("A", 300)))
    fc._record_diarization_flag("t-77", "Long meeting", "OSN", 1780000000, health)
    rows = fc.read_diarization_flags()
    assert len(rows) == 1
    assert rows[0]["transcript_id"] == "t-77"
    assert rows[0]["top_share"] == 1.0


def test_ledger_window_drops_old_flags(ledger):
    fc = ledger
    now = datetime(2026, 8, 19, tzinfo=timezone.utc)
    _write_flag(fc, (now - timedelta(days=2)).isoformat(), "fresh")
    _write_flag(fc, (now - timedelta(days=30)).isoformat(), "stale")
    ids = {r["transcript_id"] for r in fc.read_diarization_flags(7, now=now)}
    assert ids == {"fresh"}


def test_ledger_dedups_a_re_synced_meeting(ledger):
    fc = ledger
    now = datetime(2026, 8, 19, tzinfo=timezone.utc)
    _write_flag(fc, (now - timedelta(days=3)).isoformat(), "same")
    _write_flag(fc, (now - timedelta(days=1)).isoformat(), "same")
    rows = fc.read_diarization_flags(7, now=now)
    assert len(rows) == 1


def test_ledger_survives_garbage_lines(ledger):
    fc = ledger
    now = datetime(2026, 8, 19, tzinfo=timezone.utc)
    with fc.DIARIZATION_LEDGER_PATH.open("a", encoding="utf-8") as fh:
        fh.write("not json\n\n")
    _write_flag(fc, (now - timedelta(hours=2)).isoformat(), "ok")
    assert len(fc.read_diarization_flags(7, now=now)) == 1


def test_missing_ledger_is_not_an_error(ledger):
    assert ledger.read_diarization_flags() == []


def test_recording_a_flag_never_raises(monkeypatch, tmp_path):
    from cora.connectors import fireflies_connector as fc
    monkeypatch.setattr(fc, "DIARIZATION_LEDGER_PATH", tmp_path / "ro" / "d.jsonl")
    monkeypatch.setattr(Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    health = fd.assess(_transcript(_many("A", 300)))
    fc._record_diarization_flag("t", "title", "OSN", None, health)  # must not raise


def test_digest_section_is_empty_when_clean():
    from cora.connectors import fireflies_coverage as fcov
    assert fcov.format_diarization_section([]) == ""


def test_digest_section_names_the_meetings():
    from cora.connectors import fireflies_coverage as fcov
    flags = [{"title": "Harrison x Finance Weekly", "entity": "HJRG",
              "top_share": 1.0, "sentences": 1015, "expected_parties": 7}]
    text = fcov.format_diarization_section(flags)
    assert "Harrison x Finance Weekly" in text
    assert "100%" in text and "1015" in text
    assert "attribution" in text.lower()


def test_digest_section_caps_the_list():
    from cora.connectors import fireflies_coverage as fcov
    flags = [{"title": f"m{i}", "entity": "OSN", "top_share": 1.0,
              "sentences": 500, "expected_parties": 3} for i in range(14)]
    text = fcov.format_diarization_section(flags)
    assert "and 4 more" in text


def test_digest_section_tolerates_a_malformed_row():
    from cora.connectors import fireflies_coverage as fcov
    text = fcov.format_diarization_section([{"title": None, "top_share": "n/a"}])
    assert "(untitled)" in text and "?" in text


def test_digest_reports_flags_even_when_member_enumeration_failed():
    """A failed Fireflies `users` query says nothing about whether what we DID
    capture is usable -- both digest exit paths carry the section."""
    src = (_REPO_ROOT / "src" / "cora" / "connectors"
           / "fireflies_coverage.py").read_text(encoding="utf-8")
    assert src.count("format_diarization_section()") == 2


# ── retrieval label ──────────────────────────────────────────────────────────

def test_retrieval_labels_a_collapsed_transcript():
    from cora import context_loader

    class _R:
        source = "fireflies"
        title = "OSN weekly"
        entity = "OSN"
        source_id = "t1"
        deep_link = ""
        content = "[Harrison Rogers] we agreed on the vendor."
        date_created = None
        date_modified = None
        metadata = {"attribution_unreliable": True}

    out = context_loader._format_kb_chunks([_R()])
    assert "SPEAKER LABELS UNRELIABLE" in out
    assert "do NOT attribute" in out


def test_retrieval_does_not_label_a_healthy_transcript():
    from cora import context_loader

    class _R:
        source = "fireflies"
        title = "OSN weekly"
        entity = "OSN"
        source_id = "t1"
        deep_link = ""
        content = "[A] hello. [B] hi."
        date_created = None
        date_modified = None
        metadata = {"diarization_speakers": 2, "diarization_top_share": 0.6}

    assert "UNRELIABLE" not in context_loader._format_kb_chunks([_R()])


def test_ingest_tags_chunks_and_never_breaks_on_a_canary_error():
    """The connector must degrade to untagged, not to no ingest."""
    src = (_REPO_ROOT / "src" / "cora" / "connectors"
           / "fireflies_connector.py").read_text(encoding="utf-8")
    assert "**diarization_meta," in src
    assert "diarization_meta = {}" in src   # the except branch
