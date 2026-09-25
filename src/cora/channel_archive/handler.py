"""Tap processing for the dead-channel proposal card (Code #16 C1).

The app.py wrappers are Slack I/O only (post the outcome in the card's thread, then
re-render the page from the store); every correctness promise lives HERE:

  1. AUTHORITY FIRST: only Harrison may act (f3e's order -- a stranger's tap cannot
     probe which proposals exist).
  2. The proposal must exist, be neither superseded nor expired (A14), and the row
     must be in its staged set in the right section for the button (server-side).
  3. Every decision is check-then-write inside ONE lock acquisition, keyed by the
     CHANNEL across proposals (store.decide_row / claim_row, A13).
  4. The tier gate (gates.tap_gate, A12): a T0 row -- or a T1 row whose gate says
     RECORD -- records Harrison's agreement and archives nothing; a TRANSIENT refusal
     writes nothing and the buttons stay.
  5. THE ARCHIVE PATH (a T1 row whose gate says ARCHIVE):
       claim -> live re-verify with the ONE classifier on a fresh context (A5) ->
       tier re-check -> ledger INTENT (fsynced; a failed write refuses) -> no-network
       pre-notice re-check -> the one-line NOTICE in the channel -> conversations.archive
       on a no-retry client -> a conversations.info READ-BACK -> ledger outcome +
       store event.
     "Archived" is said ONLY after archive returned ok, the read-back showed the
     channel archived, and the intent row had landed. A timeout gets ONE read-back
     (A16); an indeterminate result is UNKNOWN (locked; the nightly monitor
     reconciles), never retried.
  6. Archive-all walks only the rendered, undecided section-A rows of THAT message,
     sequentially, and a SYSTEMIC Slack error aborts the loop at its first occurrence
     so the rest get no notice (A17).

``conversations_archive`` is called from exactly one function in the whole codebase,
``_archive_one`` below (an AST pin enforces it).
"""
from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import cards, clients, gates, policy
from . import classify as cl
from . import registry as reg
from . import scan as scan_mod
from . import store as st

log = logging.getLogger(__name__)

HARRISON_ID = os.environ.get("HARRISON_SLACK_USER_ID", "U0B2RM2JYJ1")

#: A17: the first of these aborts an Archive-all; the remaining rows get no notice.
SYSTEMIC_CODES: frozenset = frozenset({
    "restricted_action", "missing_scope", "not_allowed_token_type", "invalid_auth",
    "not_authed", "account_inactive", "token_revoked", "ratelimited",
    "team_access_not_granted",
})
RERENDER_EVERY = 5

_PID_RE = re.compile(r"\Achanarch-[0-9a-f]{12}\Z")
_CID_RE = re.compile(r"\AC[A-Z0-9]{8,24}\Z")
_PAGE_RE = re.compile(r"\Ap([1-9][0-9]?)\Z")

# ── reply copy (read by Harrison, never a model; no completion grammar) ──────
MSG_NOT_AUTHORIZED = "Only Harrison can act on this card."
MSG_ORPHANED = ("I don't have a record of that proposal anymore — ask 'archive the dead "
                "channels' for a fresh scan.")
MSG_ALREADY = "That channel was already decided on this card."
MSG_IN_PROGRESS = "That channel is in progress — the outcome will post in this thread."
MSG_STORE = "I couldn't read the proposal store, so nothing was recorded or archived."
RACE_OUTCOMES: frozenset = frozenset({"not_authorized", "orphaned", "already_handled",
                                      "in_progress", "store_error"})


@dataclass
class TapResult:
    outcome: str
    msg: str
    proposal_id: str = ""
    page: int | None = None
    rerender: bool = True
    counts: dict = field(default_factory=dict)

    @property
    def ephemeral(self) -> bool:
        return self.outcome in RACE_OUTCOMES


def _split_tier(target: str) -> tuple[str, str]:
    """(target, the tier the button's LABEL showed). An unmarked value is T0: it can
    only ever record (A12 -- fail safe for any value not drawn as Archive)."""
    for tier in ("T1", "T0"):
        if target.endswith(":" + tier):
            return target[:-3], tier
    return target, "T0"


