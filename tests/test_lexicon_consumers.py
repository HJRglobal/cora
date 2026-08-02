"""S2 tests: programmatic lexicon consumers (flag-gated at CORA_LEXICON=resolve).

Pins the load-bearing invariants:
  - BEHAVIOR-IDENTICAL: the inventory tool's alias step produces the same
    resolution / fall-through as today for every SKU-map input, at every flag
    level, including under a lexicon load failure (ADDITIVE rescue).
  - Ambiguous lexicon products ASK with candidates (which-line UX), never guess.
  - Write-path provenance: a lexicon-fed preview names the resolution.
  - Confirm capture: an executed lexicon-resolved write logs resolution_confirmed.
  - Person lookups: zero behavior change; telemetry only.
  - Asana task matcher: expansion on MISS only; a selected task is never
    retargeted; ambiguity never expands.
"""

from __future__ import annotations

import json
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import lexicon
from cora.connectors import shopify_client
from cora.connectors.shopify_client import VariantMatch
from cora.tools import tool_dispatch
from cora.tools.tool_dispatch import (
    _load_sku_aliases,
    _load_user_aliases,
    _resolve_sku_alias,
    _tool_f3e_shopify_set_inventory,
    resolve_name_to_slack_user_id,
)

_ALEX = "U0B3VGWJTMJ"
_CHAN = "f3e-leadership"
_OFFICE = "1337 S Gilbert Rd"
_LOCS = [{"id": 81567023424, "name": _OFFICE}]
_CONFIG = (frozenset({_OFFICE.lower()}), {"office": _OFFICE.lower()})
_PURE = VariantMatch(
    product_title="F3 PURE Original Energy Drink", variant_title="12 Pack",
    sku="PU-ORIG-12", variant_id=11, inventory_item_id=52999599030592,
)


@pytest.fixture(autouse=True)
def _fresh_lexicon(monkeypatch, tmp_path):
    """Fresh lexicon cache per test + telemetry redirected to tmp."""
    monkeypatch.setenv("LEXICON_RESOLUTIONS_PATH", str(tmp_path / "resolutions.jsonl"))
    lexicon.invalidate_cache()
    yield
    lexicon.invalidate_cache()


def _stub(stack: ExitStack, *, variants=None, current=202, set_result=203):
    stack.enter_context(patch.object(shopify_client, "get_active_locations",
                                     return_value=list(_LOCS)))
    m_resolve = stack.enter_context(patch.object(
        shopify_client, "resolve_variants",
        return_value=list([_PURE] if variants is None else variants)))
    stack.enter_context(patch.object(shopify_client, "get_inventory_level",
                                     return_value=current))
    stack.enter_context(patch.object(shopify_client, "set_inventory_level",
                                     return_value=set_result))
    stack.enter_context(patch.object(tool_dispatch, "_load_shopify_write_config",
                                     return_value=_CONFIG))
    stack.enter_context(patch.object(tool_dispatch, "_load_inventory_channel_config",
                                     side_effect=lambda name: {}))
    return m_resolve


def _preview(user=_ALEX, **kw) -> str:
    base = {"_channel_name": _CHAN}
    base.update(kw)
    return _tool_f3e_shopify_set_inventory(user, "F3E", base)


def _confirm(user=_ALEX, **kw) -> str:
    base = {"_channel_name": _CHAN, "confirmed": True}
    base.update(kw)
    return _tool_f3e_shopify_set_inventory(user, "F3E", base)


def _resolve_variants_arg(stack_calls) -> str:
    assert stack_calls.call_args_list, "resolve_variants never called"
    return stack_calls.call_args_list[0].args[0]


# ── Inventory: behavior-identical pin ────────────────────────────────────────


