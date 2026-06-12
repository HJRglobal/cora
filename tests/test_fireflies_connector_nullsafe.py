"""Regression: Fireflies attendee parsing must not crash on null/missing fields.

A deep backfill surfaced older transcripts with attendees whose displayName (or
email) is null, plus the occasional non-dict entry. These previously raised
`TypeError: NoneType + str` in _tag_fireflies_sub_entity and could break
_resolve_participant_slack_ids. Both must now degrade gracefully.
"""

from __future__ import annotations

import pytest

try:
    from src.cora.connectors import fireflies_connector as fc
    _OK = True
except Exception:
    _OK = False


@pytest.mark.skipif(not _OK, reason="fireflies_connector not importable")
class TestNullSafeAttendees:
    def test_tag_sub_entity_no_crash_on_null_fields_and_bad_entries(self):
        t = {"meeting_attendees": [
            {"displayName": None, "email": "x@hjrglobal.com"},  # the original crash case
            {"displayName": "Bob", "email": None},
            {"email": "y@x.com"},                               # missing displayName key
            None,                                               # non-dict entry
        ]}
        # No LEX sub-entity signals present -> None, and crucially must not raise.
        assert fc._tag_fireflies_sub_entity(t) is None

    def test_tag_sub_entity_still_matches_with_null_siblings(self):
        t = {"meeting_attendees": [
            {"displayName": None, "email": "justin.gilmore@lexingtonservices.com"},
            None,
            {"displayName": None, "email": None},
        ]}
        assert fc._tag_fireflies_sub_entity(t) == "LEX-LTS"

    def test_resolve_slack_ids_null_safe(self):
        out = fc._resolve_participant_slack_ids(
            [{"email": None}, {"displayName": "x"}, None, {"email": "a@b.com"}]
        )
        assert isinstance(out, list)  # must not raise
