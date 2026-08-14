"""Deposco V1 API client -- READ-ONLY BY CONSTRUCTION (Phase 1).

Design of record: `02-F3-Energy/projects/2026-08_deposco-api-order-automation/
2026-08-06_f3e_deposco-api-order-automation-design.md` (Fork 1: Cora script layer,
deterministic, no LLM anywhere in the order path). Live-API findings that shaped
this module: `_notes/2026-08-14_f3e_RESULT-ua-test-order-gotham-COMPLETE.md`.

WRITE-IMPOSSIBILITY IS THE HEADLINE INVARIANT. Phase 1 is read-only, and that is
enforced structurally rather than by discipline: this module has exactly ONE
request primitive, `_get`, which passes the literal string "GET" to httpx. No
other HTTP verb appears anywhere in the file, and `tests/test_deposco_client.py`
greps the source to keep it that way. Adding an order-push method is therefore a
visible, reviewable act -- not something a refactor can do by accident.

Six live-API findings from the 2026-08-14 UA test order are encoded here, each of
which cost a debug round to learn:

  1. TLS -- Python 3.14 on Windows misses the DigiCert G5 root (Windows fetches
     roots on demand; Python only enumerates the local cache). `truststore` reads
     the Windows store and fixes it. There is deliberately NO verification-off
     escape hatch in this client: the sandbox rehearsal script has one because its
     host is pinned to UA, and a prod-capable client must never inherit it.
  2. Accept must allow XML. Order and order-status reads are XML-ONLY (the
     OrderHeader/OrderStatus XSDs); a JSON-only Accept contributed to the 8/14
     failures. Enterprise Inventory answers JSON.
  3. SPACE-FREE PATHS ONLY. The gateway flat-400s any URL whose PATH contains an
     encoded space -- so the documented `/orders/Sales Order/{n}` and
     `/status/order/Sales Order/{n}` routes are unusable on tenant ESM. Spaces in
     the QUERY string are fine. `_check_path` makes the bad shape unconstructable.
  4. Blank 200 = FAILURE, retry (Deposco's own requirement, doc p. 25).
  5. Item numbers are the Shopify feed SKUs (`PURE-Original` ... `PURESL`), not
     UPCs -- the same keys `data/maps/f3e-channel-sku-map.yaml` already uses.
  6. ~0.6s pacing (Anthony 8/7: ~2 req/sec is fine).

CREDENTIALS NEVER LEAVE THIS MODULE. They are read from `.env` into a private
auth header, never logged, never interpolated into a URL, and `_scrub` strips them
from any message this module raises or logs -- the 13WCF-M2 `identity.failed`
lesson, and the belt behind the acceptance grep-gate.

ABSENT IS NEVER ZERO. Deposco omits measures it has no value for (the doc's own
examples show `totalOnHandQty` missing on some items and every facility carrying a
different subset). Parsed measures are therefore `int | None`, and None means
UNKNOWN -- callers must render it as such. Coercing to 0 would invent a stockout.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

import httpx

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Environments
# ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DeposcoEnv:
    """One target environment. `name` is stamped into every response and every
    file this client's consumers write, so a figure can never be misattributed
    between sandbox and production (D-051 lens 5)."""

    name: str
    base_template: str
    user_key: str
    pass_key: str


#: UA carries no inventory (Anthony, 8/5) -- it exists for order-push and
#: shipment-pull rehearsal. Inventory reads validate against PROD, which is safe
#: precisely because this client cannot write.
ENVIRONMENTS: dict[str, DeposcoEnv] = {
    "ua": DeposcoEnv(
        name="ua",
        base_template="https://sandboxapi.deposco.com/ua/integration/{tenant}",
        user_key="DEPOSCO_UA_USER",
        pass_key="DEPOSCO_UA_PASS",
    ),
    "prod": DeposcoEnv(
        name="prod",
        base_template="https://api.deposco.com/integration/{tenant}",
        user_key="DEPOSCO_PROD_USER",
        pass_key="DEPOSCO_PROD_PASS",
    ),
}

DEFAULT_TENANT = "ESM"
DEFAULT_BUSINESS_UNIT = "F3E"

#: Documented Enterprise Inventory measures (doc pp. 88-93). Requesting a measure
#: the tenant has not configured simply omits it from the response -- which is
#: why a missing measure must read UNKNOWN rather than zero.
ALL_MEASURES: tuple[str, ...] = (
    "totalOnHandQty",
    "atpQty",
    "coAtpQty",
    "totalAtp",
    "openOrderLineQty",
    "qtyOnPO",
    "inTransitQty",
)

TIMEOUT_SECONDS = 30.0
PACE_SECONDS = 0.6
BLANK_200_RETRIES = 3
TRANSIENT_RETRIES = 3
#: Hard stop on the paginated all-items route. Hitting it is LOGGED LOUDLY and
#: surfaced to the caller -- a silent truncation would read as "that is all the
#: stock we hold", which is the exact failure this codebase keeps designing out.
MAX_PAGES = 100
PAGE_SIZE = 100

_ACCEPT = "application/xml, application/json;q=0.9, */*;q=0.8"


# ─────────────────────────────────────────────────────────────────────────────
# Errors -- none of these ever carry a credential
# ─────────────────────────────────────────────────────────────────────────────


class DeposcoError(Exception):
    """Base class. Message text is scrubbed of credential material."""


class DeposcoAuthError(DeposcoError):
    """Missing, malformed, or rejected credentials (401/403)."""


class DeposcoUnavailable(DeposcoError):
    """Network failure, exhausted retries, or the blank-200 failure mode."""


class DeposcoUsageError(DeposcoError):
    """The caller asked for something this client refuses to build."""


# ─────────────────────────────────────────────────────────────────────────────
# Path safety (finding 3) -- the bad shape is unconstructable
# ─────────────────────────────────────────────────────────────────────────────

_SPACE_IN_PATH = re.compile(r"[ \t]|%20", re.IGNORECASE)


def _check_path(path: str) -> str:
    """Reject any path segment carrying a literal or encoded space.

    The UA gateway flat-400s those (finding 3), so a route built that way is
    dead on arrival -- better to fail loudly at construction than to ship a
    client whose read lane silently never works. The query string is exempt:
    `?type=Sales%20Order` is the WORKING form.
    """
    if not path.startswith("/"):
        raise DeposcoUsageError("path must start with '/'")
    head, sep, _query = path.partition("?")
    if _SPACE_IN_PATH.search(head):
        raise DeposcoUsageError(
            "refusing to build a URL with a space in the PATH -- the gateway "
            "flat-400s these. Use a search route and put the order type in the "
            "query string instead (see finding 3)."
        )
    return path


# ─────────────────────────────────────────────────────────────────────────────
# Value coercion -- absent is never zero
# ─────────────────────────────────────────────────────────────────────────────


def coerce_qty(raw: Any) -> int | None:
    """Quantity as an int, or None when the value cannot be trusted as a number.

    None means UNKNOWN/UNPARSEABLE and must render as such. Negative values are
    LEGAL and preserved -- the doc's own example shows `atpQty: -38`, and
    clamping an oversold position to zero would hide the very thing an operator
    needs to see.
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if float(raw).is_integer() else None
    text = str(raw).strip().replace(",", "")
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return int(value) if value.is_integer() else None


