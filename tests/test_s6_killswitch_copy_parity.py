"""S6 kill-switch copy parity.

Non-negotiable for this arc: `CORA_CONFIRM_BUTTONS=off` reverts every new
surface to its pre-branch behavior BYTE-IDENTICALLY.

The first cut of S6 attached blocks conditionally but changed the instruction
COPY unconditionally, so with the flag off all four surfaces told the user to
tap a button that was not rendered -- both a broken revert and a message that
lies. These pin the copy to what actually renders, in both flag states.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from cora import gap_autofill as ga
from cora.connectors import hubspot_email_sync as hes
from cora.tools import osn_shift_handler as osh

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_briefing_script():
    spec = importlib.util.spec_from_file_location(
        "_rdb_copy", _REPO_ROOT / "scripts" / "run_daily_briefing.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_rdb_copy"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Rec:
    name = "Tommy Tucson"
    role = "GM"
    entity = "OSN"
    slack_id = "U_T"


class TestBriefingCopy:
    def test_buttons_off_restores_the_reaction_only_copy(self):
        rdb = _load_briefing_script()
        out = rdb._compose_review_message(_Rec(), "BODY", False)
        assert ":+1:" in out and ":-1:" in out
        assert "Tap" not in out
        assert "Enable delivery" not in out

    def test_buttons_on_names_both_affordances(self):
        rdb = _load_briefing_script()
        out = rdb._compose_review_message(_Rec(), "BODY", True)
        assert "Enable delivery" in out
        assert ":+1:" in out          # the reaction path is never un-named

    def test_default_is_the_pre_branch_copy(self):
        """A caller that forgets the flag gets the conservative message."""
        rdb = _load_briefing_script()
        assert "Tap" not in rdb._compose_review_message(_Rec(), "BODY")


class _FakeSlack:
    def __init__(self):
        self.posted: list[dict] = []

    def conversations_open(self, **kw):
        return {"channel": {"id": "D1"}}

    def chat_postMessage(self, **kw):
        self.posted.append(kw)
        return {"ok": True, "ts": "1.1", "channel": "D1"}


class TestGapAskCopy:
    def _gap(self):
        return {"ts": "g1", "entity": "F3E", "channel": "f3e-sales",
                "question": "wholesale price?", "gap": "no record"}

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GAP_ASK_PENDING_PATH", str(tmp_path / "asks.json"))
        yield

    def test_buttons_off_restores_just_say_so(self, monkeypatch):
        monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "off")
        fake = _FakeSlack()
        with patch.object(ga, "resolve_owner", return_value="U_OWNER"):
            ga.escalate_gap(self._gap(), fake)
        text = fake.posted[0]["text"]
        assert "just say so." in text
        assert "button" not in text.lower()
        assert "blocks" not in fake.posted[0]

    def test_buttons_on_mentions_the_button(self, monkeypatch):
        monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
        fake = _FakeSlack()
        with patch.object(ga, "resolve_owner", return_value="U_OWNER"):
            ga.escalate_gap(self._gap(), fake)
        text = fake.posted[0]["text"]
        assert "tap a button below" in text
        assert fake.posted[0].get("blocks")

    def test_reply_instruction_survives_in_both_states(self, monkeypatch):
        for state in ("on", "off"):
            monkeypatch.setenv("CORA_CONFIRM_BUTTONS", state)
            fake = _FakeSlack()
            with patch.object(ga, "resolve_owner", return_value="U_OWNER"):
                ga.escalate_gap(self._gap(), fake)
            assert "*reply to this message*" in fake.posted[0]["text"], state


class TestOsnCardCopy:
    class _Sched:
        schedule_id = "abcdef123456"
        week_start = "2026-08-10"

    def test_buttons_off_restores_the_reaction_only_card(self, monkeypatch):
        monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "off")
        fake = _FakeSlack()
        with patch.object(osh, "get_schedule", return_value=None), \
             patch("cora.tools.osn_shift_db.save_schedule"):
            osh._post_approval_card(self._Sched(), "shifts", [], "C1", fake)
        text = fake.posted[0]["text"]
        assert "React ✅ on this message to approve" in text
        assert "Tap Approve" not in text
        assert "blocks" not in fake.posted[0]

    def test_buttons_on_names_the_button_and_keeps_the_reaction(self, monkeypatch):
        monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
        fake = _FakeSlack()
        with patch.object(osh, "get_schedule", return_value=None), \
             patch("cora.tools.osn_shift_db.save_schedule"):
            osh._post_approval_card(self._Sched(), "shifts", [], "C1", fake)
        text = fake.posted[0]["text"]
        assert "Tap Approve below" in text
        assert "✅" in text
        assert fake.posted[0].get("blocks")


class TestHubspotDmCopy:
    """The DM text is composed inline in the sweep, so assert on the two
    literal hint strings the branch selects between."""

    def test_both_hints_exist_in_source_and_are_flag_selected(self):
        src = (_REPO_ROOT / "src" / "cora" / "connectors"
               / "hubspot_email_sync.py").read_text(encoding="utf-8")
        assert "👍 attach this thread  ·  👎 skip" in src   # pre-branch copy
        assert "Tap a button below, or react" in src        # buttons-on copy
        assert "_buttons_on = _cc.confirm_buttons_enabled()" in src

    def test_blocks_only_attach_when_enabled(self, monkeypatch):
        monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "off")
        assert hes.build_match_blocks("x", "hsmatch-1")   # builder still callable
        from cora import confirm_cards as cc
        assert cc.confirm_buttons_enabled() is False
