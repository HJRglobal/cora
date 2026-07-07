"""W1-02: _handle_reaction gates non-Cora-authored messages with ONE check.

A redundant second `item_user != bot_user_id` gate (a verbatim re-fetch +
re-check that could never change the outcome) was removed. The single gate near
the top must still stop every capture handler for a reaction on a message NOT
authored by Cora, and let reactions on Cora's own messages through. These drive
the real handler so the removal can't silently open the gate.
"""

from unittest.mock import MagicMock, patch

import cora.app as app_module


CORA_BOT = "UCORABOT"


def _reaction_event(item_user, reaction="+1", reactor="U_HARRISON",
                    channel="C1", ts="1.1"):
    return {
        "user": reactor,
        "reaction": reaction,
        "item": {"type": "message", "channel": channel, "ts": ts},
        "item_user": item_user,
    }


def test_reaction_on_non_cora_message_is_gated():
    # feedback_log.log_reaction runs (unconditionally) only AFTER the gate, so
    # it is a clean probe: not called => the gate returned early.
    with patch.object(app_module, "_resolve_bot_user_id", return_value=CORA_BOT), \
         patch.object(app_module, "_resolve_channel_name", return_value="f3e-sales"), \
         patch.object(app_module.feedback_log, "log_reaction") as fb:
        app_module._handle_reaction(
            _reaction_event(item_user="U_SOMEONE_ELSE"), MagicMock(), "reaction_added")
    fb.assert_not_called()


def test_reaction_missing_item_user_is_gated():
    # A reaction whose item has no author (item_user "") is not Cora's -> gated.
    with patch.object(app_module, "_resolve_bot_user_id", return_value=CORA_BOT), \
         patch.object(app_module, "_resolve_channel_name", return_value="f3e-sales"), \
         patch.object(app_module.feedback_log, "log_reaction") as fb:
        app_module._handle_reaction(
            _reaction_event(item_user=""), MagicMock(), "reaction_added")
    fb.assert_not_called()


def test_reaction_on_cora_message_passes_gate():
    # A neutral reaction on Cora's own message skips the HubSpot (+1/-1), OSN
    # (white_check_mark) and knowledge-review (Harrison) branches and reaches
    # the post-gate feedback logging.
    with patch.object(app_module, "_resolve_bot_user_id", return_value=CORA_BOT), \
         patch.object(app_module, "_resolve_channel_name", return_value="f3e-sales"), \
         patch.object(app_module, "route", return_value="F3E"), \
         patch.object(app_module.feedback_log, "classify_sentiment", return_value="neutral"), \
         patch.object(app_module.feedback_log, "log_reaction") as fb:
        app_module._handle_reaction(
            _reaction_event(item_user=CORA_BOT, reaction="tada", reactor="U_X"),
            MagicMock(), "reaction_added")
    fb.assert_called_once()