def parse_value(action: str, value: str) -> tuple[str, str]:
    """(proposal_id, target) -- target is a channel id, "p<N>" for Archive-all, or ""
    for the card-level button. ("", "") when the value is malformed. A trailing
    ``:T1`` / ``:T0`` label marker is accepted and stripped (see ``button_tier``)."""
    v = str(value or "").strip()
    if action == cards.ACTION_AGREED:
        return (v, "") if _PID_RE.match(v) else ("", "")
    pid, _, target = v.partition(":")
    if not _PID_RE.match(pid):
        return "", ""
    target, _tier = _split_tier(target)
    if action == cards.ACTION_ALL:
        return (pid, target) if _PAGE_RE.match(target) else ("", "")
    return (pid, target) if _CID_RE.match(target) else ("", "")


def button_tier(action: str, value: str) -> str:
    """"T1" only for an Archive-labelled button (its value ends ``:T1``); every other
    value -- "Mark to archive", Keep, the card-level button, an unmarked or legacy
    value -- is "T0" and records at most. The handler archives ONLY from "T1"."""
    if action not in (cards.ACTION_ROW, cards.ACTION_OVERRIDE, cards.ACTION_ALL):
        return "T0"
    return _split_tier(str(value or "").strip())[1]


def needs_act_pool(action: str, value: str, *, now: float | None = None) -> bool:
    """True when the tap would run the network archive path (an Archive-labelled
    button on a T1 row, or an Archive-all over a page with T1 rows) -- the app wrapper
    then runs it on the lane's own 1-worker pool, never on Bolt's shared listener
    workers (A18). A Mark-labelled button only records: inline."""
    if action not in (cards.ACTION_ROW, cards.ACTION_OVERRIDE, cards.ACTION_ALL):
        return False
    if button_tier(action, value) != "T1":
        return False
    pid, target = parse_value(action, value)
    if not pid:
        return False
    try:
        f = st.fold(now=now)
        p = f.proposals.get(pid)
        if p is None:
            return False
        if action == cards.ACTION_ALL:
            page = int(target[1:])
            rows = p.rows_by_cid
            return any((rows.get(c) or {}).get("tier") == "T1"
                       for c in cards.undecided_a_on_page(p, page))
        return (p.rows_by_cid.get(target) or {}).get("tier") == "T1"
    except Exception:  # noqa: BLE001 -- when unsure, the pool is the safe place
        return True


def _label(row: dict) -> str:
    return f"<#{row.get('cid', '')}>"


def process_tap(action: str, value: str, actor_id: str, *, now: float | None = None,
                sleep: Callable[[float], None] | None = None,
                progress: Callable[[str, int], None] | None = None) -> TapResult:
    """One button press. Never raises. ``progress(proposal_id, page)`` is called every
    RERENDER_EVERY rows of an Archive-all so the card can re-render mid-loop."""
    now = time.time() if now is None else float(now)
    try:
        return _process(action, value, actor_id, now=now, sleep=sleep, progress=progress)
    except Exception:  # noqa: BLE001 -- a handler bug must never crash the bot
        log.exception("channel_archive tap crashed action=%s", action)
        return TapResult("error", "Something went wrong handling that tap — nothing further "
                                  "was done. Check the card thread before tapping again.")


