"""Tests for the recurring LEX Dump Folder sync (2026-06-11).

Covers:
  - resolve_sub_entity tagging: uniform LEX-LLC for ALL dump-folder content,
    including the DDD Policies tree (Harrison directive 2026-06-11 PM,
    superseding the GM-level tagging shipped earlier the same day -- the LLC
    team lives in #llc-* channels where the strict filter excludes NULL)
  - walk_folder recursion: subfolders, folder shortcuts, policy_tree
    provenance propagation from the "DDD Policies" / "EVV Documents" names
  - store Step 0 opt-out: metadata.lex_gm_level=True keeps a LEX doc at
    sub_entity NULL even when its content carries sub-entity keywords
    (generic store mechanism -- no longer used by this script, but locked)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import run_lex_dump_folder_sync as sync  # noqa: E402

from cora.knowledge_base import embeddings  # noqa: E402
from cora.knowledge_base.store import Document, KnowledgeBase  # noqa: E402

_DIM = 1536


def _unit_vec() -> list:
    vec = [0.0] * _DIM
    vec[0] = 1.0
    return vec


@pytest.fixture(autouse=True)
def patch_embeddings(monkeypatch):
    monkeypatch.setattr(embeddings, "embed_texts", lambda texts: [_unit_vec() for _ in texts])
    monkeypatch.setattr(embeddings, "embed_query", lambda q: _unit_vec())


# ---------------------------------------------------------------------------
# resolve_sub_entity
# ---------------------------------------------------------------------------

class TestResolveSubEntity:
    def test_policy_manual_tagged_llc(self):
        f = {"name": "DDD Complete Operations Manual.pdf", "policy_tree": True}
        assert sync.resolve_sub_entity(f) == ("LEX-LLC", True)

    def test_root_file_tagged_llc(self):
        f = {"name": "Employee Payrates.xlsx", "policy_tree": False}
        assert sync.resolve_sub_entity(f) == ("LEX-LLC", False)

    def test_evv_faq_tagged_llc_with_policy_provenance(self):
        f = {"name": "EVV_Live-InCaregiverFAQ.pdf", "policy_tree": True}
        assert sync.resolve_sub_entity(f) == ("LEX-LLC", True)

    def test_never_returns_null_sub_entity(self):
        """Regression for the 6/11 directive: NULL (GM-level) chunks are
        invisible in #llc-* channels, where the DDD policy consumers live."""
        for f in (
            {"name": "anything.pdf", "policy_tree": True},
            {"name": "anything.pdf", "policy_tree": False},
            {"name": "anything.pdf"},
        ):
            sub_entity, _ = sync.resolve_sub_entity(f)
            assert sub_entity == "LEX-LLC"


# ---------------------------------------------------------------------------
# walk_folder
# ---------------------------------------------------------------------------

def _fake_service(listing_by_folder: dict, file_meta_by_id: dict | None = None):
    """Minimal Drive v3 stub: files().list(q=...) + files().get(fileId=...)."""
    svc = MagicMock()

    def list_side_effect(**kwargs):
        q = kwargs.get("q", "")
        folder_id = q.split("'")[1] if "'" in q else ""
        result = MagicMock()
        result.execute.return_value = {"files": listing_by_folder.get(folder_id, [])}
        return result

    def get_side_effect(**kwargs):
        fid = kwargs.get("fileId")
        result = MagicMock()
        result.execute.return_value = (file_meta_by_id or {}).get(fid, {})
        return result

    svc.files.return_value.list.side_effect = list_side_effect
    svc.files.return_value.get.side_effect = get_side_effect
    return svc


_PDF = "application/pdf"
_FOLDER = "application/vnd.google-apps.folder"
_SHORTCUT = "application/vnd.google-apps.shortcut"


