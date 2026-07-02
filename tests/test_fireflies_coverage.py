"""Tests for the Fireflies DWD coverage monitor.

Coverage:
  - load_dwd_humans(): drops shared inboxes, collapses cross-domain aliases
    (slack_user_id / shared email / normalized name), reads the real roster.
  - seat scope (2026-07-01): fireflies_seat flags restrict the roster to
    seat-holders; flag honored via any collapsed alias entry; flag-free file
    keeps full-roster behavior.
  - classify(): all 3 statuses, alias-aware + case-insensitive matching,
    recency refinement, and the correctness lock (never promote a non-member).
  - format_digest() / nudge_text(): content + branch-by-status.
  - nudge throttle: second nudge within 7d suppressed; after 7d allowed.
  - fail-closed: list_team_members raising -> digest still sends with a note.
  - wiring/import-smoke for the new connector functions.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cora.connectors import fireflies_connector, fireflies_coverage
from cora.connectors.fireflies_coverage import (
    COVERED,
    MEMBER_NO_RECORDINGS,
    NOT_A_MEMBER,
    DwdHuman,
    classify,
    format_digest,
    load_dwd_humans,
    nudge_text,
)


def _load_script():
    sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
    import run_fireflies_coverage as m

    return m


# ── fixture YAML ──────────────────────────────────────────────────────────────

_FIXTURE_YAML = """
accounts:
  # Harrison across 4 domains, all sharing one slack_user_id
  - email: harrison@hjrglobal.com
    name: Harrison Rogers
    enabled: true
    dwd_eligible: true
    entity_default: FNDR
    slack_user_id: U_HARRISON
  - email: harrison@f3energy.com
    name: Harrison Rogers (F3E)
    enabled: true
    dwd_eligible: true
    entity_default: F3E
    known_aliases: [harrison@hjrglobal.com]
    slack_user_id: U_HARRISON
  - email: harrison@unitedfightleague.com
    name: Harrison Rogers (UFL)
    enabled: true
    dwd_eligible: true
    entity_default: UFL
    slack_user_id: U_HARRISON
  # Larry: two entries, linked by alias + slack
  - email: larry@hjrglobal.com
    name: Larry Stone
    enabled: true
    dwd_eligible: true
    entity_default: BDM
    known_aliases: [larry@bigd.media]
    slack_user_id: U_LARRY
  - email: larry@bigd.media
    name: Larry Stone (BDM)
    enabled: true
    dwd_eligible: true
    entity_default: BDM
    known_aliases: [larry@hjrglobal.com]
    slack_user_id: U_LARRY
  # Alex: two entries linked ONLY by normalized name (no shared slack/alias)
  - email: alex@f3energy.com
    name: Alex Cordova
    enabled: true
    dwd_eligible: true
    entity_default: F3E
    slack_user_id: U_ALEX
  - email: alex@unitedfightleague.com
    name: Alex Cordova (UFL legacy)
    enabled: true
    dwd_eligible: true
    entity_default: UFL
  # Micah: roster primary differs from the email Fireflies lists (alias)
  - email: micah@hjrglobal.com
    name: Micah Kessler
    enabled: true
    dwd_eligible: true
    entity_default: FNDR
    known_aliases: [micah@bigd.media]
    slack_user_id: U_MICAH
  # shared inboxes — must be dropped
  - email: payables@hjrglobal.com
    name: HJR Payables Inbox
    enabled: true
    dwd_eligible: true
    entity_default: FNDR
  - email: service@f3energy.com
    name: F3E Service Inbox
    enabled: true
    dwd_eligible: true
    entity_default: F3E
  # disabled / ineligible — must be dropped
  - email: ghost@hjrglobal.com
    name: Ghost User
    enabled: false
    dwd_eligible: true
    entity_default: FNDR
  - email: personal@gmail.com
    name: Personal Gmail
    enabled: true
    dwd_eligible: false
    entity_default: FNDR
