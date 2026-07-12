"""Anthropic Claude API client with retry logic + tool-use loop.

Two public entry points:
  - generate_response()           — blocking, returns full text after model completes
  - generate_response_streaming() — same contract + a per-delta update_callback so
                                    callers can progressively edit a Slack message
                                    or surface partial output elsewhere
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import anthropic

from .config import config
from .tools.tool_dispatch import (
    TOOL_DEFINITIONS,
    VERBATIM_TABLE_TOOLS,
    dispatch,
    tools_for_entity,
)

log = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"  # default; per-request override via `model` kwarg.
_MAX_TOKENS = 1024  # lowered 2048→1024 on 2026-05-21 — Cora replies are almost
                    # always <500 tokens; tighter ceiling = faster streaming + lower
                    # tail latency. If a reply gets clipped at max_tokens, bump back up.
_TOOL_DISPATCH_MAX_WORKERS = 4  # parallel cap when an iteration emits multiple
                                # tool_use blocks. Most iterations have 1-2 tools;
                                # 4 covers the pathological case without thrashing.
_TIMEOUT = 60.0  # bumped 25→60 for tool-use loops where the second pass synthesizes
                 # large tool results (e.g. 25-event week calendar). Anthropic SDK has
                 # its own internal retries; we just need to give them headroom.
_RETRY_DELAYS = (1, 2)  # seconds before attempt 1 and attempt 2
_MAX_TOOL_ITERATIONS = 3  # safety cap on tool-use loop

_client: "anthropic.Anthropic | None" = None


def _get_client() -> anthropic.Anthropic:
    """Return the shared Anthropic client, creating it on first use.

    Lazy initialization avoids an import-time failure when the API key is not
    present in the environment (e.g. unit tests, fresh installs).  The client
    is effectively a singleton — once created it is reused for the process
    lifetime.
    """
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    return _client


class ClaudeClientError(Exception):
    """Raised when the Claude API fails after all retries.

    The underlying anthropic exception is preserved on `__cause__` (via
    `raise ... from exc` below) so callers can classify the failure for
    user-facing messages via user_facing_message().
    """


def user_facing_message(exc: ClaudeClientError) -> str:
    """Return a user-readable message for a ClaudeClientError.

    Classifies the underlying anthropic exception and returns a specific
    message per failure mode so the user knows whether to retry, whether
    it's their problem, or whether Harrison needs to intervene.

    Falls back to the generic "trouble reaching Claude" string when the
    underlying exception is unknown or missing.
    """
    underlying = exc.__cause__

    # Anthropic exception hierarchy:
    #   anthropic.APIStatusError  (parent of HTTP-status errors)
    #     - anthropic.AuthenticationError    → 401
    #     - anthropic.PermissionDeniedError  → 403
    #     - anthropic.BadRequestError        → 400
    #     - anthropic.NotFoundError          → 404
    #     - anthropic.RateLimitError         → 429
    #     - anthropic.InternalServerError    → 5xx (including 529 overloaded)
    #   anthropic.APIConnectionError  (network)
    #     - anthropic.APITimeoutError

    if isinstance(underlying, anthropic.APIStatusError):
        status = getattr(underlying, "status_code", 0)
        if status == 529:
            return (
                "Anthropic's API is overloaded right now (HTTP 529). This usually "
                "clears in a minute or two — please retry."
            )
        if status in (401, 403):
            return (
                f"Cora's API key is failing (HTTP {status}). Harrison needs to check "
                f"the Anthropic API key — Cora can't recover from this without intervention."
            )
        if status == 429:
            return (
                "Hit Anthropic's rate limit (HTTP 429). Wait about 30 seconds and "
                "retry — Cora will throttle herself if this keeps happening."
            )
        if status == 400:
            return (
                "Cora sent a request Anthropic didn't accept (HTTP 400). This is "
                "likely a bug — Harrison should check Cora's logs. Try rephrasing "
                "your question in case it helps."
            )
        if status >= 500:
            return (
                f"Anthropic's API is having upstream issues (HTTP {status}). Try "
                f"again in a few minutes."
            )
        # Any other status code we didn't explicitly map
        return (
            f"Claude API returned HTTP {status} — usually transient. Try again in a "
            f"moment; if it persists, Harrison should check the logs."
        )

    if isinstance(underlying, anthropic.APITimeoutError):
        return (
            "Anthropic took too long to respond. Try a shorter or simpler question, "
            "or retry — Cora may have hit a complexity wall on this request."
        )

    if isinstance(underlying, anthropic.APIConnectionError):
        return (
            "Network trouble reaching Anthropic. If this keeps happening, the host "
            "machine's internet might be having issues — Harrison should check."
        )

    # Fallback for any other failure mode (including ClaudeClientError raised
    # without an underlying exception, e.g. the "Tool-use loop exited unexpectedly"
    # path at the bottom of generate_response).
    return "I'm having trouble reaching Claude right now — try again in a moment."


def _is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return True
    if isinstance(exc, anthropic.RateLimitError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code >= 500
    return False


def _create_with_retry(**kwargs) -> anthropic.types.Message:
    """Call messages.create with retry on transient errors."""
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            return _get_client().messages.create(**kwargs)
        except Exception as exc:
            if _is_retryable(exc) and attempt < 2:
                delay = _RETRY_DELAYS[attempt]
                log.warning(
                    "Claude API transient error (attempt %d/3), retrying in %ds: %s",
                    attempt + 1, delay, exc,
                )
                time.sleep(delay)
                last_exc = exc
            else:
                raise ClaudeClientError(f"Claude API error: {exc}") from exc
    raise ClaudeClientError(f"Claude API failed after 3 attempts: {last_exc}") from last_exc


def _extract_text(response: anthropic.types.Message) -> str:
    """Pull text content from a response that may also have tool_use blocks."""
    parts = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(p for p in parts if p).strip()


def _build_cached_system(
    system_prompt: str,
    volatile_context: str,
    static_context: str | None = None,
) -> list[dict]:
    """Build the system field as a cached-block array for prompt caching.

    Two shapes:

    2-block (static_context falsy — legacy/back-compat):
      Block 1: entity system prompt + voice — cached.
      Block 2: the context arg — query-specific, NOT cached.

    3-block (static_context provided — the caching split):
      Block 1: entity system prompt + voice — cached.
      Block 2: static portfolio context (founder CLAUDE.md + entity CLAUDE.md +
               known-answers + dynamic snapshots) — deterministic per entity,
               mtime-stable, CACHED. This is the large static mass (~30K tokens
               for the founder brief alone) that previously rode in the uncached
               block and was re-billed on every mention.
      Block 3: per-query KB chunks + runtime context — query-specific, NOT cached.

    Cache-control rules (Anthropic):
      - cache_control on a block caches that block AND everything before it.
      - Two breakpoints (block 1 + block 2): the block-1 hit survives even when a
        CLAUDE.md edit changes block 2, and the block-2 hit covers the whole
        static prefix when block 2 is unchanged.
      - Min cacheable size is ~1024 tokens (Sonnet): block 2 (CLAUDE.md) is far
        over it; block 1 may be under it, in which case its breakpoint is a
        harmless no-op. Max 4 breakpoints/request — we use <=3 (2 here + tools).
    """
    if not static_context:
        return [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": "\n\n---\n\n# Context\n\n" + volatile_context,
            },
        ]

    return [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": "\n\n---\n\n# Portfolio context\n\n" + static_context,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": "\n\n---\n\n# Context\n\n" + volatile_context,
        },
    ]


def _build_cached_tools(entity: str = "FNDR", cross_entity: bool = False) -> list[dict]:
    """Return the entity-scoped tool definitions with cache_control on the last.

    Tools are scoped to the channel's entity (tools_for_entity): only the tools
    that entity actually uses are offered, which shrinks the cached tools block
    and narrows the model's tool-selection space. Aggregators (FNDR/HJRG) and the
    founder-from-any-channel (cross_entity=True) get the full set. The default
    (entity="FNDR") returns all tools, so any caller that omits the args is
    unchanged.

    The per-entity subset preserves TOOL_DEFINITIONS order, so each entity's
    tools block has a stable cache key and caches independently for the window.

    Anthropic rule: cache_control on the last tool caches the entire tools
    array as a single cacheable unit.
    """
    tools = tools_for_entity(entity, cross_entity)
    if not tools:
        return []
    tools = list(tools)
    tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
    return tools


def _log_usage(response: anthropic.types.Message, iteration: int) -> None:
    """Log token usage including cache hit/miss accounting.

    Wrapped in try/except so a mock or malformed Usage object never breaks the
    request — logging is observability, not a correctness contract.
    """
    try:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        # Coerce to int defensively — in production these are always ints from
        # the Anthropic SDK Usage object, but tests use MagicMock where the
        # attributes auto-generate as Mock instances.
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        cache_create = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
        log.info(
            "claude usage iter=%d input=%d cache_create=%d cache_read=%d output=%d",
            iteration, input_tokens, cache_create, cache_read, output_tokens,
        )
    except (TypeError, ValueError, AttributeError):
        # Mock usage objects, missing fields, etc. — skip silently.
        pass


def _dispatch_tools_parallel(
    tool_use_blocks: list,
    slack_user_id: str,
    entity: str,
    iteration: int,
    log_prefix: str = "tool_use",
    channel_name: str = "",
) -> list[dict]:
    """Dispatch a batch of tool_use blocks, in parallel when there are 2+.

    Returns a list of tool_result dicts in the SAME ORDER as the input blocks
    (Anthropic's API requires tool_result blocks to match tool_use_id order).

    Logging: a single tool_use log line per block is emitted BEFORE dispatch so
    the trace stays readable even when tools run concurrently. The dispatch()
    function inside tool_dispatch.py already catches per-tool exceptions and
    returns error strings, so parallel failures stay isolated.
    """
    # Always log up front so the trace order is deterministic
    for block in tool_use_blocks:
        log.info(
            "%s iter=%d tool=%s slack_user=%s entity=%s input=%s",
            log_prefix, iteration, block.name, slack_user_id or "(none)", entity, block.input or {},
        )

    if not tool_use_blocks:
        return []

    if len(tool_use_blocks) == 1:
        block = tool_use_blocks[0]
        result_str = dispatch(block.name, block.input or {}, slack_user_id, entity, channel_name)
        return [{
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": result_str,
        }]

    # 2+ tool calls — run concurrently
    max_workers = min(len(tool_use_blocks), _TOOL_DISPATCH_MAX_WORKERS)
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="cora-tool") as executor:
        futures = [
            executor.submit(dispatch, b.name, b.input or {}, slack_user_id, entity, channel_name)
            for b in tool_use_blocks
        ]
        results = [f.result() for f in futures]

    return [
        {"type": "tool_result", "tool_use_id": b.id, "content": r}
        for b, r in zip(tool_use_blocks, results)
    ]


def _record_tool_meta(meta: dict | None, tool_use_blocks: list) -> None:
    """Record which tools a reply used into the caller's meta dict.

    Sets meta["tool_names"] (cumulative) and meta["used_verbatim_tool"] (True once
    any VERBATIM_TABLE_TOOLS member fires). app.py reads used_verbatim_tool to SKIP
    the inline voice formatter (format_reply is_tool_output=True) for that reply so
    the table is not flattened, and to keep it out of the semantic cache. The egress
    boundary still applies the universal SAFETY layer (mojibake + URL/GID/long-ID
    redaction) to it. This is the precise replacement for the old bool(used_tools)
    bypass."""
    if meta is None:
        return
    names = [n for n in (getattr(b, "name", "") for b in tool_use_blocks) if n]
    if names:
        meta.setdefault("tool_names", []).extend(names)
        if any(n in VERBATIM_TABLE_TOOLS for n in names):
            meta["used_verbatim_tool"] = True


# ── Staged-WRITE narration safety net (2026-07-10, HIGH-2) ───────────────────
# The DTC inventory WRITE tool OWNS its user-facing outcome text. On a WRITE it
# returns a WRITE_CONFIRMED payload; on ANY non-write (preview / re-preview /
# refusal / clarification / failure) it returns a WRITE_BLOCKED payload that leads
# with "NOT WRITTEN". Live 2026-07-10 (on Haiku) the model narrated "203 units set"
# after a re-preview that wrote NOTHING. So whenever this tool's LAST result is
# present, the loop POSTS THE TOOL'S OWN TEXT (the part after the first blank line),
# overriding whatever the model streamed -- a mis-narrating model can no longer
# claim a write that did not happen (nor mis-state one that did). Mirrors the
# 2026-05-26 slack_send_dm silent-completion pattern, extended to repair a
# NON-empty hallucination, and scoped to this one write tool by name.
_SHOPIFY_WRITE_TOOL = "f3e_shopify_set_inventory"
# F-23 (2026-07-12): the narration net now covers a SET of contract-write tools --
# each returns a WRITE_CONFIRMED / WRITE_BLOCKED payload whose text the loop posts
# verbatim, overriding whatever the model streamed. Extended from Shopify to the
# destructive Asana tools (which fabricated a "deleted permanently" success with NO
# tool call in the mega-smoke). asana_create_task / gmail_create_draft are NOT in
# the set (they don't emit the sentinels; adding them later would require auditing
# that their verbatim payload leaks nothing source-sensitive).
_CONTRACT_WRITE_TOOLS = frozenset({
    _SHOPIFY_WRITE_TOOL, "asana_complete_task", "asana_delete_task",
})
# The net ONLY overrides narration when the tool result carries one of these
# contract sentinels. A result WITHOUT one (e.g. dispatch()'s "Tool ... crashed:"
# string on an unhandled exception) must NOT be posted verbatim -- it would leak the
# tool name / internal directives and bypass the model's source-opaque mediation
# (D-051 hotfix review #1). Fall through to the model's text in that case.
_SHOPIFY_SENTINELS = ("WRITE_CONFIRMED", "WRITE_BLOCKED")

# F-23 phantom-destructive-claim guard: when NO contract-write tool produced a
# sentinel this turn but the model's final text ANNOUNCES a destructive Asana
# action anyway (the fabricated "Task deleted permanently" with zero tool_use),
# override it with a truthful correction. Scoped to DELETE + first-person
# task/Asana announcements so a factual task-status answer ("that task was
# completed") is not caught. Fail-safe: a false override says "I didn't change
# anything" (non-harmful) rather than letting a phantom destructive claim stand.
# D-051 #4: scoped to FIRST-PERSON Cora announcements of a just-performed action +
# the terse "permanently deleted" fabrication shape, so a FACTUAL third-person status
# answer ("that task was deleted on 6/3 by Hannah", "the task is done") survives. The
# bare third-person "task ... deleted" branch was dropped (it clobbered legit status
# reports); a bare "Task deleted" with no "permanently"/first-person is an accepted
# residual (the tool-sentinel path is the primary F-23 control; this guard is a backstop).
_DESTRUCTIVE_ASANA_CLAIM_RE = re.compile(
    r"\bi(?:'ve| have| just)?\s+(?:permanently\s+)?deleted\b[^.\n]{0,30}\b(?:task|from asana)\b"
    r"|\bi(?:'ve| have| just)?\s+marked\b[^.\n]{0,30}\bcomplete\b"
    r"|\b(?:permanently\s+deleted|deleted\s+permanently)\b",
    re.IGNORECASE,
)
_PHANTOM_DESTRUCTIVE_CORRECTION = (
    "I didn't actually change anything in Asana just now -- nothing was deleted or "
    "completed. Tell me which task and I'll take care of it (I'll show you a preview "
    "and wait for your yes first)."
)


def _guard_phantom_destructive(text: str) -> str:
    """Override a fabricated destructive-Asana success (F-23). Applied ONLY when no
    contract-write tool produced a sentinel this turn (a real write would have)."""
    if text and _DESTRUCTIVE_ASANA_CLAIM_RE.search(text):
        return _PHANTOM_DESTRUCTIVE_CORRECTION
    return text


def _is_shopify_directive(raw: str) -> bool:
    return bool(raw) and raw.startswith(_SHOPIFY_SENTINELS)


def _shopify_directed_text(raw: str) -> str:
    """The user-facing text the write tool prescribes -- the part after the first
    blank line of a WRITE_CONFIRMED / WRITE_BLOCKED payload. Falls back to the raw
    string if the blank is absent (callers gate on _is_shopify_directive first)."""
    if not raw:
        return raw
    if raw.startswith(_SHOPIFY_SENTINELS):
        parts = raw.split("\n\n", 1)
        return parts[1].strip() if len(parts) > 1 and parts[1].strip() else raw
    return raw


def _last_shopify_write_result(tool_use_blocks: list, tool_results: list) -> str:
    """The authoritative contract-write result in this turn's batch (Shopify or the
    destructive Asana tools -- _CONTRACT_WRITE_TOOLS), or '' if none was called. A
    WRITE_CONFIRMED (a real write happened) WINS over a later WRITE_BLOCKED in the
    same batch, so a double-confirm can never narrate a completed write as 'NOT
    WRITTEN' (review #3). tool_results is same-order as tool_use_blocks."""
    found = ""
    for block, result in zip(tool_use_blocks, tool_results):
        if getattr(block, "name", None) not in _CONTRACT_WRITE_TOOLS:
            continue
        content = result.get("content", "")
        text = ""
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for blk in content:
                if isinstance(blk, dict) and blk.get("type") == "text":
                    text = blk.get("text", "")
        if text.startswith("WRITE_CONFIRMED"):
            return text  # a real write is authoritative for this turn
        if text:
            found = text
    return found


