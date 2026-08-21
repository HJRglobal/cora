#!/usr/bin/env python3
"""Mon-Fri 7am AZ — knowledge-review DM and reaction-processing run.

Two jobs in one run:

1. PROCESS REACTIONS: Read cora-reply-log.jsonl, correlate Harrison reactions
   to pending entries in cora-proposed-memory-updates.jsonl, resolve state
   (APPROVED / DISMISSED), and log outcomes. APPROVED items are printed to
   stdout for downstream executors to act on (Component 3 reconciliation_engine
   calls this and handles the action dispatch).

2. SEND DM BATCH: If any updates remain PENDING (no reaction yet), DM Harrison
   a formatted batch summary with 👍/👎 instructions.

Scheduled as: cowork-cora-knowledge-review  Mon-Fri 7am AZ

Exit codes:
    0 = success (ran cleanly)
    1 = fatal error
    2 = partial — DM send failed or no SLACK_BOT_TOKEN
"""

import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cora.knowledge_review import (  # noqa: E402
    apply_autowrite,
    autowrite_level,
    build_decision_blocks,
    build_mechanical_blocks,
    correlate_reactions_to_updates,
    get_pending_updates,
    is_knowledge_update,
    propose_update,
    resolve_update,
    send_dm_to_harrison,
    send_individual_dms,
    HARRISON_SLACK_USER_ID,
    UPDATE_TYPE_DECISION as _kr_UPDATE_TYPE_DECISION,
    UPDATE_TYPE_GENERIC,
)
from cora import review_lanes  # noqa: E402  (mechanical/judgment lane split)
from cora.coras_read import build_coras_read_struct  # noqa: E402  (WS17-C enrichment)
from cora import graduated_trust_shadow as gts  # noqa: E402  (graduated-trust SHADOW)
from cora.tools.user_identity import resolve_slack_mentions  # noqa: E402  (Slice 3)

LOG_DIR = Path(__file__).resolve().parents[1] / "logs"

# Single-instance run lock (audit N2): the pending-DM batch posted 3x at
# 11:51/11:53/11:53 when invocations overlapped before dm_message_ts was
# patched. A best-effort lockfile makes a concurrent invocation a no-op.
_LOCK_PATH = Path(__file__).resolve().parents[1] / "data" / "state" / "knowledge-review.lock"
_LOCK_STALE_SECONDS = 20 * 60

# ── Phase 2.4 rebuild knobs (gate G-D) ───────────────────────────────────────
# Auto-expire: a PENDING item Harrison has SEEN (DM'd) but not acted on for this
# many days is auto-dismissed. Relaxed from the prior 48h now that new-item DMs
# batch WEEKLY (a 48h kill would drop an item before its next weekly review).
_PENDING_EXPIRY_DAYS = 14

# Ledger hygiene (WS17-B item 8): resolved/dismissed rows older than this are
# rotated out of the live ledger into the archive each run, keeping the hot-path
# reads (correlate / get_pending / per-op rewrite) on a small file. Kept a few
# days so a just-dismissed item can still correlate a late reaction / dedup a
# Slack retry before it moves to cold storage.
_ARCHIVE_AFTER_DAYS = 3

# WS-4 ledger boundedness: an OPERATIONAL item still PENDING and never routed
# to an owner after this many days auto-archives as DISMISSED with
# resolved_reason="expired_unrouted" (mirrors the knowledge 14d auto-expire).
# The owner drain moves 10/day and the routing floor excludes the pre-WS17-B
# backlog entirely, so unrouted operational rows otherwise accumulate without
# bound (PENDING grew 3,772 -> 4,277 in the last week of June 2026).
# KNOWLEDGE items are exempt -- the D-051 rule (never auto-dismiss a never-DM'd
# entry) still protects everything in Harrison's queue.
_OPERATIONAL_UNROUTED_EXPIRY_DAYS = 14

# ── WS17-C (D-060): the silent auto-approve is RETIRED ───────────────────────
# Previously, HIGH-confidence machine-mined known_answer updates wrote to
# design/known-answers/*.md WITHOUT a Harrison 👍 (the old Step 1.5). Per the
# System-2 fold decision, EVERYTHING now routes through Harrison's 👍 (D-011
# intact) -- each knowledge DM now carries Cora's read so the review is
# low-effort. The _AUTO_APPROVE_TYPES / _MAX_AUTO_APPROVE_PER_RUN /
# _AUTOAPPROVE_FLOOR_PATH constants, _autoapprove_floor(), and
# _auto_approve_eligible() are gone.

# Weekly digest weekday (Mon=0) in AZ time. NOTE (WS17-B item 4): the knowledge
# stream no longer waits for this day — known_answer / efficiency / #info-for-cora
# items now DM Harrison on EVERY scheduled run so the learning loop isn't stalled
# 5/week. _is_digest_day() is retained as a tested utility (and for any future
# weekly summary) but no longer gates the drain.
_DIGEST_WEEKDAY = 0  # Monday

# ── WS17-B drain split (items 3 + 4) ─────────────────────────────────────────
# Harrison's queue is for KNOWLEDGE (things that make Cora smarter) + the ratify.
# Operational "nudge" types are NOT his job — they route to the entity's domain
# owner as an actionable suggestion (Cora is decision-SUPPORT, not -MAKER; the
# owner acts in the native tool). A #info-for-cora generic is a human knowledge
# contribution, so it rides the knowledge stream, not the operational one.
_KNOWLEDGE_TYPES = frozenset({"known_answer", "efficiency"})
# decision_capture left this set 2026-08-01 (Fork 4): decisions are their OWN
# drain lane -- never-expiring one-tap cards to Harrison, never owner-routed,
# never TTL-expired. Its absence here is load-bearing: it keeps
# _auto_expire_unrouted_operational off decision rows (incl. legacy rows that
# still carry a stamped expires_at).
_OPERATIONAL_TYPES = frozenset(
    {"asana_task", "task_close", "hubspot_note", "generic"}
)

_MAX_KNOWLEDGE_DMS_PER_RUN = 10   # Harrison's daily knowledge queue
_MAX_DECISION_DMS_PER_RUN = 5     # Harrison's decision cards (never expire; pool drains over runs)
_MAX_OWNER_DMS_PER_RUN = 10       # total operational items routed to owners per run
_MAX_OWNER_DMS_PER_OWNER = 5      # per-owner cap so no single owner is flooded

# ── Mechanical review surface (cq-6b014816819c, the 2026-08-19 approval recon) ─
# The recon measured 296 knowledge-queue proposals over 30 days producing 13
# human decisions, of which exactly ONE was a denial: 76 aged out undecided and
# 183 were still open. 179 of the 296 were task_close / asana_task /
# hubspot_note, and they are why nobody reads the queue. They get their own
# surface so the ~85 judgment items are legible again.
#
# DEFAULT OFF. With the flag off this file behaves exactly as it does today --
# mechanical items keep routing to owners as decision-support FYI. Turning it on
# is HALF of a deliberate two-part flip; the other half is adding an approver to
# data/maps/knowledge-approvers.yaml. Enabling the surface with only Harrison
# listed simply sends the cards to him, which is a choice he can make; adding a
# name without enabling the surface changes nothing at all. Neither half is done
# here -- this session builds the surface, it grants nothing.
_MAX_MECHANICAL_DMS_PER_RUN = 5

# How long a mechanical item waits past its review deadline before it escalates
# AGAIN. The first escalation is at expires_at; after that it re-escalates (and,
# when the surface is on, re-cards) at most this often, so an undecided item
# stays visible without becoming a daily nag.
_MECHANICAL_ESCALATION_INTERVAL_DAYS = 7


# How many times an overdue item escalates before it reaches a TERMINAL,
# NAMED disposition. D-206 permits exactly two endings for an undecided item --
# it escalates, or it was something that should never have queued for a human --
# and this is where the second one is decided honestly rather than by a timer
# pretending to be a decision:
#   * a row that WAS carded and got no reaction resolves `escalated_unanswered`;
#   * a row that was never carded at all (no surface enabled, or capped out)
#     resolves `unreviewed_no_surface` -- naming the fact that no human was ever
#     asked, which is the (a) case the approval recon points at.
# Both are counted in the run summary. Neither is silent, and neither claims a
# decision happened. Without a terminal state the pool is unbounded: measured
# mechanical inflow is ~21.5 rows/day against 13 human decisions per 296 items.
_MECHANICAL_MAX_ESCALATIONS = 3


def _mechanical_review_enabled() -> bool:
    """Kill switch for the mechanical review surface. Defined in review_lanes so
    the bot process and this script read ONE definition (D-051 lens-2)."""
    return review_lanes.mechanical_review_enabled()

# Operational-routing floor: only operational items proposed at/after this stamp
# are routed to owners. Initialized to "now" on the first routing run so the
# pre-existing operational backlog (proposed before WS17-B) is NEVER freshly DM'd
# to a teammate months late — it rides Harrison's gated bulk-triage instead.
# "" -> route nothing (fail-safe).
_ROUTING_FLOOR_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "state"
    / "knowledge-review-routing-floor.txt"
)


def _is_digest_day() -> bool:
    """True if today (Arizona) is the weekly digest day.

    Arizona observes NO DST, so a fixed UTC-7 offset is correct AND robust on
    hosts without the IANA tz DB. ZoneInfo('America/Phoenix') raises
    ZoneInfoNotFoundError on this host (no tzdata), which previously fell through
    the bare except to True and silently defeated the weekly cadence. Matches the
    fixed-offset pattern in strategy_memo.py / run_due_date_escalation.py."""
    az_now = datetime.now(timezone(timedelta(hours=-7)))
    return az_now.weekday() == _DIGEST_WEEKDAY


