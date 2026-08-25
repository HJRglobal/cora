"""C14 (cq-118f8bbf842e): Klaviyo billing/seat audit -- READ-ONLY report.

WHAT THE SEED ASKED FOR: "charge basis + deactivation candidates, read-only report
to the ops lane. Any deletion stays Harrison-gated."

WHAT VERIFY-FIRST FOUND, and it changes both halves:

  * KLAVIYO EXPOSES NO BILLING, PLAN, SEAT, INVOICE OR TEAM-MEMBER ENDPOINT.
    There is no "what do we pay" call to make. So the charge basis is DERIVED, and
    this module reports the derivation -- the count AND the segment condition that
    selected it -- rather than asserting a number whose provenance the reader
    cannot check.
  * THE SEAT HALF CANNOT COME FROM THE API AT ALL. Seats live in canon: Ops Dept
    OS v1 (Harrison decision 3, 2026-08-08) and the `klaviyo_seat` roster flag on
    org-roles.yaml. The report says so, rather than presenting a roster as if it
    had been reconciled against the account.
  * OPS DEPT OS v1 FORBIDS MORE THAN THE SEED QUOTED. Its "P1 -- Klaviyo email
    consolidation" hard guardrails make BOTH contact-list deletion AND BILLING
    CLEANUP unauthorized. The kickoff quoted only the deletion half. So this
    module recommends nothing and executes nothing: it names candidates and stops.
    `klaviyo_client` is read-only by construction for the same reason.

THE CANDIDATE TEST IS THE SEGMENT'S DEFINITION, NOT ITS NAME. A name-matched
audit ("anything called 'Never Opened'") breaks the moment somebody renames a
segment, and silently -- it would report zero candidates and read as a clean
account. `zero_engagement_candidates` instead walks the condition tree for the
structural signature of non-engagement: a profile-metric condition whose count
equals 0 over all time. That is a fact about the segment, not about its label.

MEASURED LIVE 2026-08-25 (account VFstej, F3Energy, not a test account), for the
record and so the first credentialed run has something to reconcile against:
  * "All Subscribed" -- 4,464 profiles, and its definition is exactly the billable
    shape: profile-marketing-consent, channel=email, can_receive_marketing=true,
    subscription=subscribed. Net -83 over 30 days (158 added, 241 removed).
  * "Never Opened (Email)" -- 2,367, and its consent condition gates on
    subscription=**any**, which is a BROADER population than All Subscribed. So
    4,464 - 2,367 is arithmetic across two different populations and this module
    never does it.
  * "TikTok Shop Masked Emails - Exclude from Campaigns" -- 515. Relay addresses
    that are excluded from campaigns yet still sit in the marketable pool.
  * "Text Subscribed" -- 2,247 (SMS is billed separately from email).
"""

from __future__ import annotations

import logging
from typing import Any

from .connectors import klaviyo_client as kc

log = logging.getLogger(__name__)

#: #founder-operations. Deliberately allowlisted BY ID: name-based channel
#: classification calls this channel TIER_3, so every consumer in the repo
#: (pm_metrics, channel_synthesis) names it explicitly rather than trusting a
#: tier lookup. Doing the same here keeps that convention intact.
OPS_CHANNEL = "C0BCUBUDHAR"


def _condition_groups(segment: dict | None) -> list[list[dict]]:
    """The definition as GROUPS, structure preserved.

    THE SHAPE IS LOAD-BEARING AND FLATTENING IT INVERTS THE LOGIC. Klaviyo ANDs the
    condition GROUPS and ORs the conditions WITHIN a group -- verified against the
    live "Never Opened (Email)" segment, whose zero-engagement predicate and its
    consent predicate sit in two SEPARATE groups precisely because both must hold.
    The first cut flattened everything into one list and asked "does ANY condition
    match", which means:
      * an OR-group offering a billable branch alongside a non-billable one
        counted as the billable population, and
      * the verdict could flip on nothing more than JSON key order.
    So the test is now per-group: a group qualifies only when EVERY condition in
    it qualifies (an OR-group is only as strong as its weakest branch), and the
    segment qualifies when SOME group does (groups are ANDed, so one sufficient
    group is enough).

    isinstance-guarded throughout, not `or {}`-guarded: a non-dict row (an int, a
    string -- what a schema surprise or a partially-parsed page actually looks
    like) has no `.get` and would raise straight into the report.
    """
    if not isinstance(segment, dict):
        return []
    definition = (segment.get("attributes") or {}).get("definition")
    if not isinstance(definition, dict):
        return []
    out: list[list[dict]] = []
    for group in definition.get("condition_groups") or []:
        if not isinstance(group, dict):
            continue
        conds = [c for c in (group.get("conditions") or []) if isinstance(c, dict)]
        if conds:
            out.append(conds)
    return out


