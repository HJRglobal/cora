"""Tests for pure helper functions in cora.connectors.drive_connector.

Covers only the functions that don't require a real Google Drive service:
  - _is_blacklisted_path
  - _natural_title
  - _classify_entity
  - _parent_folder_name
  - drive_file_to_document
"""

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import cora
from cora.connectors.drive_connector import (
    DriveConnectorError,
    DriveFile,
    _classify_entity,
    _is_blacklisted_path,
    _natural_title,
    _parent_folder_name,
    drive_file_to_document,
    safe_drive_create,
)
from cora.knowledge_base.store import Document


# ─────────────────────────────────────────────────────────────────────────────
# _is_blacklisted_path
# ─────────────────────────────────────────────────────────────────────────────

class TestIsBlacklistedPath:

    def test_segment_in_blacklist_payroll(self):
        """A segment exactly matching a blacklist entry → True."""
        assert _is_blacklisted_path(["HJR-Founder-OS", "payroll", "file.xlsx"]) is True

    def test_phi_segment_consumers(self):
        assert _is_blacklisted_path(["HJR-Founder-OS", "consumers", "member.pdf"]) is True

    def test_phi_segment_phi(self):
        assert _is_blacklisted_path(["HJR-Founder-OS", "phi", "record.docx"]) is True

    def test_phi_segment_ehr(self):
        assert _is_blacklisted_path(["HJR-Founder-OS", "ehr", "chart.xlsx"]) is True

    def test_phi_segment_eob(self):
        """The folder-level 'eob' segment should be blacklisted."""
        assert _is_blacklisted_path(["HJR-Founder-OS", "eob", "claim.pdf"]) is True

    def test_archive_segment(self):
        assert _is_blacklisted_path(["HJR-Founder-OS", "_archive", "old.pdf"]) is True

    def test_normal_business_path_returns_false(self):
        """A totally clean business path should NOT be blacklisted."""
        assert _is_blacklisted_path(
            ["HJR-Founder-OS", "02-F3-Energy", "sales", "deck.pptx"]
        ) is False

    def test_filename_pattern_arm1_payroll(self):
        """Arm 1: 'payroll detail.xlsx' in a non-blacklisted folder → True."""
        assert _is_blacklisted_path(
            ["HJR-Founder-OS", "02-F3-Energy", "payroll detail.xlsx"]
        ) is True

    def test_filename_pattern_arm2_1065(self):
        """Arm 2: '1065 filing.pdf' → True (tax form pattern)."""
        assert _is_blacklisted_path(
            ["HJR-Founder-OS", "00-Founder", "1065 filing.pdf"]
        ) is True

    def test_filename_pattern_arm3_eob_glued(self):
        """Arm 3: 'AetnaEOB.pdf' where EOB is glued without separator → True."""
        assert _is_blacklisted_path(
            ["HJR-Founder-OS", "02-F3-Energy", "AetnaEOB.pdf"]
        ) is True

    def test_false_positive_texas_does_not_match_tax(self):
        """'Texas 2025.pdf' must NOT be blacklisted — 'tax' should require word boundary."""
        assert _is_blacklisted_path(
            ["HJR-Founder-OS", "02-F3-Energy", "Texas 2025.pdf"]
        ) is False

    def test_empty_segments(self):
        """Empty segment list → False (nothing to match)."""
        assert _is_blacklisted_path([]) is False

    def test_case_insensitive_segment_match(self):
        """Blacklist matching is case-insensitive on segments."""
        assert _is_blacklisted_path(["HJR-Founder-OS", "Payroll", "file.xlsx"]) is True


# ─────────────────────────────────────────────────────────────────────────────
# _natural_title
# ─────────────────────────────────────────────────────────────────────────────

