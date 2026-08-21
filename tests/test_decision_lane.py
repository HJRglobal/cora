"""The decisions lane: gate-date escalation and delivery evidence (slice 8).

cq-232fe6a541ff. Five decisions sat Open past their 2026-08-13 gate and reached
Harrison on no surface at all. The audit's finding that shapes every test here:
the seed asked for "delivery verification for P-decisions older than N days", and
implemented literally -- staleness on P0/P1 -- **that check would have stayed
GREEN through all five**, because every one of them is P2. So the control is
any-severity and turns on DELIVERY, not on gathering.

The end-to-end pin (test_the_five_lost_decisions_would_have_alarmed) is the one
that matters: it feeds the five real rows through the transcription mapping into
the gate check and asserts a critical.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from cora import decision_lane as dl  # noqa: E402

TODAY = date(2026, 8, 19)
NOW = datetime(2026, 8, 19, 12, tzinfo=timezone.utc)


def _entry(topic, severity="P2", gate="2026-08-13", surfaced="2026-08-10",
           touched="2026-08-13", entity="OSN", owner="Harrison", extra=""):
    return "\n".join([
        f"### {topic}",
        f"- **Entity**: {entity}",
        "- **Question**: what to do",
        f"- **Decision-maker**: {owner}",
        "- **Blockers**: bandwidth",
        f"- **Severity**: {severity}",
        f"- **Surfaced**: {surfaced}",
        f"- **Last touched**: {touched}",
        *([f"- **Gate**: {gate}"] if gate else []),
        f"- **Owner of next nudge**: {owner}",
        extra,
        "",
    ])


def _file(*entries, header="# Decisions pending\n\n"):
    return header + "\n".join(entries)


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    path = tmp_path / "decision-deliveries.jsonl"
    monkeypatch.setattr(dl, "DELIVERY_LEDGER", path)
    return path


# ── the parser ───────────────────────────────────────────────────────────────

def test_parses_every_severity_not_just_p0_p1():
    content = _file(_entry("A P2 thing", "P2"), _entry("A P1 thing", "P1"),
                    _entry("A P3 thing", "P3"))
    got = {e["topic"]: e["severity"] for e in dl.parse_entries(content, today=TODAY)}
    assert got == {"A P2 thing": "P2", "A P1 thing": "P1", "A P3 thing": "P3"}


def test_gate_date_is_parsed_and_aged():
    e = dl.parse_entries(_file(_entry("X", gate="2026-08-13")), today=TODAY)[0]
    assert e["gate"] == "2026-08-13"
    assert e["gate_overdue_days"] == 6


@pytest.mark.parametrize("label", ["Gate", "Gate date", "Decide by", "Decision due"])
def test_all_gate_spellings_parse(label):
    content = _file(_entry("X", gate=None, extra=f"- **{label}**: 2026-08-13"))
    assert dl.parse_entries(content, today=TODAY)[0]["gate"] == "2026-08-13"


def test_an_entry_with_no_gate_has_none_not_zero():
    e = dl.parse_entries(_file(_entry("X", gate=None)), today=TODAY)[0]
    assert e["gate"] is None and e["gate_overdue_days"] is None


def test_the_template_skeleton_is_skipped():
    content = _file(_entry("[Topic]", severity="P0"), _entry("Real one"))
    assert [e["topic"] for e in dl.parse_entries(content, today=TODAY)] == ["Real one"]


def test_the_recently_resolved_tail_is_skipped():
    content = _file(_entry("Live one")) + "\n## Recently resolved\n\n" + _entry("Old one")
    assert [e["topic"] for e in dl.parse_entries(content, today=TODAY)] == ["Live one"]


def test_a_closed_heading_is_skipped_by_default_and_kept_on_request():
    content = _file(_entry("Wikipedia pitches -- CLOSED 2026-07-20", severity="P1"))
    assert dl.parse_entries(content, today=TODAY) == []
    kept = dl.parse_entries(content, today=TODAY, skip_closed=False)
    assert len(kept) == 1


def test_missing_severity_is_none_not_a_guess():
    block = _entry("X").replace("- **Severity**: P2\n", "")
    assert dl.parse_entries(_file(block), today=TODAY)[0]["severity"] is None


def test_parser_is_total():
    for value in ("", None, "no entries here", "### \n"):
        assert isinstance(dl.parse_entries(value or "", today=TODAY), list)


# ── delivery evidence ────────────────────────────────────────────────────────

def test_record_and_read_back_a_delivery(ledger):
    assert dl.record_delivery(["Topic One", "Topic Two"], "strategy_memo") == 2
    index = dl.delivery_index(now=NOW)
    assert dl._topic_key("topic one") in index
    assert "strategy_memo" in index[dl._topic_key("Topic One")]["surfaces"]


def test_topic_identity_survives_punctuation_and_case_edits(ledger):
    dl.record_delivery("F3E<->HJRPROD RP receivable treatment", "synthesis:portfolio")
    index = dl.delivery_index(now=NOW)
    assert dl._topic_key("f3e  hjrprod   rp receivable treatment!") in index


def test_recording_never_raises_and_reports_zero(monkeypatch, tmp_path):
    monkeypatch.setattr(dl, "DELIVERY_LEDGER", tmp_path / "nope" / "x.jsonl")
    monkeypatch.setattr(Path, "mkdir",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    assert dl.record_delivery("X", "surface") == 0


def test_empty_or_blank_topics_write_nothing(ledger):
    assert dl.record_delivery([], "s") == 0
    assert dl.record_delivery(["", "  "], "s") == 0
    assert not ledger.exists()


def test_delivery_index_survives_garbage(ledger):
    ledger.write_text("not json\n\n", encoding="utf-8")
    dl.record_delivery("Good one", "s")
    assert len(dl.delivery_index(now=NOW)) == 1


# ── the control ──────────────────────────────────────────────────────────────

def test_a_blown_gate_with_no_delivery_is_reported(ledger):
    entries = dl.parse_entries(_file(_entry("Jerry DW access")), today=TODAY)
    rows = dl.undelivered_overdue(entries, today=TODAY, now=NOW)
    assert len(rows) == 1
    assert rows[0]["never_delivered"] is True
    assert rows[0]["gate_overdue_days"] == 6


def test_a_p2_is_reported_because_severity_is_not_a_filter(ledger):
    """The single most important assertion in this file: the seed's P0/P1 version
    of this check would have stayed green through all five lost decisions."""
    entries = dl.parse_entries(
        _file(_entry("A P2 with a blown gate", severity="P2")), today=TODAY)
    assert dl.undelivered_overdue(entries, today=TODAY, now=NOW)


def test_a_recently_delivered_decision_is_not_reported(ledger):
    dl.record_delivery("Jerry DW access", "synthesis:portfolio")
    entries = dl.parse_entries(_file(_entry("Jerry DW access")), today=TODAY)
    # `now` is the WALL CLOCK here, not the pinned NOW the rest of the file
    # uses. record_delivery stamps the ledger with datetime.now(), so pinning
    # `now` two days in its past made the fresh row read as future-dated, trip
    # the -1 day lower bound and report the decision as undelivered -- a
    # failure that arrived on 2026-08-21 with no code change behind it.
    # `today` stays pinned because it is what the GATE arithmetic keys on; the
    # two clocks are independent inputs and only the delivery one has to track
    # the row this test just wrote.
    assert dl.undelivered_overdue(
        entries, today=TODAY, now=datetime.now(timezone.utc)) == []


def test_a_stale_delivery_still_counts_as_undelivered(ledger):
    old = (NOW - timedelta(days=30)).isoformat()
    ledger.write_text(json.dumps({
        "ts": old, "surface": "strategy_memo", "topic": "Jerry DW access",
        "key": dl._topic_key("Jerry DW access")}) + "\n", encoding="utf-8")
    entries = dl.parse_entries(_file(_entry("Jerry DW access")), today=TODAY)
    rows = dl.undelivered_overdue(entries, today=TODAY, now=NOW)
    assert len(rows) == 1
    assert rows[0]["never_delivered"] is False
    assert rows[0]["last_delivered"].startswith("2026-07")


def test_a_gate_in_the_future_is_not_reported(ledger):
    entries = dl.parse_entries(_file(_entry("X", gate="2026-09-30")), today=TODAY)
    assert dl.undelivered_overdue(entries, today=TODAY, now=NOW) == []


def test_a_decision_with_no_gate_is_never_reported(ledger):
    """It has no deadline to blow. This is the gap the transcription closes by
    carrying the tracker's gate across."""
    entries = dl.parse_entries(_file(_entry("X", gate=None)), today=TODAY)
    assert dl.undelivered_overdue(entries, today=TODAY, now=NOW) == []


