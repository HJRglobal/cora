"""Live channel-membership authority for DTC inventory writes (Code #12 G1;
cq-f23d6885dd1c + cq-28de84159c3f; C1/Q4 ruled HYBRID 2026-09-01, membership
ruled 2026-09-08 -- Harrison: "Anyone 'in-channel' has authority/permission").

THE INCIDENT, verified on the roster files 2026-09-09: Skylar Eastham
(U0B7BV5688Y) is a live member of #f3-hq-inventory-adjustments (Hannah's 8/21
decision cleared her explicitly on 8/28) but appears in NEITHER user-permissions.yaml
NOR org-roles.yaml. user_access.is_authorized therefore read her as an unknown user
(FNDR/HJRG only) and the pre-LLM entity gate refused her inventory writes in an F3E
channel -- "Cora refuses DTC inventory adjustments on first/second request from an
authorized user". The same request from Hannah (in the roster) went through: the
"approval parity" bug is roster membership, not an allowlist question.

THE RULE:
  * IN the configured write channel, membership is proven by the post itself -- you
    cannot post in a channel you are not in -- so app.py grants entity scope for an
    inventory WRITE request there (user_access.check_access(entity_grant=True));
    every sensitive-topic block still runs. No API call is needed or made.
  * OUT of it (a DM, any other channel), the allowlist is the LIVE membership of
    that channel, read here at request time and cached <= MEMBERSHIP_TTL_SECONDS.
    No hand-maintained name list (frozen lists are retired, 1jjjjjjj). A lookup
    that fails REFUSES -- "could not verify" must never read as "authorized".
  * Harrison is a member like anyone else; no founder side-door in this module.

Bot-loaded (tool_dispatch imports it); the write channels come from
data/maps/inventory-channel-config.yaml (`slack_channel_id`).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable

from . import guard_input

log = logging.getLogger(__name__)

MEMBERSHIP_TTL_SECONDS = 300      # ruled: cache <= 5 min
_MAX_PAGES = 20                   # 20 x 200 members; a run-away cursor reads as a failed lookup

_CACHE: dict[str, tuple[float, frozenset[str]]] = {}
_LOCK = threading.Lock()


def _default_client() -> Any:
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        return None
    from slack_sdk import WebClient
    return WebClient(token=token, timeout=5)


def reset_cache() -> None:
    with _LOCK:
        _CACHE.clear()


def member_ids(channel_id: str, *, client_factory: Callable | None = None,
               now: float | None = None) -> frozenset[str] | None:
    """The live member ids of one channel (paginated), or None when the lookup
    FAILED -- never an empty set for a failure, so a caller can tell "nobody is a
    member" from "I could not check". Cached per channel for MEMBERSHIP_TTL_SECONDS;
    a failure is never cached."""
    cid = str(channel_id or "").strip()
    if not cid:
        return None
    now = time.monotonic() if now is None else now
    with _LOCK:
        hit = _CACHE.get(cid)
        if hit is not None and (now - hit[0]) < MEMBERSHIP_TTL_SECONDS:
            return hit[1]
    try:
        client = (client_factory or _default_client)()
        if client is None:
            log.warning("inventory_membership: no Slack client (SLACK_BOT_TOKEN unset) -- "
                        "membership of %s cannot be read", cid)
            return None
        ids: set[str] = set()
        cursor = ""
        for _ in range(_MAX_PAGES):
            kwargs: dict[str, Any] = {"channel": cid, "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = client.conversations_members(**kwargs)
            ids.update(str(m) for m in (resp.get("members") or []))
            cursor = str(((resp.get("response_metadata") or {}).get("next_cursor")) or "")
            if not cursor:
                break
        else:
            log.warning("inventory_membership: pagination did not terminate for %s -- "
                        "treating the listing as failed", cid)
            return None
        members = frozenset(ids)
    except Exception as exc:  # noqa: BLE001 -- a failed lookup is a refusal, never a crash
        log.warning("inventory_membership: conversations.members failed for %s: %s", cid, exc)
        return None
    with _LOCK:
        _CACHE[cid] = (now, members)
    return members


def allows(user_id: str, *, client_factory: Callable | None = None) -> tuple[bool, str]:
    """(allowed, reason) for an OUT-OF-CHANNEL inventory write by ``user_id``.

    reason in {"member", "not_member", "lookup_failed", "no_channel_configured",
    "no_user"} -- only "member" allows. Fail closed on every other branch.
    """
    uid = str(user_id or "").strip()
    if not uid:
        return False, "no_user"
    channel_ids = guard_input.inventory_write_channel_ids()
    if not channel_ids:
        return False, "no_channel_configured"
    any_lookup_ok = False
    for cid in sorted(channel_ids):
        members = member_ids(cid, client_factory=client_factory)
        if members is None:
            continue
        any_lookup_ok = True
        if uid in members:
            return True, "member"
    if not any_lookup_ok:
        return False, "lookup_failed"
    return False, "not_member"


def refusal_text(reason: str) -> str:
    """User-facing, source-opaque, and honest about WHY -- a failed lookup is not
    "you are not allowed", it is "I could not check"."""
    channel = "#f3-hq-inventory-adjustments"
    if reason == "lookup_failed":
        return (f"I couldn't verify channel membership just now, so I did not change the "
                f"count. Post the request in {channel} instead -- anyone in that channel can "
                f"file it there.")
    if reason == "no_channel_configured":
        return (f"The inventory write channel isn't configured on my side, so I did not "
                f"change the count. Post the request in {channel}.")
    return (f"DTC inventory changes from outside {channel} are limited to that channel's "
            f"members. Post the request there (anyone in the channel can file it), or ask "
            f"Hannah to add you to the channel.")