def _process(action: str, value: str, actor_id: str, *, now: float,
             sleep: Callable[[float], None] | None,
             progress: Callable[[str, int], None] | None) -> TapResult:
    if not actor_id or actor_id != HARRISON_ID:
        return TapResult("not_authorized", MSG_NOT_AUTHORIZED, rerender=False)
    pid, target = parse_value(action, value)
    if not pid:
        return TapResult("orphaned", MSG_ORPHANED, rerender=False)
    btier = button_tier(action, value)
    f = st.fold(now=now)
    if not f.ok:
        return TapResult("store_error", MSG_STORE, pid, rerender=False)
    p = f.proposals.get(pid)
    if p is None:
        return TapResult("orphaned", MSG_ORPHANED, pid, rerender=False)
    if policy.is_demoted():
        st.note_demoted(p, now=now)      # A12: this card outlived a demotion -- for good
    page = _page_of(p, target) if action != cards.ACTION_ALL else int(target[1:])
    sup = f.superseded_by(pid)
    if sup is not None:
        return TapResult("superseded", (f"This card was superseded by the "
                                        f"{cards._date(sup.created)} card — nothing was done. "
                                        "Use the newer card."), pid, page)
    if now >= p.expires:
        return TapResult("expired", (f"This card expired {cards._date(p.expires)} — nothing was "
                                     "done. Ask 'archive the dead channels' for a fresh scan."),
                         pid, page)
    if action == cards.ACTION_AGREED:
        return _card_agreed(p, actor_id, now)
    if action == cards.ACTION_ALL:
        return _archive_all(f, p, page, actor_id, now=now, sleep=sleep, progress=progress,
                            btier=btier)
    row = p.rows_by_cid.get(target)
    if row is None:
        return TapResult("orphaned", MSG_ORPHANED, pid, rerender=False)
    section = row.get("section")
    if action == cards.ACTION_ROW and section != cl.SECTION_A:
        return TapResult("orphaned", MSG_ORPHANED, pid, rerender=False)
    if action == cards.ACTION_OVERRIDE and section != cl.SECTION_B:
        return TapResult("orphaned", MSG_ORPHANED, pid, rerender=False)
    if action == cards.ACTION_KEEP:
        return _keep(p, row, actor_id, now, page)
    res = _decide_or_archive(p, row, actor_id, now=now, sleep=sleep,
                             override=section == cl.SECTION_B, btier=btier)
    res.page = page
    return res


def _page_of(p: st.Proposal, cid: str) -> int | None:
    for n, info in sorted(p.pages.items()):
        if cid in (info.get("rendered_cids") or []):
            return n
    return None


def _refusal(why: str, pid: str) -> TapResult:
    if why == "in_progress":
        return TapResult("in_progress", MSG_IN_PROGRESS, pid, rerender=False)
    if why == "already_handled":
        return TapResult("already_handled", MSG_ALREADY, pid, rerender=False)
    if why == "orphaned":
        return TapResult("orphaned", MSG_ORPHANED, pid, rerender=False)
    if why == "store_unreadable":
        return TapResult("store_error", MSG_STORE, pid, rerender=False)
    return TapResult("error", "I couldn't record that just now — nothing was archived. The "
                              "buttons stay; tap again.", pid)


def _card_agreed(p: st.Proposal, actor: str, now: float) -> TapResult:
    with st.CLAIM_LOCK:
        f = st.fold(now=now)
        cur = f.proposals.get(p.proposal_id)
        if cur is not None and cur.card_agreed_by:
            return TapResult("noop", "Already recorded: this list matches your read.",
                             p.proposal_id, 1)
        if not st.append_event("card_agreed", proposal_id=p.proposal_id, by=actor, ts=now):
            return _refusal("store_write_failed", p.proposal_id)
    log.info("channel_archive card_agreed proposal=%s", p.proposal_id)
    return TapResult("card_agreed", ("Recorded: this list matches your read — the T1 promotion "
                                     "evidence. Nothing was archived; promotion is a registry "
                                     "change plus a restart, never a tap."), p.proposal_id, 1)


def _keep(p: st.Proposal, row: dict, actor: str, now: float, page: int | None) -> TapResult:
    cid = row["cid"]

    def _check(f: st.Fold, _p: st.Proposal) -> str | None:
        ks = f.keep_state(now).get(cid) or {}
        if float(ks.get("until") or 0) > now:
            return "keep_window"
        if int(ks.get("count") or 0) >= st.KEEP_CAP:
            return "keep_capped"
        return None
    ok, why, f = st.decide_row(p.proposal_id, cid, st.KEPT, actor=actor, now=now, check=_check)
    if not ok:
        if why == "keep_window" and f is not None:
            until = (f.keep_state(now).get(cid) or {}).get("until")
            return TapResult("noop", f"{_label(row)} is already kept until {cards._date(until)}.",
                             p.proposal_id, page)
        if why == "keep_capped":
            return TapResult("noop", (f"{_label(row)} has been kept {st.KEEP_CAP} times — mark it "
                                      "to archive, or add it to the channel registry."),
                             p.proposal_id, page)
        return _refusal(why, p.proposal_id)
    n = int((st.fold(now=now).keep_state(now).get(cid) or {}).get("count") or 1)
    log.info("channel_archive keep proposal=%s cid=%s count=%d", p.proposal_id, cid, n)
    return TapResult("kept", (f"Kept {_label(row)} — it won't be proposed again for 90 days "
                              f"(kept x{n})."), p.proposal_id, page)


