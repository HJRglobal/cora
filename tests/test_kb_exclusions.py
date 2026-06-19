"""WS1: Cora's own build/audit/forensic docs must never enter the KB.

Covers the shared predicate (kb_exclusions) used by BOTH the ingest path
(incremental_sync_static) and the one-time purge (purge_cora_internal_kb), plus
the file_to_document wiring in the static sync script.
"""

import sys
from pathlib import Path

from cora.kb_exclusions import is_cora_internal_path, is_cora_internal_source_id

_DRIVE = r"G:\My Drive\HJR-Founder-OS"


class TestIsCoraInternalPath:
    def test_workspace_build_doc_excluded(self):
        p = Path(_DRIVE) / "_shared" / "projects" / "cora" / "2026-06-16_fndr_cora-rebuild-execution-log.md"
        assert is_cora_internal_path(p)

    def test_workspace_claude_md_excluded(self):
        p = Path(_DRIVE) / "_shared" / "projects" / "cora" / "CLAUDE.md"
        assert is_cora_internal_path(p)

    def test_workspace_design_system_prompt_excluded(self):
        # System-prompt text under the workspace must never be retrievable from KB.
        p = Path(_DRIVE) / "_shared" / "projects" / "cora" / "design" / "system-prompts" / "fndr.md"
        assert is_cora_internal_path(p)

    def test_sibling_projects_kept(self):
        for name in ("reddit-strategy", "wikipedia-strategy", "gmail-deep-dive", "asana-deep-dive"):
            p = Path(_DRIVE) / "_shared" / "projects" / name / "notes.md"
            assert not is_cora_internal_path(p), name

    def test_entity_claude_md_kept(self):
        assert not is_cora_internal_path(Path(_DRIVE) / "02-F3-Energy" / "CLAUDE.md")

    def test_business_doc_kept(self):
        assert not is_cora_internal_path(
            Path(_DRIVE) / "00-Founder" / "social-ad-accounts-inventory-2026-06-05.md"
        )

    def test_cora_build_doc_copied_elsewhere_caught_by_filename(self):
        # A forensic/code-prompt doc copied into a session-capture folder is still caught.
        p = Path(_DRIVE) / "00-Founder" / "_session-captures" / "2026-06" / "cora-forensic-findings-report.md"
        assert is_cora_internal_path(p)

    def test_cora_code_prompt_copied_elsewhere_caught(self):
        p = Path(_DRIVE) / "00-Founder" / "2026-06-19_fndr_cora-20-report-synthesis-and-path.md"
        assert is_cora_internal_path(p)

    def test_training_manual_caught_only_inside_workspace(self):
        # Inside the workspace -> excluded by folder. Outside -> kept (its filename
        # has no build keyword; it is a staff-facing doc, not org-poisoning metadata).
        inside = Path(_DRIVE) / "_shared" / "projects" / "cora" / "2026-06-06_fndr_cora-team-training-manual.md"
        outside = Path(_DRIVE) / "01-HJR-Global" / "cora-team-training-manual.md"
        assert is_cora_internal_path(inside)
        assert not is_cora_internal_path(outside)


class TestIsCoraInternalSourceId:
    def test_backslash_source_id_excluded(self):
        assert is_cora_internal_source_id(
            r"_shared\projects\cora\2026-06-16_fndr_cora-rebuild-execution-log.md"
        )

    def test_forwardslash_source_id_excluded(self):
        assert is_cora_internal_source_id("_shared/projects/cora/CLAUDE.md")

    def test_non_cora_source_ids_kept(self):
        assert not is_cora_internal_source_id(r"02-F3-Energy\CLAUDE.md")
        assert not is_cora_internal_source_id("_shared/projects/reddit-strategy/notes.md")

    def test_empty_and_none_safe(self):
        assert not is_cora_internal_source_id("")
        assert not is_cora_internal_source_id(None)  # type: ignore[arg-type]


class TestStaticSyncWiring:
    def _load_sync(self):
        repo = Path(__file__).resolve().parents[1]
        scripts_dir = str(repo / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        import incremental_sync_static as sync  # noqa: PLC0415
        return sync

    def test_file_to_document_skips_cora_internal(self, tmp_path):
        sync = self._load_sync()
        d = tmp_path / "_shared" / "projects" / "cora"
        d.mkdir(parents=True)
        f = d / "2026-06-16_fndr_cora-rebuild-execution-log.md"
        f.write_text("# build log\nsome content here", encoding="utf-8")
        assert sync.file_to_document(f) is None

    def test_file_to_document_ingests_normal(self, tmp_path):
        sync = self._load_sync()
        d = tmp_path / "02-F3-Energy" / "notes"
        d.mkdir(parents=True)
        f = d / "brand.md"
        f.write_text("# brand\nreal org knowledge", encoding="utf-8")
        doc = sync.file_to_document(f)
        assert doc is not None
        assert doc.source == "static_md"
