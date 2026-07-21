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
    correlate_reactions_to_updates,
    get_pending_updates,
    propose_update,
    resolve_update,
    send_dm_to_harrison,
    send_individual_dms,
    HARRISON_SLACK_USER_ID,
    UPDATE_TYPE_GENERIC,
)
from cora.coras_read import build_coras_read_struct  # noqa: E402  (WS17-C enrichment)
from cora import graduated_trust_shadow as gts  # noqa: E402  (graduated-trust SHADOW)

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
_OPERATIONAL_TYPES = frozenset(
    {"asana_task", "task_close", "hubspot_note", "decision_capture", "generic"}
)

_MAX_KNOWLEDGE_DMS_PER_RUN = 10   # Harrison's daily knowledge queue
_MAX_OWNER_DMS_PER_RUN = 10       # total operational items routed to owners per run
_MAX_OWNER_DMS_PER_OWNER = 5      # per-owner cap so no single owner is flooded

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


def _execute_approved_update(update: dict, slack_token: str, log: logging.Logger) -> None:
    """Execute one approved gap update. Dispatches by update_type.

    asana_task     → create the task via Asana API
    task_close     → mark the task complete via Asana API
    decision       → post formatted entry to #hjrg-leadership for manual add
    hubspot_note   → post formatted note to #hjrg-leadership with deal link
    generic        → post description to #hjrg-leadership
    """
    import json
    update_type = update.get("update_type", "generic")
    payload = update.get("payload") or {}
    desc = update.get("description", "")
    uid_short = update.get("update_id", "?")[:8]
    notify_ch = "hjrg-leadership"

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
                    msg = f":warning: *Gap executor* could not close task `[{uid_short}]`: {exc}\n> {task_name}"
                    log.warning("gap-executor: complete_task failed: %s", exc)
            else:
                msg = f":warning: *Gap executor* `[{uid_short}]` task_close missing task_gid — skipped."
                log.warning("gap-executor: task_close payload has no task_gid: %s", payload)
            _post_to_slack(slack_token, notify_ch, msg)

        elif update_type == "decision_capture":
            formatted = payload.get("formatted_entry") or payload.get("decision_text") or desc
            msg = (
                f":pencil: *Gap executor* `[{uid_short}]` — add to `memory/decisions.md`:\n"
                f"```{formatted[:600]}```"
            )
            log.info("gap-executor: decision_capture posted to #%s uid=%s", notify_ch, uid_short)
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
                msg = f":warning: *Gap executor* `[{uid_short}]` known_answer failed: {summary}"
                log.warning("gap-executor: known_answer failed uid=%s: %s", uid_short, summary)
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
                msg = f":warning: *Gap executor* `[{uid_short}]` note apply failed: {summary}"
                log.warning("gap-executor: info-for-cora note failed uid=%s: %s", uid_short, summary)
            _post_to_slack(slack_token, notify_ch, msg)

        else:
            msg = f":information_source: *Gap executor* `[{uid_short}]` ({update_type}): {desc[:300]}"
            log.info("gap-executor: generic action posted uid=%s", uid_short)
            _post_to_slack(slack_token, notify_ch, msg)

    except Exception as exc:
        log.error("gap-executor: unexpected error for update %s: %s", uid_short, exc, exc_info=True)


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
        if e.get("update_type") not in _OPERATIONAL_TYPES:
            continue
        try:
            if _dt.fromisoformat(e["proposed_at"]) < cutoff_dt:
                e["state"] = "DISMISSED"
                e["resolved_at"] = now_dt.isoformat()
                e["resolved_reason"] = "expired_unrouted"
                n += 1
        except Exception:
            pass
    return n


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
    nudge routed to an owner). Knowledge = known_answer / efficiency, or a generic
    contributed via #info-for-cora (a human-fed fact, not machine noise)."""
    utype = update.get("update_type")
    if utype in _KNOWLEDGE_TYPES:
        return True
    if utype == "generic" and (update.get("payload") or {}).get("source") == "info-for-cora":
        return True
    return False


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


def _format_owner_dm(update: dict) -> str:
    """Owner-facing card for an operational suggestion. Cora is decision-SUPPORT:
    the owner acts in the native tool (HubSpot/Asana/decisions); Cora does not."""
    utype = update.get("update_type", "generic")
    desc = update.get("description", "(no description)")
    payload = update.get("payload") or {}
    label = {
        "asana_task": "Suggested Asana task",
        "task_close": "Asana task may be done",
        "hubspot_note": "Suggested HubSpot note",
        "decision_capture": "Possible decision to record",
        "generic": "FYI",
    }.get(utype, utype)
    lines = [f":information_source: *{label}* (from Cora):", desc[:600]]
    deal_url = payload.get("deal_url")
    task_url = payload.get("task_url")
    if deal_url:
        lines.append(f"<{deal_url}|Open the deal>")
    if task_url:
        lines.append(f"<{task_url}|Open the task>")
    lines.append("\n_This is a suggestion — handle it directly in the tool if it's right. "
                 "No reply needed._")
    return "\n".join(lines)


