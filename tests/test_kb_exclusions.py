"""WS1: Cora's own build/audit/forensic docs must never enter the KB.

Covers the shared predicate (kb_exclusions) used by BOTH the ingest path
(incremental_sync_static) and the one-time purge (purge_cora_internal_kb), plus
the file_to_document wiring in the static sync script.
"""

import sys
from pathlib import Path

from cora.kb_exclusions import (
    is_cora_internal_path,
    is_cora_internal_source_id,
    is_cora_internal_title,
)

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


class TestIsCoraInternalTitle:
    """The WS1-completion fix: drive_sweep copies Founder-OS Drive files into the KB
    under a Drive-file-id source_id with the filename in `title`. The path rules
    can't see them, so we match the title. These are the REAL live filenames."""

    # --- TARGETED: genuine build/audit/forensic artifacts -> caught ---
    CAUGHT = [
        "2026-06-16_fndr_cora-rebuild-execution-log.md",
        "2026-06-16_fndr_cora-rebuild-COWORK-CASCADE-REPORT.md",
        "2026-06-16_fndr_cora-forensic-findings-report.md",
        "2026-06-16_fndr_cora-audit-addendum-runtime-evidence.md",
        "2026-06-16_fndr_cora-rebuild-master-build-plan.md",
        "2026-06-18_fndr_cora-north-star-and-two-track-plan.md",
        "2026-06-17_fndr_cora-slack-incident-triage.md",
        "2026-06-17_fndr_cora-rebuild-phase3-scope.md",
        "2026-06-08_fndr_cora-code-prompt-caching-split-phase0.md",
    ]

    # --- legit business / Cora-adjacent docs that MUST be spared in BOTH scopes ---
    SPARED = [
        "f3-brand-assets-cora-reference.md",
        "2026-05-23_lex_cora-wishlist.md",
        "2026-06-11_lex_llc-channel-routing-cora-mapping.md",
        "cora-f3-monitor-privacy-policy.md",
        "CLAUDE.md",                       # founder/entity brief -- never exclude
        "decisions.md",
        "Christmas Decorations",           # substring 'cora' inside 'deCORAtions'
        "F3 Energy Koozie order.md",
    ]

    # --- BROAD-only: Cora ops docs caught only with broad=True ---
    BROAD_ONLY = [
        "2026-06-08_fndr_cora-scaling-memory-game-plan.md",
        "2026-06-16_fndr_cora-redesign-overhaul-proposal.md",
        "2026-06-06_fndr_cora-team-training-manual.md",
        "cora-tool-ship-checklist.md",
        "2026-06-11_fndr_cora-14-day-infra-review.md",
        "2026-06-13_fndr_cora-code-6-13-sweep.md",
        "2026-06-18_fndr_cora-polar-mcp-auth-fix.md",
    ]

    def test_targeted_catches_build_docs(self):
        for name in self.CAUGHT:
            assert is_cora_internal_title(name), name
            assert is_cora_internal_title(name, broad=True), name  # broad is a superset

    def test_spares_legit_docs_both_scopes(self):
        for name in self.SPARED:
            assert not is_cora_internal_title(name), f"targeted leak-protect FAIL: {name}"
            assert not is_cora_internal_title(name, broad=True), f"broad leak-protect FAIL: {name}"

    def test_runtime_logs_caught(self):
        for log in ("cora-2026-06-06.log", "cora-2026-05-28.log", "cora_debug.log"):
            assert is_cora_internal_title(log), log
        # a non-cora log is not ours
        assert not is_cora_internal_title("server-2026-06-06.log")
        # 'cora' inside another word is not a cora log
        assert not is_cora_internal_title("decorations.log")

    def test_broad_only_docs(self):
        for name in self.BROAD_ONLY:
            assert not is_cora_internal_title(name), f"should be targeted-spared: {name}"
            assert is_cora_internal_title(name, broad=True), f"broad should catch: {name}"

    def test_title_with_path_prefix_uses_basename(self):
        # Some titles arrive with a folder prefix; match on the basename.
        assert is_cora_internal_title("00-Founder/cora-forensic-findings-report.md")
        assert is_cora_internal_title(r"sub\cora-2026-06-06.log")

    def test_empty_and_none_safe(self):
        assert not is_cora_internal_title("")
        assert not is_cora_internal_title(None)  # type: ignore[arg-type]

    def test_log_rule_also_on_source_id_and_path(self):
        # The .log rule folds into the shared predicate, so a cora-*.log ingested
        # under any path/source_id is caught everywhere, not just by title.
        assert is_cora_internal_source_id("logs/cora-2026-06-06.log")
        assert is_cora_internal_path(Path("C:/x/cora-2026-06-06.log"))
