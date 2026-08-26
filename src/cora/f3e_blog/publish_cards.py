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
STATE_PUBLISHED = "PUBLISHED"
STATE_DISMISSED = "DISMISSED"
STATE_FAILED = "FAILED"

_AZ = timezone(timedelta(hours=-7))
_LOCK = Lock()

# Outcomes whose card keeps its buttons: the action did not happen and retrying
# is the right next move. Everything else is terminal and the card is stripped.
RETRYABLE_OUTCOMES = frozenset({"failed", "error"})


def _now_iso() -> str:
    return datetime.now(_AZ).isoformat(timespec="seconds")


def pending_path() -> Path:
    return Path(os.environ.get(
        "CORA_F3E_BLOG_CARDS_PATH",
        str(_REPO_ROOT / "data" / "state" / "f3e-blog-publish-cards.json"),
    ))


def ledger_path() -> Path:
    return Path(os.environ.get(
        "CORA_F3E_BLOG_LEDGER_PATH",
        str(_REPO_ROOT / "logs" / "f3e-blog-publish-ledger.jsonl"),
    ))


def _read_all() -> dict:
    try:
        return json.loads(pending_path().read_text(encoding="utf-8"))
    except Exception:
        return {}


def _write_all(data: dict) -> None:
    p = pending_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


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
    return {r.get("article_gid") for r in recs.values() if r.get("article_gid")}


def build_publish_blocks(rec: dict) -> tuple[str, list[dict]]:
    """Return (fallback_text, blocks) for one staged article."""
    title = rec.get("title") or "(untitled)"
    lane = (rec.get("lane") or "").capitalize() or "Blog"
    excerpt = (rec.get("excerpt") or "").strip()
    admin_url = rec.get("admin_url") or ""
    handle = rec["handle"]
    rails = rec.get("rails_passed")

    lines = ["*%s draft ready to publish*" % lane, "*%s*" % title]
    staged = "Staged unpublished in /blogs/%s." % (rec.get("blog_handle") or lane.lower())
    if rails:
        staged += " Claims preflight passed %d mechanical rails." % int(rails)
    lines.append(staged)
    if excerpt:
        lines.append("")
        lines.append("> " + excerpt.replace("\n", " "))
    if admin_url:
        lines.append("")
        lines.append("<%s|Read the full draft in Shopify admin>" % admin_url)

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
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": slack_egress.sanitize_text(
            "My publish buttons are switched off right now, so publish it from "
            "the Shopify admin link above when you're ready. Nothing goes live "
            "until you do."
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
        "title": article.get("title") or "",
        "article_handle": article.get("handle") or "",
        "blog_gid": blog.get("id") or "",
        "blog_handle": blog.get("handle") or "",
        "lane": lane,
        "excerpt": excerpt or (article.get("summary") or ""),
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

    The record is persisted BEFORE the DM: a card that posted but was not recorded
    would be untappable (the tap would read orphaned), while a record with no card
    is merely re-offerable. Fail toward the recoverable side.
    """
    with _LOCK:
        data = _read_all()
        data[rec["handle"]] = rec
        _write_all(data)
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
        with _LOCK:
            data = _read_all()
            stored = data.get(rec["handle"], rec)
            stored["dm_channel_id"] = channel
            stored["dm_message_ts"] = posted.get("ts", "")
            data[rec["handle"]] = stored
            _write_all(data)
        log.info("f3e_blog: publish card sent handle=%s article=%s buttons=%s",
                 rec["handle"], rec["article_numeric"], buttons_on)
        return stored
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
        data = _read_all()
        rec = data.get(handle)
        if not rec:
            return "orphaned", "I don't have a record of that draft anymore."
        rec = dict(rec)
        if rec.get("state") != STATE_PENDING:
            return "already_handled", _already_handled_message(rec)

    if action == "dismiss":
        return _finish(handle, rec, STATE_DISMISSED, "dismissed",
                       "Left it unpublished. It stays as a draft in Shopify and I "
                       "won't offer it again.")

    # --- publish ---
    try:
        live = shopify_client.get_article(rec["article_gid"])
    except Exception as exc:  # noqa: BLE001
        log.error("f3e_blog: pre-publish read failed handle=%s: %s", handle, exc)
        _ledger("publish_precheck_failed", rec, error=str(exc)[:300])
        return "failed", (
            "I couldn't reach the site to check that draft, so I did NOT publish "
            "it. Nothing changed. Try the button again, or publish from admin."
        )

    if live.get("isPublished") is True:
        # Harrison published from admin, or a racing tap won. Honest, and not a
        # claim that this tap did it.
        return _finish(handle, rec, STATE_PUBLISHED, "already_live",
                       "That one is already live, so I left it alone. Nothing "
                       "changed just now.")

    try:
        published = shopify_client.publish_article(rec["article_gid"])
    except Exception as exc:  # noqa: BLE001
        log.error("f3e_blog: publish FAILED handle=%s: %s", handle, exc)
        _ledger("publish_failed", rec, error=str(exc)[:300])
        return "failed", (
            "The publish did NOT go through: %s. The article is still a draft and "
            "nothing was changed. You can retry the button, or publish it from "
            "the Shopify admin link." % _short(exc)
        )

    # Second half of the read-back: the API calling it published and a reader
    # being served the page are different claims (D-110 rule 2), and on this store
    # a theme template can ignore an API write entirely.
    public_url = shopify_client.article_public_url(
        published.get("blog", {}).get("handle") or rec.get("blog_handle"),
        published.get("handle") or rec.get("article_handle"),
    )
    verified, verify_note = _verify_public(public_url, published.get("title") or "")

    rec["public_url"] = public_url
    rec["published_at"] = published.get("publishedAt") or _now_iso()
    rec["public_verified"] = verified
    outcome_msg = _published_message(published.get("title") or rec["title"],
                                    public_url, verified, verify_note)
    return _finish(handle, rec, STATE_PUBLISHED, "published", outcome_msg,
                   extra={"public_url": public_url, "public_verified": verified})


def _short(exc: Exception, limit: int = 160) -> str:
    txt = " ".join(str(exc).split())
    return txt if len(txt) <= limit else txt[: limit - 3] + "..."


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
    return "That draft is already handled; nothing changed just now."


def _finish(handle: str, rec: dict, state: str, outcome: str, message: str,
            extra: dict | None = None) -> tuple[str, str]:
    """Record the terminal state under the lock, re-checking PENDING so a racing
    second tap cannot double-record."""
    with _LOCK:
        data = _read_all()
        stored = data.get(handle)
        if not stored:
            return "orphaned", "I don't have a record of that draft anymore."
        if stored.get("state") != STATE_PENDING:
            return "already_handled", _already_handled_message(stored)
        stored.update(rec)
        stored["state"] = state
        stored["resolved_at"] = _now_iso()
        stored["resolved_via"] = "button"
        if extra:
            stored.update(extra)
        data[handle] = stored
        _write_all(data)
    _ledger(outcome, stored, state=state, **(extra or {}))
    log.info("f3e_blog: card %s -> %s (%s)", handle, state, outcome)
    return outcome, message


# ---------------------------------------------------------------------------
# The #f3-marketing note (success only)
# ---------------------------------------------------------------------------


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
