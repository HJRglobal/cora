"""Deposco V1 API client -- the write path (Phase 2/3 order push).

Design of record: `02-F3-Energy/projects/2026-08_deposco-api-order-automation/
_notes/2026-09-08_f3e_DESIGN-PROPOSAL-deposco-phase2-3-push.md` (SS3-SS5, SS10
ruling). Kickoff: `_notes/2026-09-09_f3e_SONNET-HANDOFF-deposco-push-write-path-
build.md` step 3.

THIS MODULE EXISTS SO THE READ CLIENT NEVER HAS TO. `deposco_client.py`'s
headline invariant is that it CANNOT write -- one verb, "GET", never any
other. Adding a push method there would put a write inside a module whose
whole safety argument is that it has none. So the write path is a SEPARATE
module with its own, narrower invariant: exactly ONE mutating verb literal
("POST"), exactly ONE route ("/orders"), and a prod gate that makes a script
or a REPL structurally incapable of reaching production.

WHAT IS SHARED, AND HOW. Credentials, environment selection, TLS truststore
and request pacing are NOT reimplemented here -- `DeposcoPushClient` holds a
`deposco_client.DeposcoClient` internally (composition) purely to get an
already-validated auth header, base URL, tenant/business unit and pacing
state. That is the ONLY way this module touches credentials; it never parses
`.env` itself and never builds its own `Basic` token. A future edit that gave
this module its own credential-loading code would be exactly the kind of
duplicated secrets-handling path D-181 exists to prevent.

THE PROD GATE IS TWO INDEPENDENT CONDITIONS, BOTH REQUIRED, CHECKED BEFORE ANY
NETWORK CALL:
  1. the channel is in the `CORA_DEPOSCO_PUSH_CHANNELS` allowlist (an env var,
     a comma-separated list Harrison sets by hand at first-prod-push time --
     never written by this codebase, see the SONNET-HANDOFF step 8 guardrail);
  2. the caller passes a non-empty `claimed_pending_id`.

Neither condition alone is sufficient to reach prod, and satisfying (2) is not
this module's job to verify against the pending store -- that exactly-once
claim lives in `deposco_orders.pending`, and the only caller that can ever
legitimately hold a claimed id is `deposco_orders.handler`'s tap path. This
module's contract is narrower and purely structural: a script or a REPL
calling `push_order(..., env="prod")` with no id, or an id but no channel
allowlist entry, is refused before a single byte reaches Deposco. UA pushes
carry no such gate -- the whole point of Phase 2 is rehearsing the write path
somewhere it cannot cost anything.

OUTCOME CLASSIFICATION LIVES ONE LAYER UP. This module returns `PushOutcome`
-- raw HTTP-layer facts (status, body, whether blank-200 retries or transient
retries were exhausted) -- and never itself decides CONFIRMED / FAILED /
UNKNOWN / ANOMALY-UPDATED / MISMATCH. That decision needs a read-back
(D-110), which needs the stored payload and the pending entry, none of which
this module knows about. Blurring that line here would mean re-deciding it
again in `deposco_orders.handler`, and the two could drift.

VENDOR FACTS THIS MODULE IS BUILT AROUND (D-182, pinned live 2026-08-14 /
2026-09-23): `POST /orders` returns 201 ONLY on create; 200 means an existing
order in status `New` was silently UPDATED -- so a 200 here is never treated
as ordinary success, it is returned as-is for the caller to alarm on. Blank
200 is Deposco's own documented failure mode (doc p. 25) and is retried the
same way the read client retries it. There is no DELETE and no sales-order
cancel route anywhere in this module, on purpose -- see the design doc SS1.4.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

from . import deposco_client as dc

log = logging.getLogger(__name__)

ORDERS_PATH = "/orders"


class DeposcoPushError(Exception):
    """Base class. Message text is scrubbed of credential material via the
    same `_reader._scrub` the read client uses -- see the module docstring."""


class DeposcoPushRefused(DeposcoPushError):
    """The prod gate refused this call before any network I/O. Nothing was
    sent -- this is a structural refusal, not an API-level failure."""


@dataclass
class PushOutcome:
    """HTTP-layer facts about one push attempt.

    Outcome CLASSIFICATION (CONFIRMED / FAILED / UNKNOWN / ANOMALY-UPDATED /
    MISMATCH) is a domain decision made by `deposco_orders.handler` from this
    plus a read-back -- this dataclass never decides which one applies. It
    DOES expose `created` / `updated` / `conflict` as derived FACTS (what did
    Deposco actually say), because those facts live in the response BODY, not
    the outer HTTP status -- see the properties below.

    LIVE FINDING (2026-09-23, UA): Deposco wraps every `/orders` POST response
    in a multistatus envelope -- the outer HTTP status observed was 207 for
    BOTH a successful create and a genuine duplicate, with the true per-order
    result as TEXT inside the body (`<status>HTTP/1.1 201 Created</status>` /
    `<status>HTTP/1.1 409 Conflict</status>`). This is not new information --
    the 8/14 RESULT file already said "201 in the MULTISTATUS response" -- but
    the first cut of this module wrongly compared the outer `status` directly
    against 200/201, which mis-classified every real push (including a
    genuine create) as an unrecognized status. Matches the parsing
    `scripts/deposco_ua_test_order.py` already proved live 8/14
    (`"201" in body and "Created" in body`).

    A 409 Conflict was observed for a duplicate order number -- NOT the
    silent 200 D-182 (drawn from Deposco's documentation, never empirically
    tested before this) predicted. `updated` is kept as a defensive check
    regardless: D-182's underlying doctrine (`vendor doc is a hypothesis`)
    cuts both ways -- an unobserved case is not a disproven one, and the
    check costs nothing to keep. A 409 (or anything that is neither created
    nor updated) is classified FAILED one layer up -- safe, since nothing was
    created or changed.
    """

    env: str
    status: int | None = None  # None only when network_exhausted
    text: str = ""
    #: 3 consecutive blank-200 responses (Deposco doctrine, doc p. 25).
    blank_after_retries: bool = False
    #: Transient (429/5xx) or network-error retries exhausted; no response body.
    network_exhausted: bool = False

    def _has_body(self) -> bool:
        return not (self.blank_after_retries or self.network_exhausted) and bool(self.text)

    @property
    def created(self) -> bool:
        """True when the multistatus body confirms a CREATE for this push."""
        return self._has_body() and "201" in self.text and "Created" in self.text

    @property
    def updated(self) -> bool:
        """True when the body says an existing order was silently UPDATED --
        the D-182 alarm condition. Never true at the same time as `created`."""
        if not self._has_body() or self.created:
            return False
        return "Updated" in self.text or "updated" in self.text

    @property
    def conflict(self) -> bool:
        """True on a 409-shaped rejection (the live-observed duplicate
        response) -- SAFE: nothing was created or changed."""
        if not self._has_body() or self.created or self.updated:
            return False
        return "409" in self.text and "Conflict" in self.text

    @property
    def is_2xx(self) -> bool:
        """True on any 2xx that is not itself a retry-exhaustion marker.

        A raw outer-status check, kept for callers that only care whether the
        HTTP layer itself succeeded. NEVER read this as "CONFIRMED" -- it is
        True for a 207 multistatus regardless of whether the order inside it
        was created, updated, or rejected as a duplicate (207 is numerically
        a 2xx). Use `created`/`updated`/`conflict` for anything
        order-outcome-shaped; `is_2xx` cannot distinguish them.
        """
        return (
            not self.blank_after_retries
            and not self.network_exhausted
            and self.status is not None
            and 200 <= self.status < 300
        )


def _push_channels_allowlist() -> set[str]:
    raw = os.environ.get("CORA_DEPOSCO_PUSH_CHANNELS", "") or ""
    return {c.strip() for c in raw.split(",") if c.strip()}


def _require_prod_push_allowed(channel: str, claimed_pending_id: str | None) -> None:
    """Both conditions, checked in this order so the message is actionable
    either way. Raises before any network I/O."""
    if not claimed_pending_id or not str(claimed_pending_id).strip():
        raise DeposcoPushRefused(
            "refusing a prod push with no claimed pending-entry id -- the "
            "only prod push entry point is the tap handler after a "
            "successful claim, never a script or a REPL"
        )
    allowed = _push_channels_allowlist()
    if not channel or channel not in allowed:
        raise DeposcoPushRefused(
            f"prod push refused for channel {channel!r} -- not present in "
            f"CORA_DEPOSCO_PUSH_CHANNELS (currently: {sorted(allowed) or 'unset'}). "
            "Harrison sets this env var by hand at first-prod-push time."
        )


class DeposcoPushClient:
    """The one place `POST /orders` can be issued from."""

    def __init__(
        self,
        env: str = "prod",
        tenant: str | None = None,
        business_unit: str | None = None,
        transport: Any = None,
        pace_seconds: float = dc.PACE_SECONDS,
        timeout: float = dc.TIMEOUT_SECONDS,
    ) -> None:
        # Composition, not reimplementation: DeposcoClient's constructor is
        # what validates and holds credentials, the truststore SSL context,
        # and request pacing. Reusing it here is what keeps this module from
        # ever having its own copy of that logic to get wrong. `_reader`'s own
        # `_get`/`_send` (GET-only) are never called from this class.
        self._reader = dc.DeposcoClient(
            env=env, tenant=tenant, business_unit=business_unit,
            pace_seconds=pace_seconds, timeout=timeout,
        )
        self.env = self._reader.env
        self.tenant = self._reader.tenant
        self.business_unit = self._reader.business_unit
        self.base_url = self._reader.base_url
        self._transport = transport

    def __repr__(self) -> str:  # pragma: no cover -- trivial, but load-bearing
        return f"<DeposcoPushClient env={self.env} tenant={self.tenant} bu={self.business_unit}>"

    def push_order(
        self, payload: dict, *, channel: str, claimed_pending_id: str | None = None,
    ) -> PushOutcome:
        """POST one order envelope (`{"order": [{...}]}`, already built by
        `deposco_orders.payload`) to `/orders`.

        `channel` and `claimed_pending_id` are REQUIRED arguments (not
        keyword-optional in spirit, even though `claimed_pending_id` has a
        default of None for a clean UA-rehearsal call shape) -- omitting them
        against a prod client raises `DeposcoPushRefused` before anything is
        sent. Against a non-prod client (UA) they are not checked at all,
        matching the UA rehearsal's unrestricted shape.
        """
        if self.env == "prod":
            _require_prod_push_allowed(channel, claimed_pending_id)
        return self._post_orders(payload)

    def _post_orders(self, payload: dict) -> PushOutcome:
        dc._check_path(ORDERS_PATH)
        url = self.base_url + ORDERS_PATH
        headers = {
            "Authorization": self._reader._auth_header,
            "Accept": dc._ACCEPT,
            "Content-Type": "application/json",
        }
        body = json.dumps(payload).encode("utf-8")

        blank_seen = 0
        transient_seen = 0
        while True:
            self._reader._sleep_for_pace()
            try:
                response = self._send(url, headers, body)
            except httpx.HTTPError:
                transient_seen += 1
                if transient_seen > dc.TRANSIENT_RETRIES:
                    log.error(
                        "deposco_push[%s]: network error after %d retries on POST %s",
                        self.env, dc.TRANSIENT_RETRIES, ORDERS_PATH,
                    )
                    return PushOutcome(env=self.env, status=None, network_exhausted=True)
                self._reader._backoff(transient_seen)
                continue

            status = response.status_code
            text = response.text or ""

            if status in (401, 403):
                raise dc.DeposcoAuthError(self._reader._scrub(
                    f"{self.env}: credentials rejected (HTTP {status}) on POST {ORDERS_PATH}."
                ))

            if status == 200 and not text.strip():
                blank_seen += 1
                log.warning(
                    "deposco_push[%s]: blank 200 on POST %s (attempt %d) -- Deposco "
                    "doctrine says this is a FAILURE, retrying",
                    self.env, ORDERS_PATH, blank_seen,
                )
                if blank_seen >= dc.BLANK_200_RETRIES:
                    return PushOutcome(
                        env=self.env, status=200, text="", blank_after_retries=True,
                    )
                self._reader._backoff(blank_seen)
                continue

            if status == 429 or 500 <= status < 600:
                transient_seen += 1
                if transient_seen > dc.TRANSIENT_RETRIES:
                    return PushOutcome(
                        env=self.env, status=status, text=text, network_exhausted=True,
                    )
                self._reader._backoff(transient_seen)
                continue

            return PushOutcome(env=self.env, status=status, text=text)

    def _send(self, url: str, headers: dict[str, str], body: bytes):
        """The single place this module names the HTTP verb it sends."""
        if self._transport is not None:
            return self._transport(url=url, headers=headers, body=body)
        with httpx.Client(timeout=self._reader._timeout, verify=dc._ssl_context()) as client:
            return client.request("POST", url, headers=headers, content=body)
