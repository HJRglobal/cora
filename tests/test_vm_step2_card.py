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

    def test_band_verdict_is_computed_per_row_and_the_headline_is_derived(self, monkeypatch):
        assert card.in_band(450) and not card.in_band(299.99) and not card.in_band(600.01)
        for q in card.PROVIDER_QUOTES:
            row = card.render_provider_row(q)
            assert row.startswith(f"- *{q['provider']}*") and "*BAA:*" in row and q["baa"] in row
            v64 = "in band" if card.in_band(q["total_64_usd"]) else "OUTSIDE band"
            v128 = "in band" if card.in_band(q["total_128_usd"]) else "OUTSIDE band"
            assert f"*${q['total_64_usd']:,.0f}/mo* ({v64})" in row and f"*${q['total_128_usd']:,.0f}/mo* ({v128})" in row
            if q.get("storage_128_usd", q["storage_usd"]) != q["storage_usd"]:
                assert f"storage ${q['storage_usd']:,.0f} (64 GB) / ${q['storage_128_usd']:,.0f} (128 GB)" in row
        _, blocks = card.build_card()
        header = blocks[0]["text"]["text"]
        outside = sum(1 for q in card.PROVIDER_QUOTES if not card.in_band(q["total_64_usd"]))
        total = len(card.PROVIDER_QUOTES)
        assert f"({outside}/{total})" in header
        assert ("Every one of the" in header) == (outside == total)
        assert "Justin" in " ".join(b.get("text", {}).get("text", "") for b in blocks if b.get("type") == "section")
        # a quote correction that puts one provider in band flips the derived phrase, never a typed sentence
        monkeypatch.setitem(card.PROVIDER_QUOTES[0], "total_64_usd", 590.0)
        assert "of the" in card.band_headline() and "Every one of the" not in card.band_headline()
        assert f"({outside - 1}/{total})" in card.band_headline()

    def test_one_section_per_provider_and_no_section_over_slacks_limit(self):
        _, blocks = card.build_card()
        sections = [b["text"]["text"] for b in blocks if b.get("type") == "section"]
        for q in card.PROVIDER_QUOTES:
            own = [s for s in sections if s.startswith(f"- *{q['provider']}*")]
            assert len(own) == 1 and q["baa"] in own[0], q["provider"]      # the BAA column survives on ITS row
        assert all(len(s) <= card.SLACK_SECTION_LIMIT for s in sections)
        assert not any("..." == s[-3:] for s in sections)                  # nothing was sliced


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

    def test_refuses_when_a_mandatory_input_is_missing_or_blank(self):
        ok = dict(baa_status_per_provider={"p": "signed"}, emily_weighed_in="yes", emily_date="2026-10-01",
                  phi_bytes_proposed="x", target_provider="p")
        card.render_d2_card(**ok)   # the control renders
        for bad in (
            dict(ok, baa_status_per_provider={}),                          # no BAA map
            dict(ok, baa_status_per_provider={"p": ""}),                   # a BLANK status line
            dict(ok, baa_status_per_provider={"q": "signed"}),             # target provider has no line
            dict(ok, emily_weighed_in="maybe"),                            # not yes|no
            dict(ok, emily_weighed_in="yes", emily_date=""),               # yes without a date
            dict(ok, emily_weighed_in="yes", emily_date="Oct 1"),          # date not YYYY-MM-DD
            dict(ok, phi_bytes_proposed=""),                               # no bytes line
        ):
            with pytest.raises(ValueError):
                card.render_d2_card(**bad)

    def test_no_emily_input_is_a_visible_input_not_a_precondition(self):
        """Charter D2 (Harrison-edited): BAA + Emily are mandatory VISIBLE inputs, NOT preconditions
        (D-108: Harrison is the sole authority). 'no' renders truthfully and never claims to block."""
        _, blocks = card.render_d2_card(baa_status_per_provider={"p": "s"}, emily_weighed_in="no", emily_date="",
                                        phi_bytes_proposed="none", target_provider="p")
        text = blocks[0]["text"]["text"]
        assert "Emily has NOT weighed in" in text and "not a precondition" in text
        assert "REQUIRED before sign-off" not in text
