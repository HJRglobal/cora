"""app.py Socket-Mode one-tap approve/dismiss wiring (2026-07-09 write-path).

The suite's usual blind spot (W7-01) is exactly handler->guard wiring: a green
unit suite for process_one_tap_action doesn't prove the @app.action wrapper
routes the click, enforces the Harrison gate at the Slack boundary, and updates
the DM in place. These tests drive the wrapper with a fake Slack client.
"""

from __future__ import annotations

import json

import pytest

try:
    from cora import app as capp
    from cora import knowledge_review as kr
    _OK = True
except Exception:  # noqa: BLE001
    _OK = False

pytestmark = pytest.mark.skipif(not _OK, reason="cora.app import unavailable")

HARRISON = "U0B2RM2JYJ1"
OTHER = "U_NOT_HARRISON"


class _FakeClient:
    def __init__(self):
        self.updated: list[dict] = []
        self.ephemeral: list[dict] = []

    def chat_update(self, **kw):
        self.updated.append(kw)
        return {"ok": True}

    def chat_postEphemeral(self, **kw):
        self.ephemeral.append(kw)
        return {"ok": True}


def _seed(tmp_path, monkeypatch, *, update_id="ka-1", answer="Reno hub ships DTC."):
    ledger = tmp_path / "updates.jsonl"
    entry = {
        "update_id": update_id, "update_type": "known_answer", "description": "d",
        "payload": {"gap_ts": "2026-07-01T00:00:00+00:00", "entity": "F3E",
                    "question": "how does F3E ship?", "gap": "not in KB",
                    "answer": answer, "answer_source": "slack_kb"},
        "source_evidence": "", "confidence": "HIGH", "state": "PENDING",
        "proposed_at": "2026-07-08T00:00:00+00:00", "resolved_at": None,
        "dm_message_ts": "1780000000.0001", "dm_channel_id": "D1",
    }
    ledger.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    monkeypatch.setattr(kr, "_PROPOSED_UPDATES_PATH", ledger)
    monkeypatch.setattr(kr, "_REPLY_LOG_PATH", tmp_path / "reply-log.jsonl")
    monkeypatch.setenv("KNOWN_ANSWERS_DIR", str(tmp_path / "known-answers"))
    monkeypatch.setenv("RESOLVED_GAPS_PATH", str(tmp_path / "resolved.jsonl"))
    return ledger


def _body(update_id, user_id):
    return {
        "user": {"id": user_id},
        "channel": {"id": "D1"},
        "message": {"ts": "1780000000.0001", "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "Q: ... A: ..."}},
            {"type": "actions", "block_id": f"kr_actions_{update_id}", "elements": [
                {"type": "button", "action_id": kr.ACTION_APPROVE, "value": update_id},
                {"type": "button", "action_id": kr.ACTION_DISMISS, "value": update_id},
            ]},
        ]},
        "actions": [{"action_id": kr.ACTION_APPROVE, "value": update_id}],
    }


def _state(ledger, update_id="ka-1"):
    for l in ledger.read_text(encoding="utf-8").splitlines():
        if l.strip() and json.loads(l).get("update_id") == update_id:
            return json.loads(l).get("state")
    return None


def test_harrison_click_writes_and_updates_message_in_place(tmp_path, monkeypatch):
    ledger = _seed(tmp_path, monkeypatch)
    fake = _FakeClient()
    capp._handle_knowledge_one_tap(_body("ka-1", HARRISON), fake, approve=True)
    assert _state(ledger) == "APPROVED"
    assert (tmp_path / "known-answers" / "f3e.md").exists()
    assert len(fake.updated) == 1 and not fake.ephemeral
    upd = fake.updated[0]
    assert "Saved" in upd["text"]
    # buttons dropped on update -> no actions block remains
    assert all(b.get("type") != "actions" for b in upd["blocks"])
    # original item text preserved as a section
    assert any(b.get("type") == "section" for b in upd["blocks"])


def test_non_harrison_click_is_refused_no_write_no_update(tmp_path, monkeypatch):
    ledger = _seed(tmp_path, monkeypatch)
    fake = _FakeClient()
    capp._handle_knowledge_one_tap(_body("ka-1", OTHER), fake, approve=True)
    assert _state(ledger) == "PENDING"                      # untouched
    assert not (tmp_path / "known-answers").exists()         # no write
    assert not fake.updated                                  # DM not rewritten
    assert len(fake.ephemeral) == 1                          # refusal shown to actor


def test_dismiss_click_updates_message_without_writing(tmp_path, monkeypatch):
    ledger = _seed(tmp_path, monkeypatch)
    fake = _FakeClient()
    capp._handle_knowledge_one_tap(_body("ka-1", HARRISON), fake, approve=False)
    assert _state(ledger) == "DISMISSED"
    assert not (tmp_path / "known-answers").exists()
    assert len(fake.updated) == 1 and "Dismissed" in fake.updated[0]["text"]
