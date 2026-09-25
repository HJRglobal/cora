"""Shared fakes for the Code #16 C1 dead-channel lane tests.

Every Slack response is a REAL non-dict ``slack_sdk.web.slack_response.SlackResponse``
(lesson 68: a dict fake hid an ``isinstance(x, dict)`` seam that killed a lane in
prod). No test here opens a socket: every client is this fake.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from slack_sdk.errors import SlackApiError
from slack_sdk.web.slack_response import SlackResponse

REPO = Path(__file__).resolve().parents[1]
REGISTRY_FIXTURE = REPO / "tests" / "fixtures" / "channel-archive-registry.md"

NOW = datetime(2026, 9, 25, 19, 0, tzinfo=timezone.utc).timestamp()
DAY = 86400.0
HARRISON = "U0B2RM2JYJ1"
BOT_UID = "UCORABOT1"
BOT_ID = "BCORABOT1"
PERSON = "UPERSON01"
PERSON2 = "UPERSON02"
APPUSER = "UAPPUSER1"      # users.info is_app_user
BOTUSER = "UBOTUSER1"      # users.info is_bot
LEXSTAFF = "ULEXSTAFF1"    # org_roles primary entity LEX-LLC (fake roles below)


def resp(data: dict, headers: dict | None = None) -> SlackResponse:
    return SlackResponse(client=None, http_verb="POST", api_url="https://slack.test/api",
                         req_args={}, data=data, headers=headers or {}, status_code=200)


def api_error(code: str) -> SlackApiError:
    return SlackApiError(message=code, response=resp({"ok": False, "error": code}))


def ts_days_ago(days: float, now: float = NOW) -> str:
    return f"{now - days * DAY:.6f}"


def msg(days: float, user: str | None = PERSON, now: float = NOW, **kw: Any) -> dict:
    m: dict[str, Any] = {"ts": ts_days_ago(days, now), "type": "message",
                         "text": "SECRET BODY TEXT never leaves the fetch"}
    if user is not None:
        m["user"] = user
    m.update(kw)
    return m


def chan(cid: str, name: str, *, created_days: float = 400, private: bool = False,
         member: bool = True, now: float = NOW, **kw: Any) -> dict:
    c: dict[str, Any] = {"id": cid, "name": name, "is_private": private, "is_member": member,
                         "is_archived": False, "is_general": False, "is_ext_shared": False,
                         "is_shared": False, "is_org_shared": False,
                         "is_pending_ext_shared": False, "created": int(now - created_days * DAY),
                         "num_members": 5, "purpose": {"value": "SECRET PURPOSE"},
                         "topic": {"value": "SECRET TOPIC"}}
    c.update(kw)
    return c


class _Role:
    def __init__(self, entity: str):
        self.entity = entity
        self.entities = []


class FakeRoles:
    """org_roles stand-in: HARRISON's primary entity is FNDR (entities incl. LEX);
    LEXSTAFF's primary entity is LEX-LLC."""
    def __init__(self, extra: dict | None = None):
        self._map = {HARRISON: _Role("FNDR"), LEXSTAFF: _Role("LEX-LLC"),
                     PERSON: _Role("F3E"), PERSON2: _Role("OSN")}
        self._map.update(extra or {})

    def get_role(self, uid: str):
        return self._map.get(uid)


