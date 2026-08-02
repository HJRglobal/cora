#!/usr/bin/env python3
"""Delegated-work runner -- claims QUEUED jobs FIFO and executes/delivers them.

Task: ``cowork-cora-delegated-work`` (every 15 min; register via
deployment\\setup-delegated-work-task.ps1). Design of record:
_shared/projects/cora/2026-08-01_fndr_cora-delegated-work-phase1-design.md.

Usage:
    .venv\\Scripts\\python.exe scripts\\run_delegated_work_runner.py
        [--time-budget-min 12] [--dry-run]

Flag semantics (CORA_DELEGATED_WORK, re-read fresh each fire):
    off  -> the runner claims NOTHING (no simulate, no expiry processing).
    log  -> claim + SIMULATED terminal; no model calls, no delivery.
    live -> full execution + guarded delivery.

The runner is the SOLE writer of data/state/delegated-work-runner.jsonl and
the SOLE expirer of QUEUED jobs (48h from the event that entered QUEUED).
Lockfile: data/state/delegated-work.lock -- 30-min stale (12-min budget +
10-min wall + margin), pid-liveness before a stale override, ts refreshed
between jobs, FAIL CLOSED on lock infra errors (a skipped pass self-heals in
15 minutes; a double runner does not).

Every Slack post routes through slack_egress.sanitize_text explicitly (B1
doctrine -- this is a separate process; test_no_raw_slack_post covers it).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

load_dotenv(_REPO_ROOT / ".env", override=True)

from cora import delegated_work as dw  # noqa: E402
from cora import delegated_worker as worker  # noqa: E402
from cora import drive_io  # noqa: E402
from cora.reply_formatter import format_reply  # noqa: E402
from cora.slack_egress import sanitize_text  # noqa: E402

LOG_DIR = _REPO_ROOT / "logs"
LOCK_PATH = _REPO_ROOT / "data" / "state" / "delegated-work.lock"
LOCK_STALE_SECONDS = 30 * 60          # 30 min (design: NOT the copied 2h)
EXPIRE_SECONDS = dw.EXPIRE_HOURS * 3600
CRASH_AGE_SECONDS = 2 * worker.JOB_WALL_SECONDS  # RUNNING older than 2x wall
STAGING_SWEEP_DAYS = 30

log = logging.getLogger("delegated-work-runner")


# ─────────────────────────────────────────────────────────────────────────────
# Lockfile (30-min stale + pid-liveness + FAIL CLOSED)
# ─────────────────────────────────────────────────────────────────────────────
def _pid_alive(pid: int) -> bool:
    """REAL liveness probe. On Windows, ``os.kill(pid, 0)`` is NOT one -- sig 0
    maps to GenerateConsoleCtrlEvent whose result tracks console groups, not
    liveness (wrong in BOTH directions; D-051 state lens, HIGH, verified on
    this host). Use OpenProcess + GetExitCodeProcess; access-denied counts as
    ALIVE (fail closed -- never override a lock we can't inspect)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_ACCESS_DENIED = 5
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True  # can't tell -- treat as alive (fail closed)
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _try_create_lock() -> bool:
    """Atomic O_CREAT|O_EXCL creation -- two near-simultaneous starts cannot
    both pass a check-then-write (D-051 state lens)."""
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"pid": os.getpid(), "ts": time.time()}))
    return True


def acquire_lock() -> bool:
    """True iff this run may proceed. FAIL CLOSED: any lock infra error means
    the pass is SKIPPED (the opposite of the meeting-capture donor's fail-open;
    a doubled runner double-delivers, a skipped pass self-heals in 15 min)."""
    try:
        LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        if _try_create_lock():
            return True
        try:
            data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
            age = time.time() - float(data.get("ts", 0))
            holder_pid = int(data.get("pid", 0) or 0)
        except Exception:  # noqa: BLE001 -- unreadable lock: refuse (fail closed)
            log.error("Lockfile unreadable -- FAIL CLOSED, skipping this pass.")
            return False
        if age < LOCK_STALE_SECONDS:
            log.warning("Another runner holds the lock (pid=%s age=%ds) -- exiting.",
                        holder_pid, int(age))
            return False
        # Stale by age -- but only override if the holder pid is DEAD.
        if holder_pid and _pid_alive(holder_pid):
            log.warning("Lock is stale by age but pid %s is alive -- FAIL "
                        "CLOSED, skipping this pass.", holder_pid)
            return False
        log.warning("Overriding stale lock (age=%ds, pid=%s dead).",
                    int(age), holder_pid)
        try:
            LOCK_PATH.unlink()
        except OSError:
            pass
        # Another racer may claim between the unlink and this create -- if so,
        # it won; skip this pass (fail closed).
        return _try_create_lock()
    except Exception:  # noqa: BLE001 -- FAIL CLOSED on any lock infra error
        log.exception("Lock acquire errored -- FAIL CLOSED, skipping this pass.")
        return False