class TestWalkFolder:
    def test_flat_listing(self):
        svc = _fake_service({"root": [
            {"id": "f1", "name": "Billing Claims.xlsx",
             "mimeType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
        ]})
        files = sync.walk_folder(svc, "root")
        assert len(files) == 1
        assert files[0]["policy_tree"] is False

    def test_subfolder_recursion(self):
        svc = _fake_service({
            "root": [{"id": "sub", "name": "Misc", "mimeType": _FOLDER}],
            "sub": [{"id": "f2", "name": "doc.pdf", "mimeType": _PDF}],
        })
        files = sync.walk_folder(svc, "root")
        assert [f["id"] for f in files] == ["f2"]
        assert files[0]["policy_tree"] is False

    def test_ddd_policies_folder_marks_policy_tree(self):
        svc = _fake_service({
            "root": [{"id": "ddd", "name": "DDD Policies", "mimeType": _FOLDER}],
            "ddd": [
                {"id": "m1", "name": "DDD Complete Medical Manual.pdf", "mimeType": _PDF},
                {"id": "evv", "name": "EVV Documents", "mimeType": _FOLDER},
            ],
            "evv": [{"id": "m2", "name": "EVV_Live-InCaregiverFAQ.pdf", "mimeType": _PDF}],
        })
        files = sync.walk_folder(svc, "root")
        by_id = {f["id"]: f for f in files}
        assert by_id["m1"]["policy_tree"] is True
        assert by_id["m2"]["policy_tree"] is True

    def test_folder_shortcut_followed_with_policy_tree(self):
        """The real dump folder holds 'DDD Policies' as a SHORTCUT to a folder."""
        svc = _fake_service({
            "root": [{
                "id": "sc1", "name": "DDD Policies", "mimeType": _SHORTCUT,
                "shortcutDetails": {"targetId": "target", "targetMimeType": _FOLDER},
            }],
            "target": [{"id": "m3", "name": "DDD Complete Provider Manual.pdf", "mimeType": _PDF}],
        })
        files = sync.walk_folder(svc, "root")
        assert [f["id"] for f in files] == ["m3"]
        assert files[0]["policy_tree"] is True

    def test_file_shortcut_resolved(self):
        svc = _fake_service(
            {"root": [{
                "id": "sc2", "name": "Rate Book link", "mimeType": _SHORTCUT,
                "shortcutDetails": {"targetId": "rb", "targetMimeType": _PDF},
            }]},
            file_meta_by_id={"rb": {"id": "rb", "name": "Rate_Book.pdf", "mimeType": _PDF}},
        )
        files = sync.walk_folder(svc, "root")
        assert [f["id"] for f in files] == ["rb"]

    def test_depth_cap(self):
        # root -> a -> a -> ... self-recursive folder chain stops at MAX_DEPTH
        svc = _fake_service({
            "root": [{"id": "root", "name": "loop", "mimeType": _FOLDER}],
        })
        files = sync.walk_folder(svc, "root")
        assert files == []


# ---------------------------------------------------------------------------
# Store Step 0 opt-out: metadata.lex_gm_level
# ---------------------------------------------------------------------------

@pytest.fixture
def kb(tmp_path):
    db = KnowledgeBase(tmp_path / "test_kb.db")
    yield db
    db.close()


def _stored_sub_entities(kb: KnowledgeBase, source_id: str) -> set:
    cur = kb._conn.cursor()
    cur.execute(
        "SELECT DISTINCT sub_entity FROM knowledge_chunks WHERE source_id = ?",
        (source_id,),
    )
    return {row[0] for row in cur.fetchall()}


class TestGmLevelOptOut:
    def test_gm_level_flag_blocks_auto_detection(self, kb):
        """A GM-flagged policy manual whose text screams HCBS stays NULL."""
        kb.upsert_documents([Document(
            source="drive_asset",
            source_id="manual-1",
            entity="LEX",
            title="DDD Complete Provider Manual.pdf",
            content="HCBS providers delivering Supported Living and Day Program "
                    "services must comply with EVV requirements.",
            metadata={"lex_gm_level": True},
        )])
        assert _stored_sub_entities(kb, "manual-1") == {None}

    def test_without_flag_auto_detection_still_fires(self, kb):
        """Regression: the opt-out must not weaken default Step 0 behavior."""
        kb.upsert_documents([Document(
            source="drive_asset",
            source_id="manual-2",
            entity="LEX",
            title="HCBS billing report",
            content="Supported Living placements and HCBS claims for the quarter.",
        )])
        assert _stored_sub_entities(kb, "manual-2") == {"LEX-LLC"}

    def test_explicit_sub_entity_with_flag_kept(self, kb):
        """Explicit tag always wins regardless of the flag."""
        kb.upsert_documents([Document(
            source="drive_asset",
            source_id="manual-3",
            entity="LEX",
            sub_entity="LEX-LLC",
            title="Client roster",
            content="Client assignments for the LLC day program.",
            metadata={"lex_gm_level": False},
        )])
        assert _stored_sub_entities(kb, "manual-3") == {"LEX-LLC"}

    def test_gm_level_doc_visible_in_gm_scope_not_sub_entity_scope(self, kb):
        """GM-level chunks surface in #lex-* (no sub_entity filter) but the
        strict sub-entity filter excludes them -- locked siloing behavior."""
        kb.upsert_documents([Document(
            source="drive_asset",
            source_id="manual-4",
            entity="LEX",
            title="DDD Complete Operations Manual.pdf",
            content="Live-in caregivers and EVV responsibilities are defined here.",
            metadata={"lex_gm_level": True},
        )])
        gm_results = kb.search("EVV live-in caregivers", entity="LEX")
        assert any(r.source_id == "manual-4" for r in gm_results)
        llc_results = kb.search("EVV live-in caregivers", entity="LEX", sub_entity="LEX-LLC")
        assert not any(r.source_id == "manual-4" for r in llc_results)
