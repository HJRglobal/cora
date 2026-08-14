#!/usr/bin/env python3
"""
F3E - Deposco UA (sandbox) item seeder - creates the 4 F3 Pure items.

Companion to deposco_ua_test_order.py. Run this ONLY if the test-order script
exits 3 (items not found in the UA item master - expected on a fresh sandbox,
which has no data sync). It creates the four F3 Pure 12-pack items in the UA
sandbox so the order push can resolve them, then verifies each with a GET.

SANDBOX ONLY - base URL pinned to sandboxapi.deposco.com; no prod code path.
Item numbers = 12-pack UPC-A (850045501xxx), matching what the order script
probes. Real prod item numbers may differ; prod resolution happens in Phase 3
from live order data - this seeding is a UA rehearsal convenience only.

USAGE
  python C:\\Users\\Harri\\code\\cora\\scripts\\deposco_ua_create_items.py
  Options: --dry-run (print payload, no API calls)

EXIT CODES
  0 = all 4 items exist in UA (created now or already there), GET-verified
  2 = auth failed / creds missing
  4 = one or more items failed to create/verify
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

ENV_PATH = r"C:\Users\Harri\code\cora\.env"
UA_BASE = "https://sandboxapi.deposco.com/ua/integration/{tenant}"  # SANDBOX. Never prod.
DEFAULT_TENANT = "ESM"
DEFAULT_BU = "F3E"
TIMEOUT_S = 30
PACE_S = 0.6
BLANK_200_RETRIES = 3

# (sku, upc_a, gtin14, name). SKU = Deposco item number (Shopify feed SKUs,
# barcode-confirmed 2026-08-14 - the UA tenant already carries these items;
# the 8/14 create attempt 409'd on the duplicate-UPC constraint, proving it).
# Physicals from the Gotham spec sheet: 12-pack 9.5 x 7 x 6.5 in, 9.85 lb,
# lot + expiration tracked.
ITEMS = [
    ("PURE-Original", "850045501655", "00850045501655", "F3 Pure Original 12-pack"),
    ("PURE-Citrus",   "850045501631", "00850045501631", "F3 Pure Citrus Clarity 12-pack"),
    ("PURE-Tropical", "850045501648", "00850045501648", "F3 Pure Tropical Theory 12-pack"),
    ("PURESL",        "850045501617", "00850045501617", "F3 Pure Strawberry Lemonade 12-pack"),
]
UNIT_PRICE = "21.70"
PACK_WEIGHT = "9.85"


def die(code, msg):
    print("\n" + msg)
    sys.exit(code)


def load_env(path):
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
        die(2, "FATAL: cannot read %s (%s)." % (path, exc.__class__.__name__))
    return vals


def make_ssl_context(insecure):
    """Same TLS trust ladder as deposco_ua_test_order.py: truststore (Windows
    cert store) if installed -> default CA bundle -> --insecure sandbox-only
    bypass. The prod client must NEVER get an equivalent flag."""
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
    "Fix ladder: 1) py -m pip install truststore  then rerun;\n"
    "2) still failing: rerun with --insecure (SANDBOX-ONLY bypass)."
)


class Client:
    def __init__(self, tenant, user, password, insecure=False):
        self.base = UA_BASE.format(tenant=tenant)
        token = base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
        self._auth = "Basic " + token
        self._ctx = make_ssl_context(insecure)

    def request(self, method, path, payload=None):
        url = self.base + path
        data = None
        headers = {"Authorization": self._auth, "Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
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
            reason = str(getattr(exc, "reason", "unknown"))
            if "CERTIFICATE_VERIFY_FAILED" in reason:
                die(4, TLS_HINT % reason)
            die(4, "FATAL: network error calling Deposco UA (%s)." % reason)
        except Exception as exc:
            die(4, "FATAL: unexpected error (%s)." % exc.__class__.__name__)


def item_payload(bu, number, upc_a, gtin14, name):
    """Item create JSON per API doc pp. 138-141, trimmed to what we need."""
    return {
        "item": [{
            "businessUnit": bu,
            "number": number,
            "name": name,
            "shortDescription": name,
            "longDescription": name + " (12 x 12 fl oz cans)",
            "cycleCount": "false",
            "purchaseCost": "0.0",
            "unitPrice": UNIT_PRICE,
            "bornOnDateRequired": "false",
            "expirationDateRequired": "true",
            "receiveDateRequired": "false",
            "quarantineRequired": "false",
            "inspectionRequired": "false",
            "hazmat": "false",
            "hazmatCode": "",
            "inventoryTrackingEnabled": "true",
            "lotTrackingEnabled": "true",
            "serialTrackingEnabled": "false",
            "shippable": "true",
            "cycleCountFrequency": "0",
            "packs": {
                "pack": {
                    "type": "Each",
                    "quantity": "1",
                    "weight": PACK_WEIGHT,
                    "dimension": {
                        "length": "9.5", "width": "7.0", "height": "6.5",
                        "units": "Inch",
                    },
                }
            },
            "upcs": [{"upc": [upc_a, gtin14], "source": "api"}],
            "productCategory": "Miscellaneous",
            "salesEnabled": "true",
        }]
    }


def main():
    ap = argparse.ArgumentParser(description="Seed the 4 F3 Pure items into Deposco UA")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--insecure", action="store_true",
                    help="disable TLS verification (SANDBOX-ONLY escape hatch; "
                         "prefer truststore)")
    args = ap.parse_args()

    print("Deposco UA item seeder - 4 F3 Pure 12-packs (sandbox only)")
    print("=" * 66)

    env = load_env(ENV_PATH)
    tenant = env.get("DEPOSCO_TENANT", DEFAULT_TENANT)
    bu = env.get("DEPOSCO_BU", DEFAULT_BU)
    user = env.get("DEPOSCO_UA_USER", "")
    password = env.get("DEPOSCO_UA_PASS", "")

    if args.dry_run:
        sku, upc, g14, name = ITEMS[0]
        print(json.dumps(item_payload(bu, sku, upc, g14, name), indent=2))
        return

    if not user or not password:
        die(2, "FATAL: DEPOSCO_UA_USER / DEPOSCO_UA_PASS missing from " + ENV_PATH)

    if args.insecure:
        print("!! TLS VERIFICATION DISABLED - sandbox-only escape hatch.")
    c = Client(tenant, user, password, insecure=args.insecure)

    # auth smoke
    code, _ = c.request("GET", "/items/%s/%s" % (bu, ITEMS[0][0]))
    if code in (401, 403):
        die(2, "AUTH FAILED (HTTP %d) using username '%s' (password length %d).\n"
               "Cross-check the pair in the UA UI: https://esm-ua.deposco.com\n"
               "(see deposco_ua_test_order.py's auth triage for the fix ladder)."
               % (code, user, len(password)))
    print("[auth] OK (HTTP %d)" % code)

    failures = []
    for sku, upc, g14, name in ITEMS:
        # existence check across all known identifiers - skip if ANY resolves
        existing = None
        for cand in (sku, upc, g14):
            code, _ = c.request("GET", "/items/%s/%s" % (bu, urllib.parse.quote(cand)))
            if code == 200:
                existing = cand
                break
        if existing:
            print("[skip] %-34s already exists as '%s'" % (name, existing))
            continue

        body = ""
        for attempt in range(1, BLANK_200_RETRIES + 1):
            code, body = c.request("POST", "/items", item_payload(bu, sku, upc, g14, name))
            if code == 200 and not body.strip():
                print("       blank 200 (attempt %d) - retrying per Deposco doctrine" % attempt)
                continue
            break

        created = "201" in body and "Created" in body
        if not created:
            print("[FAIL] %-34s HTTP %d. Response:" % (name, code))
            print(body[:1200])
            failures.append(name)
            continue

        # D-110 read-back
        code, _ = c.request("GET", "/items/%s/%s" % (bu, urllib.parse.quote(sku)))
        if code == 200:
            print("[ OK ] %-34s created + GET-verified (%s)" % (name, sku))
        else:
            print("[????] %-34s POST said 201 but GET returned %d - check UA UI"
                  % (name, code))
            failures.append(name)

    print("\n" + "=" * 66)
    if failures:
        die(4, "FAILED/UNKNOWN for %d item(s): %s\nFix or ask Anthony to sync the "
               "item master to UA, then rerun." % (len(failures), ", ".join(failures)))
    print("CONFIRMED: all 4 F3 Pure items exist in UA.")
    print("Next: rerun deposco_ua_test_order.py")


if __name__ == "__main__":
    main()