def refresh_lock() -> None:
    LOCK_PATH.write_text(json.dumps({"pid": os.getpid(), "ts": time.time()}),
                         encoding="utf-8")


def release_lock() -> None:
    """Unlink ONLY a lock this process owns -- an overridden zombie's finally
    must not delete its successor's lock (D-051 state lens)."""
    try:
        if not LOCK_PATH.exists():
            return
        try:
            data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
            if int(data.get("pid", 0) or 0) != os.getpid():
                return
        except Exception:  # noqa: BLE001 -- unreadable: leave it for staleness
            return
        LOCK_PATH.unlink()
    except OSError:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Slack I/O (every body sanitize_text-wrapped)
# ─────────────────────────────────────────────────────────────────────────────
def _client():
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        return None
    from slack_sdk import WebClient
    return WebClient(token=token)


def _sessions_channel() -> str:
    return os.environ.get("CORA_SESSIONS_CHANNEL", "") or "#cora-sessions"


def post_sessions_line(client, text: str) -> None:
    """Machine-lane line to #cora-sessions. Best-effort; FAIL lines carry the
    failure CLASS enum only, never failure.message (the channel is public)."""
    if client is None:
        return
    try:
        client.chat_postMessage(channel=_sessions_channel(),
                                text=sanitize_text(text),
                                unfurl_links=False, unfurl_media=False)
    except Exception:  # noqa: BLE001
        log.warning("#cora-sessions line failed (non-fatal)", exc_info=True)


def post_threaded(client, job: dict, text: str) -> bool:
    """Threaded reply on the original request; channel dead -> DM fallback.
    The text must ALREADY be guarded against the SOURCE channel context --
    the DM fallback posts the same guarded text (missed_message_catchup
    precedent: guarding the DM as a DM would waive the channel classes)."""
    if client is None:
        return False
    body = sanitize_text(format_reply(text))
    channel = str(job.get("channel_id") or "") or ("#" + str(job.get("channel_name") or ""))
    thread_ts = str(job.get("thread_ts") or "") or None
    try:
        client.chat_postMessage(channel=channel, text=body, thread_ts=thread_ts,
                                unfurl_links=False, unfurl_media=False)
        return True
    except Exception:  # noqa: BLE001
        log.warning("channel post failed for %s -- DM fallback",
                    job.get("job_id"), exc_info=True)
    try:
        open_resp = client.conversations_open(users=[str(job.get("requester") or "")])
        dm = open_resp["channel"]["id"]
        client.chat_postMessage(channel=dm, text=body,
                                unfurl_links=False, unfurl_media=False)
        return True
    except Exception:  # noqa: BLE001
        log.warning("DM fallback failed for %s", job.get("job_id"), exc_info=True)
        return False


_FAIL_NOTES = {
    "interrupted": ("your delegated job was interrupted mid-run (likely a crash "
                    "or restart) and did not finish. Nothing was delivered -- "
                    "re-ask to run it again."),
    "content_guard": ("your delegated job finished, but its output tripped the "
                      "channel content guard, so no file was delivered "
                      "(fail-closed)."),
    "api_error": ("your delegated job hit a model/API error and could not "
                  "finish. Re-ask to try again."),
    "no_output": ("your delegated job produced no usable output. Try a more "
                  "specific brief."),
    "error": "your delegated job failed unexpectedly. Re-ask to try again.",
}


def notify_failure(client, job: dict, failure_class: str) -> None:
    note = _FAIL_NOTES.get(failure_class, _FAIL_NOTES["error"])
    post_threaded(client, job,
                  f"Heads up <@{job.get('requester', '')}> -- {note} "
                  f"(`{job.get('job_id', '')}`)")


