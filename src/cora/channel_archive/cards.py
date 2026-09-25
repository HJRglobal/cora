"""The dead-channel proposal card (Code #16 C1): pages, blocks, budgets, re-render,
and every string the lane shows a human.

EVERY PAGE IS RE-DERIVED FROM THE STORE. The first post and every re-render call
the same ``render_page(fold, proposal_id, page)``, so a tap can never leave a row
showing a state the store does not hold, and the ``text=`` fallback -- which the
history readers hand the model as Cora's own prior words -- is a code-built count
line of store truth, never a row name (A22).

COPY RULES (kickoff section 5, D-051 honesty): no completion grammar (Done / All set /
staged / queued / created / updated / filed / deleted); past-tense "Archived" only on
a row whose store state is ``archived`` -- which the tap handler writes only after
conversations.archive returned ok, the read-back showed the channel archived, and
the ledger intent row had landed. The T0 card says nothing will be archived. Both
tiers carry the reversibility line (A33). Blocks bypass the egress sanitizer
(slack_egress.py docstring), so every string is sanitized HERE.

BUDGET BEFORE RENDERING: at most ROWS_PER_PAGE rows (two blocks each) per message,
48 blocks per message; rows past a page go onto continuation messages of the SAME
proposal, each with its own buttons and its own Archive-all that covers only ITS
rendered, undecided section-A rows -- never a bare "+k more" (A1).
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from .. import slack_egress
from . import classify as cl
from . import policy
from . import store as st

ACTION_ROW = "cora_channel_archive_row"            # Mark to archive (T0) / Archive (T1)
ACTION_ALL = "cora_channel_archive_all"            # Mark all shown / Archive all shown
ACTION_KEEP = "cora_channel_archive_keep"
ACTION_OVERRIDE = "cora_channel_archive_override"  # section-B rows, per-row only
ACTION_AGREED = "cora_channel_archive_card_agreed" # "This list matches my read" (A31)
ACTIONS: tuple = (ACTION_ROW, ACTION_ALL, ACTION_KEEP, ACTION_OVERRIDE, ACTION_AGREED)

BLOCK_BUDGET = 48
ROWS_PER_PAGE = 20
_AZ = timezone(timedelta(hours=-7))
_BTN_MAX = 75

# ── fixed copy ───────────────────────────────────────────────────────────────
HEADER_T0 = ("*Dead-channel proposal — lane at T0: nothing will be archived.* Tapping "
             "*Mark to archive* records your agreement (the T1 promotion evidence); "
             "archiving needs the lane promoted first.")
HEADER_T1 = ("*Dead-channel proposal.* Tapping *Archive* posts a one-line notice in the "
             "channel, then archives it.")
REVERSIBLE = ("Slack archives are reversible: anyone in the channel can unarchive it from "
              "the channel settings.")
BUTTONS_ONLY = "Buttons are the only way to act — typing 'yes' does nothing."
BUTTONS_OFF = ("My buttons are switched off, so nothing can be recorded or archived from "
               "this card.")
AGREED_LINE = "_You confirmed this list matches your read (T1 promotion evidence)._"
BLIND_CAUSES = {
    "registry_unreadable": "the channel registry could not be read completely",
    "policy_unreadable": "the sweep deny-list could not be read",
    "list_incomplete": "Slack's channel list could not be read to its end",
    "bot_id_unknown": "my own Slack user id could not be confirmed",
    "store_unreadable": "my own proposal store or archive ledger could not be read",
}
EXEMPT_LABELS = (
    (cl.X_PINNED, "pinned"), (cl.X_LEAD_FIN, "finance/leadership"),
    (cl.X_YOUNG, "new (<30 days)"), (cl.X_SHARED, "Slack Connect"),
    (cl.X_GENERAL, "general"), (cl.X_INFO, "#info-for-cora"),
    (cl.X_KEPT, "kept (90-day window)"), (cl.X_UNARCHIVE_SUPPRESSED, "recently unarchived"),
    (cl.X_DENIED, "deny-listed (not named)"), (cl.X_ARCHIVED, "archived"),
)
B_LINES = {
    cl.B_LEX: "Exempt: LEX (custodian-owned). Tap to override for this channel only.",
    cl.B_REGISTRY: "Exempt: in the channel registry. Tap to override for this channel only.",
    cl.B_KEEP_LIST: "Exempt: on the sprawl keep-list. Tap to override for this channel only.",
    cl.B_PRIVATE_NOT_MEMBER: ("Private, and you are not a member — its name may not show "
                              "for you. Per-row only."),
    cl.B_THREADS_UNCHECKED: ("Older threads not checked — history longer than 2,000 "
                             "messages. Per-row only."),
}


def _clean(text: str) -> str:
    return slack_egress.sanitize_text(text)


def _esc(text: str) -> str:
    return str(text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _date(ts: float | None) -> str:
    if not ts:
        return "?"
    dt = datetime.fromtimestamp(float(ts), _AZ)
    return f"{dt:%b} {dt.day}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def effective_tier(row: dict) -> str:
    """A T1 row acts T0 while the lane is demoted (A12: a demotion newer than the
    card re-renders every T1 row T0-equivalent)."""
    if row.get("tier") != "T1":
        return "T0"
    return "T0" if policy.is_demoted() else "T1"


def card_tier(p: st.Proposal) -> str:
    return "T1" if any(effective_tier(r) == "T1" for r in p.rows) else "T0"


def paginate(rows: list[dict]) -> list[list[str]]:
    """Page -> the cids it renders, A rows first then B, ROWS_PER_PAGE per page."""
    cids = [r["cid"] for r in rows if r.get("cid")]
    return [cids[i:i + ROWS_PER_PAGE] for i in range(0, len(cids), ROWS_PER_PAGE)] or [[]]


# ── row text ─────────────────────────────────────────────────────────────────
def _name_part(row: dict) -> str:
    cid = row.get("cid", "")
    if row.get("lex"):
        return f"<#{cid}>  (LEX — name withheld)"
    return f"<#{cid}>  ({_esc(row.get('name') or '?')})"


def _fields_line(row: dict) -> str:
    bits: list[str] = []
    lp = row.get("last_person_days")
    if lp is not None:
        bits.append(f"{lp} days since a person posted")
    elif row.get("history_complete"):
        bits.append("no person has posted in its history")
    else:
        bits.append(f"no person post in the last {row.get('scanned_older') or 0} older messages")
    mc = row.get("member_count")
    bits.append(_plural(int(mc), "member") if isinstance(mc, int) else "? members")
    if not row.get("lex") and row.get("entity"):
        bits.append(str(row["entity"]))
    bits.append(_plural(int(row.get("pins") or 0), "pin"))
    bm = row.get("bookmarks")
    bits.append(_plural(int(bm), "bookmark") if isinstance(bm, int) else "bookmarks ?")
    if row.get("is_private"):
        bits.append("private")
    if row.get("canvas"):
        bits.append("canvas: yes")
    if row.get("tabs"):
        bits.append(_plural(int(row["tabs"]), "tab"))
    if row.get("created_days") is not None:
        bits.append(f"created {row['created_days']} days ago")
    if row.get("keep_count"):
        bits.append(f"kept x{row['keep_count']}")
    return " · ".join(bits)


def _b_line(row: dict) -> str:
    r = row.get("reason") or ""
    if r == cl.B_BOT_TRAFFIC:
        latest = row.get("bot_latest_days")
        tail = f", latest {latest} days ago" if latest is not None else ""
        return (f"Cora/app posts in the last 90 days: {row.get('bot_posts') or 0}{tail} — no "
                "person posted. Per-row only.")
    if r == cl.B_UNARCHIVED_BEFORE:
        by = row.get("unarchived_by") or ""
        who = f"<@{by}>" if by else "someone"
        return (f"Unarchived by {who} on {_date(row.get('unarchived_at'))} after an earlier "
                "archive. Per-row only.")
    return B_LINES.get(r, "Per-row only.")


def _decided_line(row: dict, state: dict, tier: str) -> str:
    s = state.get("state")
    by = state.get("by") or ""
    who = f"<@{by}>" if by else "you"
    if s == st.AGREED:
        ov = " (override)" if state.get("override") else ""
        return (f"_Marked to archive{ov} by {who} — recorded as T1 promotion evidence; "
                "nothing archived._")
    if s == st.KEPT:
        return f"_Kept by {who} — not proposed again for 90 days._"
    if s == st.ARCHIVED:
        tail = " (reconciled against Slack)" if state.get("reconciled") else ""
        return (f"_Archived {_date(state.get('ts'))}{tail} — tapped by {who}; ledger row "
                "written. Reversible from the channel settings._")
    if s == st.ALREADY_ARCHIVED:
        return "_Already archived by someone else — nothing done here._"
    if s == st.UNKNOWN:
        return "_Outcome unknown — the nightly monitor reconciles against Slack._"
    if s == st.STALE:
        why = state.get("code") or "it changed since the card"
        return f"_Not archived — {_esc(why)}._"
    return ""


def _open_line(row: dict, state: dict) -> str:
    s = state.get("state")
    if s == st.FAILED:
        code = state.get("code") or "error"
        return f"_Last attempt did not go through ({_esc(code)}) — the channel stays open; tap to retry._"
    if s == st.CLAIMED:
        return "_In progress…_"
    return ""


def _btn(text: str, action: str, value: str, style: str | None = None) -> dict:
    b: dict[str, Any] = {"type": "button", "action_id": action, "value": value[:2000],
                         "text": {"type": "plain_text", "text": text[:_BTN_MAX], "emoji": True}}
    if style:
        b["style"] = style
    return b


def _row_blocks(p: st.Proposal, row: dict, *, buttons: bool, actionable: bool) -> list[dict]:
    cid = row["cid"]
    state = p.row_state.get(cid) or {"state": st.OPEN}
    tier = effective_tier(row)
    lines = [_name_part(row), _fields_line(row)]
    if row.get("section") == cl.SECTION_B:
        lines.append(_b_line(row))
    decided = state.get("state") in st.TERMINAL
    if decided:
        lines.append(_decided_line(row, state, tier))
    else:
        extra = _open_line(row, state)
        if extra:
            lines.append(extra)
    keep_count = int(row.get("keep_count") or 0)
    capped = keep_count >= st.KEEP_CAP
    if capped and not decided:
        lines.append("KEPT x2 (capped) — " + ("archive it" if tier == "T1" else "mark it to archive")
                     + ", or add it to the channel registry.")
    blocks: list[dict] = [{"type": "section", "block_id": f"chanarch_row_{cid}"[:255],
                           "text": {"type": "mrkdwn", "text": _clean("\n".join(lines))}}]
    if decided or not buttons or not actionable:
        return blocks
    value = f"{p.proposal_id}:{cid}"
    elements: list[dict] = []
    if row.get("section") == cl.SECTION_B:
        exempt = row.get("reason") in cl.OVERRIDABLE_EXEMPTIONS
        if tier == "T1":
            label = "Archive (override)" if exempt else "Archive this one"
        else:
            label = "Mark to archive (override)" if exempt else "Mark to archive (this one)"
        elements.append(_btn(label, ACTION_OVERRIDE, value, "danger"))
    else:
        elements.append(_btn("Archive" if tier == "T1" else "Mark to archive", ACTION_ROW, value,
                             "danger" if tier == "T1" else "primary"))
    if not capped:
        elements.append(_btn("Keep", ACTION_KEEP, value))
    blocks.append({"type": "actions", "block_id": f"chanarch_act_{cid}"[:255], "elements": elements})
    return blocks


def _counts_line(p: st.Proposal) -> str:
    c = p.counts or {}
    ex = c.get("exempt") or {}
    parts = [f"{label} {ex[key]}" for key, label in EXEMPT_LABELS if ex.get(key)]
    parts.append(f"active {int(c.get('active') or 0)}")
    parts.append(f"could not be read {int(c.get('unreadable') or 0)} (not judged)")
    return "Not listed — " + " · ".join(parts)


def undecided_a(p: st.Proposal, cids: list[str]) -> list[str]:
    rows = p.rows_by_cid
    return [cid for cid in cids
            if (rows.get(cid) or {}).get("section") == cl.SECTION_A
            and p.state_of(cid) in (st.OPEN, st.FAILED)]


def undecided_a_on_page(p: st.Proposal, page: int) -> list[str]:
    """The cids an Archive-all on *page* acts on: RENDERED there (the page's
    `delivered` event), section A, and open or failed -- never section B, never a
    row on another page, never a decided row (A1)."""
    return undecided_a(p, list((p.pages.get(page) or {}).get("rendered_cids") or []))


def find_page(fold: st.Fold, message_ts: str) -> tuple[str, int] | None:
    """(proposal_id, page) of the card message posted at *message_ts*, else None."""
    ts = str(message_ts or "")
    if not ts:
        return None
    for pid in reversed(fold.order):
        for page, info in fold.proposals[pid].pages.items():
            if str(info.get("message_ts") or "") == ts:
                return pid, page
    return None


def fallback_text(p: st.Proposal) -> str:
    """The code-built text= of the card and of every re-render (A22): counts only."""
    if p.blind:
        return (f"Dead-channel scan could not complete ({p.blind}) — nothing proposed, "
                "nothing archived.")
    if not p.rows:
        return "Dead-channel scan: no candidates — nothing archived."
    states = [p.state_of(r["cid"]) for r in p.rows]
    marked = states.count(st.AGREED)
    kept = states.count(st.KEPT)
    if card_tier(p) == "T1":
        return (f"Dead-channel proposal (T1): {len(p.rows)} listed, archived "
                f"{states.count(st.ARCHIVED)}, marked {marked}, kept {kept}, outcome unknown "
                f"{states.count(st.UNKNOWN)}.")
    return (f"Dead-channel proposal (T0 — nothing archived): {len(p.rows)} listed, you marked "
            f"{marked}, kept {kept}.")


def render_page(fold: st.Fold, proposal_id: str, page: int, *, now: float | None = None,
                buttons: bool | None = None, page_cids: list[str] | None = None) -> tuple[list[dict], str]:
    """(blocks, text) for one page of one proposal, from store truth. ``page_cids``
    is the page's cid list at FIRST render (before any `delivered` event exists)."""
    now = time.time() if now is None else float(now)
    p = fold.proposals[proposal_id]
    pages = paginate(p.rows)
    n_pages = len(pages)
    info = p.pages.get(page) or {}
    cids = list(page_cids if page_cids is not None else (info.get("rendered_cids") or []))
    if buttons is None:
        buttons = bool(info.get("buttons"))
    sup = fold.superseded_by(proposal_id)
    expired = now >= p.expires
    actionable = buttons and sup is None and not expired
    tier = card_tier(p)
    blocks: list[dict] = []
    if sup is not None:
        blocks.append(_context(f"_Superseded by the {_date(sup.created)} card — nothing further "
                               "happens from this one._"))
    elif expired:
        blocks.append(_context(f"_Expired {_date(p.expires)} — ask 'archive the dead channels' "
                               "for a fresh scan._"))
    if page == 1:
        if p.blind:
            cause = BLIND_CAUSES.get(p.blind, p.blind)
            blocks.append(_section(f"I could not complete the dead-channel scan ({cause}), so I am "
                                   "proposing nothing this time — nothing was archived."))
            return blocks, fallback_text(p)
        if not p.rows:
            unread = int((p.counts or {}).get("unreadable") or 0)
            if unread:
                head = (f"No candidates among the {max(0, p.scanned - unread)} channels I could read; "
                        f"{unread} could not be read — not judged. Nothing was archived.")
            else:
                head = ("No channel I could read has gone 90+ days without a person posting "
                        f"(outside the exemptions). Scanned {p.scanned}. Nothing was archived.")
            blocks.append(_section(head))
            blocks.append(_context(_counts_line(p)))
            return blocks, fallback_text(p)
        blocks.append(_section(HEADER_T1 if tier == "T1" else HEADER_T0))
        blocks.append(_context(f"Scanned {p.scanned} channels I belong to; inactive = no message "
                               f"from a person for 90+ days. {BUTTONS_ONLY} {REVERSIBLE}"))
        blocks.append(_context(_counts_line(p)))
        if not buttons:
            blocks.append(_context(BUTTONS_OFF))
        if p.card_agreed_by:
            blocks.append(_context(AGREED_LINE))
        elif actionable:
            blocks.append({"type": "actions", "block_id": f"chanarch_agree_{proposal_id}"[:255],
                           "elements": [_btn("This list matches my read", ACTION_AGREED, proposal_id)]})
    else:
        blocks.append(_context(f"Dead-channel proposal (continued) — part {page} of {n_pages}. "
                               f"{'Nothing is archived at T0. ' if tier == 'T0' else ''}{REVERSIBLE}"))
    rows = p.rows_by_cid
    a_rows = [rows[c] for c in cids if (rows.get(c) or {}).get("section") == cl.SECTION_A]
    b_rows = [rows[c] for c in cids if (rows.get(c) or {}).get("section") == cl.SECTION_B]
    if a_rows:
        blocks.append(_context(f"*Section A — no person has posted in 90+ days ({len(a_rows)} on "
                               "this part)*"))
        for r in a_rows:
            blocks.extend(_row_blocks(p, r, buttons=buttons, actionable=actionable))
    if b_rows:
        blocks.append(_context(f"*Section B — inactive, but exempt or worth a closer look; per-row "
                               f"only ({len(b_rows)} on this part)*"))
        for r in b_rows:
            blocks.extend(_row_blocks(p, r, buttons=buttons, actionable=actionable))
    if actionable:
        undecided = undecided_a(p, cids)
        if undecided:
            n = len(undecided)
            label = f"Archive all {n} shown" if tier == "T1" else f"Mark all {n} shown to archive"
            blocks.append({"type": "actions", "block_id": f"chanarch_all_{proposal_id}_{page}"[:255],
                           "elements": [_btn(label, ACTION_ALL, f"{proposal_id}:p{page}",
                                             "danger" if tier == "T1" else None)]})
    if n_pages > 1:
        blocks.append(_context(f"Part {page} of {n_pages} — each part has its own buttons."))
    if len(blocks) > BLOCK_BUDGET:   # unreachable by construction; never ship >50
        blocks = blocks[:BLOCK_BUDGET - 1] + [_context("(card trimmed — ask for a fresh scan)")]
    return blocks, fallback_text(p)


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _clean(text)}}


def _context(text: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": _clean(text)}]}


# ── the channel posts (the ONLY two things the lane ever says inside a channel) ──
def notice_text(age_days: int | None, actor_id: str) -> str:
    age = f"{age_days} days since the last message from a person" if age_days \
        else "no message from a person in 90+ days"
    return _clean(f":package: Archiving for inactivity — {age}. Archiving is reversible: anyone "
                  f"here can unarchive it from the channel settings. (Approved by <@{actor_id}> "
                  "via Cora's dead-channel proposal.)")


CORRECTION_TEXT = "Archive did not go through — the channel stays open."
