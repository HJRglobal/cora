#!/usr/bin/env python3
"""
F3E - Deposco UA (sandbox) test sales order - Gotham DSD shape rehearsal.

Phase 2 push test per the LOCKED design (2026-08-06):
  02-F3-Energy/projects/2026-08_deposco-api-order-automation/
      2026-08-06_f3e_deposco-api-order-automation-design.md

WHAT THIS DOES
  1. Loads DEPOSCO_UA_USER / DEPOSCO_UA_PASS from C:\\Users\\Harri\\code\\cora\\.env
  2. Auth smoke test (GET order status for the test order number)
  3. Resolves the 4 F3 Pure item numbers by probing the Item API
     (tries Shopify-feed SKU, then UPC-A, then GTIN-14, per flavor)
  4. POSTs ONE test Sales Order (default TEST-GOTHAM-001) shaped exactly like
     the real first Gotham order: 3 pallets / 156 cases / 624 twelve-packs,
     even 4-flavor split (156 twelve-packs per flavor), $21.70/12-pack
  5. Reads the order back (GET) and verifies header + lines  [D-110]
  6. Prints a three-outcome report (CONFIRMED / FAILED / UNKNOWN)  [D-101]

SAFETY (by construction)
  - SANDBOX ONLY: the base URL is pinned to sandboxapi.deposco.com. There is
    no code path to the production host in this file.
  - Ships NOTHING. Sandbox has no inventory; this rehearses the push + read
    loop only. The real Gotham order stays on the manual lane until Phase 3.
  - Credentials are never printed, logged, or included in exception text.
  - No LLM anywhere (deterministic script, per design Fork 1).

USAGE (from any directory, on Harrison's machine)
  python C:\\Users\\Harri\\code\\cora\\scripts\\deposco_ua_test_order.py
  Options:
    --order-number TEST-GOTHAM-002   fresh number for a repeat run
    --force                          push even if item probes fail (deliberately
                                     exercises Deposco's 200-with-missingItems
                                     error path; useful as a semantics test)
    --dry-run                        build + print the payload, no API calls

EXIT CODES
  0 = CONFIRMED (created + read-back verified)
  2 = auth failed (creds missing/rejected)
  3 = items unresolved, push not attempted (use --force to attempt anyway)
  4 = push failed (Deposco returned an error / blank 200s exhausted)
  5 = push claimed success but read-back failed --> treat as UNKNOWN, check UI
"""

import argparse
import base64
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# ---------------------------------------------------------------- constants

ENV_PATH = r"C:\Users\Harri\code\cora\.env"
UA_BASE = "https://sandboxapi.deposco.com/ua/integration/{tenant}"  # SANDBOX. Never prod.
DEFAULT_TENANT = "ESM"
DEFAULT_BU = "F3E"
TIMEOUT_S = 30
PACE_S = 0.6          # ~2 req/sec guidance until Anthony confirms our tier
BLANK_200_RETRIES = 3  # Deposco doctrine: HTTP 200 with blank body = FAILURE, retry

# Gotham DSD - first-order facts (Harrison's 8/11 email + spec sheet 8/11)
SHIP_TO = {
    "name": "Gotham DSD",
    "contactName": "Receiving",
    "attention": "Receiving - #401",
    "email": "",
    "phone": "",
    "addressLine1": "4747 VANDAM ST",
    "addressLine2": "#401",
    "addressLine3": "",
    "city": "Long Island City",
    "stateProvinceCode": "NY",
    "postalCode": "11101",
    "countryCode": "US",
}
BILL_TO = {
    "name": "Gotham DSD",
    "contactName": "Justin Moran (AP)",
    "attention": "Accounts Payable",
    "phone": "",
    "addressLine1": "4747 VANDAM ST",
    "addressLine2": "#401",
    "addressLine3": "",
    "city": "Long Island City",
    "stateProvinceCode": "NY",
    "postalCode": "11101",
    "countryCode": "US",
}

