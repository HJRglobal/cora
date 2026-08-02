"""Tests for src/cora/lexicon.py -- the company-lexicon resolver (S1).

Pins the design locks: one resolver over three stores; exact resolves /
ambiguous asks / fuzzy only suggests; ADDITIVE invariant (miss or load failure
== today's behavior); entity scoping with parent collapse + FNDR union; LEX
telemetry hashing; prompt-block caps + LEX GM-only injection.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from cora import lexicon
from cora.lexicon import LexEntry, Resolution  # noqa: F401 (import shape pin)

_REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _fresh_registry():
    lexicon.invalidate_cache()
    yield
    lexicon.invalidate_cache()


@pytest.fixture()
def fixture_dir(tmp_path, monkeypatch):
    """Point every lexicon store at tmp fixtures."""
    lex_dir = tmp_path / "lexicon"
    lex_dir.mkdir()
    monkeypatch.setenv("LEXICON_DIR", str(lex_dir))
    monkeypatch.setenv("LEXICON_SKU_ALIASES_PATH", str(tmp_path / "skus.yaml"))
    monkeypatch.setenv("LEXICON_USER_ALIASES_PATH", str(tmp_path / "users.yaml"))
    monkeypatch.setenv("LEXICON_RESOLUTIONS_PATH", str(tmp_path / "resolutions.jsonl"))
    lexicon.invalidate_cache()
    return tmp_path


def _write(p: Path, text: str) -> None:
    p.write_text(text, encoding="utf-8")


# ── Normalization parity ─────────────────────────────────────────────────────


class TestNormParity:
    def test_mirrors_tool_dispatch_norm_alias(self):
        """norm_term MUST behave exactly like tool_dispatch._norm_alias (the
        design lock: mirror, do not fork). If _norm_alias ever changes, this
        breaks loudly and the mirror must be updated in the same commit."""
        from cora.tools.tool_dispatch import _norm_alias
        probes = [
            "", "  ", "BCB", "12-pack", "s&c", "Strawberries & Cream",
            "run-2", "the  Gilbert   store!", "piña colada", "Val Vista & Pecos",
            "a_b.c/d", "UPPER lower MiXeD", "  trailing  ", "&", "12 pack",
        ]
        for p in probes:
            assert lexicon.norm_term(p) == _norm_alias(p), p


# ── Loader ───────────────────────────────────────────────────────────────────


class TestLoader:
    def test_malformed_entry_skipped_rest_load(self, fixture_dir):
        _write(fixture_dir / "lexicon" / "f3e.yaml", (
            "version: 1\nentity: F3E\nterms:\n"
            '  - {term: "good", type: vendor, canonical: "GOOD", canonical_name: "Good Vendor"}\n'
            "  - not-a-mapping\n"
            '  - {term: "", type: vendor, canonical: "X", canonical_name: "X"}\n'
            '  - {term: "badtype", type: nonsense, canonical: "X", canonical_name: "X"}\n'
            '  - {term: "nocanon", type: vendor, canonical: "", canonical_name: "X"}\n'
        ))
        assert lexicon.resolve("good", "F3E").status == "exact"
        assert lexicon.resolve("badtype", "F3E").status == "miss"

    def test_malformed_file_does_not_blank_others(self, fixture_dir):
        _write(fixture_dir / "lexicon" / "bad.yaml", "{{{ not yaml")
        _write(fixture_dir / "lexicon" / "osn.yaml", (
            "version: 1\nentity: OSN\nterms:\n"
            '  - {term: "greenfield", type: location, canonical: "OSNGF", canonical_name: "Greenfield store"}\n'
        ))
        assert lexicon.resolve("greenfield", "OSN").status == "exact"

    def test_keep_last_good_on_parse_error(self, fixture_dir, monkeypatch):
        path = fixture_dir / "lexicon" / "osn.yaml"
        _write(path, (
            "version: 1\nentity: OSN\nterms:\n"
            '  - {term: "greenfield", type: location, canonical: "OSNGF", canonical_name: "Greenfield store"}\n'
        ))
        assert lexicon.resolve("greenfield", "OSN").status == "exact"
        # Corrupt the file, force TTL expiry: the last good registry survives.
        monkeypatch.setattr(lexicon, "_TTL_SECONDS", 0.0)
        _write(path, "{{{ corrupted")
        assert lexicon.resolve("greenfield", "OSN").status == "exact"

    def test_unknown_entity_fail_closed_empty(self, fixture_dir):
        _write(fixture_dir / "lexicon" / "_shared.yaml", (
            "version: 1\nentity: SHARED\nterms:\n"
            '  - {term: "the holdco", type: acronym, canonical: "HJRG", canonical_name: "HJR Global"}\n'
        ))
        assert lexicon.resolve("the holdco", "NOT-AN-ENTITY").status == "miss"

    def test_empty_terms_list_tolerated(self, fixture_dir):
        _write(fixture_dir / "lexicon" / "fndr.yaml", "version: 1\nentity: FNDR\nterms: []\n")
        _write(fixture_dir / "lexicon" / "empty.yaml", "version: 1\nentity: BDM\nterms:\n")
        assert lexicon.resolve("anything", "BDM").status == "miss"

    def test_sku_store_loaded_as_f3e_product_entries(self, fixture_dir):
        _write(fixture_dir / "skus.yaml", (
            "skus:\n"
            '  F3-Original: ["original energy", "f3 original energy"]\n'
            "learned:\n"
            '  F3SL: ["office strawberry"]\n'
        ))
        r = lexicon.resolve("original energy", "F3E")
        assert (r.status, r.canonical, r.type) == ("exact", "F3-Original", "product")
        r = lexicon.resolve("office strawberry", "F3E")
        assert (r.status, r.canonical) == ("exact", "F3SL")

    def test_person_store_loaded_org_wide_exact_only(self, fixture_dir):
        _write(fixture_dir / "users.yaml", (
            "aliases:\n"
            '  Jennifer Mortensen: ["Jen", "Jenn"]\n'
            "learned_aliases:\n"
            '  Jennifer Mortensen: ["Jenny M"]\n'
        ))
        r = lexicon.resolve("jen", "OSN", types=["person"])
        assert (r.status, r.canonical) == ("exact", "Jennifer Mortensen")
        assert lexicon.resolve("jenny m", "F3E").status == "exact"
        # No prefix/substring loosening: exact normalized surfaces only.
        assert lexicon.resolve("jennif", "OSN").status in ("miss", "suggestion")


# ── Resolution ladder ────────────────────────────────────────────────────────


class TestResolutionLadder:
    def test_exact_via_term_alias_and_punctuation(self):
        assert lexicon.resolve("bcb", "F3E").canonical == "BLUE-CHIP-BEVERAGE"
        assert lexicon.resolve("Blue Chip", "F3E").canonical == "BLUE-CHIP-BEVERAGE"
        assert lexicon.resolve("  BCB!! ", "F3E").canonical == "BLUE-CHIP-BEVERAGE"

    def test_ambiguous_asks_never_guesses(self):
        r = lexicon.resolve("the gilbert store", "OSN")
        assert r.status == "ambiguous"
        assert r.canonical == ""  # no silent pick, ever
        assert {c.canonical for c in r.candidates} == {"OSNGW", "OSNGM"}

    def test_seeded_variety_pack_is_three_way_ambiguous(self):
        r = lexicon.resolve("variety pack", "F3E")
        assert r.status == "ambiguous"
        assert {c.canonical for c in r.candidates} == {"F3VPE4", "F3VPM4", "F3-PureE-V4F"}

    def test_fuzzy_is_suggestion_only(self):
        r = lexicon.resolve("gilbert warner", "OSN")
        assert r.status == "suggestion"
        assert r.canonical == ""  # a suggestion NEVER auto-applies
        assert r.suggestion

    def test_gibberish_is_a_strict_miss(self):
        r = lexicon.resolve("zzqx flurble", "F3E")
        assert (r.status, r.canonical, r.suggestion) == ("miss", "", "")

    def test_types_filter(self):
        assert lexicon.resolve("bcb", "F3E", types=["location"]).status == "miss"
        assert lexicon.resolve("bcb", "F3E", types=["vendor"]).status == "exact"

    def test_same_canonical_duplicate_entries_dedupe_to_exact(self, fixture_dir):
        _write(fixture_dir / "lexicon" / "osn.yaml", (
            "version: 1\nentity: OSN\nterms:\n"
            '  - {term: "gw", type: location, canonical: "OSNGW", canonical_name: "GW store"}\n'
            '  - {term: "gw", type: location, canonical: "OSNGW", canonical_name: "GW store dup"}\n'
        ))
        assert lexicon.resolve("gw", "OSN").status == "exact"


# ── Entity scoping ───────────────────────────────────────────────────────────


class TestEntityScoping:
    def test_parent_collapse_stores_and_properties(self):
        assert lexicon.resolve("gilbert and warner", "OSNGW").canonical == "OSNGW"
        assert lexicon.resolve("the ranch", "HJRP-1337").canonical == "HJRP-RR"
        assert lexicon.resolve("ddd", "LEX-LLC").canonical == "AZ-DDD"

    def test_scope_filter_on_sub_entity(self, fixture_dir):
        _write(fixture_dir / "lexicon" / "lex.yaml", (
            "version: 1\nentity: LEX\nterms:\n"
            '  - {term: "llc only", type: process, canonical: "LLC-X", canonical_name: "LLC thing", scope: "LEX-LLC"}\n'
            '  - {term: "all lex", type: process, canonical: "LEX-Y", canonical_name: "LEX-wide thing"}\n'
        ))
        assert lexicon.resolve("llc only", "LEX-LLC").status == "exact"
        assert lexicon.resolve("llc only", "LEX-LTS").status == "miss"
        # GM level (LEX) sees everything, unfiltered.
        assert lexicon.resolve("llc only", "LEX").status == "exact"
        assert lexicon.resolve("all lex", "LEX-LTS").status == "exact"

    def test_fndr_and_hjrg_union(self):
        assert lexicon.resolve("bcb", "FNDR").canonical == "BLUE-CHIP-BEVERAGE"
        assert lexicon.resolve("gw store", "HJRG").canonical == "OSNGW"
        assert lexicon.resolve("ddd", "FNDR").canonical == "AZ-DDD"

    def test_shared_terms_resolve_everywhere(self):
        assert lexicon.resolve("the holdco", "OSN").canonical == "HJRG"
        assert lexicon.resolve("founder os", "HJRP").canonical == "HJR-FOUNDER-OS"

    def test_homonym_isolation_across_entities(self):
        """F3E 'vine' (Amazon Vine) vs HJRP 'vine and branches' (former tenant);
        OSN 'gm store' never resolves in LEX scope."""
        assert lexicon.resolve("vine", "F3E").canonical == "AMAZON-VINE"
        assert lexicon.resolve("vine", "HJRP").status == "miss"
        assert lexicon.resolve("vine and branches", "F3E").status == "miss"
        assert lexicon.resolve("vine and branches", "HJRP").canonical == "VINE-AND-BRANCHES"
        assert lexicon.resolve("gm store", "LEX").status == "miss"
        assert lexicon.resolve("bcb", "OSN").status == "miss"


# ── ADDITIVE invariant ───────────────────────────────────────────────────────


class TestAdditiveInvariant:
    def test_load_failure_degrades_to_miss_never_raises(self, monkeypatch):
        monkeypatch.setenv("LEXICON_DIR", "Z:/does/not/exist")
        monkeypatch.setenv("LEXICON_SKU_ALIASES_PATH", "Z:/nope.yaml")
        monkeypatch.setenv("LEXICON_USER_ALIASES_PATH", "Z:/nope2.yaml")
        lexicon.invalidate_cache()
        r = lexicon.resolve("bcb", "F3E")
        assert r.status == "miss"

    def test_registry_exception_degrades_to_miss(self, monkeypatch):
        def _boom(entity, scope):
            raise RuntimeError("synthetic registry failure")
        monkeypatch.setattr(lexicon, "_entries_for", _boom)
        assert lexicon.resolve("bcb", "F3E").status == "miss"

    def test_empty_query_is_a_miss(self):
        assert lexicon.resolve("", "F3E").status == "miss"
        assert lexicon.resolve("   ", "F3E").status == "miss"


# ── Flag ─────────────────────────────────────────────────────────────────────


class TestLexiconLevel:
    def test_default_off_and_invalid_fails_closed(self, monkeypatch):
        monkeypatch.delenv("CORA_LEXICON", raising=False)
        assert lexicon.lexicon_level() == "off"
        monkeypatch.setenv("CORA_LEXICON", "banana")
        assert lexicon.lexicon_level() == "off"

    def test_valid_levels(self, monkeypatch):
        for v in ("off", "resolve", "full"):
            monkeypatch.setenv("CORA_LEXICON", v.upper())
            assert lexicon.lexicon_level() == v


# ── Telemetry ────────────────────────────────────────────────────────────────


class TestTelemetry:
    def test_consumer_resolve_logs_a_row(self, fixture_dir):
        _write(fixture_dir / "lexicon" / "osn.yaml", (
            "version: 1\nentity: OSN\nterms:\n"
            '  - {term: "greenfield", type: location, canonical: "OSNGF", canonical_name: "Greenfield store"}\n'
        ))
        lexicon.resolve("greenfield", "OSN", consumer="test_consumer",
                        channel="osn-leadership", user="U123")
        rows = [json.loads(l) for l in
                (fixture_dir / "resolutions.jsonl").read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 1
        row = rows[0]
        assert row["status"] == "exact"
        assert row["canonical"] == "OSNGF"
        assert row["consumer"] == "test_consumer"
        assert row["query_display"] == "greenfield"
        assert len(row["query_hash"]) == 64

    def test_no_consumer_no_telemetry(self, fixture_dir):
        lexicon.resolve("anything", "OSN")
        assert not (fixture_dir / "resolutions.jsonl").exists()

    def test_lex_phi_shaped_query_withheld_but_hashed(self, fixture_dir):
        lexicon.log_event(entity="LEX-LLC", status="miss",
                          query="Bob Smith's billing authorization status",
                          consumer="test")
        row = json.loads((fixture_dir / "resolutions.jsonl").read_text(encoding="utf-8"))
        assert row["query_display"] == "[withheld]"
        assert len(row["query_hash"]) == 64

    def test_lex_clean_query_keeps_display(self, fixture_dir):
        lexicon.log_event(entity="LEX", status="exact", query="evv", consumer="test")
        row = json.loads((fixture_dir / "resolutions.jsonl").read_text(encoding="utf-8"))
        assert row["query_display"] == "evv"

    def test_non_lex_query_stored_plain(self, fixture_dir):
        lexicon.log_event(entity="F3E", status="miss", query="some plain phrase",
                          consumer="test")
        row = json.loads((fixture_dir / "resolutions.jsonl").read_text(encoding="utf-8"))
        assert row["query_display"] == "some plain phrase"

    def test_telemetry_failure_never_raises(self, fixture_dir, monkeypatch):
        blocker = fixture_dir / "blocker"
        blocker.write_text("a plain file where a directory must go", encoding="utf-8")
        monkeypatch.setenv("LEXICON_RESOLUTIONS_PATH", str(blocker / "x.jsonl"))
        # mkdir(parents=True) fails on the file-in-the-way; log_event must not raise.
        lexicon.log_event(entity="F3E", status="miss", query="x", consumer="t")


# ── Prompt block ─────────────────────────────────────────────────────────────


class TestFormatLexiconContext:
    def test_lex_gm_only_never_sub_entity(self):
        assert lexicon.format_lexicon_context("LEX") != ""
        for sub in ("LEX-LLC", "LEX-LTS", "LEX-LBHS", "LEX-LLA"):
            assert lexicon.format_lexicon_context(sub) == ""

    def test_store_channels_get_no_block(self):
        for ent in ("OSNGW", "OSNGM", "HJRP-RR", "F3"):
            assert lexicon.format_lexicon_context(ent) == ""

    def test_unknown_entity_no_block(self):
        assert lexicon.format_lexicon_context("NOPE") == ""

    def test_block_shape_and_rules(self):
        blk = lexicon.format_lexicon_context("F3E")
        assert blk.startswith("## Company lexicon")
        assert '"bcb"' in blk
        assert "never overrides Known Answers" in blk
        assert "ask which one is meant -- never guess" in blk

    def test_ambiguous_terms_rendered_as_ask(self):
        blk = lexicon.format_lexicon_context("OSN")
        assert "AMBIGUOUS: ask" in blk
        assert "OSNGW" in blk and "OSNGM" in blk

    def test_caps_enforced(self, fixture_dir):
        terms = "\n".join(
            f'  - {{term: "term number {i}", type: process, canonical: "C{i}", '
            f'canonical_name: "Canonical thing number {i}"}}'
            for i in range(60)
        )
        _write(fixture_dir / "lexicon" / "f3e.yaml",
               f"version: 1\nentity: F3E\nterms:\n{terms}\n")
        blk = lexicon.format_lexicon_context("F3E")
        assert len(blk) <= lexicon.MAX_BLOCK_CHARS
        n_lines = sum(1 for l in blk.splitlines() if l.startswith('- "'))
        assert n_lines <= lexicon.MAX_BLOCK_TERMS
        assert "more -- the resolver knows them all" in blk

    def test_phi_shaped_line_screened_at_render(self, fixture_dir):
        _write(fixture_dir / "lexicon" / "lex.yaml", (
            "version: 1\nentity: LEX\nterms:\n"
            '  - {term: "evv", type: acronym, canonical: "EVV", canonical_name: "Electronic Visit Verification"}\n'
            "  - {term: \"bad entry\", type: process, canonical: \"X\","
            " canonical_name: \"Bob Smith's billing authorization is pending\"}\n"
        ))
        blk = lexicon.format_lexicon_context("LEX")
        assert "evv" in blk
        assert "Bob Smith" not in blk

    def test_person_entries_stay_out_of_block(self, fixture_dir):
        _write(fixture_dir / "users.yaml", 'aliases:\n  Harrison Rogers: ["H"]\n')
        _write(fixture_dir / "lexicon" / "f3e.yaml", (
            "version: 1\nentity: F3E\nterms:\n"
            '  - {term: "bcb", type: vendor, canonical: "B", canonical_name: "Blue Chip"}\n'
        ))
        blk = lexicon.format_lexicon_context("F3E")
        assert "Harrison Rogers" not in blk

    def test_usage_ranking_prefers_hot_terms(self, fixture_dir):
        terms = "\n".join(
            f'  - {{term: "filler {i}", type: process, canonical: "F{i}", '
            f'canonical_name: "Filler {i}"}}' for i in range(45)
        )
        _write(fixture_dir / "lexicon" / "f3e.yaml", (
            f"version: 1\nentity: F3E\nterms:\n{terms}\n"
            '  - {term: "zzz hot term", type: vendor, canonical: "HOT", canonical_name: "Hot Vendor"}\n'
        ))
        now = int(time.time())
        with (fixture_dir / "resolutions.jsonl").open("a", encoding="utf-8") as fh:
            for _ in range(5):
                fh.write(json.dumps({"ts": now, "entity": "F3E", "status": "exact",
                                     "canonical": "HOT"}) + "\n")
        blk = lexicon.format_lexicon_context("F3E")
        assert "zzz hot term" in blk  # ranked in despite sorting last alphabetically


# ── Seed hygiene ─────────────────────────────────────────────────────────────


class TestSeedHygiene:
    _SEED_FILES = [
        "_shared.yaml", "f3e.yaml", "osn.yaml", "lex.yaml", "ufl.yaml", "hjrp.yaml",
        "hjrprod.yaml", "bdm.yaml", "f3c.yaml", "hjrg.yaml", "fndr.yaml",
    ]

    def test_all_seed_files_present_and_parse(self):
        lex_dir = _REPO / "data" / "maps" / "lexicon"
        for name in self._SEED_FILES:
            assert (lex_dir / name).exists(), name
            entries = lexicon._parse_lexicon_file(lex_dir / name)
            assert isinstance(entries, list)

    # The AHCCCS entry (bare AZ-Medicaid agency acronym + its official agency
    # name) trips the precision-biased is_phi_risk by construction -- the
    # D-046 posture: naming the program agency is not PHI, but the detector
    # flags program keywords. Exempt ONLY that entry here; the render screen
    # still drops its line from the LEX prompt block fail-closed (pinned in
    # test_ahcccs_line_screened_from_lex_block), so it never rides an egress
    # surface unscreened. No individual is referenced anywhere in the entry.
    _PROGRAM_KEYWORD_EXEMPT_CANONICALS = frozenset({"AHCCCS"})

    def test_every_seed_field_is_phi_clean(self):
        from cora.phi_guard import is_any_phi
        lex_dir = _REPO / "data" / "maps" / "lexicon"
        for name in self._SEED_FILES:
            for e in lexicon._parse_lexicon_file(lex_dir / name):
                if e.canonical in self._PROGRAM_KEYWORD_EXEMPT_CANONICALS:
                    continue
                for text in (e.term, e.canonical, e.canonical_name, e.notes, *e.aliases):
                    assert not is_any_phi(text), f"{name}: {text!r}"

    def test_ahcccs_line_screened_from_lex_block(self):
        """The AHCCCS seed resolves programmatically, but its prompt-block line
        trips the render screen and MUST be withheld from the LEX block."""
        assert lexicon.resolve("ahcccs", "LEX").status == "exact"
        blk = lexicon.format_lexicon_context("LEX")
        assert "ahcccs" not in blk.lower()

    def test_lex_seeds_are_staff_ops_terms_only(self):
        lex_dir = _REPO / "data" / "maps" / "lexicon"
        entries = lexicon._parse_lexicon_file(lex_dir / "lex.yaml")
        assert entries, "lex.yaml seeds missing"
        for e in entries:
            assert e.type in ("acronym", "process"), (
                f"LEX seed {e.term!r} has type {e.type!r} -- staff/ops terms only")