def test_nothing_is_ever_expired_only_escalated(ledger):
    """Aligned with the 8/19 approval-recon adoption: an expired-undecided item
    escalates, it never silently ages out. A 200-day-overdue gate is still
    reported, and more loudly (sorted first)."""
    entries = dl.parse_entries(
        _file(_entry("Ancient", gate="2026-01-01"), _entry("Recent", gate="2026-08-18")),
        today=TODAY)
    rows = dl.undelivered_overdue(entries, today=TODAY, now=NOW)
    assert [r["topic"] for r in rows] == ["Ancient", "Recent"]


def test_format_alarm_is_empty_when_clean():
    assert dl.format_alarm([]) == ""


def test_format_alarm_names_severity_gate_and_owner(ledger):
    entries = dl.parse_entries(_file(_entry("Jerry DW access")), today=TODAY)
    text = dl.format_alarm(dl.undelivered_overdue(entries, today=TODAY, now=NOW))
    for token in ("Jerry DW access", "P2", "2026-08-13", "6d overdue",
                  "never delivered", "Harrison"):
        assert token in text


# ── the health check ─────────────────────────────────────────────────────────

def _hc(monkeypatch, tmp_path, content):
    path = tmp_path / "decisions-pending.md"
    path.write_text(content, encoding="utf-8")
    monkeypatch.setenv("STRATEGY_DECISIONS_PATH", str(path))
    import nightly_health_check as hc
    return hc