# ─────────────────────────────────────────────────────────────────────────────
# Response envelope
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class DeposcoResponse:
    """One successful read. `env` rides along so a consumer physically cannot
    write a UA figure into a file labelled prod."""

    env: str
    path: str
    status: int
    text: str
    content_type: str = ""

    @property
    def looks_like_xml(self) -> bool:
        """Sniff the BODY. The gateway's content-type header is not reliable.

        Verified live 2026-08-14: the Enterprise Inventory endpoint answers
        `content-type: application/xml` with a JSON body. Trusting the header sent
        that payload into the XML parser, which failed with "invalid token: line
        1, column 0" -- i.e. the inventory read died on a header the server got
        wrong. The first non-whitespace character is the ground truth; the header
        is consulted only when there is no body to look at.
        """
        stripped = self.text.lstrip()
        if stripped[:1] == "<":
            return True
        if stripped[:1] in ("{", "["):
            return False
        return "xml" in self.content_type.lower()

    def json(self) -> Any:
        import json as _json  # noqa: PLC0415

        try:
            return _json.loads(self.text)
        except ValueError as exc:
            raise DeposcoError(f"response was not JSON ({exc.__class__.__name__})") from None

    def xml(self) -> ET.Element:
        try:
            return ET.fromstring(self.text)
        except ET.ParseError as exc:
            raise DeposcoError(f"response was not well-formed XML ({exc})") from None