class TestNaturalTitle:

    def test_drops_pdf_extension(self):
        result = _natural_title("proposal.pdf")
        assert not result.lower().endswith(".pdf")
        assert "proposal" in result

    def test_drops_docx_extension(self):
        result = _natural_title("report.docx")
        assert not result.lower().endswith(".docx")
        assert "report" in result

    def test_drops_xlsx_extension(self):
        result = _natural_title("budget.xlsx")
        assert not result.lower().endswith(".xlsx")
        assert "budget" in result

    def test_drops_pptx_extension(self):
        result = _natural_title("pitch.pptx")
        assert not result.lower().endswith(".pptx")
        assert "pitch" in result

    def test_strips_leading_date_prefix(self):
        """'2026-04_some title.pdf' → title starts with 'some title' part."""
        result = _natural_title("2026-04_quarterly-report.pdf")
        assert not result.startswith("2026")
        assert "quarterly" in result.lower()

    def test_strips_leading_date_prefix_with_day(self):
        """'2026-04-15_' prefix also stripped."""
        result = _natural_title("2026-04-15_sales-summary.docx")
        assert not result.startswith("2026")
        assert "sales" in result.lower()

    def test_de_kebab(self):
        """'distributor-sales-deck' → 'distributor sales deck'."""
        result = _natural_title("distributor-sales-deck.pptx")
        assert result == "distributor sales deck"

    def test_de_snake(self):
        """'sales_summary_q1' → 'sales summary q1'."""
        result = _natural_title("sales_summary_q1.xlsx")
        assert result == "sales summary q1"

    def test_already_clean_name(self):
        """A plain clean name returns the base name without extension."""
        result = _natural_title("Energy Report.pdf")
        assert result == "Energy Report"

    def test_no_double_spaces(self):
        """Multiple separators should not produce double spaces."""
        result = _natural_title("foo--bar__baz.pdf")
        assert "  " not in result


# ─────────────────────────────────────────────────────────────────────────────
# _classify_entity
# ─────────────────────────────────────────────────────────────────────────────

class TestClassifyEntity:

    def test_f3e_entity(self):
        assert _classify_entity("HJR-Founder-OS/02-F3-Energy/sales/deck.pptx") == "F3E"

    def test_osn_entity(self):
        assert _classify_entity("HJR-Founder-OS/09-One-Stop-Nutrition/ops/plan.docx") == "OSN"

    def test_lex_entity(self):
        assert _classify_entity("HJR-Founder-OS/08-Lexington-Services/hr.pdf") == "LEX"

    def test_unknown_folder_falls_back_to_fndr(self):
        assert _classify_entity("HJR-Founder-OS/unknown-folder/file.pdf") == "FNDR"

    def test_fewer_than_two_segments_returns_fndr(self):
        assert _classify_entity("just-one-segment") == "FNDR"

    def test_empty_string_returns_fndr(self):
        assert _classify_entity("") == "FNDR"

    def test_bdm_entity(self):
        assert _classify_entity("HJR-Founder-OS/07-Big-D-Media/creative/logo.png") == "BDM"

    def test_fndr_entity(self):
        assert _classify_entity("HJR-Founder-OS/00-Founder/notes.pdf") == "FNDR"


# ─────────────────────────────────────────────────────────────────────────────
# _parent_folder_name
# ─────────────────────────────────────────────────────────────────────────────

class TestParentFolderName:

    def test_deep_path(self):
        result = _parent_folder_name("HJR-Founder-OS/02-F3-Energy/sales/deck.pptx")
        assert result == "sales"

    def test_one_segment_returns_empty(self):
        assert _parent_folder_name("file.pdf") == ""

    def test_trailing_slash_handled(self):
        """A trailing slash must not confuse the parent extraction."""
        result = _parent_folder_name("HJR-Founder-OS/02-F3-Energy/sales/")
        assert result == "02-F3-Energy"

    def test_two_segments(self):
        result = _parent_folder_name("HJR-Founder-OS/deck.pptx")
        assert result == "HJR-Founder-OS"

    def test_three_segments(self):
        result = _parent_folder_name("HJR-Founder-OS/02-F3-Energy/deck.pptx")
        assert result == "02-F3-Energy"


# ─────────────────────────────────────────────────────────────────────────────
# drive_file_to_document
# ─────────────────────────────────────────────────────────────────────────────