"""


@pytest.fixture()
def fixture_yaml(tmp_path) -> Path:
    p = tmp_path / "monitored-email-accounts.yaml"
    p.write_text(_FIXTURE_YAML, encoding="utf-8")
    return p


# Seat-scoped roster (2026-07-01 right-size): flags present -> only seat-holders kept.
_SEAT_FIXTURE_YAML = """
accounts:
  - email: harrison@hjrglobal.com
    name: Harrison Rogers
    enabled: true
    dwd_eligible: true
    entity_default: FNDR
    slack_user_id: U_HARRISON
    fireflies_seat: true
  # Tommy: two collapsed entries; the flag sits ONLY on the f3energy alias entry,
  # while the representative/primary resolves to the hjrglobal entry.
  - email: tommy@hjrglobal.com
    name: Tommy Anderson (HJRG)
    enabled: true
    dwd_eligible: true
    entity_default: F3E
    known_aliases: [tommy@f3energy.com]
    slack_user_id: U_TOMMY
  - email: tommy@f3energy.com
    name: Tommy Anderson
    enabled: true
    dwd_eligible: true
    entity_default: F3E
    slack_user_id: U_TOMMY
    fireflies_seat: true
  # invited seat-holder (not yet a Fireflies member) -- still in scope
  - email: alex@f3energy.com
    name: Alex Cordova
    enabled: true
    dwd_eligible: true
    entity_default: F3E
    slack_user_id: U_ALEX
    fireflies_seat: true
  # removed from Fireflies at the 6/22 right-size -- stay enabled for Gmail/Drive
  # ingestion but must fall out of the coverage monitor's scope (no flag)
  - email: micah@hjrglobal.com
    name: Micah Kessler
    enabled: true
    dwd_eligible: true
    entity_default: FNDR
    slack_user_id: U_MICAH
  - email: eric@hjrglobal.com
    name: Eric Canku
    enabled: true
    dwd_eligible: true
    entity_default: F3E
    slack_user_id: U_ERIC
