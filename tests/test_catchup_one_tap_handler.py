"""app.py Socket-Mode wiring for the Missed-Message Catch-Up cards (Send/Skip/Edit).

Handler->processor wiring is the suite's usual blind spot: a green unit suite for
process_catchup_action doesn't prove the @app.action / @app.view wrappers route the
click, enforce the Harrison gate at the Slack boundary, open the edit modal, and
update the DM card in place. These drive the wrappers with a fake Slack client.
"""

from __future__ import annotations

import json

import pytest

try:
    from cora import app as capp
    from cora import missed_message_catchup as mmc
    _OK = True
except Exception:  # noqa: BLE001
    _OK = False

pytestmark = pytest.mark.skipif(not _OK, reason="cora.app import unavailable")

HARRISON = "U0B2RM2JYJ1"
OTHER = "U_NOT_HARRISON"
CID = "C1:500.0"


class _FakeClient:
    def __init__(self):
        self.updated: list[dict] = []
        self.ephemeral: list[dict] = []
        self.posted: list[dict] = []
        self.views: list[dict] = []

    def chat_update(self, **kw):
        self.updated.append(kw)
        return {"ok": True}

    def chat_postEphemeral(self, **kw):
        self.ephemeral.append(kw)
        return {"ok": True}

    def chat_postMessage(self, **kw):
        self.posted.append(kw)
        return {"ok": True, "ts": "999.9"}

    def views_open(self, **kw):
        self.views.append(kw)
        return {"ok": True}


@pytest.fixture(autouse=True)
def _ledger_and_no_register(tmp_path, monkeypatch):
    monkeypatch.setenv("MISSED_CATCHUP_LEDGER_PATH", str(tmp_path / "catchup.jsonl"))
    # Don't touch the real active_threads.db when a reply "posts".
    monkeypatch.setattr(capp.active_thread_store, "register", lambda *a, **k: None)
    yield


def _seed_pending(**over):
    row = dict(channel_id="C1", channel_name="f3e-sales", entity="F3E", tier="TIER_3",
               asker="UALICE", event_ts="500.0", reply_thread_ts="500.0",
               detection_tier="mention", draft_text="This is the drafted answer body here.",
               is_dm=False)
    row.update(over)
    mmc.record_row(CID, "pending", **row)


def _card_body(user_id, action_id, cid=CID):
    return {
        "user": {"id": user_id},
        "channel": {"id": "D_HARRISON"},
        "message": {"ts": "1780000000.0001", "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "Missed message ... draft ..."}},
            {"type": "actions", "block_id": f"catchup_actions_{cid}", "elements": [
                {"type": "button", "action_id": action_id, "value": cid},
            ]},
        ]},
        "actions": [{"action_id": action_id, "value": cid}],
        "trigger_id": "trig-123",
    }


def _noack():
    return lambda *a, **k: None


def test_send_click_posts_and_updates_card_in_place():
    _seed_pending()
    fake = _FakeClient()
    capp._handle_catchup_one_tap(_card_body(HARRISON, mmc.ACTION_SEND), fake, action="send")
    # Reply posted into the SOURCE channel/thread.
    assert len(fake.posted) == 1
    assert fake.posted[0]["channel"] == "C1" and fake.posted[0]["thread_ts"] == "500.0"
    # Card rewritten in place, buttons dropped.
    assert len(fake.updated) == 1
    assert all(b.get("type") != "actions" for b in fake.updated[0]["blocks"])
    assert mmc.latest_disposition(CID)["disposition"] == "sent"


def test_non_harrison_send_refused_no_post_no_rewrite():
    _seed_pending()
    fake = _FakeClient()
    capp._handle_catchup_one_tap(_card_body(OTHER, mmc.ACTION_SEND), fake, action="send")
    assert not fake.posted                  # nothing posted to the channel
    assert not fake.updated                 # Harrison's card not rewritten
    assert len(fake.ephemeral) == 1         # refusal shown to the clicker
    assert mmc.latest_disposition(CID)["disposition"] == "pending"  # untouched


def test_skip_click_updates_card_without_posting():
    _seed_pending()
    fake = _FakeClient()
    capp._handle_catchup_one_tap(_card_body(HARRISON, mmc.ACTION_SKIP), fake, action="skip")
    assert not fake.posted
    assert len(fake.updated) == 1
    assert mmc.latest_disposition(CID)["disposition"] == "skipped"


def test_edit_open_modal_for_harrison():
    _seed_pending()
    fake = _FakeClient()
    capp.handle_catchup_edit(_noack(), _card_body(HARRISON, mmc.ACTION_EDIT), fake)
    assert len(fake.views) == 1
    view = fake.views[0]["view"]
    assert view["callback_id"] == mmc.VIEW_EDIT_SUBMIT
    meta = json.loads(view["private_metadata"])
    assert meta["catchup_id"] == CID


def test_edit_open_refused_for_non_harrison():
    _seed_pending()
    fake = _FakeClient()
    capp.handle_catchup_edit(_noack(), _card_body(OTHER, mmc.ACTION_EDIT), fake)
    assert not fake.views
    assert len(fake.ephemeral) == 1


def test_edit_open_refused_when_not_pending():
    _seed_pending()
    mmc.record_row(CID, "sent")  # already terminal
    fake = _FakeClient()
    capp.handle_catchup_edit(_noack(), _card_body(HARRISON, mmc.ACTION_EDIT), fake)
    assert not fake.views
    assert len(fake.ephemeral) == 1


def test_edit_submit_posts_edited_text():
    _seed_pending()
    fake = _FakeClient()
    view = {
        "private_metadata": json.dumps({"catchup_id": CID, "dm_channel": "D_HARRISON", "dm_ts": "1.1"}),
        "state": {"values": {"catchup_edit_block": {"catchup_edit_input": {"value": "Harrison's edited reply text goes here now."}}}},
    }
    body = {"user": {"id": HARRISON}}
    capp.handle_catchup_edit_submit(_noack(), body, fake, view)
    assert len(fake.posted) == 1
    assert "edited reply text" in fake.posted[0]["text"]
    assert mmc.latest_disposition(CID)["disposition"] == "edited_sent"


def test_action_ids_registered():
    # The constants the buttons carry must match what the module builds.
    assert mmc.ACTION_SEND == "catchup_send"
    assert mmc.ACTION_SKIP == "catchup_skip"
    assert mmc.ACTION_EDIT == "catchup_edit"
    assert mmc.VIEW_EDIT_SUBMIT == "catchup_edit_submit"
    # Handlers exist on the app module.
    for name in ("handle_catchup_send", "handle_catchup_skip",
                 "handle_catchup_edit", "handle_catchup_edit_submit"):
        assert callable(getattr(capp, name))
