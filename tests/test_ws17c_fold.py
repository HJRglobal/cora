"""WS17-C — team-contribution fold into the one Harrison-gated knowledge queue.

The #cora-kq approval card + per-entity-approver path is RETIRED. A confirmed
note / bookmark / correction now proposes a GENERIC update (source='info-for-cora')
into knowledge_review's single queue and, on Harrison's 👍, writes to
known-answers via apply_contributed_note. PHI is refused at intake. These tests
cover the handler wiring (the executor + provenance live in test_gap_autofill.py).
"""

from unittest.mock import MagicMock

import pytest

import cora.app as app_module


F3E_CH = "C_F3E_LEADERSHIP"
TOMMY = "U_TOMMY_TEST"


def _pending(**over):
    p = {
        "author": TOMMY,
        "entity": "F3E",
        "channel_name": "f3e-leadership",
        "kind": "note",
        "raw_content": "Tommy said the Anaheim warehouse moved.",
        "paraphrase": "The Anaheim warehouse relocated to 500 Brand Blvd.",
        "created_at": 0,
        "preview_msg_ts": None,
    }
    p.update(over)
    return p


def _confirm_event(text="yes", thread="100.1", ts="200.2"):
    # A non-DM thread reply (channel_type absent -> not the "im" DM branch).
    return {"channel": F3E_CH, "user": TOMMY, "thread_ts": thread, "ts": ts, "text": text}


@pytest.fixture
def fold_env(monkeypatch):
    """Reach the Path-0 confirm branch with a mocked client and stubbed helpers."""
    monkeypatch.setattr(app_module, "_resolve_channel_name", lambda c, cid: "f3e-leadership")
    monkeypatch.setattr(app_module.team_learning, "clear_pending_confirm", lambda *a, **k: None)
    monkeypatch.setattr(app_module.org_roles, "get_role", lambda uid: None)
    monkeypatch.setattr(app_module.knowledge_review, "load_proposed_updates", lambda: [])
    propose = MagicMock(return_value=True)
    monkeypatch.setattr(app_module.knowledge_review, "propose_update", propose)
    return propose


def test_confirm_yes_folds_to_knowledge_queue(monkeypatch, fold_env):
    monkeypatch.setattr(app_module.team_learning, "get_pending_confirm",
                        lambda cid, tts: _pending())
    app_module.handle_message_event(_confirm_event(), MagicMock())

    fold_env.assert_called_once()
    kwargs = fold_env.call_args.kwargs
    assert kwargs["update_type"] == app_module.knowledge_review.UPDATE_TYPE_GENERIC
    assert kwargs["update_id"] == "teamnote-100.1"
    assert kwargs["confidence"] == "MED"
    payload = kwargs["payload"]
    assert payload["source"] == "info-for-cora"   # rides the existing knowledge branch
    assert payload["kind"] == "note"
    assert payload["entity"] == "F3E"
    assert payload["channel"] == "f3e-leadership"
    # The author-confirmed PARAPHRASE is stored (not the raw note).
    assert payload["text"] == "The Anaheim warehouse relocated to 500 Brand Blvd."


def test_confirm_yes_phi_refused(monkeypatch, fold_env):
    # A clinical-PHI paraphrase must NOT be proposed (is_clinical_phi catches the
    # diagnosis/medication class is_phi_risk misses).
    monkeypatch.setattr(
        app_module.team_learning, "get_pending_confirm",
        lambda cid, tts: _pending(
            paraphrase="The client was diagnosed with autism and started risperidone.",
            raw_content="client diagnosed with autism, on risperidone",
        ),
    )
    app_module.handle_message_event(_confirm_event(), MagicMock())
    fold_env.assert_not_called()


def test_confirm_yes_idempotent_on_retry(monkeypatch, fold_env):
    # A re-delivered "yes" whose deterministic update_id is already queued must
    # not re-propose (mirrors _handle_info_for_cora's dedup).
    monkeypatch.setattr(app_module.team_learning, "get_pending_confirm",
                        lambda cid, tts: _pending())
    monkeypatch.setattr(app_module.knowledge_review, "load_proposed_updates",
                        lambda: [{"update_id": "teamnote-100.1"}])
    app_module.handle_message_event(_confirm_event(), MagicMock())
    fold_env.assert_not_called()


def test_bookmark_folds_to_knowledge_queue(monkeypatch, fold_env):
    monkeypatch.setattr(app_module.team_learning, "is_authorized_contributor",
                        lambda uid, ent: True)
    monkeypatch.setattr(app_module.team_learning, "screen_contribution",
                        lambda c: (True, ""))
    monkeypatch.setattr(app_module, "route", lambda name: "F3E")
    client = MagicMock()
    client.reactions_get.return_value = {"message": {"text": "Sprouts reorder is every 2 weeks."}}
    app_module._handle_bookmark_reaction(
        client=client, reactor=TOMMY, channel_id=F3E_CH,
        channel_name="f3e-leadership", message_ts="300.3",
    )
    fold_env.assert_called_once()
    kwargs = fold_env.call_args.kwargs
    assert kwargs["update_id"] == "bookmark-300.3"
    payload = kwargs["payload"]
    assert payload["source"] == "info-for-cora"
    assert payload["kind"] == "bookmark"
    assert payload["text"] == "Sprouts reorder is every 2 weeks."


def test_bookmark_phi_refused(monkeypatch, fold_env):
    monkeypatch.setattr(app_module.team_learning, "is_authorized_contributor",
                        lambda uid, ent: True)
    monkeypatch.setattr(app_module.team_learning, "screen_contribution",
                        lambda c: (True, ""))
    monkeypatch.setattr(app_module, "route", lambda name: "F3E")
    client = MagicMock()
    client.reactions_get.return_value = {
        "message": {"text": "The client was diagnosed with bipolar disorder, on lithium."}
    }
    app_module._handle_bookmark_reaction(
        client=client, reactor=TOMMY, channel_id=F3E_CH,
        channel_name="f3e-leadership", message_ts="301.3",
    )
    fold_env.assert_not_called()


def test_retired_symbols_gone():
    # The #cora-kq approval card + per-entity-approver path is fully removed.
    for name in ("_process_contribution_reaction", "_queue_contribution",
                 "_resolve_queue_channel_id"):
        assert not hasattr(app_module, name), f"app.{name} should be gone (WS17-C)"
    import cora.team_learning as tl
    for name in ("ingest_contribution", "store_contribution", "build_approval_card",
                 "set_approval_msg", "lookup_by_approval_ts", "resolve_contribution",
                 "is_approver", "get_queue_channel", "kq_channel_for_entity",
                 "APPROVAL_CHANNEL", "KB_AUDIT_CHANNEL", "pending_stats"):
        assert not hasattr(tl, name), f"team_learning.{name} should be gone (WS17-C)"
    # The author-side primitives must survive the fold.
    for name in ("screen_contribution", "paraphrase_note", "store_pending_confirm",
                 "get_pending_confirm", "clear_pending_confirm", "is_authorized_contributor",
                 "parse_note", "is_correction", "is_confirmation"):
        assert hasattr(tl, name), f"team_learning.{name} must survive the fold"
