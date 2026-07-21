"""WS1: Cora's own build/audit/forensic docs must never enter the KB.

Covers the shared predicate (kb_exclusions) used by BOTH the ingest path
(incremental_sync_static) and the one-time purge (purge_cora_internal_kb), plus
the file_to_document wiring in the static sync script.
"""

import sys
from pathlib import Path

from cora.kb_exclusions import (
    KB_EXCLUDED_FOLDER_IDS,
    is_copa_bhrf_path,
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
        # self-audit / review / sweep docs are the self-diagnostic class -> TARGETED
        # (review HIGH finding: these escaped the default purge + re-ingested before).
        "2026-06-13_fndr_cora-slack-sweep-bug-audit.md",
        "2026-06-09_fndr_cora-slack-sweep-audit-and-plan.md",
        "2026-06-08_fndr_cora-slack-comms-review.md",
        "2026-06-11_fndr_cora-14-day-infra-review.md",
        "2026-06-11_fndr_cora-knowledge-review-slack-sweep.md",
        "2026-06-16_fndr_cora-exec-summary.md",          # "Forensic Audit Executive Summary"
        # underscore-delimited forms MUST match too (review-2 HIGH: \b is not a
        # boundary at "_"; we normalize _->- before matching).
        "CORA_IMPROVEMENT_BACKLOG.md",
        "cora_audit.md",
        "cora_forensic_report.md",
        "cora_rebuild_execution_log.md",
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

    # --- BROAD-only: long-tail Cora ops docs caught only with broad=True ---
    BROAD_ONLY = [
        "2026-06-08_fndr_cora-scaling-memory-game-plan.md",
        "2026-06-16_fndr_cora-redesign-overhaul-proposal.md",
        "2026-06-06_fndr_cora-team-training-manual.md",
        "cora-tool-ship-checklist.md",
        "2026-06-18_fndr_cora-polar-mcp-auth-fix.md",
    ]

    # --- sub-word collisions: a build keyword INSIDE a larger word must NOT fire
    # in either scope (review MEDIUM 2b -- \b token anchoring) ---
    SUBWORD_SPARED = [
        "cora-fixed-assets-register.md",     # 'fix' inside 'fixed'
        "cora-fixtures-list.md",             # 'fix' inside 'fixtures'
        "cora-planning-retreat.md",          # 'plan' inside 'planning'
        "cora-specification-of-brand.md",    # 'spec' inside 'specification'
        "cora-debrief.md",                   # 'brief' inside 'debrief'
        "cora-infrastructure-doc.md",        # 'infra' inside 'infrastructure'
    ]

    # --- protected families with a build-keyword SUFFIX must still be spared in
    # both scopes (review MEDIUM 2a -- _LEGIT_FAMILY_RE negative guard) ---
    PROTECTED_SUFFIX_SPARED = [
        "cora-wishlist-review.md",
        "f3-brand-assets-cora-reference-and-comms.md",
        "slack-to-cora-mapping-spec.md",
        "cora-f3-monitor-privacy-review.md",
        "osn_cora-wishlist-spec.md",
    ]

    # --- 'cora' as a SUBSTRING inside another word is NOT a cora token (review-3
    # HIGH: missing left boundary). These must be spared in BOTH scopes. ---
    CORA_SUBSTRING_SPARED = [
        "pecora_audit.md",                 # surname Pecora
        "pecora-dairy-invoice-review.pdf",
        "decora-findings.md",
        "mancora-beach-review.pdf",        # place Mancora
        "incora-2026-audit.pdf",           # vendor Incora
        "decorations.log",
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

    def test_subword_collisions_spared_both_scopes(self):
        # \b anchoring: a keyword inside a larger word must never fire.
        for name in self.SUBWORD_SPARED:
            assert not is_cora_internal_title(name), f"sub-word over-match (targeted): {name}"
            assert not is_cora_internal_title(name, broad=True), f"sub-word over-match (broad): {name}"

    def test_protected_family_suffix_variants_spared(self):
        # The negative guard keeps the named business-doc families safe even when a
        # SOFT build keyword is appended.
        for name in self.PROTECTED_SUFFIX_SPARED:
            assert not is_cora_internal_title(name), f"protected family purged (targeted): {name}"
            assert not is_cora_internal_title(name, broad=True), f"protected family purged (broad): {name}"

    def test_family_with_strong_keyword_is_caught(self):
        # review-2 MEDIUM: the family guard is a NARROWING refinement, not an early veto.
        # A family name that ALSO carries a STRONG build keyword is a genuine build doc.
        for name in ["cora-mapping-rebuild-execution-log.md",
                     "cora-reference-forensic-findings.md",
                     "cora-mapping-audit.md"]:
            assert is_cora_internal_title(name), f"family+strong should be caught: {name}"

    def test_underscore_delimited_keywords_caught(self):
        # review-2 HIGH: \b is not a boundary at "_"; we normalize _->- so these match.
        for name in ["CORA_IMPROVEMENT_BACKLOG.md", "cora_audit.md",
                     "cora_forensic_report.md", "cora_rebuild_execution_log.md",
                     "cora_review.md"]:
            assert is_cora_internal_title(name), f"underscore form should match: {name}"
        # but an underscore-delimited SUB-word must still be spared
        assert not is_cora_internal_title("cora_fixed_assets.md")  # 'fixed' != 'fix'

    def test_slash_in_drive_filename_not_mangled(self):
        # A Drive display name can contain "/" (e.g. a date). We must not path-split a
        # filename and lose the cora- token.
        assert is_cora_internal_title("cora-rebuild-execution-log 6/4.md")
        # space-named human notes ("CORA Task Notes 6/4") have no cora- token -> spared
        assert not is_cora_internal_title("CORA Task Notes 6/4")

    def test_ingest_broad_catches_review_escapees(self):
        # review-2 HIGH: docs that escaped the targeted ingest guard. The guard now uses
        # broad scope, so these are blocked at ingest.
        for name in ["2026-06-18_fndr_cora-code-meeting-actions-pull.md",
                     "2026-06-14_fndr_cora-connections-cowork-bootstrap.md",
                     "2026-06-17_fndr_cora-slack-archive-staged.md",
                     "2026-06-10_fndr_cora-per-user-email-drive-access-build.md",
                     "2026-06-12_fndr_cowork-cora-gmail-fireflies-kb-backfill.md"]:
            assert is_cora_internal_title(name, broad=True), f"broad ingest should block: {name}"

    def test_cora_substring_in_other_word_spared(self):
        # The cora token needs a LEFT boundary -- 'cora' inside pecora/decora/mancora/
        # incora/decorations is not a cora token and must never be purged.
        for name in self.CORA_SUBSTRING_SPARED:
            assert not is_cora_internal_title(name), f"substring over-match (targeted): {name}"
            assert not is_cora_internal_title(name, broad=True), f"substring over-match (broad): {name}"

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


class TestIsCopaBhrfPath:
    """2026-07-21 KB cleanup (§2c): the LEX copa-bhrf NDA folder is permanently
    KB-excluded. Segment-based -- never a 'copa' substring."""

    def test_project_folder_paths_excluded(self):
        for p in (
            r"08-Lexington-Services\projects\copa-bhrf\CLAUDE.md",
            r"08-Lexington-Services\projects\copa-bhrf\_notes\2026-05-15-fireflies-context-doc.md",
            r"08-Lexington-Services\projects\copa-bhrf\LBHS - COPA Research Project\_notes\x.md",
            "08-Lexington-Services/projects/copa-bhrf/y.md",  # forward slash
            r"G:\My Drive\HJR-Founder-OS\08-Lexington-Services\projects\copa-bhrf\_notes\a.md",
        ):
            assert is_copa_bhrf_path(p), p

    def test_copa_substring_not_over_matched(self):
        # 'copa' inside Maricopa / Copayment / copack / Chrysler Voyager is NOT copa-bhrf.
        for p in (
            r"02-F3-Energy\manufacturing\fwd-maricopa-county-inspection.pdf",
            r"_shared\Division_Operations_Manual_Chapter_431 Copayment.pdf",
            r"02-F3-Energy\bluechip-copack-knowledge.md",
            r"08-Lexington-Services\financial\lex-fleet-chrysler-voyager.pdf",
            r"08-Lexington-Services\projects\other\file.md",
            "",
        ):
            assert not is_copa_bhrf_path(p), p

    def test_copa_folder_id_registered_for_drive_sweep(self):
        # The drive_sweep enumeration exclusion relies on the folder-id being present.
        assert "112C7ljGRI5VO_ic66fVGQk4kf6IC40HQ" in KB_EXCLUDED_FOLDER_IDS