_SAMPLE_DRIVE_FILE = DriveFile(
    file_id="abc123",
    name="2026-04_distributor-sales-deck.pptx",
    mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    path="HJR-Founder-OS/02-F3-Energy/sales/2026-04_distributor-sales-deck.pptx",
    modified_time=1_700_000_000,
    created_time=1_690_000_000,
    owner_email="harrison@hjrglobal.com",
    web_view_link="https://docs.google.com/presentation/d/abc123/edit",
    size_bytes=204800,
)


class TestDriveFileToDocument:

    def test_returns_document_instance(self):
        doc = drive_file_to_document(_SAMPLE_DRIVE_FILE)
        assert isinstance(doc, Document)

    def test_entity_is_f3e(self):
        doc = drive_file_to_document(_SAMPLE_DRIVE_FILE)
        assert doc.entity == "F3E"

    def test_content_is_non_empty(self):
        doc = drive_file_to_document(_SAMPLE_DRIVE_FILE)
        assert doc.content and len(doc.content) > 10

    def test_deep_link_matches_web_view_link(self):
        doc = drive_file_to_document(_SAMPLE_DRIVE_FILE)
        assert doc.deep_link == _SAMPLE_DRIVE_FILE.web_view_link

    def test_title_is_natural(self):
        """Title should not contain the raw filename with extension or date prefix."""
        doc = drive_file_to_document(_SAMPLE_DRIVE_FILE)
        assert ".pptx" not in doc.title
        assert not doc.title.startswith("2026")
        assert "distributor" in doc.title.lower()

    def test_source_is_drive_asset(self):
        doc = drive_file_to_document(_SAMPLE_DRIVE_FILE)
        assert doc.source == "drive_asset"

    def test_source_id_is_file_id(self):
        doc = drive_file_to_document(_SAMPLE_DRIVE_FILE)
        assert doc.source_id == "abc123"

    def test_content_includes_filename(self):
        doc = drive_file_to_document(_SAMPLE_DRIVE_FILE)
        assert _SAMPLE_DRIVE_FILE.name in doc.content

    def test_content_includes_path(self):
        doc = drive_file_to_document(_SAMPLE_DRIVE_FILE)
        assert _SAMPLE_DRIVE_FILE.path in doc.content

    def test_author_matches_owner(self):
        doc = drive_file_to_document(_SAMPLE_DRIVE_FILE)
        assert doc.author == "harrison@hjrglobal.com"

    def test_fndr_entity_for_unknown_folder(self):
        df = DriveFile(
            file_id="xyz999",
            name="misc.pdf",
            mime_type="application/pdf",
            path="HJR-Founder-OS/unknown-entity/misc.pdf",
            modified_time=1_700_000_000,
            created_time=1_690_000_000,
            owner_email="",
            web_view_link="https://drive.google.com/file/d/xyz999/view",
            size_bytes=0,
        )
        doc = drive_file_to_document(df)
        assert doc.entity == "FNDR"


# ─────────────────────────────────────────────────────────────────────────────
# safe_drive_create — WS8 preventive Drive guardrail
# ─────────────────────────────────────────────────────────────────────────────

