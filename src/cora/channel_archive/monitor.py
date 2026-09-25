"""The dead-channel lane's evidence monitor (Code #16 C1, amendment A25): the ledger
reconciled against Slack's OWN record. It must be able to fail on the regression it
guards (kickoff section 2), so it is built in-repo and runs in the 08:45 health check
(``nightly_health_check.check_channel_archive``), never in a prompt task.

FAIL CLASSES
  (1) UNATTRIBUTED -- an archive system message by CORA (user == her bot user, or
      bot_id == her bot id) dated at/after max(now - 35 d, LANE_EPOCH), NOT paired
      one-to-one with a ledger intent for that channel whose ts is in
      [archive - 15 min, archive + 2 min], tapped by Harrison, with no failed outcome
      before the archive, and not ``acknowledged`` by Harrison's --clear-demotion.
      -> the demotion file is written (the lane acts at T0 whatever its flag says)
      + WARN. Never from a blind read; never under --dry-run. The file lists EVERY
      such (channel_id, archive_ts) -- appended even while already demoted -- and
      --clear-demotion acknowledges each one. There is no per-channel legacy
      exemption: LANE_EPOCH already excludes every pre-lane archive, and the sprawl
      script archives on Harrison's user token (never Cora's), so an id-level clause
      only hid a later untapped archive of those channels (D-051 r1 c1-monitor#4).
      The finding says what happened to the demotion: WRITTEN / would demote (dry
      run) / DEMOTION WRITE FAILED with the tier the lane is really acting at.
  (2) MISMATCH -- a ledger ``archived`` channel that Slack lists as NOT archived.
      The unarchive is searched FORWARD from the archive message's ts in bounded time
      windows (``_first_after``; newest-first paging missed an unarchive buried under
      1,000+ later messages): a person's unarchive -> an ``unarchived_seen`` row (the
      channel leaves the examined set); a BOT unarchive -> WARN (Cora has no unarchive
      path); none found -> WARN.
      A ledger ``failed`` outcome on an attempt whose archive Slack shows Cora made
      (an indeterminate error that landed) is reconciled ``archived`` once + WARN.
  (3) UNRESOLVED -- an intent older than 1 h with no outcome, or an ``unknown``
      outcome. Settled from Slack's OWN history, the first archive message in
      [intent - 2 min, intent + 1 h] (the same host-clock tolerance as (1); bounded, so a
      busy channel is never paged through nightly): Cora's (at most 15 min after the
      intent) -> ``archived (reconciled)`` carrying that message's ts, and when the
      channel is open again its unarchive is followed as in (2); a person's archive, or
      archived with no archive message in the window -> ``already_archived
      (reconciled)``; none and open -> ``failed (reconciled)``; gone from the list ->
      ``failed (reconciled: channel gone)``; an unreadable or malformed history records
      NOTHING (WARN). Each outcome goes to the ledger AND the store (the next card render
      shows it), and a dry run says "would be recorded".
  (4) BLIND -- the channel list paginated incompletely, zero archived channels in
      it, no bot identity, or an unreadable ledger/store -> WARN, never OK.
  (5) DEMOTED -- the demotion file exists -> WARN daily until Harrison clears it.
Plus: an archived channel (in window) whose newest 20 messages hold no archive-type
message -> "cannot attribute" WARN; a scan_started with no staged card after 1 h ->
WARN, settled by a ``scan_failed`` the crash path recorded (the bot's scan pool and the
monthly script; the crash was already said where the scan was asked) and dropped after
7 days when nothing was recorded (a killed process) -- an alarm the right action cannot
clear gets ignored (lesson 52); the LATEST staged proposal was blind -> "LATEST SCAN
BLIND" WARN until a sighted scan is staged (with the re-baseline command when the
registry merely shrank). The OK line carries coverage counts (lesson 63: a zero count
certifies nothing without positive coverage).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import classify as cl
from . import policy
from . import registry as reg
from . import store as st

log = logging.getLogger(__name__)

_AZ = timezone(timedelta(hours=-7))
#: Nothing this lane did can predate its own merge (Code #16, 2026-09-25).
LANE_EPOCH = datetime(2026, 9, 25, 0, 0, tzinfo=_AZ).timestamp()
WINDOW_DAYS = 35
INTENT_BEFORE_S = 15 * 60
INTENT_AFTER_S = 120
UNRESOLVED_S = 3600
#: (3) reads Slack's history over [intent - INTENT_AFTER_S, intent + this] only: the
#: attempt's own archive lands within seconds of its intent, so a busy channel's later
#: traffic is never paged through night after night (D-051 r2 c1-monitor#1).
UNRESOLVED_WINDOW_S = 3600
SCAN_STALL_S = 3600
#: A stall no crash path recorded (a killed process) stops WARNing after this long.
SCAN_STALL_MAX_S = 7 * 86400
HISTORY_LIMIT = 20
#: The forward history search (unarchive / archive after a ts): first window, read cap.
FORWARD_FIRST_WINDOW_S = 3600.0
FORWARD_SEARCH_CALLS = 32
LIST_MAX_PAGES = 20
PACE_S = 1.0
ARCHIVE_SUBTYPES = ("channel_archive", "group_archive")
UNARCHIVE_SUBTYPES = ("channel_unarchive", "group_unarchive")


def _harrison() -> str:
    from .deliver import HARRISON_ID  # noqa: PLC0415
    return HARRISON_ID


def _list_all(client: Any) -> tuple[list[dict] | None, str]:
    out: list[dict] = []
    cursor = ""
    for _ in range(LIST_MAX_PAGES):
        kwargs: dict[str, Any] = {"types": "public_channel,private_channel",
                                  "exclude_archived": False, "limit": 1000}
        if cursor:
            kwargs["cursor"] = cursor
        try:
            resp = client.conversations_list(**kwargs)
        except Exception as exc:  # noqa: BLE001
            return None, cl._err_code(exc)
        chans = resp.get("channels")
        if not isinstance(chans, list):
            return None, "shape"
        out.extend(c for c in chans if isinstance(c, dict))
        md = resp.get("response_metadata")
        nxt = md.get("next_cursor") if isinstance(md, dict) else None
        if not isinstance(nxt, str):
            return None, "no_cursor_field"
        if not nxt:
            return out, ""
        cursor = nxt
    return None, "page_cap"


def _latest_archive_event(client: Any, cid: str) -> tuple[dict | None, str]:
    """(newest archive-type message projected to {ts, user, bot_id, subtype}, error)."""
    try:
        resp = client.conversations_history(channel=cid, limit=HISTORY_LIMIT)
    except Exception as exc:  # noqa: BLE001
        return None, cl._err_code(exc)
    for m in resp.get("messages") or []:
        if isinstance(m, dict) and m.get("subtype") in ARCHIVE_SUBTYPES:
            return {k: m.get(k) for k in ("ts", "user", "bot_id", "subtype")}, ""
    return None, ""


def _first_after(client: Any, cid: str, since: float, now: float,
                 subtypes: tuple[str, ...]) -> tuple[dict | None, str]:
    """The EARLIEST message of one of *subtypes* at/after *since*, walking FORWARD in
    time (D-051 r1 c1-false-inactive#1). Each read is one window [lo, hi] with BOTH
    oldest and latest (inclusive), so Slack's newest-first order inside a window does not
    matter: an empty or target-free window moves on and doubles; a window holding more
    than one page halves (the earliest target is what matters). The old newest-first
    paging from *since* with a 5-page cap never reached an early unarchive behind 1,000+
    later messages. -> (projected message, ""), (None, "") = none up to *now*, or
    (None, code) on a read error / (None, "page_cap") past FORWARD_SEARCH_CALLS reads.
    A page with no boolean ``has_more`` or no ``messages`` list is (None, "shape"): A8,
    a malformed page is UNKNOWN, never "nothing more" (D-051 r2 c1-false-inactive#0)."""
    lo = float(since)
    width = FORWARD_FIRST_WINDOW_S
    for _ in range(FORWARD_SEARCH_CALLS):
        if lo > now:
            return None, ""
        hi = min(lo + width, now)
        try:
            resp = client.conversations_history(channel=cid, oldest=f"{lo:.6f}", latest=f"{hi:.6f}",
                                                inclusive=True, limit=200)
        except Exception as exc:  # noqa: BLE001
            return None, cl._err_code(exc)
        more, page = resp.get("has_more"), resp.get("messages")
        if not isinstance(more, bool) or not isinstance(page, list):
            return None, "shape"              # A8: never read a malformed page as "nothing more"
        if more and hi - lo > 1.0:
            width = (hi - lo) / 2.0           # narrow toward lo: the earliest target is what counts
            continue
        hits = [m for m in page if isinstance(m, dict) and m.get("subtype") in subtypes]
        if hits:
            m = min(hits, key=lambda x: float(x.get("ts") or 0))
            return {k: m.get(k) for k in ("ts", "user", "bot_id", "subtype")}, ""
        if more:
            return None, "page_cap"           # > one page inside one second: cannot narrow
        if hi >= now:
            return None, ""
        lo, width = hi, width * 2.0
    return None, "page_cap"


def _is_cora(m: dict, bot_uid: str, bot_id: str) -> bool:
    return (bool(bot_uid) and m.get("user") == bot_uid) or (bool(bot_id) and m.get("bot_id") == bot_id)


def _record_demotion(dem: dict | None, found: list[tuple[str, str]], *, now: float,
                     dry_run: bool) -> str:
    """Put EVERY unattributed (channel_id, archive_ts) on the demotion file (D-051 r1
    c1-monitor#2): a new demotion lists them all; an existing one gets the missing ones
    appended (atomic replace, its ``since`` kept). Returns what happened: written /
    updated / listed / unreadable / dry_run / dry_run_update / failed (a NEW demotion
    did not land) / update_failed (the lane is already demoted; the event is not listed)."""
    new = [{"channel_id": c, "archive_ts": t} for c, t in found]
    if dem is None:
        if dry_run:
            return "dry_run"
        rec = {"since": datetime.fromtimestamp(now, _AZ).isoformat(timespec="seconds"),
               "channel_id": new[0]["channel_id"], "archive_ts": new[0]["archive_ts"],
               "reason": "an archive by Cora with no tap-attributed ledger intent", "events": new}
        return "written" if st.write_demotion(rec, dry_run=False) else "failed"
    if dem.get("unreadable"):
        return "unreadable"              # never overwrite a demotion we cannot read
    listed = st.demotion_events(dem)
    have = {(e["channel_id"], e["archive_ts"]) for e in listed}
    missing = [e for e in new if (e["channel_id"], e["archive_ts"]) not in have]
    if not missing:
        return "listed"
    if dry_run:
        return "dry_run_update"
    rec = {k: v for k, v in dem.items() if k != "demoted"}
    rec["events"] = listed + missing
    return "updated" if st.write_demotion(rec, dry_run=False) else "update_failed"


def reconcile(client: Any, *, now: float | None = None, dry_run: bool = False,
              sleep: Callable[[float], None] | None = None) -> dict:
    """Run every class. Returns {status: ok|warn, findings[], coverage{}, demotion_written,
    demotion (what happened to the demotion file: see ``_record_demotion``), blind}."""
    now = time.time() if now is None else float(now)
    sleep = time.sleep if sleep is None else sleep
    findings: list[str] = []
    cov = {"list_rows": 0, "archived_in_list": 0, "examined": 0, "histories": 0,
           "ledger_rows": 0}
    out: dict[str, Any] = {"status": "ok", "findings": findings, "coverage": cov,
                           "demotion_written": False, "demotion": "", "blind": ""}

    def blind(why: str) -> dict:
        out["blind"] = why
        findings.append(f"BLIND: {why} -- nothing could be verified")
        out["status"] = "warn"
        return out

    dem = policy.demotion_state()
    if dem is not None:
        listed = st.demotion_events(dem)
        ids = ", ".join(e["channel_id"] for e in listed[:4]) + (f" +{len(listed) - 4}" if len(listed) > 4 else "")
        findings.append(f"DEMOTED since {dem.get('since') or '?'} ({len(listed)} unattributed archive "
                        f"event(s): {ids or '?'}; {dem.get('reason') or 'unattributed archive'}) -- the "
                        "lane acts at T0 until Harrison clears it (run_channel_archive_proposal.py "
                        "--clear-demotion --apply)")
    ledger = st.read_ledger()
    if ledger is None:
        return blind("archive ledger unreadable")
    cov["ledger_rows"] = len(ledger)
    events = st.read_events()
    if events is None:
        return blind("proposals store unreadable")
    from .deliver import bot_identity  # noqa: PLC0415
    bot_uid, bot_id = bot_identity(client)
    if not bot_uid:
        return blind("Cora's bot identity unknown (auth.test)")
    chans, err = _list_all(client)
    if chans is None:
        return blind(f"channel list incomplete ({err})")
    cov["list_rows"] = len(chans)
    by_id = {str(c.get("id")): c for c in chans if c.get("id")}
    archived_ids = {cid for cid, c in by_id.items() if c.get("is_archived")}
    cov["archived_in_list"] = len(archived_ids)
    if not archived_ids:
        return blind("zero archived channels in Slack's list (155 existed at launch) -- "
                     "exclude_archived or pagination is wrong")

    # -- ledger indexes --
    intents: dict[str, list[dict]] = {}
    outcomes: dict[tuple, list[dict]] = {}
    acked: set[tuple] = set()
    unarch_seen: dict[str, float] = {}
    for r in ledger:
        ev = r.get("event")
        cid = str(r.get("channel_id") or "")
        if ev == "intent":
            intents.setdefault(cid, []).append(r)
        elif ev == "outcome":
            outcomes.setdefault((str(r.get("proposal_id") or ""), cid), []).append(r)
        elif ev == "acknowledged":
            acked.add((cid, str(r.get("archive_ts") or "")))
        elif ev == "unarchived_seen":
            unarch_seen[cid] = max(unarch_seen.get(cid, 0.0),
                                   float(r.get("unarchive_ts") or r.get("ts") or 0))
    ledger_archived: dict[str, float] = {}
    for (pid, cid), rows in outcomes.items():
        for r in rows:
            if str(r.get("outcome") or "").startswith("archived"):
                # keyed on the archive MESSAGE's ts when the row carries it: a reconciled
                # outcome is stamped at the (later) nightly run, after any unarchive
                at = float(r.get("archive_ts") or r.get("ts") or 0)
                ledger_archived[cid] = max(ledger_archived.get(cid, 0.0), at)
    floor_ts = max(now - WINDOW_DAYS * st.DAY_S, LANE_EPOCH)
    harrison = _harrison()

    def settle(pid: str, cid: str, r: dict, outcome: str, store_outcome: str,
               archive_ts: float | None = None) -> tuple[bool | None, str]:
        """A reconciled outcome to the ledger AND the store (A25(3)). -> (True recorded /
        None dry run / False ledger write failed, the honest copy tail -- c1-monitor#5)."""
        if dry_run:
            return None, f"would be recorded as {outcome} (dry run: nothing written)"
        if not st.append_ledger("outcome", proposal_id=pid, channel_id=cid, outcome=outcome,
                                tapped_by=r.get("tapped_by"), archive_ts=archive_ts, ts=now):
            return False, f"NOT recorded as {outcome} (the ledger write FAILED; the next nightly retries)"
        st.append_event("reconciled", proposal_id=pid, cid=cid, outcome=store_outcome, ts=now)
        return True, f"recorded as {outcome}"

    def follow_unarchive(cid: str, since: float, what: str) -> None:
        """A channel archived at *since* (per the ledger) that Slack lists OPEN: search
        FORWARD for who reopened it. A person -> ``unarchived_seen``; Cora -> WARN (she
        has no unarchive path); none / unreadable -> WARN."""
        sleep(PACE_S)
        un, err = _first_after(client, cid, since, now, UNARCHIVE_SUBTYPES)
        cov["histories"] += 1
        if err:
            findings.append(f"MISMATCH: {cid} {what}, Slack says open; history unreadable ({err})")
        elif un is None:
            findings.append(f"MISMATCH: {cid} {what}, Slack says open, and no unarchive message "
                            "was found")
        elif _is_cora(un, bot_uid, bot_id):
            findings.append(f"MISMATCH: {cid} was UNARCHIVED BY CORA -- she has no unarchive path")
        elif not dry_run:
            st.append_ledger("unarchived_seen", channel_id=cid, unarchive_ts=float(un.get("ts") or now),
                             by=str(un.get("user") or ""), ts=now)

    # -- (1) UNATTRIBUTED + cannot-attribute over the recent archived set --
    examine: set[str] = set()
    for cid in archived_ids:
        c = by_id[cid]
        if not c.get("is_member"):
            continue
        upd = c.get("updated")
        try:
            upd_s = float(upd) / 1000.0 if upd is not None else None
        except (TypeError, ValueError):
            upd_s = None
        if upd_s is None or upd_s >= floor_ts:
            examine.add(cid)
    examine |= {cid for cid in intents if cid in archived_ids}
    used_intents: set[int] = set()
    unattributed: list[tuple[str, str, int]] = []     # (cid, archive ts, findings index)
    newest_archive: dict[str, dict] = {}              # cid -> its CURRENT archive message (copy for (3))
    for cid in sorted(examine):
        cov["examined"] += 1
        sleep(PACE_S)
        ev, err = _latest_archive_event(client, cid)
        cov["histories"] += 1
        if err:
            findings.append(f"{cid}: history unreadable ({err}) -- cannot attribute its archive")
            continue
        if ev is not None:
            newest_archive[cid] = ev
        if ev is None:
            findings.append(f"{cid}: archived, but no archive message in its newest "
                            f"{HISTORY_LIMIT} -- cannot attribute")
            continue
        try:
            ats = float(ev.get("ts") or 0)
        except (TypeError, ValueError):
            ats = 0.0
        if not ev.get("user") and not ev.get("bot_id"):
            findings.append(f"{cid}: archive message carries no archiver -- archiver unknown")
            continue
        if not _is_cora(ev, bot_uid, bot_id):
            continue                          # a person archived it: not this lane's act
        if ats < floor_ts:
            continue                          # predates the window / the lane (incl. the sprawl runs)
        if (cid, str(ev.get("ts") or "")) in acked:
            continue                          # Harrison acknowledged this exact event
        match = None
        for i, r in enumerate(intents.get(cid, [])):
            its = float(r.get("ts") or 0)
            if id(r) in used_intents or not (ats - INTENT_BEFORE_S <= its <= ats + INTENT_AFTER_S):
                continue
            if str(r.get("tapped_by") or "") != harrison:
                continue
            failed_before = any(str(o.get("outcome") or "").startswith(("failed", "notice_failed",
                                                                         "not_attempted"))
                                and its <= float(o.get("ts") or 0) <= ats
                                for o in outcomes.get((str(r.get("proposal_id") or ""), cid), []))
            if failed_before:
                continue
            match = r
            break
        if match is not None:
            used_intents.add(id(match))
            # a tapped attempt the ledger closed as failed while Slack shows Cora archived
            # it (internal_error / fatal_error / a 5xx can partly succeed): Slack's record
            # wins, once (D-051 r1 c1-monitor#3)
            mpid, mits = str(match.get("proposal_id") or ""), float(match.get("ts") or 0)
            mouts = [o for o in outcomes.get((mpid, cid), []) if float(o.get("ts") or 0) >= mits - 1]
            mlast = str(mouts[-1].get("outcome") or "") if mouts else ""
            if mlast.startswith(("failed", "notice_failed", "not_attempted")):
                _ok, tail = settle(mpid, cid, match, "archived (reconciled)", "archived", archive_ts=ats)
                findings.append(f"RECONCILED: {cid} intent {mpid} -- the ledger said {mlast}, but Slack "
                                f"shows Cora archived it at {ev.get('ts')}; {tail}")
            continue
        findings.append("")                   # filled once the demotion's fate is known
        unattributed.append((cid, str(ev.get("ts") or ""), len(findings) - 1))
    if unattributed:
        how = _record_demotion(dem, [(c, t) for c, t, _i in unattributed], now=now, dry_run=dry_run)
        out["demotion"] = how
        out["demotion_written"] = how in ("written", "updated")
        tier = policy.acting_tier()
        tails = {
            "written": "-- demotion WRITTEN: the lane acts at T0 until Harrison clears it",
            "updated": "-- added to the existing demotion (the lane stays at T0)",
            "listed": "-- already listed on the demotion (the lane stays at T0)",
            "unreadable": "-- the demotion file is unreadable, so the lane stays at T0; this event "
                          "could not be added to it",
            "dry_run": "-- would demote the lane to T0 (dry run: nothing written)",
            "dry_run_update": "-- would be added to the existing demotion (dry run: nothing written)",
            "failed": f"-- DEMOTION WRITE FAILED: the lane is still acting {tier}",
            "update_failed": "-- could NOT be added to the existing demotion (write FAILED); the "
                             "lane stays at T0 and the next nightly retries",
        }
        for cid, ats_s, i in unattributed:
            findings[i] = (f"UNATTRIBUTED: {cid} was archived by Cora at {ats_s} with no "
                           f"tap-attributed intent in the ledger {tails[how]}")
        if how == "failed":
            findings.insert(0, f"DEMOTION WRITE FAILED ({policy.demotion_path().name}) -- the lane is "
                               f"still acting {tier} despite {len(unattributed)} unattributed archive(s); "
                               "the next nightly retries")

    # -- (2) MISMATCH --
    for cid, arch_ts in sorted(ledger_archived.items()):
        if unarch_seen.get(cid, 0.0) >= arch_ts:
            continue
        c = by_id.get(cid)
        if c is None:
            findings.append(f"MISMATCH: {cid} has a ledger archive but is not in Slack's list")
            continue
        if c.get("is_archived"):
            continue
        follow_unarchive(cid, arch_ts, "ledger says archived")

    # -- (3) UNRESOLVED: settled from Slack's OWN history (the first archive message
    # around the intent), never from "open now = never archived"; every readable window
    # ends in a settled outcome, so no WARN repeats forever with nothing that can clear
    # it (D-051 r1 c1-monitor#1). The window uses (1)'s pairing tolerance: the intent is
    # stamped on the HOST clock and Slack stamps the archive message, so a host running
    # ahead puts Cora's own archive BEFORE the intent -- the search starts at
    # its - INTENT_AFTER_S and a Cora archive in [its - INTENT_AFTER_S, its +
    # INTENT_BEFORE_S] is this attempt's (D-051 r2 c1-false-inactive#0 / c1-monitor#0).
    # It ends at its + UNRESOLVED_WINDOW_S (r2 c1-monitor#1) --
    for cid, rows in sorted(intents.items()):
        for r in rows:
            pid = str(r.get("proposal_id") or "")
            its = float(r.get("ts") or 0)
            outs = [o for o in outcomes.get((pid, cid), []) if float(o.get("ts") or 0) >= its - 1]
            latest = str(outs[-1].get("outcome") or "") if outs else ""
            if outs and not latest.startswith("unknown"):
                continue
            if not outs and now - its < UNRESOLVED_S:
                continue
            aged = now - its >= UNRESOLVED_S
            c = by_id.get(cid)
            if c is None:
                if not aged:
                    findings.append(f"UNRESOLVED: {cid} intent {pid} has no settled outcome")
                    continue
                _ok, tail = settle(pid, cid, r, "failed (reconciled: channel gone)", "failed")
                findings.append(f"RECONCILED: {cid} intent {pid} -- the channel is no longer in Slack's "
                                f"list (deleted, or Cora lost access), so nothing more can be checked; {tail}")
                continue
            sleep(PACE_S)
            first, err = _first_after(client, cid, its - INTENT_AFTER_S,
                                      min(now, its + UNRESOLVED_WINDOW_S), ARCHIVE_SUBTYPES)
            cov["histories"] += 1
            if err:
                findings.append(f"UNRESOLVED: {cid} intent {pid} has no settled outcome; its history "
                                f"could not be searched ({err})")
                continue
            by_cora = first is not None and _is_cora(first, bot_uid, bot_id)
            fts = float(first.get("ts") or 0) if first is not None else 0.0
            if by_cora and fts <= its + INTENT_BEFORE_S:
                ok, tail = settle(pid, cid, r, "archived (reconciled)", "archived", archive_ts=fts)
                if ok is False:
                    findings.append(f"UNRESOLVED: {cid} intent {pid} -- Slack shows Cora archived it at "
                                    f"{first.get('ts')}, but it was {tail}")
                if not c.get("is_archived") and ok is not False:   # reopened since: by whom?
                    follow_unarchive(cid, fts, f"Cora archived it at {first.get('ts')} (intent {pid})")
                continue
            if not aged:
                findings.append(f"UNRESOLVED: {cid} intent {pid} has no settled outcome")
                continue
            if c.get("is_archived") and not by_cora:
                # no Cora archive in the pairing window: the attempt's own archive lands
                # within seconds, so it did not archive the channel. An empty window
                # cannot say WHEN someone else did; (1)'s read of the channel's CURRENT
                # archive message names a later archiver (r2 c1-false-inactive#0)
                late = newest_archive.get(cid) if first is None else None
                try:
                    late_ts = float((late or {}).get("ts") or 0)
                except (TypeError, ValueError):
                    late_ts = 0.0
                if first is not None:
                    how = f"a person (not Cora) archived it at {first.get('ts')}"
                elif late is not None and late_ts > its + INTENT_BEFORE_S:
                    who = ("Cora (a later archive, not this attempt's)" if _is_cora(late, bot_uid, bot_id)
                           else "a person (not Cora)")
                    how = f"{who} archived it at {late.get('ts')}, long after the attempt"
                else:
                    how = ("it archived with no archive message by Cora within 15 min of the attempt "
                           "(none in the hour around it), so it was archived before the attempt or "
                           "long after it, not by it")
                _ok, tail = settle(pid, cid, r, "already_archived (reconciled)", "already_archived")
                findings.append(f"RECONCILED: {cid} intent {pid} -- Slack shows {how}; {tail}")
                continue
            why = ("Slack shows it open and no archive message within an hour of the intent" if first is None
                   else f"the first archive after it is a later one by Cora at {first.get('ts')}"
                   if by_cora else f"the first archive after it is a person's at {first.get('ts')}")
            _ok, tail = settle(pid, cid, r, "failed (reconciled)", "failed")
            findings.append(f"RECONCILED: {cid} intent {pid} never archived by this attempt ({why}) -- "
                            f"{tail}; the channel may still carry the notice")

    # -- scan stall: settled by a staged card OR a recorded crash (scan_failed, already
    # DM'd where the scan was asked); a kill that recorded nothing drops after 7 days --
    settled = {str(e.get("scan_id")) for e in events
               if e.get("event") in ("staged", "scan_failed") and e.get("scan_id")}
    for e in events:
        if e.get("event") != "scan_started" or str(e.get("scan_id") or "") in settled:
            continue
        age = now - float(e.get("ts") or 0)
        if SCAN_STALL_S < age <= SCAN_STALL_MAX_S:
            findings.append(f"a scan started {e.get('at') or '?'} and never staged a card (no crash "
                            f"was recorded; this finding drops {SCAN_STALL_MAX_S // st.DAY_S:.0f} days "
                            "after the start)")

    # -- the latest staged proposal was BLIND: a lane that can only propose nothing must
    # not read green until a sighted scan is staged (D-051 r1 registry-ops#1) --
    last = next((e for e in reversed(events) if e.get("event") == "staged"), None)
    if last is not None and last.get("blind"):
        detail = str(last.get("blind_detail") or "")
        shrink = reg.shrink_copy(detail) if last.get("blind") == "registry_unreadable" else None
        hint = f" {shrink[0]}. {shrink[1]}".rstrip() if shrink is not None else ""
        findings.append(f"LATEST SCAN BLIND: the dead-channel scan staged {last.get('at') or '?'} "
                        f"({last.get('trigger') or '?'}) proposed nothing -- {last.get('blind')}"
                        f"{': ' + detail if detail else ''}.{hint}")
    if findings:
        out["status"] = "warn"
    log.info("channel_archive monitor status=%s findings=%d coverage=%s dry_run=%s",
             out["status"], len(findings), cov, dry_run)
    return out


def coverage_line(cov: dict) -> str:
    return (f"{cov.get('list_rows', 0)} channels listed, {cov.get('archived_in_list', 0)} archived, "
            f"{cov.get('examined', 0)} examined, {cov.get('histories', 0)} histories read, "
            f"{cov.get('ledger_rows', 0)} ledger rows")
