"""S1 (cq-551fada9dee8): last-message date + direction on every email citation.

The email-thread-review doctrine section 6 names this file's job: "code-level
enforcement is a staged code-queue item... Until that ships, treat Cora email
citations without a last-message date as unverified."

VERIFY-FIRST SPLIT THE SLICE AND OVERTURNED FOUR OF THE SEED'S NAMED MODULES.
tools/gmail_client.py is WRITE-ONLY (scope gmail.compose; its only API call is
drafts().create). missed_message_catchup reconstructs Slack, not email.
delegated_worker never touches Gmail. And revops/sweep.py is not a defect -- it is
the REFERENCE implementation, already reading "every tracked thread to the LAST
message", and its _is_outbound is the only correct direction rule in the repo. It
is MOVED here, not copied.

The READ half of the doctrine was therefore already satisfied in the automation
paths. The CITATION half was satisfied NOWHERE: 22 `format_*_for_llm` helpers and
exactly one is Gmail (a draft-confirmation renderer), so the formatting lives
inline at five unrelated sites and not one states a date or a direction.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cora import email_citation as ec
from cora.connectors import gmail_reader as gr

OWN = {"harrison@hjrglobal.com"}


def _msg(sender, ts, labels=None, internal=None):
    m = {"sender": sender, "date_ts": ts, "internal_ts": internal or ts}
    if labels is not None:
        m["label_ids"] = labels
    return m


# ── direction ───────────────────────────────────────────────────────────────

def test_the_sent_label_beats_the_from_header():
    """A From header is attacker-spoofable; a Gmail label is not."""
    spoofed = _msg("harrison@hjrglobal.com", 1, labels=["INBOX"])
    assert ec.is_outbound(spoofed, OWN) is False


def test_an_empty_label_list_is_still_authoritative():
    """`[] or fallback` would hand a spoofed From header the decision -- the
    reasoning revops documented and the reason this is one function, not two."""
    assert ec.is_outbound(_msg("harrison@hjrglobal.com", 1, labels=[]), OWN) is False
    assert ec.is_outbound(_msg("laura@gotham.com", 1, labels=["SENT"]), OWN) is True


def test_the_from_header_is_used_only_when_labels_are_absent():
    assert ec.is_outbound(_msg("harrison@hjrglobal.com", 1), OWN) is True
    assert ec.is_outbound(_msg("laura@gotham.com", 1), OWN) is False


def test_revops_delegates_rather_than_forking_the_rule():
    from cora.revops import sweep
    spoofed = _msg("harrison@hjrglobal.com", 1, labels=["INBOX"])
    assert sweep._is_outbound(spoofed, OWN) is ec.is_outbound(spoofed, OWN)
    with patch.object(ec, "is_outbound", return_value="SENTINEL"):
        assert sweep._is_outbound(spoofed, OWN) == "SENTINEL"


# ── the ordering bug ────────────────────────────────────────────────────────

def _api_msg(mid, sender, date_header, internal_ms):
    return {
        "id": mid,
        "internalDate": str(internal_ms),
        "labelIds": ["INBOX"],
        "payload": {"headers": [{"name": "From", "value": sender},
                                {"name": "Date", "value": date_header},
                                {"name": "Subject", "value": "S"}],
                    "mimeType": "text/plain",
                    "body": {"data": ""}},
    }


def test_a_backdated_reply_no_longer_sorts_into_the_middle():
    """S1 defect D. The sort keyed on `date_ts`, parsed from the SENDER-CONTROLLED
    Date header. A backdated or clock-skewed reply sorted to the middle, so
    `messages[-1]` -- which the revops state machine and asana_email_sync both
    rely on as "the last message" -- became an OLDER one. In the sweep that means
    a thread whose counterparty HAS replied stays "awaiting_reply" and Cora
    nudges someone who already answered."""
    first = _api_msg("M001", "a@x.com", "Tue, 3 Jun 2026 10:00:00 +0000", 1717000000000)
    # arrives LAST, but its Date header claims 1999
    backdated = _api_msg("M002", "b@x.com", "Mon, 4 Jan 1999 09:00:00 +0000",
                         1717009999000)
    svc = MagicMock()
    svc.users.return_value.threads.return_value.get.return_value.execute.return_value = {
        "id": "TH", "messages": [first, backdated]}
    with patch.object(gr, "_build_service", return_value=svc):
        out = gr.get_full_thread_text("harrison@hjrglobal.com", "TH")
    assert out[-1]["message_id"] == "M002", "the newest message is not last"


def test_the_displayed_date_is_still_the_header_date():
    """Sorting moved to internalDate; what a citation SHOWS is unchanged."""
    m = _api_msg("M1", "a@x.com", "Tue, 3 Jun 2026 10:00:00 +0000", 1717000000000)
    svc = MagicMock()
    svc.users.return_value.threads.return_value.get.return_value.execute.return_value = {
        "id": "TH", "messages": [m]}
    with patch.object(gr, "_build_service", return_value=svc):
        out = gr.get_full_thread_text("harrison@hjrglobal.com", "TH")
    assert out[0]["date_str"] == "Tue, 3 Jun 2026 10:00:00 +0000"
    assert out[0]["internal_ts"] == 1717000000


def test_the_keys_asana_email_sync_reads_now_exist():
    """S1 defect E: _build_comment read `latest["date_str"]` and the prefilter
    read `latest["cc"]`, and get_full_thread_text returned NEITHER -- so every
    Asana comment Cora ever posted said "Latest:  |", and the external-recipient
    prefilter silently counted To only."""
    m = _api_msg("M1", "a@x.com", "Tue, 3 Jun 2026 10:00:00 +0000", 1717000000000)
    m["payload"]["headers"].append({"name": "Cc", "value": "cc@x.com"})
    svc = MagicMock()
    svc.users.return_value.threads.return_value.get.return_value.execute.return_value = {
        "id": "TH", "messages": [m]}
    with patch.object(gr, "_build_service", return_value=svc):
        out = gr.get_full_thread_text("harrison@hjrglobal.com", "TH")
    assert out[0]["date_str"]
    assert out[0]["cc"] == "cc@x.com"


# ── the stamp ───────────────────────────────────────────────────────────────

def test_the_stamp_describes_the_LAST_message():
    stamp = ec.thread_stamp([
        _msg("laura@gotham.com", 1717000000, labels=["INBOX"]),
        _msg("harrison@hjrglobal.com", 1717100000, labels=["SENT"]),
        _msg("laura@gotham.com", 1717200000, labels=["INBOX"]),
    ], OWN)
    assert stamp["thread_msg_count"] == 3
    assert stamp["thread_last_direction"] == "inbound"
    assert stamp["thread_last_ts"] == 1717200000
    assert "Laura" in stamp["thread_last_from"]


def test_an_empty_thread_stamps_nothing():
    assert ec.thread_stamp([]) == {}
    assert ec.thread_stamp(None) == {}


# ── the citation fragment ───────────────────────────────────────────────────

def test_a_citation_states_the_date_and_the_direction():
    """Doctrine section 2.6: "Every time you report a thread's status, state its
    last-message date + direction". This is what the weekly audit checks."""
    out = ec.cite(last_ts=1755734400, last_direction="inbound",
                  last_from="Laura", msg_index=2, msg_count=12)
    assert "inbound" in out
    assert "from Laura" in out
    assert "msg 3 of 12" in out
    assert "last msg" in out


