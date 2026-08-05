"""A5 Part 2 -- cross-channel F3E inventory state.

The load-bearing pins:

  * ABSENT IS NEVER ZERO. An unread channel, a missing SKU and an unparseable
    count each render UNKNOWN/UNPARSEABLE. Rendering any of them as 0 would
    invent a stockout (or hide one).
  * THE MERGE LAYER DEFENDS ITSELF. Two of the three store files are written by
    Cowork tasks whose Write tool cannot guarantee temp+rename, so a torn file is
    an expected state: parse failure -> `.last-good` fallback, never a crash.
  * LABEL DISCIPLINE. Sales-CHANNEL names are allowed; data-SOURCE and tool names
    (Shopify, Seller Central/Center, Polar) must never reach a Slack surface.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cora import inventory_state as inv

_REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeReader:
    """Stands in for drive_io: an in-memory {path: text} mount."""

    def __init__(self, files: dict[Path, str] | None = None, raise_on: set[Path] | None = None):
        self.files = files or {}
        self.raise_on = raise_on or set()
        self.written: dict[Path, str] = {}

    def exists(self, path):
        if Path(path) in self.raise_on:
            raise OSError("mount gone")
        return Path(path) in self.files

    def read_text(self, path):
        if Path(path) in self.raise_on:
            raise OSError("mount gone")
        return self.files[Path(path)]

    def write_text_atomic(self, path, text):
        self.written[Path(path)] = text


SKU_MAP = {
    "channels": {"office": "Office / HQ", "dtc_3pl": "DTC 3PL", "unis": "UNIS (Cotton)",
                 "tiktok_fbt": "TikTok FBT", "amazon_fba": "Amazon FBA",
                 "walmart_wfs": "Walmart WFS", "manual": "Manual count"},
    "skus": {
        "PURE-Original": {"display_name": "F3 PURE Original", "line": "Pure"},
        "PURE-Citrus": {"display_name": "F3 PURE Citrus Clarity", "line": "Pure"},
        "F3SL": {"display_name": "F3 Strawberry Lemonade Energy", "line": "Energy"},
    },
}


def _shopify_payload(**skus):
    return {
        "source": "shopify", "as_of_utc": "2026-08-05T14:20:00+00:00",
        "channels": {
            "office": {"status": "ok", "as_of_utc": "2026-08-05T14:20:00+00:00",
                       "skus": skus.get("office", {})},
            "dtc_3pl": {"status": "ok", "as_of_utc": "2026-08-05T14:20:00+00:00",
                        "skus": skus.get("dtc_3pl", {})},
            "unis": {"status": "ok", "as_of_utc": "2026-08-05T14:20:00+00:00",
                     "skus": skus.get("unis", {})},
            "tiktok_fbt": {"status": "ok", "as_of_utc": "2026-08-05T14:20:00+00:00",
                           "skus": skus.get("tiktok_fbt", {})},
        },
    }


def _loads(shopify=None, channels=None, manual=None):
    return {
        "shopify": inv.SourceLoad("shopify", shopify, "ok" if shopify else "missing"),
        "channels": inv.SourceLoad("channels", channels, "ok" if channels else "missing"),
        "manual": inv.SourceLoad("manual", manual, "ok" if manual else "missing"),
    }


# ── absent is never zero ─────────────────────────────────────────────────────

class TestAbsentIsNeverZero:
    def test_unread_channel_is_unknown(self):
        merged = inv.merge(SKU_MAP, _loads(shopify=_shopify_payload(
            dtc_3pl={"PURE-Original": 6488})))
        row = next(r for r in merged.rows if r.sku == "PURE-Original")
        assert row.counts["dtc_3pl"].units == 6488
        assert row.counts["amazon_fba"].units is None
        assert row.counts["walmart_wfs"].units is None

    def test_rendered_unknown_never_reads_as_zero(self):
        merged = inv.merge(SKU_MAP, _loads(shopify=_shopify_payload(
            dtc_3pl={"PURE-Original": 6488})))
        body = "\n".join(inv.render_rows(merged, SKU_MAP, ["PURE-Original"]))
        assert "Amazon FBA UNKNOWN" in body
        assert "Amazon FBA 0" not in body

    def test_a_real_zero_is_still_reported_as_zero(self):
        """Zero stock is a real, actionable fact -- it must not be hidden behind
        UNKNOWN either. The distinction runs both ways."""
        merged = inv.merge(SKU_MAP, _loads(shopify=_shopify_payload(
            office={"PURE-Original": 0})))
        row = next(r for r in merged.rows if r.sku == "PURE-Original")
        assert row.counts["office"].units == 0
        assert "Office / HQ 0" in "\n".join(inv.render_rows(merged, SKU_MAP, ["PURE-Original"]))

    def test_known_total_excludes_unknowns_and_says_so(self):
        merged = inv.merge(SKU_MAP, _loads(shopify=_shopify_payload(
            office={"PURE-Original": 10}, dtc_3pl={"PURE-Original": 20})))
        row = next(r for r in merged.rows if r.sku == "PURE-Original")
        assert row.known_total == 30
        body = "\n".join(inv.render_rows(merged, SKU_MAP, ["PURE-Original"]))
        assert "known total 30" in body
        assert "excludes" in body

    def test_no_readable_channel_gives_unknown_total_not_zero(self):
        merged = inv.merge(SKU_MAP, _loads())
        row = next(r for r in merged.rows if r.sku == "PURE-Original")
        assert row.known_total is None
        assert "known total UNKNOWN" in "\n".join(inv.render_rows(merged, SKU_MAP))


class TestCountCoercion:
    @pytest.mark.parametrize("raw,expected", [
        (5, 5), (0, 0), (-3, -3), ("42", 42), ("1,234", 1234), (12.0, 12),
    ])
    def test_valid_counts(self, raw, expected):
        assert inv.coerce_count(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "  ", "lots", "n/a", "~50", 12.5, True, False, []])
    def test_invalid_counts_are_none_not_zero(self, raw):
        assert inv.coerce_count(raw) is None

    def test_unparseable_manual_count_renders_unparseable(self):
        manual = {"as_of_utc": "2026-08-05T00:00:00+00:00",
                  "counts": [{"sku": "PURE-Original", "count": "about 40",
                              "location": "Blue Chip"}]}
        merged = inv.merge(SKU_MAP, _loads(manual=manual))
        body = "\n".join(inv.render_rows(merged, SKU_MAP, ["PURE-Original"]))
        assert "UNPARSEABLE" in body
        assert "40" not in body.split("Manual count")[1][:20]


# ── untrusted input / self-defence ───────────────────────────────────────────

class TestUntrustedInput:
    def test_parse_failure_falls_back_to_last_good(self):
        reader = FakeReader({
            inv.store_path("channels"): "{ this is not json",
            inv.last_good_path("channels"): json.dumps(
                {"as_of_utc": "2026-08-04T00:00:00+00:00",
                 "channels": {"amazon_fba": {"status": "ok", "skus": {"PURE-Original": 180}}}}),
        })
        load = inv.load_source("channels", reader=reader)
        assert load.usable is True
        assert load.from_last_good is True
        assert load.status == "parse-error"

    def test_parse_failure_without_last_good_is_unusable_not_a_crash(self):
        reader = FakeReader({inv.store_path("channels"): "{ broken"})
        load = inv.load_source("channels", reader=reader)
        assert load.usable is False
        assert load.status == "parse-error"

    def test_missing_file_is_missing_not_an_error(self):
        load = inv.load_source("manual", reader=FakeReader({}))
        assert load.status == "missing"
        assert load.usable is False

    def test_dead_mount_degrades_rather_than_raising(self):
        reader = FakeReader({}, raise_on={inv.store_path("shopify")})
        load = inv.load_source("shopify", reader=reader)
        assert load.status == "unavailable"
        assert load.usable is False

    def test_non_object_payload_is_a_parse_error(self):
        reader = FakeReader({inv.store_path("manual"): "[1, 2, 3]"})
        assert inv.load_source("manual", reader=reader).usable is False

    def test_last_good_fallback_is_disclosed_in_the_footer(self):
        loads = _loads(shopify=_shopify_payload(office={"PURE-Original": 1}))
        loads["channels"] = inv.SourceLoad(
            "channels", {"channels": {}}, "parse-error", from_last_good=True)
        merged = inv.merge(SKU_MAP, loads)
        assert "last-good" in "\n".join(inv.render_rows(merged, SKU_MAP))
        assert merged.complete is False

    def test_merge_never_raises_on_garbage_shapes(self):
        for junk in ({"channels": "not a dict"}, {"channels": {"office": "nope"}},
                     {}, {"channels": {"office": {"skus": "nope"}}}):
            merged = inv.merge(SKU_MAP, _loads(shopify=junk))
            assert merged.rows  # still one row per known SKU

    def test_malformed_manual_entries_are_skipped_not_fatal(self):
        manual = {"counts": ["a string", {"no_sku": 1}, {"sku": "PURE-Citrus", "count": 9}]}
        merged = inv.merge(SKU_MAP, _loads(manual=manual))
        row = next(r for r in merged.rows if r.sku == "PURE-Citrus")
        assert row.counts["manual"].units == 9

    def test_promote_last_good_writes_the_sibling(self):
        writer = FakeReader()
        inv.promote_last_good("shopify", {"a": 1}, writer=writer)
        assert inv.last_good_path("shopify") in writer.written


# ── FBT mirror vs authoritative ──────────────────────────────────────────────

class TestFbtMirrorResolution:
    def _sweep(self, fbt_units):
        return {"channels": {"tiktok_fbt": {"status": "ok",
                                            "as_of_utc": "2026-08-05T00:00:00+00:00",
                                            "skus": {"PURE-Original": fbt_units}}}}

    def test_authoritative_preferred_over_mirror(self):
        merged = inv.merge(SKU_MAP, _loads(
            shopify=_shopify_payload(tiktok_fbt={"PURE-Original": 86}),
            channels=self._sweep(120)))
        row = next(r for r in merged.rows if r.sku == "PURE-Original")
        assert row.counts["tiktok_fbt"].units == 120

    def test_material_disagreement_shows_both(self):
        merged = inv.merge(SKU_MAP, _loads(
            shopify=_shopify_payload(tiktok_fbt={"PURE-Original": 86}),
            channels=self._sweep(120)))
        body = "\n".join(inv.render_rows(merged, SKU_MAP, ["PURE-Original"]))
        assert "mirror reads 86" in body

    def test_agreement_shows_no_conflict_note(self):
        merged = inv.merge(SKU_MAP, _loads(
            shopify=_shopify_payload(tiktok_fbt={"PURE-Original": 86}),
            channels=self._sweep(86)))
        assert "mirror reads" not in "\n".join(inv.render_rows(merged, SKU_MAP))

    def test_mirror_used_when_sweep_has_not_run(self):
        merged = inv.merge(SKU_MAP, _loads(
            shopify=_shopify_payload(tiktok_fbt={"PURE-Original": 86})))
        row = next(r for r in merged.rows if r.sku == "PURE-Original")
        assert row.counts["tiktok_fbt"].units == 86
        assert row.counts["tiktok_fbt"].caveat == "mirror"

    def test_mirror_caveat_is_rendered(self):
        merged = inv.merge(SKU_MAP, _loads(
            shopify=_shopify_payload(tiktok_fbt={"PURE-Original": 86})))
        assert "(mirror)" in "\n".join(inv.render_rows(merged, SKU_MAP, ["PURE-Original"]))

    def test_unis_weekly_fed_caveat_is_rendered(self):
        merged = inv.merge(SKU_MAP, _loads(shopify=_shopify_payload(
            unis={"PURE-Original": 500})))
        assert "(weekly-fed)" in "\n".join(inv.render_rows(merged, SKU_MAP, ["PURE-Original"]))


# ── unmapped items ───────────────────────────────────────────────────────────

class TestUnmapped:
    def test_unknown_channel_sku_is_surfaced_not_dropped(self):
        sweep = {"channels": {"amazon_fba": {"status": "ok",
                                             "skus": {"MYSTERY-SKU": 40}}}}
        merged = inv.merge(SKU_MAP, _loads(channels=sweep))
        assert merged.unmapped_items == [{"channel": "amazon_fba", "sku": "MYSTERY-SKU"}]
        assert "UNMAPPED" in "\n".join(inv.render_rows(merged, SKU_MAP))


# ── label discipline (D-051 finding 10) ──────────────────────────────────────

_FORBIDDEN = ("shopify", "seller central", "seller center", "polar")


class TestLabelDiscipline:
    def _all_rendered_text(self, merged):
        return " ".join(inv.render_rows(merged, SKU_MAP)
                        + [inv.render_channel_summary(merged, SKU_MAP)]).lower()

    def test_rendered_output_never_names_a_source_or_tool(self):
        merged = inv.merge(SKU_MAP, _loads(
            shopify=_shopify_payload(office={"PURE-Original": 1},
                                     dtc_3pl={"PURE-Original": 2},
                                     unis={"PURE-Original": 3},
                                     tiktok_fbt={"PURE-Original": 4}),
            channels={"channels": {"amazon_fba": {"status": "ok",
                                                  "skus": {"PURE-Original": 5}}}},
            manual={"counts": [{"sku": "PURE-Original", "count": 6}]}))
        text = self._all_rendered_text(merged)
        for banned in _FORBIDDEN:
            assert banned not in text, f"rendered output leaked the source name {banned!r}"

    def test_channel_names_ARE_allowed(self):
        merged = inv.merge(SKU_MAP, _loads(
            shopify=_shopify_payload(dtc_3pl={"PURE-Original": 2})))
        text = self._all_rendered_text(merged)
        assert "amazon fba" in text and "walmart wfs" in text and "tiktok fbt" in text

    def test_coverage_footer_uses_neutral_source_labels(self):
        """The footer names WHICH FEED was unreadable. Naming the internal source
        key ("shopify: missing") leaked a data-source name onto a Slack surface."""
        merged = inv.merge(SKU_MAP, _loads())
        footer = inv.render_rows(merged, SKU_MAP)[-1]
        assert "shopify" not in footer.lower()
        assert "warehouse + DTC feed" in footer

    def test_shipped_sku_map_labels_carry_no_source_names(self):
        shipped = inv.load_sku_map()
        for label in (shipped.get("channels") or {}).values():
            assert not any(b in str(label).lower() for b in _FORBIDDEN)

    def test_error_status_from_an_untrusted_file_is_scrubbed(self):
        sweep = {"channels": {"amazon_fba": {
            "status": "<!channel> *broken* `x`", "skus": {}}}}
        merged = inv.merge(SKU_MAP, _loads(channels=sweep))
        body = "\n".join(inv.render_rows(merged, SKU_MAP, ["PURE-Original"]))
        # '|' is OUR channel separator, so scope the assertion to the segment the
        # untrusted string landed in.
        segment = body.split("Amazon FBA")[1].split("|")[0]
        for ch in "<>`*":
            assert ch not in segment, f"unscrubbed control char in {segment!r}"
        assert "!channel broken x" in segment

    def test_scrub_flattens_and_caps(self):
        assert inv.scrub("a\n\nb   c") == "a b c"
        assert len(inv.scrub("x" * 500)) == 80
        assert inv.scrub("<!here> *bold*") == "!here bold"


# ── coverage + summary ───────────────────────────────────────────────────────

class TestCoverage:
    def test_complete_only_when_every_channel_read(self):
        partial = inv.merge(SKU_MAP, _loads(shopify=_shopify_payload(
            office={"PURE-Original": 1})))
        assert partial.complete is False

    def test_coverage_counts_channels_not_sources(self):
        merged = inv.merge(SKU_MAP, _loads(shopify=_shopify_payload(
            office={"PURE-Original": 1}, dtc_3pl={"PURE-Original": 2},
            unis={"PURE-Original": 3}, tiktok_fbt={"PURE-Original": 4})))
        assert merged.expected_channels == 6
        assert merged.covered_channels == 4

    def test_a_block_with_no_skus_does_not_count_as_read(self):
        """A present, status-ok block carrying no SKU payload is structurally
        blind. Counting it let a broken writer report full coverage while
        contributing no data -- an all-clear over nothing."""
        merged = inv.merge(SKU_MAP, _loads(shopify=_shopify_payload(
            office={"PURE-Original": 1}, dtc_3pl={}, unis={}, tiktok_fbt={})))
        assert merged.covered_channels == 1
        assert merged.complete is False

    def test_channel_summary_names_unread_channels(self):
        merged = inv.merge(SKU_MAP, _loads(shopify=_shopify_payload(
            dtc_3pl={"PURE-Original": 100})))
        line = inv.render_channel_summary(merged, SKU_MAP)
        assert "not yet swept" in line
        assert "UNKNOWN, not zero" in line

    def test_channel_summary_with_nothing_readable_says_unread(self):
        line = inv.render_channel_summary(inv.merge(SKU_MAP, _loads()), SKU_MAP)
        assert "not zero" in line

    def test_channel_totals_sum_across_skus(self):
        merged = inv.merge(SKU_MAP, _loads(shopify=_shopify_payload(
            dtc_3pl={"PURE-Original": 100, "PURE-Citrus": 50})))
        assert inv.channel_totals(merged)["dtc_3pl"]["units"] == 150


# ── shipped SKU map ──────────────────────────────────────────────────────────

class TestShippedSkuMap:
    def test_loads_and_carries_the_15_live_beverage_skus(self):
        smap = inv.load_sku_map()
        skus = smap["skus"]
        assert len(skus) == 15
        # Verified live against the Shopify Admin API 2026-08-04.
        for sku in ("PURE-Original", "PURE-Citrus", "PURE-Tropical", "PURESL",
                    "F3-PureE-V4F", "F3-Original", "F3SL", "F3VPM4"):
            assert sku in skus

    def test_pure_skus_carry_confirmed_amazon_and_tiktok_ids(self):
        """Confirmed from canon: the Amazon map is corroborated by three
        independent documents, and the TikTok goods IDs were each read inside the
        authenticated FBT console during the 2026-07-13 incident resolution."""
        skus = inv.load_sku_map()["skus"]
        assert skus["PURE-Original"]["amazon"]["asin"] == "B0GG4ZTDK9"
        assert skus["PURE-Original"]["amazon"]["confirmed"] is True
        assert skus["PURE-Original"]["tiktok"]["goods_id"] == "2084043295299590"
        assert skus["PURESL"]["amazon"]["asin"] == "B0GG4MKHY3"
        assert skus["F3-PureE-V4F"]["tiktok"]["goods_id"] == "2084018935044102"

    def test_walmart_ids_are_marked_unconfirmed(self):
        """Walmart Seller Center login has been dead since ~2026-07-27, so nothing
        could be read back. D-118: an unverified identifier renders provisional."""
        skus = inv.load_sku_map()["skus"]
        for sku in ("PURE-Original", "PURE-Citrus", "PURE-Tropical", "PURESL", "F3-PureE-V4F"):
            assert skus[sku]["walmart"]["confirmed"] is False

    def test_energy_and_mood_carry_no_marketplace_ids_yet(self):
        skus = inv.load_sku_map()["skus"]
        for sku in ("F3-Original", "F3SL", "F3-Orange", "F3VPM4"):
            assert "amazon" not in skus[sku]
            assert "tiktok" not in skus[sku]

    def test_unreadable_map_fails_soft_to_empty(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("just: [broken\n", encoding="utf-8")
        assert inv.load_sku_map(bad)["skus"] == {}

    def test_shopify_location_ids_match_the_live_store(self):
        """Pinned from a live get_active_locations() read 2026-08-04. If a location
        is renamed or re-created these ids change and the sync silently reads the
        wrong shelf."""
        assert inv.SHOPIFY_LOCATIONS == {
            "office": 81567023424,
            "dtc_3pl": 110064533824,
            "unis": 98823012672,
            "tiktok_fbt": 111242608960,
        }


# ── wiring ───────────────────────────────────────────────────────────────────

class TestWiring:
    def test_tool_is_registered_and_scoped(self):
        from cora.tools import tool_dispatch as td
        assert "f3e_channel_inventory" in td._TOOL_FUNCTIONS
        assert any(t["name"] == "f3e_channel_inventory" for t in td.TOOL_DEFINITIONS)
        names = lambda e, x=False: {t["name"] for t in td.tools_for_entity(e, x)}  # noqa: E731
        assert "f3e_channel_inventory" in names("F3E")
        assert "f3e_channel_inventory" in names("FNDR")
        assert "f3e_channel_inventory" not in names("OSN")
        assert "f3e_channel_inventory" not in names("LEX")

    def test_dashboard_registry_entry_exists_and_is_entity_scoped(self):
        from cora import dashboard_access
        store = dashboard_access.store_for("f3e-channel-inventory")
        assert set(store["files"]) == {"shopify", "channels", "manual"}
        # F3E channel allowed; an OSN channel refused.
        assert dashboard_access.check_dashboard_access(
            "f3e-channel-inventory", "U0B2RM2JYJ1", "f3e-leadership") is None
        assert dashboard_access.check_dashboard_access(
            "f3e-channel-inventory", "U0B2RM2JYJ1", "osn-leadership") is not None

    def test_never_cached(self):
        """Inventory is time-sensitive; a cached reply could report yesterday's
        stock as today's."""
        from cora.tools import tool_dispatch as td
        source = (_REPO_ROOT / "src" / "cora" / "tools" / "tool_dispatch.py").read_text(
            encoding="utf-8")
        assert "f3e_channel_inventory" in source
        # VERBATIM_TABLE_TOOLS membership does double duty: the reply skips the
        # conversational re-format AND is kept out of the shared cache.
        assert "f3e_channel_inventory" in td.VERBATIM_TABLE_TOOLS

    def test_f3e_prompt_documents_routing_and_label_discipline(self):
        prompt = (_REPO_ROOT / "design" / "system-prompts" / "f3e.md").read_text(
            encoding="utf-8")
        assert "f3e_channel_inventory" in prompt
        assert "Cross-channel inventory" in prompt
        assert "Channel names vs source names" in prompt
        assert "never say Shopify, Seller Central, Seller Center, or Polar" in prompt

    def test_store_files_are_one_per_writer(self):
        """Single-writer-per-file is what kills clobber races by construction."""
        assert len(set(inv.STORE_FILES.values())) == len(inv.STORE_FILES) == 3