class TestInventoryBehaviorIdentical:
    # (input, expected query passed to resolve_variants) -- from the live SKU map.
    _CASES = [
        ("energy variety pack", "F3VPE4"),        # SKU-map alias -> canonical
        ("Original Energy", "F3-Original"),       # case-insensitive alias
        ("pure variety", "F3-PureE-V4F"),
        ("totally unknown product zzz", "totally unknown product zzz"),  # miss -> raw
        ("PU-ORIG-12", "PU-ORIG-12"),             # bare SKU -> raw fall-through
    ]

    @pytest.mark.parametrize("level", ["off", "resolve", "full"])
    def test_same_resolution_at_every_level(self, monkeypatch, level):
        monkeypatch.setenv("CORA_LEXICON", level)
        for query, expected in self._CASES:
            with ExitStack() as s:
                m = _stub(s)
                _preview(product=query, location="office", quantity=203)
                assert _resolve_variants_arg(m) == expected, (level, query)

    def test_lexicon_load_failure_rescued_by_legacy_alias_step(self, monkeypatch):
        """ADDITIVE invariant on the write path: a broken lexicon store at
        level=resolve leaves the SKU alias step exactly as today."""
        monkeypatch.setenv("CORA_LEXICON", "resolve")
        monkeypatch.setenv("LEXICON_DIR", "Z:/does/not/exist")
        monkeypatch.setenv("LEXICON_SKU_ALIASES_PATH", "Z:/nope.yaml")
        monkeypatch.setenv("LEXICON_USER_ALIASES_PATH", "Z:/nope2.yaml")
        lexicon.invalidate_cache()
        with ExitStack() as s:
            m = _stub(s)
            _preview(product="energy variety pack", location="office", quantity=203)
            assert _resolve_variants_arg(m) == "F3VPE4"


class TestInventoryAmbiguityAsks:
    def test_seeded_variety_pack_asks_which_line(self, monkeypatch):
        monkeypatch.setenv("CORA_LEXICON", "resolve")
        with ExitStack() as s:
            m = _stub(s)
            r = _preview(product="variety pack", location="office", quantity=10)
            assert r.startswith("WRITE_BLOCKED")
            assert "NOT WRITTEN" in r
            for name in ("F3VPE4", "F3VPM4", "F3-PureE-V4F"):
                assert name in r
            assert "Which one?" in r
            assert not m.called  # blocked BEFORE any live resolve
            assert not tool_dispatch.has_pending_shopify_write(_ALEX, _CHAN)

    def test_off_level_falls_through_to_live_resolver(self, monkeypatch):
        monkeypatch.delenv("CORA_LEXICON", raising=False)
        with ExitStack() as s:
            m = _stub(s)
            _preview(product="variety pack", location="office", quantity=10)
            assert _resolve_variants_arg(m) == "variety pack"


class TestInventoryProvenance:
    def test_single_preview_names_the_resolution(self, monkeypatch):
        monkeypatch.setenv("CORA_LEXICON", "resolve")
        with ExitStack() as s:
            _stub(s)
            r = _preview(product="energy variety pack", location="office", quantity=203)
            assert 'resolved from "energy variety pack"' in r

    def test_no_provenance_without_lexicon_hit(self, monkeypatch):
        monkeypatch.setenv("CORA_LEXICON", "resolve")
        with ExitStack() as s:
            _stub(s)
            r = _preview(product="pure original 12", location="office", quantity=203)
            assert "resolved from" not in r

    def test_bulk_preview_names_the_resolution_per_row(self, monkeypatch):
        monkeypatch.setenv("CORA_LEXICON", "resolve")
        with ExitStack() as s:
            _stub(s)
            r = _preview(items=[
                {"product": "energy variety pack", "location": "office", "quantity": 5},
            ])
            assert 'resolved from "energy variety pack"' in r