# ─────────────────────────────────────────────────────────────────────────────
# Client
# ─────────────────────────────────────────────────────────────────────────────


class DeposcoClient:
    """GET-only Deposco V1 client.

    There is no `post`, `put`, `patch`, or `delete` -- not disabled, ABSENT. The
    single verb string in this module is the literal "GET" inside `_get`.
    """

    def __init__(
        self,
        env: str = "prod",
        tenant: str | None = None,
        business_unit: str | None = None,
        transport: Any = None,
        pace_seconds: float = PACE_SECONDS,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        if env not in ENVIRONMENTS:
            raise DeposcoUsageError(
                f"unknown environment {env!r} -- expected one of {sorted(ENVIRONMENTS)}"
            )
        self.env_spec = ENVIRONMENTS[env]
        self.env = self.env_spec.name
        self.tenant = (tenant or os.environ.get("DEPOSCO_TENANT") or DEFAULT_TENANT).strip()
        self.business_unit = (
            business_unit or os.environ.get("DEPOSCO_BU") or DEFAULT_BUSINESS_UNIT
        ).strip()
        self.base_url = self.env_spec.base_template.format(tenant=self.tenant)
        self._pace = pace_seconds
        self._timeout = timeout
        self._transport = transport
        self._last_call = 0.0

        user = (os.environ.get(self.env_spec.user_key) or "").strip()
        password = (os.environ.get(self.env_spec.pass_key) or "").strip()
        if not user or not password:
            missing = [
                k
                for k, v in ((self.env_spec.user_key, user), (self.env_spec.pass_key, password))
                if not v
            ]
            # Names the KEY, never a value -- and a missing prod pair is the known
            # Harrison execution-list item, so say so rather than just failing.
            raise DeposcoAuthError(
                f"{self.env} credentials missing from the environment: "
                f"{', '.join(missing)}. Add them to C:\\Users\\Harri\\code\\cora\\.env."
            )
        self._secrets = tuple(s for s in (user, password) if s)
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self._auth_header = f"Basic {token}"

    # -- credential hygiene ------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover -- trivial, but load-bearing
        return f"<DeposcoClient env={self.env} tenant={self.tenant} bu={self.business_unit}>"

    def _scrub(self, text: str) -> str:
        """Last line of defence: strip credential material from any string this
        client is about to raise or log. Nothing should put it there in the first
        place -- this exists so that a future edit which does cannot leak."""
        out = str(text)
        for secret in self._secrets:
            if secret:
                out = out.replace(secret, "[redacted]")
        if self._auth_header:
            out = out.replace(self._auth_header, "[redacted]")
        return out

    def _fail(self, cls: type[DeposcoError], message: str) -> DeposcoError:
        return cls(self._scrub(message))

    # -- the one and only request primitive --------------------------------

    def _get(self, path: str, params: dict[str, Any] | None = None) -> DeposcoResponse:
        """Issue one paced, retried, timeout-bounded READ.

        Retries cover the two documented failure modes -- a blank 200 (Deposco
        doctrine: that is a FAILURE, not an empty result) and transient
        429/5xx -- and give up loudly rather than returning something a caller
        could mistake for "no data".
        """
        _check_path(path)
        url = self.base_url + path
        headers = {"Authorization": self._auth_header, "Accept": _ACCEPT}

        blank_seen = 0
        transient_seen = 0
        attempt = 0
        while True:
            attempt += 1
            self._sleep_for_pace()
            try:
                response = self._send(url, headers, params)
            except httpx.HTTPError as exc:
                # Never surface the exception object: httpx reprs can carry the
                # request, and a future httpx could include headers in it.
                transient_seen += 1
                if transient_seen > TRANSIENT_RETRIES:
                    raise self._fail(
                        DeposcoUnavailable,
                        f"{self.env}: network error after {TRANSIENT_RETRIES} retries "
                        f"on {path} ({exc.__class__.__name__})",
                    ) from None
                self._backoff(transient_seen)
                continue

            status = response.status_code
            body = response.text or ""

            if status in (401, 403):
                raise self._fail(
                    DeposcoAuthError,
                    f"{self.env}: credentials rejected (HTTP {status}) on {path}. "
                    f"Check {self.env_spec.user_key}/{self.env_spec.pass_key} in .env.",
                )

            if status == 200 and not body.strip():
                blank_seen += 1
                log.warning(
                    "deposco[%s]: blank 200 on %s (attempt %d) -- Deposco doctrine "
                    "says this is a FAILURE, retrying",
                    self.env, path, blank_seen,
                )
                if blank_seen >= BLANK_200_RETRIES:
                    raise self._fail(
                        DeposcoUnavailable,
                        f"{self.env}: {BLANK_200_RETRIES} consecutive blank 200 responses "
                        f"on {path}. Per Deposco (doc p. 25) this is a FAILURE, not an "
                        f"empty result -- do not treat it as zero stock.",
                    )
                self._backoff(blank_seen)
                continue

            if status == 429 or 500 <= status < 600:
                transient_seen += 1
                if transient_seen > TRANSIENT_RETRIES:
                    raise self._fail(
                        DeposcoUnavailable,
                        f"{self.env}: HTTP {status} after {TRANSIENT_RETRIES} retries on {path}",
                    )
                self._backoff(transient_seen)
                continue

            if status >= 400:
                hint = ""
                if status == 400 and "%20" in path:
                    hint = " (a space in the PATH -- see finding 3)"
                raise self._fail(
                    DeposcoError,
                    f"{self.env}: HTTP {status} on {path}{hint}. Body: {body[:400]}",
                )

            return DeposcoResponse(
                env=self.env,
                path=path,
                status=status,
                text=body,
                content_type=response.headers.get("content-type", ""),
            )

    def _send(self, url: str, headers: dict[str, str], params: dict[str, Any] | None):
        """The single place an HTTP verb is named in this module."""
        if self._transport is not None:
            return self._transport(url=url, headers=headers, params=params)
        with httpx.Client(timeout=self._timeout, verify=_ssl_context()) as client:
            return client.request("GET", url, headers=headers, params=params)

    def _sleep_for_pace(self) -> None:
        if self._pace <= 0:
            return
        elapsed = time.monotonic() - self._last_call
        if elapsed < self._pace:
            time.sleep(self._pace - elapsed)
        self._last_call = time.monotonic()

    def _backoff(self, attempt: int) -> None:
        if self._pace <= 0:  # tests run unpaced
            return
        time.sleep(min(self._pace * (2 ** attempt), 8.0))

    # -- Enterprise Inventory (doc pp. 88-93) ------------------------------

    def get_enterprise_availability(
        self,
        measures: Iterable[str] = ALL_MEASURES,
        item_numbers: Iterable[str] | None = None,
        facility_numbers: Iterable[str] | None = None,
        page_size: int = PAGE_SIZE,
        max_pages: int = MAX_PAGES,
    ) -> "AvailabilityResult":
        """Enterprise Inventory availability, following pagination.

        With `item_numbers` this uses the specific-items route (no pagination
        needed); without it, the all-items route is walked page by page. Hitting
        `max_pages` is reported on the result AND logged -- never silently
        truncated.
        """
        measure_list = [m for m in measures if m]
        if not measure_list:
            raise DeposcoUsageError("at least one measure is required")

        base_params: dict[str, Any] = {"measures": ",".join(measure_list)}
        if facility_numbers:
            base_params["facilityNumbers"] = ",".join(facility_numbers)
        path = f"/enterpriseinventory/{self.business_unit}/availability"

        rows: list[EnterpriseInventoryRow] = []
        truncated = False

        if item_numbers:
            wanted = list(item_numbers)
            params = dict(base_params, itemNumbers=",".join(wanted))
            response = self._get(path, params)
            rows = parse_enterprise_availability(response)
        else:
            # Pages until an EMPTY page, not until a SHORT one. Tenant ESM does
            # honour pageSize (verified live 2026-08-14: 5/5/4 across three pages
            # of 14 items), so a short page really is the last one today -- but
            # if a gateway ever capped pageSize below what we asked, "short means
            # last" would stop early and report a partial warehouse as complete.
            # One extra request per daily run buys immunity to that.
            #
            # The other direction is covered too: a gateway that IGNORED
            # pageNumber would serve page 1 forever, so a page contributing no
            # new item is treated as broken pagination and reported as partial
            # rather than looped on until the cap.
            page = 1
            seen: set[str] = set()
            while True:
                if page > max_pages:
                    truncated = True
                    log.error(
                        "deposco[%s]: availability paging hit the %d-page cap -- "
                        "results are INCOMPLETE and must not be read as a full picture",
                        self.env, max_pages,
                    )
                    break
                params = dict(base_params, pageNumber=str(page), pageSize=str(page_size))
                page_rows = parse_enterprise_availability(self._get(path, params))
                if not page_rows:
                    break
                fresh = [r for r in page_rows if r.item_number not in seen]
                if not fresh:
                    truncated = True
                    log.error(
                        "deposco[%s]: availability page %d repeated items already seen -- "
                        "pagination is not advancing; treating the result as PARTIAL",
                        self.env, page,
                    )
                    break
                seen.update(r.item_number for r in fresh)
                rows.extend(fresh)
                page += 1

        return AvailabilityResult(env=self.env, rows=rows, truncated=truncated)

    # -- Order status (doc pp. 184-196) ------------------------------------

    def find_order(self, order_type: str, number: str) -> DeposcoResponse:
        """Definitive existence check for one order, via `/search/Order`.

        This is the ONLY route verified to filter server-side by number on tenant
        ESM (live 2026-08-14: returned exactly 1 order with its 4 lines). Use it
        for read-back verification; use `get_order_status` for the status detail.
        """
        return self._get("/search/Order", {"type": order_type, "number": number})

    def order_exists(self, order_type: str, number: str) -> bool:
        response = self.find_order(order_type, number)
        return number in response.text

    def get_order_status(self, order_type: str, number: str) -> list["OrderStatus"]:
        """Status records for one order.

        Two live constraints shape this. The documented direct route
        `/status/order/{type}/{number}` is unusable -- `Sales Order` contains a
        space and the gateway flat-400s the encoded form (finding 3). And the
        search route DOES NOT honour `number` on this tenant: it returned every
        order in the tenant (172 KB, 99+ records) while ignoring the filter
        entirely. So the filter is applied here, client-side.

        That means a miss is NOT proof of absence -- the order may simply not be
        in the slice the gateway returned. A miss is logged as indeterminate, and
        callers that need a yes/no should use `order_exists`, which is backed by
        the one route that really filters.
        """
        response = self._get("/status/order/search", {"type": order_type, "number": number})
        records = parse_order_status(response)
        matched = [r for r in records if r.order_number == number]
        if not matched and records:
            log.warning(
                "deposco[%s]: %s not found among %d record(s) returned by the "
                "unfiltered status-search route -- INDETERMINATE, not absent. Use "
                "order_exists() for a definitive answer.",
                self.env, number, len(records),
            )
        return matched

    def get_purchase_order_receipts(self) -> list["ReceiptLine"]:
        """Every receipt line the tenant will surface, with lot + expiry.

        THIS IS THE LOT SOURCE, and it is not the one the doc points at. The
        documented `/search/receiptLine` route is unusable on tenant ESM: every
        field name the doc gives (`company`, `receiptDateTime`) and every
        alternative tried (`businessUnit`, `orderNumber`) returns
        `400 ... are not found or configured for entity [receiptLine]`, and a
        bare call returns a blank 200. Deposco would have to configure search
        fields for that entity (-> Anthony).

        Purchase-order status carries the same data nested under each line, and
        is verified live (2026-08-14, UA: 96 PO records, 87 receipt lines, 16
        with a lot number). Lot rides receipts only -- see the lot-ledger module
        header for why there is no outbound equivalent.
        """
        response = self._get("/status/order/search", {"type": "Purchase Order"})
        return [
            receipt
            for record in parse_order_status(response)
            for line in record.lines
            for receipt in line.receipt_lines
        ]

    def get_order_status_range(self, start: str, end: str) -> list["OrderStatus"]:
        """Status for every order in a date range. `start`/`end` are YYYYMMDD.

        This is the documented `/status/order/{d0},{d1}` route (doc p. 196) --
        space-free, so it works directly.
        """
        for label, value in (("start", start), ("end", end)):
            if not re.fullmatch(r"\d{8}", value or ""):
                raise DeposcoUsageError(f"{label} date must be YYYYMMDD, got {value!r}")
        return parse_order_status(self._get(f"/status/order/{start},{end}"))

    def search_orders(self, order_type: str, **criteria: Any) -> DeposcoResponse:
        """Raw order-header search (`/search/Order`). Returns the envelope so
        callers can pick their own parser -- the OrderHeader schema is much wider
        than the slice the lot ledger needs."""
        return self._get("/search/Order", {"type": order_type, **criteria})

    # -- Shipments (doc pp. 363-396) ---------------------------------------

    def get_shipments(self, ship_date_from: str, ship_date_to: str) -> DeposcoResponse:
        """Shipments in a datetime window (ISO-8601, no timezone suffix per the
        doc's example). Uses the BETWEEN search operator.

        NOTE for the lot ledger: shipment records carry tracking and container
        detail but NO lot number anywhere in V1 -- see `parse_receipt_lines` and
        the module note in `deposco_lot_ledger`.
        """
        return self._get(
            "/search/shipment",
            {
                "actualShipDateOps": "BETWEEN",
                "actualShipDate": f"{ship_date_from},{ship_date_to}",
            },
        )

    # -- Receipt lines (doc pp. 252-262) -- the lot + expiry source ---------

    def get_receipt_lines(self, receipt_number: str) -> list["ReceiptLine"]:
        """Receipt lines for one receipt number."""
        return parse_receipt_lines(
            self._get(f"/receiptlines/{self.business_unit}/{receipt_number}")
        )


def _ssl_context():
    """Windows certificate store when `truststore` is importable (finding 1),
    otherwise httpx's default verification. Verification is NEVER disabled --
    there is deliberately no flag for it in a prod-capable client."""
    try:
        import ssl  # noqa: PLC0415

        import truststore  # noqa: PLC0415

        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except Exception:  # noqa: BLE001 -- fall back to normal verification
        log.debug("deposco: truststore unavailable, using default CA verification")
        return True


# ─────────────────────────────────────────────────────────────────────────────
# Parsed shapes
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class FacilityMeasures:
    facility: str
    measures: dict[str, int | None] = field(default_factory=dict)


@dataclass
class EnterpriseInventoryRow:
    item_number: str
    item_id: str | None = None
    measures: dict[str, int | None] = field(default_factory=dict)
    facilities: list[FacilityMeasures] = field(default_factory=list)

    def measure(self, name: str) -> int | None:
        """None means UNKNOWN -- the tenant did not return this measure. It does
        NOT mean zero, and callers must not render it as one."""
        return self.measures.get(name)


@dataclass
class AvailabilityResult:
    env: str
    rows: list[EnterpriseInventoryRow] = field(default_factory=list)
    #: True when the page cap stopped the walk. The caller MUST treat the result
    #: as partial -- see the coverage floor in run_deposco_inventory_sync.
    truncated: bool = False

    def by_item(self) -> dict[str, EnterpriseInventoryRow]:
        return {row.item_number: row for row in self.rows if row.item_number}


@dataclass
class ReceiptLine:
    order_number: str = ""
    line_number: str = ""
    receipt_number: str = ""
    item_number: str = ""
    quantity: int | None = None
    received_date: str = ""
    lot_number: str = ""
    expiration_date: str = ""
    status: str = ""

    @property
    def has_lot(self) -> bool:
        return bool(self.lot_number)


@dataclass
class OrderStatusLine:
    line_number: str = ""
    item_number: str = ""
    line_status: str = ""
    order_pack_quantity: int | None = None
    shipped_pack_quantity: int | None = None
    received_pack_quantity: int | None = None
    receipt_lines: list[ReceiptLine] = field(default_factory=list)


@dataclass
class OrderStatus:
    order_number: str = ""
    order_type: str = ""
    order_status: str = ""
    lines: list[OrderStatusLine] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Parsers (pure -- no network, so they are testable against captured payloads)
# ─────────────────────────────────────────────────────────────────────────────


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find_all(root: ET.Element, name: str) -> Iterator[ET.Element]:
    """Namespace-agnostic descendant search. Deposco's responses use an `ns2`
    prefix bound to a per-schema namespace, and pinning those would make the
    parser brittle against a schema-version bump."""
    for element in root.iter():
        if _strip_ns(element.tag) == name:
            yield element


def _child_text(element: ET.Element, name: str) -> str:
    for child in element:
        if _strip_ns(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _payload_root(response: DeposcoResponse | Any) -> Any:
    """Accept a DeposcoResponse or an already-decoded payload, so tests and
    callers can hand either one to a parser."""
    if isinstance(response, DeposcoResponse):
        return response.xml() if response.looks_like_xml else response.json()
    return response


def parse_enterprise_availability(response: Any) -> list[EnterpriseInventoryRow]:
    """Normalize an Enterprise Inventory availability payload.

    Handles BOTH documented shapes -- the all-items route returns a bare object
    `{"enterpriseInventory": [...]}` (doc p. 90) while the specific-items route
    returns it LIST-WRAPPED, `[{"enterpriseInventory": [...]}]` (doc p. 92). The
    doc is internally inconsistent here, so the parser accepts either rather than
    betting on one; an unexpected shape yields no rows, which the caller's
    coverage floor then reports as a failure instead of as empty stock.

    Every measure key that is absent stays absent -- it is UNKNOWN, not zero.
    """
    payload = _payload_root(response)
    if isinstance(payload, ET.Element):  # XML variant, should the server pick it
        return _parse_availability_xml(payload)

    blocks: list[Any] = []
    if isinstance(payload, dict):
        blocks = [payload]
    elif isinstance(payload, list):
        blocks = [b for b in payload if isinstance(b, dict)]

    rows: list[EnterpriseInventoryRow] = []
    for block in blocks:
        entries = block.get("enterpriseInventory")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            row = EnterpriseInventoryRow(
                item_number=str(entry.get("itemNumber") or "").strip(),
                item_id=(str(entry["itemId"]) if entry.get("itemId") is not None else None),
                measures={m: coerce_qty(entry[m]) for m in ALL_MEASURES if m in entry},
            )
            facilities = entry.get("facilities")
            if isinstance(facilities, list):
                for fac in facilities:
                    if not isinstance(fac, dict):
                        continue
                    row.facilities.append(
                        FacilityMeasures(
                            facility=str(fac.get("facility") or "").strip(),
                            measures={m: coerce_qty(fac[m]) for m in ALL_MEASURES if m in fac},
                        )
                    )
            rows.append(row)
    return rows


def _parse_availability_xml(root: ET.Element) -> list[EnterpriseInventoryRow]:
    rows: list[EnterpriseInventoryRow] = []
    for entry in _find_all(root, "enterpriseInventory"):
        row = EnterpriseInventoryRow(
            item_number=_child_text(entry, "itemNumber"),
            item_id=_child_text(entry, "itemId") or None,
        )
        for measure in ALL_MEASURES:
            text = _child_text(entry, measure)
            if text:
                row.measures[measure] = coerce_qty(text)
        for fac in entry:
            if _strip_ns(fac.tag) != "facilities":
                continue
            block = FacilityMeasures(facility=_child_text(fac, "facility"))
            for measure in ALL_MEASURES:
                text = _child_text(fac, measure)
                if text:
                    block.measures[measure] = coerce_qty(text)
            row.facilities.append(block)
        rows.append(row)
    return rows


def parse_receipt_lines(response: Any) -> list[ReceiptLine]:
    """Receipt lines from the receiptLine search / retrieval routes.

    Field names follow the receipt-line reference (doc pp. 228-230). `lotNumber`
    and `expirationDate` are documented there but do NOT appear in the doc's own
    XML example, so their presence is verified per-response rather than assumed
    (`has_lot`) -- the caller reports how many receipts actually carried a lot
    instead of implying full lot coverage.
    """
    payload = _payload_root(response)
    if not isinstance(payload, ET.Element):
        return _parse_receipt_lines_json(payload)

    out: list[ReceiptLine] = []
    for element in _find_all(payload, "receiptLine"):
        out.append(
            ReceiptLine(
                order_number=_child_text(element, "orderNumber"),
                line_number=_child_text(element, "lineNumber"),
                receipt_number=_child_text(element, "number")
                or _child_text(element, "receiptLineNumber"),
                item_number=_child_text(element, "itemNumber"),
                quantity=coerce_qty(
                    _child_text(element, "quantity")
                    or _child_text(element, "receivedPackQuantity")
                ),
                received_date=_child_text(element, "receivedDate"),
                lot_number=_child_text(element, "lotNumber"),
                expiration_date=_child_text(element, "expirationDate"),
                status=_child_text(element, "status"),
            )
        )
    return out


def _parse_receipt_lines_json(payload: Any) -> list[ReceiptLine]:
    entries: list[Any] = []
    if isinstance(payload, dict):
        raw = payload.get("receiptLines") or payload.get("receiptLine")
        if isinstance(raw, list):
            entries = raw
        elif isinstance(raw, dict):
            entries = [raw]
    elif isinstance(payload, list):
        entries = payload
    out: list[ReceiptLine] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        out.append(
            ReceiptLine(
                order_number=str(entry.get("orderNumber") or ""),
                line_number=str(entry.get("lineNumber") or ""),
                receipt_number=str(entry.get("number") or entry.get("receiptLineNumber") or ""),
                item_number=str(entry.get("itemNumber") or ""),
                quantity=coerce_qty(
                    entry.get("quantity", entry.get("receivedPackQuantity"))
                ),
                received_date=str(entry.get("receivedDate") or ""),
                lot_number=str(entry.get("lotNumber") or ""),
                expiration_date=str(entry.get("expirationDate") or ""),
                status=str(entry.get("status") or ""),
            )
        )
    return out


def parse_order_headers(response: Any) -> list[tuple[str, int]]:
    """`(order_number, line_count)` from an OrderHeader payload (`/search/Order`).

    A deliberately thin read: `/search/Order` is used for existence and read-back
    verification, where the number and the line count are the whole question. The
    full OrderHeader schema is much wider than anything Phase 1 consumes.
    """
    payload = _payload_root(response)
    if not isinstance(payload, ET.Element):
        return []
    out: list[tuple[str, int]] = []
    for order in _find_all(payload, "order"):
        number = _child_text(order, "number")
        if not number:
            continue
        out.append((number, sum(1 for _ in _find_all(order, "orderLine"))))
    return out


def parse_order_status(response: Any) -> list[OrderStatus]:
    """Order-status records, including the nested receipt lines that carry lot +
    expiry on PURCHASE orders (doc pp. 185-196)."""
    payload = _payload_root(response)
    if not isinstance(payload, ET.Element):
        return []

    out: list[OrderStatus] = []
    for element in _find_all(payload, "orderStatus"):
        # `orderStatus` names both the record and one of its children; the record
        # is the one with structure under it.
        if len(element) == 0:
            continue
        record = OrderStatus(
            order_number=_child_text(element, "orderNumber"),
            order_type=_child_text(element, "orderType"),
            order_status=_child_text(element, "orderStatus"),
        )
        for line in _find_all(element, "line"):
            parsed = OrderStatusLine(
                line_number=_child_text(line, "lineNumber"),
                item_number=_child_text(line, "itemNumber"),
                line_status=_child_text(line, "lineStatus"),
                order_pack_quantity=coerce_qty(_child_text(line, "orderPackQuantity")),
                shipped_pack_quantity=coerce_qty(_child_text(line, "shippedPackQuantity")),
                received_pack_quantity=coerce_qty(_child_text(line, "receivedPackQuantity")),
            )
            for receipt in _find_all(line, "receiptLine"):
                parsed.receipt_lines.append(
                    ReceiptLine(
                        order_number=record.order_number,
                        line_number=parsed.line_number,
                        receipt_number=_child_text(receipt, "receiptLineNumber"),
                        item_number=parsed.item_number,
                        quantity=coerce_qty(_child_text(receipt, "receivedPackQuantity")),
                        received_date=_child_text(receipt, "receivedDate"),
                        lot_number=_child_text(receipt, "lotNumber"),
                        expiration_date=_child_text(receipt, "expirationDate"),
                        status=parsed.line_status,
                    )
                )
            record.lines.append(parsed)
        out.append(record)
    return out
