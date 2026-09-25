"""The candidate scan of the dead-channel lane (Code #16 C1).

Lists the public + private channels Cora belongs to (DMs and group DMs NEVER --
they are neither requested nor kept), runs the ONE classifier over each, and
returns a plain-dict result the store persists and the card renders. Metadata
only (D-082): ids, names (non-LEX, non-denied only), ages and counts.

A BLIND scan proposes nothing and names its cause (A19): the registry or the deny
policy could not be read completely, the channel list could not be paginated to
its end, or Cora's own bot user id is unknown (her posts would then read as a
person's and every channel would look alive).
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from . import classify as cl
from . import registry as reg

log = logging.getLogger(__name__)

LIST_MAX_PAGES = 20
_CHANNEL_FIELDS = ("id", "name", "is_private", "is_member", "is_archived", "is_general",
                   "is_ext_shared", "is_shared", "is_org_shared", "is_pending_ext_shared",
                   "created", "num_members", "is_im", "is_mpim", "properties")


def project_channel(ch: Any) -> dict:
    """Metadata projection of a conversations.list / conversations.info row. Topic,
    purpose and every other free-text field are dropped here (D-082)."""
    if not isinstance(ch, dict):
        return {}
    return {k: ch.get(k) for k in _CHANNEL_FIELDS if k in ch}


def list_member_channels(client: Any) -> tuple[list[dict] | None, str]:
    """(channels, "") or (None, cause) when the list could not be read to its end."""
    out: list[dict] = []
    cursor = ""
    for _ in range(LIST_MAX_PAGES):
        kwargs: dict[str, Any] = {"types": "public_channel,private_channel",
                                  "exclude_archived": True, "limit": 1000}
        if cursor:
            kwargs["cursor"] = cursor
        try:
            resp = client.conversations_list(**kwargs)
        except Exception as exc:  # noqa: BLE001
            return None, cl._err_code(exc)
        chans = resp.get("channels")
        if not isinstance(chans, list):
            return None, "shape"
        for ch in chans:
            if not isinstance(ch, dict) or ch.get("is_im") or ch.get("is_mpim"):
                continue
            if ch.get("is_member"):
                out.append(project_channel(ch))
        md = resp.get("response_metadata")
        nxt = md.get("next_cursor") if isinstance(md, dict) else None
        if not isinstance(nxt, str):
            return None, "no_cursor_field"
        if not nxt:
            return out, ""
        cursor = nxt
    return None, "page_cap"


def row_from_verdict(meta: dict, v: cl.Verdict, *, keep_count: int = 0) -> dict:
    """The persisted/rendered row. A LEX channel's NAME is never stored (A6) -- only
    its id (rendered as <#CID>) and a name fingerprint for the tap's re-verify."""
    cid = str(meta.get("id") or "")
    name = str(meta.get("name") or "")
    row: dict[str, Any] = {
        "cid": cid,
        "section": v.kind,
        "reason": v.reason if v.kind == cl.SECTION_B else "",
        "lex": bool(v.lex),
        "name_fp": reg.name_fp(name),
        "is_private": bool(meta.get("is_private")),
        "last_person_days": v.last_person_days,
        "scanned_older": v.scanned_older,
        "history_complete": v.history_complete,
        "bot_posts": v.bot_posts,
        "bot_latest_days": v.bot_latest_days,
        "member_count": v.member_count,
        "harrison_member": v.harrison_member,
        "pins": v.pins,
        "bookmarks": v.bookmarks,
        "created_days": v.created_days,
        "canvas": v.canvas,
        "tabs": v.tabs,
        "keep_count": int(keep_count or 0),
    }
    if not v.lex:
        row["name"] = name
        row["entity"] = reg.route_label(name)
    if v.unarchived or v.reason == cl.B_UNARCHIVED_BEFORE:
        # r2:c1-false-inactive#1: persisted on EVERY row with unarchive state (the card
        # discloses it under any primary reason; the T1 re-verify requires the same flag)
        row["unarchived"] = True
        row["unarchived_by"] = v.unarchived_by
        row["unarchived_at"] = v.unarchived_at
    if "members" in (v.extra.get("unreadable") or []):
        # r2:c1-false-inactive#2: the fail-safe LEX / not-a-member came from a READ
        # FAILURE -- the card says so instead of stating a membership fact
        row["members_unreadable"] = True
    return row


def scan(client: Any, ctx: reg.Context, *, now: float | None = None,
         sleep: Callable[[float], None] | None = None) -> dict:
    """Run the whole scan. Returns
    {blind, blind_detail, scanned, rows, counts{exempt:{cls:n}, active, unreadable,
     unreadable_codes:{code:n}, a, b:{reason:n}}, candidate_ids}. Never raises."""
    now = time.time() if now is None else float(now)
    sleep = time.sleep if sleep is None else sleep
    result: dict[str, Any] = {"blind": None, "blind_detail": "", "scanned": 0, "rows": [],
                              "counts": {"exempt": {}, "active": 0, "unreadable": 0,
                                         "unreadable_codes": {}, "a": 0, "b": {}},
                              "candidate_ids": []}
    blind = ctx.blind_cause()
    if blind:
        result["blind"] = blind
        result["blind_detail"] = ctx.registry.reason if blind == "registry_unreadable" else ""
        log.info("channel_archive scan BLIND cause=%s", blind)
        return result
    chans, cause = list_member_channels(client)
    if chans is None:
        result["blind"] = "list_incomplete"
        result["blind_detail"] = cause
        log.info("channel_archive scan BLIND cause=list_incomplete code=%s", cause)
        return result
    counts = result["counts"]
    for meta in chans:
        result["scanned"] += 1
        v = cl.classify_channel(client, meta, ctx, now=now, sleep=sleep)
        if v.kind == cl.EXEMPT:
            counts["exempt"][v.reason] = counts["exempt"].get(v.reason, 0) + 1
        elif v.kind == cl.ACTIVE:
            counts["active"] += 1
        elif v.kind == cl.UNKNOWN:
            counts["unreadable"] += 1
            counts["unreadable_codes"][v.reason] = counts["unreadable_codes"].get(v.reason, 0) + 1
        else:
            keep_count = int((ctx.keep_state.get(meta.get("id")) or {}).get("count") or 0)
            row = row_from_verdict(meta, v, keep_count=keep_count)
            result["rows"].append(row)
            if v.kind == cl.SECTION_A:
                counts["a"] += 1
                result["candidate_ids"].append(row["cid"])
            else:
                counts["b"][v.reason] = counts["b"].get(v.reason, 0) + 1
    # A before B; oldest silence first inside each section (the likeliest dead first)
    result["rows"].sort(key=lambda r: (0 if r["section"] == cl.SECTION_A else 1,
                                       -(r.get("last_person_days") or 10 ** 6)))
    log.info("channel_archive scan scanned=%d a=%d b=%d active=%d unreadable=%d exempt=%d",
             result["scanned"], counts["a"], sum(counts["b"].values()), counts["active"],
             counts["unreadable"], sum(counts["exempt"].values()))
    return result
