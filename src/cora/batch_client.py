"""Message Batches utility for Cora's nightly script legs (50% off tokens).

2026-07-31 batch-API pilot, slice 2. ``batch_generate()`` submits a list of
Messages-API requests as ONE batch, polls with a hard wall-clock deadline, and
returns results keyed by ``custom_id``. FAIL-SOFT is the house invariant: any
batch-layer failure (submit error, deadline, per-item error/expiry) degrades to
plain synchronous ``messages.create`` calls per item, so a consumer is never
worse off than before batching -- the 7am knowledge review must NEVER wait on
a stuck batch (kickoff invariant).

Double-spend / dedup semantics (D-051 concern #1): this is a single-process,
synchronous flow -- results are returned in memory and the batch is never
polled again after this call returns. On deadline we best-effort CANCEL the
batch and fall back sync for every item; requests the batch already completed
before the cancel landed are billed (at the 50% batch rate) and abandoned.
That bound is logged loudly. There is no persistent batch ledger, so a batch
"completing after the fallback ran" can never double-DELIVER -- only
double-spend, bounded to one batch's worth, on the rare deadline path.

At-rest surface (D-051 concern #4): batch payloads/results live on Anthropic's
side for up to 29 days, retrievable with the org API key -- the same trust
boundary as the sync calls these legs already make. LOCALLY this module never
writes payloads or response content anywhere: log lines carry only ids,
counts, statuses and token usage (test-pinned).

Standalone by design (D-047): imports anthropic + cora.llm_usage + stdlib
only, so channel_synthesis / session_capture / strategy_memo may import it
without pulling bot-process modules.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

from .llm_usage import log_usage

log = logging.getLogger(__name__)

# Anthropic custom_id constraint (also keeps ids opaque: NEVER put message
# content, names, or anything PHI-adjacent in a custom_id).
_CUSTOM_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

# Terminal per-item result types from the Batches API.
_RESULT_SUCCEEDED = "succeeded"

# Values that read as "off" for a per-leg enable flag (flag unset => ON).
_OFF_VALUES = frozenset({"0", "off", "false", "no"})

# Sync-fallback client bounds: a stuck fallback call must not eat the caller's
# task ExecutionTimeLimit (script-side self-bounding is the real control).
_FALLBACK_TIMEOUT_S = 120.0
_FALLBACK_MAX_RETRIES = 1

DEFAULT_POLL_INTERVAL_S = 15.0
DEFAULT_DEADLINE_S = 1800.0


def batch_enabled(leg_flag: str) -> bool:
    """True when batching is enabled for the given per-leg env flag.

    The global kill switch ``CORA_BATCH_DISABLE=1`` wins over everything --
    one .env line turns every batch leg back into plain sync calls with no
    task re-registration. Otherwise the per-leg flag (e.g.
    ``CORA_BATCH_SYNTHESIS``, ``CORA_BATCH_CAPTURE``) defaults ON; set it to
    0/off/false/no to disable just that leg.
    """
    disable = os.environ.get("CORA_BATCH_DISABLE", "").strip().lower()
    if disable and disable not in _OFF_VALUES:
        return False
    leg = os.environ.get(leg_flag, "").strip().lower()
    return leg not in _OFF_VALUES or leg == ""


def _build_client(client: Any = None, api_key: str | None = None) -> Any:
    if client is not None:
        return client
    import anthropic
    key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic(
        api_key=key, timeout=_FALLBACK_TIMEOUT_S,
        max_retries=_FALLBACK_MAX_RETRIES,
    )


def _sync_one(client: Any, params: dict, *, caller: str) -> Any | None:
    """One plain messages.create for a fallback item. None on failure."""
    try:
        msg = client.messages.create(**params)
    except Exception as exc:  # noqa: BLE001 -- fail-soft per item
        log.warning("batch_client[%s]: sync fallback call failed: %s", caller, exc)
        return None
    log_usage(msg, caller=caller, via="sync-fallback")
    return msg


def _validate(requests: list[dict]) -> None:
    seen: set[str] = set()
    for req in requests:
        cid = req.get("custom_id")
        if not isinstance(cid, str) or not _CUSTOM_ID_RE.match(cid):
            raise ValueError(f"invalid custom_id {cid!r} "
                             "(must match ^[a-zA-Z0-9_-]{{1,64}}$)")
        if cid in seen:
            raise ValueError(f"duplicate custom_id {cid!r}")
        seen.add(cid)
        if not isinstance(req.get("params"), dict):
            raise ValueError(f"request {cid!r} missing params dict")


def batch_generate(
    requests: list[dict],
    *,
    caller: str,
    deadline_s: float = DEFAULT_DEADLINE_S,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    client: Any = None,
    api_key: str | None = None,
    sync_fallback: bool = True,
) -> dict[str, Any]:
    """Run *requests* through the Message Batches API; return {custom_id: Message|None}.

    Each request is ``{"custom_id": <opaque id>, "params": {<messages.create
    kwargs>}}``. Semantics:

    * submit fails            -> sync fallback for EVERY item
    * deadline exceeded       -> best-effort cancel + sync fallback for EVERY
                                 item (batch abandoned; see module docstring)
    * item errored/canceled/
      expired inside a batch  -> sync fallback for THAT item only
    * ``sync_fallback=False`` -> failed items map to None instead

    A returned ``None`` means both transports failed for that item -- callers
    keep their existing fail-closed handling. Raises ``ValueError`` only for
    malformed input (a programming error), never for transport failures.
    """
    if not requests:
        return {}
    _validate(requests)
    ids = [r["custom_id"] for r in requests]
    params_by_id = {r["custom_id"]: r["params"] for r in requests}

    def _sync_all(reason: str) -> dict[str, Any]:
        log.warning("batch_client[%s]: falling back to %d sync call(s): %s",
                    caller, len(requests), reason)
        if not sync_fallback:
            return dict.fromkeys(ids)
        fb_client = _build_client(client, api_key)
        return {cid: _sync_one(fb_client, params_by_id[cid], caller=caller)
                for cid in ids}

    try:
        api = _build_client(client, api_key)
    except Exception as exc:  # noqa: BLE001 -- no key/client -> nothing works
        log.warning("batch_client[%s]: client unavailable (%s) -- returning "
                    "no results", caller, exc)
        return dict.fromkeys(ids)

    started = time.monotonic()
    try:
        batch = api.messages.batches.create(requests=requests)
    except Exception as exc:  # noqa: BLE001
        return _sync_all(f"batch submit failed: {exc}")
    log.info("batch_client[%s]: submitted batch %s (%d request(s), "
             "deadline %.0fs)", caller, batch.id, len(requests), deadline_s)

    # Poll until ended or deadline. Transient retrieve() errors do not abort
    # before the deadline -- the batch may still complete.
    while True:
        elapsed = time.monotonic() - started
        if elapsed >= deadline_s:
            try:
                api.messages.batches.cancel(batch.id)
                log.warning(
                    "batch_client[%s]: DEADLINE (%.0fs) -- canceled batch %s; "
                    "already-completed batch items are billed then abandoned "
                    "(bounded double-spend), falling back sync",
                    caller, deadline_s, batch.id)
            except Exception as exc:  # noqa: BLE001
                log.warning("batch_client[%s]: DEADLINE (%.0fs) -- cancel of "
                            "%s failed (%s); falling back sync anyway",
                            caller, deadline_s, batch.id, exc)
            return _sync_all("deadline exceeded")
        try:
            status = api.messages.batches.retrieve(batch.id)
            if getattr(status, "processing_status", "") == "ended":
                break
        except Exception as exc:  # noqa: BLE001 -- transient; keep polling
            log.warning("batch_client[%s]: poll of %s failed (%s) -- retrying",
                        caller, batch.id, exc)
        time.sleep(min(poll_interval_s, max(0.0, deadline_s - elapsed)))

    # Collect results (returned in ANY order -- key by custom_id, never
    # position). An iteration error abandons the batch entirely.
    results: dict[str, Any] = {}
    failed: list[str] = []
    try:
        for item in api.messages.batches.results(batch.id):
            cid = getattr(item, "custom_id", None)
            if cid not in params_by_id:
                continue
            rtype = getattr(getattr(item, "result", None), "type", "")
            if rtype == _RESULT_SUCCEEDED:
                msg = item.result.message
                log_usage(msg, caller=caller, via="batch")
                results[cid] = msg
            else:
                failed.append(cid)
    except Exception as exc:  # noqa: BLE001
        return _sync_all(f"results retrieval failed: {exc}")

    # Anything the batch never reported (defensive) counts as failed.
    missing = [cid for cid in ids if cid not in results and cid not in failed]
    failed.extend(missing)
    log.info("batch_client[%s]: batch %s ended in %.0fs -- %d succeeded / "
             "%d failed%s", caller, batch.id, time.monotonic() - started,
             len(results), len(failed),
             f" ({len(missing)} unreported)" if missing else "")

    if failed:
        # Poisoned/errored items fail-soft INDIVIDUALLY (D-051 concern #3:
        # one bad item must not sink the others -- the API already isolates
        # per-item errors; this is the per-item recovery).
        if sync_fallback:
            fb_client = _build_client(client, api_key)
            for cid in failed:
                results[cid] = _sync_one(fb_client, params_by_id[cid],
                                         caller=caller)
        else:
            for cid in failed:
                results[cid] = None
    return results