def _record(p: st.Proposal, row: dict, actor: str, now: float, *, override: bool,
            why: str) -> TapResult:
    ok, refused, _f = st.decide_row(p.proposal_id, row["cid"], st.AGREED, actor=actor, now=now,
                                    override=override)
    if not ok:
        return _refusal(refused, p.proposal_id)
    ov = f" (override of its {row.get('reason') or 'exemption'} exemption)" if override else ""
    log.info("channel_archive agreed proposal=%s cid=%s override=%s", p.proposal_id, row["cid"],
             override)
    return TapResult("agreed", (f"Marked {_label(row)} to archive{ov} — recorded as T1 promotion "
                                f"evidence. Nothing was archived: {why}."), p.proposal_id)


def _decide_or_archive(p: st.Proposal, row: dict, actor: str, *, now: float,
                       sleep: Callable[[float], None] | None, override: bool,
                       read: Any = None, write: Any = None, btier: str = "T0") -> TapResult:
    if row.get("tier") != "T1":
        return _record(p, row, actor, now, override=override, why=gates.t0_reason())
    if btier != "T1":
        # A12: the label on screen was "Mark to archive" -- it only ever records
        return _record(p, row, actor, now, override=override,
                       why=gates.DEMOTED_AFTER_CARD if p.demoted else gates.MARK_BUTTON)
    try:
        read = read if read is not None else clients.read_client()
    except Exception as exc:  # noqa: BLE001
        return TapResult("refused_transient", (f"Nothing was archived — I couldn't open a Slack "
                                               f"client ({type(exc).__name__}). The buttons stay."),
                         p.proposal_id)
    gate, why = gates.tap_gate(row, read, demoted_after_card=p.demoted)
    if gate == gates.RECORD:
        return _record(p, row, actor, now, override=override, why=why)
    if gate == gates.TRANSIENT:
        return TapResult("refused_transient", (f"Nothing was archived — {why}. The buttons stay; "
                                               "tap again once that's fixed."), p.proposal_id)
    return _archive_one(p, row, actor, now=now, sleep=sleep, override=override, read=read,
                        write=write)[0]


# ── the archive path ─────────────────────────────────────────────────────────
def _reverify(p: st.Proposal, row: dict, read: Any, *, now: float,
              sleep: Callable[[float], None] | None) -> tuple[str, str, cl.Verdict | None]:
    """("ok"|"stale"|"retry", reason, fresh verdict) -- the ONE classifier on a FRESH
    context (A5). The fresh verdict's age is what the notice and the ledger carry."""
    from . import deliver  # noqa: PLC0415 -- lazy: deliver imports this module's peers
    cid = row["cid"]
    ctx = deliver.load_context(read, now=now)
    blind = ctx.blind_cause()
    if blind:
        return "retry", f"reverify_{blind}", None
    try:
        info = read.conversations_info(channel=cid, include_num_members=True)
        meta = scan_mod.project_channel(info.get("channel") or {})
    except Exception as exc:  # noqa: BLE001
        return "retry", f"reverify_{cl._err_code(exc)}", None
    if not meta.get("id"):
        return "retry", "reverify_shape", None
    if reg.name_fp(str(meta.get("name") or "")) != row.get("name_fp"):
        return "stale", "it was renamed since the card", None
    if meta.get("is_archived"):
        return "stale", "it is already archived", None
    v = cl.classify_channel(read, meta, ctx, now=now, sleep=sleep)
    if v.kind == cl.UNKNOWN:
        return "retry", f"reverify_{v.reason}", v
    if v.kind == cl.ACTIVE:
        return "stale", "a person posted since the card", v
    if v.kind == cl.EXEMPT:
        if v.reason == cl.X_KEPT:
            return "stale", "it was kept since the card", v
        return "stale", f"it is exempt now ({v.reason})", v
    if row.get("section") == cl.SECTION_A and v.kind != cl.SECTION_A:
        return "stale", f"it is no longer a clean candidate ({v.reason})", v
    if row.get("section") == cl.SECTION_B and (v.kind != cl.SECTION_B or v.reason != row.get("reason")):
        return "stale", "its class changed since the card", v
    return "ok", "", v


