"""Tests for the warehouse (3PL) consumer surface -- Phase 1, S5.

Two guarantees, both of which matter more than the feature itself:

  * NOTHING UNREADABLE EVER RENDERS AS ZERO. There are four distinct ways this
    view can fail to know the stock level, and all four must read as UNKNOWN --
    a fabricated stockout in a leadership channel gets acted on.
  * THE WMS IS NEVER NAMED on a Slack surface. The facility and the sales
    channels are operational facts and stay sayable; the system we read them out
    of is a data-source name, and the same rule that keeps the storefront
    platform off these lines applies to it.

Both consumers ship DARK: the Phase-1 gate is two consecutive clean weekly
reconciles, which no build session can satisfy.
"""

from __future__ import annotations

import pytest

from cora import dashboard_access, inventory_state as inv
from cora.tools import tool_dispatch as td

HARRISON = "U0B2RM2JYJ1"
OTHER = "U0BSOMEONEELSE"
DASH = "f3e-warehouse-inventory"

#: The WMS name must never appear on a rendered surface.
_FORBIDDEN = ("deposco", "shopify", "seller central", "seller center", "polar")


def payload(**over):
    base = {
        "source": "deposco",
        "env": "prod",
        "status": "ok",
        "as_of_utc": "2026-08-14T13:00:00+00:00",
        "items": {
            "PURE-Original": {"measures": {"totalOnHandQty": 12480}, "facilities": []},
            "PURESL": {"measures": {"totalOnHandQty": 3120}, "facilities": []},
        },
        "coverage": {"known_skus": 2, "read": 2, "missing": [], "unmapped": [],
                     "rows_returned": 2},
        "truncated": False,
    }
    base.update(over)
    return base


def load(data, status="ok"):
    return inv.SourceLoad("warehouse", data, status)


# ── Unknown is never zero ────────────────────────────────────────────────────


class TestNeverZero:
    def test_healthy_render(self):
        line = inv.render_warehouse_line(load(payload()))
        assert "12,480" in line and "3,120" in line
        assert "PURE-Original" in line

    def test_unreadable_store_file_reads_unknown(self):
        line = inv.render_warehouse_line(inv.SourceLoad("warehouse", None, "missing"))
        assert "UNKNOWN, not zero" in line
        assert "0" not in line.replace("3PL", "")

    def test_failed_sync_status_reads_unknown(self):
        """The writer's coverage floor already refused to write; if a stale
        failed payload is somehow present, the reader refuses too."""
        line = inv.render_warehouse_line(load(payload(status="failed")))
        assert "did not complete" in line and "UNKNOWN, not zero" in line

    def test_non_production_payload_is_withheld(self):
        """The sandbox carries no inventory at all -- rendering it would print
        zeroes as fact."""
        line = inv.render_warehouse_line(load(payload(env="ua")))
        assert "non-production" in line and "UNKNOWN, not zero" in line
        assert "12,480" not in line

    def test_missing_env_stamp_is_withheld(self):
        data = payload()
        del data["env"]
        assert "UNKNOWN, not zero" in inv.render_warehouse_line(load(data))

    def test_empty_items_reads_unknown(self):
        assert "UNKNOWN, not zero" in inv.render_warehouse_line(load(payload(items={})))

    def test_absent_measure_renders_unknown_for_that_sku_only(self):
        data = payload(items={
            "PURE-Original": {"measures": {"totalOnHandQty": 500}, "facilities": []},
            "PURESL": {"measures": {}, "facilities": []},
        })
        line = inv.render_warehouse_line(load(data))
        assert "PURE-Original 500" in line
        assert "PURESL UNKNOWN" in line

    def test_zero_on_hand_is_reported_as_a_real_figure(self):
        data = payload(items={"PURE-Original": {"measures": {"totalOnHandQty": 0},
                                                "facilities": []}})
        assert "PURE-Original 0" in inv.render_warehouse_line(load(data))

    def test_partial_coverage_is_named(self):
        data = payload(coverage={"known_skus": 5, "read": 2, "missing": ["A", "B", "C"],
                                 "unmapped": [], "rows_returned": 2})
        line = inv.render_warehouse_line(load(data))
        assert "3 SKU(s) not returned (UNKNOWN, not zero)" in line

    def test_truncation_is_named(self):
        assert "truncated" in inv.render_warehouse_line(load(payload(truncated=True)))

    def test_as_of_is_always_carried(self):
        assert "as of 2026-08-14" in inv.render_warehouse_line(load(payload()))

    @pytest.mark.parametrize("junk", [
        {"items": "nope"}, {"items": {"X": "nope"}}, {"items": {"X": {"measures": "nope"}}},
    ])
    def test_malformed_payload_never_crashes(self, junk):
        data = payload(**junk)
        data["env"] = "prod"
        assert isinstance(inv.render_warehouse_line(load(data)), str)


# ── Label discipline ─────────────────────────────────────────────────────────


class TestLabelDiscipline:
    def test_rendered_line_never_names_the_wms(self):
        line = inv.render_warehouse_line(load(payload())).lower()
        for banned in _FORBIDDEN:
            assert banned not in line, f"rendered line leaked the source name {banned!r}"

    def test_scrub_neutralizes_the_wms_name(self):
        assert "deposco" not in inv.scrub("pulled from Deposco WMS").lower()

    def test_an_injected_sku_name_cannot_reach_slack_intact(self):
        """Item numbers come straight out of an external system."""
        data = payload(items={
            "<!channel> https://evil.test *x*": {"measures": {"totalOnHandQty": 1},
                                                 "facilities": []},
        })
        line = inv.render_warehouse_line(load(data))
        assert "<!channel>" not in line
        assert "https://" not in line
        assert "*x*" not in line

    def test_facility_names_stay_sayable(self):
        """Nimbl is a FACILITY -- an operational fact, not a data source."""
        assert inv.scrub("Nimbl") == "Nimbl"


