"""Tests for the READ-ONLY Deposco V1 client (Phase 1).

Organized around the named D-051 review lenses for this build, because each one
guards a failure mode that a green suite would otherwise hide:

  1. credential egress          6. path-space compliance (finding 3)
  2. blank-200 / shape honesty  4. write-impossibility
  5. UA/prod swap safety        + parser fidelity to the doc's own examples
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from cora.connectors import deposco_client as dc

_SOURCE = Path(dc.__file__).read_text(encoding="utf-8")


def _code_only(source: str) -> str:
    """The module's EXECUTABLE code, with comments and docstrings removed.

    Source-grep pins must look at code, not prose. The first cut of these tests
    grepped `_SOURCE` directly and failed on the module docstring, which quite
    reasonably *names* the routes and verbs it forbids -- the same
    pin-matches-the-comment-explaining-it trap that has bitten this repo before.
    `ast.unparse` drops comments for free; docstrings are stripped explicitly.
    """
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = node.body
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            node.body = body[1:] or [ast.Pass()]
    return ast.unparse(tree)


_CODE = _code_only(_SOURCE)

USER = "F3 API Integration"
PASSWORD = "s3cr3t-ua-pass"


class FakeResponse:
    def __init__(self, status_code=200, text="", content_type="application/json"):
        self.status_code = status_code
        self.text = text
        self.headers = {"content-type": content_type}


class FakeTransport:
    """Records every call so tests can assert on the URLs actually built."""

    def __init__(self, *responses):
        self._queue = list(responses)
        self.calls: list[dict] = []

    def __call__(self, url, headers, params):
        self.calls.append({"url": url, "headers": headers, "params": params})
        if not self._queue:
            return FakeResponse(200, "{}")
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def urls(self) -> list[str]:
        return [c["url"] for c in self.calls]


@pytest.fixture
def ua_env(monkeypatch):
    monkeypatch.setenv("DEPOSCO_UA_USER", USER)
    monkeypatch.setenv("DEPOSCO_UA_PASS", PASSWORD)
    monkeypatch.setenv("DEPOSCO_TENANT", "ESM")
    monkeypatch.setenv("DEPOSCO_BU", "F3E")


@pytest.fixture
def prod_env(monkeypatch):
    monkeypatch.setenv("DEPOSCO_PROD_USER", "prod-user")
    monkeypatch.setenv("DEPOSCO_PROD_PASS", "prod-pass")
    monkeypatch.setenv("DEPOSCO_TENANT", "ESM")
    monkeypatch.setenv("DEPOSCO_BU", "F3E")


def client(env="ua", transport=None, **kw):
    return dc.DeposcoClient(env=env, transport=transport, pace_seconds=0, **kw)


# ── Lens 4: write-impossibility ──────────────────────────────────────────────


class TestWriteImpossibility:
    def test_client_exposes_no_mutating_method(self):
        for verb in ("post", "put", "patch", "delete", "create", "update", "push"):
            assert not hasattr(dc.DeposcoClient, verb), f"DeposcoClient.{verb} must not exist"

    def test_source_names_no_http_verb_other_than_get(self):
        """The single verb string in the module is the literal "GET" in `_send`.

        Greps the SOURCE, not the behaviour: a push method added by a future
        refactor fails here even if no test ever calls it.
        """
        found = set(re.findall(r"""['"](POST|PUT|PATCH|DELETE)['"]""", _CODE))
        assert not found, f"mutating HTTP verb string present in deposco_client.py: {found}"
        assert len(re.findall(r"""['"]GET['"]""", _CODE)) == 1

    def test_only_one_request_primitive(self):
        # httpx is reached through exactly one call site, so there is a single
        # place to review when Phase 2 adds writes.
        assert _CODE.count("client.request(") == 1
        assert "httpx.Client(" in _CODE

    def test_get_is_the_only_verb_sent(self, ua_env):
        t = FakeTransport(FakeResponse(200, "{}"))
        c = client(transport=t)
        c._get("/items/F3E/PURE-Original")
        assert t.calls, "transport should have been called"


# ── Lens 1: credential egress ────────────────────────────────────────────────


