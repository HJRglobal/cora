"""C14 (cq-118f8bbf842e): the Klaviyo audit is read-only and honest about limits.

The properties under test:

  1. WRITE-IMPOSSIBILITY IS STRUCTURAL. Ops Dept OS v1 makes BOTH contact-list
     deletion AND billing cleanup unauthorized (the kickoff quoted only the
     deletion half), so the client is greppably GET-only rather than merely
     well-behaved.
  2. THE CHARGE BASIS IS DERIVED AND AUDITABLE. Klaviyo publishes no billing,
     plan, seat or invoice endpoint, so the report carries the count AND the
     segment condition that selected it, and never conflates two populations.
  3. THE CANDIDATE TEST IS THE DEFINITION, NOT THE NAME. A name-matched audit
     reports zero candidates the moment a segment is renamed, and reads as a clean
     account while doing it.
  4. ABSENT IS NEVER ZERO, and a banner describes what the reader is actually
     looking at.

The segment payloads below are VERBATIM shapes read from the live account
(VFstej) on 2026-08-25, so these tests exercise real API structure rather than an
invented one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from cora import klaviyo_audit as ka
from cora.connectors import klaviyo_client as kc

_CLIENT_SRC = (Path(kc.__file__)).read_text(encoding="utf-8")


def _seg(sid, name, definition, count):
    return {"type": "segment", "id": sid,
            "attributes": {"name": name, "definition": definition,
                           "profile_count": count, "is_active": True}}


ALL_SUBSCRIBED = _seg("YnjHCT", "All Subscribed", {"condition_groups": [
    {"conditions": [{"type": "profile-marketing-consent", "consent": {
        "channel": "email", "can_receive_marketing": True,
        "consent_status": {"subscription": "subscribed", "filters": None}}}]}]}, 4464)

NEVER_OPENED = _seg("UsVHrk", "Never Opened (Email)", {"condition_groups": [
    {"conditions": [{"type": "profile-metric", "metric_id": "XFK7Ct",
                     "measurement": "count",
                     "measurement_filter": {"type": "numeric", "operator": "equals", "value": 0},
                     "timeframe_filter": {"type": "date", "operator": "alltime"},
                     "metric_filters": None}]},
    {"conditions": [{"type": "profile-marketing-consent", "consent": {
        "channel": "email", "can_receive_marketing": True,
        "consent_status": {"subscription": "any", "filters": None}}}]}]}, 2367)

TEXT_SUBSCRIBED = _seg("WAkAg8", "Text Subscribed", {"condition_groups": [
    {"conditions": [{"type": "profile-marketing-consent", "consent": {
        "channel": "sms", "can_receive_marketing": True,
        "consent_status": {"subscription": "subscribed"}}}]}]}, 2247)

TIKTOK_MASKED = _seg("UM6EkS", "TikTok Shop Masked Emails - Exclude from Campaigns",
                     {"condition_groups": []}, 515)

ACCOUNT = {"type": "account", "id": "VFstej", "attributes": {
    "contact_information": {"organization_name": "F3Energy"}}}


# ── 1. write-impossibility ───────────────────────────────────────────────────

def test_the_client_contains_no_mutating_http_verb():
    """Greps the SOURCE, not the behaviour: a suppress/delete method added by a
    future refactor fails here even if no test ever calls it. Ops Dept OS v1
    forbids both contact-list deletion and billing cleanup."""
    found = set(re.findall(r"""['"](POST|PUT|PATCH|DELETE)['"]""", _CLIENT_SRC))
    assert not found, f"mutating HTTP verb string in klaviyo_client.py: {found}"


def test_get_is_the_only_verb_and_there_is_one_request_primitive():
    assert len(re.findall(r"""['"]GET['"]""", _CLIENT_SRC)) == 1
    assert _CLIENT_SRC.count("client.request(") == 1


def test_the_credential_is_never_interpolated_into_a_url():
    assert "_BASE" in _CLIENT_SRC
    assert 'f"{_BASE}{path}"' in _CLIENT_SRC, "the URL must be built from path only"


def test_the_api_key_is_scrubbed_from_anything_logged():
    assert kc._scrub("failed for pk_abc123def456") == "failed for pk_<redacted>"


def test_an_unconfigured_client_reads_nothing_and_never_raises(monkeypatch):
    monkeypatch.delenv("KLAVIYO_API_KEY", raising=False)
    assert kc.configured() is False
    assert kc.get_account() is None
    assert kc.get_segments() is None, "None, not [] -- 'could not look' is not 'empty'"
    assert kc.get_segment("YnjHCT") is None


