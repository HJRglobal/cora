"""Tests for Gmail full-thread KB sweep — Component 2.

Layer A: string/logic assertions (no imports needed).
Layer B: import-guarded unit tests with mocks.

Coverage:
  - _derive_entity(): keyword-based entity detection
  - _is_phi_risk(): PHI subject filter
  - _chunk_text(): text chunking at paragraph boundaries
  - get_full_thread_text(): message extraction + body decode
  - list_threads_since(): thread ID listing with pagination
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── Layer A: pure logic ────────────────────────────────────────────────────────

def _load_sweep():
    """Import from scripts/gmail_threaded_sweep.py."""
    try:
        sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
        import gmail_threaded_sweep as m
        return m
    except ImportError:
        pytest.skip("gmail_threaded_sweep not importable")


class TestDeriveEntity:
    """Layer A — entity detection from email metadata."""

    def test_f3e_subject(self):
        m = _load_sweep()
        entity = m._derive_entity("F3 Energy Pure launch", "larry@bigd.media", "", [])
        assert entity == "F3E"

    def test_osn_subject(self):
        m = _load_sweep()
        entity = m._derive_entity("One Stop Nutrition inventory", "matt@hjrglobal.com", "", [])
        assert entity == "OSN"

    def test_lex_subject(self):
        m = _load_sweep()
        entity = m._derive_entity("Lexington AHCCCS audit", "shaun@lexingtonservices.com", "", [])
        assert entity == "LEX"

    def test_ufl_subject(self):
        m = _load_sweep()
        entity = m._derive_entity("UFL sponsorship proposal", "harrison@hjrglobal.com", "", [])
        assert entity == "UFL"

    def test_hjrg_subject(self):
        m = _load_sweep()
        entity = m._derive_entity("HJR Global intercompany allocation", "justin@hjrglobal.com", "", [])
        assert entity == "FNDR"

    def test_unrecognized_defaults_to_account_default(self):
        m = _load_sweep()
        entity = m._derive_entity("Random subject", "external@outside.com", "", [], account_entity_default="OSN")
        assert entity == "OSN"

    def test_case_insensitive(self):
        m = _load_sweep()
        entity = m._derive_entity("BIG D MEDIA project brief", "", "", [])
        assert entity == "BDM"

    def test_hjrp_subject(self):
        m = _load_sweep()
        entity = m._derive_entity("Rogers Ranch booking inquiry", "", "", [])
        assert entity == "HJRP"


class TestPhiRisk:
    """Layer A — PHI risk detection."""

    def test_service_note_flagged(self):
        m = _load_sweep()
        assert m._is_phi_risk("Service note for client visit", []) is True

    def test_care_plan_flagged(self):
        m = _load_sweep()
        assert m._is_phi_risk("Care plan update for John", []) is True

    def test_normal_subject_not_flagged(self):
        m = _load_sweep()
        assert m._is_phi_risk("Q2 budget review", []) is False

    def test_lex_financial_not_flagged(self):
        m = _load_sweep()
        assert m._is_phi_risk("Lexington LLC payroll run", []) is False

    def test_incident_report_flagged(self):
        m = _load_sweep()
        assert m._is_phi_risk("Incident report - residential", []) is True

    def test_prior_auth_flagged(self):
        m = _load_sweep()
        assert m._is_phi_risk("Prior auth request - Medicaid", []) is True

    def test_medication_flagged(self):
        m = _load_sweep()
        assert m._is_phi_risk("Medication refill approval", []) is True


class TestChunkText:
    """Layer A — text chunking."""

    def test_short_text_single_chunk(self):
        m = _load_sweep()
        result = m._chunk_text("Short text here", max_chars=2400)
        assert len(result) == 1
        assert result[0] == "Short text here"

    def test_empty_text_returns_empty(self):
        m = _load_sweep()
        result = m._chunk_text("", max_chars=2400)
        assert result == []

    def test_long_text_splits(self):
        m = _load_sweep()
        # Create text that exceeds max_chars with clear paragraph breaks
        para = "x" * 500 + "\n\n"
        text = para * 6  # 3000 chars * 6 = 18000 chars
        result = m._chunk_text(text, max_chars=2400)
        assert len(result) >= 2

    def test_chunks_within_limit(self):
        m = _load_sweep()
        para = "a" * 500 + "\n\n"
        text = para * 10
        result = m._chunk_text(text, max_chars=2400)
        for chunk in result:
            # Each chunk should be within max_chars (with some slack for paragraph boundary)
            assert len(chunk) <= 2400 + 600  # generous slack


class TestOrderAccounts:
    """Layer A -- stale-first account ordering (resumability)."""

    def test_never_swept_accounts_first(self):
        m = _load_sweep()
        accts = [{"email": "a@x.com"}, {"email": "b@x.com"}, {"email": "c@x.com"}]
        wm = {"a@x.com": 2000, "c@x.com": 1000}  # b has no watermark
        order = [a["email"] for a in m._order_accounts(accts, wm, fallback_ts=500)]
        assert order == ["b@x.com", "c@x.com", "a@x.com"]

    def test_never_swept_sorts_before_any_persisted(self):
        m = _load_sweep()
        accts = [{"email": "swept@x.com"}, {"email": "fresh@x.com"}]
        wm = {"swept@x.com": 999_999_999}
        order = [a["email"] for a in m._order_accounts(accts, wm, fallback_ts=100)]
        assert order == ["fresh@x.com", "swept@x.com"]

    def test_stable_for_equal_watermarks(self):
        m = _load_sweep()
        accts = [{"email": "a@x.com"}, {"email": "b@x.com"}]
        order = [a["email"] for a in m._order_accounts(accts, {}, fallback_ts=100)]
        assert order == ["a@x.com", "b@x.com"]


class TestNextWatermark:
    """Layer A -- cap-aware watermark advancement (no silent backlog drop)."""

    def test_under_cap_advances_to_sync_start(self):
        m = _load_sweep()
        assert m._next_watermark(50, 500, 1234, 9999) == 9999

    def test_at_cap_holds_at_newest_processed(self):
        m = _load_sweep()
        # cap hit -> older backlog remains -> do NOT jump to sync_start
        assert m._next_watermark(500, 500, 1234, 9999) == 1234

    def test_over_cap_holds_at_newest_processed(self):
        m = _load_sweep()
        assert m._next_watermark(600, 500, 1234, 9999) == 1234

    def test_capped_but_nothing_parsed_returns_zero(self):
        m = _load_sweep()
        # 0 signals caller to leave the watermark unchanged
        assert m._next_watermark(500, 500, 0, 9999) == 0

    def test_just_under_cap_boundary(self):
        m = _load_sweep()
        assert m._next_watermark(499, 500, 1, 9999) == 9999


class TestEffectiveSince:
    """Layer A -- windowed-backfill override (--force-since-days)."""

    def test_no_override_returns_watermark(self):
        m = _load_sweep()
        assert m._effective_since(2000, None, 9999) == 2000
        assert m._effective_since(2000, 0, 9999) == 2000

    def test_override_reaches_back_past_recent_watermark(self):
        m = _load_sweep()
        sync = 1_000_000
        # 5-day force = sync - 432000; a recent watermark (sync-100) must be pulled back
        out = m._effective_since(sync - 100, 5, sync)
        assert out == sync - 5 * 86400

    def test_override_does_not_move_an_even_older_watermark_forward(self):
        m = _load_sweep()
        sync = 1_000_000
        very_old = sync - 999 * 86400  # older than the 5-day window
        assert m._effective_since(very_old, 5, sync) == very_old  # keeps the earlier ts


# ── Layer B: import-guarded unit tests with mocks ─────────────────────────────

try:
    from src.cora.connectors import gmail_reader as gr
    _IMPORT_OK = True
except Exception:
    _IMPORT_OK = False


def _b64(text: str) -> str:
    """Encode text to base64url for fake Gmail payload."""
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


@pytest.mark.skipif(not _IMPORT_OK, reason="gmail_reader not importable on this mount")
class TestListThreadsSince:
    """Layer B — list_threads_since() with mocked Gmail API."""

    def _build_mock_service(self, thread_ids: list[str], next_page_token=""):
        mock_service = MagicMock()
        mock_threads = MagicMock()
        mock_list = MagicMock()
        resp = {
            "threads": [{"id": tid, "historyId": "123"} for tid in thread_ids],
        }
        if next_page_token:
            resp["nextPageToken"] = next_page_token
        mock_list.execute.return_value = resp
        mock_threads.list.return_value = mock_list
        mock_service.users.return_value.threads.return_value = mock_threads
        return mock_service

    def test_returns_thread_ids(self):
        mock_svc = self._build_mock_service(["T001", "T002", "T003"])
        with patch.object(gr, "_build_service", return_value=mock_svc):
            result = gr.list_threads_since("test@hjrglobal.com", since_ts=1717000000)
        assert result == ["T001", "T002", "T003"]

    def test_empty_result(self):
        mock_svc = self._build_mock_service([])
        with patch.object(gr, "_build_service", return_value=mock_svc):
            result = gr.list_threads_since("test@hjrglobal.com", since_ts=1717000000)
        assert result == []

    def test_403_raises_gmail_reader_error(self):
        from googleapiclient.errors import HttpError
        mock_resp_obj = MagicMock()
        mock_resp_obj.status = 403
        exc = HttpError(resp=mock_resp_obj, content=b"Forbidden")
        mock_service = MagicMock()
        mock_service.users.return_value.threads.return_value.list.return_value.execute.side_effect = exc
        with patch.object(gr, "_build_service", return_value=mock_service):
            with pytest.raises(gr.GmailReaderError, match="403"):
                gr.list_threads_since("test@hjrglobal.com", since_ts=1717000000)


@pytest.mark.skipif(not _IMPORT_OK, reason="gmail_reader not importable on this mount")
class TestGetFullThreadText:
    """Layer B — get_full_thread_text() with mocked Gmail API."""

    def _make_message(self, msg_id, subject, sender, body_text, ts_ms, date_str=None):
        if date_str is None:
            from datetime import datetime, timezone
            dt = datetime.fromtimestamp(ts_ms // 1000, tz=timezone.utc)
            date_str = dt.strftime("%a, %d %b %Y %H:%M:%S +0000")
        return {
            "id": msg_id,
            "threadId": "TH001",
            "internalDate": str(ts_ms),
            "labelIds": ["INBOX"],
            "payload": {
                "mimeType": "multipart/alternative",
                "headers": [
                    {"name": "From", "value": sender},
                    {"name": "To", "value": "harrison@hjrglobal.com"},
                    {"name": "Subject", "value": subject},
                    {"name": "Date", "value": date_str},
                ],
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "body": {"data": _b64(body_text), "size": len(body_text)},
                    }
                ],
            },
        }

    def test_returns_messages_oldest_first(self):
        msg1 = self._make_message("M001", "Subject", "alice@test.com", "First message", 1717000000000)
        msg2 = self._make_message("M002", "Re: Subject", "bob@test.com", "Second message", 1717001000000)
        mock_service = MagicMock()
        mock_service.users.return_value.threads.return_value.get.return_value.execute.return_value = {
            "id": "TH001",
            "messages": [msg2, msg1],  # reversed (newest first from API)
        }
        with patch.object(gr, "_build_service", return_value=mock_service):
            result = gr.get_full_thread_text("harrison@hjrglobal.com", "TH001")
        assert result[0]["message_id"] == "M001"  # oldest first after sort
        assert result[1]["message_id"] == "M002"

    def test_body_text_extracted(self):
        msg = self._make_message("M001", "Test", "alice@test.com", "Hello world body", 1717000000000)
        mock_service = MagicMock()
        mock_service.users.return_value.threads.return_value.get.return_value.execute.return_value = {
            "id": "TH001",
            "messages": [msg],
        }
        with patch.object(gr, "_build_service", return_value=mock_service):
            result = gr.get_full_thread_text("harrison@hjrglobal.com", "TH001")
        assert "Hello world body" in result[0]["body_text"]

    def test_subject_extracted(self):
        msg = self._make_message("M001", "F3 Pure Launch Discussion", "larry@bigd.media", "Content", 1717000000000)
        mock_service = MagicMock()
        mock_service.users.return_value.threads.return_value.get.return_value.execute.return_value = {
            "id": "TH001",
            "messages": [msg],
        }
        with patch.object(gr, "_build_service", return_value=mock_service):
            result = gr.get_full_thread_text("harrison@hjrglobal.com", "TH001")
        assert result[0]["subject"] == "F3 Pure Launch Discussion"

    def test_empty_thread_returns_empty_list(self):
        mock_service = MagicMock()
        mock_service.users.return_value.threads.return_value.get.return_value.execute.return_value = {
            "id": "TH001",
            "messages": [],
        }
        with patch.object(gr, "_build_service", return_value=mock_service):
            result = gr.get_full_thread_text("harrison@hjrglobal.com", "TH001")
        assert result == []

    def test_api_error_raises(self):
        from googleapiclient.errors import HttpError
        mock_resp_obj = MagicMock()
        mock_resp_obj.status = 404
        exc = HttpError(resp=mock_resp_obj, content=b"Not Found")
        mock_service = MagicMock()
        mock_service.users.return_value.threads.return_value.get.return_value.execute.side_effect = exc
        with patch.object(gr, "_build_service", return_value=mock_service):
            with pytest.raises(gr.GmailReaderError):
                gr.get_full_thread_text("harrison@hjrglobal.com", "TH_MISSING")