# ─────────────────────────────────────────────────────────────────────────────
# Maintenance passes
# ─────────────────────────────────────────────────────────────────────────────
def crash_recovery_pass(client) -> None:
    """RUNNING past 2x the wall with NO delivering marker -> FAILED(interrupted)
    + honest threaded notice. RUNNING WITH a delivering marker -> deliver-verify
    (artifact exists? re-post once with an idempotency note) -- a crash in the
    post-to-append gap must never produce a FAILED notice after a visible
    delivery (design section 6)."""
    now = datetime.now(timezone.utc)
    for rec in dw.load_jobs():
        if rec.get("state") != dw.STATE_RUNNING:
            continue
        started = dw._parse_ts(rec.get("started_at"))
        if started is None or (now - started).total_seconds() < CRASH_AGE_SECONDS:
            continue
        jid = rec.get("job_id", "")
        if rec.get("delivering"):
            art = rec.get("artifact") or {}
            target = str(art.get("target_path") or "")
            local = str(art.get("local_path") or "")
            exists = False
            try:
                if target:
                    exists = bool(drive_io.exists(target, timeout=10.0, retry_seconds=0))
            except Exception:  # noqa: BLE001
                exists = False
            if not exists and local:
                exists = Path(local).exists()
            if exists:
                log.info("deliver-verify: %s crashed post-artifact -- re-posting once", jid)
                posted = post_threaded(client, rec,
                                       f"Your delegated job `{jid}` finished earlier but "
                                       "the confirmation may not have posted (recovered "
                                       f"after a crash). The artifact is at: {target or local}")
                if not posted:
                    # Slack still down: leave the delivering marker so a LATER
                    # pass re-verifies -- appending delivered here would
                    # terminally never-notify the requester (D-051 state lens).
                    log.warning("deliver-verify re-post failed for %s -- "
                                "marker left for the next pass", jid)
                    continue
                dw.append_runner_event({"event": "delivered", "ts": dw._now_iso(),
                                        "job_id": jid, "cost": rec.get("cost") or {},
                                        "artifact": art})
                post_sessions_line(client, f"DW DONE {jid} (deliver-verify recovery)")
                continue
        log.warning("crash recovery: %s RUNNING past 2x wall -- FAILED(interrupted)", jid)
        dw.append_runner_event({"event": "failed", "ts": dw._now_iso(), "job_id": jid,
                                "failure_class": "interrupted",
                                "message": "runner crashed or was killed mid-job",
                                "cost": rec.get("cost") or {}})
        notify_failure(client, rec, "interrupted")
        post_sessions_line(client, f"DW FAIL {jid} interrupted")


def expiry_pass(client) -> None:
    """QUEUED jobs unclaimed for 48h expire with a threaded notice. The clock
    runs from the event that ENTERED the job into QUEUED (queued or released),
    so a late release gets its full window. HELD never expires.

    REQUESTED orphans (a crash between the `requested` and `queued`/`held`
    appends) are reaped on the same window -- they are never claimable, yet
    count as open (a permanent envelope reservation + a permanent dedup block
    on the requester's identical brief) until expired (D-051 state lens)."""
    now = datetime.now(timezone.utc)
    for rec in dw.load_jobs():
        state = rec.get("state")
        if state == dw.STATE_QUEUED:
            entered = dw._parse_ts(rec.get("queued_at"))
        elif state == "REQUESTED":
            entered = dw._parse_ts(rec.get("requested_at"))
        else:
            continue
        if entered is None or (now - entered).total_seconds() < EXPIRE_SECONDS:
            continue
        jid = rec.get("job_id", "")
        dw.append_runner_event({"event": "expired", "ts": dw._now_iso(), "job_id": jid})
        post_threaded(client, rec,
                      f"Your delegated job `{jid}` expired unclaimed after "
                      f"{dw.EXPIRE_HOURS}h -- re-ask if you still want it.")
        post_sessions_line(client, f"DW FAIL {jid} expired")


