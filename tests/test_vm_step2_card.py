"""VM step-2 T0 card + the D2 decision-card TEMPLATE (DR/VM step 1, slice M4; charter C5 / D2).

Contract under test:
  * the cost table has ONE quote per shortlisted provider, every row carrying a BAA column,
    total figures for 64 and 128 GB, at least one public source URL and the access date;
  * every row's band verdict is computed, not typed, and the header count of out-of-band
    rows equals the computed count;
  * the card is PROPOSE-ONLY: no actions block, no button, no accessory, no interactive
    element -- assert_propose_only refuses one when injected;
  * the D2 template renders BOTH mandatory inputs (BAA status per provider; Emily weighed in
    yes/no + date) plus the PHI-bytes line and the sign-off line, and REFUSES to render when
    either mandatory input is missing;
  * --apply is the only posting path and it is not taken on a dry run (post() is never called).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import stage_vm_step2_card as card  # noqa: E402


class TestCostTable:
    def test_one_quote_per_provider_with_baa_sources_and_date(self):
        names = [q["provider"] for q in card.PROVIDER_QUOTES]
        assert len(names) == len(set(names)) >= 4
        assert any("Azure" in n for n in names) and any("Amazon" in n for n in names) and any("Google" in n for n in names)
        for q in card.PROVIDER_QUOTES:
            assert q["baa"].strip() and q["baa_url"].startswith("https://"), q["provider"]
            assert q["total_64_usd"] > 0 and q["total_128_usd"] > q["total_64_usd"], q["provider"]
            assert q["sources"] and all(s.startswith("https://") for s in q["sources"]), q["provider"]
            assert q["in_band_path"].strip() and q["verification"].strip(), q["provider"]
            # totals add up from the components (VM + storage + backup), within rounding
            assert abs((q["vm_64_usd"] + q["storage_usd"] + q["backup_usd"]) - q["total_64_usd"]) < 0.02, q["provider"]
            storage_128 = q.get("storage_128_usd", q["storage_usd"])   # a plan may need a top-up on one row only
            assert abs((q["vm_128_usd"] + storage_128 + q["backup_usd"]) - q["total_128_usd"]) < 0.02, q["provider"]
        assert re.match(r"\d{4}-\d{2}-\d{2}", card.ACCESSED)

    def test_band_verdict_is_computed(self):
        assert card.in_band(450) and not card.in_band(299.99) and not card.in_band(600.01)
        text = card.render_cost_table_text()
        for q in card.PROVIDER_QUOTES:
            assert q["provider"] in text and "*BAA:*" in text
            expected = "in band" if card.in_band(q["total_64_usd"]) else "OUTSIDE band"
            assert expected in text
        fallback, blocks = card.build_card()
        header = blocks[0]["text"]["text"]
        outside = sum(1 for q in card.PROVIDER_QUOTES if not card.in_band(q["total_64_usd"]))
        assert f"({outside}/{len(card.PROVIDER_QUOTES)})" in header
        assert "Justin" in " ".join(b.get("text", {}).get("text", "") for b in blocks if b.get("type") == "section")


class TestProposeOnly:
    def test_card_has_no_interactive_element(self):
        _, blocks = card.build_card()
        card.assert_propose_only(blocks)          # does not raise
        types = {b["type"] for b in blocks}
        assert "actions" not in types and not any("accessory" in b for b in blocks)
        assert not re.search(r"\"type\":\s*\"button\"", str(blocks))

    def test_injected_button_is_refused(self):
        _, blocks = card.build_card()
        blocks.append({"type": "actions", "elements": [{"type": "button", "text": {"type": "plain_text", "text": "Provision"}, "action_id": "provision"}]})
        with pytest.raises(ValueError):
            card.assert_propose_only(blocks)
        _, blocks2 = card.build_card()
        blocks2[0]["accessory"] = {"type": "button", "text": {"type": "plain_text", "text": "Buy"}, "action_id": "buy"}
        with pytest.raises(ValueError):
            card.assert_propose_only(blocks2)

    def test_dry_run_never_posts(self, monkeypatch, capsys):
        called = []
        monkeypatch.setattr(card, "post", lambda blocks, fallback: called.append(1) or True)
        assert card.main([]) == 0
        assert called == []
        out = capsys.readouterr().out
        assert "DRY-RUN" in out and "nothing posted" in out


class TestD2Template:
    def test_renders_both_mandatory_inputs(self):
        _, blocks = card.render_d2_card(
            baa_status_per_provider={"Microsoft Azure": "signed via DPA", "Amazon Web Services": "accepted in Artifact"},
            emily_weighed_in="yes", emily_date="2026-10-01", phi_bytes_proposed="LEX KB partition, ~2.1 GB", target_provider="Microsoft Azure")
        text = blocks[0]["text"]["text"]
        for field in card.D2_MANDATORY_FIELDS:
            assert field in text, field
        assert "Microsoft Azure: signed via DPA" in text and "yes (2026-10-01)" in text
        assert "LEX KB partition, ~2.1 GB" in text and "pending" in text
        card.assert_propose_only(blocks)

    def test_refuses_when_a_mandatory_input_is_missing(self):
        with pytest.raises(ValueError):
            card.render_d2_card(baa_status_per_provider={}, emily_weighed_in="yes", emily_date="2026-10-01",
                                phi_bytes_proposed="x", target_provider="p")
        with pytest.raises(ValueError):
            card.render_d2_card(baa_status_per_provider={"p": "s"}, emily_weighed_in="maybe", emily_date="",
                                phi_bytes_proposed="x", target_provider="p")
        with pytest.raises(ValueError):
            card.render_d2_card(baa_status_per_provider={"p": "s"}, emily_weighed_in="yes", emily_date="",
                                phi_bytes_proposed="x", target_provider="p")
        with pytest.raises(ValueError):
            card.render_d2_card(baa_status_per_provider={"p": "s"}, emily_weighed_in="no", emily_date="",
                                phi_bytes_proposed="", target_provider="p")

    def test_no_emily_input_is_shown_as_required_not_hidden(self):
        _, blocks = card.render_d2_card(baa_status_per_provider={"p": "s"}, emily_weighed_in="no", emily_date="",
                                        phi_bytes_proposed="none", target_provider="p")
        assert "REQUIRED before sign-off" in blocks[0]["text"]["text"]