def _merge_shopify_result(prev: str, batch: str) -> str:
    """Fold a batch's shopify result into the running one across iterations. A
    WRITE_CONFIRMED already seen STICKS (a write happened; a later re-preview must
    not overwrite the narration, review #3)."""
    if not batch:
        return prev
    if batch.startswith("WRITE_CONFIRMED") or not prev.startswith("WRITE_CONFIRMED"):
        return batch
    return prev


def generate_response(
    system_prompt: str,
    context: str,
    user_message: str,
    slack_user_id: str = "",
    entity: str = "FNDR",
    model: str | None = None,
    prior_messages: list[dict] | None = None,
    channel_name: str = "",
    cached_context: str | None = None,
    cross_entity_tools: bool = False,
    meta: dict | None = None,
) -> str:
    """Call Claude (with tool-use loop) and return the final response text.

    meta: optional caller-owned dict for out-of-band response metadata. When
    provided, this function sets meta["used_tools"] (bool) so callers can tell
    whether the reply incorporates tool output (D-032: tool outputs bypass the
    reply formatter). Per-call object, so concurrent requests never race.

    slack_user_id is bound into the tool dispatcher so tools like asana_get_my_tasks
    resolve to the right Asana account. Pass empty string if there's no asking user
    (in which case tools that need a user will return a graceful error).

    entity is the routed channel entity (F3E, LEX, OSN, BDM, FNDR, etc.) — passed
    through to the dispatcher so tools can scope their results to the channel's entity.

    prior_messages: optional list of {"role": "user"|"assistant", "content": str}
    dicts representing prior thread turns. Prepended before the current user_message
    so Claude has conversation context when replying inside a thread.

    Uses Anthropic prompt caching: the system_prompt + tool definitions are cached
    ephemerally (~5min TTL). Cache hits across iterations within one request AND
    across requests within the same entity. Expect ~5-10x faster input processing
    on cache hits.

    cached_context: optional static portfolio context (founder + entity
    CLAUDE.md + known-answers + dynamic snapshots). When provided it becomes a
    second CACHED system block, so the large static mass is no longer re-billed
    on every mention — only the query-varying `context` arg stays uncached.

    Raises ClaudeClientError on hard failure after retries.
    """
    system_blocks = _build_cached_system(system_prompt, context, static_context=cached_context)
    cached_tools = _build_cached_tools(entity, cross_entity_tools)
    effective_model = model or _MODEL

    # Conversation accumulator — prepend thread history if provided, then append
    # the current user message. Grows with each tool_use / tool_result exchange.
    messages: list[dict] = list(prior_messages or []) + [{"role": "user", "content": user_message}]

    if meta is not None:
        meta["used_tools"] = False
        meta["used_verbatim_tool"] = False
        meta["tool_names"] = []

    _last_shopify_result: str = ""  # HIGH-2: the write tool owns its outcome text

    for iteration in range(_MAX_TOOL_ITERATIONS + 1):
        response = _create_with_retry(
            model=effective_model,
            max_tokens=_MAX_TOKENS,
            system=system_blocks,
            messages=messages,
            tools=cached_tools,
            timeout=_TIMEOUT,
        )
        _log_usage(response, iteration)

        if response.stop_reason != "tool_use":
            # Model is done. If the DTC write tool returned a CONTRACT result
            # (WRITE_CONFIRMED / WRITE_BLOCKED), POST ITS OWN outcome text --
            # overriding any success-claim the model produced on a non-write (HIGH-2).
            # A non-contract result (e.g. an unhandled crash string) is NOT posted
            # verbatim -- fall through to the model's source-opaque mediation.
            if _is_shopify_directive(_last_shopify_result):
                return _shopify_directed_text(_last_shopify_result) or "(Cora returned no text)"
            # No contract-write sentinel this turn -> phantom-destructive guard (F-23).
            return _guard_phantom_destructive(_extract_text(response)) or "(Cora returned no text)"

        if meta is not None:
            meta["used_tools"] = True

        if iteration >= _MAX_TOOL_ITERATIONS:
            log.warning(
                "Tool-use iteration cap (%d) hit — returning partial response",
                _MAX_TOOL_ITERATIONS,
            )
            if _is_shopify_directive(_last_shopify_result):
                return _shopify_directed_text(_last_shopify_result)
            return _guard_phantom_destructive(_extract_text(response)) or (
                "I tried to look that up but couldn't finish in time — try rephrasing."
            )

        # Capture assistant turn (must include tool_use blocks verbatim per API contract)
        messages.append({"role": "assistant", "content": response.content})

        # Execute each tool_use block in this turn (parallel when 2+)
        tool_use_blocks = [
            b for b in response.content if getattr(b, "type", None) == "tool_use"
        ]
        _record_tool_meta(meta, tool_use_blocks)
        tool_results = _dispatch_tools_parallel(
            tool_use_blocks, slack_user_id, entity, iteration,
            log_prefix="tool_use", channel_name=channel_name,
        )

        messages.append({"role": "user", "content": tool_results})
        _last_shopify_result = _merge_shopify_result(
            _last_shopify_result, _last_shopify_write_result(tool_use_blocks, tool_results))

    # Should not reach here given the iteration check above, but defensive fallback
    raise ClaudeClientError("Tool-use loop exited unexpectedly")