def test_the_deidentified_form_adds_recency_without_restoring_headers():
    """historical_access.strip_result nulls the metadata, the date AND the author
    on a non-owner chunk -- correct privacy behaviour, and it makes a compliant
    citation otherwise IMPOSSIBLE on exactly the chunks most likely to be cited."""
    out = ec.cite(last_ts=1755734400, last_direction="inbound",
                  last_from="Laura", deidentified=True)
    assert "Laura" not in out
    assert "inbound" in out
    assert "last activity" in out


def test_a_pre_stamp_chunk_is_honestly_unverified():
    """A chunk ingested before the stamp existed must say so, not silently render
    as undated -- "unverified" is exactly the doctrine's word for it."""
    assert ec.cite() == "thread position unknown (pre-stamp chunk)"
    assert ec.cite_from_metadata(None) == "thread position unknown (pre-stamp chunk)"
    assert ec.cite_from_metadata({}) == "thread position unknown (pre-stamp chunk)"


def test_a_single_message_thread_does_not_claim_a_position():
    out = ec.cite(last_ts=1755734400, last_direction="inbound", msg_index=0,
                  msg_count=1)
    assert "msg 1 of 1" not in out


def test_an_unusable_direction_is_dropped_rather_than_guessed():
    out = ec.cite(last_ts=1755734400, last_direction="sideways")
    assert "sideways" not in out
    assert "last msg" in out


def test_cite_from_metadata_reads_the_stamp():
    out = ec.cite_from_metadata({
        "thread_last_ts": 1755734400, "thread_last_direction": "inbound",
        "thread_last_from": "Laura", "thread_msg_index": 0, "thread_msg_count": 5})
    assert "inbound" in out and "msg 1 of 5" in out


def test_the_fragment_stays_short():
    """It rides _format_kb_chunks, which is on every Q&A prompt in the bot."""
    out = ec.cite(last_ts=1755734400, last_direction="inbound",
                  last_from="A Very Long Counterparty Name Indeed",
                  msg_index=11, msg_count=40)
    assert len(out) <= 70, out


# -- the Tier-1 strip keeps recency without restoring identity ---------------

def _gmail_result(meta):
    from cora.knowledge_base.store import SearchResult
    return SearchResult(
        chunk_id="c1", source="gmail", source_id="gmail:x@y.com:m1",
        entity="FNDR", title="Re: Gotham PO",
        content="From: laura@gotham.com\nSubject: x\n\nApproved.",
        deep_link="", date_modified=1755734400, distance=0.4,
        author="laura@gotham.com", metadata=meta)


def test_a_stripped_chunk_keeps_recency_and_leaks_nothing_else():
    """strip_result nulls the metadata, the date AND the author -- correct
    privacy behaviour, and it also made a doctrine-compliant citation IMPOSSIBLE
    on exactly the chunks most likely to be cited: a reader could not tell a live
    thread from a two-year-old one. The marker ADDS a recency signal; it must
    never restore a header."""
    from cora import historical_access as ha
    stripped = ha.strip_result(_gmail_result({
        "thread_last_ts": 1755734400, "thread_last_direction": "inbound",
        "thread_last_from": "Laura", "thread_msg_count": 12,
        "thread_msg_index": 2, "message_id": "m1", "user_email": "x@y.com"}))
    assert set(stripped.metadata) == {"thread_recency"}
    assert stripped.metadata["thread_recency"] == "last activity 8/21, inbound"
    for leak in ("Laura", "laura@gotham.com", "m1", "x@y.com", "Gotham"):
        assert leak not in str(stripped.metadata), leak


def test_a_pre_stamp_chunk_keeps_metadata_None_after_the_strip():
    """No marker unless there is a REAL stamp -- otherwise every chunk in the
    corpus would carry "thread position unknown" for no gain, since the sweep
    has not re-run yet and today that is all of them."""
    from cora import historical_access as ha
    assert ha.strip_result(_gmail_result({"message_id": "m1"})).metadata is None