def test_health_check_criticals_on_a_blown_gate(monkeypatch, tmp_path, ledger):
    hc = _hc(monkeypatch, tmp_path, _file(_entry("Jerry DW access")))
    result = hc.check_decision_gates(today=TODAY)
    assert result.status == "critical"
    assert "Jerry DW access" in result.detail


def test_health_check_warns_when_no_entry_carries_a_gate(monkeypatch, tmp_path, ledger):
    """The live state on 2026-08-19: 15 open decisions, zero gate dates. Reporting
    "ok" there would be a lie -- the control has nothing to enforce."""
    hc = _hc(monkeypatch, tmp_path, _file(_entry("A", gate=None), _entry("B", gate=None)))
    result = hc.check_decision_gates(today=TODAY)
    assert result.status == "warn"
    assert "NONE carrying a gate date" in result.detail


def test_health_check_ok_when_gates_are_present_and_met(monkeypatch, tmp_path, ledger):
    hc = _hc(monkeypatch, tmp_path, _file(_entry("A", gate="2026-09-30")))
    assert hc.check_decision_gates(today=TODAY).status == "ok"


def test_health_check_warns_on_an_unreadable_file(monkeypatch, tmp_path, ledger):
    monkeypatch.setenv("STRATEGY_DECISIONS_PATH", str(tmp_path / "missing.md"))
    import nightly_health_check as hc
    assert hc.check_decision_gates(today=TODAY).status == "warn"


# ── the transcription mapping ────────────────────────────────────────────────

