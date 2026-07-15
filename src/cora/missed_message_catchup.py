"""Missed-Message Catch-Up -- reconstruct + answer the Slack Q&A Cora missed while down.

Socket Mode does NOT replay events delivered while the bot was disconnected, so
there is no queue of "missed messages" -- the miss set must be RECONSTRUCTED from
channel history over an outage window and inferred. This module does that, then
generates a draft answer for each missed ask by WRAPPING the live answer pipeline
(app._dispatch_qa) so every guard (entity firewall, sibling/cross-entity, PHI wall,
finance firewall, channel_content_guard, source-opacity, reply_formatter) is
inherited unchanged -- never a parallel answer path. Nothing posts to any channel
without Harrison's per-message tap (D-011 pattern).

Architecture notes (VERIFY-FIRST reconciled against live main, 2026-07-15):
  * app._dispatch_qa RETURNS None -- it emits the answer as a side effect via the
    caller-supplied `say` (non-streaming) or client.chat_update (streaming). To
    capture a draft WITHOUT posting we pass a capturing `say` + a capturing client
    proxy and read the captured text back.
  * The entity/PHI/finance PRE-LLM guards live in the app.py HANDLERS, not inside
    _dispatch_qa. So a wrapper MUST replicate the handler guard sequence
    (user_access -> sibling_guard -> cross_entity_guard, plus help intent) BEFORE
    calling _dispatch_qa, or those firewalls are bypassed. We do exactly that.
  * Draft generation is READ-ONLY (D-051): during the wrapped _dispatch_qa call it
    sets CORA_EVAL_MODE=1 (so NO tool executes -- a reconstructed "yes" can never fire
    a real calendar invite / gmail draft / tracker write) and no-ops the shared-state
    writers active_thread_store.register + _try_cache_store + the gap/feedback loggers.
    Consequence: drafts are KB/context-only; a tool-backed answer drafts as "couldn't
    access that" (Harrison sees it and can Skip). The thread is registered, and a reply
    posted, ONLY on Harrison's approval tap. Answer generation still makes real Claude
    (and, for the still-open classifier, Haiku) calls even in dry-run.

NOT a D-047 standalone module: it deliberately (lazy-)imports app.py to reuse the
live pipeline. The RUNNER (scripts/run_missed_message_catchup.py) is a fresh
process, so generating drafts + posting review cards needs NO bot restart. Only the
@app.action / @app.view button wiring in app.py is bot-loaded (one restart to arm).
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from . import channel_classifier
from . import channel_content_guard
from . import cross_entity_guard
from . import entity_router
from . import help_responder
from . import lex_phi_access
from . import org_roles
from . import phi_guard
from . import sibling_guard
from . import slack_sweep_policy
from . import user_access
from .model_router import MODEL_HAIKU
from .tools import user_identity

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Harrison is the sole approver (D-011). Env-overridable, matches knowledge_review.
HARRISON_ID = os.environ.get("HARRISON_SLACK_USER_ID", "U0B2RM2JYJ1")

# Block Kit action ids + edit-modal callback (registered in app.py, bot-loaded).
ACTION_SEND = "catchup_send"
ACTION_EDIT = "catchup_edit"
ACTION_SKIP = "catchup_skip"
VIEW_EDIT_SUBMIT = "catchup_edit_submit"

# The exact placeholder text _dispatch_qa posts before streaming (app.py:658). We
# recognise it so the capturing say never mistakes it for the answer.
_STREAM_PLACEHOLDER = ":thought_balloon: thinking…"

# Injected as user_facing_message() during capture so a Claude/pipeline error (which
# _dispatch_qa swallows and posts as an apology) is DETECTABLE as an error rather than
# captured as a valid draft with a Send button (D-051 wrap-fidelity fix).
_PIPELINE_ERROR_SENTINEL = "\x00catchup-pipeline-error\x00"

# Configurable delay preface (Harrison: yes, configurable). Prepended only to
# non-trivial approved answers (see _PREFACE_MIN_CHARS).
CATCHUP_PREFACE = os.environ.get(
    "CATCHUP_REPLY_PREFACE",
    "Sorry for the delay -- I was offline for a bit and am catching up now.",
)
_PREFACE_MIN_CHARS = 140  # answers shorter than this are one-liners; no preface

# Detection tiers (v1 high precision + optional fuzzy).
TIER_MENTION = "mention"
TIER_DM = "dm"
TIER_THREAD = "thread_participation"
TIER_FUZZY = "fuzzy"

# Idempotency ledger (D-030 ID-based; append-only log, latest row per id wins).
_DEFAULT_LEDGER_PATH = _REPO_ROOT / "data" / "state" / "missed-message-catchup.jsonl"
_TERMINAL = frozenset({"sent", "edited_sent", "skipped"})

# Serialises the one-tap processor across the bot's socket-handler threads.
_ONE_TAP_LOCK = threading.Lock()

# History fetch guardrails.
_MAX_MESSAGES_PER_CHANNEL = 1200
_PAGE_SLEEP = 0.2
_CHANNEL_SLEEP = 1.2  # Slack Tier-3 pacing between channels (matches channel_sweep)

_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")


# ── Window derivation ──────────────────────────────────────────────────────────

_LOG_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")
_ALIVE_MARKERS = ("heartbeat alive", "kb-prewarm:", "prewarm: loaded")
_STARTUP_MARKER = "Cora starting up"


def _parse_log_ts(line: str) -> Optional[float]:
    """Leading naive-LOCAL log timestamp -> epoch seconds (tz from the machine)."""
    m = _LOG_TS_RE.match(line)
    if not m:
        return None
    try:
        naive = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S")
        # The log writer uses local naive time; interpret as local, then to epoch.
        return naive.astimezone().timestamp()
    except Exception:
        return None


def derive_window(
    *,
    logs_dir: Optional[Path] = None,
    now: Optional[float] = None,
    min_gap_minutes: float = 6.0,
    max_lookback_days: float = 4.0,
) -> Optional[tuple[float, float]]:
    """Auto-derive the outage window from Cora's own liveness gap in the logs.

    Returns (oldest_epoch, latest_epoch) bounding the largest heartbeat gap that
    exceeds ``min_gap_minutes`` (heartbeats fire every 60s, so a >6-min gap is an
    outage), or None if no clear gap is found (caller should require --since/--until).

    Reads only log files modified within ``max_lookback_days`` so it never scans the
    full 30-file rotation. Fail-soft: any parse error yields None.
    """
    now = now if now is not None else time.time()
    logs_dir = logs_dir or (_REPO_ROOT / "logs")
    cutoff_mtime = now - max_lookback_days * 86400
    try:
        files = [
            p for p in glob.glob(str(logs_dir / "cora-*.log*"))
            if os.path.getmtime(p) >= cutoff_mtime
        ]
    except Exception:
        return None
    if not files:
        return None

    alive: list[float] = []
    for path in files:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if _STARTUP_MARKER in line or any(mk in line for mk in _ALIVE_MARKERS):
                        ts = _parse_log_ts(line)
                        if ts is not None:
                            alive.append(ts)  # a startup is also an "alive" instant
        except Exception:
            continue

    alive = sorted(set(alive))
    if len(alive) < 2:
        return None

    # Prefer the MOST-RECENT qualifying gap (the outage the operator just recovered
    # from) rather than the globally largest -- a prior/longer incident or an overnight
    # host sleep must not outrank today's outage (D-051). Among consecutive gaps that
    # exceed the floor, take the one whose end is closest to now.
    min_gap = min_gap_minutes * 60.0
    chosen = None
    for a, b in zip(alive, alive[1:]):
        if (b - a) >= min_gap:
            if chosen is None or b > chosen[1]:
                chosen = (a, b)
    return chosen


def parse_ts_arg(value: str) -> float:
    """Parse an ISO-8601 datetime OR an epoch-seconds string into epoch seconds."""
    value = (value or "").strip()
    if not value:
        raise ValueError("empty timestamp")
    try:
        return float(value)  # bare epoch
    except ValueError:
        pass
    iso = value.replace("Z", "+00:00")
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.astimezone()  # naive -> local
    return dt.timestamp()


# ── Channel enumeration + windowed history ──────────────────────────────────────

def list_catchup_channels(client, *, include_dms: bool = True) -> list[dict]:
    """Enumerate channels Cora can read+reply in: public + private (+ DMs).

    Unlike connectors.channel_sweep.list_joined_channels (public-only), this passes
    the full channel_types set. Honors the slack-sweep-policy deny-list
    (should_ingest) so PHI/personal channels are never read here either.
    """
    types = "public_channel,private_channel"
    if include_dms:
        types += ",im"
    out: list[dict] = []
    cursor = None
    while True:
        kwargs: dict = {"types": types, "limit": 200, "exclude_archived": True}
        if cursor:
            kwargs["cursor"] = cursor
        try:
            resp = client.conversations_list(**kwargs)
        except Exception as exc:  # noqa: BLE001
            log.warning("catchup: conversations_list failed: %s", exc)
            break
        for ch in resp.get("channels", []):
            cid = ch.get("id", "")
            is_im = bool(ch.get("is_im"))
            if is_im:
                if include_dms and not ch.get("is_user_deleted"):
                    out.append({"id": cid, "name": "dm", "is_dm": True,
                                "is_private": True, "user": ch.get("user", "")})
                continue
            if not ch.get("is_member"):
                continue
            name = ch.get("name", "")
            is_private = bool(ch.get("is_private"))
            if not slack_sweep_policy.should_ingest(name, cid, is_private=is_private):
                log.info("catchup: channel #%s denied by sweep policy -- skipped", name)
                continue
            out.append({"id": cid, "name": name, "is_dm": False,
                        "is_private": is_private, "user": ""})
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(0.3)
    return out


def fetch_window_messages(
    client, channel_id: str, oldest: float, latest: float,
    *, max_messages: int = _MAX_MESSAGES_PER_CHANNEL,
) -> list[dict]:
    """conversations.history over [oldest, latest] -- BOTH bounds set (spec gotcha).

    Paginates the full window, skips ALL system-subtype messages (only real user
    text survives), returns ascending by ts. Fail-soft (partial list on API error).
    """
    msgs: list[dict] = []
    cursor = None
    oldest_s = f"{oldest:.6f}"
    latest_s = f"{latest:.6f}"
    more_remaining = False
    while len(msgs) < max_messages:
        kwargs: dict = {
            "channel": channel_id,
            "oldest": oldest_s,
            "latest": latest_s,
            "inclusive": True,
            "limit": 200,
        }
        if cursor:
            kwargs["cursor"] = cursor
        try:
            resp = client.conversations_history(**kwargs)
        except Exception as exc:  # noqa: BLE001
            log.warning("catchup: history failed for %s: %s", channel_id, exc)
            break
        for m in resp.get("messages", []):
            if m.get("subtype") or m.get("bot_id"):
                continue
            if not m.get("user") or not (m.get("text") or "").strip():
                continue
            msgs.append(m)
        cursor = (resp.get("response_metadata") or {}).get("next_cursor")
        if not resp.get("has_more") or not cursor:
            break
        if len(msgs) >= max_messages:
            more_remaining = True  # stopped by the cap, not by end-of-window
            break
        time.sleep(_PAGE_SLEEP)
    if more_remaining:
        # Slack returns newest-first, so the DROPPED messages are the OLDEST in the
        # window -- exactly the earliest post-outage asks most likely still unanswered.
        log.warning(
            "catchup: channel %s hit the %d-message window cap -- the OLDEST asks in "
            "the window were NOT fetched. Narrow --since/--until or --channels for full "
            "coverage of this channel.", channel_id, max_messages,
        )
    msgs.sort(key=lambda m: float(m.get("ts", "0") or 0))
    return msgs


def _thread_replies(client, channel_id: str, root_ts: str, cache: dict) -> list[dict]:
    """Fetch (cached) full thread replies up to NOW (for answered/participation checks)."""
    key = (channel_id, root_ts)
    if key in cache:
        return cache[key]
    out: list[dict] = []
    cursor = None
    try:
        while True:
            kwargs: dict = {"channel": channel_id, "ts": root_ts, "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = client.conversations_replies(**kwargs)
            out.extend(resp.get("messages", []))
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not resp.get("has_more") or not cursor:
                break
            time.sleep(_PAGE_SLEEP)
    except Exception as exc:  # noqa: BLE001
        log.warning("catchup: replies failed for %s/%s: %s", channel_id, root_ts, exc)
    cache[key] = out
    return out


def _is_cora(msg: dict, bot_id: Optional[str]) -> bool:
    """True ONLY if a Slack message was authored by Cora herself.

    D-051 fix: a bare `if msg.get("bot_id")` counted EVERY app (Make.com, Tag, the
    fighter trackers) as Cora -- which both suppressed genuine misses (a foreign bot
    replying after the ask read as "Cora already answered") and false-promoted threads
    to TIER_THREAD. Cora's own bolt-posted messages carry user == her bot USER id, so
    match strictly on that.
    """
    return bool(bot_id) and msg.get("user") == bot_id


def cora_replied_after(messages: list[dict], after_ts: str, bot_id: Optional[str]) -> bool:
    """True if any Cora-authored message exists at ts > after_ts (already answered)."""
    try:
        after = float(after_ts)
    except (TypeError, ValueError):
        return False
    for m in messages:
        try:
            if float(m.get("ts", "0") or 0) > after and _is_cora(m, bot_id):
                return True
        except (TypeError, ValueError):
            continue
    return False


def cora_participated_before(messages: list[dict], before_ts: str, bot_id: Optional[str]) -> bool:
    """True if Cora authored a message at ts < before_ts in this thread."""
    try:
        before = float(before_ts)
    except (TypeError, ValueError):
        return False
    for m in messages:
        try:
            if float(m.get("ts", "0") or 0) < before and _is_cora(m, bot_id):
                return True
        except (TypeError, ValueError):
            continue
    return False


# ── Candidate model ─────────────────────────────────────────────────────────────

@dataclass
class Candidate:
    channel_id: str
    channel_name: str
    is_dm: bool
    user_id: str
    text: str
    event_ts: str
    reply_thread_ts: Optional[str]      # thread to post the reply into
    root_thread_ts: Optional[str]       # thread root for context / registration
    detection_tier: str
    # Filled by generate_draft:
    status: str = "pending"             # draft|decline|redirect|help|error|stale
    draft_text: str = ""
    note: str = ""
    entity: str = ""
    tier: str = ""

    @property
    def catchup_id(self) -> str:
        return catchup_id(self.channel_id, self.event_ts)


def catchup_id(channel_id: str, event_ts: str) -> str:
    return f"{channel_id}:{event_ts}"


# ── Detection ────────────────────────────────────────────────────────────────────

_FUZZY_CORA_RE = re.compile(r"\bcora\b", re.IGNORECASE)


def _mentions_cora(text: str, bot_id: Optional[str]) -> bool:
    if not bot_id:
        return False
    return f"<@{bot_id}>" in (text or "")


def find_missed_messages(
    client,
    channels: list[dict],
    oldest: float,
    latest: float,
    *,
    bot_id: Optional[str],
    staleness_hours: float = 24.0,
    include_fuzzy: bool = False,
    now: Optional[float] = None,
    already_seen_fn: Optional[Callable[[str], bool]] = None,
    still_open_fn: Optional[Callable[[str, list[str]], bool]] = None,
) -> list[Candidate]:
    """Reconstruct the set of asks Cora missed in [oldest, latest].

    v1 high-precision qualification (any of): @mentions Cora, is a DM to Cora, or is
    an in-thread reply in a thread Cora already participated in. Fuzzy (bare-"cora"
    directed asks) only when include_fuzzy=True, tagged lower-confidence.

    A candidate is dropped when: Cora already replied after it (answered); the ledger
    already holds ANY row for it (idempotency -- so a re-run before Harrison actions the
    first batch does NOT re-surface still-pending cards, D-051); or a still-open
    classifier says it was self-resolved. Silent "do-not-respond" channels are skipped
    entirely (mirrors handle_mention's is_silent_channel early-return). Older-than-
    staleness asks are surfaced with status="stale" (not drafted). Answered/seen/resolved
    are dropped silently.
    """
    now = now if now is not None else time.time()
    already_seen_fn = already_seen_fn or (lambda cid: latest_disposition(cid) is not None)
    stale_cutoff = now - staleness_hours * 3600.0
    out: list[Candidate] = []
    replies_cache: dict = {}

    for ch in channels:
        cid = ch["id"]
        is_dm = ch.get("is_dm", False)
        name = "dm" if is_dm else ch.get("name", "")
        # Silent feed channels: the live handlers never speak here (is_silent_channel
        # early-returns), so a reconstructed ask must never become a postable draft.
        if not is_dm and entity_router.is_silent_channel(name):
            log.info("catchup: channel #%s is silent -- skipped", name)
            continue
        try:
            window_msgs = fetch_window_messages(client, cid, oldest, latest)
        except Exception as exc:  # noqa: BLE001
            log.warning("catchup: window fetch failed for %s: %s", cid, exc)
            continue

        # For DMs there is no thread structure -- the whole (up-to-now) history is the
        # "answered" search space. For channels we consult per-thread replies.
        dm_history = None
        if is_dm:
            try:
                dm_history = _dm_history_to_now(client, cid)
            except Exception:  # noqa: BLE001
                dm_history = window_msgs

        for m in window_msgs:
            u = m.get("user", "")
            text = (m.get("text") or "").strip()
            mts = m.get("ts", "")
            if not u or u == bot_id or not text:
                continue

            thread_root = m.get("thread_ts")
            is_thread_reply = bool(thread_root) and thread_root != mts

            tier = None
            reply_thread_ts: Optional[str] = None
            root_thread_ts: Optional[str] = None

            if is_dm:
                tier = TIER_DM
                reply_thread_ts = thread_root if is_thread_reply else None
                root_thread_ts = thread_root if is_thread_reply else mts
            elif _mentions_cora(text, bot_id):
                tier = TIER_MENTION
                root_thread_ts = thread_root or mts
                reply_thread_ts = root_thread_ts
            elif is_thread_reply:
                replies = _thread_replies(client, cid, thread_root, replies_cache)
                if cora_participated_before(replies, mts, bot_id):
                    tier = TIER_THREAD
                    root_thread_ts = thread_root
                    reply_thread_ts = thread_root
            elif include_fuzzy and _FUZZY_CORA_RE.search(text):
                tier = TIER_FUZZY
                root_thread_ts = thread_root or mts
                reply_thread_ts = root_thread_ts

            if tier is None:
                continue

            cid_key = catchup_id(cid, mts)
            if already_seen_fn(cid_key):
                continue

            # Already answered? (look forward to NOW, not just window end)
            if is_dm:
                search_space = dm_history or window_msgs
            else:
                search_space = _thread_replies(client, cid, root_thread_ts or mts, replies_cache)
            if cora_replied_after(search_space, mts, bot_id):
                continue

            # Self-resolved / later-answered? Fail-closed (unsure -> surface).
            if still_open_fn is not None:
                try:
                    following = _following_texts(search_space, mts)
                    if not still_open_fn(text, following):
                        continue
                except Exception:  # noqa: BLE001
                    pass  # fail-closed: keep the candidate

            cand = Candidate(
                channel_id=cid,
                channel_name=name,
                is_dm=is_dm,
                user_id=u,
                text=_strip_mention(text),
                event_ts=mts,
                reply_thread_ts=reply_thread_ts,
                root_thread_ts=root_thread_ts,
                detection_tier=tier,
            )
            try:
                if float(mts) < stale_cutoff:
                    cand.status = "stale"
                    cand.note = f"Original is older than {staleness_hours:g}h -- default skip."
            except (TypeError, ValueError):
                pass
            out.append(cand)

        if not is_dm:
            time.sleep(_CHANNEL_SLEEP)

    return out


def _dm_history_to_now(client, channel_id: str, limit: int = 200) -> list[dict]:
    out: list[dict] = []
    cursor = None
    try:
        while len(out) < 400:
            kwargs: dict = {"channel": channel_id, "limit": limit}
            if cursor:
                kwargs["cursor"] = cursor
            resp = client.conversations_history(**kwargs)
            out.extend(resp.get("messages", []))
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not resp.get("has_more") or not cursor:
                break
            time.sleep(_PAGE_SLEEP)
    except Exception as exc:  # noqa: BLE001
        log.warning("catchup: dm history failed for %s: %s", channel_id, exc)
    return out


def _following_texts(messages: list[dict], after_ts: str, cap: int = 8) -> list[str]:
    try:
        after = float(after_ts)
    except (TypeError, ValueError):
        return []
    texts: list[str] = []
    for m in sorted(messages, key=lambda x: float(x.get("ts", "0") or 0)):
        try:
            if float(m.get("ts", "0") or 0) > after:
                t = (m.get("text") or "").strip()
                if t:
                    texts.append(t)
        except (TypeError, ValueError):
            continue
        if len(texts) >= cap:
            break
    return texts


def _strip_mention(text: str) -> str:
    return _MENTION_RE.sub("", text or "").strip()


# ── Still-open classifier (Haiku, fail-closed) ───────────────────────────────────

def classify_still_open(question: str, following: list[str]) -> bool:
    """True if the question still needs an answer; fail-closed True on any doubt.

    Cheap Haiku call. If no following messages, it is trivially still open. On any
    API/parse error or missing key, returns True (surface for review, never auto-skip).
    """
    if not following:
        return True
    try:
        import anthropic  # lazy
    except Exception:  # noqa: BLE001
        return True
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return True
    convo = "\n".join(f"- {t[:300]}" for t in following[:6])
    prompt = (
        "A question was asked in a team chat. Cora (an assistant) was offline and "
        "never answered it. Below are the messages that came AFTER the question.\n\n"
        f"QUESTION:\n{question[:600]}\n\n"
        f"MESSAGES AFTER IT:\n{convo}\n\n"
        "Was the question already resolved by those later messages (someone answered "
        "it, or the asker said never mind / figured it out)? Reply with exactly one "
        "word: RESOLVED or OPEN."
    )
    try:
        c = anthropic.Anthropic()
        resp = c.messages.create(
            model=MODEL_HAIKU,
            max_tokens=8,
            messages=[{"role": "user", "content": prompt}],
        )
        out = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip().upper()
        return not out.startswith("RESOLVED")
    except Exception as exc:  # noqa: BLE001
        log.warning("catchup: still-open classify failed (%s) -- fail-closed open", exc)
        return True


# ── Guard replication + draft generation (wraps app._dispatch_qa) ─────────────────

def _resolve_source_context(cand: Candidate) -> tuple[str, str, str, bool]:
    """Return (entity, function, tier, is_founder_asker) exactly as the live handlers do."""
    if cand.is_dm:
        role = org_roles.get_role(cand.user_id)
        entity = ((getattr(role, "entity", "") or "").strip()) or "FNDR"
        if user_access.has_unrestricted_entity_access(cand.user_id):
            entity = "HJRG"
        if cand.user_id == HARRISON_ID:
            entity = "FNDR"
        function = channel_classifier.classify_function("dm")
        tier = "TIER_3"  # DMs are hard-pinned TIER_3 (roster-independent)
    else:
        entity = entity_router.route(cand.channel_name)
        function = channel_classifier.classify_function(cand.channel_name)
        tier = channel_classifier.tier_label(entity, function)
    return entity, function, tier, (cand.user_id == HARRISON_ID)


def generate_draft(client, cand: Candidate, *, draft_answer: bool = True) -> Candidate:
    """Populate cand.status/draft_text by replicating the handler guards, then
    (when draft_answer) wrapping app._dispatch_qa to capture the answer WITHOUT posting.

    Sets:
      status="draft"       -> draft_text is the answer Cora would have posted
      status="would_draft" -> guards pass but answer generation was skipped (--no-draft)
      status="decline"     -> user_access refusal (would decline live) -- note carries it
      status="redirect"    -> sibling/cross-entity redirect (would deflect live)
      status="help"         -> help-intent; not a real ask
      status="error"       -> pipeline produced no text
    (status="stale" is set upstream in find_missed_messages and short-circuits here.)
    """
    if cand.status == "stale":
        return cand

    entity, _function, tier, is_founder = _resolve_source_context(cand)
    cand.entity = entity
    cand.tier = tier
    is_dm = cand.is_dm
    phi_custodian = lex_phi_access.phi_allowed(cand.user_id, entity, is_dm=is_dm)

    # ── Pre-LLM handler guard sequence (order matches app.py handlers) ──────────
    # rate_limiter is intentionally NOT run: it is an abuse throttle keyed on live
    # per-user volume, not an entity/PHI/finance firewall, and this is an offline batch.
    access_block = user_access.check_access(
        cand.user_id, entity, cand.text, phi_custodian=phi_custodian, tier=tier,
    )
    if access_block:
        cand.status = "decline"
        cand.note = access_block
        return cand
    if help_responder.is_help_intent(cand.text):
        cand.status = "help"
        cand.note = "Help-intent message (not a substantive question)."
        return cand
    sibling_redirect = sibling_guard.check_redirect(entity, cand.text)
    if sibling_redirect:
        cand.status = "redirect"
        cand.note = sibling_redirect
        return cand
    cross_redirect = cross_entity_guard.check_cross_entity(cand.text, entity)
    if cross_redirect:
        cand.status = "redirect"
        cand.note = cross_redirect
        return cand

    if not draft_answer:
        cand.status = "would_draft"
        cand.note = "Guards pass; answer not generated (--no-draft)."
        return cand

    # ── Wrap _dispatch_qa: capture the answer, post nothing, mutate no live state ─
    draft = _run_dispatch_capture(client, cand, entity, is_founder)
    if draft and draft.strip():
        cand.status = "draft"
        cand.draft_text = draft.strip()
    else:
        cand.status = "error"
        cand.note = "Pipeline produced no answer text."
    return cand


def _run_dispatch_capture(client, cand: Candidate, entity: str, is_founder: bool) -> str:
    """Call app._dispatch_qa with a capturing say + client proxy; return captured text.

    READ-ONLY reconstruction (D-051 remediation). For the duration of the _dispatch_qa
    call, in THIS process only:
      * CORA_EVAL_MODE=1 -> tools_for_entity returns [], dispatch() refuses, AND the
        F-23 confirm interceptor (try_confirm_pending_write) short-circuits, so NO tool
        or staged-write executes. This is the critical guard: without it a reconstructed
        confirm-shaped message ("yes") after a pre-outage proposal would drive a real
        calendar invite / gmail draft / tracker write. Trade-off: drafts
        are KB/context-only -- a tool-backed answer (live finance, plate, calendar read)
        drafts as "couldn't access that", which Harrison sees on the card and can Skip.
      * active_thread_store.register + _try_cache_store -> no-ops (never register a stale
        active thread; never poison the shared semantic cache).
      * gap_detection.maybe_log_gap + knowledge_gaps.log_gap + uft.log_knowledge_gap ->
        no-ops (a reconstruction must not seed knowledge-gap rows that later escalate a
        DM to a domain owner; CORA_EVAL_MODE covers the no-sentinel path, these cover the
        sentinel path too).
      * user_facing_message -> a sentinel, so a swallowed ClaudeClientError is detected
        as an error (returns "") instead of captured as a valid draft.
    Returns "" on empty/error (generate_draft maps that to status="error"). Runs in the
    RUNNER process, never the bot.
    """
    from cora import app as _app  # lazy: breaks the app<->module import cycle

    holder: dict = {"text": None}

    def cap_say(**kwargs):
        t = kwargs.get("text", "")
        if t and t != _STREAM_PLACEHOLDER:
            holder["text"] = t
        return {"ts": "catchup-draft", "channel": cand.channel_id}

    class _CapClient:
        def __init__(self, real):
            object.__setattr__(self, "_real", real)

        def chat_update(self, **kwargs):
            t = kwargs.get("text")
            if t and t != _STREAM_PLACEHOLDER:
                holder["text"] = t
            return {"ok": True, "ts": kwargs.get("ts"), "channel": kwargs.get("channel")}

        def __getattr__(self, name):
            return getattr(object.__getattribute__(self, "_real"), name)

    # Prior thread/DM context, reusing the live history builders.
    prior: list[dict] = []
    try:
        if cand.is_dm and not cand.reply_thread_ts:
            prior = _app._fetch_dm_history(client, cand.channel_id, cand.event_ts)
        elif cand.root_thread_ts:
            prior = _app._fetch_thread_history(
                client, cand.channel_id, cand.root_thread_ts, cand.event_ts,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("catchup: history assembly failed for %s: %s", cand.catchup_id, exc)

    _prev_eval = os.environ.get("CORA_EVAL_MODE")
    _orig = {
        "register": _app.active_thread_store.register,
        "cache": _app._try_cache_store,
        "gap": _app.gap_detection.maybe_log_gap,
        "klog": _app.knowledge_gaps.log_gap,
        "uft": _app.uft.log_knowledge_gap,
        "ufm": _app.user_facing_message,
    }
    os.environ["CORA_EVAL_MODE"] = "1"
    _app.active_thread_store.register = lambda *a, **k: None
    _app._try_cache_store = lambda *a, **k: None
    _app.gap_detection.maybe_log_gap = lambda *a, **k: None
    _app.knowledge_gaps.log_gap = lambda *a, **k: None
    _app.uft.log_knowledge_gap = lambda *a, **k: None
    _app.user_facing_message = lambda exc: _PIPELINE_ERROR_SENTINEL
    try:
        _app._dispatch_qa(
            channel_id=cand.channel_id,
            channel_name=cand.channel_name,
            user_id=cand.user_id,
            user_message=cand.text,
            reply_thread_ts=cand.reply_thread_ts,
            entity=entity,
            client=_CapClient(client),
            say=cap_say,
            prior_messages=prior,
            root_thread_ts=cand.root_thread_ts,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("catchup: _dispatch_qa capture failed for %s: %s", cand.catchup_id, exc)
    finally:
        if _prev_eval is None:
            os.environ.pop("CORA_EVAL_MODE", None)
        else:
            os.environ["CORA_EVAL_MODE"] = _prev_eval
        _app.active_thread_store.register = _orig["register"]
        _app._try_cache_store = _orig["cache"]
        _app.gap_detection.maybe_log_gap = _orig["gap"]
        _app.knowledge_gaps.log_gap = _orig["klog"]
        _app.uft.log_knowledge_gap = _orig["uft"]
        _app.user_facing_message = _orig["ufm"]

    text = holder.get("text") or ""
    if text == _PIPELINE_ERROR_SENTINEL:
        return ""  # swallowed ClaudeClientError -> status="error", no Send button
    return text


# ── Review card (Harrison DM) ─────────────────────────────────────────────────────

def scrub_card_body(
    body: str,
    *,
    source_entity: str,
    source_tier: str,
    source_channel_name: str,
    source_is_dm: bool,
    asker_id: str = "",
) -> str:
    """Scrub a source-message body for display on Harrison's DM review card.

    The card is an OUTBOUND surface (spec doctrine: outbound twin of the retrieval
    scrub). guard_outbound keyed on the CARD's DM surface would wrongly exempt
    confidential classes, so we evaluate against the SOURCE channel context and, if a
    confidential class trips, withhold the body. LEX-scope bodies are additionally
    PHI-scrubbed (maximal redaction -- allowed_names=None -- since this is a judgment
    surface, not the answer).
    """
    body = (body or "").strip()
    if not body:
        return "(empty)"
    try:
        _guarded, tripped = channel_content_guard.guard_outbound(
            body, entity=source_entity, tier=source_tier,
            channel_name=source_channel_name, user_id=asker_id, is_dm=source_is_dm,
        )
        if tripped:
            return f"[original withheld on this review surface -- {tripped} content]"
    except Exception:  # noqa: BLE001
        pass
    if source_entity == "LEX" or str(source_entity).startswith("LEX-"):
        try:
            body = phi_guard.redact_cue_adjacent_names(body)
            body = phi_guard.scrub_lex_phi(body)
        except Exception:  # noqa: BLE001
            return "[original withheld on this review surface -- LEX content]"
    return body


def _asker_label(user_id: str) -> str:
    try:
        return user_identity.display_name(user_id) or user_id
    except Exception:  # noqa: BLE001
        return user_id


def build_review_card(cand: Candidate) -> tuple[str, list[dict]]:
    """(fallback_text, blocks) for one review card with Send / Edit / Skip buttons.

    Only status=="draft" cards get action buttons. decline/redirect/help/error/stale
    are shown as context-only (no draft to post).
    """
    uid = cand.catchup_id
    asker = _asker_label(cand.user_id)
    where = "your DM" if cand.is_dm else f"#{cand.channel_name}"
    when = _fmt_ts(cand.event_ts)
    scrubbed = scrub_card_body(
        cand.text, source_entity=cand.entity or "FNDR", source_tier=cand.tier or "TIER_3",
        source_channel_name=cand.channel_name, source_is_dm=cand.is_dm, asker_id=cand.user_id,
    )

    header = (
        f"*Missed message* -- {asker} in {where} ({when}) "
        f"| detection: {cand.detection_tier}"
    )
    orig = f"*They asked:*\n>{_blockquote(scrubbed)}"

    if cand.status == "draft":
        text = f"{header}\n\n{orig}\n\n*Draft reply:*\n{cand.draft_text[:2500]}"
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}},
            {
                "type": "actions",
                "block_id": f"catchup_actions_{uid}"[:255],
                "elements": [
                    {"type": "button", "action_id": ACTION_SEND, "style": "primary",
                     "text": {"type": "plain_text", "text": "✅ Send"}, "value": uid},
                    {"type": "button", "action_id": ACTION_EDIT,
                     "text": {"type": "plain_text", "text": "✏️ Edit"}, "value": uid},
                    {"type": "button", "action_id": ACTION_SKIP,
                     "text": {"type": "plain_text", "text": "\U0001f5d1️ Skip"}, "value": uid},
                ],
            },
        ]
        return text, blocks

    reason = {
        "decline": "Cora would DECLINE this live (access/finance/PHI guard) -- not drafting.",
        "redirect": "Cora would REDIRECT this live (sibling/cross-entity) -- not drafting.",
        "help": "Help-intent message -- no substantive answer needed.",
        "error": "Pipeline produced no answer -- nothing to send.",
        "would_draft": "Guards pass -- re-run without --no-draft to generate the reply.",
        "stale": cand.note or "Stale -- default skip.",
    }.get(cand.status, cand.note or cand.status)
    text = f"{header}\n\n{orig}\n\n_{reason}_ _(no action needed)_"
    return text, [{"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}]


def _blockquote(text: str) -> str:
    return (text or "").replace("\n", "\n>")[:800]


def _fmt_ts(ts: str) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).astimezone().strftime("%b %d %H:%M")
    except Exception:  # noqa: BLE001
        return str(ts)


# ── Idempotency ledger (append-only; latest row per catchup_id wins) ──────────────

def _ledger_path() -> Path:
    return Path(os.environ.get("MISSED_CATCHUP_LEDGER_PATH") or _DEFAULT_LEDGER_PATH)


def _iter_ledger_rows(path: Path):
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict) and "_schema" not in row:
            yield row


def latest_disposition(cid: str) -> Optional[dict]:
    """Most recent ledger row for a catchup_id (or None)."""
    latest = None
    for row in _iter_ledger_rows(_ledger_path()):
        if row.get("catchup_id") == cid:
            latest = row
    return latest


def is_terminal(cid: str) -> bool:
    """STICKY: True if ANY ledger row for this id is terminal.

    D-051 fix: latest-row-wins alone let a re-run append a 'pending' row AFTER a
    terminal 'sent' row (two overlapping --send-cards runs), re-arming an already-posted
    item for a double-post. Scanning for any terminal row makes a sent/skipped item
    permanently closed regardless of later appends.
    """
    for row in _iter_ledger_rows(_ledger_path()):
        if row.get("catchup_id") == cid and row.get("disposition") in _TERMINAL:
            return True
    return False


def record_row(
    cid: str,
    disposition: str,
    *,
    run_id: str = "",
    channel_id: str = "",
    channel_name: str = "",
    entity: str = "",
    tier: str = "",
    asker: str = "",
    event_ts: str = "",
    reply_thread_ts: Optional[str] = None,
    detection_tier: str = "",
    draft_text: str = "",
    is_dm: bool = False,
    posted_ts: str = "",
    note: str = "",
) -> bool:
    """Append one ledger row (best-effort, never raises)."""
    path = _ledger_path()
    try:
        now = time.time()
        row = {
            "catchup_id": cid,
            "disposition": disposition,
            "run_id": run_id,
            "channel_id": channel_id,
            "channel_name": channel_name,
            "entity": entity,
            "tier": tier,
            "asker": asker,
            "event_ts": event_ts,
            "reply_thread_ts": reply_thread_ts,
            "detection_tier": detection_tier,
            "draft_text": draft_text,
            "is_dm": is_dm,
            "posted_ts": posted_ts,
            "note": note,
            "ts": now,  # epoch, for age-pruning
            "decided_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("catchup ledger append failed for %s: %s", cid, exc)
        return False


def prune_ledger(max_age_days: float = 90.0, *, now: Optional[float] = None) -> int:
    """Rewrite the ledger dropping rows older than max_age_days (best-effort).

    Keeps the at-rest store bounded -- the pending rows carry drafted answer text
    (host-local, encrypted disk, Harrison-review-only), so age-cap it. Rows missing a
    parseable ts are kept (fail-safe). Returns the number of rows dropped.
    """
    path = _ledger_path()
    if not path.exists():
        return 0
    now = now if now is not None else time.time()
    cutoff = now - max_age_days * 86400.0
    kept: list[str] = []
    dropped = 0
    try:
        for row in _iter_ledger_rows(path):
            ts = row.get("ts")
            try:
                if ts is not None and float(ts) < cutoff:
                    dropped += 1
                    continue
            except (TypeError, ValueError):
                pass
            kept.append(json.dumps(row))
        if dropped:
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
            tmp.replace(path)
    except Exception as exc:  # noqa: BLE001
        log.warning("catchup ledger prune failed: %s", exc)
        return 0
    return dropped


def record_pending(cand: Candidate, run_id: str, posted_ts: str = "") -> bool:
    """Persist a drafted candidate as PENDING so the (separate-process) bot button
    handler can post it later. Carries everything the handler needs.

    Refuses to re-arm an already-terminal item (D-051 double-post guard): if a
    concurrent run already got it sent/skipped, do not append a fresh 'pending' row.
    """
    if is_terminal(cand.catchup_id):
        log.info("catchup: %s already terminal -- not re-arming pending", cand.catchup_id)
        return False
    return record_row(
        cand.catchup_id,
        "pending",
        run_id=run_id,
        channel_id=cand.channel_id,
        channel_name=cand.channel_name,
        entity=cand.entity,
        tier=cand.tier,
        asker=cand.user_id,
        event_ts=cand.event_ts,
        reply_thread_ts=cand.reply_thread_ts,
        detection_tier=cand.detection_tier,
        draft_text=cand.draft_text,
        is_dm=cand.is_dm,
        posted_ts=posted_ts,
        note=cand.note,
    )


# ── Approval processor (bot-loaded path; called from app.py @app.action) ──────────

def _pending_row(cid: str) -> Optional[dict]:
    row = latest_disposition(cid)
    if row and row.get("disposition") == "pending":
        return row
    return None


def _apply_preface(text: str) -> str:
    if len(text) >= _PREFACE_MIN_CHARS and CATCHUP_PREFACE:
        return f"{CATCHUP_PREFACE}\n\n{text}"
    return text


def post_approved_reply(client, row: dict, *, edited_text: Optional[str] = None) -> Optional[str]:
    """Post the (possibly edited) approved reply into the SOURCE thread as Cora.

    Re-runs channel_content_guard against the SOURCE channel context (belt-and-suspenders,
    and load-bearing when Harrison EDITED the text), applies the delay preface to
    non-trivial answers, posts in-thread (egress sanitize is automatic via the WebClient
    class patch), then registers the source thread active so follow-ups work.
    Returns the posted message ts, or None on failure.
    """
    text = (edited_text if edited_text is not None else row.get("draft_text") or "").strip()
    if not text:
        return None
    entity = row.get("entity") or "FNDR"
    tier = row.get("tier") or "TIER_3"
    channel_id = row.get("channel_id") or ""
    channel_name = row.get("channel_name") or ""
    is_dm = bool(row.get("is_dm"))
    reply_thread_ts = row.get("reply_thread_ts")

    try:
        guarded, _tripped = channel_content_guard.guard_outbound(
            text, entity=entity, tier=tier, channel_name=channel_name,
            user_id="", is_dm=is_dm,
        )
        text = guarded
    except Exception as exc:  # noqa: BLE001
        log.warning("catchup: post-time guard failed for %s: %s", row.get("catchup_id"), exc)

    text = _apply_preface(text)
    kwargs: dict = {"channel": channel_id, "text": text,
                    "unfurl_links": False, "unfurl_media": False}
    if reply_thread_ts:
        kwargs["thread_ts"] = reply_thread_ts
    try:
        resp = client.chat_postMessage(**kwargs)
    except Exception as exc:  # noqa: BLE001
        log.warning("catchup: post failed for %s: %s", row.get("catchup_id"), exc)
        return None

    posted_ts = resp.get("ts", "")
    # Register the thread active now that a real reply landed (mirrors _dispatch_qa).
    try:
        from cora import app as _app  # lazy
        register_ts = reply_thread_ts or posted_ts
        if register_ts:
            _app.active_thread_store.register(channel_id, register_ts)
    except Exception:  # noqa: BLE001
        pass
    return posted_ts


def process_catchup_action(
    cid: str, actor_id: str, client, *, action: str, edited_text: Optional[str] = None,
) -> tuple[str, str]:
    """Harrison-gated one-tap processor for a review card. Returns (outcome, message).

    outcome in {not_authorized, not_found, already_resolved, sent, edited_sent,
    skipped, post_failed}. All state mutation happens under _ONE_TAP_LOCK with a
    re-read of the ledger inside the lock (apply-then-record) so a double-tap or a
    crash can never double-post (D-030 idempotency, D-011 Harrison-only).
    """
    if actor_id != HARRISON_ID:
        log.warning("catchup: one-tap action by non-Harrison %s ignored", actor_id)
        return "not_authorized", "Only Harrison can approve catch-up replies."

    with _ONE_TAP_LOCK:
        if is_terminal(cid):
            return "already_resolved", "That catch-up item was already handled."
        row = _pending_row(cid)
        if row is None:
            return "not_found", "That catch-up item is no longer available."

        if action == "skip":
            record_row(cid, "skipped", run_id=row.get("run_id", ""),
                       channel_id=row.get("channel_id", ""),
                       channel_name=row.get("channel_name", ""),
                       entity=row.get("entity", ""), tier=row.get("tier", ""),
                       asker=row.get("asker", ""), event_ts=row.get("event_ts", ""),
                       detection_tier=row.get("detection_tier", ""),
                       is_dm=bool(row.get("is_dm")), note="skipped by Harrison")
            return "skipped", "Skipped -- nothing posted."

        if action == "send":
            posted_ts = post_approved_reply(client, row, edited_text=edited_text)
            if not posted_ts:
                return "post_failed", "Posting failed -- the item is still pending, try again."
            disp = "edited_sent" if edited_text is not None else "sent"
            record_row(cid, disp, run_id=row.get("run_id", ""),
                       channel_id=row.get("channel_id", ""),
                       channel_name=row.get("channel_name", ""),
                       entity=row.get("entity", ""), tier=row.get("tier", ""),
                       asker=row.get("asker", ""), event_ts=row.get("event_ts", ""),
                       reply_thread_ts=row.get("reply_thread_ts"),
                       detection_tier=row.get("detection_tier", ""),
                       is_dm=bool(row.get("is_dm")),
                       draft_text=(edited_text if edited_text is not None else row.get("draft_text", "")),
                       posted_ts=posted_ts,
                       note=("edited + sent" if edited_text is not None else "sent"))
            where = "your DM" if row.get("is_dm") else f"#{row.get('channel_name')}"
            verb = "Edited + posted" if edited_text is not None else "Posted"
            return disp, f"{verb} to {where}."

    return "not_found", "Unknown action."


def edit_modal_view(cid: str, dm_channel: str, dm_ts: str, draft_text: str) -> dict:
    """Slack modal (views.open) prefilled with the draft for the Edit button."""
    meta = json.dumps({"catchup_id": cid, "dm_channel": dm_channel, "dm_ts": dm_ts})
    return {
        "type": "modal",
        "callback_id": VIEW_EDIT_SUBMIT,
        "private_metadata": meta[:3000],
        "title": {"type": "plain_text", "text": "Edit catch-up reply"},
        "submit": {"type": "plain_text", "text": "Send"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "input",
                "block_id": "catchup_edit_block",
                "label": {"type": "plain_text", "text": "Reply text"},
                "element": {
                    "type": "plain_text_input",
                    "action_id": "catchup_edit_input",
                    "multiline": True,
                    "initial_value": (draft_text or "")[:2900],
                },
            }
        ],
    }