class TestConfirmCapture:
    def test_executed_lexicon_write_logs_resolution_confirmed(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CORA_LEXICON", "resolve")
        log_path = tmp_path / "resolutions.jsonl"
        monkeypatch.setenv("LEXICON_RESOLUTIONS_PATH", str(log_path))
        with ExitStack() as s:
            _stub(s)
            _preview(product="energy variety pack", location="office", quantity=203)
            r = _confirm()
            assert r.startswith("WRITE_CONFIRMED")
        rows = [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines()]
        confirmed = [r for r in rows if r.get("event") == "resolution_confirmed"]
        assert len(confirmed) == 1
        assert confirmed[0]["user"] == _ALEX
        assert confirmed[0]["canonical"] == "F3VPE4"
        assert confirmed[0]["query_display"] == "energy variety pack"

    def test_non_lexicon_write_logs_no_confirm_event(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CORA_LEXICON", "resolve")
        log_path = tmp_path / "resolutions.jsonl"
        monkeypatch.setenv("LEXICON_RESOLUTIONS_PATH", str(log_path))
        with ExitStack() as s:
            _stub(s)
            _preview(product="pure original 12", location="office", quantity=203)
            r = _confirm()
            assert r.startswith("WRITE_CONFIRMED")
        rows = [json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines()] \
            if log_path.exists() else []
        assert not [r for r in rows if r.get("event") == "resolution_confirmed"]


# ── Learned-section merges (append-only review-rail writes) ──────────────────


class TestLearnedSectionMerges:
    def test_sku_learned_section_merges_seed_wins(self, monkeypatch, tmp_path):
        p = tmp_path / "skus.yaml"
        p.write_text(
            "skus:\n"
            '  F3-Original: ["original energy"]\n'
            "learned:\n"
            '  - {sku: F3SL, aliases: ["office strawberry"]}\n'
            '  - {sku: F3SL, aliases: ["office sl"]}\n'          # 2nd row, same SKU
            '  - {sku: F3VPM4, aliases: ["original energy"]}\n',  # collides -> seed wins
            encoding="utf-8")
        monkeypatch.setattr(tool_dispatch, "_SHOPIFY_SKU_ALIAS_PATH", p)
        alias_to_sku, display = _load_sku_aliases()
        assert alias_to_sku["office strawberry"] == "F3SL"
        assert alias_to_sku["office sl"] == "F3SL"
        assert alias_to_sku["original energy"] == "F3-Original"  # seed precedence
        assert display.count("original energy") == 1

    def test_missing_learned_section_is_noop(self, monkeypatch, tmp_path):
        p = tmp_path / "skus.yaml"
        p.write_text('skus:\n  F3-Original: ["original energy"]\n', encoding="utf-8")
        monkeypatch.setattr(tool_dispatch, "_SHOPIFY_SKU_ALIAS_PATH", p)
        alias_to_sku, _ = _load_sku_aliases()
        assert alias_to_sku == {"original energy": "F3-Original"}

    def test_user_learned_aliases_extend_never_replace(self, monkeypatch, tmp_path):
        p = tmp_path / "users.yaml"
        p.write_text(
            "aliases:\n"
            '  Jennifer Mortensen: ["Jen"]\n'
            "learned_aliases:\n"
            '  - {name: "Jennifer Mortensen", aliases: ["Jenny M"]}\n'
            '  - {name: "Tommy Anderson", aliases: ["T-dawg"]}\n',
            encoding="utf-8")
        monkeypatch.setattr(tool_dispatch, "_ALIASES_PATH", p)
        cfg = _load_user_aliases()
        assert cfg["aliases"]["Jennifer Mortensen"] == ["Jen", "Jenny M"]
        assert cfg["aliases"]["Tommy Anderson"] == ["T-dawg"]


# ── Person lookups: zero behavior change + telemetry ─────────────────────────


_SLACK_MAP = {
    "U111": {"slack_user_id": "U111", "display_name": "Jennifer Mortensen",
             "asana_user_gid": "g111"},
    "U222": {"slack_user_id": "U222", "display_name": "Tommy Anderson",
             "asana_user_gid": "g222"},
}


class TestPersonLookupReadThrough:
    def _patched(self, stack: ExitStack):
        stack.enter_context(patch.object(tool_dispatch, "_load_slack_asana_map",
                                         return_value=dict(_SLACK_MAP)))

    @pytest.mark.parametrize("name,expected", [
        ("Jennifer Mortensen", "U111"),
        ("Tommy Anderson", "U222"),
        ("nobody at all", None),
    ])
    def test_zero_behavior_change_across_levels(self, monkeypatch, name, expected):
        results = {}
        for level in ("off", "resolve"):
            monkeypatch.setenv("CORA_LEXICON", level)
            with ExitStack() as s:
                self._patched(s)
                results[level] = resolve_name_to_slack_user_id(name)
        assert results["off"] == results["resolve"]
        assert results["off"][0] == expected

    def test_telemetry_row_written_when_active(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CORA_LEXICON", "resolve")
        log_path = tmp_path / "res.jsonl"
        monkeypatch.setenv("LEXICON_RESOLUTIONS_PATH", str(log_path))
        with ExitStack() as s:
            self._patched(s)
            resolve_name_to_slack_user_id("Tommy Anderson", "F3E")
        row = json.loads(log_path.read_text(encoding="utf-8"))
        assert row["consumer"] == "person_lookup"
        assert row["status"] == "exact"
        assert row["canonical"] == "Tommy Anderson"

    def test_no_telemetry_when_off(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CORA_LEXICON", raising=False)
        log_path = tmp_path / "res.jsonl"
        monkeypatch.setenv("LEXICON_RESOLUTIONS_PATH", str(log_path))
        with ExitStack() as s:
            self._patched(s)
            resolve_name_to_slack_user_id("Tommy Anderson", "F3E")
        assert not log_path.exists()


# ── Asana task-op matcher: query expansion on miss only ─────────────────────


def _lex_fixture(tmp_path, monkeypatch):
    lex_dir = tmp_path / "lexicon"
    lex_dir.mkdir(exist_ok=True)
    (lex_dir / "f3e.yaml").write_text(
        "version: 1\nentity: F3E\nterms:\n"
        '  - {term: "the mood run", type: project, canonical: "F3E-RUN2-MOOD",'
        ' canonical_name: "Production Run 2 Mood leg"}\n'
        '  - {term: "the shared thing", type: project, canonical: "A1", canonical_name: "Ambi One"}\n'
        '  - {term: "the shared thing", type: project, canonical: "A2", canonical_name: "Ambi Two"}\n',
        encoding="utf-8")
    monkeypatch.setenv("LEXICON_DIR", str(lex_dir))
    monkeypatch.setenv("LEXICON_SKU_ALIASES_PATH", str(tmp_path / "no-skus.yaml"))
    monkeypatch.setenv("LEXICON_USER_ALIASES_PATH", str(tmp_path / "no-users.yaml"))
    lexicon.invalidate_cache()


_TASKS = [
    {"gid": "1001", "name": "Production Run 2 Mood leg kickoff", "completed": False},
    {"gid": "1002", "name": "the mood run retro notes", "completed": False},
    {"gid": "1003", "name": "Ambi One planning", "completed": False},
]


class TestAsanaQueryExpansion:
    def _resolve(self, task_name, tasks=None):
        from cora.tools import asana_client
        with ExitStack() as s:
            s.enter_context(patch.object(
                tool_dispatch, "_load_slack_asana_map",
                return_value={"U333": {"slack_user_id": "U333", "display_name": "X",
                                       "asana_user_gid": "g333"}}))
            s.enter_context(patch.object(asana_client, "get_user_tasks",
                                         return_value=list(_TASKS if tasks is None else tasks)))
            return tool_dispatch._resolve_asker_task("U333", "", task_name, "F3E")

    def test_direct_match_never_retargeted(self, tmp_path, monkeypatch):
        """A phrase that already matches a task keeps that task even though the
        lexicon knows the term (expansion is miss-only)."""
        _lex_fixture(tmp_path, monkeypatch)
        monkeypatch.setenv("CORA_LEXICON", "resolve")
        gid, label, err = self._resolve("the mood run retro")
        assert (gid, err) == ("1002", None)

    def test_miss_expands_via_canonical_name(self, tmp_path, monkeypatch):
        _lex_fixture(tmp_path, monkeypatch)
        monkeypatch.setenv("CORA_LEXICON", "resolve")
        tasks = [t for t in _TASKS if t["gid"] != "1002"]  # remove the direct match
        gid, label, err = self._resolve("the mood run", tasks=tasks)
        assert (gid, err) == ("1001", None)

    def test_off_level_no_expansion(self, tmp_path, monkeypatch):
        _lex_fixture(tmp_path, monkeypatch)
        monkeypatch.delenv("CORA_LEXICON", raising=False)
        tasks = [t for t in _TASKS if t["gid"] != "1002"]
        gid, label, err = self._resolve("the mood run", tasks=tasks)
        assert gid is None
        assert "No open task" in err

    def test_ambiguous_term_never_expands(self, tmp_path, monkeypatch):
        _lex_fixture(tmp_path, monkeypatch)
        monkeypatch.setenv("CORA_LEXICON", "resolve")
        gid, label, err = self._resolve("the shared thing")
        assert gid is None
        assert "No open task" in err  # plain miss; no guessing between A1/A2

    def test_expansion_ambiguity_still_asks(self, tmp_path, monkeypatch):
        """If the canonical_name matches SEVERAL tasks, the existing >1 ask fires
        (selection rules untouched)."""
        _lex_fixture(tmp_path, monkeypatch)
        monkeypatch.setenv("CORA_LEXICON", "resolve")
        tasks = [
            {"gid": "2001", "name": "Production Run 2 Mood leg kickoff", "completed": False},
            {"gid": "2002", "name": "Production Run 2 Mood leg wrap", "completed": False},
        ]
        gid, label, err = self._resolve("the mood run", tasks=tasks)
        assert gid is None
        assert "Several open tasks" in err