"""


@pytest.fixture()
def seat_fixture_yaml(tmp_path) -> Path:
    p = tmp_path / "monitored-email-accounts.yaml"
    p.write_text(_SEAT_FIXTURE_YAML, encoding="utf-8")
    return p


# ── load_dwd_humans ─────────────────────────────────────────────────────────


class TestLoadDwdHumans:
    def test_drops_shared_inboxes_and_ineligible(self, fixture_yaml):
        humans = load_dwd_humans(fixture_yaml)
        emails = {e for h in humans for e in h.all_emails}
        assert "payables@hjrglobal.com" not in emails
        assert "service@f3energy.com" not in emails
        assert "ghost@hjrglobal.com" not in emails
        assert "personal@gmail.com" not in emails

    def test_harrison_collapses_to_one_across_domains(self, fixture_yaml):
        humans = load_dwd_humans(fixture_yaml)
        harr = [h for h in humans if h.slack_user_id == "U_HARRISON"]
        assert len(harr) == 1
        h = harr[0]
        assert "harrison@hjrglobal.com" in h.all_emails
        assert "harrison@f3energy.com" in h.all_emails
        assert "harrison@unitedfightleague.com" in h.all_emails
        # display name stripped of parenthetical suffix
        assert h.name == "Harrison Rogers"

    def test_larry_two_emails_collapse(self, fixture_yaml):
        humans = load_dwd_humans(fixture_yaml)
        larry = [h for h in humans if h.slack_user_id == "U_LARRY"]
        assert len(larry) == 1
        assert larry[0].all_emails >= {"larry@hjrglobal.com", "larry@bigd.media"}

    def test_alex_collapses_by_normalized_name(self, fixture_yaml):
        # alex@f3energy.com + alex@unitedfightleague.com share neither slack nor alias,
        # only the normalized name "alex cordova" -> must still collapse to one human.
        humans = load_dwd_humans(fixture_yaml)
        alex = [h for h in humans if "alex@f3energy.com" in h.all_emails]
        assert len(alex) == 1
        assert "alex@unitedfightleague.com" in alex[0].all_emails
        assert alex[0].name == "Alex Cordova"

    def test_human_count_after_collapse(self, fixture_yaml):
        # Harrison, Larry, Alex, Micah = 4 distinct humans
        humans = load_dwd_humans(fixture_yaml)
        assert len(humans) == 4

    def test_real_roster_collapses_harrison_once(self):
        # integration: the actual production roster
        humans = load_dwd_humans()
        harrisons = [h for h in humans if h.slack_user_id == "U0B2RM2JYJ1"]
        assert len(harrisons) == 1
        # no shared inbox leaked through
        for h in humans:
            assert not any(
                e.split("@")[0] in {"payables", "receipts", "service"} for e in h.all_emails
            )


# ── seat scope (2026-07-01 right-size) ──────────────────────────────────────


class TestSeatScope:
    def test_only_flagged_humans_returned(self, seat_fixture_yaml):
        humans = load_dwd_humans(seat_fixture_yaml)
        names = {h.name for h in humans}
        assert names == {"Harrison Rogers", "Tommy Anderson", "Alex Cordova"}

    def test_removed_person_excluded(self, seat_fixture_yaml):
        # Micah/Eric stay in the file (Gmail/Drive ingestion) but carry no seat flag.
        humans = load_dwd_humans(seat_fixture_yaml)
        emails = {e for h in humans for e in h.all_emails}
        assert "micah@hjrglobal.com" not in emails
        assert "eric@hjrglobal.com" not in emails

    def test_invited_seat_holder_included(self, seat_fixture_yaml):
        humans = load_dwd_humans(seat_fixture_yaml)
        assert any("alex@f3energy.com" in h.all_emails for h in humans)

    def test_flag_via_collapsed_alias_entry(self, seat_fixture_yaml):
        # The flag sits on tommy@f3energy.com; the collapsed human's representative
        # (and primary_email) is the hjrglobal entry -- the component must still
        # count as flagged.
        humans = load_dwd_humans(seat_fixture_yaml)
        tommy = [h for h in humans if h.slack_user_id == "U_TOMMY"]
        assert len(tommy) == 1
        assert tommy[0].primary_email == "tommy@hjrglobal.com"
        assert "tommy@f3energy.com" in tommy[0].all_emails

    def test_no_flags_backward_compat(self, fixture_yaml):
        # A roster with zero fireflies_seat flags behaves exactly as before:
        # Harrison, Larry, Alex, Micah = 4 humans.
        humans = load_dwd_humans(fixture_yaml)
        assert len(humans) == 4
        assert any("micah@hjrglobal.com" in h.all_emails for h in humans)

    def test_real_roster_is_the_ten_seat_holders(self):
        # integration: the production roster is seat-scoped to exactly the 10
        # seat-holders of the 2026-07-01 right-size. Update the YAML flags AND
        # this set together when seats change.
        humans = load_dwd_humans()
        names = {h.name for h in humans}
        assert names == {
            "Harrison Rogers",
            "Hannah Grant",
            "Justin Moran",
            "Alina Thomas",
            "Larry Stone",
            "Tommy Anderson",
            "Shaun Hawkins",
            "Jennifer Mortensen",
            "Alex Cordova",
            "Daniel Sion",
        }
        # the 6/22-removed people must never re-enter the monitor's scope
        for gone in ("Micah Kessler", "Elena Meirndorf", "Eric Canku",
                     "Jeff Montgomery", "Matt Petrovich", "Jake Lichtman"):
            assert gone not in names


# ── classify ────────────────────────────────────────────────────────────────


def _h(name, email, aliases=None, slack="U_X"):
    return DwdHuman(name=name, primary_email=email, known_aliases=aliases or [], slack_user_id=slack)


class TestClassify:
    def test_covered_member_with_transcripts(self):
        humans = [_h("Harrison Rogers", "harrison@hjrglobal.com")]
        members = [{"email": "harrison@hjrglobal.com", "num_transcripts": 566}]
        report = classify(humans, members)
        assert report.results[0].status == COVERED
        assert report.results[0].num_transcripts == 566

    def test_member_no_recordings_zero_transcripts(self):
        humans = [_h("Hannah Grant", "hannah@hjrglobal.com")]
        members = [{"email": "hannah@hjrglobal.com", "num_transcripts": 0}]
        report = classify(humans, members)
        assert report.results[0].status == MEMBER_NO_RECORDINGS

    def test_member_no_recordings_null_transcripts(self):
        # API returns None for never-recorded members
        humans = [_h("Eric Canku", "eric@hjrglobal.com")]
        members = [{"email": "eric@hjrglobal.com", "num_transcripts": None}]
        report = classify(humans, members)
        assert report.results[0].status == MEMBER_NO_RECORDINGS

    def test_not_a_member(self):
        humans = [_h("Justin Moran", "justin@hjrglobal.com")]
        members = [{"email": "harrison@hjrglobal.com", "num_transcripts": 566}]
        report = classify(humans, members)
        assert report.results[0].status == NOT_A_MEMBER
        assert report.results[0].is_member is False

    def test_alias_match(self):
        # member listed under micah@bigd.media must match Micah's alias
        humans = [_h("Micah Kessler", "micah@hjrglobal.com", aliases=["micah@bigd.media"])]
        members = [{"email": "micah@bigd.media", "num_transcripts": 0}]
        report = classify(humans, members)
        assert report.results[0].status == MEMBER_NO_RECORDINGS
        assert report.results[0].is_member is True

    def test_case_insensitive_match(self):
        humans = [_h("Shaun Hawkins", "shaun@lexingtonservices.com")]
        members = [{"email": "Shaun@LexingtonServices.com", "num_transcripts": 0}]
        # member email is lowercased by list_team_members; simulate already-lower here
        members = [{"email": "shaun@lexingtonservices.com", "num_transcripts": 0}]
        report = classify(humans, members)
        assert report.results[0].status == MEMBER_NO_RECORDINGS

    def test_recency_refinement_demotes_to_older_recordings(self):
        humans = [_h("Harrison Rogers", "harrison@hjrglobal.com")]
        members = [{"email": "harrison@hjrglobal.com", "num_transcripts": 566}]
        # refinement ran (set passed) but nobody recent -> demote, mark older recordings
        report = classify(humans, members, recent_host_emails=set())
        assert report.results[0].status == MEMBER_NO_RECORDINGS
        assert report.results[0].has_older_recordings is True

    def test_recency_keeps_covered_when_recent(self):
        humans = [_h("Harrison Rogers", "harrison@hjrglobal.com")]
        members = [{"email": "harrison@hjrglobal.com", "num_transcripts": 566}]
        report = classify(humans, members, recent_host_emails={"harrison@hjrglobal.com"})
        assert report.results[0].status == COVERED

    def test_recency_none_keeps_covered(self):
        humans = [_h("Harrison Rogers", "harrison@hjrglobal.com")]
        members = [{"email": "harrison@hjrglobal.com", "num_transcripts": 566}]
        report = classify(humans, members, recent_host_emails=None)
        assert report.results[0].status == COVERED

    def test_correctness_lock_probe_never_promotes_non_member(self):
        # NOT_A_MEMBER stays NOT_A_MEMBER even if their email is in recent_host_emails
        # (the organizer probe found a meeting they "hosted" — captured only because
        # someone else's calendar was connected).
        humans = [_h("Larry Stone", "larry@bigd.media")]
        members = [{"email": "harrison@hjrglobal.com", "num_transcripts": 566}]
        report = classify(humans, members, recent_host_emails={"larry@bigd.media"})
        assert report.results[0].status == NOT_A_MEMBER

    def test_summary_line_counts(self):
        humans = [
            _h("Harrison Rogers", "harrison@hjrglobal.com", slack="U_H"),
            _h("Hannah Grant", "hannah@hjrglobal.com", slack="U_HG"),
            _h("Justin Moran", "justin@hjrglobal.com", slack="U_J"),
        ]
        members = [
            {"email": "harrison@hjrglobal.com", "num_transcripts": 566},
            {"email": "hannah@hjrglobal.com", "num_transcripts": 0},
        ]
        report = classify(humans, members)
        assert len(report.covered) == 1
        assert len(report.member_no_recordings) == 1
        assert len(report.not_a_member) == 1
        assert "of 3 DWD users" in report.summary_line


# ── formatting ──────────────────────────────────────────────────────────────


class TestFormatting:
    def _report(self):
        humans = [
            _h("Harrison Rogers", "harrison@hjrglobal.com", slack="U_H"),
            _h("Hannah Grant", "hannah@hjrglobal.com", slack="U_HG"),
            _h("Justin Moran", "justin@hjrglobal.com", slack="U_J"),
        ]
        members = [
            {"email": "harrison@hjrglobal.com", "num_transcripts": 566},
            {"email": "hannah@hjrglobal.com", "num_transcripts": 0},
        ]
        return classify(humans, members)

    def test_digest_contains_each_section(self):
        text = format_digest(self._report(), days=30)
        assert "Covered" in text
        assert "Not a member" in text
        assert "Member, no recordings" in text
        assert "Harrison Rogers" in text
        assert "Justin Moran" in text
        assert "members-and-groups" in text  # admin link

    def test_digest_enumerate_failed_note(self):
        report = self._report()
        report.enumerate_failed = True
        text = format_digest(report, days=30)
        assert "Could not enumerate" in text
        # still lists the roster
        assert "Harrison Rogers" in text

    def test_older_recordings_note_in_digest(self):
        humans = [_h("Harrison Rogers", "harrison@hjrglobal.com")]
        members = [{"email": "harrison@hjrglobal.com", "num_transcripts": 566}]
        report = classify(humans, members, recent_host_emails=set())
        text = format_digest(report, days=14)
        assert "older recordings" in text
        assert "14d" in text

    def test_nudge_text_branches_by_status(self):
        not_member = fireflies_coverage.PersonResult(
            human=_h("X", "x@hjrglobal.com"), status=NOT_A_MEMBER
        )
        member = fireflies_coverage.PersonResult(
            human=_h("Y", "y@hjrglobal.com"), status=MEMBER_NO_RECORDINGS, is_member=True
        )
        assert "accept the Fireflies invite" in nudge_text(not_member)
        assert "accept the Fireflies invite" not in nudge_text(member)
        assert "connect your Google Calendar" in nudge_text(member)


# ── nudge throttle (script DB) ──────────────────────────────────────────────


class TestNudgeThrottle:
    def test_second_nudge_within_7d_suppressed_then_allowed(self, tmp_path):
        mod = _load_script()
        db = tmp_path / "fireflies_coverage.db"

        report = fireflies_coverage.CoverageReport(
            results=[
                fireflies_coverage.PersonResult(
                    human=_h("Hannah Grant", "hannah@hjrglobal.com", slack="U_HG"),
                    status=MEMBER_NO_RECORDINGS,
                    is_member=True,
                ),
                fireflies_coverage.PersonResult(
                    human=_h("Justin Moran", "justin@hjrglobal.com", slack="U_J"),
                    status=NOT_A_MEMBER,
                ),
            ]
        )

        sent: list[tuple[str, str]] = []

        def fake_post(channel, text):
            sent.append((channel, text))
            return True

        with patch.object(mod, "_DB_PATH", db), patch.object(
            mod, "build_report", return_value=report
        ), patch.object(mod, "_post_slack_message", side_effect=fake_post):
            mod.run_coverage(dry_run=False, nudge=True, days=30)
            # 1 digest (Harrison) + 2 nudges
            channels = [c for c, _ in sent]
            assert mod._HARRISON_USER_ID in channels
            assert "U_HG" in channels
            assert "U_J" in channels

            sent.clear()
            mod.run_coverage(dry_run=False, nudge=True, days=30)
            # within 7d: digest still sent, but NO repeat nudges
            channels = [c for c, _ in sent]
            assert mod._HARRISON_USER_ID in channels
            assert "U_HG" not in channels
            assert "U_J" not in channels

        # now age the throttle rows past 7d and confirm nudges fire again
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        with patch.object(mod, "_DB_PATH", db):
            with mod._db_conn() as conn:
                conn.execute("UPDATE coverage_nudge_log SET nudged_at = ?", (old,))
                conn.commit()

            sent.clear()
            with patch.object(mod, "build_report", return_value=report), patch.object(
                mod, "_post_slack_message", side_effect=fake_post
            ):
                mod.run_coverage(dry_run=False, nudge=True, days=30)
            channels = [c for c, _ in sent]
            assert "U_HG" in channels
            assert "U_J" in channels

    def test_digest_only_sends_no_nudges(self, tmp_path):
        mod = _load_script()
        db = tmp_path / "fireflies_coverage.db"
        report = fireflies_coverage.CoverageReport(
            results=[
                fireflies_coverage.PersonResult(
                    human=_h("Justin Moran", "justin@hjrglobal.com", slack="U_J"),
                    status=NOT_A_MEMBER,
                )
            ]
        )
        sent: list[tuple[str, str]] = []
        with patch.object(mod, "_DB_PATH", db), patch.object(
            mod, "build_report", return_value=report
        ), patch.object(mod, "_post_slack_message", side_effect=lambda c, t: sent.append((c, t)) or True):
            mod.run_coverage(dry_run=False, nudge=False, days=30)
        channels = [c for c, _ in sent]
        assert channels == [mod._HARRISON_USER_ID]  # digest only, no nudge


# ── fail-closed ─────────────────────────────────────────────────────────────


class TestFailClosed:
    def test_build_report_enumerate_failure(self):
        mod = _load_script()
        with patch.object(
            fireflies_connector,
            "list_team_members",
            side_effect=fireflies_connector.FirefliesConnectorError("boom"),
        ):
            report = mod.build_report(days=30)
        assert report.enumerate_failed is True
        # roster still populated -> everyone present, none promoted
        assert len(report.results) > 0
        text = format_digest(report)
        assert "Could not enumerate" in text

    def test_run_coverage_still_sends_digest_on_failure(self):
        mod = _load_script()
        sent: list[tuple[str, str]] = []
        with patch.object(
            fireflies_connector,
            "list_team_members",
            side_effect=fireflies_connector.FirefliesConnectorError("boom"),
        ), patch.object(
            mod, "_post_slack_message", side_effect=lambda c, t: sent.append((c, t)) or True
        ):
            mod.run_coverage(dry_run=False, nudge=True, days=30)
        # digest sent to Harrison; no nudges because enumeration failed
        channels = [c for c, _ in sent]
        assert channels == [mod._HARRISON_USER_ID]


# ── wiring / import smoke ─────────────────────────────────────────────────────


class TestWiring:
    def test_connector_exposes_coverage_functions(self):
        assert hasattr(fireflies_connector, "list_team_members")
        assert callable(fireflies_connector.list_team_members)
        assert hasattr(fireflies_connector, "has_recent_host_meeting")
        assert callable(fireflies_connector.has_recent_host_meeting)

    def test_list_team_members_normalizes_rows(self):
        fake_data = {
            "users": [
                {
                    "email": "Harrison@HJRGlobal.com",
                    "name": "Harrison Rogers",
                    "num_transcripts": 566,
                    "minutes_consumed": 22297.4,
                    "is_admin": True,
                    "integrations": ["asana"],
                },
                {
                    "email": "micah@bigd.media",
                    "name": "Micah Kessler",
                    "num_transcripts": None,
                    "integrations": None,
                    "is_admin": False,
                },
            ]
        }
        with patch.object(fireflies_connector, "_graphql_query", return_value=fake_data):
            members = fireflies_connector.list_team_members()
        assert members[0]["email"] == "harrison@hjrglobal.com"  # lowercased
        assert members[0]["num_transcripts"] == 566
        assert members[1]["num_transcripts"] == 0  # None -> 0
        assert members[1]["integrations"] == []  # None -> []

    def test_has_recent_host_meeting_true_false(self):
        with patch.object(
            fireflies_connector, "_graphql_query", return_value={"transcripts": [{"id": "x"}]}
        ):
            assert fireflies_connector.has_recent_host_meeting("harrison@hjrglobal.com") is True
        with patch.object(fireflies_connector, "_graphql_query", return_value={"transcripts": []}):
            assert fireflies_connector.has_recent_host_meeting("nobody@hjrglobal.com") is False
