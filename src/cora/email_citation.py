"""S1 (cq-551fada9dee8): last-message date + direction on every email citation.

The email-thread-review doctrine's section 6 names this file's job: "Cora's
automated Gmail passes (filer, sweeps, any read path) are bound by this
doctrine... code-level enforcement is a staged code-queue item... Until that
ships, treat Cora email citations without a last-message date as unverified."

VERIFY-FIRST SPLIT THE SLICE IN HALF, and four of the seed's named modules were
wrong. `tools/gmail_client.py` is WRITE-ONLY (scope gmail.compose, its only API
call is drafts().create). `missed_message_catchup` reconstructs Slack, not email.
`delegated_worker` never touches Gmail. And `revops/sweep.py` is not a defect at
all -- it is the REFERENCE implementation, already documented as reading "every
tracked thread to the LAST message (full-thread doctrine)", and its `_is_outbound`
is the only correct direction rule in the repo. It is MOVED here rather than
copied, so the SENT-label-is-authoritative reasoning cannot fork.

WHAT IS ACTUALLY MISSING is the CITATION half, and it is missing everywhere.
`grep 'def format_.*_for_llm'` returns 22 helpers and exactly one is Gmail -- a
draft-confirmation renderer. There is no shared email formatter; the formatting
is inline at five unrelated sites, and not one of them states a last-message date
or a direction:

  context_loader._format_kb_chunks      the highest-volume context block there is
  historical_access.format_owned_chunks
  finance_receipts.format_finance_chunks   a near-verbatim clone of the above
  tools/person_dossier._gmail_block        COLLAPSES direction outright
  run_daily_briefing._recent_activity      does not even SELECT the date

AND THE KB IS PER-MESSAGE. The sweep reads the full thread correctly, then emits
one Document per MESSAGE with that message's own date. Vector retrieval therefore
returns the best-MATCHING message, never the thread's LAST -- so every downstream
surface prints a mid-thread date as if it were current state. That is the D-Backs
/ Fuoco failure class the doctrine's section 1 documents, reproduced structurally.

The fix is a stamp at INGEST (where the whole thread is already in hand, at zero
extra API cost) plus one shared renderer, rather than N call-site rewrites or a
live API call on the read path.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

# Keep the rendered fragment short: it rides _format_kb_chunks, which is on every
# Q&A prompt in the bot.
_MAX_NAME = 24


def _addr_of(value: str) -> str:
    """The bare address out of a From/To header value."""
    m = re.search(r"<([^>]+)>", str(value or ""))
    return (m.group(1) if m else str(value or "")).strip().lower()


def _display_name(value: str) -> str:
    """A short human name for a header value, falling back to the mailbox."""
    raw = str(value or "").strip()
    m = re.match(r'^\s*"?([^"<]+?)"?\s*<', raw)
    name = (m.group(1) if m else "").strip()
    if not name:
        name = _addr_of(raw).split("@")[0].replace(".", " ").title()
    return name[:_MAX_NAME]


def is_outbound(msg: dict[str, Any], own: set[str]) -> bool:
    """Direction from Gmail's own SENT label when present; the From header only
    as a fallback for readers that omit labels.

    MOVED from revops/sweep.py verbatim, not copied -- that module now imports
    this one. A From header is attacker-spoofable and a Gmail label is not, and
    an EMPTY label list is still authoritative ("Gmail says not sent"), so this
    tests for PRESENCE of the key and never truthiness: `[] or fallback` would
    hand a spoofed From header the decision.
    """
    labels = msg.get("label_ids")
    if labels is None:
        labels = msg.get("labelIds")
    if labels is not None:
        return "SENT" in labels
    return _addr_of(msg.get("sender")) in (own or set())


def thread_stamp(messages: list[dict[str, Any]],
                 own: set[str] | None = None) -> dict[str, Any]:
    """Thread-level facts, computed ONCE per thread at ingest.

    Merged into every chunk's metadata so the read path can cite the thread's
    LAST message without a query or an API call -- and so a mid-thread chunk
    retrieved by vector match still reports current state rather than its own
    stale date.
    """
    msgs = [m for m in (messages or []) if isinstance(m, dict)]
    if not msgs:
        return {}
    last = msgs[-1]
    return {
        "thread_msg_count": len(msgs),
        "thread_last_ts": int(last.get("internal_ts") or last.get("date_ts") or 0),
        "thread_last_from": _display_name(last.get("sender", "")),
        "thread_last_direction": ("outbound" if is_outbound(last, own or set())
                                  else "inbound"),
    }


def _fmt_date(ts: int | float | None) -> str:
    try:
        if not ts:
            return ""
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%-m/%-d")
    except Exception:  # noqa: BLE001 -- platform without %-m
        try:
            return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%m/%d").lstrip("0").replace("/0", "/")
        except Exception:  # noqa: BLE001
            return ""


def cite(*, last_ts: int | float | None = None, last_direction: str = "",
         last_from: str = "", msg_index: int | None = None,
         msg_count: int | None = None, deidentified: bool = False) -> str:
    """THE citation fragment. "" when there is nothing honest to say.

    Identified:      "last msg 8/21 inbound from Laura - msg 3 of 12"
    De-identified:   "last activity 8/21, inbound"   (Tier-1-stripped chunks)
    Missing stamp:   "thread position unknown (pre-stamp chunk)"

    The de-identified form exists because historical_access.strip_result nulls
    the metadata, the date AND the author on a non-owner chunk -- correct privacy
    behaviour, and it means a compliant citation is otherwise IMPOSSIBLE on
    exactly the chunks most likely to be cited. This ADDS a recency marker; it
    never restores headers.
    """
    day = _fmt_date(last_ts)
    direction = (last_direction or "").strip().lower()
    if direction not in ("inbound", "outbound"):
        direction = ""
    if not day and not direction:
        return "thread position unknown (pre-stamp chunk)"
    if deidentified:
        parts = [f"last activity {day}"] if day else ["last activity unknown"]
        if direction:
            parts.append(direction)
        return ", ".join(parts)
    out = f"last msg {day}" if day else "last msg (date unknown)"
    if direction:
        out += f" {direction}"
    # Named only on INBOUND. On an outbound message `last_from` is US, and
    # "outbound from Harrison" adds nothing while "outbound to Harrison" would
    # be false -- the recipient is not what this field holds.
    if last_from and direction == "inbound":
        out += f" from {str(last_from)[:_MAX_NAME]}"
    if isinstance(msg_index, int) and isinstance(msg_count, int) and msg_count > 1:
        out += f" - msg {msg_index + 1} of {msg_count}"
    return out


def cite_from_metadata(meta: dict[str, Any] | None, *,
                       deidentified: bool = False) -> str:
    """cite() over a chunk's stored metadata. Degrades to the pre-stamp wording
    rather than guessing, so a chunk ingested before the stamp existed is
    HONESTLY unverified rather than silently undated."""
    m = meta if isinstance(meta, dict) else {}
    return cite(
        last_ts=m.get("thread_last_ts"),
        last_direction=str(m.get("thread_last_direction") or ""),
        last_from=str(m.get("thread_last_from") or ""),
        msg_index=m.get("thread_msg_index"),
        msg_count=m.get("thread_msg_count"),
        deidentified=deidentified,
    )