class TestToolBehaviour:
    """The tool body itself -- gating, filtering, and the degraded path."""

    HARRISON = "U0B2RM2JYJ1"

    def _call(self, monkeypatch, merged=None, channel="f3e-leadership", **inp):
        from cora.tools import tool_dispatch as td
        if merged is not None:
            monkeypatch.setattr(inv, "merge", lambda *a, **k: merged)
        return td._tool_f3e_channel_inventory(
            self.HARRISON, "F3E", {"_channel_name": channel, **inp})

    def test_refuses_outside_the_allowed_scope(self, monkeypatch):
        out = self._call(monkeypatch, channel="osn-leadership")
        assert "3PL" not in out and "Amazon FBA" not in out

    def test_renders_rows_in_an_allowed_channel(self, monkeypatch):
        merged = inv.merge(SKU_MAP, _loads(shopify=_shopify_payload(
            dtc_3pl={"PURE-Original": 6488})))
        out = self._call(monkeypatch, merged)
        assert "F3 PURE Original" in out
        assert "6,488" in out

    def test_partial_coverage_is_announced_in_the_header(self, monkeypatch):
        merged = inv.merge(SKU_MAP, _loads(shopify=_shopify_payload(
            dtc_3pl={"PURE-Original": 1})))
        out = self._call(monkeypatch, merged)
        assert "partial coverage" in out
        assert "not zero" in out

    def test_line_filter_narrows_to_that_line(self, monkeypatch):
        merged = inv.merge(SKU_MAP, _loads(shopify=_shopify_payload(
            dtc_3pl={"PURE-Original": 1, "F3SL": 2})))
        out = self._call(monkeypatch, merged, sku_filter="Energy")
        assert "Strawberry Lemonade Energy" in out
        assert "F3 PURE Original" not in out

    def test_exact_sku_filter_works(self, monkeypatch):
        merged = inv.merge(SKU_MAP, _loads(shopify=_shopify_payload(
            dtc_3pl={"PURE-Citrus": 3648})))
        out = self._call(monkeypatch, merged, sku_filter="PURE-Citrus")
        assert "3,648" in out

    def test_unknown_filter_says_so_rather_than_showing_everything(self, monkeypatch):
        merged = inv.merge(SKU_MAP, _loads())
        out = self._call(monkeypatch, merged, sku_filter="Gatorade")
        assert "don't have a SKU or product line" in out

    def test_empty_store_degrades_honestly_without_zeros(self, monkeypatch):
        """Every store file missing must read as UNKNOWN, never as a stockout."""
        merged = inv.merge(SKU_MAP, _loads())
        out = self._call(monkeypatch, merged)
        assert "UNKNOWN" in out
        assert "known total 0" not in out

    def test_tool_output_never_names_a_source(self, monkeypatch):
        merged = inv.merge(SKU_MAP, _loads(shopify=_shopify_payload(
            dtc_3pl={"PURE-Original": 1})))
        out = self._call(monkeypatch, merged).lower()
        for banned in _FORBIDDEN:
            assert banned not in out