def _archiver_is_cora(read: Any, cid: str, bot_uid: str, bot_id: str) -> bool | None:
    """Read the newest channel_archive/group_archive message: True if Cora archived it."""
    try:
        resp = read.conversations_history(channel=cid, limit=20)
        for m in resp.get("messages") or []:
            if not isinstance(m, dict):
                continue
            if m.get("subtype") in ("channel_archive", "group_archive"):
                return (bool(bot_uid) and m.get("user") == bot_uid) or \
                       (bool(bot_id) and m.get("bot_id") == bot_id)
    except Exception:  # noqa: BLE001
        return None
    return None


def _readback_archived(read: Any, cid: str) -> bool | None:
    try:
        info = read.conversations_info(channel=cid)
        ch = info.get("channel") or {}
        val = ch.get("is_archived") if isinstance(ch, dict) else None
        return bool(val) if isinstance(val, bool) else None
    except Exception:  # noqa: BLE001
        return None


def _finish(p: st.Proposal, row: dict, actor: str, now: float, outcome: str, *,
            store_event: str, code: str = "", release: bool = False) -> None:
    """Ledger outcome (fail-soft) + store event. The store still tells the truth if
    the ledger append fails, and the fold folds a ledger outcome if the store fails."""
    if not st.append_ledger("outcome", proposal_id=p.proposal_id, channel_id=row["cid"],
                            outcome=outcome, tapped_by=actor, ts=time.time()):
        log.error("channel_archive: ledger OUTCOME append failed proposal=%s cid=%s outcome=%s",
                  p.proposal_id, row["cid"], outcome)
    st.append_event("released" if release else store_event, proposal_id=p.proposal_id,
                    cid=row["cid"], by=actor, code=code or None, ts=time.time())


def _correction(write: Any, cid: str) -> None:
    try:
        write.chat_postMessage(channel=cid, text=cards.CORRECTION_TEXT,
                               unfurl_links=False, unfurl_media=False)
    except Exception:  # noqa: BLE001 -- best effort; the card says it did not go through
        log.warning("channel_archive: correction line failed cid=%s", cid)