class TestCredentialEgress:
    def test_repr_carries_no_credential(self, ua_env):
        c = client()
        text = repr(c)
        assert PASSWORD not in text and USER not in text
        assert "Basic" not in text

    def test_auth_rejection_message_has_no_credential(self, ua_env):
        t = FakeTransport(FakeResponse(401, "denied"))
        c = client(transport=t)
        with pytest.raises(dc.DeposcoAuthError) as exc:
            c._get("/status/order/search")
        message = str(exc.value)
        assert PASSWORD not in message and "Basic" not in message
        # It should still be actionable: name the KEY, never the value.
        assert "DEPOSCO_UA_USER" in message

    def test_scrub_removes_credentials_that_reach_a_message(self, ua_env):
        c = client()
        leaked = f"boom user={USER} pass={PASSWORD} auth={c._auth_header}"
        cleaned = c._scrub(leaked)
        assert PASSWORD not in cleaned
        assert USER not in cleaned
        assert c._auth_header not in cleaned
        assert cleaned.count("[redacted]") >= 3

    def test_error_body_excerpt_is_scrubbed(self, ua_env):
        """A server that echoes the credential back must not get it into our text."""
        t = FakeTransport(FakeResponse(422, f"rejected for {PASSWORD}"))
        c = client(transport=t)
        with pytest.raises(dc.DeposcoError) as exc:
            c._get("/items/F3E/PURE-Original")
        assert PASSWORD not in str(exc.value)

    def test_missing_credentials_names_the_key_not_a_value(self, monkeypatch):
        monkeypatch.delenv("DEPOSCO_PROD_USER", raising=False)
        monkeypatch.delenv("DEPOSCO_PROD_PASS", raising=False)
        with pytest.raises(dc.DeposcoAuthError) as exc:
            dc.DeposcoClient(env="prod", pace_seconds=0)
        assert "DEPOSCO_PROD_USER" in str(exc.value)
        assert "DEPOSCO_PROD_PASS" in str(exc.value)

    def test_credentials_never_ride_in_the_url(self, ua_env):
        t = FakeTransport(FakeResponse(200, "{}"))
        c = client(transport=t)
        c._get("/enterpriseinventory/F3E/availability", {"measures": "atpQty"})
        assert PASSWORD not in t.urls[0]
        assert "@" not in t.urls[0].split("//", 1)[1].split("/", 1)[0]


# ── Lens 5: UA / prod swap safety ────────────────────────────────────────────


class TestEnvironmentSafety:
    def test_ua_and_prod_hit_different_hosts(self, ua_env, prod_env):
        assert "sandboxapi.deposco.com" in client("ua").base_url
        assert "api.deposco.com" in client("prod").base_url
        assert "sandbox" not in client("prod").base_url

    def test_each_env_reads_only_its_own_credential_pair(self, monkeypatch):
        """A prod run must never silently fall back to the sandbox pair."""
        monkeypatch.setenv("DEPOSCO_UA_USER", USER)
        monkeypatch.setenv("DEPOSCO_UA_PASS", PASSWORD)
        monkeypatch.delenv("DEPOSCO_PROD_USER", raising=False)
        monkeypatch.delenv("DEPOSCO_PROD_PASS", raising=False)
        with pytest.raises(dc.DeposcoAuthError):
            dc.DeposcoClient(env="prod", pace_seconds=0)

    def test_unknown_env_is_refused(self, ua_env):
        with pytest.raises(dc.DeposcoUsageError):
            dc.DeposcoClient(env="staging", pace_seconds=0)

    def test_env_is_stamped_on_every_response(self, ua_env):
        t = FakeTransport(FakeResponse(200, "{}"))
        response = client(transport=t)._get("/items/F3E/PURE-Original")
        assert response.env == "ua"

    def test_availability_result_carries_the_env(self, ua_env):
        t = FakeTransport(FakeResponse(200, '{"enterpriseInventory": []}'))
        result = client(transport=t).get_enterprise_availability(item_numbers=["PURE-Original"])
        assert result.env == "ua"

    def test_no_verification_disable_flag_exists(self):
        """The UA rehearsal script has `--insecure`; a prod-capable client must
        never inherit one."""
        assert "CERT_NONE" not in _SOURCE
        assert "check_hostname = False" not in _SOURCE
        assert "verify=False" not in _SOURCE


# ── Lens 6: path-space compliance (finding 3) ────────────────────────────────


