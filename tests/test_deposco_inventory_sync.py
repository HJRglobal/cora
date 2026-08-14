"""Tests for the Deposco warehouse inventory sync (Phase 1, S2).

The coverage floor is the whole point of this script (D-133 class): a warehouse
feed that silently returns nothing must never be written as a fresh, green,
zero-stock file. These tests assert that from both directions -- the payload it
builds, and the file it declines to write.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from cora.connectors import deposco_client as dc  # noqa: E402

import run_deposco_inventory_sync as sync  # noqa: E402

KNOWN = ["PURE-Original", "PURE-Citrus", "PURESL"]


def row(item_number, **measures):
    facilities = measures.pop("_facilities", None)
    return dc.EnterpriseInventoryRow(
        item_number=item_number,
        measures={k: v for k, v in measures.items()},
        facilities=facilities or [],
    )


class FakeClient:
    env = "prod"
    tenant = "ESM"
    business_unit = "F3E"

    def __init__(self, rows=None, truncated=False, receipts=None, receipts_error=None):
        self._rows = rows if rows is not None else []
        self._truncated = truncated
        self._receipts = receipts or []
        self._receipts_error = receipts_error

    def get_enterprise_availability(self, **kw):
        return dc.AvailabilityResult(env=self.env, rows=self._rows, truncated=self._truncated)

    def get_purchase_order_receipts(self):
        if self._receipts_error:
            raise self._receipts_error
        return self._receipts


class FakeReceipt:
    def __init__(self, has_lot=True):
        self.has_lot = has_lot


# ── The coverage floor ───────────────────────────────────────────────────────


class TestCoverageFloor:
    def test_empty_payload_from_a_working_api_is_failed_not_zero(self):
        """A working API returning no items is SUSPICIOUS. Reading it as
        'the warehouse holds nothing' would be a fabricated stockout."""
        payload = sync.build_payload(FakeClient(rows=[]), KNOWN)
        assert payload["status"] == "failed"
        assert payload["coverage"]["rows_returned"] == 0

    def test_rows_returned_but_none_of_ours_is_failed(self):
        payload = sync.build_payload(FakeClient(rows=[row("SOMEONE-ELSE", atpQty=5)]), KNOWN)
        assert payload["status"] == "failed"
        assert payload["coverage"]["read"] == 0

    def test_some_skus_missing_is_partial_and_names_them(self):
        payload = sync.build_payload(
            FakeClient(rows=[row("PURE-Original", totalOnHandQty=100)]), KNOWN
        )
        assert payload["status"] == "partial"
        assert set(payload["coverage"]["missing"]) == {"PURE-Citrus", "PURESL"}

    def test_full_coverage_is_ok(self):
        payload = sync.build_payload(
            FakeClient(rows=[row(sku, totalOnHandQty=10) for sku in KNOWN]), KNOWN
        )
        assert payload["status"] == "ok"
        assert payload["coverage"]["missing"] == []

    def test_page_truncation_downgrades_to_partial(self):
        """Hitting the page cap means the picture is incomplete even if every
        known SKU happened to appear."""
        payload = sync.build_payload(
            FakeClient(rows=[row(sku, totalOnHandQty=10) for sku in KNOWN], truncated=True), KNOWN
        )
        assert payload["truncated"] is True
        assert payload["status"] == "partial"

    def test_unmapped_items_are_reported_not_dropped(self):
        payload = sync.build_payload(
            FakeClient(rows=[row(sku, atpQty=1) for sku in KNOWN] + [row("F3-NEWTHING", atpQty=9)]),
            KNOWN,
        )
        assert payload["coverage"]["unmapped"] == ["F3-NEWTHING"]

    def test_failed_status_makes_main_write_nothing(self, monkeypatch, tmp_path):
        target = tmp_path / "f3e-inventory-deposco.json"
        target.write_text('{"as_of_utc": "OLD-STAMP"}', encoding="utf-8")

        monkeypatch.setattr(sync.inv, "load_sku_map", lambda: {"skus": {k: {} for k in KNOWN}})
        monkeypatch.setattr(sync.dc, "DeposcoClient", lambda **kw: FakeClient(rows=[]))
        monkeypatch.setattr(sync, "store_path", lambda: target)
        wrote = []
        monkeypatch.setattr(sync.drive_io, "write_text_atomic",
                            lambda p, t: wrote.append(p))

        assert sync.main([]) == 2
        assert wrote == [], "a failed run must not write"
        # D-094: the stale file keeps its own honest timestamp.
        assert json.loads(target.read_text())["as_of_utc"] == "OLD-STAMP"

    def test_partial_run_writes_and_exits_one(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sync.inv, "load_sku_map", lambda: {"skus": {k: {} for k in KNOWN}})
        monkeypatch.setattr(
            sync.dc, "DeposcoClient",
            lambda **kw: FakeClient(rows=[row("PURE-Original", totalOnHandQty=1)]),
        )
        monkeypatch.setattr(sync, "store_path", lambda: tmp_path / "out.json")
        written = {}
        monkeypatch.setattr(sync.drive_io, "write_text_atomic",
                            lambda p, t: written.update(json.loads(t)))
        assert sync.main([]) == 1
        assert written["status"] == "partial"

    def test_read_failure_leaves_the_previous_file_alone(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sync.inv, "load_sku_map", lambda: {"skus": {k: {} for k in KNOWN}})

        class Boom(FakeClient):
            def get_enterprise_availability(self, **kw):
                raise dc.DeposcoUnavailable("3 consecutive blank 200 responses")

        monkeypatch.setattr(sync.dc, "DeposcoClient", lambda **kw: Boom())
        wrote = []
        monkeypatch.setattr(sync.drive_io, "write_text_atomic", lambda p, t: wrote.append(p))
        assert sync.main([]) == 2
        assert wrote == []

    def test_empty_sku_map_aborts_without_writing(self, monkeypatch):
        monkeypatch.setattr(sync.inv, "load_sku_map", lambda: {"skus": {}})
        wrote = []
        monkeypatch.setattr(sync.drive_io, "write_text_atomic", lambda p, t: wrote.append(p))
        assert sync.main([]) == 2
        assert wrote == []


# ── Absent is never zero ─────────────────────────────────────────────────────


class TestMeasureHonesty:
    def test_absent_measure_is_omitted_so_consumers_render_unknown(self):
        payload = sync.build_payload(
            FakeClient(rows=[row(sku, atpQty=5) for sku in KNOWN]), KNOWN
        )
        block = payload["items"]["PURE-Original"]
        assert block["measures"]["atpQty"] == 5
        assert "totalOnHandQty" not in block["measures"]

    def test_unparseable_measure_is_named_not_zeroed(self):
        rows = [dc.EnterpriseInventoryRow(item_number=sku, measures={"atpQty": None})
                for sku in KNOWN]
        block = sync.build_payload(FakeClient(rows=rows), KNOWN)["items"]["PURE-Original"]
        assert block["measures"] == {}
        assert block["unparseable_measures"] == ["atpQty"]

    def test_zero_is_preserved_as_a_real_figure(self):
        payload = sync.build_payload(
            FakeClient(rows=[row(sku, totalOnHandQty=0) for sku in KNOWN]), KNOWN
        )
        assert payload["items"]["PURE-Original"]["measures"]["totalOnHandQty"] == 0

    def test_facility_breakdown_is_carried(self):
        rows = [
            dc.EnterpriseInventoryRow(
                item_number=sku,
                measures={"totalOnHandQty": 100},
                facilities=[dc.FacilityMeasures("ESM1", {"totalOnHandQty": 100, "qtyOnPO": 4})],
            )
            for sku in KNOWN
        ]
        block = sync.build_payload(FakeClient(rows=rows), KNOWN)["items"]["PURE-Original"]
        assert block["facilities"][0]["facility"] == "ESM1"
        assert block["facilities"][0]["measures"]["qtyOnPO"] == 4


# ── Env safety ───────────────────────────────────────────────────────────────


class TestEnvSafety:
    def test_env_is_stamped_into_the_payload(self):
        payload = sync.build_payload(
            FakeClient(rows=[row(sku, atpQty=1) for sku in KNOWN]), KNOWN
        )
        assert payload["env"] == "prod"
        assert payload["tenant"] == "ESM"
        assert payload["business_unit"] == "F3E"

    def test_non_prod_run_refuses_to_write_the_store_file(self, monkeypatch, tmp_path):
        """UA carries no inventory; letting a sandbox payload land in the store
        would put '0 units' in front of an operator."""
        class UaClient(FakeClient):
            env = "ua"

        monkeypatch.setattr(sync.inv, "load_sku_map", lambda: {"skus": {k: {} for k in KNOWN}})
        monkeypatch.setattr(
            sync.dc, "DeposcoClient",
            lambda **kw: UaClient(rows=[row(sku, atpQty=1) for sku in KNOWN]),
        )
        monkeypatch.setattr(sync, "store_path", lambda: tmp_path / "out.json")
        wrote = []
        monkeypatch.setattr(sync.drive_io, "write_text_atomic", lambda p, t: wrote.append(p))
        assert sync.main(["--env", "ua"]) == 0
        assert wrote == [], "a UA run must never write the production store file"

    def test_store_file_is_its_own_writer_path(self):
        assert sync.STORE_FILENAME == "f3e-inventory-deposco.json"
        from cora import inventory_state as inv
        assert sync.STORE_FILENAME not in inv.STORE_FILES.values(), (
            "the warehouse file must not be registered as a sales-channel source"
        )


# ── Receipts lane ────────────────────────────────────────────────────────────


class TestReceiptSummary:
    def test_lot_coverage_is_reported_rather_than_assumed(self):
        client = FakeClient(
            rows=[row(sku, atpQty=1) for sku in KNOWN],
            receipts=[FakeReceipt(True), FakeReceipt(False), FakeReceipt(True)],
        )
        receipts = sync.build_payload(client, KNOWN)["receipts"]
        assert receipts["lines"] == 3
        assert receipts["with_lot"] == 2
        assert receipts["lot_coverage"] == "2 of 3"

    def test_receipt_failure_is_soft_but_visible(self):
        """A dead receipt lane must not blank the inventory figures -- nor be
        silently reported as zero receipts."""
        client = FakeClient(
            rows=[row(sku, atpQty=1) for sku in KNOWN],
            receipts_error=dc.DeposcoUnavailable("network error"),
        )
        payload = sync.build_payload(client, KNOWN)
        assert payload["status"] == "ok", "inventory still reads"
        assert payload["receipts"]["status"] == "unavailable"
        assert "lines" not in payload["receipts"]

    def test_no_receipts_says_so(self):
        client = FakeClient(rows=[row(sku, atpQty=1) for sku in KNOWN], receipts=[])
        assert sync.build_payload(client, KNOWN)["receipts"]["lot_coverage"] == (
            "no receipts returned"
        )

    def test_receipt_scope_is_described_honestly_not_as_a_date_window(self):
        """The status route takes no date filter, so the payload must not imply
        one -- a claimed "since 2026-07-01" would describe a filter that never ran."""
        client = FakeClient(rows=[row(sku, atpQty=1) for sku in KNOWN],
                            receipts=[FakeReceipt(True)])
        receipts = sync.build_payload(client, KNOWN)["receipts"]
        assert "since" not in receipts
        assert receipts["window"] == sync._RECEIPT_WINDOW


class TestDryRunRendering:
    def test_dry_run_renders_unknown_for_absent_measures(self):
        payload = sync.build_payload(
            FakeClient(rows=[row(sku, atpQty=5) for sku in KNOWN]), KNOWN
        )
        text = sync.render_dry_run(payload)
        assert "UNKNOWN" in text
        assert "PURE-Original" in text

    def test_dry_run_shouts_about_truncation_and_gaps(self):
        payload = sync.build_payload(
            FakeClient(rows=[row("PURE-Original", atpQty=5)], truncated=True), KNOWN
        )
        text = sync.render_dry_run(payload)
        assert "PAGE CAP HIT" in text
        assert "not returned" in text
        assert "PURESL" in text

    def test_dry_run_never_crashes_on_a_failed_payload(self):
        payload = sync.build_payload(FakeClient(rows=[]), KNOWN)
        assert "FAILED" in sync.render_dry_run(payload)