def _archive_one(p: st.Proposal, row: dict, actor: str, *, now: float,
                 sleep: Callable[[float], None] | None, override: bool,
                 read: Any, write: Any = None) -> tuple[TapResult, str | None]:
    """The ONLY caller of conversations_archive. Returns (result, systemic_code|None)."""
    cid = row["cid"]
    pid = p.proposal_id
    ok, why, _f = st.claim_row(pid, cid, "archive", actor=actor, now=now)
    if not ok:
        return _refusal(why, pid), None
    status, reason, fresh = _reverify(p, row, read, now=now, sleep=sleep)
    if status == "stale":
        st.append_event(st.STALE, proposal_id=pid, cid=cid, by=actor, code=reason, ts=time.time())
        return TapResult("stale_refused", f"Not archived: {_label(row)} — {reason}.", pid), None
    if status == "retry":
        st.append_event("released", proposal_id=pid, cid=cid, code=reason, ts=time.time())
        return TapResult("failed", (f"Not archived: I couldn't re-check {_label(row)} just now "
                                    f"({reason}). The buttons stay; tap again."), pid), None
    gate, gwhy = gates.tap_gate(row, read, demoted_after_card=p.demoted)
    if gate != gates.ARCHIVE:
        st.append_event("released", proposal_id=pid, cid=cid, code="gate", ts=time.time())
        return TapResult("refused_transient", f"Nothing was archived — {gwhy}.", pid), None
    # the age as of THIS tap (the card's figure can be up to 14 days old)
    age = fresh.last_person_days if fresh is not None else row.get("last_person_days")
    if not st.append_ledger("intent", proposal_id=pid, channel_id=cid,
                            channel_name=None if row.get("lex") else row.get("name"),
                            lex=True if row.get("lex") else None, age_days=age,
                            tapped_by=actor, override=bool(override), ts=time.time()):
        st.append_event("released", proposal_id=pid, cid=cid, code="ledger_write_failed",
                        ts=time.time())
        return TapResult("failed", ("Nothing was done: the archive ledger write failed, and an "
                                    "archive the ledger can't show never happens. The buttons "
                                    "stay."), pid), None
    ok2, why2 = gates.pre_notice_check()
    if not ok2:
        _finish(p, row, actor, now, f"not_attempted:{why2}", store_event="released", release=True)
        return TapResult("refused_transient", f"Nothing was archived — {why2}.", pid), None
    try:
        write = write if write is not None else clients.write_client()
    except Exception as exc:  # noqa: BLE001
        _finish(p, row, actor, now, "not_attempted:no_write_client", store_event="released",
                release=True)
        return TapResult("failed", f"Nothing was archived — no Slack write client ({type(exc).__name__}).",
                         pid), None
    # (1) the notice goes FIRST (kickoff section 3)
    try:
        write.chat_postMessage(channel=cid, text=cards.notice_text(age, actor),
                               unfurl_links=False, unfurl_media=False)
    except Exception as exc:  # noqa: BLE001
        code = cl._err_code(exc)
        _finish(p, row, actor, now, f"notice_failed:{code}", store_event=st.FAILED,
                code=f"notice_failed:{code}")
        return (TapResult("failed", (f"Not archived: the notice in {_label(row)} did not post "
                                     f"({code}), so I stopped there. The Archive button stays."),
                          pid), code if code in SYSTEMIC_CODES else None)
    # (2) the archive, on a client with NO retry handlers
    bot_uid, bot_id = _identity(read)
    try:
        write.conversations_archive(channel=cid)
        archived = _readback_archived(read, cid)
        if archived is True:
            outcome = "archived"
        else:
            outcome = "unknown"
    except Exception as exc:  # noqa: BLE001
        code = cl._err_code(exc)
        if getattr(exc, "response", None) is not None and code == "already_archived":
            mine = _archiver_is_cora(read, cid, bot_uid, bot_id)
            outcome = "archived" if mine else "already_archived"
        elif getattr(exc, "response", None) is not None:
            _correction(write, cid)
            _finish(p, row, actor, now, f"failed:{code}", store_event=st.FAILED, code=code)
            return (TapResult("failed", (f"Not archived: Slack refused ({code}). I posted a "
                                         f"correction in {_label(row)} — the channel stays open. "
                                         "The Archive button stays."), pid),
                    code if code in SYSTEMIC_CODES else None)
        else:
            # A16: a timeout / dropped connection -> ONE bounded read-back, never a retry
            archived = _readback_archived(read, cid)
            if archived is True:
                outcome = "archived"
            elif archived is False:
                _correction(write, cid)
                _finish(p, row, actor, now, "failed:timeout_not_archived", store_event=st.FAILED,
                        code="timeout_not_archived")
                return (TapResult("failed", (f"Not archived: the request to Slack timed out and a "
                                             f"read-back shows {_label(row)} still open. I posted "
                                             "a correction there. The Archive button stays."),
                                  pid), None)
            else:
                outcome = "unknown"
    if outcome == "archived":
        _finish(p, row, actor, now, "archived", store_event=st.ARCHIVED)
        log.info("channel_archive ARCHIVED proposal=%s cid=%s", pid, cid)
        agetxt = f"{age} days since a person posted" if age else "no person post in 90+ days"
        return TapResult("archived", (f"Archived {_label(row)} — {agetxt}. The notice went in "
                                      "first; ledger row written. Reversible from the channel "
                                      "settings."), pid), None
    if outcome == "already_archived":
        _finish(p, row, actor, now, "already_archived", store_event=st.ALREADY_ARCHIVED)
        return TapResult("already_archived", (f"{_label(row)} was already archived by someone "
                                              "else — nothing done here."), pid), None
    _finish(p, row, actor, now, "unknown", store_event=st.UNKNOWN)
    log.warning("channel_archive UNKNOWN outcome proposal=%s cid=%s", pid, cid)
    return TapResult("unknown", (f"Outcome unknown for {_label(row)}: Slack did not confirm the "
                                 "archive. I won't retry — the nightly monitor reconciles "
                                 "against Slack."), pid), None