# ── Both consumers ship dark ─────────────────────────────────────────────────


class TestShipsDark:
    def test_flag_defaults_off(self, monkeypatch):
        monkeypatch.delenv(inv.WAREHOUSE_FLAG, raising=False)
        assert inv.warehouse_enabled() is False

    @pytest.mark.parametrize("value,expected", [
        ("1", True), ("true", True), ("TRUE", True), ("yes", True), ("on", True),
        ("0", False), ("false", False), ("", False), ("maybe", False),
    ])
    def test_flag_parsing(self, monkeypatch, value, expected):
        monkeypatch.setenv(inv.WAREHOUSE_FLAG, value)
        assert inv.warehouse_enabled() is expected

    def test_synthesis_omits_the_line_when_the_flag_is_off(self, monkeypatch):
        monkeypatch.delenv(inv.WAREHOUSE_FLAG, raising=False)
        from cora import channel_synthesis as cs
        source = __import__("pathlib").Path(cs.__file__).read_text(encoding="utf-8")
        assert "warehouse_enabled()" in source, "the synthesis line must be flag-gated"

    def test_tool_refuses_while_the_feed_is_unreconciled(self, monkeypatch):
        monkeypatch.delenv(inv.WAREHOUSE_FLAG, raising=False)
        out = td._tool_f3e_warehouse_inventory(
            HARRISON, "F3E", {"_channel_name": "f3e-leadership"}
        )
        assert "reconcil" in out.lower()
        # It must not leak a figure while declining, and must offer the lane that
        # IS validated today.
        assert "cross-channel" in out.lower()

    def test_tool_declines_without_naming_the_wms(self, monkeypatch):
        monkeypatch.delenv(inv.WAREHOUSE_FLAG, raising=False)
        out = td._tool_f3e_warehouse_inventory(
            HARRISON, "F3E", {"_channel_name": "f3e-leadership"}
        ).lower()
        for banned in _FORBIDDEN:
            assert banned not in out


# ── Channel allowlist (the cq-0b5a374c5b07 narrowing) ────────────────────────


class TestChannelAllowlist:
    def test_registered_in_the_shipped_registry(self):
        assert dashboard_access.check_dashboard_access(DASH, HARRISON, "f3e-leadership") is None

    @pytest.mark.parametrize("channel", [
        "f3e-leadership", "f3-hq-inventory-adjustments", "f3e-sales", "founder-operations",
    ])
    def test_allowed_channels(self, channel):
        assert dashboard_access.check_dashboard_access(DASH, HARRISON, channel) is None

    @pytest.mark.parametrize("channel", [
        "f3-athletes", "llc-leadership", "osn-leadership", "random-channel",
    ])
    def test_refused_elsewhere(self, channel):
        """Starts at the NARROW end, matching the 2026-08-05 audience pullback:
        per-SKU stock does not belong in every f3-* channel."""
        refusal = dashboard_access.check_dashboard_access(DASH, HARRISON, channel)
        assert refusal, f"{channel} must be refused"

    def test_non_harrison_dm_refused(self):
        assert dashboard_access.check_dashboard_access(DASH, OTHER, "dm")

    def test_refusal_leaks_no_channel_or_source_name(self):
        refusal = dashboard_access.check_dashboard_access(DASH, OTHER, "f3-athletes") or ""
        low = refusal.lower()
        for term in ("deposco", "f3e-leadership", "founder-operations", "warehouse"):
            assert term not in low, f"refusal leaked {term!r}"

    def test_tool_refuses_in_a_disallowed_channel_even_with_the_flag_on(self, monkeypatch):
        monkeypatch.setenv(inv.WAREHOUSE_FLAG, "1")
        out = td._tool_f3e_warehouse_inventory(
            HARRISON, "F3E", {"_channel_name": "f3-athletes"}
        )
        assert "12,480" not in out
        assert "deposco" not in out.lower()


# ── Wiring ───────────────────────────────────────────────────────────────────


class TestWiring:
    NAME = "f3e_warehouse_inventory"

    def test_registered_everywhere_it_must_be(self):
        assert self.NAME in {d["name"] for d in td.TOOL_DEFINITIONS}
        assert self.NAME in td._TOOL_FUNCTIONS
        assert self.NAME in td.VERBATIM_TABLE_TOOLS       # verbatim + never cached
        assert self.NAME in td._TOOL_TIMEOUTS

    def test_exposed_to_f3e_and_founder_only(self):
        for entity in ("F3E", "FNDR"):
            assert self.NAME in {d["name"] for d in td.tools_for_entity(entity)}
        for entity in ("LEX", "LEX-LLC", "OSN", "UFL", "BDM"):
            assert self.NAME not in {d["name"] for d in td.tools_for_entity(entity)}, (
                f"{entity} must not see F3E warehouse stock"
            )

    def test_description_points_at_the_right_sibling(self):
        spec = [d for d in td.TOOL_DEFINITIONS if d["name"] == self.NAME][0]
        assert "f3e_channel_inventory" in spec["description"]
        assert "UNKNOWN, never as zero" in spec["description"]

    def test_warehouse_file_is_not_a_sales_channel_source(self):
        assert inv.WAREHOUSE_FILE not in inv.STORE_FILES.values()

    def test_warehouse_file_matches_the_writer(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
        import run_deposco_inventory_sync as sync
        assert sync.STORE_FILENAME == inv.WAREHOUSE_FILE, (
            "the writer and the reader must agree on the filename"
        )