def _route_operational_to_owners(
    items: list[dict], slack_token: str, log: logging.Logger, _client_factory=None,
) -> int:
    """Route operational-nudge items to their entity's domain owner. Returns count routed.

    Each routed item is DM'd to the owner (decision-SUPPORT) then marked DISMISSED
    with reason 'routed_to_owner:<id>'. Guardrails:
      * LEX* entities are NEVER routed (PHI) — left PENDING.
      * Only items proposed >= the routing floor are routed (no stale-backlog spam).
      * Per-owner + per-run caps so no owner is flooded; deferred counts are logged.
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
        log.warning("route-to-owner: no routing floor — routing nothing this run")
        return 0

    # HIGH-confidence first, then oldest first (stable).
    eligible = [u for u in items if u.get("proposed_at", "") >= floor]
    eligible.sort(key=lambda u: (0 if u.get("confidence") == "HIGH" else 1,
                                 u.get("proposed_at", "")))

    routed = 0
    deferred_cap = 0
    skipped_lex = 0
    skipped_no_owner = 0
    per_owner: dict[str, int] = {}

    for u in eligible:
        if routed >= _MAX_OWNER_DMS_PER_RUN:
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
        if per_owner.get(owner, 0) >= _MAX_OWNER_DMS_PER_OWNER:
            deferred_cap += 1
            continue
        ts = _send_dm_to_user(owner, _format_owner_dm(u), slack_token, _client_factory)
        if not ts:
            continue  # DM failed — leave PENDING, retry next run
        resolve_update(u["update_id"], "DISMISSED", reason=f"routed_to_owner:{owner}")
        per_owner[owner] = per_owner.get(owner, 0) + 1
        routed += 1

    if routed or deferred_cap or skipped_lex or skipped_no_owner:
        log.info(
            "route-to-owner: routed=%d deferred(cap)=%d skipped(lex)=%d skipped(no-owner)=%d "
            "below-floor=%d",
            routed, deferred_cap, skipped_lex, skipped_no_owner,
            len([u for u in items if u.get("proposed_at", "") < floor]),
        )
    return routed


def _autowrite_eligible(update: dict, level: str) -> tuple[bool, int, str]:
    """(eligible, tier, reason) for the graduated-trust auto-write flip (§7B).

    Uses the graduated-trust classifier for the tier, then an INDEPENDENT
    is_high_stakes belt so a high-stakes / conflicts-with-canon item can NEVER
    auto-write even if the tier were miscomputed. is_high_stakes fails CLOSED (a
    phi_guard exception counts as high-stakes), and the belt itself fails closed.
    Tier-2 is never eligible; Tier-1 only at level 'all'; Tier-0 at 'tier0'/'all'.
    """
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
        return True, 1, "auto_tier1"
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

    for update, reaction in pairs:
        uid = update["update_id"]
        action = reaction["action"]
        log.info(
            "Resolving update_id=%s (%s) -> %s",
            uid[:8], update.get("update_type"), action,
        )
        if not args.dry_run:
            resolve_update(uid, action)

        if action == "APPROVED":
            approved_updates.append(update)
        elif action == "DISMISSED":
            dismissed_updates.append(update)

    if approved_updates:
        log.info("APPROVED %d updates — executing now:", len(approved_updates))
        slack_token = os.environ.get("SLACK_BOT_TOKEN", "")
        for u in approved_updates:
            log.info("  [%s] %s — %s", u["update_type"], u["update_id"][:8], u["description"][:120])
            _execute_approved_update(u, slack_token, log)

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
    knowledge_unsent = [u for u in unsent if _is_knowledge_item(u)]
    operational_unsent = [u for u in unsent if not _is_knowledge_item(u)]
    log.info(
        "Step 2 drain: %d PENDING, %d unsent (%d knowledge, %d operational)",
        len(pending), len(unsent), len(knowledge_unsent), len(operational_unsent),
    )

    slack_token = os.environ.get("SLACK_BOT_TOKEN", "")

    if args.dry_run:
        for i, u in enumerate(knowledge_unsent[:_MAX_KNOWLEDGE_DMS_PER_RUN], 1):
            log.info("[DRY RUN] knowledge %d: %s", i, u.get("description", "?")[:120])
        log.info("[DRY RUN] would route up to %d operational item(s) to owners",
                 len(operational_unsent))
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
                    contributor=str((u.get("payload") or {}).get("contributor_id", "")))
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
