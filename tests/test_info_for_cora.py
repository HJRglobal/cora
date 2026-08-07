"""Tests for the #info-for-cora message-event intake path (D1, 2026-06-13).

Messages posted in #info-for-cora are routed into the Harrison-gated
knowledge-review queue (knowledge_review.propose_update) so fed facts surface
in the next 7am review DM instead of being dropped. No canonical auto-write
(D-011); PHI is refused.

CONTRACT CHANGE 2026-08-06 (cq-f1236540b61e): this handler now delegates to the
shared info_intake chokepoint, and entity is derived from the CONTENT, not from
the asker's org-roles primary entity. #info-for-cora is a cross-entity intake
surface -- Harrison's primary entity is FNDR but his F3 Pure pricing note is an
F3E fact and belongs in known-answers/f3e.md. Content naming exactly one entity
wins; several named, or none, files under FNDR (several also sets an
ambiguous_entity flag). The PHI screen was also raised from the LEX-asker-scoped
billing check to an unconditional is_any_phi union -- strictly stricter, and the
over-refusal guard below still passes.
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
        # Content names no entity, so it files portfolio-wide -- NOT the poster's
        # F3E primary entity. Guessing a business entity from the author is exactly
        # what the 2026-08-06 contract forbids.
        assert kw["payload"]["entity"] == "FNDR"
        assert kw["payload"]["ambiguous_entity"] is False
        assert kw["payload"]["source"] == "info-for-cora"
        assert kw["payload"]["intake_route"] == "message_event"
        assert kw["confidence"] == "MED"
        assert client.chat_postMessage.called
        assert "review" in client.chat_postMessage.call_args.kwargs["text"].lower()

    def test_entity_comes_from_content_not_author(self):
        """Harrison (FNDR) posting an F3E fact must file under F3E."""
        client = MagicMock()
        harrison = app_module.org_roles.RoleRecord(
            slack_id="U_H", name="Harrison Rogers", role="Founder", entity="FNDR")
        with patch.object(app_module.org_roles, "get_role", return_value=harrison), \
             patch.object(app_module.knowledge_review, "load_proposed_updates", return_value=[]), \
             patch.object(app_module.knowledge_review, "propose_update") as prop:
            app_module._handle_info_for_cora(
                _event(text="F3 Pure retail price is $36.99 everywhere", user="U_H"),
                client)
        assert prop.call_args.kwargs["payload"]["entity"] == "F3E"

    def test_connector_post_footer_stripped_and_flagged(self):
        """The Cowork connector's un-@-mentioned posts carry no bot_id -- they are
        human contributions and must survive the guard with the footer removed."""
        client = MagicMock()
        with patch.object(app_module.org_roles, "get_role", return_value=None), \
             patch.object(app_module.knowledge_review, "load_proposed_updates", return_value=[]), \
             patch.object(app_module.knowledge_review, "propose_update") as prop:
            app_module._handle_info_for_cora(
                _event(text="F3 Pure retail price is $36.99 *Sent using* <@U0B3V5RHT3P>",
                       user="U_H"),
                client)
        payload = prop.call_args.kwargs["payload"]
        assert payload["connector_relayed"] is True
        assert "Sent using" not in payload["text"]

    def test_qa_prefixed_message_quarantined(self):
        client = MagicMock()
        with patch.object(app_module.org_roles, "get_role", return_value=None), \
             patch.object(app_module.knowledge_review, "load_proposed_updates", return_value=[]), \
             patch.object(app_module.knowledge_review, "propose_update") as prop:
            app_module._handle_info_for_cora(
                _event(text="[QA] smoke test -- please ignore"), client)
        assert not prop.called

    def test_question_is_not_queued_as_a_fact(self):
        """Every one of Hannah's 5/28-6/17 posts here was a question."""
        client = MagicMock()
        with patch.object(app_module.org_roles, "get_role", return_value=None), \
             patch.object(app_module.knowledge_review, "load_proposed_updates", return_value=[]), \
             patch.object(app_module.knowledge_review, "propose_update") as prop:
            app_module._handle_info_for_cora(
                _event(text="Where are the insurance cards for the new Lariat?"), client)
        assert not prop.called
        assert not client.chat_postMessage.called

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

    def test_phi_strictness_direction_non_lex_author_still_refused(self):
        """R7(c): the STRICTNESS DIRECTION of the parity-raise was unpinned.

        The old path applied is_lex_billing_status_phi only for a LEX-entity
        ASKER; the raise made the union unconditional. This test uses the REAL
        predicates (no wholesale phi_guard patching) with a NON-LEX author, so it
        fails if the screen ever reverts to asker-scoped.
        """
        client = MagicMock()
        f3e_role = app_module.org_roles.RoleRecord(
            slack_id="U_TOMMY", name="Tommy Anderson", role="Sales", entity="F3E")
        with patch.object(app_module.org_roles, "get_role", return_value=f3e_role), \
             patch.object(app_module.knowledge_review, "load_proposed_updates", return_value=[]), \
             patch.object(app_module.knowledge_review, "propose_update") as prop:
            app_module._handle_info_for_cora(
                _event(text="Bob Smith's billing authorization is pending",
                       user="U_TOMMY"),
                client)
        assert not prop.called
        assert "EHR" in client.chat_postMessage.call_args.kwargs["text"]

    def test_non_lex_business_authorization_not_over_refused(self):
        # The over-refusal guard for the unconditional is_any_phi union: a
        # company-named PO authorization carries an admin term but no possessive
        # personal name, so it must still flow. (The comment previously said the
        # augmentation was "scoped to LEX askers" -- stale since the parity-raise.)
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

    def test_clinical_phi_refused_non_lex_poster(self):
        # MF-1 (WS17-C): is_phi_risk misses the clinical class (diagnosis/meds);
        # is_clinical_phi must catch it at intake even for a NON-LEX poster, so it
        # never reaches the ledger or the Haiku enrichment. is_phi_risk is forced
        # False to prove is_clinical_phi is the catcher.
        client = MagicMock()
        f3e_role = app_module.org_roles.RoleRecord(
            slack_id="U_TOMMY", name="Tommy Anderson", role="Sales", entity="F3E")
        with patch.object(app_module.phi_guard, "is_phi_risk", return_value=False), \
             patch.object(app_module.org_roles, "get_role", return_value=f3e_role), \
             patch.object(app_module.knowledge_review, "propose_update") as prop:
            app_module._handle_info_for_cora(
                _event(text="Our participant was diagnosed with autism and is on risperidone",
                       user="U_TOMMY"),
                client)
        assert not prop.called
        assert "EHR" in client.chat_postMessage.call_args.kwargs["text"]


