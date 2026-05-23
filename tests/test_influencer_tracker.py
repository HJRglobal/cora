"""Unit tests for influencer_client and tool_dispatch influencer tools."""

import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import cora.tools.influencer_client as ic
import cora.tools.tool_dispatch as td


# ---------------------------------------------------------------------------
# Fixtures — use a temp DB for every test so they don't share state
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _tmp_db(tmp_path, monkeypatch):
    """Redirect all DB writes to a fresh temp file per test."""
    monkeypatch.setattr(ic, "_DB_PATH", tmp_path / "test_influencer.db")
    yield


_MOCK_MAP = {
    "U_ALEX": {"display_name": "Alex Cordova", "asana_email": "alex@hjrglobal.com", "asana_user_gid": "999"},
    "U_HARRISON": {"display_name": "Harrison", "asana_email": "harrison@hjrglobal.com", "asana_user_gid": "888"},
}


# ---------------------------------------------------------------------------
# influencer_client.add_deliverable
# ---------------------------------------------------------------------------

class TestAddDeliverable:
    def test_happy_path_returns_row(self):
        row = ic.add_deliverable(
            athlete_name="Luis Pena",
            platform="instagram",
            deliverable_type="reel",
            due_date="2026-06-01",
            entity="F3E",
        )
        assert row["id"] > 0
        assert row["athlete_name"] == "Luis Pena"
        assert row["platform"] == "instagram"
        assert row["deliverable_type"] == "reel"
        assert row["status"] == "pending"
        assert row["due_date"] == "2026-06-01"

    def test_missing_athlete_raises(self):
        with pytest.raises(ic.InfluencerClientError, match="athlete_name"):
            ic.add_deliverable(athlete_name="", platform="instagram", deliverable_type="post")

    def test_missing_platform_raises(self):
        with pytest.raises(ic.InfluencerClientError, match="platform"):
            ic.add_deliverable(athlete_name="Athlete X", platform="", deliverable_type="post")

    def test_missing_type_raises(self):
        with pytest.raises(ic.InfluencerClientError, match="deliverable_type"):
            ic.add_deliverable(athlete_name="Athlete X", platform="tiktok", deliverable_type="")

    def test_bad_due_date_raises(self):
        with pytest.raises(ic.InfluencerClientError, match="ISO date"):
            ic.add_deliverable(
                athlete_name="A",
                platform="instagram",
                deliverable_type="post",
                due_date="June 1st",
            )

    def test_platform_stored_lowercase(self):
        row = ic.add_deliverable(
            athlete_name="A", platform="Instagram", deliverable_type="Post"
        )
        assert row["platform"] == "instagram"
        assert row["deliverable_type"] == "post"

    def test_hubspot_deal_id_stored(self):
        row = ic.add_deliverable(
            athlete_name="A", platform="tiktok", deliverable_type="video",
            hubspot_deal_id="123456789",
        )
        assert row["hubspot_deal_id"] == "123456789"


# ---------------------------------------------------------------------------
# influencer_client.mark_complete
# ---------------------------------------------------------------------------

class TestMarkComplete:
    def test_marks_pending_as_complete(self):
        row = ic.add_deliverable(athlete_name="Fighter A", platform="instagram", deliverable_type="story")
        updated = ic.mark_complete(deliverable_id=row["id"], completion_link="https://www.instagram.com/p/abc")
        assert updated["status"] == "complete"
        assert updated["completion_link"] == "https://www.instagram.com/p/abc"

    def test_unknown_id_raises(self):
        with pytest.raises(ic.InfluencerClientError, match="not found"):
            ic.mark_complete(deliverable_id=9999)

    def test_already_complete_raises(self):
        row = ic.add_deliverable(athlete_name="B", platform="instagram", deliverable_type="post")
        ic.mark_complete(deliverable_id=row["id"])
        with pytest.raises(ic.InfluencerClientError, match="already complete"):
            ic.mark_complete(deliverable_id=row["id"])


# ---------------------------------------------------------------------------
# influencer_client.mark_waived
# ---------------------------------------------------------------------------

class TestMarkWaived:
    def test_marks_pending_as_waived(self):
        row = ic.add_deliverable(athlete_name="C", platform="tiktok", deliverable_type="reel")
        updated = ic.mark_waived(deliverable_id=row["id"], notes="Injured during training")
        assert updated["status"] == "waived"
        assert "Injured" in updated["notes"]

    def test_unknown_id_raises(self):
        with pytest.raises(ic.InfluencerClientError, match="not found"):
            ic.mark_waived(deliverable_id=7777)


# ---------------------------------------------------------------------------
# influencer_client.get_deliverables — overdue detection
# ---------------------------------------------------------------------------