def test_the_key_is_read_per_call_not_snapshotted(monkeypatch):
    """A module constant reading os.environ is the cq-06f4797db4f1 class: frozen
    at bot start, so adding the credential would appear to do nothing."""
    monkeypatch.delenv("KLAVIYO_API_KEY", raising=False)
    assert kc.configured() is False
    monkeypatch.setenv("KLAVIYO_API_KEY", "pk_test")
    assert kc.configured() is True


# ── 2. the charge basis ──────────────────────────────────────────────────────

def test_the_billable_basis_is_the_subscribed_email_population():
    assert ka.is_billable_basis(ALL_SUBSCRIBED) is True


def test_an_sms_segment_is_not_the_email_charge_basis():
    """Klaviyo bills SMS separately. Counting Text Subscribed as the email basis
    would overstate the bill by 2,247."""
    assert ka.is_billable_basis(TEXT_SUBSCRIBED) is False


def test_a_subscription_any_segment_is_not_the_charge_basis():
    """"any" admits profiles that can receive marketing without being subscribed --
    a BROADER population. Treating it as the basis, or subtracting it from the
    basis, is arithmetic across two different populations."""
    assert ka.is_billable_basis(NEVER_OPENED) is False


def test_the_report_quotes_the_condition_that_produced_the_number():
    audit = ka.build_audit(segments=[ALL_SUBSCRIBED], account=ACCOUNT, seat_holders=[])
    report = ka.format_report(audit)
    assert "4,464" in report
    assert "subscription: subscribed" in report
    assert "no billing, plan or invoice endpoint" in report, \
        "the report must say the basis is derived, not invoiced"


def test_the_largest_matching_segment_wins_the_basis():
    """A narrower segment sharing the billable consent shape would understate the
    bill."""
    narrow = _seg("N1", "AZ Subscribed", ALL_SUBSCRIBED["attributes"]["definition"], 70)
    audit = ka.build_audit(segments=[narrow, ALL_SUBSCRIBED], account=ACCOUNT,
                           seat_holders=[])
    assert audit["charge_basis"]["count"] == 4464
    assert "AZ Subscribed" in [o["name"] for o in audit["other_segments"]]


def test_no_matching_segment_means_the_basis_is_not_assumed():
    audit = ka.build_audit(segments=[TEXT_SUBSCRIBED], account=ACCOUNT, seat_holders=[])
    assert audit["charge_basis"] is None
    assert "could not be derived" in ka.format_report(audit)
    assert "has NOT been assumed" in ka.format_report(audit)


# ── 3. candidates by definition ──────────────────────────────────────────────

def test_a_never_engaged_segment_is_a_candidate_by_its_definition():
    assert ka.is_zero_engagement(NEVER_OPENED) is True


def test_the_candidate_test_survives_a_rename():
    renamed = _seg("UsVHrk", "Cold list Q3", NEVER_OPENED["attributes"]["definition"], 2367)
    assert ka.is_zero_engagement(renamed) is True, \
        "a name-matched audit would report zero candidates here"


def test_a_recent_window_of_no_opens_is_not_never_engaged():
    """30 days of no opens is a recent-activity segment. Suppressing on it would
    cull people who simply did not open last month."""
    recent = _seg("R1", "No opens 30d", {"condition_groups": [
        {"conditions": [{"type": "profile-metric", "measurement": "count",
                         "measurement_filter": {"operator": "equals", "value": 0},
                         "timeframe_filter": {"type": "date", "operator": "in_the_last"}}]}]}, 900)
    assert ka.is_zero_engagement(recent) is False


def test_a_nonzero_count_condition_is_not_a_candidate():
    engaged = _seg("E1", "Opened at least once", {"condition_groups": [
        {"conditions": [{"type": "profile-metric", "measurement": "count",
                         "measurement_filter": {"operator": "equals", "value": 3},
                         "timeframe_filter": {"operator": "alltime"}}]}]}, 500)
    assert ka.is_zero_engagement(engaged) is False


def test_the_charge_basis_segment_is_never_also_a_candidate():
    assert ka.is_zero_engagement(ALL_SUBSCRIBED) is False