def _tracker_row(item, **over):
    row = {"Item": item, "Type": "Decision", "Status": "Open", "Owner": "Harrison",
           "Severity": "P2", "Entity": "OSN", "Gate date": "2026-08-13"}
    row.update(over)
    return row


def test_only_open_decisions_are_transcribed():
    import sync_decisions_from_tracker as sync
    assert sync.to_entry(_tracker_row("Real"), today=TODAY) is not None
    assert sync.to_entry(_tracker_row("Build", Type="Build"), today=TODAY) is None
    assert sync.to_entry(_tracker_row("Done", Status="Closed"), today=TODAY) is None
    assert sync.to_entry(_tracker_row(""), today=TODAY) is None


def test_severity_is_carried_across_never_promoted():
    """Quietly upgrading a P2 to P1 to get it surfaced would hide the real defect
    -- the gate control is severity-blind precisely so that is unnecessary."""
    import sync_decisions_from_tracker as sync
    assert sync.to_entry(_tracker_row("X", Severity="P2"), today=TODAY)["severity"] == "P2"


def test_the_live_tracker_column_names_are_the_ones_read():
    """PROBED, not guessed (D-051 lens-5 HIGH). The first cut was written from the
    audit prose and got five of nine wrong -- "Name" does not exist, so every read
    422'd into a fetch-ALL, and the gate date ("Due"), severity ("Priority"),
    question ("Detail") and entity ("For") were all read from columns that are not
    there. Every live proposal therefore carried no gate date at all."""
    import sync_decisions_from_tracker as sync
    live_row = {"Item": "Jerry DW access", "Type": "Decision", "Status": "Open",
                "Owner": "Harrison", "Priority": "P2", "For": "HJRG",
                "Due": "2026-08-13", "Detail": "May be his lane by design."}
    entry = sync.to_entry(live_row, today=TODAY)
    assert entry["topic"] == "Jerry DW access"
    assert entry["gate"] == "2026-08-13"      # from "Due"
    assert entry["severity"] == "P2"          # from "Priority"
    assert entry["entity"] == "HJRG"          # from "For"
    assert "his lane" in entry["question"]    # from "Detail"
    # The doc-derived spellings stay as fallbacks, so a rename either way works.
    legacy = {"Item": "X", "Type": "Decision", "Status": "Open",
              "Severity": "P1", "Entity": "OSN", "Gate date": "2026-08-01"}
    alt = sync.to_entry(legacy, today=TODAY)
    assert alt["severity"] == "P1" and alt["entity"] == "OSN"
    assert alt["gate"] == "2026-08-01"
    # And every requested column must exist in the live table.
    assert set(sync.FIELDS) <= {"Ref (cq / doc)", "Detail", "Priority", "Due",
                                "Type", "Item", "Status", "Owner", "For"}


def test_a_rendered_block_round_trips_through_the_parser():
    """The proposal must be in the format the parser reads back -- otherwise a
    filed entry is invisible to the very control that asked for it."""
    import sync_decisions_from_tracker as sync
    entry = sync.to_entry(_tracker_row("Jerry DW access"), today=TODAY)
    block = sync.render_block(entry, today=TODAY)
    parsed = dl.parse_entries(_file(block), today=TODAY)
    assert len(parsed) == 1
    assert parsed[0]["gate"] == "2026-08-13"
    assert parsed[0]["severity"] == "P2"
    assert parsed[0]["topic"] == "Jerry DW access"


def test_the_five_lost_decisions_would_have_alarmed(monkeypatch, tmp_path, ledger):
    """End-to-end on the five real tracker rows: transcription -> file -> control."""
    import sync_decisions_from_tracker as sync
    five = [
        "OSN data-source decision (mailhook vs Clover API vs stay-manual)",
        "Jerry DW access",
        "BDM department lock (post co-owner consult)",
        "Eric LEX Learning Center queue keep-vs-rehome",
        "LEX Phase 2 go/no-go",
    ]
    blocks = [sync.render_block(sync.to_entry(_tracker_row(t), today=TODAY),
                                today=TODAY) for t in five]
    hc = _hc(monkeypatch, tmp_path, _file(*blocks))
    result = hc.check_decision_gates(today=TODAY)
    assert result.status == "critical"
    for topic in five:
        assert topic in result.detail


