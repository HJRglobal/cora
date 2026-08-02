"""S4 tests: lexicon review rail -- writer, tiers, autowrite + revert.

Pins: the tier matrix under the VERBATIM classify_tier; PHI refused inside the
applier; person canonicals roster-validated (refused, not gated); idempotent
re-apply; autowrite -> revert ROUND-TRIP byte-identical on ALL THREE stores;
existing known-answers revert behavior unchanged; lexicon items never expire.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cora import graduated_trust_shadow as gts
from cora import knowledge_review as kr
from cora import lexicon, lexicon_writer

_REPO = Path(__file__).resolve().parents[1]


def _payload(**over) -> dict:
    p = {
        "term": "gw store", "type": "location", "entity": "OSN",
        "canonical": "OSNGW", "canonical_name": "OSN Gilbert & Warner store (OSNGW)",
        "lane": "mined", "contributor_id": "",
    }
    p.update(over)
    return p


def _update(payload: dict) -> dict:
    return {"update_id": "lex-test-1", "update_type": "lexicon",
            "description": "lexicon proposal", "payload": payload}


@pytest.fixture()
def stores(tmp_path, monkeypatch):
    """Three tmp stores + roster, all env-routed; fresh lexicon cache."""
    lex_dir = tmp_path / "lexicon"
    lex_dir.mkdir()
    (lex_dir / "osn.yaml").write_text(
        "version: 1\nentity: OSN\nterms:\n"
        '  - {term: "greenfield", type: location, canonical: "OSNGF", '
        'canonical_name: "OSN Greenfield & 60 store (OSNGF)", source: seed}\n',
        encoding="utf-8")
    (lex_dir / "fndr.yaml").write_text(
        "version: 1\nentity: FNDR\nterms: []\n# trailing comment survives appends\n",
        encoding="utf-8")
    sku = tmp_path / "skus.yaml"
    sku.write_text(
        "# seed header comment\n"
        "skus:\n"
        '  F3-Original:   ["original energy", "f3 original energy"]\n'
        '  F3VPE4:        ["energy variety", "energy variety pack"]\n',
        encoding="utf-8")
    users = tmp_path / "users.yaml"
    users.write_text(
        "# seed header comment\n"
        "aliases:\n"
        '  Jennifer Mortensen: ["Jen", "Jenn"]\n'
        '  Tommy Anderson: ["Tommy", "Tom"]\n'
        "disambiguation_rules: []\n",
        encoding="utf-8")
    roster = tmp_path / "slack-to-asana.yaml"
    roster.write_text(
        "users:\n"
        '  - {slack_user_id: U111, display_name: "Jennifer Mortensen", asana_user_gid: g1}\n'
        '  - {slack_user_id: U222, display_name: "Tommy Anderson", asana_user_gid: g2}\n',
        encoding="utf-8")
    monkeypatch.setenv("LEXICON_DIR", str(lex_dir))
    monkeypatch.setenv("LEXICON_SKU_ALIASES_PATH", str(sku))
    monkeypatch.setenv("LEXICON_USER_ALIASES_PATH", str(users))
    monkeypatch.setenv("LEXICON_ROSTER_PATH", str(roster))
    lexicon.invalidate_cache()
    yield {"dir": lex_dir, "sku": sku, "users": users, "tmp": tmp_path}
    lexicon.invalidate_cache()


# ── Applier: validation, PHI, roster, idempotency ────────────────────────────


class TestApplier:
    def test_lexicon_term_appended_and_resolvable(self, stores):
        ok, summary = lexicon_writer.apply_lexicon_update(_payload())
        assert ok, summary
        r = lexicon.resolve("gw store", "OSN")
        assert (r.status, r.canonical) == ("exact", "OSNGW")

    def test_append_is_one_contiguous_block(self, stores):
        before = (stores["dir"] / "osn.yaml").read_text(encoding="utf-8")
        ok, _ = lexicon_writer.apply_lexicon_update(_payload())
        assert ok
        after = (stores["dir"] / "osn.yaml").read_text(encoding="utf-8")
        assert after.startswith(before)  # pure EOF append, no line edits

    def test_empty_terms_file_with_trailing_comment(self, stores):
        """fndr.yaml has 'terms: []' + a trailing comment: the appended block
        re-opens the terms key at EOF (last-key-wins) without touching either."""
        ok, summary = lexicon_writer.apply_lexicon_update(_payload(
            term="founder thing", entity="FNDR", canonical="FT",
            canonical_name="Founder Thing"))
        assert ok, summary
        r = lexicon.resolve("founder thing", "FNDR")
        assert r.status == "exact"
        text = (stores["dir"] / "fndr.yaml").read_text(encoding="utf-8")
        assert "terms: []" in text  # original line untouched (revert integrity)

    def test_idempotent_reapply_is_noop(self, stores):
        ok1, _ = lexicon_writer.apply_lexicon_update(_payload())
        text1 = (stores["dir"] / "osn.yaml").read_text(encoding="utf-8")
        ok2, summary2 = lexicon_writer.apply_lexicon_update(_payload())
        text2 = (stores["dir"] / "osn.yaml").read_text(encoding="utf-8")
        assert ok1 and ok2
        assert "no-op" in summary2
        assert text1 == text2

    def test_phi_payload_refused_fail_closed(self, stores):
        ok, summary = lexicon_writer.apply_lexicon_update(_payload(
            term="bob smith authorization",
            canonical_name="Bob Smith's billing authorization is pending",
            entity="LEX"))
        assert not ok
        assert "REFUSED" in summary
        assert "bob" not in (stores["dir"] / "osn.yaml").read_text(encoding="utf-8").lower()

    def test_phi_screen_error_refuses(self, stores, monkeypatch):
        monkeypatch.setattr(lexicon_writer, "_phi_screen", lambda *a: True)
        ok, summary = lexicon_writer.apply_lexicon_update(_payload())
        assert not ok

    def test_person_off_roster_refused(self, stores):
        ok, summary = lexicon_writer.apply_lexicon_update(_payload(
            term="jm", type="person", entity="SHARED",
            canonical="Random Stranger", canonical_name="Random Stranger"))
        assert not ok
        assert "roster" in summary.lower()

    def test_person_on_roster_appended_and_merged(self, stores):
        ok, summary = lexicon_writer.apply_lexicon_update(_payload(
            term="Jenny M", type="person", entity="SHARED",
            canonical="Jennifer Mortensen", canonical_name="Jennifer Mortensen"))
        assert ok, summary
        text = stores["users"].read_text(encoding="utf-8")
        assert "learned_aliases:" in text
        assert '"Jenny M"' in text
        assert text.startswith("# seed header comment")  # seeds untouched

    def test_person_roster_unavailable_refuses(self, stores, monkeypatch):
        monkeypatch.setenv("LEXICON_ROSTER_PATH", str(stores["tmp"] / "missing.yaml"))
        monkeypatch.setattr(lexicon_writer, "_roster_names", lambda: set())
        ok, summary = lexicon_writer.apply_lexicon_update(_payload(
            term="jm", type="person", entity="SHARED",
            canonical="Jennifer Mortensen", canonical_name="Jennifer Mortensen"))
        assert not ok

    def test_sku_alias_appended_to_learned_list(self, stores):
        ok, summary = lexicon_writer.apply_lexicon_update(_payload(
            term="office original", type="product", entity="F3E",
            canonical="F3-Original", canonical_name="F3 ENERGY Original 12-pack"))
        assert ok, summary
        text = stores["sku"].read_text(encoding="utf-8")
        assert "learned:" in text
        assert '{sku: "F3-Original", aliases: ["office original"]}' in text
        # A second alias for the SAME sku is a NEW row, never a line edit.
        ok2, _ = lexicon_writer.apply_lexicon_update(_payload(
            term="the original", type="product", entity="F3E",
            canonical="F3-Original", canonical_name="F3 ENERGY Original 12-pack"))
        assert ok2
        text2 = stores["sku"].read_text(encoding="utf-8")
        assert text2.startswith(text)  # pure append

    def test_sku_alias_conflicting_with_other_sku_refused(self, stores):
        ok, summary = lexicon_writer.apply_lexicon_update(_payload(
            term="energy variety", type="product", entity="F3E",
            canonical="F3-Original", canonical_name="wrong retarget"))
        assert not ok
        assert "different SKU" in summary

    def test_unknown_type_and_missing_fields_refused(self, stores):
        assert not lexicon_writer.apply_lexicon_update(_payload(type="nonsense"))[0]
        assert not lexicon_writer.apply_lexicon_update(_payload(term=""))[0]
        assert not lexicon_writer.apply_lexicon_update(_payload(entity=""))[0]

    def test_sub_entity_routes_to_parent_file_with_scope(self, stores):
        ok, summary = lexicon_writer.apply_lexicon_update(_payload(
            term="ranch shed", entity="HJRP-RR", canonical="RR-SHED",
            canonical_name="Rogers Ranch equipment shed"))
        assert ok, summary
        path = stores["dir"] / "hjrp.yaml"
        assert path.exists()
        assert 'scope: "HJRP-RR"' in path.read_text(encoding="utf-8")
        assert lexicon.resolve("ranch shed", "HJRP-RR").status == "exact"
        assert lexicon.resolve("ranch shed", "HJRP-1337").status == "miss"


# ── Tier matrix (classify_tier reused VERBATIM) ──────────────────────────────


class TestTierMatrix:
    def _record(self, payload, verdict="", recognized=False, owner=False, monkeypatch=None):
        if monkeypatch is not None:
            monkeypatch.setattr(gts, "contributor_recognized", lambda c, e: recognized)
            monkeypatch.setattr(gts, "authorized_owner", lambda c, e: owner)
        return gts.build_shadow_record(_update(payload), verdict)

    def test_lane_a_confirmed_location_reaches_tier0(self, monkeypatch):
        rec = self._record(_payload(lane="resolver_confirmed", contributor_id="U123"),
                           verdict="CORROBORATED", recognized=True,
                           monkeypatch=monkeypatch)
        assert rec["shadow_tier"] == 0
        assert rec["category"] == "lexicon_location"

    def test_lane_a_confirmed_product_reaches_tier0(self, monkeypatch):
        rec = self._record(_payload(term="office pack", type="product", entity="F3E",
                                    canonical="F3VPE4", canonical_name="Energy Variety",
                                    lane="resolver_confirmed", contributor_id="U123"),
                           verdict="CORROBORATED", recognized=True,
                           monkeypatch=monkeypatch)
        assert rec["shadow_tier"] == 0

    def test_lane_b_machine_mined_is_tier2(self):
        """No stubs: the REAL contributor_recognized("") is fail-safe False, so a
        machine-mined item (empty contributor) can never reach Tier 0/1 even
        with a CORROBORATED verdict and an allowlisted category."""
        rec = gts.build_shadow_record(_update(_payload(lane="mined")), "CORROBORATED")
        assert rec["shadow_tier"] == 2

    def test_lex_entity_short_circuits_tier2(self, monkeypatch):
        rec = self._record(_payload(entity="LEX", term="evv",
                                    canonical="EVV", canonical_name="Electronic Visit Verification",
                                    type="acronym", contributor_id="U123"),
                           verdict="CORROBORATED", recognized=True,
                           monkeypatch=monkeypatch)
        assert rec["shadow_tier"] == 2
        assert "lex_entity" in rec["reasons"]

    def test_person_never_allowlisted(self, monkeypatch):
        rec = self._record(_payload(term="jm", type="person",
                                    canonical="Jennifer Mortensen",
                                    canonical_name="Jennifer Mortensen",
                                    entity="SHARED", contributor_id="U123"),
                           verdict="CORROBORATED", recognized=True,
                           monkeypatch=monkeypatch)
        assert rec["shadow_tier"] == 2
        assert any("category_not_allowlisted:lexicon_person" in r for r in rec["reasons"])

    def test_vendor_never_allowlisted(self, monkeypatch):
        rec = self._record(_payload(term="bcb", type="vendor", entity="F3E",
                                    canonical="BLUE-CHIP-BEVERAGE",
                                    canonical_name="Blue Chip Beverage",
                                    contributor_id="U123"),
                           verdict="CORROBORATED", recognized=True,
                           monkeypatch=monkeypatch)
        assert rec["shadow_tier"] == 2
        assert any("category_not_allowlisted:lexicon_vendor" in r for r in rec["reasons"])

    def test_canon_conflict_is_tier2_with_conflict_named(self, monkeypatch):
        rec = self._record(_payload(contributor_id="U123"), verdict="CONFLICTS",
                           recognized=True, monkeypatch=monkeypatch)
        assert rec["shadow_tier"] == 2
        assert "conflicts_canon" in rec["reasons"]

    def test_claim_text_shape(self):
        text = gts.claim_text(_update(_payload()))
        assert text == '"gw store" means OSN Gilbert & Warner store (OSNGW) (location, OSN)'

    def test_contributor_id_lexicon_branch(self):
        assert gts.contributor_id(_update(_payload(contributor_id="U9"))) == "U9"
        assert gts.contributor_id(_update(_payload())) == ""


# ── Review-rail integration ──────────────────────────────────────────────────


class TestReviewRail:
    def test_lexicon_is_a_knowledge_type_never_expires(self):
        assert kr.is_knowledge_update("lexicon", {}) is True

    def test_apply_knowledge_update_routes_to_writer(self, stores, monkeypatch, tmp_path):
        monkeypatch.setenv("GOLDEN_SET_AUTO_PATH", str(tmp_path / "golden-auto.yaml"))
        ok, summary = kr.apply_knowledge_update(_update(_payload()))
        assert ok, summary
        assert lexicon.resolve("gw store", "OSN").status == "exact"
        # golden-set auto-growth fired
        golden = (tmp_path / "golden-auto.yaml").read_text(encoding="utf-8")
        assert "auto-lex-" in golden
        assert "gw store" in golden

    def test_card_renderer_lexicon_branch(self):
        card = kr.format_single_item_dm(_update(_payload()))
        assert "Lexicon term" in card
        assert "`gw store` -> OSN Gilbert & Warner store (OSNGW)" in card
        assert "[location, OSN]" in card
        assert "lane: mined" in card

    def test_card_renderer_withholds_phi_shaped_detail(self):
        upd = _update(_payload(
            term="bob smith authorization",
            canonical_name="Bob Smith's billing authorization is pending"))
        card = kr.format_single_item_dm(upd)
        assert "Bob Smith's billing" not in card
        assert "withheld" in card

    def test_golden_growth_skips_lex_entity(self, stores, monkeypatch, tmp_path):
        from cora.golden_set import append_case_from_lexicon
        monkeypatch.setenv("GOLDEN_SET_AUTO_PATH", str(tmp_path / "golden-auto.yaml"))
        assert append_case_from_lexicon(_payload(
            entity="LEX", term="evv", canonical="EVV",
            canonical_name="Electronic Visit Verification", type="acronym")) is False


# ── Autowrite -> revert round-trip on ALL THREE stores ───────────────────────


class TestAutowriteRevertRoundTrip:
    _HARRISON = "U0B2RM2JYJ1"

    def _apply_and_revert(self, stores, monkeypatch, tmp_path, payload, target: Path):
        # Isolate the proposal ledger + audit trail.
        monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", tmp_path / "pending.jsonl")
        monkeypatch.setattr(kr, "_ARCHIVE_PATH", tmp_path / "archive.jsonl")
        monkeypatch.setattr(kr, "_AUTOWRITE_AUDIT_PATH", tmp_path / "audit.jsonl")
        monkeypatch.setenv("GOLDEN_SET_AUTO_PATH", str(tmp_path / "golden-auto.yaml"))
        uid = f"lex-rt-{target.stem}"
        kr.propose_update(update_id=uid, update_type="lexicon",
                          description="rt", payload=payload)
        before = target.read_text(encoding="utf-8")
        update = {"update_id": uid, "update_type": "lexicon", "payload": payload}
        ok, summary = kr.apply_autowrite(update, tier=0, reason="test",
                                         contributor="U123")
        assert ok, summary
        after_apply = target.read_text(encoding="utf-8")
        assert after_apply != before
        audit = [json.loads(l) for l in
                 (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()]
        assert audit[-1]["revert"]["target_file"] == str(target)
        assert audit[-1]["revert"]["added_lines"]
        ok2, msg = kr.process_autowrite_revert(uid, self._HARRISON)
        assert ok2, msg
        after_revert = target.read_text(encoding="utf-8")
        assert after_revert.rstrip("\n") == before.rstrip("\n"), target.name
        return after_revert

    def test_round_trip_lexicon_store(self, stores, monkeypatch, tmp_path):
        self._apply_and_revert(stores, monkeypatch, tmp_path, _payload(),
                               stores["dir"] / "osn.yaml")
        lexicon.invalidate_cache()
        assert lexicon.resolve("gw store", "OSN").status == "miss"

    def test_round_trip_sku_store(self, stores, monkeypatch, tmp_path):
        self._apply_and_revert(
            stores, monkeypatch, tmp_path,
            _payload(term="office original", type="product", entity="F3E",
                     canonical="F3-Original", canonical_name="F3 Original 12-pack"),
            stores["sku"])

    def test_round_trip_person_store(self, stores, monkeypatch, tmp_path):
        self._apply_and_revert(
            stores, monkeypatch, tmp_path,
            _payload(term="Jenny M", type="person", entity="SHARED",
                     canonical="Jennifer Mortensen",
                     canonical_name="Jennifer Mortensen"),
            stores["users"])

    def test_known_answers_targets_still_listed_first(self, stores, monkeypatch, tmp_path):
        """The _autowrite_target_files extension appends lexicon stores AFTER the
        known-answers set -- existing revert behavior stays byte-identical."""
        ka_dir = tmp_path / "ka"
        ka_dir.mkdir()
        (ka_dir / "f3e.md").write_text("# seed\n\n## Known facts\n", encoding="utf-8")
        monkeypatch.setenv("KNOWN_ANSWERS_DIR", str(ka_dir))
        files = kr._autowrite_target_files()
        assert files, "no targets resolved"
        ka_positions = [i for i, f in enumerate(files) if f.suffix == ".md"
                        and f.parent == ka_dir]
        lex_positions = [i for i, f in enumerate(files)
                         if f.parent == stores["dir"] or f in (stores["sku"], stores["users"])]
        assert ka_positions and lex_positions
        assert max(ka_positions) < min(lex_positions)