class TestPathSpaceCompliance:
    @pytest.mark.parametrize(
        "path",
        [
            "/orders/Sales Order/TEST-GOTHAM-001",
            "/orders/Sales%20Order/TEST-GOTHAM-001",
            "/status/order/Sales%20order/X",
        ],
    )
    def test_space_in_path_is_refused(self, path):
        with pytest.raises(dc.DeposcoUsageError, match="space in the PATH"):
            dc._check_path(path)

    def test_space_in_query_is_allowed(self):
        assert dc._check_path("/search/Order?type=Sales%20Order&number=X")
        assert dc._check_path("/status/order/search?type=Sales Order")

    def test_order_status_uses_the_search_route_not_the_documented_direct_one(self, ua_env):
        t = FakeTransport(FakeResponse(200, "<orders/>", "application/xml"))
        client(transport=t).get_order_status("Sales Order", "TEST-GOTHAM-001")
        url = t.urls[0]
        assert url.endswith("/status/order/search")
        assert "%20" not in url
        # the type rides the QUERY, where spaces are fine
        assert t.calls[0]["params"]["type"] == "Sales Order"

    def test_no_path_builder_in_the_module_interpolates_an_order_type(self):
        """Guards against a future edit reintroducing the dead direct route.

        Checked against CODE only -- the module docstring names these routes on
        purpose, to explain why they are unusable.
        """
        assert "/orders/Sales" not in _CODE
        assert "/status/order/{order_type}" not in _CODE
        assert "/orders/" not in _CODE

    def test_date_range_route_is_space_free_and_validated(self, ua_env):
        t = FakeTransport(FakeResponse(200, "<orders/>", "application/xml"))
        c = client(transport=t)
        c.get_order_status_range("20260813", "20260815")
        assert t.urls[0].endswith("/status/order/20260813,20260815")
        with pytest.raises(dc.DeposcoUsageError):
            c.get_order_status_range("2026-08-13", "20260815")


# ── Lens 2: blank-200 and shape honesty ──────────────────────────────────────


class TestBlank200AndFailureHonesty:
    def test_blank_200_retries_then_raises_rather_than_returning_empty(self, ua_env):
        t = FakeTransport(*[FakeResponse(200, "   ")] * dc.BLANK_200_RETRIES)
        with pytest.raises(dc.DeposcoUnavailable, match="blank 200"):
            client(transport=t)._get("/enterpriseinventory/F3E/availability")
        assert len(t.calls) == dc.BLANK_200_RETRIES

    def test_blank_200_that_recovers_is_returned(self, ua_env):
        t = FakeTransport(FakeResponse(200, ""), FakeResponse(200, '{"ok": 1}'))
        response = client(transport=t)._get("/items/F3E/PURE-Original")
        assert response.json() == {"ok": 1}

    def test_blank_200_is_never_reported_as_zero_stock(self, ua_env):
        """The whole point of the doctrine: a blank body must not become a number."""
        t = FakeTransport(*[FakeResponse(200, "")] * dc.BLANK_200_RETRIES)
        with pytest.raises(dc.DeposcoUnavailable) as exc:
            client(transport=t).get_enterprise_availability(item_numbers=["PURE-Original"])
        assert "not an empty result" in str(exc.value)

    def test_transient_5xx_is_retried_then_raises(self, ua_env):
        t = FakeTransport(*[FakeResponse(503, "nope")] * (dc.TRANSIENT_RETRIES + 1))
        with pytest.raises(dc.DeposcoUnavailable, match="503"):
            client(transport=t)._get("/items/F3E/PURE-Original")

    def test_429_is_retried_then_recovers(self, ua_env):
        t = FakeTransport(FakeResponse(429, "slow down"), FakeResponse(200, '{"ok": 1}'))
        assert client(transport=t)._get("/items/F3E/X").json() == {"ok": 1}

    def test_flat_400_surfaces_the_body(self, ua_env):
        t = FakeTransport(FakeResponse(400, "parser rejected the payload"))
        with pytest.raises(dc.DeposcoError, match="parser rejected"):
            client(transport=t)._get("/enterpriseinventory/F3E/availability")

    def test_renamed_json_keys_yield_no_rows_rather_than_silent_zeros(self):
        """cq-db2fd53aa608: verify key names against a LIVE response.

        If Deposco renames the envelope, we must produce nothing (which the
        caller's coverage floor reports as a failure) -- never rows of zeros.
        """
        renamed = {"enterprise_inventory": [{"itemNumber": "PURE-Original", "atpQty": 5}]}
        assert dc.parse_enterprise_availability(renamed) == []

    def test_garbage_payload_yields_no_rows(self):
        for payload in (None, "", [], {}, {"enterpriseInventory": "not-a-list"}, 42):
            assert dc.parse_enterprise_availability(payload) == []

    def test_page_cap_is_reported_not_silently_truncated(self, ua_env):
        full = '{"enterpriseInventory": [%s]}' % ",".join(
            '{"itemNumber": "SKU%d", "atpQty": 1}' % i for i in range(dc.PAGE_SIZE)
        )
        t = FakeTransport(*[FakeResponse(200, full)] * 5)
        result = client(transport=t).get_enterprise_availability(max_pages=2)
        assert result.truncated is True

    def test_short_page_ends_pagination(self, ua_env):
        t = FakeTransport(FakeResponse(200, '{"enterpriseInventory": [{"itemNumber": "A"}]}'))
        result = client(transport=t).get_enterprise_availability(page_size=dc.PAGE_SIZE)
        assert result.truncated is False
        assert [r.item_number for r in result.rows] == ["A"]


