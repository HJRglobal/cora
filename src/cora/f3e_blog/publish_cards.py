"""One-tap publish cards for staged F3E blog articles (S4).

Harrison ruled 2026-08-26: auto-draft, one-tap approve, publish. Full-auto
publishing was REJECTED and Harrison is the sole publisher. This module is the
tap.

WHY THIS DOES NOT USE `confirm_cards.mint_stash_id`
---------------------------------------------------
`confirm_cards`' stash index lives in PROCESS MEMORY. A card here is minted by a
weekly SCHEDULED SCRIPT and tapped later in the always-on BOT process -- two
different interpreters -- so a stash minted by the script would simply not exist
when the tap arrived, and every tap would read as orphaned. The pending store is
therefore on disk, the same shape `code_queue` and `gap_autofill` use for exactly
this reason. `confirm_cards.chunk_mrkdwn_sections` (a pure helper) is still used,
because a card body that silently truncates at Slack's 3000-char section limit is
a defect this repo has already paid for once.

AUTHORITY
---------
`actor_id != HARRISON_ID` is refused in code. Deliberately NOT routed through
`review_lanes.can_approve`: that function's docstring guarantees, structurally,
that a non-Harrison actor can only ever be admitted on the MECHANICAL lane, and
widening it to admit a publish tap would put a new lane inside the one function
whose shape is the guarantee. Publishing to the public web is not mechanical.
There is no YAML, roster, or flag that can grant it.

NO EXPIRY CLOCK, BY CHOICE
--------------------------
A staged article does not rot, and Harrison has set no TTL for this lane, so
inventing one would mean retiring a card he might still want on a number nobody
chose. Instead every tap RE-READS the article's live state from Shopify first, so
"already published elsewhere", "deleted", and "still staged" are distinguished by
verified state rather than by a guessed clock -- which is also what makes the
outcomes honest when Harrison publishes from admin instead of from the card.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

from .. import confirm_cards, slack_egress
from ..connectors import shopify_client
from . import preflight

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]

ACTION_PUBLISH = "cora_f3e_blog_publish"
ACTION_DISMISS = "cora_f3e_blog_dismiss"

# Same fixed id as user_access._HARRISON_ID / review_lanes._FOUNDER_ID.
HARRISON_ID = os.environ.get("HARRISON_SLACK_USER_ID", "U0B2RM2JYJ1")

# #f3-marketing. #f3-hq does not exist in the workspace -- the first fire of the
# interim task proved it, and Harrison named this channel on 2026-08-26.
MARKETING_CHANNEL = "C0B4V8BGJSJ"

STATE_PENDING = "PENDING"
#: Claimed by a publish tap, network I/O in flight. Not terminal, but not
#: available either -- a Dismiss tap arriving now must not take the row.
STATE_PUBLISHING = "PUBLISHING"
STATE_PUBLISHED = "PUBLISHED"
STATE_DISMISSED = "DISMISSED"
STATE_FAILED = "FAILED"

_AZ = timezone(timedelta(hours=-7))
_LOCK = Lock()

# Outcomes whose card keeps its buttons: the action did not happen and retrying
# is the right next move. Everything else is terminal and the card is stripped.
# "error" was in the first cut and no branch ever returned it, which made the
# keep-buttons contract read wider than it was.
RETRYABLE_OUTCOMES = frozenset({"failed"})


def _now_iso() -> str:
    return datetime.now(_AZ).isoformat(timespec="seconds")


def pending_path() -> Path:
    """The card store: an APPEND-ONLY event log, not a rewritten JSON blob.

    This shape is load-bearing. The first cut kept a JSON dict and did whole-file
    read-modify-write under a `threading.Lock` -- which is process-local, while
    the two writers here are DIFFERENT PROCESSES (the weekly script mints cards;
    the always-on bot resolves taps). Both directions of lost update were
    reproduced with two real interpreters:

      * the script's stale snapshot reverted a PUBLISHED card to PENDING, and
      * the script's own newly staged card was ERASED by the bot's write -- the
        article staged in Shopify, the backlog row consumed, the card sitting in
        Harrison's DM, and every tap answering "I don't have a record of that
        draft anymore", permanently.

    An append-only log has no read-modify-write, so neither can happen: a writer
    only ever adds a line. The fold in `_fold` is what turns events back into
    state, and it refuses to move a row OUT of a terminal state, so a late
    `staged` event cannot resurrect a resolved card.
    """
    return Path(os.environ.get(
        "CORA_F3E_BLOG_CARDS_PATH",
        str(_REPO_ROOT / "data" / "state" / "f3e-blog-card-events.jsonl"),
    ))


def ledger_path() -> Path:
    return Path(os.environ.get(
        "CORA_F3E_BLOG_LEDGER_PATH",
        str(_REPO_ROOT / "logs" / "f3e-blog-publish-ledger.jsonl"),
    ))


class CardStoreCorrupt(RuntimeError):
    """The event log exists but has unreadable lines."""


#: States no later event may move a row out of.
_TERMINAL = frozenset({STATE_PUBLISHED, STATE_DISMISSED})


def _append_event(handle: str, event: str, fields: dict) -> None:
    """Append one event line. The ONLY write path for card state."""
    row = {"handle": handle, "event": event, "at": _now_iso()}
    row.update(fields)
    p = pending_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False) + "\n"
    # Single append under one open(): the OS appends atomically for a write this
    # small, so two processes interleave as whole lines rather than corrupting
    # each other. No temp file, so no shared fixed .tmp name to collide on.
    with p.open("a", encoding="utf-8") as fh:
        fh.write(line)


def _fold(*, strict: bool = False) -> dict:
    """Replay the event log into {handle: record}.

    A malformed line is SKIPPED and counted, never treated as an empty store.
    That distinction matters: the first cut's `except Exception: return {}` could
    not tell "no file yet" from "torn file", so a single bad byte silently
    emptied the store -- and on the pipeline's state file the same shape made a
    fail-closed drift gate report "first run, nothing to compare against" and
    re-arm staging against un-reviewed claims rails.

    `strict=True` raises `CardStoreCorrupt` instead, for callers that must not
    act on a partial view.
    """
    p = pending_path()
    if not p.exists():
        return {}
    out: dict = {}
    bad = 0
    try:
        raw = p.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        if strict:
            raise CardStoreCorrupt("card event log unreadable: %s" % exc) from exc
        log.error("f3e_blog: card event log unreadable (%s)", exc)
        return {}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
            handle = row["handle"]
        except Exception:  # noqa: BLE001
            bad += 1
            continue
        cur = out.get(handle)
        if cur is None:
            out[handle] = {k: v for k, v in row.items() if k != "event"}
            continue
        # Terminal is sticky: a later non-terminal event must not resurrect a
        # resolved card (fold is otherwise last-write-wins).
        if cur.get("state") in _TERMINAL and row.get("state") not in _TERMINAL:
            row = {k: v for k, v in row.items() if k not in ("state", "event")}
        cur.update({k: v for k, v in row.items() if k != "event"})
    if bad:
        msg = "card event log has %d unreadable line(s)" % bad
        if strict:
            raise CardStoreCorrupt(msg)
        log.error("f3e_blog: %s -- state may be incomplete", msg)
    return out


def _read_all() -> dict:
    return _fold()


def _ledger(event: str, rec: dict, **extra) -> None:
    """Append-only audit row. Fail-soft: an audit failure must never break the
    reply, but it IS logged loudly rather than swallowed."""
    try:
        p = ledger_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": int(time.time()),
            "at": _now_iso(),
            "event": event,
            "handle": rec.get("handle"),
            "article_gid": rec.get("article_gid"),
            "title": rec.get("title"),
            "lane": rec.get("lane"),
            "backlog_row": rec.get("backlog_row"),
        }
        row.update(extra)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        log.error("f3e_blog: ledger write FAILED for %s: %s", rec.get("handle"), exc)


def mint_handle() -> str:
    return "blogpub-" + secrets.token_hex(6)


# A card excerpt past a couple of sentences has no reader value, and an uncapped
# one is a real performance hazard rather than a cosmetic one: slack_egress's
# bare-URL redactor is quadratic over a long uniform run, so sanitising an
# uncapped body measured 1.4s at 20 KB, 8.8s at 50 KB and 36s at 100 KB. The
# card-drafts tool passes a Shopify `summary` straight through and loops up to 50
# articles inside a 25s tool timeout, so the cap is what keeps that bounded.
_MAX_EXCERPT_CHARS = 600
_MAX_TITLE_CHARS = 200


def _one_line(text: str, limit: int) -> str:
    """Collapse to a single line and bound the length.

    Applied to the TITLE as well as the excerpt: the first cut flattened only the
    excerpt, so a multi-line title broke its own bold wrapper and injected a
    stray body line into the card.
    """
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


# Report-scrub patterns. Each is a single bounded quantifier with a distinct
# terminator, so none can backtrack.
_URL_RE = re.compile(r"https?://[^\s'\")]{0,400}")
# Spaces are ALLOWED inside the path and the bound is the quote, because the one
# path this exists to hide is "G:\My Drive\HJR-Founder-OS\..." -- a class that
# excluded whitespace stopped at "My Drive" and left the rest of the Founder-OS
# tree in the channel post.
_WIN_PATH_RE = re.compile(r"[A-Za-z]:\\[^'\")]{0,400}")
_STORE_HOST_RE = re.compile(r"\b[\w.-]{1,60}\.myshopify\.com\b")
_ADMIN_HOST_RE = re.compile(r"\badmin\.shopify\.com\b")
_ADMIN_API_RE = re.compile(r"/admin/api/[\d-]{1,12}/[\w.]{0,40}")


def scrub_for_report(exc: Exception | str, limit: int = 200) -> str:
    """A bounded, path-free, host-free rendering of an error for a REPORT.

    The weekly report is posted to #f3-marketing, and raw exception text there
    leaked the Founder-OS Drive path, the myshopify store host, the admin API
    path and raw Shopify response bodies -- none of which belong in a marketing
    channel, and the connector's own contract says never to surface store URLs.
    The class-level Slack sanitiser does not cover Windows paths or Shopify
    hosts, so this is the scrub that has to do it.
    """
    txt = " ".join(str(exc).split())
    # Order matters: a full URL collapses whole before the host rules run.
    txt = _URL_RE.sub("<a link>", txt)
    txt = _WIN_PATH_RE.sub("<a file path>", txt)
    txt = _STORE_HOST_RE.sub("the store", txt)
    txt = _ADMIN_HOST_RE.sub("the store admin", txt)
    txt = _ADMIN_API_RE.sub("the store API", txt)
    return txt if len(txt) <= limit else txt[: limit - 3] + "..."


def card_was_delivered(rec: dict | None) -> bool:
    """True only when a card message actually landed in Harrison's DM.

    `stage_card` is fail-soft (no token, or a Slack error, records the card but
    delivers nothing), and the first cut had four callers -- including the
    permanent Drive pipeline log -- asserting "Publish card sent to Harrison"
    from the mere absence of an exception.
    """
    return bool((rec or {}).get("dm_message_ts"))


# ---------------------------------------------------------------------------
# Card copy
#
# Every string below is read by a HUMAN and by no model, so it carries no
# sentinel and no model-facing directive (D-232..D-236). There is nothing to
# strip and nothing that could be stripped: a belt keyed on a sentinel fails open
# on text that has none, which is exactly how directive prose reached humans on
# six Class-B kinds. tests/test_f3e_blog_cards.py asserts this against the same
# token lists the Class-B guard uses, so the two cannot drift.
# ---------------------------------------------------------------------------


def already_carded_gids(data: dict | None = None) -> set[str]:
    """Article gids that already have a card in ANY state.

    Includes DISMISSED on purpose: re-offering a card Harrison declined is the
    re-card loop the spec forbids.
    """
    recs = data if data is not None else _read_all()
    return {
        r.get("article_gid") for r in recs.values()
        if r.get("article_gid")
        # A recorded-but-UNDELIVERED card must not suppress re-offering: it was
        # what made "Every staged draft already has a card in your DMs" a false
        # statement about Harrison's DMs, with no resend path anywhere.
        and (card_was_delivered(r) or r.get("state") in _TERMINAL)
    }


def undelivered_records() -> list[dict]:
    """PENDING records whose card never reached Slack -- re-offerable."""
    return [dict(r) for r in _read_all().values()
            if r.get("state") == STATE_PENDING and not card_was_delivered(r)]


def build_publish_blocks(rec: dict) -> tuple[str, list[dict]]:
    """Return (fallback_text, blocks) for one staged article."""
    title = _one_line(rec.get("title"), _MAX_TITLE_CHARS) or "(untitled)"
    lane = (rec.get("lane") or "").capitalize() or "Blog"
    excerpt = _one_line(rec.get("excerpt"), _MAX_EXCERPT_CHARS)
    admin_url = rec.get("admin_url") or ""
    handle = rec["handle"]
    rails = rec.get("rails_passed")

    lines = ["*%s draft ready to publish*" % lane, "*%s*" % title]
    staged = "Staged unpublished in /blogs/%s." % (rec.get("blog_handle") or lane.lower())
    if rails:
        staged += " Claims preflight passed %d mechanical rails." % int(rails)
    if rails is None:
        # No preflight ran on this one -- it was not staged by this lane (the
        # card-drafts tool offers whatever is sitting unpublished). Saying so is
        # required: the footer below names the rails that were NOT machine
        # checked, whose only reading is that the others WERE.
        staged += (" I did not draft this one, so my claims preflight never ran "
                   "on it. Read it before publishing.")
    lines.append(staged)
    if excerpt:
        lines.append("")
        lines.append("> " + excerpt)
    if admin_url:
        lines.append("")
        lines.append("<%s|Read the full draft in Shopify admin>" % admin_url)
    else:
        # Never name a link the card does not carry. The admin URL is empty only
        # when the store env is unset, and the first cut still told Harrison to
        # "publish it from the Shopify admin link above".
        lines.append("")
        lines.append("I could not build the admin link for this one. It is in the "
                     "Shopify admin under this title.")

    body = slack_egress.sanitize_text("\n".join(lines))
    blocks = confirm_cards.chunk_mrkdwn_sections(body)
    blocks.append({
        "type": "actions",
        "block_id": ("cora_f3e_blog_actions_%s" % handle)[:255],
        "elements": [
            {"type": "button", "action_id": ACTION_PUBLISH, "style": "primary",
             "text": {"type": "plain_text", "text": "Publish"}, "value": handle},
            {"type": "button", "action_id": ACTION_DISMISS,
             "text": {"type": "plain_text", "text": "Dismiss"}, "value": handle},
        ],
    })
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": slack_egress.sanitize_text(
            "Publishing is yours alone; I never publish on my own. Not machine "
            "checked: %s." % ", ".join(preflight.UNENFORCED_RAILS)
        )}],
    })
    fallback = "%s draft ready to publish: %s" % (lane, title)
    return fallback, blocks


def terminal_card_blocks(orig_blocks: list[dict], message: str,
                         *, keep_buttons: bool) -> list[dict]:
    """Rewrite a tapped card so its own body no longer contradicts the outcome.

    The first cut kept every section verbatim and appended the outcome as a small
    grey context line -- so after a successful publish the card still headlined
    "Learn draft ready to publish" and "Staged unpublished in /blogs/learn", both
    of which the tap had just falsified, with the truth in the smallest element on
    screen. The sibling this was copied from keeps its sections because they hold
    REVIEWED CONTENT the reader still needs; here the first lines are STATE CLAIMS.

    So: the state line is replaced, the title / excerpt / link lines are kept, and
    the actions block survives only on a retryable outcome (where nothing
    happened and the ask still stands).
    """
    kept: list[dict] = []
    for block in orig_blocks or []:
        btype = block.get("type")
        if btype == "section":
            text = ((block.get("text") or {}).get("text") or "")
            lines = [
                ln for ln in text.split("\n")
                if not ln.startswith("*") or ln.count("*") > 2
            ]
            # Drop the two state-claim lines; keep the title, excerpt and link.
            lines = [ln for ln in lines
                     if "ready to publish" not in ln
                     and not ln.startswith("Staged unpublished")]
            body = "\n".join(lines).strip()
            if body:
                kept.append({"type": "section",
                             "text": {"type": "mrkdwn", "text": body}})
        elif btype == "actions" and keep_buttons:
            kept.append(block)
    kept.insert(0, {"type": "section", "text": {
        "type": "mrkdwn", "text": slack_egress.sanitize_text(message)}})
    if keep_buttons:
        # The retry path still asks for a publish decision, so the disclosure of
        # what was NOT machine checked has to survive with it.
        kept.append({"type": "context", "elements": [{
            "type": "mrkdwn", "text": slack_egress.sanitize_text(
                "Still not machine checked: %s." % ", ".join(
                    preflight.UNENFORCED_RAILS))}]})
    return kept


def build_buttons_off_blocks(rec: dict) -> tuple[str, list[dict]]:
    """The card shown when the button surface is switched off.

    It promises no affordance it cannot honour: no Publish button, and no typed
    command either, because none is implemented for this lane. Publishing from
    Shopify admin is the route, which is what Harrison already does today. A card
    that keeps advertising a dead affordance gets acted on -- that happened 11
    times on one card in this repo.
    """
    fallback, blocks = build_publish_blocks(rec)
    blocks = [b for b in blocks if b.get("type") != "actions"]
    where = ("the Shopify admin link above" if rec.get("admin_url")
             else "the Shopify admin, under this title")
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": slack_egress.sanitize_text(
            "My publish buttons are switched off right now, so publish it from "
            "%s when you're ready. Nothing goes live until you do." % where
        )}],
    })
    return fallback, blocks


# ---------------------------------------------------------------------------
# Staging a card
# ---------------------------------------------------------------------------


def _default_client_factory():
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        return None
    from slack_sdk import WebClient
    return WebClient(token=token)


def record_for_article(
    *,
    article: dict,
    lane: str,
    excerpt: str,
    backlog_row: str | None = None,
    rails_passed: int | None = None,
) -> dict:
    """Build (but do not persist) a card record from a read-back article dict."""
    gid = article.get("id") or ""
    blog = article.get("blog") or {}
    return {
        "handle": mint_handle(),
        "article_gid": gid,
        "article_numeric": str(gid).rsplit("/", 1)[-1],
        "title": _one_line(article.get("title"), _MAX_TITLE_CHARS),
        "article_handle": article.get("handle") or "",
        "blog_gid": blog.get("id") or "",
        "blog_handle": blog.get("handle") or "",
        "lane": lane,
        "excerpt": _one_line(excerpt or article.get("summary"),
                             _MAX_EXCERPT_CHARS),
        "admin_url": _safe_admin_url(gid),
        "backlog_row": backlog_row,
        "rails_passed": rails_passed,
        "state": STATE_PENDING,
        "created_at": _now_iso(),
        "target_user_id": HARRISON_ID,
        "dm_channel_id": "",
        "dm_message_ts": "",
    }


def _safe_admin_url(gid: str) -> str:
    try:
        return shopify_client.article_admin_url(gid)
    except Exception:  # noqa: BLE001 -- a missing store env must not lose the card
        return ""


def stage_card(rec: dict, *, client_factory=None) -> dict:
    """Persist the record and DM the card to Harrison.

    Returns the record. Callers MUST check `dm_message_ts` before reporting that
    a card was sent: this function is fail-soft by design (no token, or a Slack
    error, leaves the record recorded but undelivered), and the first cut had
    four callers that all asserted "Publish card sent to Harrison" from the mere
    absence of an exception -- including the permanent Drive pipeline log. Use
    `card_was_delivered`.

    The record is persisted BEFORE the DM: a card that posted but was not recorded
    would be untappable, while a record with no card is merely re-offerable. Fail
    toward the recoverable side.
    """
    _append_event(rec["handle"], "staged", rec)
    _ledger("staged", rec, state=rec.get("state"))

    factory = client_factory or _default_client_factory
    client = factory()
    if client is None:
        log.warning("f3e_blog: no Slack token -- card %s recorded but not sent",
                    rec["handle"])
        return rec

    buttons_on = confirm_cards.confirm_buttons_enabled()
    fallback, blocks = (build_publish_blocks(rec) if buttons_on
                        else build_buttons_off_blocks(rec))
    try:
        dm = client.conversations_open(users=[HARRISON_ID])
        channel = dm["channel"]["id"]
        posted = client.chat_postMessage(
            channel=channel, text=fallback, blocks=blocks,
            unfurl_links=False, unfurl_media=False,
        )
        _append_event(rec["handle"], "delivered", {
            "dm_channel_id": channel,
            "dm_message_ts": posted.get("ts", ""),
            "buttons": buttons_on,
        })
        log.info("f3e_blog: publish card sent handle=%s article=%s buttons=%s",
                 rec["handle"], rec["article_numeric"], buttons_on)
        return get_record(rec["handle"]) or rec
    except Exception as exc:  # noqa: BLE001
        log.error("f3e_blog: card DM FAILED handle=%s: %s", rec["handle"], exc)
        _ledger("card_send_failed", rec, error=str(exc)[:300])
        return rec


# ---------------------------------------------------------------------------
# The tap
# ---------------------------------------------------------------------------


def process_tap(handle: str, actor_id: str, *, action: str) -> tuple[str, str]:
    """Apply a Publish or Dismiss tap. Returns (outcome, message-for-the-human).

    Outcomes: published | dismissed | already_live | already_handled | orphaned
              | not_authorized | failed | error

    Ordering is deliberate:
      1. authority   -- before any lookup, so a stranger's tap cannot become an
                        existence oracle for staged content
      2. lookup      -- orphaned
      3. state       -- already_handled (distinct from orphaned)
      4. live re-read-- already_live / vanished, from Shopify not from our record
      5. act         -- publish, then read back
      6. record      -- AFTER the act succeeds, so a failed publish leaves the
                        row PENDING and re-tappable rather than "approved but
                        never published"
    """
    if not actor_id or actor_id != HARRISON_ID:
        # Deliberately says nothing about whether `handle` exists.
        return "not_authorized", "Only Harrison can publish to the site."
    if not handle:
        return "orphaned", "I don't have a record of that draft anymore."

    with _LOCK:
        rec = _read_all().get(handle)
        if not rec:
            return "orphaned", "I don't have a record of that draft anymore."
        rec = dict(rec)
        state = rec.get("state")
        if state == STATE_PUBLISHING:
            return "already_handled", (
                "I'm in the middle of publishing that one right now. Give it a "
                "few seconds and I'll report back on the card.")
        if state != STATE_PENDING:
            return "already_handled", _already_handled_message(rec)
        if action == "publish":
            # CLAIM the row before any network I/O, so a Dismiss tap arriving
            # during the publish cannot take it. The first cut released the lock
            # and then spent up to ~40s in Shopify calls: a Harrison who saw
            # nothing happen and tapped Dismiss won the row, the publish then
            # SUCCEEDED, and _finish reported "already handled -- it's still a
            # draft" for an article that was by then publicly live, with no
            # ledger row and no marketing note. Only the log knew.
            _append_event(handle, "publishing", {"state": STATE_PUBLISHING})
            rec["state"] = STATE_PUBLISHING

    if action != "publish":
        # Anything that is not an explicit publish is treated as a dismiss: the
        # default direction on an irreversible outward action must be the safe
        # one, so a future typo cannot publish.
        try:
            live = shopify_client.get_article(rec["article_gid"])
        except Exception:  # noqa: BLE001 -- a read failure must not block a dismiss
            live = {}
        if live.get("isPublished") is True:
            # Harrison published from admin and is now clearing the stale card.
            # Saying "it stays as a draft" here would assert a false fact about
            # the public site AND drop a live post out of the later-day recheck.
            return _finish(handle, rec, STATE_PUBLISHED, "already_live",
                           "That one is actually live already, so I left it alone "
                           "and marked it published rather than dismissed.",
                           extra=_public_fields(live, rec))
        return _finish(handle, rec, STATE_DISMISSED, "dismissed",
                       "Left it unpublished. It stays as a draft in Shopify and I "
                       "won't offer it again.")

    # --- publish ---
    try:
        live = shopify_client.get_article(rec["article_gid"])
    except Exception as exc:  # noqa: BLE001
        log.error("f3e_blog: pre-publish read failed handle=%s: %s", handle, exc)
        _release_claim(handle, "precheck_failed")
        _ledger("publish_precheck_failed", rec, error=str(exc)[:300])
        return "failed", (
            "I couldn't reach the site to check that draft, so I did NOT publish "
            "it. Nothing changed. Try the button again, or publish from the "
            "Shopify admin."
        )

    if live.get("isPublished") is True:
        # Harrison published from admin, or a racing tap won. Honest, and not a
        # claim that this tap did it. Records the public URL so the later-day
        # read-back still covers it (it was skipped entirely before).
        return _finish(handle, rec, STATE_PUBLISHED, "already_live",
                       "That one is already live, so I left it alone. Nothing "
                       "changed just now.",
                       extra=_public_fields(live, rec))

    try:
        published = shopify_client.publish_article(rec["article_gid"])
    except Exception as exc:  # noqa: BLE001
        log.error("f3e_blog: publish FAILED handle=%s: %s", handle, exc)
        _release_claim(handle, "publish_failed")
        _ledger("publish_failed", rec, error=str(exc)[:300])
        return "failed", (
            "The publish did NOT go through (%s). The article is still a draft "
            "and nothing was changed. You can retry the button, or publish it "
            "from the Shopify admin." % scrub_for_report(exc, 160)
        )

    # From here the article IS LIVE. Everything below is reporting, and no
    # failure in it may lose that fact -- so it is wrapped: a raise between the
    # write and the record would otherwise leave the article public, the row
    # PENDING, and Harrison told nothing at all.
    try:
        public = _public_fields(published, rec)
        verified, verify_note = _verify_public(public["public_url"],
                                              published.get("title") or "")
        public["public_verified"] = verified
        outcome_msg = _published_message(published.get("title") or rec["title"],
                                         public["public_url"], verified, verify_note)
    except Exception as exc:  # noqa: BLE001
        log.error("f3e_blog: post-publish verification raised handle=%s: %s",
                  handle, exc)
        public = {"public_url": "", "public_verified": False}
        outcome_msg = (
            "Published *%s* in Shopify. I then hit an error checking the live "
            "page, so treat it as live-but-unverified and give it a look."
            % (rec.get("title") or "the article"))
    return _finish(handle, rec, STATE_PUBLISHED, "published", outcome_msg,
                   extra=public)


def _public_fields(article: dict, rec: dict) -> dict:
    """The public-URL fields for a record, derived from a live article dict."""
    return {
        "public_url": shopify_client.article_public_url(
            (article.get("blog") or {}).get("handle") or rec.get("blog_handle"),
            article.get("handle") or rec.get("article_handle"),
        ),
        "published_at": article.get("publishedAt") or _now_iso(),
    }


def _release_claim(handle: str, why: str) -> None:
    """Hand a claimed row back to PENDING after a failed publish attempt.

    Without this a transient Shopify error would leave the card stuck in
    PUBLISHING forever, and the retryable outcome that keeps its buttons would be
    un-retryable.
    """
    _append_event(handle, "claim_released", {"state": STATE_PENDING, "why": why})


def _verify_public(url: str, title: str) -> tuple[bool, str]:
    """(verified, note). Never raises; a verification failure is reported, not
    swallowed and not upgraded to a publish failure -- the write DID happen."""
    if not url:
        return False, "I couldn't build the public link to check it."
    code, text = shopify_client.fetch_public_page(url)
    if code == 200 and title and title[:40] in text:
        return True, ""
    if code == 200:
        return False, ("the page answered but I couldn't find the title on it, so "
                       "give it a look")
    if code == 0:
        return False, "I couldn't reach the public page to confirm it"
    return False, "the public page answered %s" % code


def _published_message(title: str, url: str, verified: bool, note: str) -> str:
    if verified:
        return ("Published: *%s*. I loaded the live page and it's serving.\n<%s|%s>"
                % (title, url, url))
    tail = (" -- %s" % note) if note else ""
    return ("Published *%s* in Shopify, but I could not confirm the live page%s. "
            "Treat it as live-but-unverified until someone opens it.\n%s"
            % (title, tail, ("<%s|%s>" % (url, url)) if url else ""))


def _already_handled_message(rec: dict) -> str:
    state = rec.get("state")
    if state == STATE_PUBLISHED:
        url = rec.get("public_url") or ""
        return ("That one is already published%s."
                % ((": <%s|see it live>" % url) if url else ""))
    if state == STATE_DISMISSED:
        return "You already left that one unpublished. It's still a draft."
    if state == STATE_PUBLISHING:
        return ("I'm partway through publishing that one. Give it a few seconds "
                "and the card will say how it went.")
    return "That draft is already handled; nothing changed just now."


def _finish(handle: str, rec: dict, state: str, outcome: str, message: str,
            extra: dict | None = None) -> tuple[str, str]:
    """Record the terminal state under the lock, re-checking PENDING so a racing
    second tap cannot double-record."""
    with _LOCK:
        stored = _read_all().get(handle)
        if not stored:
            return "orphaned", "I don't have a record of that draft anymore."
        # PENDING or the row this tap itself claimed. A row claimed by a DIFFERENT
        # tap, or already terminal, is not ours to resolve.
        if stored.get("state") not in (STATE_PENDING, STATE_PUBLISHING):
            return "already_handled", _already_handled_message(stored)
        fields = dict(extra or {})
        fields.update({
            "state": state,
            "resolved_at": _now_iso(),
            "resolved_via": "button",
        })
        _append_event(handle, outcome, fields)
        stored = _read_all().get(handle) or dict(stored, **fields)
    _ledger(outcome, stored, state=state, **(extra or {}))
    _advance_backlog_row(stored, state)
    log.info("f3e_blog: card %s -> %s (%s)", handle, state, outcome)
    return outcome, message


# ---------------------------------------------------------------------------
# The #f3-marketing note (success only)
# ---------------------------------------------------------------------------


def _advance_backlog_row(rec: dict, state: str) -> None:
    """Reflect a terminal outcome back into the human editorial table.

    Without this the backlog row stayed at DRAFTED forever: Harrison's own
    source-of-truth table showed "DRAFTED" for a post that had been live for
    weeks, and -- worse -- a DISMISSED topic kept the DRAFTED cell, so
    `next_queued` never returned it again and the topic was silently consumed
    with nothing published. Two helpers written for exactly this
    (`published_status_cell` / `dismissed_status_cell`) had no production caller
    at all.

    FAIL-SOFT and deliberately last: the Shopify write and the ledger row are
    already done, so a Drive mount blip must not turn a real publish into a
    reported failure. Imported lazily so the always-on bot's import graph does
    not gain the Drive layer for a path it only needs at tap time.
    """
    row_number = rec.get("backlog_row")
    if not row_number or state not in _TERMINAL:
        return  # News-lane cards carry no backlog row.
    try:
        from . import operating_files as of  # noqa: PLC0415
        text, rows = of.read_backlog()
        match = next((r for r in rows if r.number == str(row_number)), None)
        if match is None:
            log.warning("f3e_blog: backlog row %s not found -- not advancing",
                        row_number)
            return
        cell = (of.published_status_cell if state == STATE_PUBLISHED
                else of.dismissed_status_cell)(rec.get("article_gid") or "")
        of.write_backlog(of.set_row_status(text, match, cell))
        log.info("f3e_blog: backlog row %s -> %s", row_number, state)
    except Exception as exc:  # noqa: BLE001
        log.error("f3e_blog: could not advance backlog row %s to %s: %s",
                  row_number, state, exc)


def marketing_note(rec: dict) -> str:
    """One line, reader-facing, for #f3-marketing after a real publish."""
    lane = (rec.get("lane") or "blog").capitalize()
    title = rec.get("title") or "(untitled)"
    url = rec.get("public_url") or ""
    line = "New %s post is live: *%s*" % (lane, title)
    if url:
        line += " <%s|Read it>" % url
    if rec.get("public_verified") is False:
        line += " (published in admin; I could not confirm the live page yet)"
    return slack_egress.sanitize_text(line)


def post_marketing_note(rec: dict, *, client_factory=None) -> bool:
    """Fail-soft: a missed channel note must never turn a real publish into a
    reported failure."""
    factory = client_factory or _default_client_factory
    try:
        client = factory()
        if client is None:
            return False
        client.chat_postMessage(channel=MARKETING_CHANNEL, text=marketing_note(rec),
                               unfurl_links=False, unfurl_media=False)
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("f3e_blog: #f3-marketing note failed for %s: %s",
                  rec.get("handle"), exc)
        return False


def get_record(handle: str) -> dict | None:
    rec = _read_all().get(handle)
    return dict(rec) if rec else None


def pending_records() -> list[dict]:
    return [dict(r) for r in _read_all().values() if r.get("state") == STATE_PENDING]
