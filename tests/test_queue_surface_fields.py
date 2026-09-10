"""S3'(b)+(c) (Code #12, cq-deca62a00719 / cq-0f8abd3c1981): every queue surface
says, per item, whether a kickoff exists, whether a card was ever posted, and how
to stage it -- and the MCP seed tool's success text no longer points at a Stage
button a seed does not have.
"""

from __future__ import annotations

import pytest

from cora import code_queue as cq
from cora import mcp_server

HARRISON = "U0B2RM2JYJ1"


@pytest.fixture
def qenv(tmp_path, monkeypatch):
    monkeypatch.setattr(cq, "_EVENT_LEDGER", tmp_path / "code-session-queue.jsonl")
    monkeypatch.setattr(cq, "_FINGERPRINT_LEDGER", tmp_path / "fp.jsonl")
    monkeypatch.setattr(cq, "_SIGNALS_LEDGER", tmp_path / "sig.jsonl")
    monkeypatch.setattr(cq, "_NOTES_DIR", tmp_path / "_notes")
    monkeypatch.setenv("FOUNDER_OS_ROOT", str(tmp_path / "founder-os"))
    monkeypatch.setenv("CORA_CODE_QUEUE", "log")
    monkeypatch.setattr(cq, "HARRISON_ID", HARRISON)
    monkeypatch.setattr(cq, "_SYNC", True)

    def _plain_write(path, text, **kw):
        from pathlib import Path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(text, encoding="utf-8")
    monkeypatch.setattr(cq.drive_io, "write_text_atomic", _plain_write)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setattr(cq, "_default_embed", lambda texts: [])
    cq._STAGING_INFLIGHT.clear()
    return tmp_path


class TestSurfaceFields:
    def test_proposed_no_card(self, qenv):
        cid = cq.seed_item(kind="bug", severity="P3", title="A", summary="s", entity="F3E",
                           signal="explicit", status="PROPOSED")
        f = cq.item_surface_fields(cq.get_item(cid))
        # no card -> no 'tap Queue' offer (D-051 lens F MED #2); the verb names WHO
        # types it (lens A LOW #12: the line is read on the KB-ingested backlog by anyone)
        assert f == {"kickoff": False, "card": False,
                     "how_to_stage": f"approve first (Harrison replies `approve {cid}` in a Cora DM "
                                     f"-- no card, a seeded item), then Harrison replies `stage {cid}`"}

    def test_proposed_with_card_offers_the_queue_tap(self, qenv):
        cid = cq.seed_item(kind="bug", severity="P3", title="A2", summary="s", entity="F3E",
                           signal="explicit", status="PROPOSED")
        cq._append_event({"event": "dm_sent", "ts": cq._now_iso(), "id": cid,
                          "dm_channel_id": "D1", "dm_message_ts": "1.2"})
        f = cq.item_surface_fields(cq.get_item(cid))
        assert f["card"] is True
        assert f["how_to_stage"].startswith("approve first (tap Queue on its card, or Harrison replies")
        assert "no card" not in f["how_to_stage"]

    def test_approved_seed_without_card_names_the_verb_and_step_75(self, qenv):
        cid = cq.seed_item(kind="bug", severity="P3", title="B", summary="s", entity="F3E",
                           signal="explicit", status="APPROVED")
        f = cq.item_surface_fields(cq.get_item(cid))
        assert f["kickoff"] is False and f["card"] is False
        assert f"Harrison replies `stage {cid}` in a Cora DM" in f["how_to_stage"]
        assert "no card" in f["how_to_stage"] and "step 7.5" in f["how_to_stage"]

    def test_approved_with_card_offers_the_button_too(self, qenv):
        cid = cq.seed_item(kind="bug", severity="P3", title="C", summary="s", entity="F3E",
                           signal="explicit", status="APPROVED")
        cq._append_event({"event": "dm_sent", "ts": cq._now_iso(), "id": cid,
                          "dm_channel_id": "D1", "dm_message_ts": "1.2"})
        f = cq.item_surface_fields(cq.get_item(cid))
        assert f["card"] is True and "taps Stage prompt on its card" in f["how_to_stage"]

    def test_staged_and_terminal(self, qenv):
        cid = cq.seed_item(kind="bug", severity="P2", title="D", summary="s", entity="F3E",
                           signal="explicit", status="APPROVED")
        cq.stage_by_id(cid, HARRISON)
        f = cq.item_surface_fields(cq.get_item(cid))
        assert f["kickoff"] is True and "already staged" in f["how_to_stage"]
        cq._append_event({"event": "shipped", "ts": cq._now_iso(), "id": cid, "bundle_id": "b"})
        assert cq.item_surface_fields(cq.get_item(cid))["how_to_stage"] == "closed (SHIPPED)"

    def test_backlog_render_carries_the_line_per_item(self, qenv):
        cid = cq.seed_item(kind="bug", severity="P3", title="E", summary="s", entity="F3E",
                           signal="explicit", status="APPROVED")
        text = cq.render_backlog_text()
        assert f"kickoff: no · card: no · how-to-stage: Harrison replies `stage {cid}` in a Cora DM" in text

    def test_item_card_carries_the_line_as_this_message(self, qenv):
        cid = cq.seed_item(kind="bug", severity="P3", title="F", summary="s", entity="F3E",
                           signal="explicit", status="APPROVED")
        text, _blocks = cq.build_item_card(cq.get_item(cid))
        assert "kickoff: no · card: this message · how-to-stage:" in text