class TestGetDeliverables:
    def test_overdue_row_gets_display_status(self):
        # Due in the past → should show as overdue
        row = ic.add_deliverable(
            athlete_name="Past Due", platform="instagram", deliverable_type="post",
            due_date="2020-01-01",  # definitely in the past
        )
        results = ic.get_deliverables()
        match = next(r for r in results if r["id"] == row["id"])
        assert match["display_status"] == "overdue"

    def test_future_due_stays_pending(self):
        row = ic.add_deliverable(
            athlete_name="Future Post", platform="tiktok", deliverable_type="video",
            due_date="2099-01-01",
        )
        results = ic.get_deliverables()
        match = next(r for r in results if r["id"] == row["id"])
        assert match["display_status"] == "pending"

    def test_complete_excluded_by_default(self):
        row = ic.add_deliverable(athlete_name="Done", platform="youtube", deliverable_type="video")
        ic.mark_complete(deliverable_id=row["id"])
        results = ic.get_deliverables(include_complete=False)
        assert not any(r["id"] == row["id"] for r in results)

    def test_entity_filter_applies(self):
        ic.add_deliverable(athlete_name="F3E Athlete", platform="instagram", deliverable_type="post", entity="F3E")
        ic.add_deliverable(athlete_name="UFL Fighter", platform="tiktok", deliverable_type="video", entity="UFL")
        f3e_rows = ic.get_deliverables(entity="F3E")
        ufl_rows = ic.get_deliverables(entity="UFL")
        assert all(r["entity"] == "F3E" for r in f3e_rows)
        assert all(r["entity"] == "UFL" for r in ufl_rows)

    def test_athlete_filter_partial_match(self):
        ic.add_deliverable(athlete_name="Johnny Walker", platform="instagram", deliverable_type="post")
        ic.add_deliverable(athlete_name="Luis Pena", platform="tiktok", deliverable_type="reel")
        results = ic.get_deliverables(athlete="johnny")
        assert len(results) == 1
        assert results[0]["athlete_name"] == "Johnny Walker"


# ---------------------------------------------------------------------------
# influencer_client.get_compliance_report
# ---------------------------------------------------------------------------

class TestGetComplianceReport:
    def test_compliance_calculation(self):
        # 2 deliverables: 1 complete, 1 pending
        r1 = ic.add_deliverable(athlete_name="Athlete Z", platform="instagram", deliverable_type="post")
        r2 = ic.add_deliverable(athlete_name="Athlete Z", platform="instagram", deliverable_type="story")
        ic.mark_complete(deliverable_id=r1["id"])
        report = ic.get_compliance_report()
        row = next(r for r in report if r["athlete_name"] == "Athlete Z")
        assert row["complete"] == 1
        assert row["total"] == 2
        assert row["compliance_pct"] == 50

    def test_waived_excluded_from_denominator(self):
        r1 = ic.add_deliverable(athlete_name="Waive Test", platform="instagram", deliverable_type="post")
        r2 = ic.add_deliverable(athlete_name="Waive Test", platform="instagram", deliverable_type="story")
        ic.mark_complete(deliverable_id=r1["id"])
        ic.mark_waived(deliverable_id=r2["id"])
        report = ic.get_compliance_report()
        row = next(r for r in report if r["athlete_name"] == "Waive Test")
        # 1 complete, 1 waived → denominator = 1 → 100%
        assert row["compliance_pct"] == 100


# ---------------------------------------------------------------------------
# tool_dispatch._tool_influencer_get_status
# ---------------------------------------------------------------------------

class TestToolInfluencerGetStatus:
    def _call(self, input_data: dict, user_id="U_ALEX", entity="F3E"):
        with patch("cora.tools.tool_dispatch._load_slack_asana_map", return_value=_MOCK_MAP):
            return td._tool_influencer_get_status(user_id, entity, input_data)

    def test_empty_tracker_returns_none_message(self):
        result = self._call({})
        assert "no open" in result.lower() or "nothing" in result.lower() or "caught up" in result.lower()

    def test_status_report_contains_athlete_name(self):
        ic.add_deliverable(athlete_name="Luis Pena", platform="instagram", deliverable_type="reel", entity="F3E")
        result = self._call({"report_type": "status"}, entity="F3E")
        assert "Luis Pena" in result

    def test_compliance_report_type(self):
        ic.add_deliverable(athlete_name="Test Fighter", platform="tiktok", deliverable_type="video", entity="F3E")
        result = self._call({"report_type": "compliance"}, entity="F3E")
        assert "Compliance" in result
        assert "Test Fighter" in result

    def test_overdue_report_filters(self):
        ic.add_deliverable(
            athlete_name="Overdue Guy", platform="instagram", deliverable_type="post",
            due_date="2020-01-01", entity="F3E",
        )
        ic.add_deliverable(
            athlete_name="Future Guy", platform="instagram", deliverable_type="post",
            due_date="2099-01-01", entity="F3E",
        )
        result = self._call({"report_type": "overdue"}, entity="F3E")
        assert "Overdue Guy" in result
        assert "Future Guy" not in result

    def test_athlete_filter_param(self):
        ic.add_deliverable(athlete_name="Alex Volkanovski", platform="instagram", deliverable_type="post", entity="F3E")
        ic.add_deliverable(athlete_name="Israel Adesanya", platform="instagram", deliverable_type="story", entity="F3E")
        result = self._call({"athlete": "volkanovski"}, entity="F3E")
        assert "Volkanovski" in result
        assert "Adesanya" not in result

    def test_fndr_entity_sees_all(self):
        ic.add_deliverable(athlete_name="F3E Star", platform="instagram", deliverable_type="post", entity="F3E")
        ic.add_deliverable(athlete_name="UFL Champ", platform="tiktok", deliverable_type="video", entity="UFL")
        result = self._call({}, user_id="U_HARRISON", entity="FNDR")
        assert "F3E Star" in result
        assert "UFL Champ" in result