def is_billable_basis(segment: dict | None) -> bool:
    """Does this segment select exactly the population Klaviyo's email plan bills?

    Structural, not by name: a marketing-consent condition on the email channel
    with `can_receive_marketing: true` AND `subscription: subscribed`. The
    subscribed clause is what separates the billable population from the broader
    "can receive marketing at all", and conflating the two is how an audit
    reports a confidently wrong number.
    """
    return any(
        all(_is_billable_condition(c) for c in group)
        for group in _condition_groups(segment)
    )


def _is_billable_condition(cond: dict) -> bool:
    """One condition selecting exactly the population Klaviyo's email plan bills:
    marketing consent, email channel, can-receive true, subscription subscribed.

    The `subscribed` clause is what separates the billable population from the
    broader "can receive marketing at all". Conflating them is how an audit
    reports a confidently wrong number -- measured live, "All Subscribed" gates on
    `subscribed` (4,464) while "Never Opened (Email)" gates on `any` (2,367).
    """
    if not isinstance(cond, dict) or cond.get("type") != "profile-marketing-consent":
        return False
    consent = cond.get("consent")
    if not isinstance(consent, dict):
        return False
    if str(consent.get("channel") or "").strip().lower() != "email":
        return False
    if consent.get("can_receive_marketing") is not True:
        return False
    status = consent.get("consent_status")
    if not isinstance(status, dict):
        return False
    return str(status.get("subscription") or "").strip().lower() == "subscribed"


def is_zero_engagement(segment: dict | None) -> bool:
    """Does this segment's DEFINITION select profiles that have never engaged?

    Name-independent on purpose: a name-matched audit reports zero candidates the
    moment somebody renames a segment, and reads as a clean account while doing it.

    Per-GROUP, for the reason `_condition_groups` documents -- an OR-group is only
    as strong as its weakest branch, so a group offering "never opened OR opened
    twice" must not qualify.
    """
    return any(
        all(_is_zero_engagement_condition(c) for c in group)
        for group in _condition_groups(segment)
    )


def _is_zero_engagement_condition(cond: dict) -> bool:
    """One condition meaning "this metric never happened": a profile-metric count
    equal to 0 over an ALL-TIME window.

    All-time is required. A 30-day window of no opens is a recent-activity
    segment, not a never-engaged one, and suppressing on it would cull people who
    simply did not open last month.
    """
    if not isinstance(cond, dict) or cond.get("type") != "profile-metric":
        return False
    if str(cond.get("measurement") or "").lower() != "count":
        return False
    mf = cond.get("measurement_filter")
    if not isinstance(mf, dict):
        return False
    if str(mf.get("operator") or "").lower() != "equals":
        return False
    try:
        if float(mf.get("value")) != 0.0:
            return False
    except (TypeError, ValueError):
        return False
    tf = cond.get("timeframe_filter")
    if isinstance(tf, dict) and str(tf.get("operator") or "").lower() != "alltime":
        return False
    return True


def build_audit(*, segments: list[dict] | None,
                account: dict | None,
                seat_holders: list[dict] | None) -> dict[str, Any]:
    """The audit result. Pure: takes already-fetched data, calls nothing.

    `segments=None` means the read did not happen (unconfigured or failed) and is
    reported as such -- never as an account with no segments, which would read as
    a cancelled account.
    """
    out: dict[str, Any] = {
        "configured": kc.configured(),
        "account_name": "",
        "segments_available": segments is not None,
        "charge_basis": None,
        "candidates": [],
        "other_segments": [],
        "seat_holders": list(seat_holders or []),
    }
    if isinstance(account, dict):
        attrs = account.get("attributes") or {}
        contact = attrs.get("contact_information") or {}
        out["account_name"] = str(contact.get("organization_name") or "").strip()

    if segments is None:
        return out

    rows = [s for s in segments if isinstance(s, dict)]
    for seg in rows:
        entry = {
            "id": kc.segment_id(seg),
            "name": kc.segment_name(seg),
            "count": kc.segment_profile_count(seg),
            "channels": list(kc.marketing_consent_channels(seg)),
            "subscription": kc.subscription_status(seg),
        }
        if is_billable_basis(seg):
            # If more than one segment matches, keep the LARGEST: the billable
            # population is the whole marketable pool, and a narrower segment
            # sharing the same consent shape would understate the bill.
            current = out["charge_basis"]
            if current is None or (entry["count"] or 0) > (current["count"] or 0):
                if current is not None:
                    out["other_segments"].append(current)
                out["charge_basis"] = entry
                continue
        if is_zero_engagement(seg):
            out["candidates"].append(entry)
            continue
        out["other_segments"].append(entry)
    return out