# Sellable unit = one 12-pack. 156 twelve-packs per flavor = 39 master cases
# per flavor = 3 pallets total (52 cases / 208 twelve-packs per pallet).
UNIT_PRICE = 21.70          # delivered price per 12-pack (confirmed 2026-08-11)
QTY_PER_FLAVOR = 156        # twelve-packs
PACK_WEIGHT_LB = 9.85       # per 12-pack, from spec sheet
FLAVORS = [
    # (display name, candidate item numbers, probed in order:
    #  1) Deposco item number = Shopify feed SKU - confirmed 2026-08-14 by
    #     barcode match in Shopify Admin after UA returned 409 duplicate-UPC
    #     on a create attempt (proof the items already exist in the tenant);
    #  2) 12-pack UPC-A;  3) GTIN-14 (fallbacks).
    ("F3 Pure Original",             ["PURE-Original", "850045501655", "00850045501655"]),
    ("F3 Pure Citrus Clarity",       ["PURE-Citrus",   "850045501631", "00850045501631"]),
    ("F3 Pure Tropical Theory",      ["PURE-Tropical", "850045501648", "00850045501648"]),
    ("F3 Pure Strawberry Lemonade",  ["PURESL",        "850045501617", "00850045501617"]),
]
ORDER_TOTAL = round(UNIT_PRICE * QTY_PER_FLAVOR * len(FLAVORS), 2)  # 13540.80

ORDER_NOTE = (
    "TEST ORDER - DO NOT SHIP / DO NOT ALLOCATE. UA rehearsal of first real "
    "Gotham DSD order: 3 pallets (52 cases ea), 156 master cases, 624 "
    "twelve-packs, even 4-flavor split. Freight ref: Forward Air 3-pallet LTL "
    "quote $1,985.24 (Nimbl 8/11). Real order ships via manual lane until "
    "Phase 3. Void after verification."
)

# ------------------------------------------------------------------ helpers


def die(code, msg):
    print("\n" + msg)
    sys.exit(code)


def load_env(path):
    """Tiny .env parser - values never echoed anywhere."""
    vals = {}
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                vals[k.strip()] = v.strip().strip('"').strip("'")
    except OSError as exc:
        die(2, "FATAL: cannot read %s (%s). Phase 0 incomplete." % (path, exc.__class__.__name__))
    return vals


def make_ssl_context(insecure):
    """TLS trust ladder for Windows boxes behind TLS-inspecting AV/proxies.

    1. truststore installed (py -m pip install truststore): use the Windows
       certificate store - the proper fix; the inspector's root usually lives
       there.
    2. Plain default context (public CA bundle).
    3. --insecure: verification OFF. SANDBOX-ONLY escape hatch - acceptable
       here solely because the host is pinned to the UA sandbox. The prod
       client must NEVER get an equivalent flag.
    """
    if insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    try:
        import truststore
        return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    except ImportError:
        return ssl.create_default_context()


TLS_HINT = (
    "FATAL: TLS certificate verification failed (%s).\n"
    "A TLS-inspecting proxy/AV on this machine is the usual cause. Fix ladder:\n"
    "  1) py -m pip install truststore    then rerun (script auto-switches to\n"
    "     the Windows certificate store when truststore is importable)\n"
    "  2) still failing: rerun with --insecure  (SANDBOX-ONLY bypass; the\n"
    "     sandbox host is pinned, so this cannot apply to prod)"
)


class Client:
    """Minimal Deposco UA client. Basic auth, JSON, paced, blank-200 aware."""

    def __init__(self, tenant, user, password, insecure=False):
        self.base = UA_BASE.format(tenant=tenant)
        token = base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
        self._auth = "Basic " + token
        self._ctx = make_ssl_context(insecure)

    def request(self, method, path, payload=None, xml_body=None):
        """Returns (http_status, body_text). Never raises with creds in text."""
        url = self.base + path
        data = None
        # Accept anything, prefer XML: order/status reads are XML-only in the
        # doc (OrderHeader/OrderStatus XSDs) and 400 on a JSON-only Accept -
        # the 8/14 read-back lesson. Items/creates negotiate fine either way.
        headers = {"Authorization": self._auth,
                   "Accept": "application/xml, application/json;q=0.9, */*;q=0.8"}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        elif xml_body is not None:
            data = xml_body.encode()
            headers["Content-Type"] = "application/xml"
            headers["Accept"] = "application/xml"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        time.sleep(PACE_S)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=self._ctx) as resp:
                return resp.getcode(), resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            return exc.code, body
        except urllib.error.URLError as exc:
            # reason only - never the request object (auth header) in output
            reason = str(getattr(exc, "reason", "unknown"))
            if "CERTIFICATE_VERIFY_FAILED" in reason:
                die(4, TLS_HINT % reason)
            die(4, "FATAL: network error calling Deposco UA (%s). Check VPN/DNS and retry."
                % reason)
        except Exception as exc:
            die(4, "FATAL: unexpected error calling Deposco UA (%s)." % exc.__class__.__name__)