def test_candidates_are_named_with_no_action_and_no_recommendation():
    audit = ka.build_audit(segments=[ALL_SUBSCRIBED, NEVER_OPENED], account=ACCOUNT,
                           seat_holders=[])
    report = ka.format_report(audit)
    assert "Never Opened (Email)" in report and "2,367" in report
    assert "unauthorized under Ops Dept OS v1" in report
    assert "Harrison's call" in report
    assert "Nothing was created, changed, suppressed or deleted" in report


# ── 4. absent is never zero ──────────────────────────────────────────────────

def test_an_unavailable_read_reports_unavailable_not_zero():
    audit = ka.build_audit(segments=None, account=None, seat_holders=[])
    report = ka.format_report(audit)
    assert audit["segments_available"] is False
    assert audit["charge_basis"] is None
    assert "Unavailable this run" in report
    assert "0 email-marketable" not in report


def test_a_missing_profile_count_renders_unknown_not_zero():
    """Klaviyo omits profile_count unless explicitly requested, so a missing key
    means 'not asked for' -- rendering it as 0 reports a live segment as empty."""
    no_count = {"type": "segment", "id": "X", "attributes": {"name": "X", "definition": {}}}
    assert kc.segment_profile_count(no_count) is None
    audit = ka.build_audit(segments=[no_count], account=ACCOUNT, seat_holders=[])
    assert "unknown" in ka.format_report(audit)


def test_the_banner_is_gated_on_the_figures_not_on_the_credential(monkeypatch):
    """The first cut keyed this on `configured` alone and printed "the profile
    figures could not be read" directly above 4,464 profiles -- the exact
    contradiction the C4 honesty work exists to stop."""
    monkeypatch.delenv("KLAVIYO_API_KEY", raising=False)
    audit = ka.build_audit(segments=[ALL_SUBSCRIBED], account=ACCOUNT, seat_holders=[])
    report = ka.format_report(audit)
    assert "4,464" in report
    assert "could not be read" not in report


def test_a_malformed_segment_never_raises():
    for junk in (None, 42, "string", {}, {"attributes": None},
                 {"attributes": {"definition": "not a dict"}}):
        assert ka.is_billable_basis(junk) is False
        assert ka.is_zero_engagement(junk) is False
    audit = ka.build_audit(segments=[None, 42, {}], account=None, seat_holders=[])
    assert isinstance(ka.format_report(audit), str)


# ── seats come from canon, and the report says so ────────────────────────────

def test_the_seat_roster_comes_from_the_klaviyo_seat_flag():
    holders = ka.seat_holders_from_roster()
    assert any(h["name"] == "Tessa Miller" for h in holders), \
        "Ops Dept OS v1 decision 3 (2026-08-08) confirms Tessa's seat"


def test_the_report_says_the_seat_list_cannot_be_reconciled():
    audit = ka.build_audit(segments=None, account=None,
                           seat_holders=[{"name": "Tessa Miller", "role": "Ops Coordinator"}])
    report = ka.format_report(audit)
    assert "NO seat, user or team-member endpoint" in report
    assert "incomplete by nature" in report


def test_an_empty_seat_roster_says_so_rather_than_omitting_the_section():
    report = ka.format_report(ka.build_audit(segments=None, account=None, seat_holders=[]))
    assert "No roster entry carries" in report


def test_the_ops_channel_is_allowlisted_by_id():
    """#founder-operations classifies as TIER_3 by name, so every consumer in the
    repo names it by id rather than trusting a tier lookup."""
    assert ka.OPS_CHANNEL == "C0BCUBUDHAR"


def test_klaviyo_is_on_the_renewal_radar():
    """It was the one live recurring-charge surface Cora already reads weekly, and
    Klaviyo was absent from it."""
    from cora import finance_close
    items = finance_close.load_renewals() or []
    names = " ".join(str(i.get("name") or "") for i in items).lower()
    assert "klaviyo" in names
    klav = [i for i in items if "klaviyo" in str(i.get("name") or "").lower()][0]
    assert klav.get("confirmed") is False, \
        "no invoice was read, so the amount/date must stay flagged unconfirmed"


# ── D-051 review fixes (2026-08-25) ─────────────────────────────────────────

def _grouped(sid, name, groups, count=100):
    """A segment in the REAL Klaviyo shape: condition_groups is a list of
    {"conditions": [...]} objects. Groups are ANDed; conditions within a group
    are ORed."""
    return {"type": "segment", "id": sid, "attributes": {
        "name": name, "profile_count": count,
        "definition": {"condition_groups": [{"conditions": g} for g in groups]}}}


