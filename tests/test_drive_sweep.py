"""Unit tests for src/cora/connectors/drive_sweep.py.

Tests use mocks for Drive API, Anthropic client, and KB -- no real network calls.
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from cora.connectors.drive_sweep import (
    _chunk_text,
    _classify,
    _extract_google_doc,
    _extract_google_sheet,
    _extract_pdf_bytes,
    _extract_xlsx_bytes,
    _extract_content,
    _ingest_file,
    _PHI_PATTERNS,
    run_sweep,
    sweep_user,
)


# ── _chunk_text ────────────────────────────────────────────────────────────────

class TestChunkText:
    def test_short_text_single_chunk(self):
        text = "short text"
        chunks = _chunk_text(text)
        assert chunks == [text]

    def test_long_text_splits_correctly(self):
        text = "x" * 3000
        chunks = _chunk_text(text)
        assert len(chunks) > 1
        # Each chunk no longer than _CHUNK_SIZE
        for chunk in chunks:
            assert len(chunk) <= 1400

    def test_overlap_between_chunks(self):
        text = "a" * 1400 + "b" * 1400
        chunks = _chunk_text(text)
        assert len(chunks) >= 2
        # Overlap: end of chunk 0 should appear in start of chunk 1
        assert chunks[0][-50:] in chunks[1]

    def test_empty_string(self):
        assert _chunk_text("") == [""]

    def test_exactly_chunk_size(self):
        text = "x" * 1400
        assert _chunk_text(text) == [text]


# ── _PHI_PATTERNS ──────────────────────────────────────────────────────────────

class TestPhiPatterns:
    def test_matches_dob(self):
        assert _PHI_PATTERNS.search("patient dob: 1980-01-01")

    def test_matches_ahcccs(self):
        assert _PHI_PATTERNS.search("AHCCCS member enrolled")

    def test_matches_diagnosis(self):
        assert _PHI_PATTERNS.search("Diagnosis: autism spectrum disorder")

    def test_matches_icd10(self):
        assert _PHI_PATTERNS.search("ICD-10 code F84.0")

    def test_matches_npi(self):
        assert _PHI_PATTERNS.search("Provider NPI number")

    def test_no_match_for_normal_text(self):
        assert not _PHI_PATTERNS.search("quarterly revenue report Q1 2026")

    def test_no_match_for_f3_content(self):
        assert not _PHI_PATTERNS.search("F3 Energy brand guidelines Pure tagline")

    def test_case_insensitive(self):
        assert _PHI_PATTERNS.search("DOB")
        assert _PHI_PATTERNS.search("medicaid")
        assert _PHI_PATTERNS.search("MEDICAID")


# ── _classify ─────────────────────────────────────────────────────────────────

class TestClassify:
    def _make_client(self, response_json: dict) -> MagicMock:
        msg = MagicMock()
        msg.content = [MagicMock()]
        msg.content[0].text = json.dumps(response_json)
        client = MagicMock()
        client.messages.create.return_value = msg
        return client

    def test_returns_parsed_classification(self):
        client = self._make_client(
            {"score": 9, "entity": "F3E", "summary": "Q1 contract", "discard_reason": ""}
        )
        result = _classify(client, "contract.pdf", "Tommy", "tommy@f3energy.com", "F3E", "content")
        assert result["score"] == 9
        assert result["entity"] == "F3E"

    def test_strips_markdown_fences(self):
        msg = MagicMock()
        msg.content = [MagicMock()]
        msg.content[0].text = "```json\n{\"score\": 7, \"entity\": \"OSN\", \"summary\": \"x\", \"discard_reason\": \"\"}\n```"
        client = MagicMock()
        client.messages.create.return_value = msg
        result = _classify(client, "file.txt", "Matt", "matt@osn.com", "OSN", "preview")
        assert result["score"] == 7

    def test_falls_back_on_api_error(self):
        client = MagicMock()
        client.messages.create.side_effect = Exception("network error")
        result = _classify(client, "file.txt", "User", "user@hjrglobal.com", "HJRG", "preview")
        # Fallback: score=5 so the file is kept
        assert result["score"] == 5

    def test_falls_back_on_json_parse_error(self):
        msg = MagicMock()
        msg.content = [MagicMock()]
        msg.content[0].text = "not json at all"
        client = MagicMock()
        client.messages.create.return_value = msg
        result = _classify(client, "file.txt", "User", "user@hjrglobal.com", "HJRG", "preview")
        assert result["score"] == 5


# ── _extract_google_doc ────────────────────────────────────────────────────────

class TestExtractGoogleDoc:
    def test_returns_decoded_bytes(self):
        service = MagicMock()
        service.files().export().execute.return_value = b"Contract text here"
        result = _extract_google_doc(service, "file123")
        assert result == "Contract text here"

    def test_returns_string_directly(self):
        service = MagicMock()
        service.files().export().execute.return_value = "Already a string"
        result = _extract_google_doc(service, "file123")
        assert result == "Already a string"

    def test_returns_empty_on_error(self):
        service = MagicMock()
        service.files().export.side_effect = Exception("api error")
        result = _extract_google_doc(service, "file123")
        assert result == ""


# ── _extract_google_sheet ──────────────────────────────────────────────────────

class TestExtractGoogleSheet:
    def test_returns_tabular_text(self):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Q1"
        ws.append(["Revenue", "Cost", "Net"])
        ws.append([100000, 80000, 20000])
        buf = io.BytesIO()
        wb.save(buf)
        xlsx_bytes = buf.getvalue()

        service = MagicMock()
        service.files().export().execute.return_value = xlsx_bytes
        result = _extract_google_sheet(service, "sheet123")
        assert "Q1" in result
        assert "Revenue" in result

    def test_returns_empty_on_error(self):
        service = MagicMock()
        service.files().export.side_effect = Exception("api error")
        result = _extract_google_sheet(service, "sheet123")
        assert result == ""


# ── _extract_pdf_bytes ─────────────────────────────────────────────────────────

class TestExtractPdfBytes:
    def test_returns_empty_gracefully_when_pdfplumber_missing(self):
        with patch.dict("sys.modules", {"pdfplumber": None}):
            result = _extract_pdf_bytes(b"%PDF-1.4 fake")
            assert result == ""

    def test_extracts_text_with_pdfplumber(self):
        mock_page = MagicMock()
        mock_page.extract_text.return_value = "Page content here"
        mock_pdf = MagicMock()
        mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
        mock_pdf.__exit__ = MagicMock(return_value=False)
        mock_pdf.pages = [mock_page]

        with patch("pdfplumber.open", return_value=mock_pdf):
            result = _extract_pdf_bytes(b"%PDF fake bytes")
        assert "Page content here" in result


# ── _extract_xlsx_bytes ────────────────────────────────────────────────────────

class TestExtractXlsxBytes:
    def test_returns_tabular_text(self):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Data"
        ws.append(["Name", "Value"])
        ws.append(["Item A", 42])
        buf = io.BytesIO()
        wb.save(buf)
        result = _extract_xlsx_bytes(buf.getvalue())
        assert "Name" in result
        assert "Item A" in result

    def test_returns_empty_on_bad_bytes(self):
        result = _extract_xlsx_bytes(b"not an xlsx file")
        assert result == ""


# ── _extract_content routing ───────────────────────────────────────────────────

class TestExtractContent:
    def test_skips_image_mime(self):
        service = MagicMock()
        result = _extract_content(service, {"id": "x", "mimeType": "image/png"})
        assert result == ""

    def test_skips_folder_mime(self):
        service = MagicMock()
        result = _extract_content(
            service,
            {"id": "x", "mimeType": "application/vnd.google-apps.folder"}
        )
        assert result == ""

    def test_routes_google_doc(self):
        service = MagicMock()
        service.files().export().execute.return_value = b"doc text"
        result = _extract_content(
            service,
            {"id": "x", "mimeType": "application/vnd.google-apps.document"}
        )
        assert result == "doc text"

    def test_routes_google_sheet(self):
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["A", "B"])
        buf = io.BytesIO()
        wb.save(buf)

        service = MagicMock()
        service.files().export().execute.return_value = buf.getvalue()
        result = _extract_content(
            service,
            {"id": "x", "mimeType": "application/vnd.google-apps.spreadsheet"}
        )
        assert result != ""


# ── _ingest_file ───────────────────────────────────────────────────────────────

class TestIngestFile:
    def _make_kb(self):
        kb = MagicMock()
        kb.upsert_documents = MagicMock(return_value=1)
        return kb

    def test_ingests_single_chunk(self):
        kb = self._make_kb()
        file_meta = {"id": "file1", "name": "contract.pdf", "mimeType": "application/pdf", "modifiedTime": "2026-01-01T00:00:00Z"}
        classification = {"score": 9, "entity": "F3E", "summary": "Q1 distribution contract", "discard_reason": ""}
        user = {"email": "tommy@f3energy.com", "name": "Tommy Anderson", "entity_default": "F3E"}
        content = "This is the contract text"
        n = _ingest_file(kb, file_meta, content, classification, user)
        assert n == 1
        kb.upsert_documents.assert_called_once()
        docs = kb.upsert_documents.call_args[0][0]
        doc = docs[0]
        assert doc.source == "drive_sweep"
        assert doc.entity == "F3E"
        assert doc.source_id == "file1"

    def test_lex_sub_entity_split(self):
        kb = self._make_kb()
        file_meta = {"id": "file2", "name": "llc-ops.docx", "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "modifiedTime": "2026-01-01T00:00:00Z"}
        classification = {"score": 8, "entity": "LEX-LLC", "summary": "LLC operations manual", "discard_reason": ""}
        user = {"email": "shaun@lexingtonservices.com", "name": "Shaun Hawkins", "entity_default": "LEX"}
        n = _ingest_file(kb, file_meta, "LLC SOP content", classification, user)
        docs = kb.upsert_documents.call_args[0][0]
        doc = docs[0]
        assert doc.entity == "LEX"
        assert doc.sub_entity == "LEX-LLC"

    def test_multiple_chunks_for_long_content(self):
        kb = self._make_kb()
        kb.upsert_documents = MagicMock(return_value=4)
        file_meta = {"id": "file3", "name": "big.txt", "mimeType": "text/plain", "modifiedTime": "2026-01-01T00:00:00Z"}
        classification = {"score": 7, "entity": "HJRG", "summary": "Big document", "discard_reason": ""}
        user = {"email": "harrison@hjrglobal.com", "name": "Harrison", "entity_default": "FNDR"}
        long_content = "x" * 5000
        n = _ingest_file(kb, file_meta, long_content, classification, user)
        assert n == 4
        # Full content passed as single Document — KB handles chunking internally
        kb.upsert_documents.assert_called_once()
        docs = kb.upsert_documents.call_args[0][0]
        assert len(docs) == 1
        assert docs[0].content == long_content

    def test_drive_link_format(self):
        kb = self._make_kb()
        file_meta = {"id": "abc123", "name": "doc.txt", "mimeType": "text/plain", "modifiedTime": "2026-01-01T00:00:00Z"}
        classification = {"score": 6, "entity": "FNDR", "summary": "A doc", "discard_reason": ""}
        user = {"email": "harrison@hjrglobal.com", "name": "Harrison", "entity_default": "FNDR"}
        _ingest_file(kb, file_meta, "content here", classification, user)
        docs = kb.upsert_documents.call_args[0][0]
        assert "drive.google.com/file/d/abc123" in docs[0].deep_link


# ── sweep_user ────────────────────────────────────────────────────────────────

class TestSweepUser:
    def _make_kb(self):
        kb = MagicMock()
        kb.get_sync_state.return_value = None
        kb.set_sync_state = MagicMock()
        kb.upsert_documents = MagicMock(return_value=1)
        return kb

    def _make_anthropic(self, score: int = 8):
        msg = MagicMock()
        msg.content = [MagicMock()]
        msg.content[0].text = json.dumps({
            "score": score, "entity": "F3E", "summary": "test doc", "discard_reason": ""
        })
        client = MagicMock()
        client.messages.create.return_value = msg
        return client

    def _make_drive_service(self, files: list[dict]):
        service = MagicMock()
        service.files().list().execute.return_value = {"files": files, "nextPageToken": None}
        service.files().export().execute.return_value = b"document content that is long enough to pass the 150 char threshold and contain meaningful business information for the test"
        service.files().get_media().execute.return_value = b"file content" * 20
        return service

    def test_returns_stats_dict(self):
        user = {"email": "tommy@f3energy.com", "name": "Tommy", "entity_default": "F3E",
                "enabled": True, "dwd_eligible": True, "drive_sweep": True}
        kb = self._make_kb()
        anthropic_client = self._make_anthropic(score=9)

        with patch("cora.connectors.drive_sweep._build_drive_service") as mock_build:
            mock_build.return_value = self._make_drive_service([
                {"id": "f1", "name": "contract.pdf",
                 "mimeType": "application/pdf",
                 "modifiedTime": "2026-01-01T00:00:00Z", "size": "10000"}
            ])
            with patch("cora.connectors.drive_sweep._extract_content") as mock_extract:
                mock_extract.return_value = "Meaningful business content " * 20
                stats = sweep_user(user, "/fake/sa.json", kb, anthropic_client,
                                   freshness_days=30, dry_run=False)

        assert "files_enumerated" in stats
        assert "chunks_ingested" in stats
        assert stats["files_enumerated"] >= 1

    def test_dry_run_does_not_call_upsert(self):
        user = {"email": "tommy@f3energy.com", "name": "Tommy", "entity_default": "F3E",
                "enabled": True, "dwd_eligible": True, "drive_sweep": True}
        kb = self._make_kb()
        anthropic_client = self._make_anthropic(score=9)

        with patch("cora.connectors.drive_sweep._build_drive_service") as mock_build:
            mock_build.return_value = self._make_drive_service([
                {"id": "f2", "name": "notes.txt", "mimeType": "text/plain",
                 "modifiedTime": "2026-01-01T00:00:00Z", "size": "5000"}
            ])
            with patch("cora.connectors.drive_sweep._extract_content") as mock_extract:
                mock_extract.return_value = "Business notes " * 20
                sweep_user(user, "/fake/sa.json", kb, anthropic_client,
                           freshness_days=30, dry_run=True)

        kb.upsert_documents.assert_not_called()

    def test_phi_guard_skips_lex_phi_content(self):
        user = {"email": "shaun@lexingtonservices.com", "name": "Shaun", "entity_default": "LEX",
                "enabled": True, "dwd_eligible": True, "drive_sweep": True}
        kb = self._make_kb()
        anthropic_client = self._make_anthropic(score=9)

        with patch("cora.connectors.drive_sweep._build_drive_service") as mock_build:
            mock_build.return_value = self._make_drive_service([
                {"id": "f3", "name": "client-record.pdf",
                 "mimeType": "application/pdf",
                 "modifiedTime": "2026-01-01T00:00:00Z", "size": "8000"}
            ])
            # Content that triggers PHI guard
            phi_content = "Patient DOB: 1990-01-01, AHCCCS member ID: 12345, diagnosis: F84.0" * 10
            with patch("cora.connectors.drive_sweep._extract_content") as mock_extract:
                mock_extract.return_value = phi_content
                stats = sweep_user(user, "/fake/sa.json", kb, anthropic_client,
                                   freshness_days=30, dry_run=False)

        assert stats["phi_skipped"] >= 1
        kb.upsert_documents.assert_not_called()

    def test_dedup_skips_shared_files(self):
        user = {"email": "gaelan@f3energy.com", "name": "Gaelan", "entity_default": "F3E",
                "enabled": True, "dwd_eligible": True, "drive_sweep": True}
        kb = self._make_kb()
        anthropic_client = self._make_anthropic(score=9)
        # File ID already seen by a prior user
        seen = {"already_seen_file_id"}

        with patch("cora.connectors.drive_sweep._build_drive_service") as mock_build:
            mock_build.return_value = self._make_drive_service([
                {"id": "already_seen_file_id", "name": "shared.docx",
                 "mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                 "modifiedTime": "2026-01-01T00:00:00Z", "size": "5000"}
            ])
            stats = sweep_user(user, "/fake/sa.json", kb, anthropic_client,
                               freshness_days=30, dry_run=False, seen_file_ids=seen)

        assert stats["dedup_skipped"] == 1
        kb.upsert_documents.assert_not_called()

    def test_noise_filtered_when_score_below_4(self):
        user = {"email": "tommy@f3energy.com", "name": "Tommy", "entity_default": "F3E",
                "enabled": True, "dwd_eligible": True, "drive_sweep": True}
        kb = self._make_kb()
        # Low score -- should be filtered
        anthropic_client = self._make_anthropic(score=2)

        with patch("cora.connectors.drive_sweep._build_drive_service") as mock_build:
            mock_build.return_value = self._make_drive_service([
                {"id": "f5", "name": "template.docx", "mimeType": "text/plain",
                 "modifiedTime": "2026-01-01T00:00:00Z", "size": "5000"}
            ])
            with patch("cora.connectors.drive_sweep._extract_content") as mock_extract:
                mock_extract.return_value = "Content " * 30
                stats = sweep_user(user, "/fake/sa.json", kb, anthropic_client,
                                   freshness_days=30, dry_run=False)

        assert stats["noise_filtered"] >= 1
        kb.upsert_documents.assert_not_called()

    def test_build_service_failure_returns_empty_stats(self):
        user = {"email": "nonexistent@nowhere.com", "name": "X", "entity_default": "FNDR",
                "enabled": True, "dwd_eligible": True, "drive_sweep": True}
        kb = self._make_kb()
        anthropic_client = MagicMock()

        with patch("cora.connectors.drive_sweep._build_drive_service",
                   side_effect=Exception("DWD not configured")):
            stats = sweep_user(user, "/fake/sa.json", kb, anthropic_client,
                               freshness_days=30, dry_run=False)

        assert stats["files_enumerated"] == 0
        kb.upsert_documents.assert_not_called()

    def test_cora_internal_doc_skipped_before_extract(self):
        # WS1-DRIVE: a Cora build/audit doc is skipped at ingest BEFORE extraction,
        # so it never reaches the KB and re-poisons RAG (the Minute Press leak vector).
        user = {"email": "harrison@hjrglobal.com", "name": "Harrison", "entity_default": "FNDR",
                "enabled": True, "dwd_eligible": True, "drive_sweep": True}
        kb = self._make_kb()
        anthropic_client = self._make_anthropic(score=9)

        with patch("cora.connectors.drive_sweep._build_drive_service") as mock_build:
            mock_build.return_value = self._make_drive_service([
                {"id": "fc1", "name": "2026-06-16_fndr_cora-rebuild-execution-log.md",
                 "mimeType": "text/markdown",
                 "modifiedTime": "2026-06-16T00:00:00Z", "size": "9000"}
            ])
            with patch("cora.connectors.drive_sweep._extract_content") as mock_extract:
                stats = sweep_user(user, "/fake/sa.json", kb, anthropic_client,
                                   freshness_days=30, dry_run=False)

        assert stats.get("cora_internal_skipped", 0) >= 1
        kb.upsert_documents.assert_not_called()
        mock_extract.assert_not_called()  # guard fires BEFORE extraction

    def test_legit_cora_adjacent_doc_not_skipped(self):
        # Negative control: a legit cora-ADJACENT business doc is NOT skipped.
        user = {"email": "harrison@hjrglobal.com", "name": "Harrison", "entity_default": "F3E",
                "enabled": True, "dwd_eligible": True, "drive_sweep": True}
        kb = self._make_kb()
        anthropic_client = self._make_anthropic(score=9)

        with patch("cora.connectors.drive_sweep._build_drive_service") as mock_build:
            mock_build.return_value = self._make_drive_service([
                {"id": "fc2", "name": "f3-brand-assets-cora-reference.md",
                 "mimeType": "text/markdown",
                 "modifiedTime": "2026-06-16T00:00:00Z", "size": "9000"}
            ])
            with patch("cora.connectors.drive_sweep._extract_content") as mock_extract:
                mock_extract.return_value = "F3 brand reference content " * 20
                stats = sweep_user(user, "/fake/sa.json", kb, anthropic_client,
                                   freshness_days=30, dry_run=False)

        assert stats.get("cora_internal_skipped", 0) == 0
        kb.upsert_documents.assert_called()  # legit doc still ingested

    def test_founders_os_loop_skips_cora_internal(self):
        # The dominant real leak vector: the founders_os folder walk must guard too.
        from cora.connectors.drive_sweep import _process_single_folder_files
        kb = self._make_kb()
        anthropic_client = self._make_anthropic(score=9)
        service = self._make_drive_service([
            {"id": "fos1", "name": "2026-06-16_fndr_cora-forensic-findings-report.md",
             "mimeType": "text/markdown", "modifiedTime": "2026-06-16T00:00:00Z", "size": "9000"}
        ])
        stats = {"files_enumerated": 0, "files_extracted": 0, "chunks_ingested": 0,
                 "phi_skipped": 0, "noise_filtered": 0, "dedup_skipped": 0}
        with patch("cora.connectors.drive_sweep._extract_content") as mock_extract:
            _process_single_folder_files(
                service=service, folder_id="F", label="FNDR", effective_entity="FNDR",
                kb=kb, anthropic_client=anthropic_client, cutoff_str="2020-01-01T00:00:00Z",
                dry_run=False, is_lex=False, score_threshold=4, seen_file_ids=set(), stats=stats,
            )
        assert stats.get("cora_internal_skipped", 0) >= 1
        kb.upsert_documents.assert_not_called()
        mock_extract.assert_not_called()  # guard fires BEFORE extraction


# ── run_sweep ─────────────────────────────────────────────────────────────────

class TestRunSweep:
    def _accounts_yaml(self, tmp_path) -> str:
        import yaml
        data = {
            "accounts": [
                {"email": "harrison@hjrglobal.com", "name": "Harrison",
                 "enabled": True, "dwd_eligible": True, "drive_sweep": True,
                 "entity_default": "FNDR"},
                {"email": "tommy@f3energy.com", "name": "Tommy",
                 "enabled": True, "dwd_eligible": True, "drive_sweep": True,
                 "entity_default": "F3E"},
                # drive_sweep: false -- should be excluded
                {"email": "skip@hjrglobal.com", "name": "Skip",
                 "enabled": True, "dwd_eligible": True, "drive_sweep": False,
                 "entity_default": "FNDR"},
            ]
        }
        path = tmp_path / "accounts.yaml"
        path.write_text(yaml.dump(data), encoding="utf-8")
        return str(path)

    def test_sweeps_only_eligible_accounts(self, tmp_path):
        yaml_path = self._accounts_yaml(tmp_path)
        kb = MagicMock()
        kb.get_sync_state.return_value = None
        kb.set_sync_state = MagicMock()
        anthropic_client = MagicMock()

        with patch("cora.connectors.drive_sweep.sweep_user") as mock_sweep:
            mock_sweep.return_value = {
                "files_enumerated": 5, "files_extracted": 3,
                "chunks_ingested": 10, "phi_skipped": 0,
                "noise_filtered": 2, "dedup_skipped": 0,
            }
            stats = run_sweep(
                sa_json_path="/fake/sa.json",
                accounts_yaml_path=yaml_path,
                kb=kb,
                anthropic_client=anthropic_client,
            )

        # skip@ was excluded because drive_sweep: false
        assert mock_sweep.call_count == 2
        assert stats["accounts_swept"] == 2
        assert stats["chunks_ingested"] == 20  # 10 * 2 accounts

    def test_only_email_filter(self, tmp_path):
        yaml_path = self._accounts_yaml(tmp_path)
        kb = MagicMock()
        kb.get_sync_state.return_value = None
        kb.set_sync_state = MagicMock()
        anthropic_client = MagicMock()

        with patch("cora.connectors.drive_sweep.sweep_user") as mock_sweep:
            mock_sweep.return_value = {
                "files_enumerated": 2, "files_extracted": 1,
                "chunks_ingested": 3, "phi_skipped": 0,
                "noise_filtered": 1, "dedup_skipped": 0,
            }
            stats = run_sweep(
                sa_json_path="/fake/sa.json",
                accounts_yaml_path=yaml_path,
                kb=kb,
                anthropic_client=anthropic_client,
                only_email="tommy@f3energy.com",
            )

        assert mock_sweep.call_count == 1
        assert stats["accounts_swept"] == 1

    def test_seen_file_ids_shared_across_users(self, tmp_path):
        """Verify that seen_file_ids set is passed to each sweep_user call."""
        yaml_path = self._accounts_yaml(tmp_path)
        kb = MagicMock()
        kb.get_sync_state.return_value = None
        kb.set_sync_state = MagicMock()
        anthropic_client = MagicMock()
        captured_sets = []

        def capture_seen(user, sa_json_path, kb, anthropic_client,
                         freshness_days, dry_run, seen_file_ids):
            captured_sets.append(seen_file_ids)
            seen_file_ids.add(f"file_from_{user['email']}")
            return {"files_enumerated": 1, "files_extracted": 1,
                    "chunks_ingested": 1, "phi_skipped": 0,
                    "noise_filtered": 0, "dedup_skipped": 0}

        with patch("cora.connectors.drive_sweep.sweep_user", side_effect=capture_seen):
            run_sweep(
                sa_json_path="/fake/sa.json",
                accounts_yaml_path=yaml_path,
                kb=kb,
                anthropic_client=anthropic_client,
            )

        # Same set object shared between both calls
        assert len(captured_sets) == 2
        assert captured_sets[0] is captured_sets[1]
        # Second call should see the file_id added by first call
        assert any("harrison@hjrglobal.com" in fid for fid in captured_sets[1])