def test_the_transcription_never_writes_canon():
    src = (_REPO_ROOT / "scripts" / "sync_decisions_from_tracker.py").read_text(
        encoding="utf-8")
    assert "decisions_pending_path()" not in src.replace(
        "decision_lane.load_entries", "")
    assert "write_text" in src          # it writes exactly one file...
    assert src.count("write_text") == 1  # ...the proposal, under logs/
    assert "logs" in src


def test_the_tracker_read_does_not_widen_the_dashboard_allowlist():
    """Two existing invariants forbid the obvious shortcut, and both have tests:
    the dashboard client's allowlist is the two dashboard bases (and its
    credential cannot even reach the tracker), and the training-log module's
    stated contract is "ONE operation: create ... no list". Hence a third,
    narrower module."""
    from cora.connectors import airtable_client, airtable_org_tracker
    import sync_decisions_from_tracker as sync
    assert airtable_org_tracker.BASE_ID not in airtable_client.ALLOWED_BASES
    assert sync.TRACKER_BASE == airtable_org_tracker.BASE_ID
    assert sync.TRACKER_TABLE == airtable_org_tracker.TABLE_ID


def test_the_tracker_reader_is_get_only_by_construction():
    src = (_REPO_ROOT / "src" / "cora" / "connectors"
           / "airtable_org_tracker.py").read_text(encoding="utf-8")
    for verb in (".post(", ".patch(", ".delete(", ".put(",
                 "def create_", "def update_", "def delete_"):
        assert verb not in src, f"the tracker reader must stay GET-only ({verb})"
    # And it cannot be pointed anywhere else.
    assert "def list_pending_rows(fields" in src
    assert "base_id" not in src.split("def list_pending_rows")[1]


def test_the_training_log_write_path_gains_no_read():
    """Its docstring promises "ONE operation: create. No update, no delete, no
    list." -- this build must not have quietly added one."""
    src = (_REPO_ROOT / "src" / "cora" / "connectors"
           / "airtable_training_log.py").read_text(encoding="utf-8")
    assert "def list_" not in src


# ── the shared parser (no second grammar) ────────────────────────────────────

def test_strategy_memo_uses_the_shared_parser(monkeypatch, tmp_path):
    """One grammar over decisions-pending.md. Two was the shape of the original
    bug: a field visible to one surface and invisible to another."""
    from cora import strategy_memo as sm
    src = (_REPO_ROOT / "src" / "cora" / "strategy_memo.py").read_text(encoding="utf-8")
    assert "decision_lane.parse_entries" in src

    path = tmp_path / "d.md"
    path.write_text(_file(_entry("A P1 thing", "P1"), _entry("A P2 thing", "P2")),
                    encoding="utf-8")
    monkeypatch.setenv("STRATEGY_DECISIONS_PATH", str(path))
    out = sm.gather_stalled_decisions(today=TODAY)
    # The memo's OWN policy is unchanged: P0/P1 only.
    assert [d["topic"] for d in out["decisions"]] == ["A P1 thing"]
    # And the historical keys its persisted snapshots compare are all present.
    for key in ("topic", "entity", "severity", "age_days", "stale_days",
                "open_days", "surfaced", "origination_unknown", "owner"):
        assert key in out["decisions"][0]


def test_the_surfaces_record_delivery_only_after_a_successful_send():
    for rel in ("src/cora/strategy_memo.py", "src/cora/channel_synthesis.py"):
        src = (_REPO_ROOT / rel).read_text(encoding="utf-8")
        assert "decision_lane.record_delivery" in src, rel
        idx = src.index("decision_lane.record_delivery")
        window = src[max(0, idx - 1600):idx]
        assert "if delivered:" in window, f"{rel} records delivery unconditionally"