_BILLABLE_COND = {"type": "profile-marketing-consent", "consent": {
    "channel": "email", "can_receive_marketing": True,
    "consent_status": {"subscription": "subscribed"}}}
_ANY_SUB_COND = {"type": "profile-marketing-consent", "consent": {
    "channel": "email", "can_receive_marketing": True,
    "consent_status": {"subscription": "any"}}}
_SMS_COND = {"type": "profile-marketing-consent", "consent": {
    "channel": "sms", "can_receive_marketing": True,
    "consent_status": {"subscription": "subscribed"}}}
_ZERO_COND = {"type": "profile-metric", "measurement": "count",
              "measurement_filter": {"operator": "equals", "value": 0},
              "timeframe_filter": {"operator": "alltime"}}
_OPENED_COND = {"type": "profile-metric", "measurement": "count",
                "measurement_filter": {"operator": "equals", "value": 3},
                "timeframe_filter": {"operator": "alltime"}}


def test_an_or_group_is_only_as_strong_as_its_weakest_branch():
    """THE AND/OR defect. The first cut FLATTENED all condition groups and asked
    "does ANY condition match", so a group offering a billable branch alongside a
    non-billable one counted as the billable population -- and the verdict could
    flip on nothing but JSON key order. Klaviyo ANDs the GROUPS and ORs the
    conditions WITHIN a group."""
    assert ka.is_billable_basis(
        _grouped("M", "Mixed", [[_BILLABLE_COND, _ANY_SUB_COND]])) is False
    assert ka.is_billable_basis(
        _grouped("M2", "Mixed2", [[_BILLABLE_COND, _SMS_COND]])) is False
    assert ka.is_zero_engagement(
        _grouped("M3", "Mixed3", [[_ZERO_COND, _OPENED_COND]])) is False


def test_the_verdict_does_not_depend_on_condition_order():
    a = ka.is_billable_basis(_grouped("A", "A", [[_BILLABLE_COND, _ANY_SUB_COND]]))
    b = ka.is_billable_basis(_grouped("B", "B", [[_ANY_SUB_COND, _BILLABLE_COND]]))
    assert a == b is False


def test_anded_groups_still_qualify_on_one_sufficient_group():
    """Groups are ANDed, so one group that fully qualifies is enough -- which is
    exactly the live "Never Opened (Email)" shape (metric group AND consent
    group)."""
    assert ka.is_billable_basis(
        _grouped("S", "Split", [[_BILLABLE_COND], [_ZERO_COND]])) is True
    assert ka.is_zero_engagement(
        _grouped("N", "Never Opened", [[_ZERO_COND], [_ANY_SUB_COND]])) is True


def test_the_live_shapes_still_classify_correctly_after_the_group_fix():
    """Regression net over the real payloads: the fix must not have broken the
    classifications that were already right."""
    assert ka.is_billable_basis(ALL_SUBSCRIBED) is True
    assert ka.is_billable_basis(NEVER_OPENED) is False
    assert ka.is_billable_basis(TEXT_SUBSCRIBED) is False
    assert ka.is_zero_engagement(NEVER_OPENED) is True
    assert ka.is_zero_engagement(ALL_SUBSCRIBED) is False


def test_the_client_reads_profile_count_from_the_single_segment_endpoint():
    """`profile_count` is an additional-field on the SINGLE-segment resource, not
    on the collection. Asking /segments for it returns segments with no count, so
    the audit's headline number -- the whole charge basis -- could never be read.
    Pinned on the source so the two-pass shape is not "simplified" back."""
    src = _CLIENT_SRC
    listing = src[src.index("def get_segments("):src.index("def _next_cursor(")]
    assert "additional-fields[segment]" not in listing, (
        "the collection endpoint does not support profile_count")
    assert "get_segment(sid" in listing, "counts must come from the single resource"


def test_the_client_follows_pagination_and_bounds_it():
    """`page[size]` maxes at 10, so one page silently truncated the list -- and a
    truncated list can hide the very segment that IS the billable population,
    which then reads as "no charge basis could be derived"."""
    assert "links" in _CLIENT_SRC and "page[cursor]" in _CLIENT_SRC
    assert kc._MAX_PAGES >= 2
    assert kc._next_cursor(
        {"links": {"next": "https://a.klaviyo.com/api/segments?page%5Bcursor%5D=abc123"}}
    ) == "abc123"
    for junk in (None, {}, {"links": None}, {"links": {"next": None}},
                 {"links": {"next": "not a url"}}):
        assert kc._next_cursor(junk) == ""
