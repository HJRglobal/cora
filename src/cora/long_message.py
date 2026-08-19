"""Long outbound messages that do not end mid-word (cq-64a8f5e3e654).

THE FAMILY
----------
Briefings on 7/27 and 7/29 ended mid-word ("How can I", "...priorit") while 7/28
and 7/30 completed; the 7/26 strategy memo split the word "for" across two Slack
messages; plate replies clipped mid-link. One family, two independent causes:

  1. A HARD max_tokens cap with NO DETECTION. `stop_reason == "max_tokens"` was
     never compared anywhere in the codebase -- the only acknowledgement of it
     was a comment. The API raises nothing, so a clipped reply is delivered as
     if complete. That is why the defect is length-dependent and looks random:
     short days finish, long days get cut.
  2. NAIVE CHARACTER SLICING at the Slack boundary. `text[:39000]` in
     strategy_memo and channel_synthesis cuts wherever the count lands --
     mid-word, mid-link, mid-number.

Both produce the same symptom and neither leaves a trace, so a reader cannot
tell a truncated message from a short one. Every function here is pure except
`post_long`, which does the posting.

WHY NOT JUST RAISE THE CAP
---------------------------
Because "raise it until it stops happening" is unfalsifiable -- there is no
length at which you know you are done, and the failure stays silent when you are
wrong. Detection makes the cap a performance choice instead of a correctness
one: continue when it binds, and say so when continuation cannot finish.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("cora.long_message")

#: Slack hard-rejects a chat.postMessage `text` over 40k; stay well under so a
#: sanitizer or footer appended downstream cannot push a chunk over the line.
SLACK_TEXT_LIMIT = 38_000

#: Sentence-ish boundary: terminator + whitespace, not inside a decimal or an
#: abbreviation we care about. Deliberately conservative -- a missed boundary
#: falls through to a word split, which is still never mid-word.
_SENTENCE_END = re.compile(r"(?<=[.!?:])\s+")


def _split_oversized_line(line: str, limit: int) -> list[str]:
    """Break one over-long line without cutting a word or a link.

    Order of preference: sentence boundary, then whitespace, then -- only if a
    single unbroken token genuinely exceeds the limit -- a hard cut. A URL is an
    unbroken token, so the hard cut is what a mid-link truncation would look
    like; it is reachable only for a token longer than the whole limit, which no
    real link is.
    """
    out: list[str] = []
    rest = line
    while len(rest) > limit:
        window = rest[:limit]
        cut = -1
        for m in _SENTENCE_END.finditer(window):
            cut = m.end()
        if cut <= 0:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit          # one token longer than the entire limit
        out.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        out.append(rest)
    return out


def split_for_slack(text: str, limit: int = SLACK_TEXT_LIMIT) -> list[str]:
    """Split text into postable chunks on real boundaries. Never mid-word.

    Prefers paragraph breaks, then line breaks, then sentence/word boundaries
    within an over-long line. Returns `[""]`-free output; an empty input yields
    an empty list so callers post nothing rather than an empty message.
    """
    text = text or ""
    if not text.strip():
        return []
    if len(text) <= limit:
        # Return the input VERBATIM when it fits. Splitting is the job; trimming
        # is not, and a caller that deliberately built trailing whitespace (or a
        # sanitizer that left it) must get back exactly what it passed in.
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            pieces = _split_oversized_line(line, limit)
            chunks.extend(pieces[:-1])
            current = pieces[-1] if pieces else ""
            continue
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current.strip():
        chunks.append(current)
    return [c.strip("\n") for c in chunks if c.strip()]


def post_long(client, channel: str, text: str, *, thread_ts: str | None = None,
              limit: int = SLACK_TEXT_LIMIT, **post_kwargs) -> int:
    """Post `text` across as many messages as it needs. Returns the count posted.

    Continuation parts are labelled `(continued N/M)` so a reader can tell a
    multi-part message from two unrelated posts arriving together -- without
    that, part 2 opening mid-sentence reads as a glitch.

    Fail-soft: a failed part is logged and the rest are still attempted, because
    delivering 3 of 4 parts beats delivering none.
    """
    parts = split_for_slack(text, limit)
    if not parts:
        return 0
    total = len(parts)
    sent = 0
    for i, part in enumerate(parts, start=1):
        body = part if total == 1 else f"{part}\n\n_(continued {i}/{total})_"
        kwargs = dict(channel=channel, text=body, **post_kwargs)
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        try:
            client.chat_postMessage(**kwargs)
            sent += 1
        except Exception:  # noqa: BLE001 -- 3 of 4 parts beats none
            log.exception("post_long: part %d/%d failed to post", i, total)
    return sent


# ── truncation detection + continuation ─────────────────────────────────────
def was_truncated(response) -> bool:
    """True when the model stopped because it hit max_tokens.

    The API raises nothing in this case, so without an explicit check a clipped
    reply is indistinguishable from a complete one.
    """
    return getattr(response, "stop_reason", None) == "max_tokens"


CONTINUE_PROMPT = (
    "That reply was cut off at the length limit. Continue it from exactly where "
    "it stopped -- do not repeat anything, do not restart, and do not add a "
    "preamble. If it was cut mid-word, complete that word first."
)

TRUNCATION_NOTICE = "\n\n_(This was cut short at the length limit.)_"


def complete_truncated(
    client,
    *,
    model: str,
    system,
    messages: list[dict],
    first_text: str,
    first_response,
    max_tokens: int,
    max_continuations: int = 2,
    caller: str = "",
    **create_kwargs,
) -> tuple[str, bool]:
    """Continue a max_tokens-truncated completion. Returns ``(text, complete)``.

    ``complete`` is False when the continuation budget ran out and the text is
    STILL truncated -- callers append :data:`TRUNCATION_NOTICE` rather than
    delivering a silent cut. Saying "this was cut short" is a worse-looking
    message and a strictly better one: the reader knows to ask for the rest.

    Bounded on purpose. An unbounded continue loop turns one over-long day into
    an unbounded bill, and the surfaces this serves are scheduled jobs where
    nobody is watching the spend.

    Fail-soft: if a continuation call raises, the text gathered so far is
    returned marked incomplete -- never an exception into a scheduled job.
    """
    text = first_text or ""
    response = first_response
    convo = list(messages)

    for attempt in range(max_continuations):
        if not was_truncated(response):
            return text, True
        log.warning("long_message: %s hit max_tokens (continuation %d/%d)",
                    caller or model, attempt + 1, max_continuations)
        convo = convo + [
            {"role": "assistant", "content": text},
            {"role": "user", "content": CONTINUE_PROMPT},
        ]
        try:
            kwargs = dict(model=model, max_tokens=max_tokens, messages=convo,
                          **create_kwargs)
            # OMIT `system` rather than passing a sentinel: this SDK version has
            # no NOT_GIVEN, and passing None is a type error. A caller with no
            # system prompt simply does not send the field.
            if system:
                kwargs["system"] = system
            response = client.messages.create(**kwargs)
        except Exception:  # noqa: BLE001
            log.exception("long_message: continuation call failed for %s", caller)
            return text, False
        chunk = "".join(
            getattr(b, "text", "") for b in (getattr(response, "content", None) or [])
        )
        if not chunk.strip():
            return text, not was_truncated(response)
        # The model resumes mid-sentence by design, so join WITHOUT a separator
        # unless the seam would weld two words together.
        if text and not text[-1].isspace() and not chunk[:1].isspace() \
                and text[-1].isalnum() and chunk[:1].isalnum():
            text = f"{text}{chunk}"
        else:
            text = f"{text}{chunk}"
        convo = convo[:-2]

    return text, not was_truncated(response)
