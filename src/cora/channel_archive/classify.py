"""The ONE classifier of the dead-channel lane (Code #16 C1, amendment A5).

``classify_channel`` is called by the scan AND by the T1 tap's live re-verify, with
a context loaded fresh each time, so the tap can never act on a weaker judgement
than the one that listed the channel.

INACTIVE (ruled 2026-09-21, kickoff section 9.1) = no message from a PERSON for >= 90
days AND no exemption class. Boundary: a person's message aged 89.99 days keeps the
channel active; aged exactly 90.0 days it does not.

A PERSON (A3, measured 2026-09-25 over 5,038 live messages): the message has a
``user``, its subtype is not a system event, the user is not Cora's bot user or
Slackbot, and ``users.info`` says the account is not a bot and not an app user. The
live census found 106 messages carrying ``user + bot_id`` that were made BY PEOPLE
through apps with their own token -- "no bot_id" is the wrong test. A users.info
FAILURE counts as a person (fail safe = active). Everything that is neither a person
nor a system event is bot/app traffic, which moves an otherwise-dead channel to
section B ("informational", never an Archive-all row -- the kickoff's D-051 probe).
``bot_message``-subtype posts (webhooks, legacy integrations) are bot traffic here,
not system events: a webhook-only channel is exactly "a channel whose only recent
traffic is bot posts".

FAIL SAFE: every read error, a page cap, and any pagination shape other than
``has_more is False`` make the channel UNKNOWN -- never a candidate (A8).

D-082 METADATA ONLY: each message is projected to
{ts, user, bot_id, app_id, subtype, reply_count, latest_reply} at the call site;
``text``/``blocks``/``files`` never leave ``_history_page``.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import registry as reg

THRESHOLD_DAYS = 90
YOUNG_DAYS = 30
DAY_S = 86400.0
PAGE_LIMIT = 200
PHASE1_MAX_PAGES = 10
PHASE2_MAX_PAGES = 10          # 2,000 messages (A4)
MEMBERS_MAX_PAGES = 10
PACE_S = 2.0                   # >= 2.0 s between history/pins calls (A24)
LATEST_SKEW_S = 300            # latest = now + 300 s: Slack's clock may run ahead of ours (A8)
SLACKBOT = "USLACKBOT"

#: System events: neither a person's activity nor bot traffic.
SYSTEM_SUBTYPES: frozenset = frozenset({
    # the sprawl script's set (scripts/archive_sprawl_channels.py), minus
    # channel_unarchive (A10: an unarchive by a person IS activity) and minus
    # bot_message (bot traffic -- see the module docstring)
    "channel_join", "channel_leave", "channel_topic", "channel_purpose",
    "channel_name", "channel_archive", "pinned_item", "bot_add", "bot_remove",
    "reminder_add", "tombstone", "group_join", "group_leave",
    # A3 additions
    "group_archive", "channel_convert_to_private", "channel_convert_to_public",
    "app_conversation_join", "tabbed_canvas_updated", "unpinned_item",
    "group_topic", "group_purpose", "group_name",
})
UNARCHIVE_SUBTYPES: frozenset = frozenset({"channel_unarchive", "group_unarchive"})

# Verdict kinds
EXEMPT, ACTIVE, UNKNOWN, SECTION_A, SECTION_B = "exempt", "active", "unknown", "A", "B"

# Non-overridable exemption classes (never listed with a button; counted only).
X_DM = "dm"
X_DENIED = "denied"
X_GENERAL = "general"
X_SHARED = "shared"
X_INFO = "info_for_cora"
X_ARCHIVED = "archived"
X_LEAD_FIN = "leadership_finance"
X_YOUNG = "young"
X_KEPT = "kept"
X_UNARCHIVE_SUPPRESSED = "unarchive_suppressed"
X_PINNED = "pinned"

# Section-B reasons. The first three are EXEMPTIONS Harrison may override for one
# channel; the rest are "look closer" rows. All of them are per-row only.
B_LEX = "lex"
B_REGISTRY = "registry"
B_KEEP_LIST = "keep_list"
B_UNARCHIVED_BEFORE = "unarchived_before"
B_PRIVATE_NOT_MEMBER = "private_not_member"
B_BOT_TRAFFIC = "bot_traffic"
B_THREADS_UNCHECKED = "threads_unchecked"
B_REASONS: tuple = (B_LEX, B_REGISTRY, B_KEEP_LIST, B_UNARCHIVED_BEFORE,
                    B_PRIVATE_NOT_MEMBER, B_BOT_TRAFFIC, B_THREADS_UNCHECKED)
OVERRIDABLE_EXEMPTIONS: frozenset = frozenset({B_LEX, B_REGISTRY, B_KEEP_LIST})


@dataclass
class Verdict:
    kind: str
    reason: str = ""
    lex: bool = False
    age_days: float | None = None          # active: age of the newest person message seen
    last_person_days: int | None = None    # inactive: from phase 2 (None = none seen)
    scanned_older: int = 0                 # phase-2 messages examined
    history_complete: bool = False         # phase 2 reached the channel's first message
    bot_posts: int = 0                     # bot/app posts inside the 90-day window
    bot_latest_days: int | None = None
    pins: int | None = None
    bookmarks: int | None = None
    member_count: int | None = None
    harrison_member: bool | None = None
    created_days: int | None = None
    canvas: bool = False
    tabs: int = 0
    unarchived_by: str = ""
    unarchived_at: float | None = None
    extra: dict = field(default_factory=dict)


def _err_code(exc: BaseException) -> str:
    resp = getattr(exc, "response", None)
    try:
        code = resp.get("error") if resp is not None else None
    except Exception:  # noqa: BLE001
        code = None
    return str(code) if code else type(exc).__name__.lower()


def _ts(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _project(m: Any) -> dict:
    if not isinstance(m, dict):
        return {}
    return {k: m.get(k) for k in ("ts", "user", "bot_id", "app_id", "subtype",
                                  "reply_count", "latest_reply")}


def _history_page(client: Any, cid: str, *, oldest: float | None, latest: float,
                  cursor: str, inclusive: bool) -> tuple[list[dict], Any, Any]:
    kwargs: dict[str, Any] = {"channel": cid, "latest": f"{latest:.6f}", "limit": PAGE_LIMIT,
                              "inclusive": inclusive}
    if oldest is not None:
        kwargs["oldest"] = f"{oldest:.6f}"
    if cursor:
        kwargs["cursor"] = cursor
    resp = client.conversations_history(**kwargs)
    raw = resp.get("messages")
    msgs = [_project(m) for m in raw] if isinstance(raw, list) else None
    md = resp.get("response_metadata")
    nxt = md.get("next_cursor") if isinstance(md, dict) else None
    if msgs is None:
        return [], None, None          # shape we do not recognise -> UNKNOWN upstream
    return msgs, resp.get("has_more"), nxt


#: ``ctx.user_cache`` value for a users.info lookup that FAILED: truthy (unknown = a
#: person, fail safe = active) but distinguishable, so a verdict decided by it can say
#: so (``Verdict.extra["unreadable"]``) and the T1 re-verify retries (A5).
USER_UNREADABLE = "unreadable"


def is_person(client: Any, uid: str, ctx: reg.Context) -> bool:
    if not uid or uid in (ctx.bot_uid, SLACKBOT):
        return False
    hit = ctx.user_cache.get(uid)
    if hit is not None:
        return bool(hit)
    try:
        resp = client.users_info(user=uid)
        u = resp.get("user") or {}
        person: Any = not bool(u.get("is_bot")) and not bool(u.get("is_app_user"))
    except Exception:  # noqa: BLE001 -- unknown = a person (fail safe = active)
        person = USER_UNREADABLE
    ctx.user_cache[uid] = person
    return bool(person)


def person_unreadable(uid: str, ctx: reg.Context) -> bool:
    """True when *uid* counted as a person only because users.info failed."""
    return bool(uid) and ctx.user_cache.get(uid) == USER_UNREADABLE


def message_kind(client: Any, m: dict, ctx: reg.Context) -> str:
    """"person" | "system" | "bot" for one projected message."""
    st = str(m.get("subtype") or "")
    uid = str(m.get("user") or "")
    if st in UNARCHIVE_SUBTYPES:
        return "person" if is_person(client, uid, ctx) else "system"
    if st in SYSTEM_SUBTYPES:
        return "system"
    if st == "bot_message" or not uid:
        return "bot"
    return "person" if is_person(client, uid, ctx) else "bot"


def thread_live(m: dict, cutoff: float) -> bool:
    """A parent with replies whose latest reply is missing, unparseable or inside the
    window keeps the channel ACTIVE (conservative: reply authors are not resolved,
    A4). A bot parent counts too -- people reply under bot posts."""
    try:
        rc = int(m.get("reply_count") or 0)
    except (TypeError, ValueError):
        rc = 1
    if rc <= 0:
        return False
    lr = _ts(m.get("latest_reply"))
    return lr is None or lr >= cutoff


def _pagination(has_more: Any, nxt: Any) -> str:
    """"done" | "next" | "unknown" (A8): exhausted ONLY when has_more is False."""
    if has_more is False:
        return "done"
    if has_more is True and isinstance(nxt, str) and nxt:
        return "next"
    return "unknown"


def _phase1(client: Any, cid: str, ctx: reg.Context, *, now: float,
            sleep: Callable[[float], None]) -> dict:
    cutoff = now - THRESHOLD_DAYS * DAY_S
    cursor = ""
    bot_posts = 0
    bot_latest: float | None = None
    for page in range(PHASE1_MAX_PAGES):
        sleep(PACE_S)
        try:
            msgs, has_more, nxt = _history_page(client, cid, oldest=cutoff,
                                                latest=now + LATEST_SKEW_S, cursor=cursor,
                                                inclusive=False)
        except Exception as exc:  # noqa: BLE001
            return {"state": "unknown", "code": _err_code(exc)}
        if has_more is None and not msgs:
            return {"state": "unknown", "code": "shape"}
        for m in msgs:
            ts = _ts(m.get("ts"))
            kind = message_kind(client, m, ctx)
            if kind == "person":
                # a person ONLY because users.info failed: the verdict says so (A5)
                unread = person_unreadable(str(m.get("user") or ""), ctx)
                if ts is None:
                    return {"state": "active", "age_days": None, "unreadable_user": unread}
                age = (now - ts) / DAY_S
                if age < THRESHOLD_DAYS:
                    return {"state": "active", "age_days": round(age, 2),
                            "unreadable_user": unread}
            elif kind == "bot" and ts is not None and ts >= cutoff:
                bot_posts += 1
                bot_latest = ts if bot_latest is None else max(bot_latest, ts)
            if thread_live(m, cutoff):
                return {"state": "active", "age_days": None, "via": "thread"}
        step = _pagination(has_more, nxt)
        if step == "done":
            return {"state": "quiet", "bot_posts": bot_posts, "bot_latest": bot_latest}
        if step == "unknown":
            return {"state": "unknown", "code": "pagination"}
        cursor = nxt
    return {"state": "unknown", "code": "page_cap"}


def _phase2(client: Any, cid: str, ctx: reg.Context, *, now: float,
            sleep: Callable[[float], None]) -> dict:
    cutoff = now - THRESHOLD_DAYS * DAY_S
    cursor = ""
    scanned = 0
    last_person: float | None = None
    for page in range(PHASE2_MAX_PAGES):
        sleep(PACE_S)
        try:
            msgs, has_more, nxt = _history_page(client, cid, oldest=None, latest=cutoff,
                                                cursor=cursor, inclusive=True)
        except Exception as exc:  # noqa: BLE001
            return {"state": "unknown", "code": _err_code(exc)}
        if has_more is None and not msgs:
            return {"state": "unknown", "code": "shape"}
        for m in msgs:
            scanned += 1
            if last_person is None and message_kind(client, m, ctx) == "person":
                last_person = _ts(m.get("ts")) or cutoff
            if thread_live(m, cutoff):
                return {"state": "active", "via": "thread"}
        step = _pagination(has_more, nxt)
        if step == "done":
            return {"state": "complete", "last_person": last_person, "scanned": scanned}
        if step == "unknown":
            return {"state": "unknown", "code": "pagination"}
        cursor = nxt
    return {"state": "capped", "last_person": last_person, "scanned": scanned}


def _count(client: Any, method: str, key: str, **kwargs: Any) -> int | None:
    try:
        resp = getattr(client, method)(**kwargs)
        items = resp.get(key)
        return len(items) if isinstance(items, list) else None
    except Exception:  # noqa: BLE001
        return None


def pins_count(client: Any, cid: str) -> int | None:
    return _count(client, "pins_list", "items", channel=cid)


def bookmarks_count(client: Any, cid: str) -> int | None:
    return _count(client, "bookmarks_list", "bookmarks", channel_id=cid)


def member_ids(client: Any, cid: str) -> list[str] | None:
    """Every member id, or None when the list could not be read COMPLETELY."""
    out: list[str] = []
    cursor = ""
    for _ in range(MEMBERS_MAX_PAGES):
        try:
            kwargs: dict[str, Any] = {"channel": cid, "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = client.conversations_members(**kwargs)
        except Exception:  # noqa: BLE001
            return None
        mem = resp.get("members")
        if not isinstance(mem, list):
            return None
        out.extend(str(x) for x in mem)
        md = resp.get("response_metadata")
        nxt = md.get("next_cursor") if isinstance(md, dict) else ""
        if not nxt:
            return out
        cursor = str(nxt)
    return None


def _properties(client: Any, cid: str, meta: dict) -> dict:
    props = meta.get("properties")
    if isinstance(props, dict):
        return props
    try:
        info = client.conversations_info(channel=cid)
        ch = info.get("channel") or {}
        props = ch.get("properties") if isinstance(ch, dict) else None
    except Exception:  # noqa: BLE001 -- informational only
        props = None
    return props if isinstance(props, dict) else {}


def precheck(meta: dict, ctx: reg.Context, *, now: float) -> str | None:
    """The non-overridable classes decidable WITHOUT a history read, in counting
    precedence. Returns the class, or None when the channel must be read."""
    cid = str(meta.get("id") or "")
    name = str(meta.get("name") or "")
    if meta.get("is_im") or meta.get("is_mpim") or cid.startswith("D"):
        return X_DM
    if ctx.deny is not None and reg.is_denied(ctx.deny, name, cid):
        return X_DENIED
    if meta.get("is_general") or cid in reg.BLOCKED_CHANNEL_IDS:
        return X_GENERAL
    if any(meta.get(k) for k in ("is_ext_shared", "is_shared", "is_org_shared",
                                 "is_pending_ext_shared")):
        return X_SHARED
    if cid == reg.INFO_FOR_CORA_CHANNEL_ID:
        return X_INFO
    if meta.get("is_archived"):
        return X_ARCHIVED
    if reg.is_leadership_or_finance(name):
        return X_LEAD_FIN
    created = _ts(meta.get("created"))
    if created is None or (now - created) / DAY_S < YOUNG_DAYS:
        return X_YOUNG
    keep = ctx.keep_state.get(cid) or {}
    if float(keep.get("until") or 0) > now:
        return X_KEPT
    un = ctx.unarchive_state.get(cid) or {}
    if un and float(un.get("at") or 0) + THRESHOLD_DAYS * DAY_S > now:
        return X_UNARCHIVE_SUPPRESSED
    return None


def classify_channel(client: Any, meta: dict, ctx: reg.Context, *, now: float | None = None,
                     sleep: Callable[[float], None] | None = None) -> Verdict:
    """Judge one channel. ``meta`` is a conversations.list / conversations.info row
    (metadata only). Never raises."""
    now = time.time() if now is None else float(now)
    sleep = time.sleep if sleep is None else sleep
    try:
        return _classify(client, meta, ctx, now=now, sleep=sleep)
    except Exception as exc:  # noqa: BLE001 -- a bug is UNKNOWN, never a candidate
        return Verdict(kind=UNKNOWN, reason=f"error:{type(exc).__name__}")


def _classify(client: Any, meta: dict, ctx: reg.Context, *, now: float,
              sleep: Callable[[float], None]) -> Verdict:
    cid = str(meta.get("id") or "")
    name = str(meta.get("name") or "")
    blind = ctx.blind_cause()
    if blind:
        return Verdict(kind=UNKNOWN, reason=blind)
    created = _ts(meta.get("created"))
    created_days = int((now - created) / DAY_S) if created is not None else None
    pre = precheck(meta, ctx, now=now)
    if pre is not None:
        return Verdict(kind=EXEMPT, reason=pre, created_days=created_days)
    lex = reg.lex_by_name(cid, name, ctx.registry)
    p1 = _phase1(client, cid, ctx, now=now, sleep=sleep)
    if p1["state"] == "unknown":
        return Verdict(kind=UNKNOWN, reason=p1["code"], lex=lex)
    if p1["state"] == "active":
        v = Verdict(kind=ACTIVE, age_days=p1.get("age_days"), lex=lex)
        if p1.get("unreadable_user"):
            v.extra["unreadable"] = ["users_info"]
        return v
    p2 = _phase2(client, cid, ctx, now=now, sleep=sleep)
    if p2["state"] == "unknown":
        return Verdict(kind=UNKNOWN, reason=p2["code"], lex=lex)
    if p2["state"] == "active":
        return Verdict(kind=ACTIVE, lex=lex)
    # -- inactive: every further read decides A vs B vs pinned (metadata only) --
    sleep(PACE_S)
    pins = pins_count(client, cid)
    if pins is None:
        return Verdict(kind=UNKNOWN, reason="pins_unknown", lex=lex)
    if pins > 0:
        return Verdict(kind=EXEMPT, reason=X_PINNED, pins=pins, created_days=created_days)
    members = member_ids(client, cid)
    lex = lex or reg.lex_by_members(members, roles=ctx.roles)
    harrison_member = (ctx.harrison_id in members) if members is not None else None
    num = meta.get("num_members")
    member_count = int(num) if isinstance(num, int) else (len(members) if members is not None else None)
    props = _properties(client, cid, meta)
    cv = props.get("canvas")
    canvas = (isinstance(cv, dict) and not cv.get("is_empty")) or cv is True
    tabs = props.get("tabs")
    lp = p2.get("last_person")
    v = Verdict(
        kind=SECTION_A, lex=lex,
        last_person_days=int((now - lp) / DAY_S) if lp else None,
        scanned_older=int(p2.get("scanned") or 0),
        history_complete=p2["state"] == "complete",
        bot_posts=int(p1.get("bot_posts") or 0),
        bot_latest_days=(int((now - p1["bot_latest"]) / DAY_S) if p1.get("bot_latest") else None),
        pins=pins, bookmarks=bookmarks_count(client, cid), member_count=member_count,
        harrison_member=harrison_member, created_days=created_days,
        canvas=canvas, tabs=len(tabs) if isinstance(tabs, list) else 0,
    )
    if members is None:
        # the fail-safe LEX / not-a-member classes below came from a READ FAILURE: the
        # scan keeps them, the T1 re-verify retries on them (A5)
        v.extra["unreadable"] = ["members"]
    un = ctx.unarchive_state.get(cid) or {}
    in_registry = cid in ctx.registry.ids or (name.lower() in ctx.registry.names if name else False)
    if lex:
        v.kind, v.reason = SECTION_B, B_LEX
    elif in_registry:
        v.kind, v.reason = SECTION_B, B_REGISTRY
    elif reg.keep_list_reason(cid, name):
        v.kind, v.reason = SECTION_B, B_KEEP_LIST
    elif un:
        v.kind, v.reason = SECTION_B, B_UNARCHIVED_BEFORE
        v.unarchived_by = str(un.get("by") or "")
        v.unarchived_at = float(un.get("at") or 0) or None
    elif meta.get("is_private") and harrison_member is not True:
        v.kind, v.reason = SECTION_B, B_PRIVATE_NOT_MEMBER
    elif v.bot_posts > 0:
        v.kind, v.reason = SECTION_B, B_BOT_TRAFFIC
    elif p2["state"] == "capped":
        v.kind, v.reason = SECTION_B, B_THREADS_UNCHECKED
    return v