def _acquire_run_lock(log: logging.Logger) -> bool:
    """Return True if this process took the run lock, False if a fresh run holds it."""
    _LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        age = time.time() - _LOCK_PATH.stat().st_mtime
        if age > _LOCK_STALE_SECONDS:
            log.warning("Clearing stale knowledge-review lock (age %.0fs)", age)
            _LOCK_PATH.unlink()
    except FileNotFoundError:
        pass
    try:
        fd = os.open(str(_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    try:
        os.write(fd, f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}\n".encode("utf-8"))
    finally:
        os.close(fd)
    return True


def _release_run_lock() -> None:
    try:
        _LOCK_PATH.unlink()
    except OSError:
        pass


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    log_file = LOG_DIR / f"knowledge-review-{today}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _post_to_slack(token: str, channel: str, text: str) -> None:
    """Post a message to a Slack channel. Silently logs on failure."""
    if not token:
        return
    try:
        from slack_sdk import WebClient as _WC
        _WC(token=token).chat_postMessage(
            channel=channel, text=text, unfurl_links=False, unfurl_media=False
        )
    except Exception as exc:
        logging.getLogger("knowledge-review").warning(
            "gap-executor: Slack post to #%s failed: %s", channel, exc
        )


def _execute_approved_update(update: dict, slack_token: str, log: logging.Logger) -> bool:
    """Execute one approved gap update. Dispatches by update_type.

    asana_task     → create the task via Asana API
    task_close     → mark the task complete via Asana API
    decision       → post formatted entry to #hjrg-leadership for manual add
    hubspot_note   → post formatted note to #hjrg-leadership with deal link
    generic        → post description to #hjrg-leadership

    Returns True when the durable apply succeeded (or the action was an advisory
    post-for-manual-add), False on any apply failure or unexpected error. The D2
    reaction-ack consumes this so it never shows Harrison a false "Saved" (D-051
    remediation): the advisory post branches (decision_capture / hubspot_note /
    plain generic) have no durable write, so a successful post is a truthful ack.
    """
    import json
    update_type = update.get("update_type", "generic")
    payload = update.get("payload") or {}
    desc = update.get("description", "")
    uid_short = update.get("update_id", "?")[:8]
    notify_ch = "hjrg-leadership"
    success = True

    try:
        if update_type == "asana_task":
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
            from cora.tools.asana_client import create_task, AsanaClientError
            task_name = (payload.get("suggested_task_name") or desc)[:150].strip()
            notes = (
                f"Auto-created from Cora reconciliation gap.\n\n"
                f"Evidence: {update.get('source_evidence', '')[:400]}"
            )
            try:
                task = create_task(name=task_name, notes=notes)
                url = task.get("permalink_url", "")
                msg = f":white_check_mark: *Gap executor* created Asana task: <{url}|{task_name}> `[{uid_short}]`"
                log.info("gap-executor: created Asana task gid=%s name=%s", task.get("gid"), task_name)
            except AsanaClientError as exc:
                success = False
                msg = f":warning: *Gap executor* could not create Asana task `[{uid_short}]`: {exc}\n> {task_name}"
                log.warning("gap-executor: create_task failed: %s", exc)
            _post_to_slack(slack_token, notify_ch, msg)

        elif update_type == "task_close":
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
            from cora.tools.asana_client import complete_task, AsanaClientError
            task_gid = payload.get("task_gid", "")
            task_name = payload.get("task_name", task_gid)
            task_url = payload.get("task_url", "")
            if task_gid:
                try:
                    complete_task(task_gid)
                    link = f"<{task_url}|{task_name}>" if task_url else task_name
                    msg = f":white_check_mark: *Gap executor* marked complete: {link} `[{uid_short}]`"
                    log.info("gap-executor: completed task gid=%s", task_gid)
                except AsanaClientError as exc:
                    success = False
                    msg = f":warning: *Gap executor* could not close task `[{uid_short}]`: {exc}\n> {task_name}"
                    log.warning("gap-executor: complete_task failed: %s", exc)
            else:
                success = False
                msg = f":warning: *Gap executor* `[{uid_short}]` task_close missing task_gid — skipped."
                log.warning("gap-executor: task_close payload has no task_gid: %s", payload)
            _post_to_slack(slack_token, notify_ch, msg)

        elif update_type == "decision_capture":
            # Fork 4: an approved decision now has a DURABLE, non-canon landing --
            # the decisions inbox -- instead of the old "add to memory/decisions.md"
            # advisory post (which implied a canon write nothing performed).
            # Promotion into decisions.md stays the Cowork cascade (D-011).
            # APPLY-FIRST-THEN-RESOLVE (D-051 emoji-resolve-before-apply): the
            # Step-1 loop deliberately did NOT resolve this row; this branch
            # resolves APPROVED only after the durable filing succeeds. A
            # deterministic LEX/PHI refusal dismisses (parity with the tap
            # path); any transient failure (I/O, lock busy, screen_error)
            # leaves the row PENDING so the next run's correlate retries.
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
            from cora.decision_inbox import apply_decision_accept
            ok, summary = apply_decision_accept(update, via="emoji_reaction")
            if ok:
                resolve_update(update.get("update_id", ""), "APPROVED",
                               reason="emoji_reaction")
                msg = (
                    f":inbox_tray: *Gap executor* `[{uid_short}]` decision filed to the "
                    f"non-canon inbox ({summary}):\n> {desc[:300]}"
                )
                log.info("gap-executor: decision filed to inbox uid=%s", uid_short)
            elif summary.startswith("excluded:") and "screen_error" not in summary:
                resolve_update(update.get("update_id", ""), "DISMISSED",
                               reason="lex_phi_excluded")
                success = False
                msg = (f":no_entry_sign: *Gap executor* `[{uid_short}]` decision "
                       f"withheld -- LEX/PHI hard-exclusion (fail-closed). Dismissed.")
                log.warning("gap-executor: decision excluded uid=%s: %s",
                            uid_short, summary)
            else:
                success = False
                msg = (f":warning: *Gap executor* `[{uid_short}]` decision inbox filing "
                       f"failed: {summary}. Left pending -- will retry next run.")
                log.warning("gap-executor: decision inbox failed uid=%s: %s",
                            uid_short, summary)
            _post_to_slack(slack_token, notify_ch, msg)

        elif update_type == "known_answer":
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
            from cora.gap_autofill import apply_known_answer
            ok, summary = apply_known_answer(payload)
            q_short = (payload.get("question") or desc)[:160]
            if ok:
                msg = (
                    f":white_check_mark: *Gap executor* `[{uid_short}]` learned a new answer "
                    f"({summary}):\n> Q: {q_short}\n> A: {(payload.get('answer') or '')[:300]}"
                )
                log.info("gap-executor: known_answer applied uid=%s", uid_short)
                # WS-3 golden-set auto-growth: every Harrison-approved fact
                # becomes a standing L1 eval case. Fires only on ok=True (the
                # durable write's PHI re-check passed); id-idempotent, so the
                # dedup-skip / crash-recovery ok=True returns can't double-add.
                # Fail-soft -- never affects the executor or the D-011 gate.
                try:
                    from cora.golden_set import append_case_from_known_answer
                    append_case_from_known_answer(payload)
                except Exception:  # noqa: BLE001
                    log.warning("golden-set auto-growth failed (non-fatal)",
                                exc_info=True)
            else:
                success = False
                msg = f":warning: *Gap executor* `[{uid_short}]` known_answer failed: {summary}"
                log.warning("gap-executor: known_answer failed uid=%s: %s", uid_short, summary)
            _post_to_slack(slack_token, notify_ch, msg)

        elif update_type == "lexicon":
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
            from cora.lexicon_writer import apply_lexicon_update
            ok, summary = apply_lexicon_update(payload)
            term_short = (payload.get("term") or desc)[:120]
            if ok:
                msg = (
                    f":book: *Gap executor* `[{uid_short}]` lexicon term approved "
                    f"({summary}):\n> \"{term_short}\" -> "
                    f"{(payload.get('canonical_name') or '')[:160]} "
                    f"[{payload.get('type', '?')}, {payload.get('entity', '?')}]"
                )
                log.info("gap-executor: lexicon applied uid=%s", uid_short)
                # Golden-set auto-growth (parity with known_answer): fail-soft,
                # id-idempotent, fires only on ok=True (applier PHI screen passed).
                try:
                    from cora.golden_set import append_case_from_lexicon
                    append_case_from_lexicon(payload)
                except Exception:  # noqa: BLE001
                    log.warning("golden-set auto-growth failed (non-fatal)",
                                exc_info=True)
            else:
                success = False
                msg = f":warning: *Gap executor* `[{uid_short}]` lexicon apply failed: {summary}"
                log.warning("gap-executor: lexicon failed uid=%s: %s", uid_short, summary)
            # Render screen at THIS egress (#hjrg-leadership is a multi-person
            # channel; the read side never trusts write-side redaction -- D-051
            # remediation F1). Fail-closed: a screen error also withholds.
            try:
                from cora.phi_guard import is_any_phi
                if is_any_phi(msg):
                    msg = (f":book: *Gap executor* `[{uid_short}]` lexicon item "
                           f"{'applied' if ok else 'failed'} -- details withheld "
                           f"(PHI-shaped); see the local executor log.")
            except Exception:  # noqa: BLE001
                msg = (f":book: *Gap executor* `[{uid_short}]` lexicon item "
                       f"{'applied' if ok else 'failed'} -- details withheld "
                       f"(screen unavailable).")
            _post_to_slack(slack_token, notify_ch, msg)

        elif update_type == "efficiency":
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
            from cora.friction_mining import apply_efficiency
            ok, summary = apply_efficiency(payload)
            title = (payload.get("title") or desc)[:160]
            if ok:
                msg = (
                    f":bulb: *Gap executor* `[{uid_short}]` efficiency finding approved "
                    f"({summary}):\n> {title}\n"
                    f"> Route: {payload.get('route', '?')} | {payload.get('frequency', '')}"
                )
                log.info("gap-executor: efficiency applied uid=%s", uid_short)
            else:
                success = False
                msg = f":warning: *Gap executor* `[{uid_short}]` efficiency apply failed: {summary}"
                log.warning("gap-executor: efficiency failed uid=%s: %s", uid_short, summary)
            _post_to_slack(slack_token, notify_ch, msg)

        elif update_type == "hubspot_note":
            deal_name = payload.get("deal_name", "(unknown deal)")
            deal_url = payload.get("deal_url", "")
            note_text = payload.get("note") or desc
            link = f"<{deal_url}|{deal_name}>" if deal_url else deal_name
            msg = (
                f":pencil: *Gap executor* `[{uid_short}]` — add HubSpot note to {link}:\n"
                f"> {note_text[:400]}"
            )
            log.info("gap-executor: hubspot_note posted to #%s uid=%s", notify_ch, uid_short)
            _post_to_slack(slack_token, notify_ch, msg)

        elif update_type == "generic" and payload.get("source") == "info-for-cora":
            # WS17-B item 5: an approved #info-for-cora contribution actually
            # LEARNS now -- it's written to the entity's known-answers file (the
            # runtime-loaded store), not just posted as a Slack suggestion.
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
            from cora.gap_autofill import apply_contributed_note
            ok, summary = apply_contributed_note(payload)
            snippet = (payload.get("text") or desc)[:300]
            if ok:
                msg = (f":white_check_mark: *Gap executor* `[{uid_short}]` learned a "
                       f"contributed note ({summary}):\n> {snippet}")
                log.info("gap-executor: info-for-cora note applied uid=%s", uid_short)
                # WS-3 golden-set auto-growth (same contract as the
                # known_answer branch above).
                try:
                    from cora.golden_set import append_case_from_note
                    append_case_from_note(payload)
                except Exception:  # noqa: BLE001
                    log.warning("golden-set auto-growth failed (non-fatal)",
                                exc_info=True)
            else:
                success = False
                msg = f":warning: *Gap executor* `[{uid_short}]` note apply failed: {summary}"
                log.warning("gap-executor: info-for-cora note failed uid=%s: %s", uid_short, summary)
            # R4 (fan-out Lens A-2): render screen at THIS egress. #hjrg-leadership
            # is a multi-person channel and this branch interpolates the raw
            # contribution; the lexicon branch ~40 lines above already screens at
            # exactly this point on the principle that the read side never trusts
            # write-side redaction (D-051 remediation F1). Fail-closed: a screen
            # error also withholds.
            try:
                from cora.phi_guard import is_any_phi
                if is_any_phi(msg):
                    msg = (f":white_check_mark: *Gap executor* `[{uid_short}]` a "
                           f"contributed note was filed to "
                           f"{payload.get('entity', 'FNDR')} -- details withheld "
                           f"(PHI-shaped); see the local executor log.")
            except Exception:  # noqa: BLE001
                msg = (f":white_check_mark: *Gap executor* `[{uid_short}]` a "
                       f"contributed note was filed to "
                       f"{payload.get('entity', 'FNDR')} -- details withheld "
                       f"(screen unavailable).")
            _post_to_slack(slack_token, notify_ch, msg)

        else:
            msg = f":information_source: *Gap executor* `[{uid_short}]` ({update_type}): {desc[:300]}"
            log.info("gap-executor: generic action posted uid=%s", uid_short)
            _post_to_slack(slack_token, notify_ch, msg)

    except Exception as exc:
        success = False
        log.error("gap-executor: unexpected error for update %s: %s", uid_short, exc, exc_info=True)
    return success


def _auto_dismiss_stale_pending(entries: list, cutoff_dt, now_dt) -> int:
    """Flip to DISMISSED, in place, only PENDING entries that have ALREADY been
    DM'd to Harrison (dm_message_ts set) and left unreacted past cutoff_dt.
    Returns the count dismissed.

    A never-DM'd PENDING entry is intentionally left alone -- Harrison has not
    seen it yet (Step 2 DMs it this run). Dismissing un-shown entries on age
    alone silently drops a contribution posted right before a >48h gap (e.g. an
    #info-for-cora note Friday evening whose next review is Monday 7am)."""
    from datetime import datetime as _dt
    n = 0
    for e in entries:
        if e.get("update_type") == _kr_UPDATE_TYPE_DECISION:
            # Fork 4: decision cards NEVER expire -- not even DM'd-unreacted.
            # The 63-expired-unseen failure is the exact reason this lane exists.
            continue
        if review_lanes.is_mechanical(e):
            # cq-6b014816819c: mechanical rows only acquire a dm_message_ts now
            # that they have a review surface, and this pass would then dismiss
            # them silently -- undoing the escalation _escalate_stale_mechanical
            # exists to perform, and re-arming on the SEEN side exactly the
            # age-out D-206 forbids on the unseen side.
            #
            # The first version of this comment said "48h later". It is
            # _PENDING_EXPIRY_DAYS (14) measured from proposed_at, not from the
            # DM -- and the correction makes the exemption MORE necessary, not
            # less: every one of the 124 live rows is already older than 14 days,
            # so a mechanical row would have been dismissed by the very next run
            # after it was first shown. The escalation pass is their sole timer.
            continue
        if e.get("state") == "PENDING" and e.get("dm_message_ts"):
            try:
                if _dt.fromisoformat(e["proposed_at"]) < cutoff_dt:
                    e["state"] = "DISMISSED"
                    e["resolved_at"] = now_dt.isoformat()
                    e["resolved_reason"] = "auto_expired_dmd_unreacted"
                    n += 1
            except Exception:
                pass
    return n


def _auto_expire_unrouted_operational(entries: list, cutoff_dt, now_dt) -> int:
    """Flip to DISMISSED, in place, OPERATIONAL entries that are still PENDING,
    were never DM'd anywhere (no dm_message_ts -- not to Harrison, not routed
    to an owner), and are older than cutoff_dt. Returns the count expired.

    WS-4 ledger boundedness. This is a DELIBERATE, spec'd exception to the
    D-051 never-dismiss-unseen rule, scoped strictly to the operational nudge
    stream: those items route to owners at 10/run behind a routing floor, so
    anything unrouted after 14 days (below-floor backlog, LEX-skipped rows,
    cap overflow) is structurally unroutable dead weight that otherwise grows
    the ledger forever. Knowledge items (known_answer / efficiency /
    #info-for-cora generics) are EXEMPT -- Harrison's queue keeps the
    never-expire-unseen guarantee. Unknown update_types are also left alone
    (fail-safe)."""
    from datetime import datetime as _dt
    n = 0
    for e in entries:
        if e.get("state") != "PENDING" or e.get("dm_message_ts"):
            continue
        if _is_knowledge_item(e):
            continue
        if e.get("update_type") == _kr_UPDATE_TYPE_DECISION:
            continue  # Fork 4: never TTL-expired, even legacy rows w/ expires_at
        if review_lanes.is_mechanical(e):
            # cq-6b014816819c / D-206. Every one of the 89 expired_unrouted
            # dismissals on the live ledger was one of these three types --
            # measured, not assumed. They now ESCALATE instead
            # (_escalate_stale_mechanical): an item nobody decided is made
            # louder, never quietly resolved as though a decision happened.
            # What remains here is the bare non-info-for-cora `generic`, which
            # has never produced an expired_unrouted row at all.
            continue
        if e.get("update_type") not in _OPERATIONAL_TYPES:
            continue
        try:
            # Slice 2 TTL-at-creation: honor the per-item expires_at stamped by
            # propose_update when present; fall back to the old fixed
            # proposed_at + cutoff for pre-Slice-2 rows that have no expires_at
            # (back-compat -- the ~286-row pre-existing backlog still expires at
            # the historical 14d).
            exp = e.get("expires_at")
            if exp:
                expired = now_dt >= _dt.fromisoformat(exp)
            else:
                expired = _dt.fromisoformat(e["proposed_at"]) < cutoff_dt
            if expired:
                e["state"] = "DISMISSED"
                e["resolved_at"] = now_dt.isoformat()
                e["resolved_reason"] = "expired_unrouted"
                n += 1
        except Exception:
            pass
    return n


def _mechanical_past_deadline(entry: dict, now_dt) -> bool:
    """Is this PENDING mechanical row past its review deadline?

    The ONE deadline definition, shared by the escalation pass and by the
    read-only dry-run report, so a preview can never disagree with what the
    real run would do. Malformed timestamps read as NOT expired (fail-safe:
    the row stays pending and un-escalated rather than being acted on).
    """
    from datetime import datetime as _dt, timedelta as _td
    if entry.get("state") != "PENDING" or not review_lanes.is_mechanical(entry):
        return False
    try:
        exp = entry.get("expires_at")
        if exp:
            deadline = _dt.fromisoformat(exp)
        else:
            # Pre-TTL-at-creation rows carry no expires_at; use the same
            # fallback window the expiry pass used for them.
            deadline = (_dt.fromisoformat(entry["proposed_at"])
                        + _td(days=_OPERATIONAL_UNROUTED_EXPIRY_DAYS))
        return now_dt >= deadline
    except Exception:
        return False


def _escalate_stale_mechanical(entries: list, now_dt) -> tuple[int, int, int]:
    """Escalate PENDING mechanical rows past their review deadline, in place.

    Returns (newly_escalated_this_run, total_pending_past_deadline,
    retired_to_a_named_terminal_state).

    D-206: escalation and expiry are different things. Until now these rows were
    flipped to DISMISSED/expired_unrouted after 14 days with nobody having seen
    them -- a silent age-out that reads, in every count downstream, exactly like
    a decision. 89 of the live ledger's rows went that way. They now stay
    PENDING and get louder, and when they finally end they end BY NAME.

    `escalated_at` / `escalation_count` are stamped on the row, so "nobody
    decided this for three weeks" is a readable fact rather than an absence, and
    `_rank` in the sender sorts escalated items first.

    THE dm_message_ts IS DELIBERATELY NOT CLEARED (D-051 lens-3 HIGH). The first
    cut cleared it, borrowing the decision lane's re-card mechanism -- but that
    lane's cards resolve by `update_id` through a button, while a mechanical card
    carries NO buttons and its `dm_message_ts` is its ONLY correlation key. So
    clearing it destroyed the approver's decision: Step 0 wiped the ts, Step 1
    re-read the ledger and skipped the row for having none, and the 👍 sat in
    cora-reply-log.jsonl forever keyed to a ts nothing points at. Reproduced by
    the review: 1 pair without the escalation pass, 0 with it. Not re-carding
    also keeps the ORIGINAL card live and reactable, which is what the approver
    is actually looking at, and it leaves the row ineligible for
    expire_stale_operational_updates.py's empty-ts bulk sweep.

    TERMINAL AFTER _MECHANICAL_MAX_ESCALATIONS, by name. Nothing else bounds the
    pool: mechanical inflow measured ~21.5 rows/day, `rotate_resolved` only
    archives rows that are already resolved, and the timer this replaced was 65%
    of all mechanical dispositions. An unbounded PENDING pool is not "louder", it
    is a file that grows forever and is re-parsed on the bot's serving path. So
    an item that has escalated its full budget resolves DISMISSED with a reason
    that says which of D-206's two endings it actually reached -- see
    _MECHANICAL_MAX_ESCALATIONS.
    """
    from datetime import datetime as _dt, timedelta as _td
    newly = 0
    overdue = 0
    retired = 0
    for e in entries:
        if not _mechanical_past_deadline(e, now_dt):
            continue
        overdue += 1
        last = e.get("escalated_at")
        if last:
            try:
                if (now_dt - _dt.fromisoformat(last)) < _td(
                        days=_MECHANICAL_ESCALATION_INTERVAL_DAYS):
                    continue
            except Exception:
                # OVERWRITE a malformed stamp rather than skip past it
                # (D-051 lens-3 MED). The first cut's `except: pass` meant an
                # unparseable escalated_at froze the row FOREVER -- never
                # re-escalated, never retired, never expired by either pass,
                # visible only as a permanent +1 in the overdue counter.
                logging.getLogger("knowledge-review").warning(
                    "mechanical escalation: unparseable escalated_at on %s "
                    "-- restamping", str(e.get("update_id"))[:8])
                last = None
        count = int(e.get("escalation_count") or 0) + 1
        if count > _MECHANICAL_MAX_ESCALATIONS:
            e["state"] = "DISMISSED"
            e["resolved_at"] = now_dt.isoformat()
            e["resolved_reason"] = ("escalated_unanswered"
                                    if e.get("dm_message_ts")
                                    else "unreviewed_no_surface")
            retired += 1
            continue
        e["escalated_at"] = now_dt.isoformat()
        e["escalation_count"] = count
        newly += 1
    return newly, overdue, retired


# Fork 4 D-051 (step0-rmw-zombie / clobbered-tap): a PENDING decision row DM'd
# this long ago with no action is RE-CARDED (dm_message_ts cleared -> a fresh
# card renders next run). Decisions never expire; without this, a row whose
# tap resolution was clobbered by a concurrent whole-ledger rewrite -- or one
# simply buried in Harrison's DM scroll -- would sit PENDING and invisible
# forever (knowledge rows had the 14d backstop; decisions deliberately don't).
_DECISION_RECARD_DAYS = 14


def _self_heal_decisions(entries: list, filed_ids: set, now_dt) -> tuple[int, int]:
    """Fork 4 cross-process belts, in place over the loaded ledger (runs inside
    Step 0's lock+rewrite):

    (a) SELF-HEAL: a PENDING decision row whose update_id already appears in
        the decisions-inbox ledger WAS durably filed -- its APPROVED resolution
        was lost to a concurrent whole-ledger rewrite (bot tap racing this
        script's Step 0 / _patch_dm_ts read-modify-write; the two processes
        hold different in-process locks). Re-resolve it APPROVED.
    (b) RE-CARD: a PENDING decision row DM'd > _DECISION_RECARD_DAYS ago with
        no action gets dm_message_ts cleared so a FRESH card renders -- the
        never-expire guarantee stays visibility, not a zombie state.

    Returns (healed, recarded)."""
    healed = recarded = 0
    for e in entries:
        if e.get("update_type") != _kr_UPDATE_TYPE_DECISION:
            continue
        if e.get("state") != "PENDING":
            continue
        if e.get("update_id") in filed_ids:
            e["state"] = "APPROVED"
            e["resolved_at"] = now_dt.isoformat()
            e["resolved_reason"] = "self_heal_inbox_filed"
            healed += 1
            continue
        ts = str(e.get("dm_message_ts") or "").strip()
        if not ts:
            continue
        try:
            age_days = (now_dt.timestamp() - float(ts)) / 86400.0
        except (ValueError, TypeError):
            continue  # unparseable ts -> leave alone (fail-safe)
        if age_days > _DECISION_RECARD_DAYS:
            e["dm_message_ts"] = ""
            e["dm_channel_id"] = ""
            recarded += 1
    return healed, recarded


def _routing_floor() -> str:
    """ISO timestamp before which operational items are NEVER routed to owners.

    Initialized to 'now' on first call so the pre-WS17-B operational backlog isn't
    freshly DM'd to teammates. Returns '' on any error -> route NOTHING (fail-safe)."""
    try:
        if _ROUTING_FLOOR_PATH.exists():
            return _ROUTING_FLOOR_PATH.read_text(encoding="utf-8").strip()
        _ROUTING_FLOOR_PATH.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).isoformat()
        _ROUTING_FLOOR_PATH.write_text(now, encoding="utf-8")
        return now
    except Exception:
        return ""


def _is_knowledge_item(update: dict) -> bool:
    """True if this update belongs in Harrison's knowledge queue (vs an operational
    nudge routed to an owner). Delegates to knowledge_review.is_knowledge_update so
    the drain's knowledge/operational split and propose_update's TTL-at-creation
    decision share ONE definition and cannot drift (Slice 2)."""
    return is_knowledge_update(update.get("update_type"), update.get("payload"))


def _send_dm_to_user(user_id: str, text: str, slack_token: str, _client_factory=None) -> str | None:
    """DM an arbitrary Slack user. Returns message_ts on success, None on failure.

    Distinct from knowledge_review.send_dm_to_harrison (hard-coded to Harrison) so
    that module keeps its Harrison-only discipline; operational nudges go to owners."""
    if not slack_token or not user_id:
        return None
    try:
        if _client_factory is not None:
            client = _client_factory()
        else:
            from slack_sdk import WebClient as _WC
            client = _WC(token=slack_token)
        dm = client.conversations_open(users=[user_id])["channel"]["id"]
        resp = client.chat_postMessage(
            channel=dm, text=text, unfurl_links=False, unfurl_media=False,
        )
        return resp.get("ts", "")
    except Exception as exc:  # noqa: BLE001 — a failed owner DM must not crash the run
        logging.getLogger("knowledge-review").warning(
            "route-to-owner: DM to %s failed: %s", user_id, exc
        )
        return None


def _ack_reaction_text(action: str, update_type: str, success: bool = True) -> str:
    """The one-liner Cora posts back on a card whose emoji reaction the scheduled
    run just processed (D2). Pure function -- kept separate for testing.

    For APPROVED, `success` reflects whether the durable apply actually landed
    (_execute_approved_update's return): a FAILED apply must NEVER be acked as
    "Saved" (D-051 remediation -- a false success would invert the exact trust
    guarantee D2 exists to provide)."""
    if action == "APPROVED":
        if not success:
            return (":warning: Approved -- but the automatic save didn't go through; "
                    "I've flagged it in #hjrg-leadership.")
        if update_type == "known_answer":
            return ":white_check_mark: Saved to Cora's known-answers."
        if update_type == "efficiency":
            return ":white_check_mark: Logged to the efficiency backlog."
        if update_type == "decision_capture":
            return (":inbox_tray: Filed to your decisions inbox (non-canon; "
                    "promotion stays with the cascade).")
        return ":white_check_mark: Approved -- I've recorded this."
    if action == "DISMISSED":
        return ":x: Dismissed -- no action taken."
    return ""


def _ack_correlated_reaction(reaction: dict, action: str, update: dict,
                             slack_token: str, log: logging.Logger,
                             _client_factory=None, success: bool = True) -> None:
    """D2: acknowledge on the ORIGINAL card that an emoji reaction Harrison already
    made has now been processed by this run -- a threaded one-liner ("Saved to
    known-answers" / "Dismissed") plus, for a SUCCESSFUL approval, a glanceable
    check reaction. Silent processing (the run resolved his reaction with no
    visible response) is the trust-killer this rider exists to fix; an equally bad
    outcome is a FALSE "Saved", so `success` (the durable-apply result) gates both
    the wording and the check reaction (D-051 remediation).

    Only fires for the emoji-fallback path: correlate_reactions_to_updates yields
    reaction_added events, never block_action button taps (those resolve + ack
    in-message in app.py), so there is no double-ack. Fail-soft: an ack error must
    never affect the resolve/execute that already happened."""
    text = _ack_reaction_text(action, update.get("update_type", ""), success)
    if not text or not slack_token:
        return
    channel = (reaction or {}).get("channel_id", "")
    ts = (reaction or {}).get("message_ts", "")
    if not channel or not ts:
        return  # nothing to anchor the ack to
    try:
        if _client_factory is not None:
            client = _client_factory()
        else:
            from slack_sdk import WebClient as _WC
            client = _WC(token=slack_token)
    except Exception as exc:  # noqa: BLE001
        log.warning("reaction-ack: client build failed: %s", exc)
        return
    try:
        client.chat_postMessage(channel=channel, thread_ts=ts, text=text,
                                unfurl_links=False, unfurl_media=False)
    except Exception as exc:  # noqa: BLE001
        log.warning("reaction-ack: threaded reply failed: %s", exc)
    if action == "APPROVED" and success:
        try:
            client.reactions_add(channel=channel, timestamp=ts, name="white_check_mark")
        except Exception:  # noqa: BLE001 -- already_reacted / perms; the reply is the ack
            pass


_OWNER_ITEM_LABELS = {
    "asana_task": "Suggested Asana task",
    "task_close": "Asana task may be done",
    "hubspot_note": "Suggested HubSpot note",
    "generic": "FYI",
}


def _format_owner_item_line(update: dict, idx: int) -> str:
    """One numbered suggestion line inside the batched owner DM (D4). Cora is
    decision-SUPPORT: the owner acts in the native tool; Cora does not."""
    utype = update.get("update_type", "generic")
    # Defensive resolve (Slice 3): strip/resolve any raw <U…> token quoted from swept
    # content -- catches already-PENDING items proposed before the reconciliation-side
    # fix, so no Harrison-facing card ever shows a bare Slack id.
    desc = resolve_slack_mentions((update.get("description") or "(no description)").strip())
    payload = update.get("payload") or {}
    label = _OWNER_ITEM_LABELS.get(utype, utype)
    line = f"{idx}. *{label}*: {desc[:400]}"
    links = []
    deal_url = payload.get("deal_url")
    task_url = payload.get("task_url")
    if deal_url:
        links.append(f"<{deal_url}|Open the deal>")
    if task_url:
        links.append(f"<{task_url}|Open the task>")
    if links:
        line += "\n   " + "  ".join(links)
    return line


def _format_owner_batch_dm(updates: list[dict]) -> str:
    """D3 + D4: ONE decision-support DM per owner per run.

    The no-action line LEADS (D3 -- it was previously the last line, below the
    links) so the informational nature is clear at a glance, and every suggestion
    for this owner in this run is batched into a single message (D4) so a genuinely
    actionable card elsewhere stands out when it arrives. Per-item links preserved."""
    n = len(updates)
    noun = "suggestion" if n == 1 else "suggestions"
    head = (f":information_source: *FYI -- no action needed.* I've routed {n} {noun} "
            f"below; handle any directly in Asana/HubSpot if it looks right. No reply needed.")
    lines = [head, ""]
    for i, u in enumerate(updates, 1):
        lines.append(_format_owner_item_line(u, i))
    return "\n".join(lines)


def _route_operational_to_owners(
    items: list[dict], slack_token: str, log: logging.Logger, _client_factory=None,
) -> int:
    """Route operational-nudge items to their entity's domain owner. Returns the
    count of items routed (marked DISMISSED with reason 'routed_to_owner:<id>').

    D4: each owner receives ONE batched decision-support DM per run whose lead line
    states no action is needed (D3), instead of one DM per item. The routing
    DECISION is unchanged -- floor, HIGH-first order, per-owner + per-run caps, and
    LEX/no-owner skips select the SAME items in the SAME order; only DELIVERY
    collapses from N DMs into one DM per owner:
      * LEX* entities are NEVER routed (PHI) -- left PENDING.
      * Only items proposed >= the routing floor are routed (no stale-backlog spam).
      * Per-owner + per-run caps so no owner is flooded; deferred counts are logged.
      * A failed owner DM leaves that owner's items PENDING for the next run.

    Invariance scope (D-051): on an ALL-SUCCESS run the DISMISSED-as-routed SET is
    identical to the old per-item version. The per-run cap here counts SELECTED
    items (Phase 1), whereas the old version counted SUCCESSFULLY-SENT items, so
    under a PARTIAL DM failure a failed owner's selected items can defer another
    owner's items to the next run. This is strictly conservative (no item is ever
    wrongly dismissed; deferred items stay PENDING and route next run) and is the
    accepted trade-off of per-owner batching -- keeping the per-run cap on selection
    preserves the all-success SET exactly, which the alternative (cap on delivery)
    would not. See test_route_partial_failure_defers_conservatively.
    """
    if not items or not slack_token:
        return 0
    try:
        from cora.gap_autofill import resolve_owner
    except Exception as exc:  # noqa: BLE001
        log.warning("route-to-owner: could not import resolve_owner: %s", exc)
        return 0

    floor = _routing_floor()
    if not floor:
        log.warning("route-to-owner: no routing floor -- routing nothing this run")
        return 0

    # HIGH-confidence first, then oldest first (stable).
    eligible = [u for u in items if u.get("proposed_at", "") >= floor]
    eligible.sort(key=lambda u: (0 if u.get("confidence") == "HIGH" else 1,
                                 u.get("proposed_at", "")))

    # Phase 1 -- SELECT items per owner under the SAME caps and gating as the
    # per-item version (selection order identical -> the DISMISSED-as-routed set on
    # an all-success run is unchanged). dict preserves insertion order (3.7+), so
    # owners are delivered in first-seen order.
    deferred_cap = 0
    skipped_lex = 0
    skipped_no_owner = 0
    selected = 0
    buckets: dict[str, list[dict]] = {}

    for u in eligible:
        if selected >= _MAX_OWNER_DMS_PER_RUN:
            deferred_cap += 1
            continue
        entity = ((u.get("payload") or {}).get("entity") or "FNDR").strip().upper()
        if entity.startswith("LEX"):
            skipped_lex += 1
            continue
        owner = resolve_owner(entity)
        if not owner:
            skipped_no_owner += 1
            continue
        if len(buckets.get(owner, [])) >= _MAX_OWNER_DMS_PER_OWNER:
            deferred_cap += 1
            continue
        buckets.setdefault(owner, []).append(u)
        selected += 1

    # Phase 2 -- DELIVER one batched DM per owner; resolve that owner's items ONLY
    # on a successful send (a failed DM leaves them PENDING to retry next run).
    routed = 0
    for owner, owner_items in buckets.items():
        ts = _send_dm_to_user(
            owner, _format_owner_batch_dm(owner_items), slack_token, _client_factory)
        if not ts:
            continue  # DM failed -- leave this owner's items PENDING, retry next run
        for u in owner_items:
            resolve_update(u["update_id"], "DISMISSED", reason=f"routed_to_owner:{owner}")
        routed += len(owner_items)

    if routed or deferred_cap or skipped_lex or skipped_no_owner:
        log.info(
            "route-to-owner: routed=%d owners=%d deferred(cap)=%d skipped(lex)=%d "
            "skipped(no-owner)=%d below-floor=%d",
            routed, len(buckets), deferred_cap, skipped_lex, skipped_no_owner,
            len([u for u in items if u.get("proposed_at", "") < floor]),
        )
    return routed


def _send_mechanical_review_dms(
    items: list[dict], slack_token: str, log: logging.Logger, _client_factory=None,
) -> int:
    """Send mechanical review cards to the mechanical-lane approver(s).

    Returns the count of cards sent. No-op (returns 0) unless
    CORA_MECHANICAL_REVIEW is on -- with the flag off these items keep routing
    to owners exactly as they did before the split.

    Delivery rules, each with a reason:
      * ESCALATED FIRST, then HIGH-confidence, then oldest. An item that already
        blew its review deadline is the one most in need of a decision, and
        _escalate_stale_mechanical exists precisely so it can be sorted here.
      * LEX-entity items go to HARRISON ONLY, whoever else is listed. PHI: the
        owner-routing path has never sent a LEX item to a teammate, and this
        surface must not become the way one gets there. review_lanes.can_approve
        enforces the same rule again at correlation time, so a card that somehow
        reached the wrong person still could not be acted on by them.
      * Capped per run. The pending mechanical pool is ~124 items; the point of
        the split is a readable surface, not a relocated flood.
      * A failed send leaves the item PENDING and unsent -- it retries next run.
    """
    if not items or not slack_token:
        return 0
    if not _mechanical_review_enabled():
        return 0

    approvers = list(review_lanes.mechanical_approvers())
    delegated = [a for a in approvers if a != HARRISON_SLACK_USER_ID]

    def _rank(u: dict) -> tuple:
        return (0 if int(u.get("escalation_count") or 0) else 1,
                0 if u.get("confidence") == "HIGH" else 1,
                u.get("proposed_at", ""))

    ordered = sorted(items, key=_rank)
    sent_total = 0
    remaining = _MAX_MECHANICAL_DMS_PER_RUN

    # Bucket by recipient, and pick that recipient with the SAME predicate that
    # will decide whether their reaction counts (D-051 lens-2/3/4 HIGH). The
    # first cut re-derived the rule here as `entity.startswith("LEX")`, which
    # reads a MISSING entity as delegable -- 116 of 124 live rows carry no
    # entity, and two of them target [LEX-LLC] Operations. Routing through
    # can_approve means a card can never be sent to someone who would then be
    # refused, and the LEX/PHI + content screens are applied once, in one place.
    buckets: dict[str, list[dict]] = {}
    for u in ordered:
        if remaining <= 0:
            break
        target = next((a for a in delegated if review_lanes.can_approve(u, a)),
                      HARRISON_SLACK_USER_ID)
        buckets.setdefault(target, []).append(u)
        remaining -= 1

    for target, batch in buckets.items():
        n_over = sum(1 for u in batch if int(u.get("escalation_count") or 0))
        # `items` is the UNSENT slice, not the pool. Reporting it as "pending in
        # total" told an approver holding 200 undecided items that 8 were
        # pending -- understating the queue in exactly the direction this feature
        # exists to fix (D-051 lens-3 MED).
        try:
            pool = sum(1 for u in get_pending_updates() if review_lanes.is_mechanical(u))
        except Exception:  # noqa: BLE001 -- a count is never worth a failed send
            pool = len(items)
        header = (
            f":card_index_dividers: {len(batch)} bookkeeping item(s) for review "
            f"({pool} pending in total, {len(items)} not yet shown). React :+1: "
            f"to carry one out or :-1: to dismiss it; I act on them at the next "
            f"review run."
        )
        if n_over:
            # Deliberately not "have been waiting for a decision": on the first
            # run after the surface is enabled EVERY legacy row is already past
            # its 7d deadline, and none of them has ever been shown to anybody.
            header += (f" {n_over} of these are already past their review "
                       f"deadline.")
        if target == HARRISON_SLACK_USER_ID:
            send_dm_to_harrison(header, slack_token, _client_factory=_client_factory)
        else:
            _send_dm_to_user(target, header, slack_token, _client_factory)
        sent_map = send_individual_dms(
            batch, slack_token, _client_factory,
            block_builder=build_mechanical_blocks, recipient_id=target)
        for u in batch:
            ts = sent_map.get(u["update_id"])
            if ts:
                _patch_dm_ts(u["update_id"], ts)
        sent_total += len(sent_map)
        if len(sent_map) < len(batch):
            log.warning("mechanical-review: sent %d/%d to %s (failed sends stay "
                        "PENDING and retry next run)", len(sent_map), len(batch), target)

    log.info("mechanical-review: sent %d card(s) to %d approver(s) of %d unsent",
             sent_total, len(buckets), len(items))
    return sent_total


def _screen_and_send_decision_cards(
    items: list[dict], slack_token: str, log: logging.Logger, _client_factory=None,
) -> tuple[int, int]:
    """Fork 4 decision lane: LEX/PHI-screen unsent decision_capture items, then
    DM Harrison up to _MAX_DECISION_DMS_PER_RUN never-expiring one-tap cards.
    Returns (sent, excluded).

    * Screening is FAIL-CLOSED (decision_inbox.screen_decision: any error =
      excluded). Excluded items are DISMISSED with resolved_reason
      "lex_phi_excluded:<reason>" -- with no TTL on this lane, leaving them
      PENDING would accumulate un-renderable rows forever.
    * Order: HIGH confidence first, then oldest first (stable), matching the
      knowledge queue. Overflow just waits -- cards never expire.
    * A failed DM leaves the item PENDING and unsent (no dm_message_ts), so it
      re-sends next run.
    """
    if not items:
        return 0, 0
    try:
        from cora.decision_inbox import screen_decision
    except Exception as exc:  # noqa: BLE001 -- fail closed: render nothing
        log.warning("decision-cards: could not import screen_decision (%s) -- "
                    "sending nothing this run", exc)
        return 0, 0

    renderable: list[dict] = []
    excluded = 0
    skipped_err = 0
    for u in items:
        try:
            bad, reason = screen_decision(u)
        except Exception:  # noqa: BLE001 -- screen_decision is documented
            bad, reason = True, "screen_error"  # never-raise; belt anyway
        if bad:
            if reason == "screen_error":
                # D-051 (screen-error-terminal-mass-dismiss): a TRANSIENT screen
                # failure must never terminally DISMISS the pool (one bad
                # phi_guard import run would otherwise wipe every PENDING
                # decision unrecoverably). Fail closed on RENDERING only: skip
                # this run, leave the row PENDING for the next.
                skipped_err += 1
                continue
            resolve_update(u["update_id"], "DISMISSED",
                           reason=f"lex_phi_excluded:{reason}")
            excluded += 1
            log.info("decision-cards: excluded %s (%s)",
                     str(u.get("update_id", "?"))[:12], reason)
            continue
        renderable.append(u)
    if skipped_err:
        log.warning("decision-cards: %d row(s) skipped on screen_error (left "
                    "PENDING; will retry next run)", skipped_err)

    renderable.sort(key=lambda u: (0 if u.get("confidence") == "HIGH" else 1,
                                   u.get("proposed_at", "")))
    batch = renderable[:_MAX_DECISION_DMS_PER_RUN]
    if not batch:
        return 0, excluded
    if len(renderable) > len(batch):
        log.info("decision-cards: %d renderable, sending first %d (rest never "
                 "expire and ride the next runs)", len(renderable), len(batch))

    send_dm_to_harrison(
        f"📥 {len(batch)} captured decision(s) below. Accept files each to your "
        f"decisions inbox (non-canon; promotion to decisions.md stays with the "
        f"cascade). These cards never expire.",
        slack_token, _client_factory=_client_factory,
    )
    sent_map = send_individual_dms(batch, slack_token, _client_factory,
                                   block_builder=build_decision_blocks)
    for u in batch:
        ts = sent_map.get(u["update_id"])
        if ts:
            _patch_dm_ts(u["update_id"], ts)
    if len(sent_map) < len(batch):
        log.warning("decision-cards: sent %d/%d (failed sends stay PENDING and "
                    "retry next run)", len(sent_map), len(batch))
    return len(sent_map), excluded


def _autowrite_eligible(update: dict, level: str) -> tuple[bool, int, str]:
    """(eligible, tier, reason) for the graduated-trust auto-write flip (§7B).

    Uses the graduated-trust classifier for the tier, then an INDEPENDENT
    is_high_stakes belt (fails CLOSED: a phi_guard exception counts as
    high-stakes) so a high-stakes item can never auto-write even if the tier were
    miscomputed. NOTE: is_high_stakes does NOT detect conflicts-with-canon -- that
    signal lives ONLY in the coras_read verdict. Tier-0 already requires a
    CORROBORATED verdict, so it fails SAFE when the read is unavailable; Tier-1
    does NOT, so we additionally require Tier-1 to carry a real, non-CONFLICTS
    verdict -- an unavailable/empty read (KB locked, no API key, LLM timeout)
    routes the item to Harrison rather than auto-writing a possibly-conflicting
    fact (D-051 fix). Tier-2 never eligible; Tier-1 only at 'all'; Tier-0 at
    'tier0'/'all'.
    """
    # R3 (fan-out Lens B-3.1, HIGH): a teammate contribution posted in
    # #info-for-cora is NEVER autowrite-eligible, by SOURCE. Verified chain before
    # this exclusion: info-for-cora generics are knowledge-class
    # (knowledge_review.is_knowledge_update), so they entered this scan; the live
    # .env carries CORA_AUTOWRITE_LIVE=all; categorize() allowlists
    # operational/sop/ownership/contacts/logistics/addresses/product_inventory; and
    # the corroboration verdict comes from a Haiku read whose prompt embeds the
    # contribution's OWN text. So an allowlist-category teammate note that read
    # CORROBORATED would have auto-written into always-injected known-answers with
    # zero Harrison tap -- and this branch newly turns ON a previously-dead producer
    # feeding this drain. Excluding by source RESTORES standing doctrine (D-060):
    # teammate-sourced answers stay Harrison-gated by construction.
    if ((update or {}).get("payload") or {}).get("source") == "info-for-cora":
        return False, 2, "info_for_cora_never_autowrites"
    verdict = str(update.get("_coras_read_verdict", ""))
    rec = gts.build_shadow_record(update, verdict)
    tier = int(rec.get("shadow_tier", 2))
    try:
        high, _reasons = gts.is_high_stakes(
            gts.claim_text(update), rec.get("entity", "FNDR"),
            rec.get("category", ""), rec.get("entities") or None)
    except Exception:  # noqa: BLE001 -- belt fails closed
        high = True
    if high or rec.get("conflicts"):
        return False, tier, "high_stakes_or_conflict"
    if tier == 0 and level in ("tier0", "all"):
        return True, 0, "auto_tier0"
    if tier == 1 and level == "all":
        # Fail SAFE: Tier-1 (no corroboration required) auto-writes ONLY when a
        # real non-conflict coras_read verdict was produced. '' (fail-soft/errored
        # read) or CONFLICTS -> route to Harrison, never auto-write.
        v = str(rec.get("coras_read_verdict", "")).strip().upper()
        if v and v != "CONFLICTS":
            return True, 1, "auto_tier1"
        return False, 1, "tier1_read_unavailable"
    return False, tier, "harrison"


def _attach_coras_read(items: list[dict], log: logging.Logger) -> None:
    """Attach a fail-soft 'Cora's read' to each KNOWLEDGE item (WS17-C Part 3).

    Decision-SUPPORT only: the read is advisory text stashed on the in-memory
    update dict -- never persisted, never affects Harrison's gate. Opens ONE
    KnowledgeBase for the batch (items are already capped at the per-run knowledge
    cap); ANY error -- dead KB, missing API key, LLM/parse failure -- leaves the
    item without a read and never blocks the DM.
    """
    if not items:
        return
    kb = None
    try:
        from cora.coras_read import _KB_DB_PATH
        from cora.knowledge_base import KnowledgeBase
        kb = KnowledgeBase(_KB_DB_PATH, check_same_thread=False)
    except Exception as exc:  # noqa: BLE001 -- fall back to no read
        log.warning("coras_read: batch KB open failed (%s) -- proceeding without reads", exc)
        kb = None
    try:
        for it in items:
            try:
                # build_coras_read_struct exposes the structured verdict (WS17-C left
                # it transient). it["_coras_read"] stays the rendered LINE so the DM is
                # byte-identical; it["_coras_read_verdict"] is consumed by the
                # graduated-trust SHADOW pass (decision-SUPPORT, never read by the DM).
                res = build_coras_read_struct(it, kb=kb)
                it["_coras_read"] = res.line
                it["_coras_read_verdict"] = res.verdict
            except Exception as exc:  # noqa: BLE001 -- a read failure must not block the DM
                log.warning("coras_read: attach failed for %s (%s)",
                            str(it.get("update_id", "?"))[:8], exc)
    finally:
        if kb is not None:
            try:
                kb.close()
            except Exception:  # noqa: BLE001
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would happen without sending DMs or writing state changes",
    )
    parser.add_argument(
        "--reset-dm-ts", action="store_true",
        help="Clear dm_message_ts on all PENDING items so they get re-sent as individual DMs",
    )
    parser.add_argument(
        "--force-digest", action="store_true",
        help="(Deprecated since WS17-B: knowledge items now DM every run.) Accepted for compatibility.",
    )
    parser.add_argument(
        "--report", action="store_true",
        help="Print the graduated-trust SHADOW report (counts by tier, would-Tier-0 "
             "rate/week, would-Tier-0 false-positive rate) and exit. Read-only -- no "
             "lock, no drain, no DMs.",
    )
    parser.add_argument(
        "--report-days", type=int, default=None,
        help="With --report: limit to shadow decisions from the last N days.",
    )
    args = parser.parse_args()

    # ── Graduated-trust SHADOW report mode (read-only; no lock, no drain) ────────
    if args.report:
        stats = gts.build_report(LOG_DIR, days=args.report_days)
        print(gts.format_report(stats))
        return 0

    _setup_logging()
    log = logging.getLogger("knowledge-review")
    log.info("=" * 60)
    log.info("Knowledge review run starting (dry_run=%s)", args.dry_run)

    # N2 race guard: refuse to run if another invocation is already in flight,
    # so the same PENDING batch can't be DM'd two or three times in a row.
    if not args.dry_run:
        if not _acquire_run_lock(log):
            log.warning("Another knowledge-review run holds the lock — skipping this invocation.")
            return 0
        import atexit
        atexit.register(_release_run_lock)

    exit_code = 0

    # ─── Optional: reset dm_message_ts so items get re-sent individually ─────
    if args.reset_dm_ts:
        _reset_all_dm_ts()
        log.info("Reset dm_message_ts on all PENDING items — they will be re-sent individually")

    # ─── Step 0: Auto-dismiss stale entries Harrison has SEEN but not acted on ─
    # Only entries already DM'd (dm_message_ts set) and left unreacted past 48h
    # are dismissed. A never-DM'd PENDING entry is NOT dismissed here -- Step 2
    # DMs it this run. Otherwise a fact posted right before a >48h gap (e.g. an
    # #info-for-cora note Friday evening, next review Monday 7am) would be
    # silently dropped before Harrison ever saw it.
    if not args.dry_run:
        import json as _json
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        from cora.knowledge_review import (
            _PROPOSED_UPDATES_PATH, _UPDATES_LOCK, _write_entries_atomic, rotate_resolved,
        )
        now = _dt.now(_tz.utc)
        cutoff = now - _td(days=_PENDING_EXPIRY_DAYS)
        auto_dismissed = 0
        expired_unrouted = 0
        mech_escalated = 0
        mech_overdue = 0
        mech_retired = 0
        if _PROPOSED_UPDATES_PATH.exists():
            with _UPDATES_LOCK:
                entries = []
                malformed: list[str] = []
                for _l in _PROPOSED_UPDATES_PATH.read_text(encoding="utf-8").splitlines():
                    _l = _l.strip()
                    if not _l:
                        continue
                    try:
                        entries.append(_json.loads(_l))
                    except _json.JSONDecodeError:
                        # Preserve malformed lines verbatim rather than crash OR
                        # silently drop them (no-silent-data-loss invariant).
                        malformed.append(_l)
                        log.warning("Step 0: preserving 1 malformed ledger line on rewrite")
                auto_dismissed = _auto_dismiss_stale_pending(entries, cutoff, now)
                # WS-4 ledger boundedness: expire never-routed OPERATIONAL rows
                # past their own cutoff in the SAME pass/rewrite. Knowledge
                # items are exempt (D-051 never-expire-unseen preserved).
                unrouted_cutoff = now - _td(days=_OPERATIONAL_UNROUTED_EXPIRY_DAYS)
                expired_unrouted = _auto_expire_unrouted_operational(
                    entries, unrouted_cutoff, now)
                # D-206: mechanical rows no longer age out silently -- they
                # escalate. Same pass, same lock, same rewrite as the expiry it
                # replaces for those three types.
                mech_escalated, mech_overdue, mech_retired = \
                    _escalate_stale_mechanical(entries, now)
                # Fork 4 cross-process belts: heal filed-but-unresolved decision
                # rows + re-card stale DM'd ones (same lock, same rewrite).
                _filed_ids: set = set()
                try:
                    from cora.decision_inbox import filed_update_ids
                    _filed_ids = filed_update_ids()
                except Exception:  # noqa: BLE001 -- heal is best-effort
                    _filed_ids = set()
                healed, recarded = _self_heal_decisions(entries, _filed_ids, now)
                if healed or recarded:
                    log.info("Decision self-heal: %d filed-row(s) re-resolved "
                             "APPROVED, %d stale card(s) queued for re-send",
                             healed, recarded)
                # atomic — no partial-write window; malformed lines kept verbatim.
                _write_entries_atomic(_PROPOSED_UPDATES_PATH, entries, raw_lines=malformed)
        if auto_dismissed:
            log.info("Auto-dismissed %d stale entries (DM'd >%dd ago, no reaction)",
                     auto_dismissed, _PENDING_EXPIRY_DAYS)
        if expired_unrouted:
            log.info("Expired %d unrouted operational entr%s (PENDING >%dd, never "
                     "DM'd/routed) as expired_unrouted",
                     expired_unrouted, "y" if expired_unrouted == 1 else "ies",
                     _OPERATIONAL_UNROUTED_EXPIRY_DAYS)
        if mech_overdue:
            # The standing measure of the thing D-206 forbids hiding. WARNING,
            # not INFO, and it names the reason nobody can act when that is the
            # reason -- an alarm has to be clearable by doing the right thing,
            # and the right thing here is enabling the surface and deciding.
            detail = ("" if _mechanical_review_enabled() else
                      " -- NO mechanical review surface is enabled "
                      "(CORA_MECHANICAL_REVIEW=off), so nothing can decide them")
            log.warning("MECHANICAL BACKLOG: %d item(s) past their review deadline, "
                        "%d escalated this run, %d retired unanswered%s",
                        mech_overdue, mech_escalated, mech_retired, detail)

        # Ledger hygiene (item 8): rotate old resolved rows to the archive so the
        # live file stays small. Fail-soft — a rotation error must not block review.
        try:
            n_rot = rotate_resolved(_ARCHIVE_AFTER_DAYS)
            if n_rot:
                log.info("Rotated %d resolved row(s) to the archive", n_rot)
        except Exception as exc:  # noqa: BLE001
            log.warning("ledger rotation failed (non-fatal): %s", exc)

    # ─── Step 1: Process any reactions Harrison has already made ─────────────
    pairs = correlate_reactions_to_updates()
    log.info("Found %d reaction-to-update correlations to process", len(pairs))

    approved_updates = []
    dismissed_updates = []
    # D2: token + reaction lookup so a processed reaction can be acknowledged on
    # its original card (dismiss acked in-loop; approve acked after execution).
    ack_token = os.environ.get("SLACK_BOT_TOKEN", "")
    reaction_by_uid: dict[str, dict] = {}

    for update, reaction in pairs:
        uid = update["update_id"]
        reaction_by_uid[uid] = reaction
        action = reaction["action"]
        log.info(
            "Resolving update_id=%s (%s) -> %s",
            uid[:8], update.get("update_type"), action,
        )
        # D-051 (emoji-resolve-before-apply): an APPROVED decision_capture is
        # NOT resolved here -- its durable inbox filing resolves it inside
        # _execute_approved_update (apply-first-then-resolve, mirroring
        # process_decision_tap). Resolving first would strand the decision
        # APPROVED-but-never-filed on a crash or transient filing failure, and
        # with no TTL + dm_ts set nothing would ever retry it. A dry run also
        # skips EXECUTING decisions (the decision branch has a durable write +
        # its own resolve, unlike the advisory branches).
        defer = (action == "APPROVED"
                 and update.get("update_type") == _kr_UPDATE_TYPE_DECISION)
        if not args.dry_run and not defer:
            resolve_update(uid, action)

        if action == "APPROVED":
            if defer and args.dry_run:
                log.info("[DRY RUN] would file decision %s to the inbox", uid[:8])
            else:
                approved_updates.append(update)
        elif action == "DISMISSED":
            dismissed_updates.append(update)
            if not args.dry_run:
                _ack_correlated_reaction(reaction, "DISMISSED", update, ack_token, log)

    if approved_updates:
        log.info("APPROVED %d updates — executing now:", len(approved_updates))
        slack_token = os.environ.get("SLACK_BOT_TOKEN", "")
        for u in approved_updates:
            log.info("  [%s] %s — %s", u["update_type"], u["update_id"][:8], u["description"][:120])
            ok = _execute_approved_update(u, slack_token, log)
            # D2: ack AFTER the apply, gated on its result so "Saved" reflects the
            # durable write and a failed apply is never shown as success (D-051).
            if not args.dry_run:
                _ack_correlated_reaction(
                    reaction_by_uid.get(u["update_id"]) or {}, "APPROVED", u,
                    slack_token, log, success=ok)

    if dismissed_updates:
        log.info("DISMISSED %d updates (no action taken)", len(dismissed_updates))

    # ── Graduated-trust SHADOW: append the real Harrison reaction to the shadow
    # log so --report can mark would-Tier-0 items he thumbs-down'd as false
    # positives. Records ALL resolved reactions (joined by update_id at report
    # time). Non-dry-run only (a dry run does not resolve, so it must not record
    # a reaction that didn't actually happen). FAIL-SOFT -- acts on nothing.
    if not args.dry_run and pairs:
        try:
            gts.record_shadow_reactions(pairs, log_dir=LOG_DIR, logger=log)
        except Exception as exc:  # noqa: BLE001 -- shadow must never affect the run
            log.warning("graduated-shadow: reaction logging error (ignored): %s", exc)

    # ─── Step 2: Drain PENDING updates (WS17-B items 3 + 4) ──────────────────
    # Split the unsent queue: operational "nudge" items route to their entity's
    # domain owner (Cora is decision-SUPPORT); knowledge items (known_answer /
    # efficiency / #info-for-cora contributions) DM Harrison DAILY — no longer
    # Monday-gated, so the learning loop isn't stalled 5/week. Reaction-processing
    # and auto-expire (Steps 0/1) already ran above.
    pending = get_pending_updates()
    unsent = [u for u in pending if not u.get("dm_message_ts")]
    # Three-way split (Fork 4): knowledge -> Harrison's queue; decisions ->
    # Harrison's never-expiring one-tap cards; everything else -> owner routing.
    decision_unsent = [
        u for u in unsent if u.get("update_type") == _kr_UPDATE_TYPE_DECISION
    ]
    knowledge_unsent = [u for u in unsent if _is_knowledge_item(u)]
    operational_unsent = [
        u for u in unsent
        if not _is_knowledge_item(u)
        and u.get("update_type") != _kr_UPDATE_TYPE_DECISION
    ]
    # cq-6b014816819c: the bookkeeping types leave the operational stream and
    # get their own review surface -- but ONLY when that surface is enabled.
    # With the flag off this list is empty and operational_unsent is byte-
    # identical to what it was, so merging this change alone changes nothing
    # that Slack sees.
    mechanical_unsent: list[dict] = []
    if _mechanical_review_enabled():
        mechanical_unsent = [u for u in operational_unsent if review_lanes.is_mechanical(u)]
        operational_unsent = [u for u in operational_unsent
                              if not review_lanes.is_mechanical(u)]
    log.info(
        "Step 2 drain: %d PENDING, %d unsent (%d knowledge, %d decision, "
        "%d mechanical, %d operational)",
        len(pending), len(unsent), len(knowledge_unsent), len(decision_unsent),
        len(mechanical_unsent), len(operational_unsent),
    )

    slack_token = os.environ.get("SLACK_BOT_TOKEN", "")

    if args.dry_run:
        for i, u in enumerate(knowledge_unsent[:_MAX_KNOWLEDGE_DMS_PER_RUN], 1):
            log.info("[DRY RUN] knowledge %d: %s", i, u.get("description", "?")[:120])
        for i, u in enumerate(decision_unsent[:_MAX_DECISION_DMS_PER_RUN], 1):
            log.info("[DRY RUN] decision %d: %s", i, u.get("description", "?")[:120])
        log.info("[DRY RUN] would send up to %d decision card(s) (of %d unsent; "
                 "screening not run in dry-run) and route up to %d operational "
                 "item(s) to owners",
                 min(len(decision_unsent), _MAX_DECISION_DMS_PER_RUN),
                 len(decision_unsent), len(operational_unsent))
        log.info("[DRY RUN] mechanical review surface: %s -- would send up to %d "
                 "card(s) of %d unsent to %s",
                 "on" if _mechanical_review_enabled() else "OFF",
                 min(len(mechanical_unsent), _MAX_MECHANICAL_DMS_PER_RUN),
                 len(mechanical_unsent), ", ".join(review_lanes.mechanical_approvers()))
        # Step 0 does not run in a dry run, so the backlog alarm would be
        # invisible in exactly the preview an operator reads before flipping
        # the flag. Reported read-only here, off the SAME deadline predicate
        # the real escalation uses, so the two can never disagree.
        _dry_overdue = sum(
            1 for u in pending
            if _mechanical_past_deadline(u, datetime.now(timezone.utc)))
        if _dry_overdue:
            log.warning("[DRY RUN] MECHANICAL BACKLOG: %d item(s) past their "
                        "review deadline and awaiting a decision", _dry_overdue)
        return exit_code

    if not slack_token:
        log.warning("SLACK_BOT_TOKEN not set — cannot send DMs / route, exit_code=2")
        return 2

    # ── 2a: Route operational nudges to domain owners (floor-gated, capped) ──
    try:
        n_routed = _route_operational_to_owners(operational_unsent, slack_token, log)
        if n_routed:
            log.info("Routed %d operational nudge(s) to domain owners", n_routed)
    except Exception as exc:  # noqa: BLE001 — routing must not block the knowledge DM
        log.warning("route-to-owner: unexpected error (continuing): %s", exc)

    # ── 2a.5: Decision cards -> Harrison (Fork 4; BEFORE the knowledge-empty
    # early-returns below so an all-decision run still sends its cards) ──────
    try:
        n_dec_sent, n_dec_excluded = _screen_and_send_decision_cards(
            decision_unsent, slack_token, log)
        if n_dec_sent or n_dec_excluded:
            log.info("Decision cards: sent %d, excluded %d (lex_phi_excluded)",
                     n_dec_sent, n_dec_excluded)
    except Exception as exc:  # noqa: BLE001 — decisions must not block the knowledge DM
        log.warning("decision-cards: unexpected error (continuing): %s", exc)

    # ── 2a.6: Mechanical review cards -> the mechanical approver(s) ─────────
    # Before the knowledge-empty early-return below, for the same reason the
    # decision cards are: a run with no knowledge items must still deliver this
    # surface.
    try:
        n_mech = _send_mechanical_review_dms(mechanical_unsent, slack_token, log)
        if n_mech:
            log.info("Sent %d mechanical review card(s)", n_mech)
    except Exception as exc:  # noqa: BLE001 - must not block the knowledge DM
        log.warning("mechanical-review: unexpected error (continuing): %s", exc)

    # ── 2b: Knowledge items → Harrison, every run (item 4) ──────────────────
    k = knowledge_unsent
    if len(k) > _MAX_KNOWLEDGE_DMS_PER_RUN:
        log.info("Capping knowledge DMs: %d -> top %d (HIGH first)",
                 len(k), _MAX_KNOWLEDGE_DMS_PER_RUN)
        k = sorted(k, key=lambda u: 0 if u.get("confidence") == "HIGH" else 1)
        k = k[:_MAX_KNOWLEDGE_DMS_PER_RUN]

    if not k:
        log.info("No knowledge items to DM Harrison this run")
        log.info(
            "Knowledge review complete — approved=%d dismissed=%d pending=%d (exit=%d)",
            len(approved_updates), len(dismissed_updates), len(pending), exit_code,
        )
        return exit_code

    # ── WS17-C: attach Cora's read to each knowledge item (decision-SUPPORT) ──
    # Fail-soft -- a dead KB / LLM never blocks the DM; the read is advisory only.
    _attach_coras_read(k, log)

    # ── Graduated-trust SHADOW (2026-06-29): for each knowledge item being DM'd,
    # compute + PERSIST what graduated trust WOULD have done (tier/decision using
    # the coras_read verdict just attached). ACTS ON NOTHING -- every item below
    # still DMs Harrison exactly as today; this only appends to the shadow log.
    # FAIL-SOFT: a logging error must never affect the DM or the gate.
    try:
        n_shadow = gts.record_shadow_decisions(k, log_dir=LOG_DIR, logger=log)
        if n_shadow:
            log.info("graduated-shadow: logged %d shadow decision(s)", n_shadow)
    except Exception as exc:  # noqa: BLE001 -- shadow must never block the DM
        log.warning("graduated-shadow: decision logging error (ignored): %s", exc)

    # ── Graduated-trust AUTO-WRITE (§7B, D-011 relaxed). DEFAULT OFF: when
    # CORA_AUTOWRITE_LIVE is unset this whole block no-ops and every item DMs
    # Harrison exactly as before. When enabled, Tier-0 (level tier0/all) and
    # Tier-1 (level all) items auto-apply via the SAME idempotent executor the
    # gated path uses; Tier-2 (high-stakes/PHI/cross-entity/conflicts) is NEVER
    # auto-written (classifier + independent belt). Every auto-write is audited +
    # one-tap revertible in the weekly digest. Any apply failure / error routes
    # the item to Harrison (never silently dropped).
    level = autowrite_level()
    if level != "off":
        auto_done = 0
        keep: list[dict] = []
        for u in k:
            try:
                elig, tier, why = _autowrite_eligible(u, level)
            except Exception as exc:  # noqa: BLE001 -- any error -> route to Harrison
                log.warning("autowrite: eligibility error (-> Harrison): %s", exc)
                keep.append(u)
                continue
            if not elig:
                keep.append(u)
                continue
            try:
                ok, summary = apply_autowrite(
                    u, tier=tier, reason=why,
                    contributor=str(gts.contributor_id(u) or ""))
            except Exception as exc:  # noqa: BLE001
                log.warning("autowrite: apply error (-> Harrison): %s", exc)
                keep.append(u)
                continue
            if ok:
                auto_done += 1
            else:
                log.warning("autowrite: apply failed %s (-> Harrison): %s",
                            str(u.get("update_id", ""))[:8], summary)
                keep.append(u)
        if auto_done:
            log.info("autowrite(%s): %d item(s) auto-written; %d -> Harrison",
                     level, auto_done, len(keep))
        k = keep

    if not k:
        log.info("autowrite: all knowledge items handled automatically -- no Harrison DM needed")
        log.info(
            "Knowledge review complete — approved=%d dismissed=%d pending=%d (exit=%d)",
            len(approved_updates), len(dismissed_updates), len(pending), exit_code,
        )
        return exit_code

    send_dm_to_harrison(
        f"Cora knowledge review: {len(k)} item(s) below for your approval. "
        f"React 👍 to approve or 👎 to dismiss each. "
        f"Un-actioned items auto-expire in {_PENDING_EXPIRY_DAYS} days.",
        slack_token,
    )
    log.info("Sending %d individual knowledge DMs to Harrison (user=%s)...",
             len(k), HARRISON_SLACK_USER_ID)
    sent_map = send_individual_dms(k, slack_token)  # {update_id: ts}

    if sent_map:
        log.info("Sent %d/%d knowledge DMs successfully", len(sent_map), len(k))
        for update in k:
            ts = sent_map.get(update["update_id"])
            if ts:
                _patch_dm_ts(update["update_id"], ts)
        log.info("Patched dm_message_ts on %d entries", len(sent_map))
    else:
        log.warning("No knowledge DMs were sent — check SLACK_BOT_TOKEN and im:write scope")
        exit_code = 2

    log.info(
        "Knowledge review complete — approved=%d dismissed=%d pending=%d (exit=%d)",
        len(approved_updates), len(dismissed_updates), len(pending), exit_code,
    )

    # ── Code-session queue (rides this run; zero new scheduled tasks) ────────────
    # Overflow flush on EVERY run; the bundle menu only on the Monday digest day
    # (_is_digest_day). Both no-op unless CORA_CODE_QUEUE=live. Fail-soft: a queue
    # error must never change the knowledge-review exit code.
    if not args.dry_run:
        try:
            from cora import code_queue
            flushed = code_queue.maybe_flush_overflow()
            if flushed:
                log.info("code_queue: flushed %d overflow item(s)", flushed)
            if _is_digest_day() and code_queue.maybe_send_weekly_menu():
                log.info("code_queue: Monday build menu sent")
        except Exception as exc:  # noqa: BLE001
            log.warning("code_queue menu/flush failed (non-fatal): %s", exc)

    return exit_code