# ── Parser fidelity to the doc's own examples ────────────────────────────────


class TestAvailabilityParsing:
    #: doc p. 90 -- the ALL-ITEMS route returns a bare object
    BARE = {
        "enterpriseInventory": [
            {
                "itemId": 1046,
                "itemNumber": "136200",
                "openOrderLineQty": 0,
                "atpQty": 1122,
                "coAtpQty": 1127,
                "facilities": [
                    {"facility": "MS", "coAtpQty": 168, "atpQty": 167},
                    {"facility": "TN", "atpQty": -38, "coAtpQty": -34},
                ],
            }
        ]
    }
    #: doc p. 92 -- the SPECIFIC-ITEMS route returns the same thing LIST-WRAPPED
    WRAPPED = [
        {
            "enterpriseInventory": [
                {
                    "itemId": 1,
                    "itemNumber": "Item1",
                    "totalOnHandQty": 101348,
                    "atpQty": 98267,
                    "facilities": [
                        {"facility": "NY-FC", "totalOnHandQty": 101348, "qtyOnPO": 24},
                    ],
                }
            ]
        }
    ]

    def test_both_documented_shapes_parse(self):
        assert dc.parse_enterprise_availability(self.BARE)[0].item_number == "136200"
        assert dc.parse_enterprise_availability(self.WRAPPED)[0].item_number == "Item1"

    def test_absent_measure_is_unknown_not_zero(self):
        """The doc's own p.90 example omits totalOnHandQty entirely."""
        row = dc.parse_enterprise_availability(self.BARE)[0]
        assert row.measure("totalOnHandQty") is None
        assert "totalOnHandQty" not in row.measures

    def test_negative_quantities_are_preserved(self):
        row = dc.parse_enterprise_availability(self.BARE)[0]
        tn = [f for f in row.facilities if f.facility == "TN"][0]
        assert tn.measures["atpQty"] == -38

    def test_facility_measures_are_independent_subsets(self):
        row = dc.parse_enterprise_availability(self.WRAPPED)[0]
        assert row.facilities[0].measures["qtyOnPO"] == 24
        assert "atpQty" not in row.facilities[0].measures

    def test_unparseable_quantity_is_none_not_zero(self):
        payload = {"enterpriseInventory": [{"itemNumber": "X", "atpQty": "n/a"}]}
        assert dc.parse_enterprise_availability(payload)[0].measure("atpQty") is None

    def test_by_item_index(self):
        result = dc.AvailabilityResult("prod", dc.parse_enterprise_availability(self.BARE))
        assert "136200" in result.by_item()


class TestCoerceQty:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (0, 0), (5, 5), (-38, -38), ("101348", 101348), ("1,122", 1122),
            (5.0, 5), ("5.0", 5), (None, None), ("", None), ("n/a", None),
            (5.5, None), (True, None), ("  12  ", 12),
        ],
    )
    def test_coercion(self, raw, expected):
        assert dc.coerce_qty(raw) is expected or dc.coerce_qty(raw) == expected

    def test_zero_and_unknown_are_distinguishable(self):
        assert dc.coerce_qty(0) == 0
        assert dc.coerce_qty("garbage") is None
        assert dc.coerce_qty(0) is not None


# ── Receipt lines: the lot + expiry source ───────────────────────────────────