def mis_homed_retry_pass(client) -> None:
    """DELIVERED jobs whose Drive write failed at delivery: retry from staging,
    append artifact_homed on success. NEVER re-posts the Slack delivery."""
    for rec in dw.load_jobs():
        if rec.get("state") != dw.STATE_DELIVERED:
            continue
        art = rec.get("artifact") or {}
        if not art.get("mis_homed"):
            continue
        local = Path(str(art.get("local_path") or ""))
        target = str(art.get("target_path") or "")
        if not target or not local.exists():
            continue
        jid = rec.get("job_id", "")
        try:
            if local.suffix == ".xlsx":
                drive_io.write_bytes_atomic(target, local.read_bytes())
            else:
                drive_io.write_text_atomic(target, local.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 -- G: still down; retry next pass
            log.info("mis-homed retry for %s still failing: %s", jid, exc)
            continue
        dw.append_runner_event({"event": "artifact_homed", "ts": dw._now_iso(),
                                "job_id": jid, "target_path": target})
        _clean_staging(jid)
        log.info("artifact homed for %s -> %s", jid, target)


def overflow_digest_pass(client) -> None:
    """HELD jobs whose card was capped: ONE digest DM to Harrison with per-job
    Release/Dismiss buttons. Bookkeeping rides the RUNNER ledger (card_flushed)
    -- single-writer-per-file holds."""
    held = dw.held_jobs_awaiting_card()
    if not held or client is None:
        return
    try:
        open_resp = client.conversations_open(users=[dw.HARRISON_ID])
        dm = open_resp["channel"]["id"]
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": sanitize_text(
            f"*Delegated work -- {len(held)} more HELD job(s) (over the card cap):*")}}]
        for rec in held[:10]:
            jid = rec.get("job_id", "")
            line = (f"`{jid}` {rec.get('archetype', '?')} [{rec.get('entity', '?')}] "
                    f"from {rec.get('requester_name') or rec.get('requester', '?')} "
                    f"({rec.get('held_reason', '?')})")
            blocks.append({"type": "section",
                           "text": {"type": "mrkdwn", "text": sanitize_text(line)[:2900]}})
            blocks.append({"type": "actions", "block_id": f"dw_ovf_{jid}"[:255],
                           "elements": [
                {"type": "button", "action_id": dw.ACTION_RELEASE, "style": "primary",
                 "text": {"type": "plain_text", "text": "▶️ Release"}, "value": jid},
                {"type": "button", "action_id": dw.ACTION_DISMISS,
                 "text": {"type": "plain_text", "text": "🗑️ Dismiss"}, "value": jid},
            ]})
        client.chat_postMessage(channel=dm,
                                text=sanitize_text(f"Delegated work: {len(held)} held job(s) awaiting review"),
                                blocks=blocks, unfurl_links=False, unfurl_media=False)
        for rec in held[:10]:
            dw.append_runner_event({"event": "card_flushed", "ts": dw._now_iso(),
                                    "job_id": rec.get("job_id", "")})
    except Exception:  # noqa: BLE001
        log.warning("overflow digest failed (non-fatal)", exc_info=True)


def staging_sweep() -> None:
    """Purge terminal-job staging dirs older than 30 days (the host has a
    documented disk-pressure history; nothing here may grow unbounded)."""
    root = dw._STAGING_ROOT
    if not root.exists():
        return
    cutoff = time.time() - STAGING_SWEEP_DAYS * 86400
    jobs = dw._fold_jobs()
    for d in root.iterdir():
        if not d.is_dir():
            continue
        rec = jobs.get(d.name)
        state = rec.get("state") if rec else None
        # A mis-homed DELIVERED job's staging dir is the ONLY copy of its
        # artifact until it homes -- never purge it, however old (a >30d G:
        # outage must not silently destroy a promised file; D-051 state lens).
        if (rec and state == dw.STATE_DELIVERED
                and (rec.get("artifact") or {}).get("mis_homed")):
            continue
        terminal_or_unknown = rec is None or state in dw.TERMINAL_STATES
        try:
            if terminal_or_unknown and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                log.info("staging sweep: purged %s (state=%s)", d.name, state)
        except OSError:
            continue


