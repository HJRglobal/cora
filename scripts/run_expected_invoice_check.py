#!/usr/bin/env python3
"""C13 (cq-015b3bc779e9): monthly "are the expected invoices filed?" check.

Reads data/maps/finance-expected-invoices.yaml, checks the attachment filer's own
content ledger for a matching filing in the last CLOSED month, and posts one
short report to #hjrg-finance.

WHY THIS EXISTS RATHER THAN A RETRIEVAL LANE. The seed asked for a Google Ads
invoice retrieval lane. Verify-first found the retrieval lane already works -- the
filer has been filing Google WORKSPACE invoices monthly on its own -- and that
Google ADS invoices have never arrived at any monitored mailbox at all (zero in
the ledger ever; zero billing emails in 120 days). No retrieval code can fetch a
document that was never delivered. What was actually missing is that nobody is
TOLD, so it stays missing quietly, month after month. This says it out loud until
the delivery is fixed.

CODE #13 SLICE 9 (D-302 + D-310; cq-21207e34a954). Two additions:
  (a) OWNER NUDGE. A MISSING month for a portal-only vendor (known_undelivered)
      DMs the yaml-named `owner:` (resolved through org-roles, fail-closed to
      Harrison) with the portal link + the receipts@ mailbox instruction (the
      one drop point the attachment filer actually reads -- see
      expected_invoices.format_owner_nudge). One DM per missing month;
      PHI-screened (refused, never sent, if the text trips phi_guard.is_any_phi);
      egress via sanitize_text.
  (b) REPEAT SIGNAL. Every MISSING row is a signal (expected-invoice|vendor|
      entity, fire_id = the period). Consecutive missing months escalate by
      changing surface (owner nudge -> Harrison briefing line -> ONE propose-only
      decision card + suppression of the Harrison-facing escalation until his
      ack). A known_undelivered row's original surface is the owner DM -- which
      is NOT suppressed: the owner keeps being nudged at tier 3 and beyond
      (ruled 9.10(iii), Code #14 R14-5). An un-flagged row's surface is the
      channel line, which keeps its quiet "suppressed pending Harrison's ack"
      form at tier 3. A PRESENT month CLEARS the vendor's signal
      (misses that are not consecutive never climb), and a fire is RECORDED only
      after its surface delivered (a Slack-down run never counts as an
      unacknowledged alarm) -- tier 3 excepted, where the card is the surface.

Dry-run is the DEFAULT: prints the report and the nudge plan, posts nothing,
writes NO ledger row (a dry run is a claim about every write site). `--post`
sends the channel report, sends the owner nudges, and records the signals.

Run:  python scripts/run_expected_invoice_check.py [--post] [--period YYYY-MM]
Scheduled: "Cora - Expected Invoice Check", monthly day 9 09:38 AZ (moved from
day 4 on 2026-08-25 for the vendor publication SLA; the day lives in the schtasks
trigger, deployment/setup-monthly-finance-report-tasks.ps1 -- not here).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env", override=True)
sys.path.insert(0, str(_REPO_ROOT / "src"))

# Windows console guard. These reports print emoji (the card's affordance line,
# the report's status glyphs) and the default Windows stdout codec is cp1252, which
# RAISES UnicodeEncodeError on them -- so the dry-run/inspection path, which is the
# first thing the setup PS1 tells you to run, crashed mid-card. Found by running it,
# not by a test: pytest captures stdout through a UTF-8 pipe and never sees cp1252.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

from cora import expected_invoices, repeat_signal  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("expected-invoices")

#: #hjrg-finance -- the same target as the close pack and the adherence check.
#: #hjr-finance (C0BAK65N4TA) has been archived since 2026-08-04.
HJRG_FINANCE_CHANNEL = "C0B3V5SDNAG"


def _client():
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.error("SLACK_BOT_TOKEN not set -- cannot post")
        return None
    from slack_sdk import WebClient  # noqa: PLC0415
    return WebClient(token=token)


def _egress(text: str) -> str:
    # B1 egress doctrine: a WebClient sender in a script that imports no cora
    # module bypasses the class-level patch, so route through the boundary
    # explicitly. (This script DOES import cora, but the CI guard enforces
    # both halves and the explicit call is the reviewable one.)
    from cora.reply_formatter import normalize_slack_bold  # noqa: PLC0415
    from cora.slack_egress import sanitize_text  # noqa: PLC0415
    return normalize_slack_bold(sanitize_text(text))


def _post(text: str, client=None) -> bool:
    client = client or _client()
    if client is None:
        return False
    try:
        client.chat_postMessage(
            channel=HJRG_FINANCE_CHANNEL,
            text=_egress(text),
            unfurl_links=False,
            unfurl_media=False,
        )
        log.info("posted expected-invoice report to #hjrg-finance")
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("post failed: %s", exc)
        return False


def _open_dm(slack_client, user_id: str) -> str | None:
    """Open a DM channel with user_id and return channel ID."""
    try:
        resp = slack_client.conversations_open(users=[user_id])
        return resp["channel"]["id"]
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to open DM with %s: %s", user_id, exc)
        return None


def _send_dm(slack_client, user_id: str, text: str, dry_run: bool):
    """Post the DM and RETURN ITS IDENTITY -- (channel, ts) on success, else None.
    Copied from run_due_date_escalation._send_dm (dry-run aware, identity
    returned) with the egress boundary applied to the body."""
    if dry_run:
        log.info("[DRY-RUN] DM to %s: %s", user_id, text[:120])
        return ("", "")
    dm_ch = _open_dm(slack_client, user_id)
    if not dm_ch:
        return None
    try:
        resp = slack_client.chat_postMessage(channel=dm_ch, text=_egress(text),
                                             unfurl_links=False, unfurl_media=False)
        return (dm_ch, str((resp or {}).get("ts") or ""))
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to send DM to %s: %s", user_id, exc)
        return None


#: The two normal surfaces a MISSING row's signal can have.
SURFACE_OWNER_DM = "dm:owner"
SURFACE_CHANNEL = "#hjrg-finance"


def _fire_kwargs(row: dict[str, Any], period: str, *, owner_id: str, owner_name: str,
                 nudge: bool) -> dict[str, Any]:
    return dict(
        fire_id=period,
        subject=f"{row.get('name')} not filed for {period}",
        entity=str(row.get("entity") or ""),
        owner_slack_id=owner_id, owner_name=owner_name,
        normal_surface=(SURFACE_OWNER_DM if nudge else SURFACE_CHANNEL),
    )


def plan_signals(result: dict[str, Any], *, post: bool) -> dict[str, Any]:
    """PREVIEW the repeat signal for every MISSING row (dry_run=True), and under
    --post record the TIER-3 ones now.

    Returns {vendor name: {"key", "outcome", "nudge", "owner_id", "owner_name",
    "row", "fire_kwargs"}}. A known_undelivered row's normal surface is the owner
    DM; an un-flagged row's is the #hjrg-finance line.

    ORDER OF OPERATIONS (D-051 EF-4). A fire is recorded only AFTER its normal
    surface actually delivered -- `record_signals` does that, once the DMs and the
    channel post have been attempted. Recording first counted three Slack-down
    months as three unacknowledged fires and then SUPPRESSED the first DM that
    would have worked. The one exception is tier 3: there the surface IS the card
    mint (Harrison's tap is the only way out), so a tier-3 outcome is recorded
    here regardless of Slack, and its REAL outcome (the card may be withheld by
    the LEX/PHI screen -> not suppressed) replaces the preview. Nothing is
    written without --post.
    """
    period = str(result.get("period") or "")
    out: dict[str, Any] = {}
    if not result.get("available"):
        return out
    for row in result.get("results") or []:
        if row.get("status") != expected_invoices.STATUS_MISSING:
            continue
        nudge = bool(row.get("known_undelivered"))
        owner_id, owner_name, _resolved = expected_invoices.resolve_owner(row.get("owner"))
        key = expected_invoices.signal_key_for(row)
        kwargs = _fire_kwargs(row, period, owner_id=owner_id, owner_name=owner_name,
                              nudge=nudge)
        outcome = repeat_signal.fire(key, dry_run=True, **kwargs)
        if post and outcome.tier >= repeat_signal.TIER_CARD:
            # the card IS the surface: mint/record now, regardless of Slack
            outcome = repeat_signal.fire(key, dry_run=False, **kwargs)
        out[str(row.get("name"))] = {
            "key": key, "outcome": outcome, "nudge": nudge,
            "owner_id": owner_id, "owner_name": owner_name,
            "row": row, "fire_kwargs": kwargs,
        }
    return out


def record_signals(signals: dict[str, Any], *, delivered_dm: set[str],
                   channel_posted: bool) -> list[str]:
    """Record the tier-1/2 fires whose normal surface DELIVERED (D-051 EF-4):
    an owner-DM signal when its DM posted (`delivered_dm` holds the vendor
    names), a channel-line signal when the #hjrg-finance report posted. A fire
    that reached no human is logged and NOT recorded -- it never climbs the
    ladder. Tier-3 outcomes were recorded in plan_signals (the card is the
    surface) and are skipped here. Returns the vendor names recorded."""
    recorded: list[str] = []
    for name, sig in signals.items():
        outcome = sig.get("outcome")
        if outcome is None or outcome.recorded or outcome.tier >= repeat_signal.TIER_CARD:
            continue
        delivered = (name in delivered_dm) if sig.get("nudge") else channel_posted
        if not delivered:
            log.warning("signal %s NOT recorded -- its surface (%s) did not deliver this "
                        "run, so the fire never reached a human", sig.get("key"),
                        sig["fire_kwargs"]["normal_surface"])
            continue
        real = repeat_signal.fire(sig["key"], dry_run=False, **sig["fire_kwargs"])
        sig["outcome"] = real
        recorded.append(name)
    return recorded


def reconcile_clears(result: dict[str, Any], *, post: bool) -> list[tuple[str, str]]:
    """CLEAR the signals whose fact has gone away (D-051 EF-1; mirrors
    nightly_health_check._gate_reconcile_clears):

      * a vendor PRESENT this period -> clear(why="invoice filed for <period>");
      * a live `expected-invoice|...` key with no row in the yaml any more (the
        vendor was removed or renamed) -> clear(why="retired from the yaml").

    Without this, misses that were NOT consecutive still climbed the ladder --
    MISSING, MISSING, PRESENT, MISSING read as consecutive=3 -- and a tier-3
    suppression outlived any number of clean months. UNKNOWN rows clear nothing
    (we could not look), and an UNAVAILABLE check clears nothing at all.
    Returns [(key, why)] planned; written only under --post.
    """
    planned: list[tuple[str, str]] = []
    if not result.get("available"):
        return planned
    period = str(result.get("period") or "")
    rows = result.get("results") or []
    known_keys = {expected_invoices.signal_key_for(r) for r in rows}
    for row in rows:
        if row.get("status") == expected_invoices.STATUS_PRESENT:
            planned.append((expected_invoices.signal_key_for(row),
                            f"invoice filed for {period}"))
    try:
        live = repeat_signal.active_keys(prefix=expected_invoices.SIGNAL_TASK + "|")
    except Exception:  # noqa: BLE001 -- bookkeeping never breaks the check
        log.warning("expected-invoices: could not list live signals", exc_info=True)
        live = []
    for key in live:
        if key not in known_keys:
            planned.append((key, "vendor no longer in finance-expected-invoices.yaml"))
    for key, why in planned:
        if post:
            if repeat_signal.clear(key, why=why):
                log.info("signal %s CLEARED (%s)", key, why)
        else:
            print(f"[clear] {key} -- would clear ({why})")
    return planned


def send_nudges(result: dict[str, Any], signals: dict[str, Any], *,
                post: bool, client=None, delivered: set[str] | None = None) -> int:
    """One owner DM per nudge candidate -- at EVERY tier, including tier 3 and
    beyond. Returns the number of DMs sent (0 in dry-run); `delivered`, when
    given, is filled with the vendor names whose DM actually posted
    (record_signals reads it). PHI-screened: a nudge whose rendered text trips
    phi_guard.is_any_phi is refused and logged, never sent.

    TIER 3 KEEPS NUDGING THE OWNER (ruled 2026-09-19, 9.10(iii): "keep nudging
    the owner; suppress only the Harrison-facing escalation"; Code #14 R14-5).
    The old code skipped the owner DM whenever outcome.suppressed, so from the
    third missed month on the one person who could file the invoice heard
    nothing until Harrison tapped the card. The Harrison-facing escalation stays
    suppressed: the card is minted once per cycle (plan_signals), the tier-2
    briefing line drops out at tier 3 (tier2_signals needs consecutive == 2),
    and the non-nudge #hjrg-finance line still renders its quiet form (main's
    `suppressed` dict). record_signals skips tier >= 3, so a tier-3 DM never
    double-records."""
    from cora.phi_guard import is_any_phi  # noqa: PLC0415
    period = str(result.get("period") or "")
    sent = 0
    for row in expected_invoices.nudge_candidates(result):
        name = str(row.get("name"))
        sig = signals.get(name) or {}
        outcome = sig.get("outcome")
        if outcome is not None and outcome.suppressed:
            log.info("nudge for %s still sent at tier %s (Harrison card %s pending; "
                     "only the Harrison-facing escalation is suppressed)", name,
                     outcome.tier, outcome.card_update_id)
        text = expected_invoices.format_owner_nudge(
            row, period, owner_name=str(sig.get("owner_name") or ""))
        if is_any_phi(text):
            log.error("nudge for %s REFUSED -- rendered text trips the PHI screen; "
                      "check the yaml note/name", name)
            continue
        owner_id = str(sig.get("owner_id") or expected_invoices._HARRISON_SLACK_ID)
        print(f"\n[nudge -> {sig.get('owner_name') or owner_id} ({owner_id})]\n{text}")
        if not post:
            continue
        client = client or _client()
        if client is None:
            return sent
        ident = _send_dm(client, owner_id, text, dry_run=False)
        if ident is None:
            log.error("nudge DM to %s failed for %s", owner_id, name)
            continue
        sent += 1
        if delivered is not None:
            delivered.add(name)
        log.info("nudge DM sent to %s for %s (tier %s)", owner_id, name,
                 getattr(outcome, "tier", "?"))
    return sent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--post", action="store_true",
                    help="post to #hjrg-finance, DM the owners and record the "
                         "repeat signals (default: print only, write nothing)")
    ap.add_argument("--period", default="",
                    help="YYYY-MM to check (default: the last closed month)")
    args = ap.parse_args(argv)

    result = expected_invoices.assess(args.period or None)
    # 1. the fact side first: PRESENT / retired vendors clear their signals
    reconcile_clears(result, post=args.post)
    # 2. preview every MISSING signal (tier-3 recorded now: the card is the surface)
    signals = plan_signals(result, post=args.post)
    suppressed = {
        name: s["outcome"].card_update_id
        for name, s in signals.items()
        if s["outcome"].suppressed and not s["nudge"]
    }
    report = expected_invoices.format_report(result, suppressed=suppressed)
    print(report)
    flags = expected_invoices.flag_count(result)
    print(f"\n[{flags} row(s) need a human]")
    for name, s in signals.items():
        o = s["outcome"]
        print(f"[signal] {s['key']} -> tier {o.tier} (consecutive {o.consecutive}"
              f"{', SUPPRESSED card ' + str(o.card_update_id) if o.suppressed else ''})")

    # 3. deliver
    client = None
    if args.post:
        client = _client()
    delivered_dm: set[str] = set()
    sent = send_nudges(result, signals, post=args.post, client=client,
                       delivered=delivered_dm)

    if not args.post:
        print("\n(dry run -- nothing posted, no DM sent, no ledger row written; "
              "pass --post to send)")
        return 0
    ok = _post(report, client=client)
    # 4. record ONLY the fires whose surface delivered
    recorded = record_signals(signals, delivered_dm=delivered_dm, channel_posted=ok)
    log.info("owner nudges sent: %d; signals recorded: %d", sent, len(recorded))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