class TestMentionIntakeRoute:
    """Route 1 of 3 -- the @mention path, the ONLY one that fires in this channel
    today (channel `message` events never reach the app). Added 2026-08-06."""

    @staticmethod
    def _run(text, client=None, ts="1700000000.5"):
        say, client = MagicMock(), client or MagicMock()
        event = {"channel": app_module.INFO_FOR_CORA_CHANNEL_ID, "user": "U_H",
                 "ts": ts, "text": f"<@UBOT> {text}"}
        with patch.object(app_module.rate_limiter, "check", return_value=(True, None)), \
             patch.object(app_module, "_resolve_channel_name", return_value="info-for-cora"), \
             patch.object(app_module, "_resolve_bot_user_id"), \
             patch.object(app_module.org_roles, "get_role", return_value=None), \
             patch.object(app_module.knowledge_review, "load_proposed_updates", return_value=[]), \
             patch.object(app_module.knowledge_review, "propose_update") as prop, \
             patch.object(app_module, "_dispatch_qa") as dispatch:
            app_module.handle_mention(event, say, client)
        return prop, dispatch, say

    def test_statement_is_queued_and_not_answered(self):
        prop, dispatch, say = self._run("The Tucson stove vendor is Apex Appliance")
        assert prop.called
        assert prop.call_args.kwargs["payload"]["intake_route"] == "mention"
        assert not dispatch.called          # a contribution is not a question
        assert say.called                   # contributor gets a threaded ack
        assert "review" in say.call_args.kwargs["text"].lower()

    def test_question_still_reaches_qa(self):
        """Hannah's real usage: questions must keep working, never be filed as facts."""
        prop, dispatch, _ = self._run("Where are the insurance cards for the new Lariat?")
        assert not prop.called
        assert dispatch.called

    def test_qa_prefixed_is_quarantined_not_queued_not_answered(self):
        prop, dispatch, say = self._run("[QA] smoke test -- ignore")
        assert not prop.called
        assert not dispatch.called
        assert "quarantined" in say.call_args.kwargs["text"].lower()

    def test_note_prefix_is_unwrapped(self):
        prop, _, _ = self._run("note: The Tucson stove vendor is Apex Appliance")
        assert prop.called
        assert prop.call_args.kwargs["payload"]["text"].startswith("The Tucson")

    def test_other_channels_are_untouched(self):
        """The intake branch must be scoped to #info-for-cora only. Asserted on
        info_intake.ingest directly rather than on _dispatch_qa: reaching dispatch
        in another channel depends on the whole guard trio, which is not what this
        test is about -- the claim is simply that intake does not fire elsewhere."""
        say, client = MagicMock(), MagicMock()
        event = {"channel": "C_OTHER", "user": "U_H", "ts": "1.1",
                 "text": "<@UBOT> The Tucson stove vendor is Apex Appliance"}
        with patch.object(app_module.rate_limiter, "check", return_value=(True, None)), \
             patch.object(app_module, "_resolve_channel_name", return_value="f3e-sales"), \
             patch.object(app_module, "_resolve_bot_user_id"), \
             patch.object(app_module.team_learning, "parse_note", return_value=None), \
             patch.object(app_module.info_intake, "ingest") as ingest:
            app_module.handle_mention(event, say, client)
        assert not ingest.called