def _clean_staging(job_id: str) -> None:
    d = dw.staging_dir(job_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


# ─────────────────────────────────────────────────────────────────────────────
# Claim + execute + deliver
# ─────────────────────────────────────────────────────────────────────────────
def next_claimable_job() -> dict | None:
    """Oldest-queued first (FIFO), skipping envelope-blocked jobs. A Harrison-
    released job carries his explicit override and is claimable even when the
    envelope is exhausted -- and must not be STARVED behind a non-released
    FIFO head (D-051 cost lens: break-on-first-blocked left the override
    partially dead)."""
    queued = [r for r in dw.load_jobs() if r.get("state") == dw.STATE_QUEUED]
    queued.sort(key=lambda r: str(r.get("queued_at") or ""))
    if not queued:
        return None
    envelope_ok = dw.envelope_headroom() >= 0
    for cand in queued:
        if envelope_ok or cand.get("released_at"):
            return cand
        log.warning("envelope exhausted -- leaving %s queued this pass",
                    cand.get("job_id"))
    return None


def _duration_label(rec: dict) -> str:
    started = dw._parse_ts(rec.get("started_at"))
    if started is None:
        return "?"
    secs = int((datetime.now(timezone.utc) - started).total_seconds())
    return f"{secs // 60}m{secs % 60:02d}s"


def deliver(client, job_id: str, outcome: dict) -> None:
    """Guard -> stage -> Drive -> `delivering` -> Slack -> `delivered`."""
    rec = dw.get_job(job_id)  # re-fold immediately before delivery
    if rec is None:
        return
    if rec.get("state") == dw.STATE_CANCELLED:
        log.info("delivery suppressed for %s (cancelled mid-run)", job_id)
        return
    cost = outcome.get("cost") or {}

    if not outcome.get("ok"):
        fclass = str(outcome.get("failure_class") or "error")
        dw.append_runner_event({"event": "failed", "ts": dw._now_iso(), "job_id": job_id,
                                "failure_class": fclass,
                                "message": str(outcome.get("message") or "")[:400],
                                "cost": cost})
        notify_failure(client, rec, fclass)
        post_sessions_line(client, f"DW FAIL {job_id} {fclass}")
        return

    # The artifact body + thread summary are ONE guarded egress surface,
    # evaluated against the REQUESTING channel's context (design section 7).
    guard_surface = "\n\n".join(filter(None, [
        str(outcome.get("summary") or ""),
        str(outcome.get("xlsx_cell_text") or "") if outcome.get("artifact_bytes")
        else str(outcome.get("artifact_text") or ""),
    ]))
    fclass, guard_text = worker.guard_artifact_text(rec, guard_surface)
    if fclass:
        dw.append_runner_event({"event": "failed", "ts": dw._now_iso(), "job_id": job_id,
                                "failure_class": "content_guard",
                                "message": "artifact tripped the channel content guard",
                                "cost": cost})
        post_threaded(client, rec,
                      f"{guard_text}\n(Delegated job `{job_id}` failed: "
                      "content guard -- no file was delivered.)")
        post_sessions_line(client, f"DW FAIL {job_id} content_guard")
        return

    # Stage locally (retained until homed, then cleaned).
    ext = str(outcome.get("artifact_ext") or "md")
    staging = dw.staging_dir(job_id)
    staging.mkdir(parents=True, exist_ok=True)
    local = staging / f"artifact.{ext}"
    if outcome.get("artifact_bytes"):
        local.write_bytes(outcome["artifact_bytes"])
    else:
        local.write_text(str(outcome.get("artifact_text") or ""), encoding="utf-8")

    target = worker.artifact_target_path(rec, ext_override=ext)
    mis_homed = False
    try:
        if outcome.get("artifact_bytes"):
            drive_io.write_bytes_atomic(target, outcome["artifact_bytes"])
        else:
            drive_io.write_text_atomic(target, str(outcome.get("artifact_text") or ""))
    except Exception as exc:  # noqa: BLE001 -- G: down: deliver the summary anyway
        log.warning("Drive write failed for %s (mis_homed): %s", job_id, exc)
        mis_homed = True

    artifact_meta = {"local_path": str(local), "target_path": str(target),
                     "mis_homed": mis_homed}
    # Crash-safe delivery: `delivering` (with artifact + cost) BEFORE the post.
    dw.append_runner_event({"event": "delivering", "ts": dw._now_iso(),
                            "job_id": job_id, "artifact": artifact_meta,
                            "cost": cost})

    lines = [f"<@{rec.get('requester', '')}> your delegated job `{job_id}` is done."]
    summary = str(outcome.get("summary") or "").strip()
    if summary:
        lines.append(summary)
    if outcome.get("partial"):
        lines.append(f"(Partial result -- the job hit its "
                     f"{outcome.get('partial_reason') or 'budget'} limit.)")
    if outcome.get("web_withheld_reason"):
        lines.append("(Web research was withheld for this job; internal "
                     "sources only.)")
    if mis_homed:
        lines.append("The file is staged locally and will land on Drive "
                     "automatically once it's reachable.")
    else:
        lines.append(f"File: {target}")
    posted = post_threaded(client, rec, "\n\n".join(lines))
    if not posted:
        # Leave the delivering marker -- the deliver-verify recovery re-posts
        # once on a later pass instead of double-posting now.
        log.warning("delivery post failed for %s -- delivering marker left "
                    "for deliver-verify", job_id)
        return
    dw.append_runner_event({"event": "delivered", "ts": dw._now_iso(),
                            "job_id": job_id, "cost": cost,
                            "artifact": artifact_meta})
    est = float(cost.get("est_usd") or 0.0)
    post_sessions_line(client,
                       f"DW DONE {job_id} ${est:.2f} {_duration_label(rec)}")
    if not mis_homed:
        _clean_staging(job_id)


def run_pass(time_budget_min: float, dry_run: bool) -> int:
    level = dw.delegated_level()
    if level == "off":
        log.info("CORA_DELEGATED_WORK=off -- runner claims nothing.")
        return 0

    client = None if dry_run else _client()
    deadline = time.monotonic() + time_budget_min * 60

    if dry_run:
        queued = [r for r in dw.load_jobs() if r.get("state") == dw.STATE_QUEUED]
        log.info("[dry-run] level=%s queued=%d held=%d", level, len(queued),
                 len([r for r in dw.load_jobs() if r.get("state") == dw.STATE_HELD]))
        for r in queued:
            log.info("[dry-run] would claim %s (%s, %s)", r.get("job_id"),
                     r.get("archetype"), r.get("entity"))
        return 0

    crash_recovery_pass(client)
    expiry_pass(client)
    mis_homed_retry_pass(client)
    overflow_digest_pass(client)
    staging_sweep()

    jobs_run = 0
    while time.monotonic() < deadline:
        # Envelope re-check at claim happens inside next_claimable_job
        # (design section 10; released jobs = Harrison's explicit override).
        job = next_claimable_job()
        if job is None:
            break
        jid = str(job.get("job_id") or "")
        refresh_lock()
        dw.append_runner_event({"event": "started", "ts": dw._now_iso(), "job_id": jid})
        post_sessions_line(client, f"DW START {jid} {job.get('archetype', '?')} "
                                   f"{job.get('entity', '?')} "
                                   f"{job.get('requester_name') or job.get('requester', '?')}")
        if level == "log":
            dw.append_runner_event({"event": "simulated", "ts": dw._now_iso(),
                                    "job_id": jid})
            log.info("log mode: %s SIMULATED (no model calls, no delivery)", jid)
            jobs_run += 1
            continue
        try:
            outcome = worker.run_job(job)
        except Exception as exc:  # noqa: BLE001 -- belt: run_job should not raise
            log.exception("run_job crashed for %s", jid)
            outcome = {"ok": False, "failure_class": "error",
                       "message": f"{type(exc).__name__}: {exc}", "cost": {}}
        deliver(client, jid, outcome)
        jobs_run += 1
    return jobs_run


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_DIR / f"delegated-work-{today}.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--time-budget-min", type=float, default=12.0,
                        help="self-bounding wall budget for this pass (default 12)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be claimed; no writes, no posts")
    args = parser.parse_args()

    _setup_logging()
    log.info("=" * 60)
    log.info("Delegated-work runner starting (level=%s dry_run=%s budget=%.0fmin)",
             dw.delegated_level(), args.dry_run, args.time_budget_min)

    if args.dry_run:
        run_pass(args.time_budget_min, dry_run=True)
        return 0

    if dw.delegated_level() == "off":
        log.info("CORA_DELEGATED_WORK=off -- exiting without claiming the lock.")
        return 0

    if not acquire_lock():
        return 0
    try:
        n = run_pass(args.time_budget_min, dry_run=False)
        log.info("Runner pass complete: %d job(s) processed.", n)
    finally:
        release_lock()
    return 0


if __name__ == "__main__":
    sys.exit(main())