RECEIPT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ns2:receiptLines xmlns:ns2="http://integration.deposco.com/receiptline">
  <receiptLine>
    <orderType>Purchase Order</orderType>
    <orderNumber>SPPO92_11</orderNumber>
    <lineNumber>SPPO92_11--2</lineNumber>
    <number>47</number>
    <itemNumber>PURE-Original</itemNumber>
    <quantity>2</quantity>
    <receivedDate>2021-10-25</receivedDate>
    <lotNumber>L-2026-01</lotNumber>
    <expirationDate>2027-09-02T00:00:00-04:00</expirationDate>
    <status>Received</status>
  </receiptLine>
  <receiptLine>
    <orderNumber>SPPO92_12</orderNumber>
    <itemNumber>PURESL</itemNumber>
    <quantity>4</quantity>
    <status>Received</status>
  </receiptLine>
</ns2:receiptLines>"""


class TestReceiptLineParsing:
    def test_namespaced_xml_parses(self):
        lines = dc.parse_receipt_lines(dc.DeposcoResponse("prod", "/x", 200, RECEIPT_XML))
        assert [line.item_number for line in lines] == ["PURE-Original", "PURESL"]

    def test_lot_and_expiry_are_captured(self):
        line = dc.parse_receipt_lines(dc.DeposcoResponse("prod", "/x", 200, RECEIPT_XML))[0]
        assert line.lot_number == "L-2026-01"
        assert line.expiration_date.startswith("2027-09-02")
        assert line.has_lot is True

    def test_missing_lot_is_reported_not_invented(self):
        """The doc's own receiptLine example carries NO lot, so absence is a real
        state -- it must be visible, not defaulted."""
        line = dc.parse_receipt_lines(dc.DeposcoResponse("prod", "/x", 200, RECEIPT_XML))[1]
        assert line.lot_number == ""
        assert line.has_lot is False

    def test_json_variant_parses(self):
        payload = {"receiptLines": [{"itemNumber": "PURE-Citrus", "quantity": 9,
                                     "lotNumber": "L9", "expirationDate": "2027-01-01"}]}
        line = dc.parse_receipt_lines(payload)[0]
        assert line.item_number == "PURE-Citrus" and line.quantity == 9 and line.has_lot

    def test_receipts_come_from_purchase_order_status_not_the_receiptline_search(self, ua_env):
        """LIVE FINDING 2026-08-14: `/search/receiptLine` is unusable on tenant
        ESM -- every documented field name (`company`, `receiptDateTime`) and
        every alternative tried returns `400 ... not found or configured for
        entity [receiptLine]`, and a bare call returns a blank 200. PO status
        carries the same data nested, and was verified live (87 receipt lines,
        16 with a lot)."""
        t = FakeTransport(FakeResponse(200, ORDER_STATUS_XML, "application/xml"))
        receipts = client(transport=t).get_purchase_order_receipts()
        assert t.urls[0].endswith("/status/order/search")
        assert t.calls[0]["params"]["type"] == "Purchase Order"
        assert [r.lot_number for r in receipts] == ["12345"]

    def test_the_dead_receiptline_search_route_is_not_reachable_from_the_client(self):
        assert not hasattr(dc.DeposcoClient, "search_receipt_lines")
        assert "/search/receiptLine" not in _CODE


# ── Order status, including nested PO receipt lines ──────────────────────────

ORDER_STATUS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<ns2:orders xmlns:ns2="http://integration.deposco.com/orderstatus">
  <orderStatus>
    <orderNumber>PO12345</orderNumber>
    <orderStatus>Received</orderStatus>
    <orderType>Purchase Order</orderType>
    <lines>
      <line>
        <lineNumber>PO12345--1</lineNumber>
        <lineStatus>Received</lineStatus>
        <itemNumber>PURE-Tropical</itemNumber>
        <orderPackQuantity>100.0</orderPackQuantity>
        <shippedPackQuantity>0.0</shippedPackQuantity>
        <receivedPackQuantity>100.0</receivedPackQuantity>
        <receiptLines>
          <receiptLine>
            <receiptLineNumber>143</receiptLineNumber>
            <receivedPackQuantity>100.0</receivedPackQuantity>
            <receivedDate>2026-05-19T12:14:46-04:00</receivedDate>
            <lotNumber>12345</lotNumber>
            <expirationDate>2027-09-02T00:00:00-04:00</expirationDate>
          </receiptLine>
        </receiptLines>
      </line>
    </lines>
    <shipments/>
  </orderStatus>
</ns2:orders>"""