# ───────────────────────────────────────────────────────────────────────────
# Streaming variant
# ───────────────────────────────────────────────────────────────────────────

# Type alias for the update callback. Receives the cumulative response text so far
# (NOT just the latest delta). Caller decides whether/how to push to a UI surface
# (e.g., Slack chat_update with rate-limiting).
UpdateCallback = Callable[[str], None]


def _stream_with_retry(**kwargs):
    """Open a streaming response context with retry on transient errors.

    Returns the stream context manager. Caller is responsible for iterating it
    and calling `get_final_message()`. Same retry semantics as _create_with_retry.
    """
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            return _get_client().messages.stream(**kwargs)
        except Exception as exc:
            if _is_retryable(exc) and attempt < 2:
                delay = _RETRY_DELAYS[attempt]
                log.warning(
                    "Claude streaming transient error (attempt %d/3), retrying in %ds: %s",
                    attempt + 1, delay, exc,
                )
                time.sleep(delay)
                last_exc = exc
            else:
                raise ClaudeClientError(f"Claude API error: {exc}") from exc
    raise ClaudeClientError(f"Claude streaming failed after 3 attempts: {last_exc}") from last_exc


def generate_response_streaming(
    system_prompt: str,
    context: str,
    user_message: str,
    update_callback: Optional[UpdateCallback] = None,
    slack_user_id: str = "",
    entity: str = "FNDR",
    model: str | None = None,
    prior_messages: list[dict] | None = None,
    channel_name: str = "",
    cached_context: str | None = None,
    cross_entity_tools: bool = False,
    meta: dict | None = None,
) -> str:
    """Streaming variant of generate_response.

    meta: optional caller-owned dict for out-of-band response metadata — sets
    meta["used_tools"] (bool) exactly like generate_response (D-032 bypass signal).

    Calls Claude with `messages.stream()` and invokes `update_callback(text)` on
    every text-delta event with the CUMULATIVE response text so far (not just the
    new delta). Caller is responsible for:
      - Rate-limiting actual UI updates (Slack chat_update etc.) — this function
        calls the callback on EVERY text delta, which could be many per second.
      - Handling callback exceptions — they propagate out of this function. If
        the caller wants to swallow them, they should wrap update_callback.

    Returns the final accumulated text (same string the callback was last called
    with, unless the final iteration emitted text after the last callback fired).

    Tool-use loop: identical to generate_response. On stop_reason=tool_use,
    dispatch tools and loop. Text accumulates across iterations — if iter 0
    emits "Let me check that..." and iter 1 emits "Here's what I found...",
    the user sees the concatenation. Matches the model's intent.

    update_callback may be None (e.g., during tests) — in that case no
    progressive updates fire, but the function still returns the final text.

    cached_context: optional static portfolio context — see generate_response.
    When provided it becomes a second CACHED system block.

    Raises ClaudeClientError on hard failure after retries.
    """
    system_blocks = _build_cached_system(system_prompt, context, static_context=cached_context)
    cached_tools = _build_cached_tools(entity, cross_entity_tools)
    effective_model = model or _MODEL

    messages: list[dict] = list(prior_messages or []) + [{"role": "user", "content": user_message}]
    accumulated_text = ""
    _last_tool_result_text: str = ""  # safety net: fallback if Claude emits no text after a write
    _last_shopify_result: str = ""    # HIGH-2: the DTC write tool owns its outcome text

    if meta is not None:
        meta["used_tools"] = False
        meta["used_verbatim_tool"] = False
        meta["tool_names"] = []

    def _maybe_push(text: str) -> None:
        if update_callback is not None:
            update_callback(text)

    for iteration in range(_MAX_TOOL_ITERATIONS + 1):
        try:
            with _stream_with_retry(
                model=effective_model,
                max_tokens=_MAX_TOKENS,
                system=system_blocks,
                messages=messages,
                tools=cached_tools,
                timeout=_TIMEOUT,
            ) as stream:
                for event in stream:
                    # Two event shapes carry text:
                    #   content_block_delta with delta.type == text_delta
                    #   (some SDK versions also expose a `.text` attribute on event)
                    event_type = getattr(event, "type", None)
                    if event_type != "content_block_delta":
                        continue
                    delta = getattr(event, "delta", None)
                    if delta is None:
                        continue
                    if getattr(delta, "type", None) != "text_delta":
                        continue
                    chunk = getattr(delta, "text", "") or ""
                    if chunk:
                        accumulated_text += chunk
                        _maybe_push(accumulated_text)

                final = stream.get_final_message()
        except ClaudeClientError:
            raise
        except Exception as exc:  # noqa: BLE001 — defensive; SDK errors should be caught above
            raise ClaudeClientError(f"Streaming error: {exc}") from exc

        _log_usage(final, iteration)

        if final.stop_reason != "tool_use":
            # Model is done — return the cumulative text.
            # `accumulated_text` should already match the final message's text
            # content, but extract from final as a safety net if streaming dropped events.
            final_text = _extract_text(final)
            if final_text and final_text != accumulated_text:
                # Stream missed some text — push the corrected final
                accumulated_text = final_text
                _maybe_push(accumulated_text)
            directive_fired = _is_shopify_directive(_last_shopify_result)
            if directive_fired:
                # HIGH-2: the contract-write tool OWNS its outcome text. Post it
                # verbatim, OVERRIDING any narration the model streamed -- so a
                # mis-narrating model can never claim a write that did not happen (nor
                # mis-state one that did). Fires whether or not the model emitted text.
                # Only a CONTRACT result (WRITE_CONFIRMED/WRITE_BLOCKED) overrides; a
                # crash string falls through to the model's source-opaque mediation (#1).
                directed = _shopify_directed_text(_last_shopify_result)
                if directed and directed != accumulated_text:
                    accumulated_text = directed
                    _maybe_push(accumulated_text)
            elif not accumulated_text and _last_tool_result_text:
                # Claude produced no text after a tool call (silent-completion).
                # Extract the WRITE_CONFIRMED payload if present, otherwise
                # surface the raw tool result so the user sees something.
                raw = _last_tool_result_text
                if "WRITE_CONFIRMED" in raw:
                    # Strip the instruction prefix — only post the user-facing lines
                    parts = raw.split("\n\n", 1)
                    accumulated_text = parts[1].strip() if len(parts) > 1 else raw
                else:
                    accumulated_text = raw
                log.warning(
                    "Silent-completion fallback triggered: extracted %d chars from last tool result",
                    len(accumulated_text),
                )
            if not directive_fired:
                # No contract-write sentinel this turn -> phantom-destructive guard
                # (F-23): override a fabricated "task deleted" success with no tool call.
                guarded = _guard_phantom_destructive(accumulated_text)
                if guarded != accumulated_text:
                    accumulated_text = guarded
                    _maybe_push(accumulated_text)
            return accumulated_text or "(Cora returned no text)"

        if meta is not None:
            meta["used_tools"] = True

        if iteration >= _MAX_TOOL_ITERATIONS:
            log.warning(
                "Tool-use iteration cap (%d) hit during streaming — returning partial response",
                _MAX_TOOL_ITERATIONS,
            )
            if _is_shopify_directive(_last_shopify_result):
                return _shopify_directed_text(_last_shopify_result)
            return _guard_phantom_destructive(accumulated_text) or (
                "I tried to look that up but couldn't finish in time — try rephrasing."
            )

        # Capture assistant turn (must include tool_use blocks verbatim per API contract)
        messages.append({"role": "assistant", "content": final.content})

        # Execute each tool_use block in this turn (parallel when 2+)
        tool_use_blocks = [
            b for b in final.content if getattr(b, "type", None) == "tool_use"
        ]
        _record_tool_meta(meta, tool_use_blocks)
        tool_results = _dispatch_tools_parallel(
            tool_use_blocks, slack_user_id, entity, iteration,
            log_prefix="tool_use (stream)", channel_name=channel_name,
        )

        messages.append({"role": "user", "content": tool_results})

        # Track the last tool result text so we can surface it if Claude emits
        # no text in the next iteration (silent-completion bug on write tools).
        for tr in tool_results:
            content = tr.get("content", "")
            if isinstance(content, str) and content.strip():
                _last_tool_result_text = content.strip()
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        t = block.get("text", "").strip()
                        if t:
                            _last_tool_result_text = t
        # HIGH-2: remember the DTC write tool's authoritative result (by tool name)
        # so the stop handler can post its outcome text; a WRITE_CONFIRMED sticks.
        _last_shopify_result = _merge_shopify_result(
            _last_shopify_result, _last_shopify_write_result(tool_use_blocks, tool_results))

    raise ClaudeClientError("Tool-use loop exited unexpectedly during streaming")
