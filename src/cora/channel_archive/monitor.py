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
      + WARN. Never from a blind read; never under --dry-run.
  (2) MISMATCH -- a ledger ``archived`` channel that Slack lists as NOT archived.
      The unarchive is searched from the archive time forward (bounded): a person's
      unarchive -> an ``unarchived_seen`` row (the channel leaves the examined set);
      a BOT unarchive -> WARN (Cora has no unarchive path); none found -> WARN.
  (3) UNRESOLVED -- an intent older than 1 h with no outcome, or an ``unknown``
      outcome -> WARN with ids; once Slack decides, a reconciled outcome is appended
      to the ledger AND the store (the next card render shows it).
  (4) BLIND -- the channel list paginated incompletely, zero archived channels in
      it, no bot identity, or an unreadable ledger/store -> WARN, never OK.
  (5) DEMOTED -- the demotion file exists -> WARN daily until Harrison clears it.
Plus: an archived channel (in window) whose newest 20 messages hold no archive-type
message -> "cannot attribute" WARN; a scan_started with no staged card after 1 h ->
WARN. The OK line carries coverage counts (lesson 63: a zero count certifies nothing
without positive coverage).
"""
from __future__ import annotations

import glob
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import classify as cl
from . import policy
from . import store as st

log = logging.getLogger(__name__)

_AZ = timezone(timedelta(hours=-7))
#: Nothing this lane did can predate its own merge (Code #16, 2026-09-25).
LANE_EPOCH = datetime(2026, 9, 25, 0, 0, tzinfo=_AZ).timestamp()
WINDOW_DAYS = 35
INTENT_BEFORE_S = 15 * 60
INTENT_AFTER_S = 120
UNRESOLVED_S = 3600
SCAN_STALL_S = 3600
HISTORY_LIMIT = 20
UNARCHIVE_SEARCH_PAGES = 5
LIST_MAX_PAGES = 20
PACE_S = 1.0
ARCHIVE_SUBTYPES = ("channel_archive", "group_archive")
UNARCHIVE_SUBTYPES = ("channel_unarchive", "group_unarchive")
_REPO_ROOT = Path(__file__).resolve().parents[3]


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


def _unarchive_after(client: Any, cid: str, since: float) -> tuple[dict | None, str]:
    cursor = ""
    for _ in range(UNARCHIVE_SEARCH_PAGES):
        kwargs: dict[str, Any] = {"channel": cid, "oldest": f"{since:.6f}", "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        try:
            resp = client.conversations_history(**kwargs)
        except Exception as exc:  # noqa: BLE001
            return None, cl._err_code(exc)
        for m in resp.get("messages") or []:
            if isinstance(m, dict) and m.get("subtype") in UNARCHIVE_SUBTYPES:
                return {k: m.get(k) for k in ("ts", "user", "bot_id")}, ""
        md = resp.get("response_metadata")
        nxt = md.get("next_cursor") if isinstance(md, dict) else ""
        if not (resp.get("has_more") is True and nxt):
            return None, ""
        cursor = str(nxt)
    return None, "page_cap"


def _legacy_archived_ids() -> set[str]:
    """Channels the pre-lane sprawl script archived (logs/archive-sprawl-*.jsonl)."""
    ids: set[str] = set()
    for path in glob.glob(str(_REPO_ROOT / "logs" / "archive-sprawl-*.jsonl")):
        try:
            for line in Path(path).read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(row, dict) and str(row.get("status") or "") == "archived" and row.get("id"):
                    ids.add(str(row["id"]))
        except Exception:  # noqa: BLE001
            continue
    return ids


def _is_cora(m: dict, bot_uid: str, bot_id: str) -> bool:
    return (bool(bot_uid) and m.get("user") == bot_uid) or (bool(bot_id) and m.get("bot_id") == bot_id)


def reconcile(client: Any, *, now: float | None = None, dry_run: bool = False,
              sleep: Callable[[float], None] | None = None) -> dict:
    """Run every class. Returns {status: ok|warn, findings[], coverage{}, demotion_written}."""
    now = time.time() if now is None else float(now)
    sleep = time.sleep if sleep is None else sleep
    findings: list[str] = []
    cov = {"list_rows": 0, "archived_in_list": 0, "examined": 0, "histories": 0,
           "ledger_rows": 0}
    out: dict[str, Any] = {"status": "ok", "findings": findings, "coverage": cov,
                           "demotion_written": False, "blind": ""}

    def blind(why: str) -> dict:
        out["blind"] = why
        findings.append(f"BLIND: {why} -- nothing could be verified")
        out["status"] = "warn"
        return out

    dem = policy.demotion_state()
    if dem is not None:
        findings.append(f"DEMOTED since {dem.get('since') or '?'} (channel {dem.get('channel_id') or '?'}: "
                        f"{dem.get('reason') or 'unattributed archive'}) -- the lane acts at T0 until "
                        "Harrison clears it (run_channel_archive_proposal.py --clear-demotion --apply)")
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
                ledger_archived[cid] = max(ledger_archived.get(cid, 0.0), float(r.get("ts") or 0))
    legacy = _legacy_archived_ids()
    floor_ts = max(now - WINDOW_DAYS * st.DAY_S, LANE_EPOCH)
    harrison = _harrison()

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
    for cid in sorted(examine):
        cov["examined"] += 1
        sleep(PACE_S)
        ev, err = _latest_archive_event(client, cid)
        cov["histories"] += 1
        if err:
            findings.append(f"{cid}: history unreadable ({err}) -- cannot attribute its archive")
            continue
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
            continue                          # predates the window / the lane
        if cid in legacy:
            continue                          # the pre-lane sprawl script's archive
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
            continue
        findings.append(f"UNATTRIBUTED: {cid} was archived by Cora at {ev.get('ts')} with no "
                        "tap-attributed intent in the ledger -- the lane is DEMOTED to T0")
        if dem is None and not out["demotion_written"]:
            wrote = st.write_demotion({
                "since": datetime.fromtimestamp(now, _AZ).isoformat(timespec="seconds"),
                "channel_id": cid, "archive_ts": str(ev.get("ts") or ""),
                "reason": "an archive by Cora with no tap-attributed ledger intent"},
                dry_run=dry_run)
            out["demotion_written"] = wrote
            dem = {"channel_id": cid}         # one demotion record per run

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
        sleep(PACE_S)
        un, err = _unarchive_after(client, cid, arch_ts)
        cov["histories"] += 1
        if err:
            findings.append(f"MISMATCH: {cid} ledger says archived, Slack says open; history "
                            f"unreadable ({err})")
        elif un is None:
            findings.append(f"MISMATCH: {cid} ledger says archived, Slack says open, and no "
                            "unarchive message was found")
        elif _is_cora(un, bot_uid, bot_id):
            findings.append(f"MISMATCH: {cid} was UNARCHIVED BY CORA -- she has no unarchive path")
        elif not dry_run:
            st.append_ledger("unarchived_seen", channel_id=cid, unarchive_ts=float(un.get("ts") or now),
                             by=str(un.get("user") or ""), ts=now)

    # -- (3) UNRESOLVED --
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
            c = by_id.get(cid)
            if c is not None and c.get("is_archived"):
                sleep(PACE_S)
                ev, _err = _latest_archive_event(client, cid)
                cov["histories"] += 1
                if ev and _is_cora(ev, bot_uid, bot_id) and float(ev.get("ts") or 0) >= its - 1:
                    if not dry_run:
                        st.append_ledger("outcome", proposal_id=pid, channel_id=cid,
                                         outcome="archived (reconciled)", tapped_by=r.get("tapped_by"),
                                         ts=now)
                        st.append_event("reconciled", proposal_id=pid, cid=cid, outcome="archived",
                                        ts=now)
                    continue
            elif c is not None and now - its >= UNRESOLVED_S:
                if not dry_run:
                    st.append_ledger("outcome", proposal_id=pid, channel_id=cid,
                                     outcome="failed (reconciled)", tapped_by=r.get("tapped_by"),
                                     ts=now)
                    st.append_event("reconciled", proposal_id=pid, cid=cid, outcome="failed",
                                    ts=now)
                findings.append(f"RECONCILED: {cid} intent {pid} never archived (Slack shows it "
                                "open) -- recorded as failed; the channel may still carry the "
                                "notice")
                continue
            findings.append(f"UNRESOLVED: {cid} intent {pid} has no settled outcome")

    # -- scan stall --
    staged_scans = {str(e.get("scan_id")) for e in events if e.get("event") == "staged" and e.get("scan_id")}
    for e in events:
        if e.get("event") == "scan_started" and str(e.get("scan_id")) not in staged_scans \
                and now - float(e.get("ts") or 0) > SCAN_STALL_S:
            findings.append(f"a scan started {e.get('at') or '?'} and never staged a card")
    if findings:
        out["status"] = "warn"
    log.info("channel_archive monitor status=%s findings=%d coverage=%s dry_run=%s",
             out["status"], len(findings), cov, dry_run)
    return out


def coverage_line(cov: dict) -> str:
    return (f"{cov.get('list_rows', 0)} channels listed, {cov.get('archived_in_list', 0)} archived, "
            f"{cov.get('examined', 0)} examined, {cov.get('histories', 0)} histories read, "
            f"{cov.get('ledger_rows', 0)} ledger rows")
