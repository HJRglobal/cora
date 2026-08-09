"""v2b S5: slack_send_dm gets a real preview branch + stash + card.

This is the one Class-B tool where the button tap is a ONE-TAP SEND to another
human, so it gets the most scrutiny:

  * before this, an unconfirmed call just REFUSED -- there was no preview branch
    at all, so there was nothing to card and the model had to invent the preview
    itself;
  * the recipient now binds SERVER-SIDE at preview time (resolved id in the
    stash), so a confirm turn naming somebody else cannot redirect the message.
    That is the same doctrine the Shopify hotfix locked: staged-write identity
    binds server-side, never an LLM echo;
  * the egress screens run in the EXECUTOR, which is the only code both routes
    (typed confirm and button tap) pass through. Proven behaviourally below,
    through the real WebClient method, not asserted from the preview.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")

import pytest  # noqa: E402
from slack_sdk.web.client import WebClient  # noqa: E402

import cora  # noqa: E402,F401 -- importing installs the egress sanitizer
from cora import confirm_cards as cc  # noqa: E402
from cora import slack_egress  # noqa: E402
from cora.tools import tool_dispatch as td  # noqa: E402

USER = "U0B2RM2JYJ1"
CHAN = "f3e-leadership"
IN = {"_channel_name": CHAN}
TOMMY = "U0TOMMY"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "on")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test-token")
    monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
    cc.reset_cards_for_tests()
    with cc._INDEX_LOCK:
        cc._INDEX.clear()
    for kind in td._CLASSB_KINDS:
        td._CLASSB[kind]["store"].clear()
    yield
    for kind in td._CLASSB_KINDS:
        td._CLASSB[kind]["store"].clear()
    cc.reset_cards_for_tests()


def _peek():
    return td._CLASSB["slack_dm"]["peek"](USER, CHAN)


def _resolves_to(uid=TOMMY, info=""):
    return patch.object(td, "resolve_name_to_slack_user_id", return_value=(uid, info))


def _preview(recipient="Tommy", message="Heads up, the pallet ships Friday.",
             entity="F3E"):
    with _resolves_to(), patch.object(
            td, "_load_slack_asana_map",
            return_value={TOMMY: {"display_name": "Tommy Tucker"}}):
        return td._tool_slack_send_dm(USER, entity, {
            **IN, "recipient_name": recipient, "message": message})


class _FakeSlack:
    """Stands in for the constructed WebClient in the executor."""

    def __init__(self, *a, **kw):
        self.sent = []

    def conversations_open(self, users):
        return {"channel": {"id": "D0TOMMY"}}

    def chat_postMessage(self, channel, text):
        self.sent.append((channel, text))
        return {"ts": "1.23"}


def _confirm(entity="F3E", **overrides):
    fake = _FakeSlack()
    with patch("slack_sdk.WebClient", return_value=fake), \
         patch.object(td, "_load_slack_asana_map",
                      return_value={TOMMY: {"display_name": "Tommy Tucker"}}):
        out = td._tool_slack_send_dm(USER, entity, {
            **IN, "confirmed": True, **overrides})
    return out, fake


# ── preview ────────────────────────────────────────────────────────────────


class TestPreviewBranch:
    def test_an_unconfirmed_call_now_previews_instead_of_refusing(self):
        out = _preview()
        assert "NOT SENT yet" in out
        assert "Tommy Tucker" in out and "pallet ships Friday" in out
        assert "refused" not in out.lower()

    def test_the_preview_stashes_the_RESOLVED_recipient_id(self):
        _preview()
        entry = _peek()
        assert entry["recipient_id"] == TOMMY, \
            "the recipient must bind server-side at preview, not on the confirm turn"
        assert entry["message"] == "Heads up, the pallet ships Friday."

    def test_the_preview_carries_an_honest_confirm_instruction(self):
        out = _preview()
        user_facing = out.split("\n\n", 1)[1]
        assert "@mention me with" in user_facing or "tap *Confirm* below" in user_facing

    def test_an_unresolvable_recipient_refuses_and_stashes_nothing(self):
        with _resolves_to(uid=None, info="No match for 'Zzz'."):
            out = td._tool_slack_send_dm(USER, "F3E", {
                **IN, "recipient_name": "Zzz", "message": "hi"})
        assert "could not resolve" in out
        assert _peek() is None

    def test_missing_fields_refuse_and_stash_nothing(self):
        for bad, word in (({"message": "hi"}, "recipient_name"),
                          ({"recipient_name": "Tommy"}, "message")):
            out = td._tool_slack_send_dm(USER, "F3E", {**IN, **bad})
            assert word in out
            assert _peek() is None

    def test_nothing_is_sent_during_a_preview(self):
        with patch("slack_sdk.WebClient") as ctor:
            _preview()
        ctor.assert_not_called()


# ── confirm executes the stash ─────────────────────────────────────────────


class TestConfirmExecutesTheStash:
    def test_confirm_sends_the_STASHED_message(self):
        _preview()
        out, fake = _confirm()
        assert fake.sent == [("D0TOMMY", "Heads up, the pallet ships Friday.")]
        assert "WRITE_CONFIRMED" in out and "Tommy Tucker" in out

    def test_a_confirm_turn_cannot_REDIRECT_the_dm_to_someone_else(self):
        """The security property this migration exists for."""
        _preview()
        with _resolves_to(uid="U0VICTIM"):
            out, fake = _confirm(recipient_name="Somebody Else",
                                 message="totally different text")
        assert fake.sent == [("D0TOMMY", "Heads up, the pallet ships Friday.")], \
            "confirm-turn args retargeted the DM"
        assert "Tommy Tucker" in out

    def test_a_confirm_with_no_pending_is_honest_and_sends_nothing(self):
        out, fake = _confirm(recipient_name="Tommy", message="hi")
        assert fake.sent == []
        assert "NOT DONE" in out and "expired" in out.lower()

    def test_the_stash_is_consumed_exactly_once(self):
        _preview()
        first, f1 = _confirm()
        second, f2 = _confirm()
        assert len(f1.sent) == 1
        assert f2.sent == []
        assert "NOT DONE" in second

    def test_a_slack_api_error_does_not_claim_success(self):
        from slack_sdk.errors import SlackApiError
        _preview()

        class _Boom(_FakeSlack):
            def chat_postMessage(self, channel, text):
                raise SlackApiError("nope", {"error": "channel_not_found"})

        with patch("slack_sdk.WebClient", return_value=_Boom()), \
             patch.object(td, "_load_slack_asana_map", return_value={}):
            out = td._tool_slack_send_dm(USER, "F3E", {**IN, "confirmed": True})
        assert "WRITE_CONFIRMED" not in out
        assert "wasn't sent" in out

    def test_a_missing_token_sends_nothing(self, monkeypatch):
        _preview()
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        with patch("slack_sdk.WebClient") as ctor:
            out = td._tool_slack_send_dm(USER, "F3E", {**IN, "confirmed": True})
        ctor.assert_not_called()
        assert "SLACK_BOT_TOKEN" in out


# ── the button tap ─────────────────────────────────────────────────────────


class TestButtonTap:
    def _tap(self, sid, who=USER, action="confirm"):
        fake = _FakeSlack()
        with patch("slack_sdk.WebClient", return_value=fake), \
             patch.object(td, "_load_slack_asana_map",
                          return_value={TOMMY: {"display_name": "Tommy Tucker"}}), \
             patch("cora.entity_router.route", return_value="F3E"):
            res = td.resolve_and_claim_stash(sid, who, action)
        return res, fake

    def test_a_tap_sends_the_same_stash(self):
        _preview()
        sid = _peek()["stash_id"]
        res, fake = self._tap(sid)
        assert res["outcome"] == "executed"
        assert fake.sent == [("D0TOMMY", "Heads up, the pallet ships Friday.")]
        assert "WRITE_CONFIRMED" not in res["message"]

    def test_a_non_requester_tap_sends_nothing_and_consumes_nothing(self):
        _preview()
        sid = _peek()["stash_id"]
        res, fake = self._tap(sid, who="U0INTRUDER")
        assert res["outcome"] == "unauthorized"
        assert fake.sent == []
        assert td.stash_is_live(sid)

    def test_a_cancel_tap_sends_nothing(self):
        _preview()
        sid = _peek()["stash_id"]
        res, fake = self._tap(sid, action="cancel")
        assert res["outcome"] == "cancelled"
        assert fake.sent == []
        assert not td.stash_is_live(sid)

    def test_an_expired_tap_names_the_dm(self):
        _preview()
        entry = _peek()
        sid = entry["stash_id"]
        entry["ts"] -= td._CLASSB_TTL_SECONDS + 5
        res, fake = self._tap(sid)
        assert res["outcome"] == "expired" and res["label"] == "that DM"
        assert fake.sent == []

    def test_eval_mode_refuses_a_tap(self, monkeypatch):
        _preview()
        sid = _peek()["stash_id"]
        monkeypatch.setenv("CORA_EVAL_MODE", "1")
        res, fake = self._tap(sid)
        assert res["outcome"] == "orphaned"
        assert fake.sent == []


# ── egress screens run in the EXECUTOR ─────────────────────────────────────


class TestEgressScreensRunInTheExecutor:
    """The kickoff's explicit requirement. The point is that the screen sits on
    the code BOTH routes share, so a one-tap send can never skip what the typed
    path does."""

    def test_the_send_boundary_itself_is_the_patched_webclient_method(self):
        assert getattr(WebClient.chat_postMessage, "_cora_egress_wrapped", False), \
            "the class-level egress patch is what sanitizes an outbound DM"

    def test_the_executor_send_is_sanitized_on_the_wire(self):
        """Behavioural, through the REAL chat_postMessage: api_call sits BELOW
        the egress wrapper, so what lands there is what Slack would receive."""
        raw = "Heads up **bold** https://example.com/some/long/path?x=1"
        _preview(message=raw)
        assert _peek()["message"] == raw, "the stash holds the author's text"

        calls = {}

        def _fake_api_call(self, api_method, **kwargs):
            calls[api_method] = kwargs
            if api_method == "conversations.open":
                return {"channel": {"id": "D0TOMMY"}}
            return {"ts": "1.23"}

        with patch.object(WebClient, "api_call", _fake_api_call), \
             patch.object(WebClient, "conversations_open",
                          lambda self, users: {"channel": {"id": "D0TOMMY"}}), \
             patch.object(td, "_load_slack_asana_map",
                          return_value={TOMMY: {"display_name": "Tommy Tucker"}}):
            td._tool_slack_send_dm(USER, "F3E", {**IN, "confirmed": True})

        sent = calls["chat.postMessage"]["json"]["text"]
        assert sent == slack_egress.sanitize_text(raw)
        assert sent != raw, "the fixture must actually exercise a transform"

    def test_a_button_tap_goes_through_the_same_executor(self):
        """Same proof for the tap route: one write implementation, one screen."""
        raw = "Ship **now** https://example.com/a/b/c?q=2"
        _preview(message=raw)
        sid = _peek()["stash_id"]
        calls = {}

        def _fake_api_call(self, api_method, **kwargs):
            calls[api_method] = kwargs
            return {"ts": "1.23"}

        with patch.object(WebClient, "api_call", _fake_api_call), \
             patch.object(WebClient, "conversations_open",
                          lambda self, users: {"channel": {"id": "D0TOMMY"}}), \
             patch.object(td, "_load_slack_asana_map",
                          return_value={TOMMY: {"display_name": "Tommy Tucker"}}), \
             patch("cora.entity_router.route", return_value="F3E"):
            res = td.resolve_and_claim_stash(sid, USER, "confirm")

        assert res["outcome"] == "executed"
        assert calls["chat.postMessage"]["json"]["text"] == slack_egress.sanitize_text(raw)


# ── PHI / LEX floor ────────────────────────────────────────────────────────


class TestPhiAndLexFloor:
    def test_lex_scope_is_blocked_at_preview_and_stashes_nothing(self):
        for ent in ("LEX", "LEX-LLC", "lex-lbhs"):
            out = td._tool_slack_send_dm(USER, ent, {
                **IN, "recipient_name": "Tommy", "message": "hi"})
            assert "blocked" in out.lower()
            assert _peek() is None

    def test_the_executor_re_checks_the_stashed_entity(self):
        fake = _FakeSlack()
        with patch("slack_sdk.WebClient", return_value=fake):
            out = td._execute_claimed_slack_dm({
                "recipient_id": TOMMY, "display_name": "Tommy Tucker",
                "message": "hi", "entity": "LEX-LLC"}, USER)
        assert fake.sent == []
        assert "Nothing was sent" in out

    def test_a_person_linked_phi_message_refuses_at_preview_and_stashes_nothing(self):
        out = _preview(message="Pull Bob's ICD-10 diagnosis before the call")
        assert "PHI" in out
        assert _peek() is None

    def test_the_executor_re_screens_phi(self):
        """Defense in depth on the one-tap send path."""
        fake = _FakeSlack()
        with patch("slack_sdk.WebClient", return_value=fake):
            out = td._execute_claimed_slack_dm({
                "recipient_id": TOMMY, "display_name": "Tommy Tucker",
                "message": "his care plan says no transfers", "entity": "F3E"}, USER)
        assert fake.sent == []
        assert "PHI" in out

    def test_a_bare_program_name_is_NOT_treated_as_phi(self):
        """The 2026-08-07 precision lesson: is_phi_risk is an INGESTION screen and
        over-refuses request-shaped text on a bare 'AHCCCS'. This path uses the
        person-linked predicate instead."""
        out = _preview(message="Can you send me the AHCCCS policy summary?")
        assert "PHI" not in out
        assert _peek() is not None


class TestRegistration:
    def test_the_kind_is_registered_everywhere_it_has_to_be(self):
        assert "slack_dm" in td._stash_kind_specs()
        assert "slack_dm" in td._defer_to_model_kinds()
        assert "slack_dm" in td._PENDING_KIND_LABELS

    def test_flag_off_leaves_the_preview_text_button_free(self, monkeypatch):
        monkeypatch.setenv("CORA_CONFIRM_BUTTONS", "off")
        out = _preview()
        assert "tap *Confirm*" not in out
        assert "@mention me with" in out