class TestSafeDriveCreate:

    def _service_returning(self, result):
        service = MagicMock()
        service.files.return_value.create.return_value.execute.return_value = result
        return service

    def test_rejects_missing_parents(self):
        service = self._service_returning({"id": "x"})
        with pytest.raises(DriveConnectorError, match="parents"):
            safe_drive_create(service, body={"name": "f.txt"})
        # Guard fires BEFORE any API call
        service.files.return_value.create.assert_not_called()

    def test_rejects_empty_parents(self):
        service = self._service_returning({"id": "x"})
        with pytest.raises(DriveConnectorError, match="parents"):
            safe_drive_create(service, body={"name": "f.txt", "parents": []})
        service.files.return_value.create.assert_not_called()

    def test_rejects_none_parents(self):
        service = self._service_returning({"id": "x"})
        with pytest.raises(DriveConnectorError, match="parents"):
            safe_drive_create(service, body={"name": "f.txt", "parents": None})
        service.files.return_value.create.assert_not_called()

    def test_rejects_empty_string_parent_element(self):
        # D-051: parents=[''] must not slip past the guard toward root.
        service = self._service_returning({"id": "x"})
        with pytest.raises(DriveConnectorError, match="parents"):
            safe_drive_create(service, body={"name": "f.txt", "parents": [""]})
        service.files.return_value.create.assert_not_called()

    def test_rejects_whitespace_and_nonstring_parent_element(self):
        service = self._service_returning({"id": "x"})
        with pytest.raises(DriveConnectorError, match="parents"):
            safe_drive_create(service, body={"name": "f.txt", "parents": ["  "]})
        with pytest.raises(DriveConnectorError, match="parents"):
            safe_drive_create(service, body={"name": "f.txt", "parents": [None]})
        service.files.return_value.create.assert_not_called()

    def test_rejects_permissions_in_body(self):
        service = self._service_returning({"id": "x"})
        with pytest.raises(DriveConnectorError, match="permissions"):
            safe_drive_create(
                service,
                body={
                    "name": "f.txt",
                    "parents": ["folder-1"],
                    "permissions": [{"type": "any" "one", "role": "reader"}],
                },
            )
        service.files.return_value.create.assert_not_called()

    def test_passes_through_valid_create(self):
        service = self._service_returning({"id": "file-123", "webViewLink": "u"})
        out = safe_drive_create(
            service,
            body={"name": "f.txt", "parents": ["folder-1"]},
            fields="id,webViewLink",
        )
        assert out == {"id": "file-123", "webViewLink": "u"}
        service.files.return_value.create.assert_called_once()
        _, kwargs = service.files.return_value.create.call_args
        assert kwargs["body"]["parents"] == ["folder-1"]
        assert kwargs["fields"] == "id,webViewLink"

    def test_media_body_threaded(self):
        service = self._service_returning({"id": "f"})
        media_sentinel = object()
        safe_drive_create(
            service,
            body={"name": "f.png", "parents": ["folder-1"]},
            media_body=media_sentinel,
            fields="id",
        )
        _, kwargs = service.files.return_value.create.call_args
        assert kwargs["media_body"] is media_sentinel


# ─────────────────────────────────────────────────────────────────────────────
# Repo-wide regression guard — no automation applies public link-sharing (WS8)
# ─────────────────────────────────────────────────────────────────────────────

class TestNoPublicShareInCodebase:
    """The runtime safe_drive_create() guard covers files().create bodies, but the
    real anyone-with-link vector is the Drive permissions-create endpoint, which a
    create-body guard cannot see. This static scan fails if any code path under
    src/cora or scripts/ introduces public Drive sharing.

    Patterns are assembled from fragments so this test file never matches itself.
    """

    def _scan_dirs(self):
        pkg_root = Path(cora.__file__).resolve().parent          # .../src/cora
        repo_root = pkg_root.parent.parent                       # repo root
        return [d for d in (pkg_root, repo_root / "scripts") if d.exists()]

    def test_no_public_share_constructs(self):
        anyone = "any" + "one"
        pat_link = re.compile(anyone + "WithLink")
        pat_type = re.compile(r"""['"]type['"]\s*:\s*['"]""" + anyone + r"""['"]""")
        pat_perm = re.compile(r"\.permissions\(\)\s*\.\s*create\(")
        patterns = [(pat_link, "anyone-with-link literal"),
                    (pat_type, "type:anyone permission body"),
                    (pat_perm, "permissions-create call")]

        offenders: list[str] = []
        scanned = 0
        for d in self._scan_dirs():
            for py in d.rglob("*.py"):
                if py.name == Path(__file__).name:
                    continue  # this test file references the patterns intentionally
                scanned += 1
                text = py.read_text(encoding="utf-8", errors="ignore")
                for pat, label in patterns:
                    if pat.search(text):
                        offenders.append(f"{py}: {label}")

        assert scanned > 0, "grep-guard scanned no files — path resolution broke"
        assert not offenders, (
            "WS8: automation must never apply public Drive sharing. Found:\n"
            + "\n".join(offenders)
        )