class TestOrderStatusParsing:
    def test_header_fields(self):
        record = dc.parse_order_status(dc.DeposcoResponse("ua", "/x", 200, ORDER_STATUS_XML))[0]
        assert record.order_number == "PO12345"
        assert record.order_type == "Purchase Order"
        assert record.order_status == "Received"

    def test_line_quantities_coerce_from_decimal_strings(self):
        record = dc.parse_order_status(dc.DeposcoResponse("ua", "/x", 200, ORDER_STATUS_XML))[0]
        line = record.lines[0]
        assert line.order_pack_quantity == 100
        assert line.received_pack_quantity == 100
        assert line.item_number == "PURE-Tropical"

    def test_nested_receipt_lines_carry_lot_and_inherit_the_item(self):
        record = dc.parse_order_status(dc.DeposcoResponse("ua", "/x", 200, ORDER_STATUS_XML))[0]
        receipt = record.lines[0].receipt_lines[0]
        assert receipt.lot_number == "12345"
        assert receipt.item_number == "PURE-Tropical"   # inherited from the parent line
        assert receipt.order_number == "PO12345"
        assert receipt.quantity == 100

    def test_status_search_filters_client_side_because_the_server_does_not(self, ua_env):
        """LIVE FINDING 2026-08-14: `/status/order/search` IGNORES `number` on
        this tenant -- it returned every order in the tenant (172 KB, 99 records)
        while the filter was set. Returning that unfiltered pile to a caller who
        asked for one order would be the worst outcome, so the filter is applied
        here."""
        t = FakeTransport(FakeResponse(200, ORDER_STATUS_XML, "application/xml"))
        assert client(transport=t).get_order_status("Purchase Order", "PO99999") == []

    def test_a_status_search_miss_is_indeterminate_not_absence(self, ua_env, caplog):
        t = FakeTransport(FakeResponse(200, ORDER_STATUS_XML, "application/xml"))
        with caplog.at_level("WARNING"):
            client(transport=t).get_order_status("Purchase Order", "PO99999")
        assert "INDETERMINATE, not absent" in caplog.text

    def test_order_exists_uses_the_route_that_really_filters(self, ua_env):
        header_xml = (
            '<?xml version="1.0"?><ns3:orders xmlns:ns3="urn:x"><order>'
            "<number>TEST-GOTHAM-001</number><type>Sales Order</type>"
            "<orderLines><orderLine/><orderLine/><orderLine/><orderLine/></orderLines>"
            "</order></ns3:orders>"
        )
        t = FakeTransport(FakeResponse(200, header_xml, "application/xml"))
        c = client(transport=t)
        assert c.order_exists("Sales Order", "TEST-GOTHAM-001") is True
        assert t.urls[0].endswith("/search/Order")
        assert t.calls[0]["params"] == {"type": "Sales Order", "number": "TEST-GOTHAM-001"}

    def test_order_headers_parse_number_and_line_count(self):
        header_xml = (
            '<?xml version="1.0"?><ns3:orders xmlns:ns3="urn:x"><order>'
            "<number>TEST-GOTHAM-001</number>"
            "<orderLines><orderLine/><orderLine/><orderLine/><orderLine/></orderLines>"
            "</order></ns3:orders>"
        )
        parsed = dc.parse_order_headers(dc.DeposcoResponse("ua", "/x", 200, header_xml))
        assert parsed == [("TEST-GOTHAM-001", 4)]

    def test_non_xml_payload_yields_nothing(self):
        assert dc.parse_order_status({"orderStatus": "Received"}) == []

    def test_empty_orders_envelope_yields_nothing(self):
        empty = '<?xml version="1.0"?><ns2:orders xmlns:ns2="urn:x"/>'
        assert dc.parse_order_status(dc.DeposcoResponse("ua", "/x", 200, empty)) == []


# ── Envelope + misc ──────────────────────────────────────────────────────────