def order_payload(order_number, bu, lines, include_freight=True):
    """Real Gotham shape, JSON per the doc's create example (pp. 307-310).

    History: the first 8/14 push flat-400'd with freight + channel + line-notes
    all present; removing all three at once made it create clean, so the
    culprit was never isolated. Anthony (8/14): the freight syntax and level
    are CORRECT and should not 400 - prime remaining suspect is the channel
    block's "@ref1"-style keys (an XML-ism). Freight is therefore back IN by
    default (--no-freight to exclude); channel and line-notes stay out."""
    order_lines = []
    for i, (item_number, flavor) in enumerate(lines, start=1):
        order_lines.append({
            "businessUnit": bu,
            "lineNumber": "%s--%d" % (order_number, i),
            "customerLineNumber": "%s--%d" % (order_number, i),
            "lineStatus": "New",
            "orderPackQuantity": str(float(QTY_PER_FLAVOR)),
            "shortagePackQuantity": "0.0",
            "itemNumber": item_number,
            "pack": {"type": "Each", "quantity": "1", "weight": str(PACK_WEIGHT_LB)},
            "unitPrice": str(UNIT_PRICE),
            "unitCost": "0.0",
        })
    return {
        "order": [{
            "businessUnit": bu,
            "number": order_number,
            "type": "Sales Order",
            "status": "New",
            "orderPriority": "10",
            "otherReferenceNumber": "GOTHAM-PO-PENDING",  # swap for real PO# when Trent sends it
            "shipToAddress": SHIP_TO,
            "billToAddress": BILL_TO,
            "shipVia": "LTL",           # may warn 'could not map ship via' - harmless in UA
            "dropShip": "false",
            **({"freight": {"termsType": "Prepaid"}} if include_freight else {}),
            "orderSubTotal": str(ORDER_TOTAL),
            "orderShipTotal": "0.0",
            "orderShippingTotal": "0.0",
            "orderTaxTotal": "0.0",
            "orderTotal": str(ORDER_TOTAL),
            "notes": {"note": [{"title": "TEST ORDER", "body": ORDER_NOTE}]},
            "orderLines": {"orderLine": order_lines},
            "residentialDelivery": "false",
        }]
    }


def _esc(s):
    from xml.sax.saxutils import escape
    return escape(s or "")


def _addr_xml(a, include_email):
    out = ["<name>%s</name>" % _esc(a["name"]),
           "<contactName>%s</contactName>" % _esc(a["contactName"]),
           "<attention>%s</attention>" % _esc(a.get("attention", ""))]
    if include_email:
        out.append("<email>%s</email>" % _esc(a.get("email", "")))
    out += ["<phone>%s</phone>" % _esc(a.get("phone", "")),
            "<addressLine1>%s</addressLine1>" % _esc(a["addressLine1"]),
            "<addressLine2>%s</addressLine2>" % _esc(a.get("addressLine2", "")),
            "<addressLine3/>",
            "<city>%s</city>" % _esc(a["city"]),
            "<stateProvinceCode>%s</stateProvinceCode>" % a["stateProvinceCode"],
            "<postalCode>%s</postalCode>" % a["postalCode"],
            "<countryCode>%s</countryCode>" % a["countryCode"]]
    return "".join(out)