# ---------------------------------------------------------------------------
# tool_dispatch._tool_influencer_log_deliverable
# ---------------------------------------------------------------------------

class TestToolInfluencerLogDeliverable:
    def _call(self, input_data: dict, user_id="U_ALEX", entity="F3E"):
        with patch("cora.tools.tool_dispatch._load_slack_asana_map", return_value=_MOCK_MAP):
            return td._tool_influencer_log_deliverable(user_id, entity, input_data)

    def test_refuses_without_confirmed(self):
        result = self._call({"action": "add", "athlete_name": "A", "platform": "instagram", "deliverable_type": "post"})
        assert "refused" in result.lower()

    def test_refuses_with_confirmed_false(self):
        result = self._call({
            "action": "add", "athlete_name": "A", "platform": "instagram",
            "deliverable_type": "post", "confirmed": False,
        })
        assert "refused" in result.lower()

    def test_unknown_action_returns_error(self):
        result = self._call({"action": "delete", "confirmed": True})
        assert "unknown action" in result.lower()

    def test_add_missing_athlete_returns_message(self):
        result = self._call({"action": "add", "platform": "instagram", "deliverable_type": "post", "confirmed": True})
        assert "athlete_name" in result.lower()

    def test_add_missing_platform_returns_message(self):
        result = self._call({"action": "add", "athlete_name": "A", "deliverable_type": "post", "confirmed": True})
        assert "platform" in result.lower()

    def test_successful_add_returns_confirmation(self):
        result = self._call({
            "action": "add",
            "athlete_name": "Luis Pena",
            "platform": "instagram",
            "deliverable_type": "reel",
            "due_date": "2026-06-15",
            "confirmed": True,
        })
        assert "Luis Pena" in result
        assert "LOGGED" in result.upper() or "logged" in result.lower()

    def test_complete_without_id_returns_message(self):
        result = self._call({"action": "complete", "confirmed": True})
        assert "deliverable_id" in result.lower()

    def test_complete_unknown_id_returns_friendly_error(self):
        result = self._call({"action": "complete", "deliverable_id": 9999, "confirmed": True})
        assert "error" in result.lower() or "not found" in result.lower()

    def test_successful_complete_returns_confirmation(self):
        row = ic.add_deliverable(athlete_name="Fighter A", platform="instagram", deliverable_type="story", entity="F3E")
        result = self._call({
            "action": "complete",
            "deliverable_id": row["id"],
            "completion_link": "https://www.instagram.com/p/test123",
            "confirmed": True,
        })
        assert "COMPLETE" in result.upper() or "complete" in result.lower()
        assert "Fighter A" in result

    def test_waive_marks_correctly(self):
        row = ic.add_deliverable(athlete_name="Fighter B", platform="tiktok", deliverable_type="video", entity="F3E")
        result = self._call({
            "action": "waive",
            "deliverable_id": row["id"],
            "notes": "Injury",
            "confirmed": True,
        })
        assert "WAIVED" in result.upper() or "waived" in result.lower()
        assert "Fighter B" in result

    def test_entity_defaults_to_channel_entity(self):
        self._call({
            "action": "add",
            "athlete_name": "Channel Default",
            "platform": "instagram",
            "deliverable_type": "post",
            "confirmed": True,
        }, entity="UFL")
        rows = ic.get_deliverables(entity="UFL")
        assert any(r["athlete_name"] == "Channel Default" for r in rows)

    def test_add_with_hubspot_deal_id(self):
        result = self._call({
            "action": "add",
            "athlete_name": "HubSpot Athlete",
            "platform": "instagram",
            "deliverable_type": "post",
            "hubspot_deal_id": "987654321",
            "confirmed": True,
        })
        assert "HubSpot Athlete" in result
        rows = ic.get_deliverables(athlete="HubSpot Athlete")
        assert rows[0]["hubspot_deal_id"] == "987654321"
