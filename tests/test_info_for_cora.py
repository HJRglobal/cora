"""Tests for the #info-for-cora intake path (D1, 2026-06-13).

Messages posted in #info-for-cora are routed into the Harrison-gated
knowledge-review queue (knowledge_review.propose_update) so fed facts surface
in the next 7am review DM instead of being dropped. No canonical auto-write
(D-011); PHI is refused; entity = the asker's org-roles primary entity.
"""

from unittest.mock import MagicMock, patch

import pytest

import cora.app as app_module


def _event(text="The Tucson stove vendor is Apex Appliance", user="U_TOMMY",
           ts="1700000000.0001", subtype=None, bot_id=None, thread_ts=None):
    e = {"channel": app_module.INFO_FOR_CORA_CHANNEL_ID, "user": user,
         "text": text, "ts": ts}
    if subtype:
        e["subtype"] = subtype
    if bot_id:
        e["bot_id"] = bot_id
    if thread_ts:
        e["thread_ts"] = thread_ts
    return e


def _role(name="Tommy Anderson", entity="F3E"):
    return app_module.org_roles.RoleRecord(
        slack_id="U_TOMMY", name=name, role="Sales", entity=entity)


class TestInfoForCoraIntake:
    def test_normal_fact_proposed_and_acked(self):
        client = MagicMock()
        with patch.object(app_module.phi_guard, "is_phi_risk", return_value=False), \
             patch.object(app_module.org_roles, "get_role", return_value=_role()), \
             patch.object(app_module.knowledge_review, "load_proposed_updates", return_value=[]), \
             patch.object(app_module.knowledge_review, "propose_update") as prop:
            app_module._handle_info_for_cora(_event(), client)
        assert prop.called
        kw = prop.call_args.kwargs
        assert kw["update_type"] == app_module.knowledge_review.UPDATE_TYPE_GENERIC
        assert kw["payload"]["entity"] == "F3E"
        assert kw["payload"]["source"] == "info-for-cora"
        assert kw["confidence"] == "MED"
        assert client.chat_postMessage.called
        assert "review" in client.chat_postMessage.call_args.kwargs["text"].lower()

    def test_phi_refused_not_proposed(self):
        client = MagicMock()
        with patch.object(app_module.phi_guard, "is_phi_risk", return_value=True), \
             patch.object(app_module.knowledge_review, "propose_update") as prop:
            app_module._handle_info_for_cora(
                _event(text="client Bob Smith's diagnosis is X"), client)
        assert not prop.called
        assert client.chat_postMessage.called
        assert "EHR" in client.chat_postMessage.call_args.kwargs["text"]

    def test_bot_message_ignored(self):
        client = MagicMock()
        with patch.object(app_module.knowledge_review, "propose_update") as prop:
            app_module._handle_info_for_cora(_event(bot_id="B123"), client)
        assert not prop.called
        assert not client.chat_postMessage.called

    def test_subtype_noise_ignored(self):
        client = MagicMock()
        with patch.object(app_module.knowledge_review, "propose_update") as prop:
            app_module._handle_info_for_cora(_event(subtype="channel_join"), client)
        assert not prop.called

    def test_empty_text_ignored(self):
        client = MagicMock()
        with patch.object(app_module.knowledge_review, "propose_update") as prop:
            app_module._handle_info_for_cora(_event(text="   "), client)
        assert not prop.called

    def test_unknown_user_falls_back_to_fndr(self):
        client = MagicMock()
        with patch.object(app_module.phi_guard, "is_phi_risk", return_value=False), \
             patch.object(app_module.org_roles, "get_role", return_value=None), \
             patch.object(app_module.knowledge_review, "load_proposed_updates", return_value=[]), \
             patch.object(app_module.knowledge_review, "propose_update") as prop:
            app_module._handle_info_for_cora(_event(user="U_UNKNOWN"), client)
        assert prop.call_args.kwargs["payload"]["entity"] == "FNDR"

    def test_idempotent_skip_on_duplicate_ts(self):
        client = MagicMock()
        existing = [{"update_id": "infocora-1700000000.0001"}]
        with patch.object(app_module.phi_guard, "is_phi_risk", return_value=False), \
             patch.object(app_module.org_roles, "get_role", return_value=None), \
             patch.object(app_module.knowledge_review, "load_proposed_updates", return_value=existing), \
             patch.object(app_module.knowledge_review, "propose_update") as prop:
            app_module._handle_info_for_cora(_event(ts="1700000000.0001"), client)
        assert not prop.called

    def test_lex_admin_phi_refused(self):
        # D-050 class: a LEX asker posting a named person's billing/authorization
        # must be refused even though is_phi_risk() alone returns False.
        client = MagicMock()
        lex_role = app_module.org_roles.RoleRecord(
            slack_id="U_SHAUN", name="Shaun Hawkins", role="GM", entity="LEX")
        with patch.object(app_module.org_roles, "get_role", return_value=lex_role), \
             patch.object(app_module.knowledge_review, "propose_update") as prop:
            app_module._handle_info_for_cora(
                _event(text="Bob Smith's billing authorization is pending", user="U_SHAUN"),
                client)
        assert not prop.called
        assert "EHR" in client.chat_postMessage.call_args.kwargs["text"]

    def test_non_lex_business_authorization_not_over_refused(self):
        # The LEX augmentation is scoped to LEX askers, so an F3E business fact
        # mentioning "authorization" is NOT over-refused.
        client = MagicMock()
        f3e_role = app_module.org_roles.RoleRecord(
            slack_id="U_TOMMY", name="Tommy Anderson", role="Sales", entity="F3E")
        with patch.object(app_module.org_roles, "get_role", return_value=f3e_role), \
             patch.object(app_module.knowledge_review, "load_proposed_updates", return_value=[]), \
             patch.object(app_module.knowledge_review, "propose_update") as prop:
            app_module._handle_info_for_cora(
                _event(text="American Discount Foods PO authorization is approved", user="U_TOMMY"),
                client)
        assert prop.called  # business fact, not PHI -> proposed