# ── D-051 lens-1: the health-digest surface and the injection path ───────────

def test_a_lex_decision_is_counted_not_itemized_in_the_alarm(ledger):
    """Every pre-existing surface for decisions-pending.md is Harrison-only; the
    health digest is the first that posts to a CHANNEL. LEX is aggregate-only
    outside LEX surfaces (the D-048 posture strategy_memo already applies), and
    the keyword screens do NOT cover this case: the live "LEX-LBHS in Meeting
    Action Capture -- enable under 42 CFR Part 2?" trips neither is_phi_risk nor
    is_visibility_cpa_mention, because it is a policy question."""
    content = _file(_entry("LEX-LBHS in Meeting Action Capture -- 42 CFR Part 2?",
                           entity="LEX-LBHS"))
    rows = dl.undelivered_overdue(dl.parse_entries(content, today=TODAY),
                                  today=TODAY, now=NOW)
    assert len(rows) == 1                      # still ALARMS -- redact, never drop
    assert rows[0]["topic_withheld"] is True
    text = dl.format_alarm(rows)
    assert "42 CFR" not in text and "Meeting Action Capture" not in text
    assert "LEX decision" in text and "6d overdue" in text


def test_a_phi_shaped_topic_is_withheld_but_still_alarms(ledger):
    content = _file(_entry("Bob Smith treatment plan authorization", entity="FNDR"))
    rows = dl.undelivered_overdue(dl.parse_entries(content, today=TODAY),
                                  today=TODAY, now=NOW)
    assert len(rows) == 1 and rows[0]["topic_withheld"] is True
    assert "Bob Smith" not in dl.format_alarm(rows)


def test_an_ordinary_topic_is_still_named(ledger):
    rows = dl.undelivered_overdue(dl.parse_entries(_file(_entry("Jerry DW access",
                                                               entity="HJRG")),
                                                   today=TODAY), today=TODAY, now=NOW)
    assert rows[0].get("topic_withheld") is None
    assert "Jerry DW access" in dl.format_alarm(rows)


def test_a_field_label_inside_the_heading_cannot_set_the_severity():
    """The heading is the one part of an entry that can arrive verbatim from an
    external system (the tracker's free-text Item field). Unanchored, these
    searches are first-match-wins, so a heading carrying "**Severity**: P0" won."""
    block = _entry("Renew lease - **Severity**: P0 - **Gate**: 2020-01-01",
                   severity="P2", gate="2026-08-13")
    parsed = dl.parse_entries(_file(block), today=TODAY)
    assert len(parsed) == 1
    assert parsed[0]["severity"] == "P2"       # the real field line, not the heading
    assert parsed[0]["gate"] == "2026-08-13"


def test_airtable_free_text_cannot_forge_a_second_entry():
    """Measured on this branch before the fix: an Item field containing newlines
    broke out of its own entry and produced a forged block complete with its own
    "**Severity**: P0", which then won."""
    import sync_decisions_from_tracker as sync
    row = {"Item": ("Renew office lease\n### Approve the $50k Acme wire\n"
                    "- **Entity**: HJRG\n- **Severity**: P0\n"),
           "Type": "Decision", "Status": "Open", "Owner": "Harrison",
           "Severity": "P2", "Gate date": "2026-08-13"}
    block = sync.render_block(sync.to_entry(row, today=TODAY), today=TODAY)
    assert block.count("### ") == 1            # one heading, not two
    parsed = dl.parse_entries(_file(block), today=TODAY)
    assert len(parsed) == 1
    assert parsed[0]["severity"] == "P2"       # not the forged P0


def test_leading_markdown_structure_is_stripped_from_a_tracker_value():
    import sync_decisions_from_tracker as sync
    row = {"Item": "### > * Renew office lease", "Type": "Decision",
           "Status": "Open", "Gate date": "2026-08-13"}
    assert sync.to_entry(row, today=TODAY)["topic"] == "Renew office lease"