def _identity(read: Any) -> tuple[str, str]:
    from . import deliver  # noqa: PLC0415
    return deliver.bot_identity(read)


def _archive_all(f: st.Fold, p: st.Proposal, page: int, actor: str, *, now: float,
                 sleep: Callable[[float], None] | None,
                 progress: Callable[[str, int], None] | None, btier: str = "T0") -> TapResult:
    targets = cards.undecided_a_on_page(p, page)
    if not targets:
        return TapResult("noop", "Nothing left to act on in this part of the card.",
                         p.proposal_id, page)
    rows = p.rows_by_cid
    counts: dict[str, int] = {}
    any_t1 = any((rows.get(c) or {}).get("tier") == "T1" for c in targets)
    # A12: only an Archive-labelled "Archive all" may archive; "Mark all ... to archive"
    # records every row, whatever the rows' staged tier says -- and so does any button
    # on a card that outlived a demotion
    t1 = any_t1 and btier == "T1" and not p.demoted
    t0_why = ((gates.DEMOTED_AFTER_CARD if p.demoted else gates.MARK_BUTTON) if any_t1
              else gates.t0_reason())
    read = None
    if t1:
        try:
            read = clients.read_client()
        except Exception as exc:  # noqa: BLE001
            return TapResult("refused_transient", (f"Nothing was archived — I couldn't open a "
                                                   f"Slack client ({type(exc).__name__}). The "
                                                   "buttons stay."), p.proposal_id, page)
    aborted = ""
    crashed = ""
    done = 0
    for i, cid in enumerate(targets):
        row = rows[cid]
        try:
            if not t1:
                res, systemic = _record(p, row, actor, now, override=False, why=t0_why), None
            elif row.get("tier") == "T1":
                gate, why = gates.tap_gate(row, read, demoted_after_card=p.demoted)
                if gate == gates.ARCHIVE:
                    res, systemic = _archive_one(p, row, actor, now=time.time(), sleep=sleep,
                                                 override=False, read=read)
                elif gate == gates.RECORD:
                    res, systemic = _record(p, row, actor, now, override=False, why=why), None
                else:
                    res, systemic = TapResult("refused_transient", why, p.proposal_id), None
            else:
                res, systemic = _record(p, row, actor, now, override=False,
                                        why=gates.t0_reason()), None
        except Exception as exc:  # noqa: BLE001 -- A18: say how far it got, never go silent
            log.exception("channel_archive archive-all crashed at row %d of %d", i + 1, len(targets))
            crashed = type(exc).__name__
            break
        counts[res.outcome] = counts.get(res.outcome, 0) + 1
        done = i + 1
        if systemic:
            aborted = systemic
            break
        if progress is not None and done % RERENDER_EVERY == 0 and done < len(targets):
            try:
                progress(p.proposal_id, page)
            except Exception:  # noqa: BLE001
                log.warning("channel_archive archive-all progress render failed", exc_info=True)
    parts = [f"{k} {v}" for k, v in sorted(counts.items())] or ["none"]
    if crashed:
        msg = (f"Stopped after {done} of {len(targets)} ({crashed}) — the rest were not attempted. "
               "Outcomes so far: " + ", ".join(parts) + ". Check each row before tapping again.")
        return TapResult("archive_all", msg, p.proposal_id, page, counts=counts)
    if not t1:
        msg = (f"Marked {counts.get('agreed', 0)} of {len(targets)} shown to archive — recorded as "
               f"T1 promotion evidence. Nothing was archived: {t0_why}.")
    elif aborted:
        msg = (f"Stopped at the first systemic Slack error ({aborted}): {len(targets) - done} of "
               f"{len(targets)} not attempted (no notice posted to them). Outcomes: "
               + ", ".join(parts) + ".")
    else:
        msg = f"Archive-all finished for this part: " + ", ".join(parts) + "."
    log.info("channel_archive archive-all proposal=%s page=%d targets=%d %s aborted=%s",
             p.proposal_id, page, len(targets), counts, aborted or "-")
    return TapResult("archive_all", msg, p.proposal_id, page, counts=counts)
