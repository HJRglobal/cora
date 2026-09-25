"""ONE delivery function for the dead-channel proposal (Code #16 C1): the founder's
DM ask and the monthly script both call ``deliver_proposal``.

Order: scan lock -> ``scan_started`` -> fresh context -> scan -> per-row tier (A12)
-> ``staged`` (persist BEFORE posting, so a card tapped in the bot always finds its
record) -> render each page from the store -> post -> ``delivered`` per page with its
``rendered_cids`` (the exact set that page's Archive-all may act on).

Nothing here archives anything. Under CORA_EVAL_MODE it makes no Slack call at all
(A20); with the lane ``off`` it returns before any read; ``dry_run`` scans (reads
only) and writes nothing -- not the store, not the lock, not a marker (A28).
"""
from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Any, Callable

from .. import confirm_cards
from . import cards, clients, gates, policy
from . import registry as reg
from . import scan as scan_mod
from . import store as st

log = logging.getLogger(__name__)

HARRISON_ID = os.environ.get("HARRISON_SLACK_USER_ID", "U0B2RM2JYJ1")


def bot_identity(client: Any) -> tuple[str, str]:
    """(bot user id, bot id) from auth.test; ("", "") when unknown (the scan is then
    BLIND: Cora's own posts would read as a person's)."""
    try:
        resp = client.auth_test()
        return str(resp.get("user_id") or ""), str(resp.get("bot_id") or "")
    except Exception:  # noqa: BLE001
        return "", ""


def load_context(read_client: Any, *, now: float, fold: st.Fold | None = None) -> reg.Context:
    """Every exemption source, read FRESH (A5): the registry (with the last good
    parse count as its floor), the strict deny policy, Cora's identity, and the keep
    and unarchive windows from the store + ledger."""
    f = fold if fold is not None else st.fold(now=now)
    ledger = st.read_ledger()
    registry = reg.load_registry(last_good_count=f.last_registry_count)
    deny = reg.load_deny_policy()
    bot_uid, bot_id = bot_identity(read_client)
    # A store or ledger we cannot read leaves keeps and unarchives unknown -- a kept
    # channel could be re-proposed -- so the scan is BLIND ("store_unreadable").
    return reg.Context(registry=registry, deny=deny, bot_uid=bot_uid, bot_id=bot_id,
                       harrison_id=HARRISON_ID, keep_state=f.keep_state(now),
                       unarchive_state=st.unarchive_state(ledger),
                       store_ok=bool(f.ok) and ledger is not None)


def _result() -> dict:
    return {"delivered": False, "reason": "", "proposal_id": "", "pages": 0, "scanned": 0,
            "blind": None, "counts": {}, "candidate_ids": [], "rows": 0}