class TestResponseEnvelope:
    def test_body_beats_a_wrong_content_type_header(self):
        """LIVE FINDING 2026-08-14: the Enterprise Inventory endpoint answers
        `content-type: application/xml` with a JSON body. Trusting the header
        sent that payload into the XML parser and killed the inventory read with
        "invalid token: line 1, column 0". The body is the ground truth."""
        lying = dc.DeposcoResponse(
            "prod", "/enterpriseinventory/F3E/availability", 200,
            '{"enterpriseInventory":[{"itemNumber":"PURE-Original","totalOnHandQty":0}]}',
            "application/xml",
        )
        assert lying.looks_like_xml is False
        assert dc.parse_enterprise_availability(lying)[0].item_number == "PURE-Original"

    def test_xml_body_is_detected_despite_a_plain_text_header(self):
        assert dc.DeposcoResponse("ua", "/x", 200, "<a/>", "text/plain").looks_like_xml

    def test_header_decides_only_when_there_is_no_body_to_sniff(self):
        assert dc.DeposcoResponse("ua", "/x", 200, "   ", "application/xml").looks_like_xml
        assert not dc.DeposcoResponse("ua", "/x", 200, "   ", "application/json").looks_like_xml

    def test_json_body_and_json_header_agree(self):
        assert not dc.DeposcoResponse("ua", "/x", 200, "{}", "application/json").looks_like_xml

    def test_bad_json_raises_a_scrubbed_error(self):
        with pytest.raises(dc.DeposcoError, match="not JSON"):
            dc.DeposcoResponse("ua", "/x", 200, "not json").json()

    def test_bad_xml_raises(self):
        with pytest.raises(dc.DeposcoError, match="not well-formed XML"):
            dc.DeposcoResponse("ua", "/x", 200, "<a>").xml()


class TestClientDefaults:
    def test_business_unit_and_tenant_come_from_env(self, ua_env):
        c = client()
        assert c.tenant == "ESM"
        assert c.business_unit == "F3E"
        assert c.base_url.endswith("/integration/ESM")

    def test_availability_requires_a_measure(self, ua_env):
        with pytest.raises(dc.DeposcoUsageError):
            client().get_enterprise_availability(measures=[])

    def test_availability_sends_all_documented_measures(self, ua_env):
        t = FakeTransport(FakeResponse(200, '{"enterpriseInventory": []}'))
        client(transport=t).get_enterprise_availability(item_numbers=["PURE-Original"])
        sent = t.calls[0]["params"]["measures"].split(",")
        assert set(sent) == set(dc.ALL_MEASURES)
        assert t.calls[0]["params"]["itemNumbers"] == "PURE-Original"


class TestPaginationRobustness:
    """Lens 2, hardened after a live check of tenant ESM's paging semantics.

    ESM honours pageSize and pageNumber correctly today (5/5/4 across three pages
    of 14 items). These pin the two ways a gateway could break that without the
    result ever looking broken.
    """

    @staticmethod
    def _page(*item_numbers):
        items = ",".join('{"itemNumber": "%s", "atpQty": 1}' % n for n in item_numbers)
        return FakeResponse(200, '{"enterpriseInventory": [%s]}' % items)

    def test_a_short_page_is_not_assumed_to_be_the_last(self, ua_env):
        """If a gateway capped pageSize below what we asked, stopping on the
        first short page would report a partial warehouse as complete."""
        t = FakeTransport(
            self._page("A", "B"),          # short, but NOT the end
            self._page("C", "D"),
            self._page(),                  # the real end
        )
        result = client(transport=t).get_enterprise_availability(page_size=10)
        assert [r.item_number for r in result.rows] == ["A", "B", "C", "D"]
        assert result.truncated is False

    def test_pagination_that_does_not_advance_is_reported_partial(self, ua_env):
        """A gateway ignoring pageNumber would serve page 1 forever."""
        t = FakeTransport(*[self._page("A", "B")] * 6)
        result = client(transport=t).get_enterprise_availability(page_size=2)
        assert [r.item_number for r in result.rows] == ["A", "B"]
        assert result.truncated is True, "a stuck pager must not read as complete"
        assert len(t.calls) < 6, "and it must stop rather than loop to the cap"

    def test_duplicate_items_across_pages_are_not_double_counted(self, ua_env):
        t = FakeTransport(self._page("A", "B"), self._page("B", "C"), self._page())
        result = client(transport=t).get_enterprise_availability(page_size=2)
        assert [r.item_number for r in result.rows] == ["A", "B", "C"]

    def test_page_cap_still_reports_truncation(self, ua_env):
        pages = [self._page(f"SKU{i}") for i in range(10)]
        result = client(transport=FakeTransport(*pages)).get_enterprise_availability(
            page_size=1, max_pages=3
        )
        assert result.truncated is True