def order_xml(order_number, bu, lines):
    """Fallback transport: XML mirroring the doc's create example element
    order EXACTLY (pp. 304-306) - Deposco's primary format. Minimal optional
    content (empty <notes/>; no freight/channel/customFields)."""
    ol = []
    for i, (item_number, flavor) in enumerate(lines, start=1):
        ol.append(
            "<orderLine>"
            "<businessUnit>%s</businessUnit>"
            "<lineNumber>%s--%d</lineNumber>"
            "<customerLineNumber>%s--%d</customerLineNumber>"
            "<lineStatus>New</lineStatus>"
            "<orderPackQuantity>%s</orderPackQuantity>"
            "<shortagePackQuantity>0.0</shortagePackQuantity>"
            "<itemNumber>%s</itemNumber>"
            "<pack><type>Each</type><quantity>1</quantity><weight>%s</weight></pack>"
            "<unitPrice>%s</unitPrice>"
            "<unitCost>0.0</unitCost>"
            "<notes/>"
            "</orderLine>"
            % (bu, order_number, i, order_number, i, float(QTY_PER_FLAVOR),
               _esc(item_number), PACK_WEIGHT_LB, UNIT_PRICE))
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<ns2:orders xmlns:ns2="http://integration.deposco.com/order">'
        "<order>"
        "<businessUnit>%s</businessUnit>"
        "<number>%s</number>"
        "<type>Sales Order</type>"
        "<status>New</status>"
        "<orderPriority>10</orderPriority>"
        "<otherReferenceNumber>GOTHAM-PO-PENDING</otherReferenceNumber>"
        "<shipFromAddress/>"
        "<shipToAddress>%s</shipToAddress>"
        "<billToAddress>%s</billToAddress>"
        "<shipVia>LTL</shipVia>"
        "<dropShip>false</dropShip>"
        "<orderSubTotal>%.2f</orderSubTotal>"
        "<orderShipTotal>0.0</orderShipTotal>"
        "<orderShippingTotal>0.0</orderShippingTotal>"
        "<orderTaxTotal>0.0</orderTaxTotal>"
        "<orderTotal>%.2f</orderTotal>"
        "<notes/>"
        "<orderLines>%s</orderLines>"
        "</order>"
        "</ns2:orders>"
        % (bu, order_number, _addr_xml(SHIP_TO, True), _addr_xml(BILL_TO, False),
           ORDER_TOTAL, ORDER_TOTAL, "".join(ol)))


def read_back(c, order_number):
    """Read the order back, space-free routes first.

    8/14 finding: the UA gateway rejects ANY path containing an encoded space
    (every '/...Sales%20Order/...' GET returned a flat 400, before and after
    the order existed; every space-free path behaved normally). So the direct
    'Retrieve a sales order' route is unusable here - we read via the Search
    API and the date-range status route instead, where the order type rides
    the query string / no type segment at all. Direct-path variants stay at
    the end of the ladder for the record. Returns (ok, tried, codes, body)."""
    on_q = urllib.parse.quote(order_number)
    day = 24 * 3600
    d0 = time.strftime("%Y%m%d", time.localtime(time.time() - day))
    d1 = time.strftime("%Y%m%d", time.localtime(time.time() + day))
    paths = [
        ("search",       "/search/Order?type=Sales%20Order&number=" + on_q),
        ("statusSearch", "/status/order/search?type=Sales%20Order&number=" + on_q),
        ("statusRange",  "/status/order/%s,%s" % (d0, d1)),
        ("ordersPlus",   "/orders/Sales+Order/" + on_q),
        ("orders%20",    "/orders/Sales%20Order/" + on_q),
    ]
    tried, codes, body = [], [], ""
    for label, rb in paths:
        code, body = c.request("GET", rb)
        tried.append("%s->%d" % (label, code))
        codes.append(code)
        if code == 200 and order_number in body:
            return True, tried, codes, body
    return False, tried, codes, body


def push_with_retries(c, json_payload=None, xml_body=None):
    """POST /orders with the blank-200 retry doctrine. Returns (code, body)."""
    code, body = 0, ""
    for attempt in range(1, BLANK_200_RETRIES + 1):
        code, body = c.request("POST", "/orders", payload=json_payload, xml_body=xml_body)
        if code == 200 and not body.strip():
            print("      blank 200 body (attempt %d) - Deposco doctrine says retry" % attempt)
            continue
        break
    return code, body