def deliver_proposal(*, trigger: str, now: float | None = None,
                     sleep: Callable[[float], None] | None = None,
                     dry_run: bool = False) -> dict:
    """Scan, stage, post. Returns a result dict; never archives. Raises only
    ``clients.LiveClientRefused`` (a default client under pytest / no token)."""
    now = time.time() if now is None else float(now)
    out = _result()
    if os.environ.get("CORA_EVAL_MODE") == "1":
        out["reason"] = "eval_mode"
        return out
    if policy.mode() == "off":
        out["reason"] = "off"
        return out
    read = clients.read_client()
    if dry_run:
        ctx = load_context(read, now=now)
        res = scan_mod.scan(read, ctx, now=now, sleep=sleep)
        scopes = gates.granted_scopes(read, force=True)
        out.update(reason="dry_run", scanned=res["scanned"], blind=res["blind"],
                   counts=res["counts"], candidate_ids=list(res["candidate_ids"]),
                   rows=len(res["rows"]), blind_detail=res.get("blind_detail", ""),
                   b_ids=[r["cid"] for r in res["rows"] if r["section"] == "B"],
                   b_reasons={r["cid"]: r["reason"] for r in res["rows"] if r["section"] == "B"},
                   scopes={s: gates.scope_state(scopes, priv) for s, priv in
                           ((gates.SCOPE_PUBLIC, False), (gates.SCOPE_PRIVATE, True))},
                   acting_tier=policy.acting_tier(), registry_t1=gates.registry_allows_t1())
        return out
    token = st.acquire_scan_lock(now=now)
    if token is None:
        out["reason"] = "scan_running"
        return out
    try:
        scan_id = secrets.token_hex(6)
        st.append_event("scan_started", scan_id=scan_id, trigger=trigger, ts=now)
        ctx = load_context(read, now=now)
        res = scan_mod.scan(read, ctx, now=now, sleep=sleep)
        acting = policy.acting_tier()
        reg_t1 = gates.registry_allows_t1()
        scopes = gates.granted_scopes(read) if (acting == "T1" and reg_t1) else None
        for row in res["rows"]:
            row["tier"] = gates.stage_tier(bool(row.get("is_private")), scopes=scopes,
                                           registry_t1=reg_t1, acting=acting)
        pid = st.mint_proposal_id()
        tier_at_stage = "T1" if any(r.get("tier") == "T1" for r in res["rows"]) else "T0"
        out.update(proposal_id=pid, scanned=res["scanned"], blind=res["blind"],
                   counts=res["counts"], candidate_ids=list(res["candidate_ids"]),
                   rows=len(res["rows"]))
        if not st.append_event(
                "staged", proposal_id=pid, scan_id=scan_id, trigger=trigger, ts=now,
                expires_ts=now + st.EXPIRY_DAYS * st.DAY_S, tier_at_stage=tier_at_stage,
                blind=res["blind"], blind_detail=res.get("blind_detail") or "",
                scanned=res["scanned"], counts=res["counts"], rows=res["rows"],
                registry_count=ctx.registry.count if ctx.registry.ok else 0):
            out["reason"] = "store_write_failed"
            return out
        buttons = confirm_cards.confirm_buttons_enabled()
        f = st.fold(now=now)
        write = clients.write_client()
        try:
            opened = write.conversations_open(users=[HARRISON_ID])
            dm = str((opened.get("channel") or {}).get("id") or "")
        except Exception as exc:  # noqa: BLE001
            dm = ""
            out["reason"] = f"dm_open_failed:{_code(exc)}"
        if not dm:
            out["reason"] = out["reason"] or "dm_open_failed"
            st.append_event("delivery_failed", proposal_id=pid, page=1, error=out["reason"], ts=now)
            return out
        for page, cids in enumerate(cards.paginate(res["rows"]), start=1):
            blocks, text = cards.render_page(f, pid, page, now=now, buttons=buttons, page_cids=cids)
            try:
                resp = write.chat_postMessage(channel=dm, text=text, blocks=blocks,
                                              unfurl_links=False, unfurl_media=False)
                ts = str(resp.get("ts") or "")
            except Exception as exc:  # noqa: BLE001
                out["reason"] = f"post_failed:{_code(exc)}"
                st.append_event("delivery_failed", proposal_id=pid, page=page,
                                error=out["reason"], ts=now)
                break
            if not ts:
                out["reason"] = "post_failed:no_ts"
                st.append_event("delivery_failed", proposal_id=pid, page=page, error="no_ts", ts=now)
                break
            st.append_event("delivered", proposal_id=pid, page=page, dm_channel=dm,
                            message_ts=ts, rendered_cids=cids, buttons=buttons, ts=now)
            out["pages"] += 1
        out["delivered"] = out["pages"] > 0
        if out["delivered"] and not out["reason"]:
            out["reason"] = "delivered"
        log.info("channel_archive deliver trigger=%s proposal=%s pages=%d blind=%s a=%d rows=%d",
                 trigger, pid, out["pages"], res["blind"], len(res["candidate_ids"]),
                 len(res["rows"]))
        return out
    finally:
        st.release_scan_lock(token)


def _code(exc: BaseException) -> str:
    resp = getattr(exc, "response", None)
    try:
        code = resp.get("error") if resp is not None else None
    except Exception:  # noqa: BLE001
        code = None
    return str(code) if code else type(exc).__name__
