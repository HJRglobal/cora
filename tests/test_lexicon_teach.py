"""S6 tests: cora_lexicon_add teach tool (F-23 parity staged write).

Pins: full-level gate; NOT-SAVED preview + server-side stash; confirmed=true
executes the STASHED payload (a model echo on the confirm turn is ignored);
founder fast-path applies directly with the audit proposal; teammate confirm
files a Harrison-gated proposal with their id as contributor; PHI + roster
refusals at PREVIEW time; no-pending confirm is truthful; negative-routing
description pins (the cq-a1306f3835f8 class)."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import knowledge_review as kr
from cora import lexicon
from cora.tools import tool_dispatch
from cora.tools.tool_dispatch import (
    TOOL_DEFINITIONS,
    _GLOBAL_CORE_TOOLS,
    _PENDING_LEXICON_ADDS,
    _TOOL_FUNCTIONS,
    _TOOL_TIMEOUTS,
    _tool_cora_lexicon_add,
)

_HARRISON = tool_dispatch._HARRISON_SLACK_ID
_ALEX = "U0B3VGWJTMJ"
_CHAN = "f3e-leadership"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    _PENDING_LEXICON_ADDS.clear()
    lex_dir = tmp_path / "lexicon"
    lex_dir.mkdir()
    monkeypatch.setenv("LEXICON_DIR", str(lex_dir))
    monkeypatch.setenv("LEXICON_SKU_ALIASES_PATH", str(tmp_path / "no-skus.yaml"))
    monkeypatch.setenv("LEXICON_USER_ALIASES_PATH", str(tmp_path / "no-users.yaml"))
    monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", tmp_path / "pending.jsonl")
    monkeypatch.setattr(kr, "_ARCHIVE_PATH", tmp_path / "archive.jsonl")
    lexicon.invalidate_cache()
    yield {"dir": lex_dir, "tmp": tmp_path}
    _PENDING_LEXICON_ADDS.clear()
    lexicon.invalidate_cache()


def _call(user=_ALEX, entity="UFL", **kw) -> str:
    base = {"_channel_name": _CHAN}
    base.update(kw)
    return _tool_cora_lexicon_add(user, entity, base)


class TestWiring:
    def test_registered_everywhere(self):
        assert any(t["name"] == "cora_lexicon_add" for t in TOOL_DEFINITIONS)
        assert "cora_lexicon_add" in _TOOL_FUNCTIONS
        assert "cora_lexicon_add" in _GLOBAL_CORE_TOOLS
        assert _TOOL_TIMEOUTS.get("cora_lexicon_add") == 15

    def test_description_negative_routing_pins(self):
        """The cq-a1306f3835f8 class: an intake phrase must not hijack unrelated
        intents. The description must anti-route personal notes, scheduler
        phrases, and task ops, and default unclear cases to cora_remember."""
        desc = next(t["description"] for t in TOOL_DEFINITIONS
                    if t["name"] == "cora_lexicon_add")
        assert "cora_remember" in desc
        assert "scheduling" in desc
        assert "task" in desc.lower()
        assert "means" in desc and "is short for" in desc
        assert "confirmed=true" in desc
        assert "ignored on the" in desc  # echo-proof confirm-turn contract


class TestLevelGate:
    @pytest.mark.parametrize("level", ["off", "resolve"])
    def test_below_full_refuses(self, monkeypatch, level):
        monkeypatch.setenv("CORA_LEXICON", level)
        r = _call(term="the cage", meaning="the UFL octagon set")
        assert r.startswith("NOT SAVED")
        assert not _PENDING_LEXICON_ADDS


class TestStagedWrite:
    def test_phase1_previews_and_stashes_nothing_written(self, monkeypatch, _fresh):
        monkeypatch.setenv("CORA_LEXICON", "full")
        r = _call(term="the cage", meaning="the UFL octagon set", type="process")
        assert r.startswith("NOT SAVED")
        assert '"the cage"' in r and "UFL octagon set" in r
        assert len(_PENDING_LEXICON_ADDS) == 1
        assert not (_fresh["dir"] / "ufl.yaml").exists()

    def test_confirm_executes_stashed_payload_not_the_echo(self, monkeypatch, _fresh):
        """F-23: the confirm turn's own fields are IGNORED -- the stashed entry
        is what a founder confirm writes."""
        monkeypatch.setenv("CORA_LEXICON", "full")
        _call(user=_HARRISON, term="the cage", meaning="the UFL octagon set")
        r = _call(user=_HARRISON, confirmed=True,
                  term="SOMETHING ELSE", meaning="a hallucinated echo")
        assert r.startswith("Saved to the UFL lexicon")
        text = (_fresh["dir"] / "ufl.yaml").read_text(encoding="utf-8")
        assert "the cage" in text
        assert "SOMETHING ELSE" not in text

    def test_no_pending_confirm_is_truthful(self, monkeypatch):
        monkeypatch.setenv("CORA_LEXICON", "full")
        r = _call(user=_HARRISON, confirmed=True)
        assert r.startswith("NOT SAVED")
        assert "pending" in r

    def test_teammate_confirm_files_proposal_never_writes(self, monkeypatch, _fresh):
        monkeypatch.setenv("CORA_LEXICON", "full")
        _call(term="the cage", meaning="the UFL octagon set")
        r = _call(confirmed=True)
        assert "Queued for Harrison's review" in r
        assert not (_fresh["dir"] / "ufl.yaml").exists()
        pending = (_fresh["tmp"] / "pending.jsonl").read_text(encoding="utf-8")
        assert '"update_type": "lexicon"' in pending
        assert f'"contributor_id": "{_ALEX}"' in pending
        assert '"lane": "taught"' in pending

    def test_founder_confirm_applies_and_records_approved(self, monkeypatch, _fresh):
        monkeypatch.setenv("CORA_LEXICON", "full")
        monkeypatch.setenv("GOLDEN_SET_AUTO_PATH",
                           str(_fresh["tmp"] / "golden-auto.yaml"))
        _call(user=_HARRISON, term="the cage", meaning="the UFL octagon set")
        r = _call(user=_HARRISON, confirmed=True)
        assert r.startswith("Saved to the UFL lexicon")
        lexicon.invalidate_cache()
        assert lexicon.resolve("the cage", "UFL").status == "exact"
        ledger = (_fresh["tmp"] / "pending.jsonl").read_text(encoding="utf-8")
        assert '"state": "APPROVED"' in ledger or '"APPROVED"' in ledger

    def test_stash_keyed_on_user_not_channel_wide(self, monkeypatch):
        monkeypatch.setenv("CORA_LEXICON", "full")
        _call(user=_ALEX, term="the cage", meaning="the UFL octagon set")
        # A DIFFERENT user's confirm finds no pending of their own.
        r = _call(user="U999OTHER", confirmed=True)
        assert r.startswith("NOT SAVED")


class TestPreviewRefusals:
    def test_missing_fields_refused(self, monkeypatch):
        monkeypatch.setenv("CORA_LEXICON", "full")
        assert _call(term="x", meaning="").startswith("NOT SAVED")
        assert _call(term="", meaning="y").startswith("NOT SAVED")

    def test_phi_shaped_teach_refused_at_preview(self, monkeypatch):
        monkeypatch.setenv("CORA_LEXICON", "full")
        r = _call(entity="LEX",
                  term="bob's auth",
                  meaning="Bob Smith's billing authorization is pending")
        assert r.startswith("NOT SAVED")
        assert "staff/ops terms only" in r
        assert not _PENDING_LEXICON_ADDS

    def test_person_off_roster_refused_at_preview(self, monkeypatch):
        monkeypatch.setenv("CORA_LEXICON", "full")
        with patch.object(tool_dispatch, "_HARRISON_SLACK_ID", _HARRISON):
            import cora.lexicon_writer as lw
            with patch.object(lw, "_roster_names",
                              return_value={"jennifer mortensen"}):
                r = _call(term="jm", meaning="Random Stranger", type="person")
        assert r.startswith("NOT SAVED")
        assert "roster" in r

    def test_person_on_roster_previews(self, monkeypatch):
        monkeypatch.setenv("CORA_LEXICON", "full")
        import cora.lexicon_writer as lw
        with patch.object(lw, "_roster_names", return_value={"jennifer mortensen"}):
            r = _call(term="jm", meaning="Jennifer Mortensen", type="person")
        assert r.startswith("NOT SAVED yet")
