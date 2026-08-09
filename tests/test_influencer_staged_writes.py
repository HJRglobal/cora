"""v2b S5: influencer_add_handle + influencer_log_deliverable as staged writes.

Both were honor gates. The one that mattered most is log_deliverable, because it
does NAME-BASED RESOLUTION: "complete Mario Bautista story" becomes a specific
row via resolve_pending_deliverable, which returns the OLDEST pending match. Under
the honor gate that resolution ran on the confirm call, so the row the user was
shown in the preview and the row that actually got closed were resolved at
different moments against a table that moves. Now the preview resolves and the
resolved id goes in the stash.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")

import pytest  # noqa: E402

import cora.tools.influencer_client as ic  # noqa: E402
from cora import confirm_cards as cc  # noqa: E402
from cora.tools import tool_dispatch as td  # noqa: E402

ALEX = "U0B3VGWJTMJ"
CHAN = "f3e-sales"
IN = {"_channel_name": CHAN}
_MAP = {ALEX: {"display_name": "Alex Cordova"}}


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    monkeypatch.setattr(ic, "_DB_PATH", tmp_path / "influencer_test.db", raising=False)
    cc.reset_cards_for_tests()
    with cc._INDEX_LOCK:
        cc._INDEX.clear()
    for kind in td._CLASSB_KINDS:
        td._CLASSB[kind]["store"].clear()
    yield
    for kind in td._CLASSB_KINDS:
        td._CLASSB[kind]["store"].clear()
    cc.reset_cards_for_tests()


def _peek(kind):
    return td._CLASSB[kind]["peek"](ALEX, CHAN)


def _handle(**kw):
    with patch.object(td, "_load_slack_asana_map", return_value=_MAP):
        return td._tool_influencer_add_handle(ALEX, "F3E", {**IN, **kw})


def _deliv(**kw):
    with patch.object(td, "_load_slack_asana_map", return_value=_MAP):
        return td._tool_influencer_log_deliverable(ALEX, "F3E", {**IN, **kw})


# ── influencer_add_handle ──────────────────────────────────────────────────


class TestAddHandle:
    def test_unconfirmed_previews_and_registers_nothing(self):
        with patch.object(ic, "register_handle") as write:
            out = _handle(athlete_name="Luis Pena", platform="instagram",
                          handle="@luispena_ufc")
        write.assert_not_called()
        assert "NOT REGISTERED yet" in out
        assert "Luis Pena" in out and "luispena_ufc" in out
        assert _peek("influencer_handle")["stash_id"]

    def test_the_preview_carries_an_honest_confirm_instruction(self):
        out = _handle(athlete_name="A", platform="instagram", handle="a")
        user_facing = out.split("\n\n", 1)[1]
        assert "@mention me with" in user_facing or "tap *Confirm* below" in user_facing

    def test_confirm_registers_the_STASHED_handle(self):
        _handle(athlete_name="Luis Pena", platform="instagram", handle="luispena_ufc")
        with patch.object(ic, "register_handle",
                          return_value={"handle": "luispena_ufc"}) as write:
            _deliv_out = _handle(confirmed=True, athlete_name="SOMEONE ELSE",
                                 platform="tiktok", handle="attacker")
        kw = write.call_args.kwargs
        assert kw["athlete_name"] == "Luis Pena"
        assert kw["platform"] == "instagram"
        assert kw["handle"] == "luispena_ufc"
        assert "REGISTERED" in _deliv_out

    def test_a_confirm_with_no_pending_is_honest(self):
        with patch.object(ic, "register_handle") as write:
            out = _handle(confirmed=True, athlete_name="A", platform="instagram",
                          handle="a")
        write.assert_not_called()
        assert "NOT DONE" in out

    def test_missing_fields_refuse_and_stash_nothing(self):
        for bad, word in (
            ({"platform": "instagram", "handle": "h"}, "athlete_name"),
            ({"athlete_name": "A", "handle": "h"}, "platform"),
            ({"athlete_name": "A", "platform": "instagram"}, "handle"),
        ):
            out = _handle(**bad)
            assert word in out
            assert _peek("influencer_handle") is None

    def test_a_tap_registers_the_same_stash(self):
        _handle(athlete_name="Luis Pena", platform="instagram", handle="luispena_ufc")
        sid = _peek("influencer_handle")["stash_id"]
        with patch.object(ic, "register_handle",
                          return_value={"handle": "luispena_ufc"}) as write, \
             patch("cora.entity_router.route", return_value="F3E"):
            res = td.resolve_and_claim_stash(sid, ALEX, "confirm")
        write.assert_called_once()
        assert res["outcome"] == "executed"

    def test_a_non_requester_tap_registers_nothing(self):
        _handle(athlete_name="Luis Pena", platform="instagram", handle="h")
        sid = _peek("influencer_handle")["stash_id"]
        with patch.object(ic, "register_handle") as write:
            res = td.resolve_and_claim_stash(sid, "U0INTRUDER", "confirm")
        write.assert_not_called()
        assert res["outcome"] == "unauthorized"
        assert td.stash_is_live(sid)

    def test_a_tracker_error_does_not_claim_success(self):
        _handle(athlete_name="Luis Pena", platform="instagram", handle="h")
        with patch.object(ic, "register_handle",
                          side_effect=ic.InfluencerClientError("db locked")):
            out = _handle(confirmed=True)
        assert "REGISTERED" not in out
        assert "wasn't registered" in out


# ── influencer_log_deliverable ─────────────────────────────────────────────


class TestLogDeliverableAdd:
    def test_unconfirmed_previews_and_writes_nothing(self):
        out = _deliv(action="add", athlete_name="Mario Bautista",
                     platform="instagram", deliverable_type="story",
                     due_date="2026-09-01")
        assert "NOT LOGGED yet" in out
        assert "Mario Bautista" in out and "story" in out
        assert ic.get_deliverables(athlete="Mario Bautista") == []

    def test_confirm_adds_the_STASHED_payload(self):
        _deliv(action="add", athlete_name="Mario Bautista", platform="instagram",
               deliverable_type="story", due_date="2026-09-01")
        _deliv(confirmed=True, action="add", athlete_name="SOMEONE ELSE",
               platform="tiktok", deliverable_type="video")
        rows = ic.get_deliverables()
        assert len(rows) == 1
        assert rows[0]["athlete_name"] == "Mario Bautista"
        assert rows[0]["deliverable_type"] == "story"

    def test_the_stash_is_consumed_exactly_once(self):
        _deliv(action="add", athlete_name="Solo", platform="instagram",
               deliverable_type="post")
        _deliv(confirmed=True)
        second = _deliv(confirmed=True)
        assert len(ic.get_deliverables(athlete="Solo")) == 1
        assert "NOT DONE" in second


class TestLogDeliverableResolutionHappensAtPreview:
    """The defect this migration closes for this tool."""

    def _two_pending(self):
        old = ic.add_deliverable(athlete_name="Mario Bautista", platform="instagram",
                                 deliverable_type="story", due_date="2026-01-01",
                                 entity="F3E")
        new = ic.add_deliverable(athlete_name="Mario Bautista", platform="instagram",
                                 deliverable_type="story", due_date="2026-12-01",
                                 entity="F3E")
        return old, new

    def test_the_preview_resolves_the_row_and_names_it(self):
        old, _new = self._two_pending()
        out = _deliv(action="complete", athlete_name="Mario Bautista",
                     deliverable_type="story")
        assert f"#{old['id']}" in out, "the preview must name the row it resolved"
        assert _peek("influencer_deliverable")["deliverable_id"] == old["id"]

    def test_the_confirm_closes_the_row_the_preview_named_even_if_it_would_now_resolve_differently(self):
        """Under the honor gate the confirm re-resolved from scratch. Close the
        previewed row out of band and the confirm would silently retarget the
        NEXT oldest pending one -- a row the user never saw."""
        old, new = self._two_pending()
        _deliv(action="complete", athlete_name="Mario Bautista",
               deliverable_type="story")
        staged_id = _peek("influencer_deliverable")["deliverable_id"]
        assert staged_id == old["id"]

        with patch.object(ic, "resolve_pending_deliverable") as reresolve:
            _deliv(confirmed=True, action="complete", athlete_name="Mario Bautista")
        # The confirm must not re-resolve; it targets the stashed row.
        reresolve.assert_not_called()

        rows = {r["id"]: r for r in ic.get_deliverables(include_complete=True,
                                                        include_waived=True)}
        assert rows[old["id"]]["status"] == "complete"
        assert rows[new["id"]]["status"] == "pending", "the wrong row was closed"

    def test_an_unresolvable_name_refuses_at_preview_and_stashes_nothing(self):
        out = _deliv(action="complete", athlete_name="Nobody At All")
        assert "No pending deliverable found" in out
        assert _peek("influencer_deliverable") is None

    def test_a_non_numeric_id_refuses_at_preview_and_stashes_nothing(self):
        out = _deliv(action="complete", deliverable_id="abc")
        assert "must be a number" in out
        assert _peek("influencer_deliverable") is None

    def test_an_unknown_action_refuses_before_stashing(self):
        out = _deliv(action="delete", deliverable_id=1)
        assert "unknown action" in out.lower()
        assert _peek("influencer_deliverable") is None


class TestLogDeliverableWaiveAndTaps:
    def test_waive_writes_the_stashed_row(self):
        row = ic.add_deliverable(athlete_name="Fighter B", platform="tiktok",
                                 deliverable_type="video", entity="F3E")
        _deliv(action="waive", deliverable_id=row["id"], notes="Injury")
        out = _deliv(confirmed=True)
        assert "waive" in out.lower()
        stored = {r["id"]: r for r in ic.get_deliverables(include_waived=True)}
        assert stored[row["id"]]["status"] == "waived"

    def test_a_tap_executes_the_stashed_action(self):
        row = ic.add_deliverable(athlete_name="Fighter C", platform="instagram",
                                 deliverable_type="post", entity="F3E")
        _deliv(action="complete", deliverable_id=row["id"])
        sid = _peek("influencer_deliverable")["stash_id"]
        with patch("cora.entity_router.route", return_value="F3E"):
            res = td.resolve_and_claim_stash(sid, ALEX, "confirm")
        assert res["outcome"] == "executed"
        stored = {r["id"]: r for r in ic.get_deliverables(include_complete=True)}
        assert stored[row["id"]]["status"] == "complete"

    def test_a_cancel_tap_writes_nothing(self):
        row = ic.add_deliverable(athlete_name="Fighter D", platform="instagram",
                                 deliverable_type="post", entity="F3E")
        _deliv(action="complete", deliverable_id=row["id"])
        sid = _peek("influencer_deliverable")["stash_id"]
        res = td.resolve_and_claim_stash(sid, ALEX, "cancel")
        assert res["outcome"] == "cancelled"
        assert ic.get_deliverables()[0]["status"] == "pending"

    def test_an_expired_tap_names_the_deliverable(self):
        row = ic.add_deliverable(athlete_name="Fighter E", platform="instagram",
                                 deliverable_type="post", entity="F3E")
        _deliv(action="complete", deliverable_id=row["id"])
        entry = _peek("influencer_deliverable")
        sid = entry["stash_id"]
        entry["ts"] -= td._CLASSB_TTL_SECONDS + 5
        res = td.resolve_and_claim_stash(sid, ALEX, "confirm")
        assert res["outcome"] == "expired"
        assert res["label"] == "that deliverable update"
        assert ic.get_deliverables()[0]["status"] == "pending"

    def test_eval_mode_refuses_a_tap(self, monkeypatch):
        row = ic.add_deliverable(athlete_name="Fighter F", platform="instagram",
                                 deliverable_type="post", entity="F3E")
        _deliv(action="complete", deliverable_id=row["id"])
        sid = _peek("influencer_deliverable")["stash_id"]
        monkeypatch.setenv("CORA_EVAL_MODE", "1")
        res = td.resolve_and_claim_stash(sid, ALEX, "confirm")
        assert res["outcome"] == "orphaned"
        assert ic.get_deliverables()[0]["status"] == "pending"


class TestBothKindsRegistered:
    @pytest.mark.parametrize("kind", ["influencer_handle", "influencer_deliverable"])
    def test_the_kind_is_registered_everywhere_it_has_to_be(self, kind):
        assert kind in td._stash_kind_specs()
        assert kind in td._defer_to_model_kinds()
        assert kind in td._PENDING_KIND_LABELS

    def test_the_two_kinds_do_not_collide(self):
        _handle(athlete_name="Luis", platform="instagram", handle="l")
        _deliv(action="add", athlete_name="Mario", platform="instagram",
               deliverable_type="post")
        assert _peek("influencer_handle")["athlete_name"] == "Luis"
        assert _peek("influencer_deliverable")["athlete_name"] == "Mario"