def _patch_dm_ts(update_id: str, dm_ts: str) -> None:
    """Patch dm_message_ts on a proposed-update entry in-place (atomic rewrite)."""
    import json
    from cora.knowledge_review import _PROPOSED_UPDATES_PATH, _UPDATES_LOCK

    if not _PROPOSED_UPDATES_PATH.exists():
        return

    with _UPDATES_LOCK:
        entries = []
        with _PROPOSED_UPDATES_PATH.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("update_id") == update_id and not entry.get("dm_message_ts"):
                    entry["dm_message_ts"] = dm_ts
                entries.append(entry)

        tmp = _PROPOSED_UPDATES_PATH.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        tmp.replace(_PROPOSED_UPDATES_PATH)


def _reset_all_dm_ts() -> int:
    """Clear dm_message_ts on all PENDING items so they get re-sent as individual DMs."""
    import json
    from cora.knowledge_review import _PROPOSED_UPDATES_PATH, _UPDATES_LOCK

    if not _PROPOSED_UPDATES_PATH.exists():
        return 0

    count = 0
    with _UPDATES_LOCK:
        entries = []
        with _PROPOSED_UPDATES_PATH.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("state") == "PENDING" and entry.get("dm_message_ts"):
                    entry["dm_message_ts"] = ""
                    entry["dm_channel_id"] = ""
                    count += 1
                entries.append(entry)

        tmp = _PROPOSED_UPDATES_PATH.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        tmp.replace(_PROPOSED_UPDATES_PATH)

    return count


if __name__ == "__main__":
    sys.exit(main())