class FakeSlack:
    """A Slack WebClient stand-in with Slack's window/pagination semantics."""

    def __init__(self, *, channels: list[dict] | None = None, history: dict | None = None,
                 users: dict | None = None, pins: dict | None = None,
                 bookmarks: dict | None = None, members: dict | None = None,
                 scopes: list[str] | None = None, bot_uid: str = BOT_UID, bot_id: str = BOT_ID,
                 page_size: int | None = None, list_error: Exception | None = None,
                 archive: Any = None, post: Any = None, info: dict | None = None,
                 history_override: Any = None):
        self.channels = list(channels or [])
        self.history = dict(history or {})
        self.users = {PERSON: {"is_bot": False}, PERSON2: {"is_bot": False},
                      HARRISON: {"is_bot": False}, LEXSTAFF: {"is_bot": False},
                      APPUSER: {"is_bot": False, "is_app_user": True},
                      BOTUSER: {"is_bot": True}}
        self.users.update(users or {})
        self.pins = dict(pins or {})
        self.bookmarks = dict(bookmarks or {})
        self.members = dict(members or {})
        self.scopes = scopes
        self.bot_uid = bot_uid
        self.bot_id = bot_id
        self.page_size = page_size
        self.list_error = list_error
        self.archive_behaviour = archive
        self.post_behaviour = post
        self.info_override = dict(info or {})
        self.history_override = history_override
        self.calls: list[tuple[str, dict]] = []
        self.archived: set[str] = set()
        self.posts: list[dict] = []

    # ── identity ──
    def auth_test(self, **kw):
        self.calls.append(("auth_test", kw))
        headers = {"x-oauth-scopes": ",".join(self.scopes)} if self.scopes else {}
        return resp({"ok": True, "user_id": self.bot_uid, "bot_id": self.bot_id}, headers)

    def users_info(self, user: str, **kw):
        self.calls.append(("users_info", {"user": user}))
        u = self.users.get(user)
        if u is None or isinstance(u, Exception):
            raise u if isinstance(u, Exception) else api_error("user_not_found")
        return resp({"ok": True, "user": {"id": user, **u}})

    # ── listing ──
    def conversations_list(self, **kw):
        self.calls.append(("conversations_list", kw))
        if self.list_error is not None:
            raise self.list_error
        return resp({"ok": True, "channels": self.channels,
                     "response_metadata": {"next_cursor": ""}})

    def conversations_info(self, channel: str, **kw):
        self.calls.append(("conversations_info", {"channel": channel, **kw}))
        ov = self.info_override.get(channel)
        if isinstance(ov, Exception):
            raise ov
        base = next((dict(c) for c in self.channels if c.get("id") == channel), None)
        if base is None:
            raise api_error("channel_not_found")
        if channel in self.archived:
            base["is_archived"] = True
        if isinstance(ov, dict):
            base.update(ov)
        return resp({"ok": True, "channel": base})

    def conversations_history(self, channel: str, oldest: str | None = None,
                              latest: str | None = None, limit: int = 100,
                              cursor: str | None = None, inclusive: bool = False, **kw):
        self.calls.append(("conversations_history", {"channel": channel, "oldest": oldest,
                                                     "latest": latest, "cursor": cursor,
                                                     "inclusive": inclusive}))
        if self.history_override is not None:
            return self.history_override(channel, oldest, latest, cursor)
        raw = self.history.get(channel, [])
        if isinstance(raw, Exception):
            raise raw
        lo = float(oldest) if oldest is not None else None
        hi = float(latest) if latest is not None else None

        def inside(m):
            t = float(m["ts"])
            if lo is not None and not (t > lo or (inclusive and t == lo)):
                return False
            if hi is not None and not (t < hi or (inclusive and t == hi)):
                return False
            return True
        msgs = sorted([m for m in raw if inside(m)], key=lambda m: -float(m["ts"]))
        size = self.page_size or limit
        start = int(cursor or 0)
        page = msgs[start:start + size]
        more = start + size < len(msgs)
        return resp({"ok": True, "messages": page, "has_more": more,
                     "response_metadata": {"next_cursor": str(start + size) if more else ""}})

    # ── per-channel extras ──
    def pins_list(self, channel: str, **kw):
        self.calls.append(("pins_list", {"channel": channel}))
        v = self.pins.get(channel, 0)
        if isinstance(v, Exception):
            raise v
        return resp({"ok": True, "items": [{"type": "message"}] * int(v)})

    def bookmarks_list(self, channel_id: str, **kw):
        self.calls.append(("bookmarks_list", {"channel_id": channel_id}))
        v = self.bookmarks.get(channel_id, 0)
        if isinstance(v, Exception):
            raise v
        return resp({"ok": True, "bookmarks": [{"id": "Bk"}] * int(v)})

    def conversations_members(self, channel: str, limit: int = 200, cursor: str | None = None, **kw):
        self.calls.append(("conversations_members", {"channel": channel}))
        v = self.members.get(channel, [HARRISON, PERSON])
        if isinstance(v, Exception):
            raise v
        return resp({"ok": True, "members": list(v), "response_metadata": {"next_cursor": ""}})

    # ── writes ──
    def conversations_open(self, users: list[str], **kw):
        self.calls.append(("conversations_open", {"users": users}))
        return resp({"ok": True, "channel": {"id": "DHARRISON1"}})

    def chat_postMessage(self, **kw):
        self.calls.append(("chat_postMessage", kw))
        if callable(self.post_behaviour):
            out = self.post_behaviour(kw)
            if isinstance(out, Exception):
                raise out
        self.posts.append(kw)
        return resp({"ok": True, "ts": f"1790000000.{len(self.posts):06d}", "channel": kw.get("channel")})

    def chat_update(self, **kw):
        self.calls.append(("chat_update", kw))
        return resp({"ok": True, "ts": kw.get("ts")})

    def chat_postEphemeral(self, **kw):
        self.calls.append(("chat_postEphemeral", kw))
        return resp({"ok": True})

    def conversations_archive(self, channel: str, **kw):
        self.calls.append(("conversations_archive", {"channel": channel}))
        b = self.archive_behaviour
        if callable(b):
            out = b(channel)
            if isinstance(out, BaseException):
                raise out
            if out == "ok_no_effect":
                return resp({"ok": True})
        elif isinstance(b, BaseException):
            raise b
        self.archived.add(channel)
        return resp({"ok": True})

    def method_names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def history_channels(self) -> set[str]:
        return {c[1]["channel"] for c in self.calls if c[0] == "conversations_history"}


def context(*, registry=None, deny="real", bot_uid: str = BOT_UID, bot_id: str = BOT_ID,
            keep_state: dict | None = None, unarchive_state: dict | None = None,
            roles: Any = None, store_ok: bool = True):
    from cora.channel_archive import registry as reg
    if registry is None:
        registry = reg.parse_registry(REGISTRY_FIXTURE.read_text(encoding="utf-8"))
    if deny == "real":
        deny = reg.load_deny_policy()
    return reg.Context(registry=registry, deny=deny, bot_uid=bot_uid, bot_id=bot_id,
                       harrison_id=HARRISON, keep_state=dict(keep_state or {}),
                       unarchive_state=dict(unarchive_state or {}),
                       roles=roles if roles is not None else FakeRoles(), store_ok=store_ok)


def no_sleep(_s: float) -> None:
    return None
