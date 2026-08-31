#!/usr/bin/env python3
"""Monday 09:00 AZ -- build and deliver the weekly finance close-support pack.

WHAT THIS DOES
--------------
Builds the deterministic pack (``cora.finance_close.build_pack`` -- every figure
computed in Python, never by a model) and delivers three cuts:

  * FULL pack   -> #hjrg-finance   (C0B3V5SDNAG)
  * FULL pack   -> DM Justin Moran (U0B3AEJCYGP)
  * FOUNDER cut -> #founder-finance (C0BCXPJDP42)   -- flagged items only

CHANNEL TARGETS ARE HARDCODED AND FINANCE-ONLY
----------------------------------------------
#hjr-finance (C0BAK65N4TA) is ARCHIVED -- verified via the Slack API 2026-08-04 --
so posting there fails ``is_archived``, which is precisely the silent-failure mode
the finance-receipt digest logged through July (W4-02). This job therefore targets
#hjrg-finance, which is live and classifies TIER_1 (``channel_classifier``:
function "finance"), so the finance firewall's intent is preserved.

Every delivery target passes through ``_assert_finance_surface`` before a single
byte is sent. There is NO fallback to a non-finance channel: if a finance post
fails, the notice that goes to the ops channel is METADATA-ONLY (counts + reason +
fix, never a figure), so a delivery break is loud without leaking financial
content off a finance surface.

Usage:
    .venv\\Scripts\\python.exe scripts\\run_finance_close_pack.py --dry-run
    .venv\\Scripts\\python.exe scripts\\run_finance_close_pack.py
    .venv\\Scripts\\python.exe scripts\\run_finance_close_pack.py --force
    .venv\\Scripts\\python.exe scripts\\run_finance_close_pack.py --entities F3E,OSNGW

Registered as Windows Task Scheduler task: cowork-cora-finance-close-pack
Schedule: Monday 09:00 AZ (after the weekly cash flow refresh).
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
from cora import run_marker
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(_REPO_ROOT / ".env", override=True)
sys.path.insert(0, str(_REPO_ROOT / "src"))

# Windows consoles default to cp1252, which cannot encode several characters the
# pack renders -- so `--dry-run` died with UnicodeEncodeError on real data (any
# aged-tail line). The dry run is the ONLY pre-flight gate before the first live
# post to a finance channel and Justin's DM, so it must not be the thing that breaks.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):  # pragma: no cover -- non-reconfigurable stream
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            _REPO_ROOT / "logs"
            / f"finance-close-pack-{datetime.datetime.now().strftime('%Y-%m-%d')}.log",
            encoding="utf-8",
        ),
    ],
)
log = logging.getLogger("finance-close-pack")

# ── delivery targets (hardcoded; finance surfaces only) ──────────────────────

# #hjrg-finance -- live, private, TIER_1 (function "finance"). Full pack.
HJRG_FINANCE_CHANNEL = "C0B3V5SDNAG"
# #founder-finance -- live, TIER_1 founder finance surface. Founder cut.
FOUNDER_FINANCE_CHANNEL = "C0BCXPJDP42"
# Justin Moran -- full pack by DM.
JUSTIN_SLACK_ID = "U0B3AEJCYGP"

# A single Slack USER id. Enforced in dm_user so a comma (-> MPIM) or a channel id
# can never widen the DM audience.
_USER_ID_RE = re.compile(r"U[A-Z0-9]{6,}")

# The closed allowlist. A channel id absent from this set can never receive pack
# content -- see _assert_finance_surface. Keyed by id AND carrying the name so the
# guard test can assert the name classifies TIER_1.
FINANCE_SURFACES: dict[str, str] = {
    HJRG_FINANCE_CHANNEL: "hjrg-finance",
    FOUNDER_FINANCE_CHANNEL: "founder-finance",
}

# ARCHIVED 2026-08-04. Pinned here ONLY so the guard test can assert it is never a
# delivery target -- posting to it fails is_archived and reaches nobody.
ARCHIVED_HJR_FINANCE = "C0BAK65N4TA"

_DEDUP_PATH = _REPO_ROOT / "data" / "cache" / "finance-close-pack-sent.json"

# Delivery target names, used as the per-target dedup keys.
TARGET_HJRG = "#hjrg-finance"
TARGET_DM = "DM Justin"
TARGET_FOUNDER = "#founder-finance"
_ALL_TARGETS = (TARGET_HJRG, TARGET_DM, TARGET_FOUNDER)


class DeliveryTargetError(RuntimeError):
    """A non-finance surface was offered pack content. Never caught -- it is a bug."""


def _assert_finance_surface(channel_id: str) -> None:
    """Refuse to send pack content anywhere but a known-live finance channel."""
    if channel_id not in FINANCE_SURFACES:
        raise DeliveryTargetError(
            f"refusing to post finance close-support content to channel {channel_id!r}: "
            f"not in the finance-surface allowlist {sorted(FINANCE_SURFACES)}"
        )


def _ops_alert_channel() -> str:
    """Where a DELIVERY-FAILURE notice goes. NOT a finance surface -- see W4-02.

    The notice is metadata-only by construction (``_delivery_failure_notice``), so
    routing it to an ops channel leaks no financial content.
    """
    return (
        os.environ.get("FINANCE_DIGEST_FALLBACK_CHANNEL", "").strip()
        or os.environ.get("HEALTH_REPORT_CHANNEL", "").strip()
        or "hjrg-leadership"
    )


# ── founder cut ──────────────────────────────────────────────────────────────

_FLAG_MARKERS = (":triangular_flag_on_post:", ":rotating_light:", ":warning:")


# ---------------------------------------------------------------------------
# Justin's DM cut (cq-f330d402e5cd) -- necessity-gated, plain language
# ---------------------------------------------------------------------------
#
# Justin's complaint, verbatim from the 8/18 Finance x Cora meeting: "scattered
# information... my eyes start to roll". He was being DM'd the entire pack --
# nine sections, every entity, flagged and clean alike -- and had to work out for
# himself which parts were addressed to him. When most of a message needs nothing
# from you, the parts that DO need something stop being visible. The pack was not
# too long; it was undifferentiated.
#
# So the DM is now NECESSITY-GATED: a section reaches Justin only when it is
# waiting on an action HE takes. Everything else still ships in full to
# #hjrg-finance, where it is a record rather than an ask. Nothing is deleted and
# no figure is recomputed -- the same computed lines are ROUTED differently.
#
# WHY A STATIC MAP RATHER THAN "sections with flags"
# ---------------------------------------------------
# Flags mark ANOMALIES, not actions, and the two come apart in both directions.
# The Monday worksheet carries no flag and is the single thing he must act on
# every week. A P&L variance flag is real and interesting and is Cora telling him
# something, not asking him for something. Gating on flags alone would have
# dropped the worksheet from his DM and kept a month-over-month expense delta --
# exactly inverting the intent.
#
# NEEDS_JUSTIN is therefore a deliberate editorial list, keyed to what the
# section ASKS OF HIM, with the reason stated in the same place so the two cannot
# drift. Each entry is a plain-language purpose line: one sentence, no jargon,
# saying what he is being asked to do and why.

#: section key -> (does it ask something of Justin, one-line plain-language purpose)
SECTION_PURPOSE: dict[str, tuple[bool, str]] = {
    "cash": (True,
             "Check these against the bank before Monday's sheet -- Cora compares "
             "the cash sheet to the books and cannot see which one is right."),
    "intercompany": (True,
                     "Confirm which of these are genuine intercompany accounts. "
                     "Until you do, Cora treats every one as unconfirmed and "
                     "checks none of them."),
    "close_prep": (True,
                   "The close to-dos for this week."),
    "aging": (True,
              "AR and AP that moved week over week -- collections and payables "
              "to chase."),
    "renewals": (True,
                 "Renewals and payments coming up that need a decision or a "
                 "cancellation before they auto-renew."),
    "qbo_bank": (False,
                 "Freshness of the bank feed vs the books -- informational unless "
                 "something is flagged."),
    "pnl": (False,
            "Month-over-month P&L sanity -- informational unless something is "
            "flagged."),
    "forecast_assist": (False,
                        "Forecast accuracy and next week's carry-in references."),
    "cashflow_parallel": (False,
                          "Parallel run of the sheet against QBO actuals."),
}

#: Fallback for a section key nobody has classified yet. TRUE on purpose: a new
#: section silently vanishing from the one person who acts on this pack is a
#: worse failure than one extra section in his DM, and the missing-key case is
#: exactly what a fresh section looks like. It also fails LOUDLY in the test
#: suite, which pins that every section build_pack emits has an entry here.
_UNCLASSIFIED_NEEDS_ACTION = True


def justin_needs(section_key: str) -> bool:
    return SECTION_PURPOSE.get(section_key, (_UNCLASSIFIED_NEEDS_ACTION, ""))[0]


def build_justin_cut(pack, flag_markers: tuple[str, ...] = _FLAG_MARKERS) -> str:
    """The close pack, cut down to what Justin has to DO.

    Deterministic slice of the same computed lines: it re-renders nothing,
    recomputes nothing, and drops no figure it shows.

    A non-action section is included ONLY when it flagged something -- a flag is
    Cora saying "look at this", which earns a place even in an
    action-only message. When such a section rides along, only its flagged lines
    do, not its whole body.

    Coverage still travels. An action list that quietly omits "this ran on 3 of
    10 entities" reads as a complete list of what needs doing, which is the same
    failure class as an all-clear that was never checked.
    """
    lines = [
        ":ledger: *Your close-support items* — what needs you this week",
        f"_Generated {pack.generated_at}. The full pack is in #hjrg-finance; "
        "this DM is only the parts waiting on you._",
        "",
    ]
    included = 0
    partial_any = False

    for section in pack.sections:
        # Duck-typed for the same reason build_founder_cut is: this repo runs
        # `cora.*` and `src.cora.*` as distinct module objects, and an isinstance
        # check would silently SKIP a section built under the other import path --
        # turning a type mismatch into a quietly shorter action list.
        key = getattr(section, "key", "") or ""
        title = getattr(section, "title", None)
        if title is None:
            continue

        needs = justin_needs(key)
        _, purpose = SECTION_PURPOSE.get(key, (_UNCLASSIFIED_NEEDS_ACTION, ""))

        if not getattr(section, "available", True):
            # An unavailable ACTION section is itself actionable: he is being
            # told a check he relies on did not run this week.
            if needs:
                partial_any = True
                included += 1
                lines.append(f"*{title}*")
                lines.append(f"  _Couldn't run this week — "
                             f"{getattr(section, 'stub_reason', 'no data')}._")
                lines.append("")
            continue

        body = list(getattr(section, "lines", []))
        if not needs:
            hits = [ln for ln in body if any(m in ln for m in flag_markers)]
            if not hits:
                continue
            included += 1
            # A flagged section that ran on part of the portfolio still owes the
            # partial-list caveat -- the docstring promises coverage travels, and
            # the first cut only set the flag on the ACTION branch.
            if getattr(section, "is_partial", False):
                partial_any = True
            lines.append(f"*{title}*")
            lines.append("  _Not an action item, but Cora flagged this:_")
            lines.extend(f"  {h}" for h in hits)
            lines.append("")
            continue

        included += 1
        if getattr(section, "is_partial", False):
            partial_any = True
        lines.append(f"*{title}*")
        if purpose:
            lines.append(f"  _{purpose}_")
        lines.extend(f"  {ln}" for ln in body)
        lines.append("")

    if included == 0:
        lines.append("_Nothing in this week's pack needs an action from you._")
        lines.append("")

    if partial_any:
        lines.append("_Some of the above ran on only part of the portfolio — "
                     "treat it as a partial list, not a complete one._")
    lines.append("_Everything else this week is in #hjrg-finance._")
    return "\n".join(lines)


def build_founder_cut(pack) -> str:
    """Flagged-items-only view for #founder-finance.

    Deterministic slice of the same computed lines -- it re-renders nothing and
    recomputes nothing.

    COVERAGE IS NOT OPTIONAL HERE. Filtering to flag-emoji lines alone dropped the
    per-entity "unavailable" lines and the "N of M" footers, so a run where 9 of 10
    QBO realms 401'd rendered a flat "No item crossed a flag threshold this week" to
    the one reader who sees only this cut. An all-clear is therefore claimable ONLY
    when every section reported full coverage.
    """
    lines = [
        ":ledger: *Finance close-support — founder cut*",
        f"_Generated {pack.generated_at}. Flagged items and coverage gaps; "
        "the full pack is in #hjrg-finance._",
        "",
    ]
    flagged_any = False
    partial_any = False
    for section in pack.sections:
        # Duck-typed deliberately. An isinstance() check against one import path
        # silently SKIPS a section built under the other (this repo runs both
        # `cora.*` and `src.cora.*` as distinct module objects), converting a type
        # mismatch into a false clean bill of health.
        title = getattr(section, "title", None)
        if title is None:
            continue
        if not getattr(section, "available", True):
            partial_any = True
            lines.append(f"*{title}* — _unavailable: {getattr(section, 'stub_reason', '')}_")
            continue
        body = list(getattr(section, "lines", []))
        hits = [ln for ln in body if any(m in ln for m in _FLAG_MARKERS)]
        # Coverage lines carry no emoji, so they are collected explicitly.
        gaps = [ln for ln in body if "unavailable —" in ln or "NOT cross-checked" in ln]
        is_partial = bool(getattr(section, "is_partial", False)) or bool(gaps)
        footer = [ln for ln in body if ln.startswith("_") and " of " in ln] if is_partial else []
        if is_partial:
            partial_any = True
        if not (hits or gaps or footer):
            continue
        if hits:
            flagged_any = True
        lines.append(f"*{title}*")
        lines.extend(f"  {h}" for h in hits)
        lines.extend(f"  {g}" for g in gaps)
        lines.extend(f"  {f}" for f in footer)

    if not flagged_any and not partial_any:
        lines.append("_No item crossed a flag threshold this week, and every section had full coverage._")
    elif not flagged_any:
        lines.append(
            "_No item crossed a flag threshold — but coverage was INCOMPLETE (see above), "
            "so this is not an all-clear._"
        )
    lines.append("")
    footer_note = f"_{pack.total_flags} item(s) flagged across {len(pack.sections)} section(s)."
    if partial_any:
        footer_note += " One or more sections could not cover everything."
    lines.append(footer_note + "_")
    return "\n".join(lines)


def _delivery_failure_notice(failed: list[str], n_flags: int) -> str:
    """METADATA-ONLY failure notice. Must contain no money figure of any kind.

    Pinned by a test that runs channel_content_guard's money-figure detector over
    this string -- the ops channel is not a finance surface.
    """
    return (
        ":warning: *Weekly finance close-support pack could not be fully delivered.* "
        f"The pack was built (flag count: {n_flags}) but delivery failed for: "
        f"{', '.join(failed)}. The channel may be archived, or Cora may not be a "
        "member. *Fix:* confirm those surfaces are live and Cora is in them, or "
        "repoint the target constants in scripts/run_finance_close_pack.py "
        "(FINANCE_SURFACES is a closed allowlist -- adding a channel there is a "
        "deliberate finance-firewall decision). No figures are included in this "
        "notice by design."
    )


# ── dedup ────────────────────────────────────────────────────────────────────

def _iso_week(today: datetime.date | None = None) -> str:
    day = today or datetime.date.today()
    iso = day.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _sent_targets(week: str) -> set[str]:
    """Targets already delivered this ISO week.

    PER-TARGET, not a single week scalar. With one flag for the whole run, a kill
    after #hjrg-finance succeeded but before #founder-finance left nothing recorded,
    so the retry re-posted the full pack to #hjrg-finance and re-DM'd Justin.
    """
    try:
        data = json.loads(_DEDUP_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    if not isinstance(data, dict) or data.get("last_week") != week:
        return set()
    targets = data.get("targets")
    if isinstance(targets, list):
        return {str(t) for t in targets}
    # Legacy scalar-only record from before per-target tracking: a marked week means
    # the whole run completed.
    return set(_ALL_TARGETS)


def _already_sent(week: str) -> bool:
    """True only when EVERY target has been delivered for this week."""
    return set(_ALL_TARGETS).issubset(_sent_targets(week))


def _mark_sent(week: str, targets: set[str] | None = None) -> None:
    delivered = sorted(set(targets) if targets is not None else set(_ALL_TARGETS))
    _DEDUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    _DEDUP_PATH.write_text(
        json.dumps({
            "last_week": week,
            "targets": delivered,
            "sent_at": datetime.datetime.now().isoformat(),
        }),
        encoding="utf-8",
    )


# ── Slack delivery ───────────────────────────────────────────────────────────

def _sanitized(text: str) -> str:
    """Egress boundary + Slack-bold normalization.

    This script imports cora, so the class-level WebClient patch installs in-process
    (B1); sanitizing explicitly as well is belt-and-suspenders on the highest-stakes
    egress surface in the system and satisfies the CI guard on its own terms.
    """
    from cora.reply_formatter import normalize_slack_bold  # noqa: PLC0415
    from cora.slack_egress import sanitize_text  # noqa: PLC0415
    return normalize_slack_bold(sanitize_text(text))


def post_to_channel(client, channel_id: str, text: str) -> bool:
    """Post pack content to an allowlisted finance channel. True on success."""
    _assert_finance_surface(channel_id)
    try:
        client.chat_postMessage(
            channel=channel_id, text=_sanitized(text),
            unfurl_links=False, unfurl_media=False,
        )
        log.info("close-pack: posted to %s (%s)", FINANCE_SURFACES[channel_id], channel_id)
        return True
    except Exception as exc:  # noqa: BLE001 -- one dead surface must not kill the rest
        log.error("close-pack: post to %s failed: %s", channel_id, exc)
        return False


def dm_user(client, user_id: str, text: str) -> bool:
    """DM the full pack. A DM is the user's own private surface, not a channel.

    Shape-guarded: channels get a closed allowlist, so the DM target gets at least a
    format check. A comma in the id would make ``conversations.open`` create an MPIM
    and deliver the full pack to an unintended additional recipient.
    """
    if not _USER_ID_RE.fullmatch(user_id or ""):
        raise DeliveryTargetError(
            f"refusing to DM finance content to {user_id!r}: not a single Slack user id"
        )
    try:
        opened = client.conversations_open(users=[user_id])
        dm_channel = opened["channel"]["id"]
        client.chat_postMessage(
            channel=dm_channel, text=_sanitized(text),
            unfurl_links=False, unfurl_media=False,
        )
        log.info("close-pack: DM sent to %s", user_id)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("close-pack: DM to %s failed: %s", user_id, exc)
        return False


def _alert_build_failure(exc_name: str) -> None:
    """Metadata-only notice that the pack could not be BUILT. Never raises."""
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        log.error("close-pack: no SLACK_BOT_TOKEN -- build-failure alert not sent")
        return
    notice = (
        ":rotating_light: *Weekly finance close-support pack FAILED to build* "
        f"({exc_name}) — no pack was posted to any finance surface this week. "
        "Silence would otherwise read as 'no problems'. *Next step:* run "
        "`scripts\\run_finance_close_pack.py --dry-run` and read "
        "`logs/finance-close-pack-<date>.log`. No figures are included in this notice."
    )
    try:
        from slack_sdk import WebClient  # noqa: PLC0415
        WebClient(token=token).chat_postMessage(
            channel=_ops_alert_channel(), text=_sanitized(notice),
            unfurl_links=False, unfurl_media=False,
        )
        log.info("close-pack: build-failure alert posted to #%s", _ops_alert_channel())
    except Exception as exc:  # noqa: BLE001
        log.error("close-pack: build-failure alert ALSO failed: %s", exc)


def post_ops_alert(client, failed: list[str], n_flags: int) -> None:
    notice = _delivery_failure_notice(failed, n_flags)
    try:
        client.chat_postMessage(
            channel=_ops_alert_channel(), text=_sanitized(notice),
            unfurl_links=False, unfurl_media=False,
        )
        log.info("close-pack: delivery-failure notice posted to #%s", _ops_alert_channel())
    except Exception as exc:  # noqa: BLE001
        log.error("close-pack: delivery-failure notice ALSO failed: %s", exc)


def worksheet_date(pack) -> datetime.date:
    """The date the worksheet's CONTENT was computed for.

    Taken from the pack, never from the wall clock at write time: the pack is
    built with one `today` and every figure in the worksheet was computed
    against it, so a run that straddles midnight would otherwise file Monday's
    worksheet under Tuesday's name. Falls back to today only if the pack's
    stamp is unparseable, which cannot happen from build_pack.
    """
    try:
        return datetime.date.fromisoformat(str(pack.generated_at))
    except (TypeError, ValueError):
        log.warning("close-pack: pack has no parseable generated_at -- "
                    "naming the worksheet by today's date")
        return datetime.date.today()


def write_worksheet(pack, day: datetime.date) -> None:
    """Write the Monday worksheet locally and mirror it into the accounting tree.

    SILENT OVERWRITE IS CORRECT HERE, and the asymmetry with
    `cashflow_ledger.write_snapshot` (which refuses a same-date overwrite) is
    deliberate. A forecast snapshot captures a sheet state that exists for one
    morning only, so replacing it destroys history no later run can recover. The
    worksheet is fully DERIVED from those banked stores -- regenerating it reads
    the same inputs and produces the same file, so refusing would only make a
    legitimate re-run fail and train the operator to reach for a force flag.

    FAIL-SOFT AND NEVER LOAD-BEARING. The pack is the deliverable; the worksheet
    is a durable artifact beside it, so a Drive blip or a read-only mount logs
    and moves on rather than failing a run that already posted to three finance
    surfaces.

    Change-gated on the mirror side (the M1 snapshot pattern): rewriting an
    identical file every Monday is pure churn on a network mount.

    The destination is KB-EXCLUDED by construction -- it sits under
    `01-HJR-Global/accounting/cashflow-ledger/`, whose Drive folder id is pinned
    in `kb_exclusions.KB_EXCLUDED_FOLDER_IDS` (which prunes the whole subtree
    from `sweep_founders_os`) and whose path segment and dated
    `*cashflow-worksheet*` filename are both matched at the store chokepoint. A
    cross-portfolio cash worksheet must never become retrievable KB chunks.
    """
    if not pack.worksheet:
        log.warning("close-pack: no worksheet was built -- nothing written")
        return

    from cora import cashflow_worksheet as cw  # noqa: PLC0415
    from cora import drive_io  # noqa: PLC0415

    try:
        path = cw.write_worksheet(pack.worksheet, day)
        log.info("close-pack: worksheet written to %s", path)
    except OSError as exc:
        log.warning("close-pack: local worksheet write failed: %s", exc)
        return

    target = cw.mirror_worksheet_path(day)
    try:
        existing = drive_io.read_text(target) if drive_io.exists(target) else None
        if existing == pack.worksheet:
            log.info("close-pack: worksheet mirror unchanged -- skipping write")
            return
        drive_io.write_text_atomic(target, pack.worksheet)
        log.info("close-pack: worksheet mirrored to %s", target)
    except drive_io.DriveUnavailable as exc:
        log.warning("close-pack: worksheet mirror skipped (mount unavailable): %s", exc)
    except OSError as exc:
        log.warning("close-pack: worksheet mirror failed: %s", exc)


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print both cuts; post nothing and write no snapshot")
    parser.add_argument("--force", action="store_true",
                        help="Ignore the once-per-ISO-week dedup")
    parser.add_argument("--entities", default="",
                        help="Comma-separated QBO entity codes to limit the run (testing)")
    parser.add_argument("--no-worksheet", action="store_true",
                        help="Skip writing the Monday worksheet file and its Drive mirror")
    parser.add_argument("--worksheet-only", action="store_true",
                        help=("Build and write ONLY the Monday worksheet -- post "
                              "nothing. The recovery path for a worksheet that "
                              "failed to write after the pack was delivered."))
    args = parser.parse_args()

    from cora import finance_close  # noqa: PLC0415

    week = _iso_week()
    log.info("=== finance close-support pack starting (week=%s dry_run=%s force=%s) ===",
             week, args.dry_run, args.force)

    # --worksheet-only deliberately runs BEFORE the dedup check. Recovering a
    # worksheet must not require --force, whose only other effect is to re-post
    # the full pack and the founder cut to three finance surfaces a second time.
    if (not args.dry_run and not args.worksheet_only and not args.force
            and _already_sent(week)):
        log.info("close-pack: already sent for %s -- skipping (use --force to override)", week)
        return 0

    entities = [e.strip().upper() for e in args.entities.split(",") if e.strip()] or None

    try:
        pack = finance_close.build_pack(
            entities=entities,
            # A dry run must not advance the WoW baseline, or the next real run would
            # diff against a snapshot nobody ever saw and report zero movement.
            persist_snapshot=not args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        # A total build failure was previously complete silence to all three readers,
        # and "no pack this Monday" reads as "no problems". Make it loud.
        log.exception("close-pack: build FAILED: %s", exc)
        if not args.dry_run:
            _alert_build_failure(type(exc).__name__)
        return 1

    full = pack.render()
    narration = finance_close.narrate(pack)
    if narration:
        full = (
            f"_Summary (restatement of the facts below):_ {narration}\n\n{full}"
        )
    founder = build_founder_cut(pack)
    # cq-f330d402e5cd: Justin gets the ACTION cut, not the whole pack. The full
    # pack still lands in #hjrg-finance unchanged -- nothing is deleted, the same
    # computed lines are routed by whether they ask something of him.
    justin = build_justin_cut(pack)

    if entities:
        banner = (
            f":warning: *SCOPED RUN* — this pack covers only {', '.join(entities)}, "
            "not the full portfolio.\n\n"
        )
        full, founder, justin = banner + full, banner + founder, banner + justin

    log.info("close-pack: built -- %d flag(s), %d unavailable section(s)",
             pack.total_flags, len(pack.unavailable_sections))

    if args.worksheet_only:
        # No delivery, no dedup mark. The worksheet is derived from banked
        # stores, so this regenerates TODAY's from whatever S1/S2 hold now. It
        # is deliberately NOT a backfill and offers no --week: the carry-in
        # references read the CURRENT daily bank snapshot, so a regenerated past
        # worksheet would carry today's balances under an old date -- the
        # plausible-wrong-figure class this program exists to stop.
        write_worksheet(pack, worksheet_date(pack))
        log.info("=== worksheet-only run complete (nothing posted) ===")
        return 0

    if args.dry_run:
        print("\n" + "=" * 72)
        print(f"[DRY RUN] FULL PACK -> #hjrg-finance ({HJRG_FINANCE_CHANNEL}) + DM Justin")
        print("=" * 72)
        print(full)
        print("\n" + "=" * 72)
        print(f"[DRY RUN] FOUNDER CUT -> #founder-finance ({FOUNDER_FINANCE_CHANNEL})")
        print("=" * 72)
        print(founder)
        print("\n" + "=" * 72)
        print("[DRY RUN] MONDAY WORKSHEET -> cashflow-ledger/worksheets/ "
              "(local + Drive mirror)")
        print("=" * 72)
        # The worksheet is the artifact Justin types from, so the dry run -- the
        # only pre-flight gate before it lands in a shared accounting folder --
        # must show it in full, not merely report that one was built.
        print(pack.worksheet or "(no worksheet was built)")
        return 0

    # Written BEFORE delivery on purpose: the worksheet is a local/Drive artifact
    # with no Slack dependency, and a missing token or a failed post must not also
    # cost Justin the worksheet.
    if args.no_worksheet:
        log.info("close-pack: --no-worksheet -- skipping the worksheet file")
    elif entities:
        log.info("close-pack: scoped run -- worksheet NOT written (it would cover "
                 "only %s while carrying the whole week's filename)",
                 ", ".join(entities))
    else:
        write_worksheet(pack, worksheet_date(pack))

    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not bot_token:
        log.error("close-pack: SLACK_BOT_TOKEN not set -- cannot deliver")
        return 1

    from slack_sdk import WebClient  # noqa: PLC0415
    client = WebClient(token=bot_token)

    # Skip anything a previous (killed) attempt already delivered this week.
    already = set() if args.force else _sent_targets(week)
    if already:
        log.info("close-pack: skipping already-delivered target(s): %s", sorted(already))

    delivered: set[str] = set(already)
    failed: list[str] = []

    def deliver(target: str, fn) -> None:
        if target in already:
            return
        if fn():
            delivered.add(target)
        else:
            failed.append(target)

    deliver(TARGET_HJRG, lambda: post_to_channel(client, HJRG_FINANCE_CHANNEL, full))
    deliver(TARGET_DM, lambda: dm_user(client, JUSTIN_SLACK_ID, justin))
    deliver(TARGET_FOUNDER, lambda: post_to_channel(client, FOUNDER_FINANCE_CHANNEL, founder))

    if failed:
        post_ops_alert(client, failed, pack.total_flags)

    # Record exactly which targets are done, so a retry resumes rather than
    # re-posting. A scoped (--entities) run is NOT the week's pack, so it never
    # records anything -- otherwise it would suppress the real Monday run.
    if delivered and not entities:
        _mark_sent(week, delivered)
    elif entities:
        log.info("close-pack: scoped run -- dedup NOT marked, the full weekly run still owes")

    log.info("=== finance close-support pack complete (%d/%d delivered) ===",
             len(delivered), len(_ALL_TARGETS))
    # S4 run marker. `delivered` is the count of targets that actually received
    # the pack. A run where every target FAILED previously wrote no marker of any
    # kind (_mark_sent is only called on success), leaving it indistinguishable
    # from a run that never happened -- which is the whole class this closes.
    run_marker.write("cowork-cora-finance-close-pack",
                     script="run_finance_close_pack.py", ok=bool(delivered),
                     outputs=len(delivered) if hasattr(delivered, "__len__") else int(bool(delivered)),
                     outcome="delivered" if delivered else "no_targets_delivered")
    return 1 if not delivered else 0


if __name__ == "__main__":
    sys.exit(main())