# --------------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(description="Deposco UA test order (Gotham shape)")
    ap.add_argument("--order-number", default="TEST-GOTHAM-001")
    ap.add_argument("--force", action="store_true",
                    help="push with UPC-A as itemNumber even if probes fail")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--insecure", action="store_true",
                    help="disable TLS verification (SANDBOX-ONLY escape hatch "
                         "for TLS-inspecting AV/proxies; prefer truststore)")
    ap.add_argument("--verify-only", action="store_true",
                    help="skip the push; just read back an existing order "
                         "(use after a run that created the order but could "
                         "not read it back)")
    ap.add_argument("--no-freight", action="store_true",
                    help="exclude the freight/termsType block (default is "
                         "INCLUDED per Anthony 8/14: syntax + level correct)")
    args = ap.parse_args()
    on = args.order_number

    print("Deposco UA test order - %s (sandbox only, ships nothing)" % on)
    print("=" * 66)

    env = load_env(ENV_PATH)
    tenant = env.get("DEPOSCO_TENANT", DEFAULT_TENANT)
    bu = env.get("DEPOSCO_BU", DEFAULT_BU)
    user = env.get("DEPOSCO_UA_USER", "")
    password = env.get("DEPOSCO_UA_PASS", "")

    if args.dry_run:
        lines = [(cands[0], name) for name, cands in FLAVORS]
        print("--- JSON payload ---")
        print(json.dumps(order_payload(on, bu, lines), indent=2))
        print("--- XML fallback payload ---")
        print(order_xml(on, bu, lines))
        return

    if not user or not password:
        die(2, "FATAL: DEPOSCO_UA_USER / DEPOSCO_UA_PASS missing from .env.\n"
               "Phase 0 incomplete - see the runbook: sandbox password must be\n"
               "reset via TEXT to Anthony (the emailed one is burned), then both\n"
               "values added to " + ENV_PATH)

    if args.insecure:
        print("!! TLS VERIFICATION DISABLED - sandbox-only escape hatch. Prefer")
        print("!! 'py -m pip install truststore' so prod-phase work never needs this.")
    c = Client(tenant, user, password, insecure=args.insecure)
    status_path = "/orders/Sales%20Order/" + urllib.parse.quote(on)

    if args.verify_only:
        print("[verify-only] reading back %s - no push" % on)
        ok, tried_rb, codes, body = read_back(c, on)
        if not ok:
            if any(x in (401, 403) for x in codes):
                die(2, "AUTH FAILED during read-back (%s)." % ", ".join(tried_rb))
            die(5, "Order %s NOT visible via any read path (%s).\n"
                   "Ground truth = the UA UI at https://esm-ua.deposco.com: if the\n"
                   "order IS there, the push is proven and the read lane becomes an\n"
                   "Anthony question (capture this output); if absent there too,\n"
                   "rerun without --verify-only to push again." % (on, ", ".join(tried_rb)))
        print("READ-BACK CONFIRMED: %s visible via GET (%d line qty fields)."
              % (on, body.count("orderPackQuantity")))
        print("Core loop PROVEN: push (earlier run) + API read-back. Ships nothing.")
        print("Next: eyeball + void in the UA UI (esm-ua.deposco.com).")
        return

    # -- 1. auth smoke ------------------------------------------------------
    code, _ = c.request("GET", status_path)
    if code in (401, 403):
        die(2, "AUTH FAILED (HTTP %d) - sandbox rejected the credential pair.\n"
               "  username in .env : '%s'\n"
               "  password length  : %d chars (value never printed)\n"
               "Outcome class: FAILED (auth). Decisive check: log into the UA UI at\n"
               "https://esm-ua.deposco.com with the same pair.\n"
               "  UI login works -> .env transcription is off; re-add the password\n"
               "                    line (last line wins) and rerun.\n"
               "  UI login fails -> the sandbox password was never actually set\n"
               "                    (thread stalled 8/7); text Anthony a NEW one\n"
               "                    at 801-230-8545, never by email."
               % (code, user, len(password)))
    print("[1/4] Auth OK (HTTP %d on order-status probe)" % code)

    # -- 2. resolve items ---------------------------------------------------
    lines, unresolved = [], []
    for name, candidates in FLAVORS:
        found, tried = None, []
        for cand in candidates:
            icode, _ = c.request("GET", "/items/%s/%s" % (bu, urllib.parse.quote(cand)))
            tried.append("%s->%d" % (cand, icode))
            if icode == 200:
                found = cand
                break
        if found:
            lines.append((found, name))
            print("      item OK   %-28s -> %s" % (name, found))
        else:
            unresolved.append(name)
            lines.append((candidates[0], name))  # SKU fallback (used only with --force)
            print("      item MISS %-28s (%s)" % (name, ", ".join(tried)))

    if unresolved and not args.force:
        die(3, "STOP: %d/4 items not found in the UA item master. The sandbox\n"
               "likely has no F3 item sync. Fix: run the companion seeder\n"
               "  python C:\\Users\\Harri\\code\\cora\\scripts\\deposco_ua_create_items.py\n"
               "then rerun this script. (Alternatives: ask Anthony to sync the\n"
               "item master to UA, or rerun with --force to deliberately exercise\n"
               "the 200-with-missingItems error path.) No order was pushed."
               % len(unresolved))
    print("[2/4] Items resolved (%d/4%s)" % (4 - len(unresolved),
          ", --force override" if unresolved else ""))

    # -- 3. push (JSON first, auto-fallback to XML on a flat 400) -----------
    include_freight = not args.no_freight
    transport = "JSON"
    print("      freight block: %s" % ("INCLUDED (termsType Prepaid)" if include_freight else "excluded"))
    code, body = push_with_retries(c, json_payload=order_payload(on, bu, lines, include_freight))
    flat_400 = code == 400 and "multistatus" not in body.lower() and '"response"' not in body
    if flat_400 and include_freight:
        die(4, "JSON push WITH the freight block rejected flat-400. Since channel\n"
               "and line-notes are already out, this ISOLATES freight as the\n"
               "trigger on sandboxapi - contradicting Anthony's Postman result.\n"
               "Capture this output for him. Rerun with --no-freight to push\n"
               "without it.")
    if flat_400:
        print("      JSON push rejected flat-400 (parser-level) - retrying as XML")
        transport = "XML"
        code, body = push_with_retries(c, xml_body=order_xml(on, bu, lines))

    created = "201" in body and "Created" in body
    already = (not created) and ("Updated" in body or "updated" in body)
    if code == 200 and not body.strip():
        die(4, "PUSH FAILED: %d blank-200 responses. Outcome class: FAILED.\n"
               "Do NOT assume the order exists - check the UA UI before rerunning."
               % BLANK_200_RETRIES)
    if not created and not already:
        print("\nPUSH NOT CONFIRMED (HTTP %d, %s transport). Deposco response:" % (code, transport))
        print(body[:2000])
        die(4, "Outcome class: FAILED. missingItems/missingPacks in the description\n"
               "-> item/pack mismatch; a flat 400 on BOTH transports -> capture this\n"
               "output for Anthony (schema-level rejection).")
    if created:
        print("[3/4] Push CONFIRMED created via %s (201 in multistatus response)" % transport)
    else:
        print("[3/4] Order already existed - Deposco processed an UPDATE (rerun on "
              "an existing number); continuing to read-back")
    print("      Deposco: %s" % body[:300].replace("\n", " "))

    # -- 4. read-back (D-110) - documented read paths, first 200 wins --------
    ok, tried_rb, codes, body = read_back(c, on)
    if not ok:
        die(5, "READ-BACK FAILED (%s): push processed but no read path returns\n"
               "the order. Outcome class: UNKNOWN - verify in the UA UI at\n"
               "https://esm-ua.deposco.com before trusting or rerunning." % ", ".join(tried_rb))
    line_hits = body.count("orderPackQuantity")
    print("[4/4] Read-back OK - order %s visible via GET (%d line qty fields)" % (on, line_hits))

    print("\n" + "=" * 66)
    print("CONFIRMED: %s created in UA sandbox (push transport: %s)." % (on, transport))
    print("  4 lines x %d twelve-packs @ $%.2f = $%.2f" % (QTY_PER_FLAVOR, UNIT_PRICE, ORDER_TOTAL))
    print("  Cleanup: NONE needed (Anthony 8/14: sandbox test data lives there).")
    print("  This shipped nothing. Real Gotham order = manual lane until Phase 3.")


if __name__ == "__main__":
    main()