class TestMcpSeedMessage:
    def test_message_says_no_card_and_names_the_verb_never_tap_stage(self, qenv):
        out = mcp_server.code_queue_seed("bug", "HIGH", "seed msg fixture", "summary text",
                                         "F3E", status="APPROVED")
        assert out["seeded"] is True and out["card"] is False
        assert out["kickoff_missing"] is True
        msg = out["message"]
        assert "No DM card is posted for a seed" in msg
        assert f"stage {out['id']}" in msg and "step 7.5" in msg
        assert "Tap Stage" not in msg

    def test_stage_now_generates_the_kickoff_for_a_priority_approved_seed(self, qenv):
        out = mcp_server.code_queue_seed("bug", "HIGH", "seed stage_now fixture", "summary",
                                         "F3E", status="APPROVED", stage_now=True)
        assert out["kickoff_missing"] is False
        assert "Kickoff generated inline" in out["message"]
        assert cq.get_item(out["id"])["status"] == "STAGED"

    def test_stage_now_on_a_non_priority_seed_is_explained_not_silent(self, qenv):
        out = mcp_server.code_queue_seed("bug", "LOW", "seed low fixture", "summary",
                                         "F3E", status="APPROVED", stage_now=True)
        assert cq.get_item(out["id"])["status"] == "APPROVED"
        assert "stage_now applies only to P0/P1-class seeds" in out["message"]

    def test_proposed_seed_points_at_the_monday_coverage(self, qenv):
        out = mcp_server.code_queue_seed("bug", "LOW", "seed proposed fixture", "summary", "F3E")
        assert "PROPOSED coverage" in out["message"] and f"approve {out['id']}" in out["message"]

    def test_tool_spec_exposes_stage_now_and_passes_it(self, qenv, monkeypatch):
        captured = {}
        monkeypatch.setattr(cq, "seed_item", lambda **kw: captured.update(kw) or "cq-000000000001")
        spec = next(s for s in mcp_server._TOOL_SPECS if s["name"] == "cora_code_queue_seed")
        assert spec["input_schema"]["properties"]["stage_now"]["type"] == "boolean"
        spec["fn"]({"kind": "gap", "severity": "HIGH", "title": "t", "summary": "s",
                    "entity": "F3E", "status": "APPROVED", "stage_now": True})
        assert captured["stage_now"] is True