def seat_holders_from_roster() -> list[dict]:
    """Everyone flagged `klaviyo_seat: true` on the roster.

    This is the ONLY seat record that exists -- Klaviyo has no seat endpoint -- so
    it is authoritative-by-default and incomplete-by-nature, and `format_report`
    says both.
    """
    try:
        from . import org_roles  # noqa: PLC0415
        return [
            {"name": r.name, "role": r.role, "entity": r.entity}
            for r in org_roles.all_roles()
            if getattr(r, "klaviyo_seat", False)
        ]
    except Exception:  # noqa: BLE001 -- a report must never die on a roster read
        log.warning("klaviyo_audit: roster unreadable", exc_info=True)
        return []


def _fmt_count(value: Any) -> str:
    """A count, or 'unknown'. NEVER 0 for a missing value: an absent
    `profile_count` means Klaviyo was not asked for it, and rendering that as
    zero would report a live segment as empty."""
    return f"{value:,}" if isinstance(value, int) else "unknown"


def format_report(audit: dict[str, Any]) -> str:
    """The Slack report. Read-only by construction and it says so."""
    name = audit.get("account_name") or "Klaviyo"
    lines = [f":bar_chart: *Klaviyo billing & seat audit — {name}*"]

    # THE BANNER IS GATED ON THE FIGURES, NOT ON THE CREDENTIAL. The first cut
    # keyed it on `configured` alone, which produced the exact contradiction C4
    # exists to stop: "the profile figures below could not be read" printed
    # directly above 4,464 profiles, because the figures had been supplied by a
    # caller while this process held no key. A banner must describe what the
    # reader is actually looking at.
    if not audit.get("segments_available"):
        if not audit.get("configured"):
            lines.append(
                "• :warning: *No `KLAVIYO_API_KEY` is configured*, so the profile "
                "figures could not be read. The seat section still applies — it "
                "comes from canon, not the API."
            )
        else:
            lines.append(
                "• :warning: *The Klaviyo read failed* — figures unavailable this "
                "run. Most likely the pinned API revision; the connector logs it "
                "by name. Reporting nothing rather than a zero."
            )

    basis = audit.get("charge_basis")
    lines.append("")
    lines.append("*Charge basis*")
    if basis:
        lines.append(
            f"• *{_fmt_count(basis.get('count'))} email-marketable profiles* "
            f"— from _{basis.get('name')}_, whose own definition selects "
            f"`can_receive_marketing: true` + `subscription: subscribed` on the "
            f"email channel."
        )
        lines.append(
            "• _Klaviyo publishes no billing, plan or invoice endpoint, so this is "
            "the DERIVED basis, not the invoice. The condition is quoted above so "
            "the derivation is checkable._"
        )
    elif audit.get("segments_available"):
        lines.append(
            "• :warning: No segment in this account selects the billable shape "
            "(email + can_receive_marketing + subscribed), so the charge basis "
            "could not be derived. It has NOT been assumed."
        )
    else:
        lines.append("• Unavailable this run (see above).")

    cands = audit.get("candidates") or []
    lines.append("")
    lines.append("*Deactivation candidates*")
    if cands:
        for c in cands:
            lines.append(f"• _{c.get('name')}_ — {_fmt_count(c.get('count'))} profiles "
                         f"(definition: never engaged, all-time)")
        lines.append(
            "• _Named only. Suppression and deletion are BOTH unauthorized under "
            "Ops Dept OS v1 (contact-list deletion and billing cleanup), so "
            "nothing here is acted on and no recommendation is implied — this is "
            "Harrison's call._"
        )
    elif audit.get("segments_available"):
        lines.append("• None: no segment's definition selects a never-engaged "
                     "population.")
    else:
        lines.append("• Unavailable this run.")

    others = audit.get("other_segments") or []
    if others:
        lines.append("")
        lines.append("*Other segments, for context*")
        for o in others[:10]:
            lines.append(f"• _{o.get('name')}_ — {_fmt_count(o.get('count'))}")
        if len(others) > 10:
            lines.append(f"• _…and {len(others) - 10} more._")

    seats = audit.get("seat_holders") or []
    lines.append("")
    lines.append("*Seats*")
    if seats:
        for s in seats:
            lines.append(f"• {s.get('name')} — {s.get('role')}")
    else:
        lines.append("• No roster entry carries `klaviyo_seat: true`.")
    lines.append(
        "• _Klaviyo exposes NO seat, user or team-member endpoint, so this list "
        "CANNOT be reconciled against the account. It is the roster's record "
        "(`klaviyo_seat` in org-roles.yaml) and it is incomplete by nature — add "
        "the flag as seats are confirmed._"
    )

    lines.append("")
    lines.append("_Read-only audit. Nothing was created, changed, suppressed or "
                 "deleted in Klaviyo._")
    return "\n".join(lines)
