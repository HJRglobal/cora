#!/usr/bin/env python3
r"""Founder-OS KB cleanup: archive-move + KB purge, ONE reviewed pass (2026-07-21).

Physically relocates every APPROVED stale/superseded ``.md`` into a mirrored,
path-preserving ``_archive/`` tree (reversible, with a source->dest manifest) AND
purges the matching chunks from Cora's vector KB (cora_kb.db), so Cora stops
answering from archived content -- leaving all HOLD/KEEP items untouched.

Scope is decision-complete and locked in the disposition list
(``00-Founder/2026-07-20_fndr_kb-cleanup-pass2-disposition-list.md`` + the
Pass-1 banner'd files + the §2c held-item lock). This script MECHANICALLY
translates that list into disk moves + KB deletes; it does NOT re-adjudicate.

SAFETY RAILS (non-negotiable):
  * Archive, never delete files. Move to _archive/<relpath> (reversible; manifest).
  * Dry-run is the DEFAULT and is read-only (safe even while Cora is live).
  * --apply performs the move + the KB purge. STOP Cora + BACK UP cora_kb.db first
    (the delete contends with live writes; the purge is only reversible from a backup).
  * Layered over-inclusion defense: narrow per-cluster globs -> KEEP-as-class filter
    -> HOLD hard-guard (ABORTS if a held/sensitive path ever enters the set) ->
    the human-reviewed manifest.
  * The drive-copy purge is SELF-GUARDING: a filename is only purged from the Drive
    copies (drive_sweep/drive_asset) when that exact title maps to <= 2 distinct
    Drive file-ids (distinctive dated names = 1; generic scaffolding names like
    00-README.md map to 30+ and are refused, logged, and left in place).

Usage (from the repo root):
    .venv\Scripts\python.exe scripts\archive_founder_os_kb.py                 # DRY-RUN (default)
    .venv\Scripts\python.exe scripts\archive_founder_os_kb.py --apply         # MOVE + PURGE (Cora stopped)
    .venv\Scripts\python.exe scripts\archive_founder_os_kb.py --skip-purge    # move only (writes a manifest with the purge chunk_ids persisted)
    .venv\Scripts\python.exe scripts\archive_founder_os_kb.py --apply --skip-move --from-manifest logs\...manifest.json  # finish the purge (reads persisted chunk_ids -- does NOT re-glob the moved tree)
    .venv\Scripts\python.exe scripts\archive_founder_os_kb.py --revert logs\archive-founder-os-manifest-<ts>.json
    .venv\Scripts\python.exe scripts\archive_founder_os_kb.py --db <path>     # target a specific KB db

After --apply: reclaim disk with scripts\reclaim_kb_space.py, restart Cora
(activates the copa-bhrf kb_exclusions change), then verify (see the session runbook).

Exit codes: 0 ok, 1 fatal, 2 HOLD-guard tripped (a held/sensitive path entered the set).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

# The move/purge/manifest engine now lives in the shared, reusable core module
# (src/cora/kb_archive.py). This script keeps its disposition-list clusters + all
# guard constants + CLI/orchestration and is a thin config+CLI over that core.
from cora import kb_archive  # noqa: E402
from cora.kb_archive import ArchiveConfig, HoldGuardTripped  # noqa: E402

FOUNDER_OS_ROOT = Path(r"G:\My Drive\HJR-Founder-OS")
ARCHIVE_ROOT = FOUNDER_OS_ROOT / "_archive"
KB_DB_PATH = _REPO / "data" / "cora_kb.db"
LOG_DIR = _REPO / "logs"
_BATCH = 500

# ─────────────────────────────────────────────────────────────────────────────
# copa-bhrf (decision §2c): dedup the loose duplicate + purge ALL copa-bhrf chunks
# + block re-ingest (the kb_exclusions edits). The CANONICAL copy stays IN PLACE.
# ─────────────────────────────────────────────────────────────────────────────
COPA_PURGE_GLOB = r"08-Lexington-Services\projects\copa-bhrf\*"  # every copa-bhrf static_md chunk
COPA_LOOSE_DUP = r"08-Lexington-Services\projects\copa-bhrf\_notes\2026-05-15-fireflies-context-doc.md"
# Distinctive .md filenames of the copa-bhrf project notes that ALSO have Drive
# copies (verified 1 file-id each). NOT the Fireflies meeting transcripts
# ("LBHS COPA" / "Virtual Voyager Copa Model") -- those live under
# _shared\meetings\Fireflies Meetings\, are OUT of the copa-bhrf project-folder
# scope this session is authorized for, and are FLAGGED (not purged).
COPA_DRIVE_TITLES = (
    "2026-05-15-fireflies-context-doc.md",
    "2026-05-15-fireflies-context-doc-supplement-tldr-and-reframings.md",
)

# ─────────────────────────────────────────────────────────────────────────────
# HARD HOLD GUARD -- these must NEVER be archived. If any path containing one of
# these segments enters the computed archive set, the run ABORTS (exit 2). This
# is defense-in-depth on top of the narrow cluster globs. copa-bhrf is guarded
# separately (only the one loose-dup path is permitted).
# ─────────────────────────────────────────────────────────────────────────────
HOLD_SEGMENTS = frozenset({
    "watchtower",       # live $200K deal, 7/31 forfeiture cliff (TOM 1y) -- keep whole record
    "oneamerica",       # PERSONAL / KB-excluded / out of scope
    "capital-raise",    # HIGHLY CONFIDENTIAL / KB-excluded / out of scope
    "travel-points",    # PERSONAL / KB-excluded / out of scope
})

# Soft KEEP guards (filtered from glob candidates + logged; NOT abort). These are
# KEEP decisions from §2c that my clusters shouldn't reach anyway, guarded for
# defense-in-depth. Matched as a lowercase substring of the backslash relpath.
KEEP_SUBSTRINGS = (
    "bootstrap-context-2026-05-24",   # cora orientation kit, still cross-referenced (§2c.3)
    "kb-cleanup-ad-media-disposition-list",  # a parallel workstream's list (§1j)
    "2026-07-03_hjr-pb_linkedin-audit-and-optimization-plan",  # not superseded (§2c.4)
)

# KEEP-as-class (never archive from a GLOB match). Explicit entries are ALSO
# subject to this filter UNLESS on the CLASS_EXCEPTIONS allowlist below -- so an
# accidental memory/** or CLAUDE.md explicit path can never slip through (the
# D-051 critic's LEAK-2). Only the 3 disposition-named class-exceptions bypass.
KEEP_CLASS_BASENAMES = frozenset({
    "claude.md", "readme.md", "bootstrap.txt", "_context.md",
    "canonical-assets.md", "content-calendar.md",
})
KEEP_CLASS_SEGMENTS = frozenset({
    "production", "_session-captures", "_strategy-memos", "memory", "playbooks",
})
KEEP_CLASS_BASENAME_SUBSTR = ("brand-guidelines",)

# The ONLY explicit paths permitted to bypass KEEP-as-class -- they sit under a
# class segment but the disposition explicitly names them for archival:
#   - the Pass-1 press-pipeline session-capture (§1j, banner'd superseded)
#   - the 2 non-evergreen playbooks (§1i)
CLASS_EXCEPTIONS = frozenset({
    r"00-Founder\_session-captures\2026-07\2026-07-08_fndr_press-pipeline-review-and-tracker-update.md",
    r"_shared\playbooks\harrison-out-of-office-DRAFT.md",
    r"_shared\playbooks\chat-consolidation-2026-05-14.md",
})

# Drive-copy purge self-guard: refuse any candidate title that maps to more than
# this many distinct Drive file-ids (generic scaffolding names collide portfolio-
# wide). Also a belt denylist of the known generic scaffolding basenames.
_DRIVE_TITLE_MAX_FILEIDS = 2
_SCAFFOLD_BASENAMES = frozenset({
    "00-readme.md", "01-asana-tasks-to-create.md", "02-decisions-md-appends.md",
    "03-slack-summaries.md", "04-project-notes.md", "05-asana-cross-ref.md",
    "06-closure-proposals.md", "readme.md", "claude.md", "_context.md",
    "canonical-assets.md", "claude-ai-project-setup.md", "cascade-log.md",
})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("archive-founder-os-kb")


# ─────────────────────────────────────────────────────────────────────────────
# ARCHIVE CLUSTERS -- the verified translation of the disposition list.
# Each cluster: globs (relative, expanded against the live tree), explicit (exact
# relpaths always archived, bypassing the KEEP-as-class filter), keep (relpaths a
# glob would catch but MUST be kept), expected (disposition-stated count, for
# reconciliation). All paths are BACKSLASH-separated relative to FOUNDER_OS_ROOT.
# NOTE: the narrow/explicit clusters (§1e-§1j) are populated from the verified
# enumeration pass; the clean whole-folder clusters below are already verified
# against the live KB (2026-07-21).
# ─────────────────────────────────────────────────────────────────────────────
ARCHIVE_CLUSTERS: list[dict] = [
    {
        "id": "1a-tag-standup",
        "section": "1a -- Tag-migration effort, REVERSED 6/29",
        "globs": [
            r"00-Founder\tag-standup\**\*.md",
        ],
        "explicit": [
            r"00-Founder\2026-06-23_fndr_cora-to-tag-migration-and-portfolio-standup-plan.md",
        ],
        "keep": [],
        "expected": "~41 folder + 1 plan = 42",
        "purge": True,
    },
    {
        "id": "1b-nightly-sweep",
        "section": "1b -- fireflies-deep-dive nightly-sweep scratch",
        "globs": [
            r"_shared\projects\fireflies-deep-dive\_notes\*-nightly-sweep\**\*.md",
        ],
        "explicit": [
            r"_shared\projects\fireflies-deep-dive\_notes\2026-07-14-nightly-sweep-SKIPPED.md",
        ],
        "keep": [],
        "expected": "~248 files + 1 stub",
        "purge": True,
    },
    {
        "id": "1c-cora-digests-slices",
        "section": "1c + 2b -- cora knowledge-gaps digests + merged audit-slice prompts (cora folder KB-excluded: pure move, no static purge)",
        "globs": [
            r"_shared\projects\cora\knowledge-gaps\*-digest.md",
            r"_shared\projects\cora\_notes\2026-07-02_*cora-code-prompt-audit-slice-0[1-7]*.md",
        ],
        "explicit": [],
        "keep": [],
        "expected": "34 digests + 7 slice prompts",
        "purge": False,
    },
    {
        "id": "1d-hygiene-pending",
        "section": "1d -- executed hygiene-pending-moves scratch (26 of 27)",
        "globs": [
            r"_shared\hygiene-pending-moves\*.md",
        ],
        "explicit": [],
        "keep": [
            r"_shared\hygiene-pending-moves\2026-07-10_fndr_drive-dedup-cleanup-staged.md",
        ],
        "expected": "26 of 27 (keep 1)",
        "purge": True,
    },
    {
        "id": "1e-deep-dives",
        "section": "1e -- May-2026 deep-dive project scratch (gmail/asana/hubspot/drive-cleanup/social)",
        "globs": [],
        "explicit": [
            r"_shared\projects\gmail-deep-dive\_notes\2026-05-17-gmail-triage-top-25-unread-over-14d.md",
            r"_shared\projects\gmail-deep-dive\_notes\2026-05-18-gmail-takeout-36mo-v1.md",
            r"_shared\projects\gmail-deep-dive\_notes\2026-05-18-gmail-takeout-36mo-v2.md",
            r"_shared\projects\gmail-deep-dive\_notes\2026-05-18-gmail-takeout-36mo-v3.md",
            r"_shared\projects\gmail-deep-dive\_notes\2026-05-18-gmail-takeout-36mo-v4.md",
            r"_shared\projects\gmail-deep-dive\_notes\2026-05-21-v4-filing-report.md",
            r"_shared\projects\gmail-deep-dive\_notes\2026-05-21-cascade-draft.md",
            r"_shared\projects\gmail-deep-dive\_notes\2026-05-21-cascade-draft-batch-2.md",
            r"_shared\projects\gmail-deep-dive\_notes\2026-05-21-surprises-2023-05-to-2024-05-draft.md",
            r"_shared\projects\gmail-deep-dive\_notes\2026-05-21-fullbloom-investigation-findings.md",
            r"_shared\projects\gmail-deep-dive\_notes\2026-05-22-resume-prompt.md",
            r"_shared\projects\gmail-deep-dive\_notes\2026-05-21-cascade-draft-2024-2025.md",
            r"_shared\projects\gmail-deep-dive\_notes\esign-misfile-moveplan.md",
            r"_shared\projects\gmail-deep-dive\_notes\esign-moves-log.md",
            r"_shared\projects\gmail-deep-dive\_notes\2026-05-22-cascade-draft-manifest-sweep.md",
            r"_shared\projects\gmail-deep-dive\_notes\2026-05-22-cascade-draft-2025-2026.md",
            r"_shared\projects\gmail-deep-dive\_notes\2026-05-22-cascade-draft-2023-2024.md",
            r"_shared\projects\asana-deep-dive\fndr-portfolio-audit-2026-05-17.md",
            r"_shared\projects\asana-deep-dive\_notes\fndr-portfolio-audit-2026-05-17.md",
            r"_shared\projects\asana-deep-dive\_notes\ufl-july-10-triage-2026-05-17.md",
            r"_shared\projects\asana-deep-dive\2026-05-22_fndr_chrome-agent-prompt-tier4-portfolio-custom-fields.md",
            r"_shared\projects\asana-deep-dive\2026-05-22_hannah_chrome-agent-prompt-asana-cleanup.md",
            r"_shared\projects\hubspot-deep-dive\2026-05-17_f3e_chrome-agent-prompt-contact-backfill.md",
            r"_shared\projects\drive-cleanup\_notes\2026-05-19-storage-tier-audit-findings.md",
            r"_shared\projects\drive-cleanup\_notes\2026-05-20-harrison-ui-action-checklist.md",
            r"_shared\projects\social-data-snapshot-2026-05\_notes\chrome-agent-prompts.md",
        ],
        "keep": [],
        "expected": "26",
        "purge": True,
    },
    {
        "id": "1f-impulse",
        "section": "1f -- F3E Impulse-theme legacy cluster (pure-shopify _notes + runbooks + _shared)",
        "globs": [],
        "explicit": [
            r"02-F3-Energy\projects\pure-shopify-store\_notes\2026-05-22-cai-strat-architecture-pivot-update.md",
            r"02-F3-Energy\projects\pure-shopify-store\_notes\2026-05-22-chrome-agent-domain-addition-prompt.md",
            r"02-F3-Energy\projects\pure-shopify-store\_notes\2026-05-22-energy-store-gap-analysis.md",
            r"02-F3-Energy\projects\pure-shopify-store\_notes\2026-05-22-phase-1-completion-snapshot.md",
            r"02-F3-Energy\projects\pure-shopify-store\_notes\2026-05-22-phase-2-and-3-completion-snapshot.md",
            r"02-F3-Energy\projects\pure-shopify-store\_notes\2026-05-22-pure-store-build-brief-v0.md",
            r"02-F3-Energy\projects\pure-shopify-store\_notes\2026-05-23-final-session-snapshot.md",
            r"02-F3-Energy\projects\pure-shopify-store\_notes\2026-05-23-phase-4-completion-snapshot.md",
            r"02-F3-Energy\projects\pure-shopify-store\_notes\2026-05-23-phase-5-cleanup-and-phase-6-prep-snapshot.md",
            r"02-F3-Energy\projects\pure-shopify-store\_notes\2026-05-23-phases-5-13-through-5-17-completion-snapshot.md",
            r"02-F3-Energy\projects\pure-shopify-store\_notes\2026-05-23-phases-5-18-through-5-29-completion-snapshot.md",
            r"02-F3-Energy\projects\pure-shopify-store\_notes\index-with-image-hero-STAGED.md",
            r"02-F3-Energy\projects\pure-shopify-store\harrison-action-punchlist-v1.md",
            r"02-F3-Energy\projects\pure-shopify-store\customizer-settings-audit-v1.md",
            r"02-F3-Energy\projects\pure-shopify-store\phase-6-cutover-playbook.md",
            r"02-F3-Energy\projects\pure-shopify-store\bdm-walkthrough-guide-2026-05-26.md",
            r"02-F3-Energy\projects\pure-shopify-store\cross-brand-consistency-audit-2026-05-25.md",
            r"02-F3-Energy\projects\pure-shopify-store\collection-pages-state-2026-05-25.md",
            r"02-F3-Energy\projects\pure-shopify-store\bdm-hero-prompts-2026-05-25.md",
            r"02-F3-Energy\projects\pure-shopify-store\strategy-chat-sync-2026-05-25.md",
            r"02-F3-Energy\projects\pure-shopify-store\asset-production-checklist-v1.md",
            r"02-F3-Energy\projects\pure-shopify-store\demo-pdp-status-2026-05-25.md",
            r"02-F3-Energy\projects\pure-shopify-store\nav-menu-audit-2026-05-25.md",
            r"02-F3-Energy\projects\pure-shopify-store\tuesday-publish-runbook-2026-05-26.md",
            r"02-F3-Energy\projects\pure-shopify-store\hero-swap-mutation-ready.md",
            r"02-F3-Energy\projects\pure-shopify-store\screenshot-capture-plan-2026-05-26.md",
            r"02-F3-Energy\projects\pure-shopify-store\ad-infrastructure-readiness-2026-05-25.md",
            r"02-F3-Energy\projects\pure-shopify-store\meta-capi-setup-runbook-2026-05-25.md",
            r"02-F3-Energy\projects\pure-shopify-store\photoroom-activation-runbook-2026-05-25.md",
            r"02-F3-Energy\projects\pure-shopify-store\photoroom-orchestrator-spec-2026-05-25.md",
            r"02-F3-Energy\projects\pure-shopify-store\HERO-PNG-DRAG-DROP-GUIDE.md",
            r"02-F3-Energy\_shared\impulse-correction-punch-list-2026-06-04.md",
            r"02-F3-Energy\_shared\impulse-correction-punch-list-CORRECTED-2026-06-04.md",
            r"02-F3-Energy\_shared\impulse-correction-punch-list-AMENDED-2026-06-04.md",
            r"02-F3-Energy\_shared\impulse-correction-punch-list-v3-2026-06-04.md",
            r"02-F3-Energy\_shared\chrome-agent-impulse-theme-audit-2026-06-04.md",
            r"02-F3-Energy\_shared\f3-website-brand-config-inventory-2026-05-27.md",
            r"02-F3-Energy\_shared\report-for-code-2026-06-04.md",
            r"02-F3-Energy\_shared\report-for-claude-ai-2026-06-04.md",
        ],
        "keep": [],
        "expected": "39",
        "purge": True,
    },
    {
        "id": "1g-tiktok",
        "section": "1g -- tiktok-shop SHIPPED-launch one-time prompts (chrome-agent via glob + named one-offs; non-named scratch KEPT)",
        "globs": [
            r"02-F3-Energy\projects\tiktok-shop\_notes\*chrome-agent*.md",
        ],
        "explicit": [
            r"02-F3-Energy\projects\tiktok-shop\_notes\2026-06-18_f3e_claude-ai-prompt-pure-copy-review.md",
            r"02-F3-Energy\projects\tiktok-shop\_notes\2026-06-25_f3e_RESUME-PROMPT-fbt-cowork-handoff.md",
            r"02-F3-Energy\projects\tiktok-shop\_notes\2026-07-01_f3e_RESUME-PROMPT-tiktok-cowork-handoff.md",
            r"02-F3-Energy\projects\tiktok-shop\_notes\2026-07-02_f3e_RESUME-PROMPT-tiktok-pure-launch.md",
            r"02-F3-Energy\projects\tiktok-shop\_notes\2026-07-08_f3e_RESUME-PROMPT-f3pure-tiktok-setup.md",
            r"02-F3-Energy\projects\tiktok-shop\_notes\2026-07-10_f3e_RESUME-PROMPT-tiktok-ecommerce-ban-evidence.md",
            r"02-F3-Energy\projects\tiktok-shop\_notes\2026-07-13_f3e_KICKOFF-PROMPT-tiktok-creator-invites-pure-only.md",
        ],
        "keep": [],
        "expected": "~29 chrome-agent + 7 named",
        "purge": True,
    },
    {
        "id": "1g-meta",
        "section": "1g -- meta-social-relaunch SHIPPED-launch one-time prompts (chrome-agent via glob + named one-offs; recent >=07-13 RESUMEs KEPT)",
        "globs": [
            r"02-F3-Energy\projects\meta-social-relaunch\_notes\*chrome-agent*.md",
            r"02-F3-Energy\_shared\*chrome-agent-prompt-meta*.md",
        ],
        "explicit": [
            r"02-F3-Energy\projects\meta-social-relaunch\_notes\2026-06-26_f3e_meta-social-relaunch-KICKOFF-PROMPT.md",
            r"02-F3-Energy\projects\meta-social-relaunch\_notes\2026-07-01_f3e_meta-social-cowork-bootstrap.md",
            r"02-F3-Energy\projects\meta-social-relaunch\_notes\2026-07-02_f3e_RESUME-PROMPT-pure-social.md",
            r"02-F3-Energy\projects\meta-social-relaunch\_notes\2026-07-09_f3e_RESUME-PROMPT-pure-social-content-upload.md",
            r"02-F3-Energy\projects\meta-social-relaunch\_notes\2026-07-10_f3e_RESUME-PROMPT-pure-launch-staging.md",
            r"02-F3-Energy\projects\meta-social-relaunch\_notes\2026-07-12_f3e_RESUME-PROMPT-store-connections-and-bio-links.md",
            r"02-F3-Energy\projects\meta-social-relaunch\_notes\2026-07-12_f3e_KICKOFF-PROMPT-pure-paid-ads-launch.md",
            r"02-F3-Energy\projects\meta-social-relaunch\_notes\2026-07-12_f3e_RESUME-PROMPT-pure-paid-golive.md",
            r"02-F3-Energy\projects\meta-social-relaunch\_notes\2026-07-13_f3e_KICKOFF-PROMPT-social-accounts-design-look-and-feel.md",
        ],
        "keep": [],
        "expected": "15 _notes + 5 _shared chrome-agent + 9 named",
        "purge": True,
    },
    {
        "id": "1g-amazon-etc",
        "section": "1g -- amazon/youtube/walmart/target/rangeme/events/ai-visibility/launch-readiness one-offs (HXP0KLQ kept per LEAK-1 fix)",
        "globs": [],
        "explicit": [
            r"02-F3-Energy\projects\amazon-store\2026-06-20_f3e_amazon-store-rebuild-kickoff-prompt.md",
            r"02-F3-Energy\projects\amazon-store\2026-06-26_f3e_amazon-RESUME-PROMPT.md",
            r"02-F3-Energy\projects\amazon-store\2026-06-27_f3e_pure-image-upload-runbook.md",
            r"02-F3-Energy\projects\amazon-store\2026-06-27_f3e_pure-nimbl-fba-inbound-brief.md",
            r"02-F3-Energy\projects\amazon-store\2026-07-03_f3e_amazon-pure-dims-support-case-draft.md",
            r"02-F3-Energy\projects\amazon-store\_notes\2026-07-03_f3e_RESUME-PROMPT-pure-fba-golive.md",
            r"02-F3-Energy\projects\amazon-store\2026-07-09_f3e_pure-nimbl-fba-inbound-brief-FBA19HLYWBBQ.md",
            r"02-F3-Energy\projects\amazon-store\2026-07-10_f3e_RESUME-PROMPT-amazon-post-ship.md",
            r"02-F3-Energy\projects\youtube-shopping\_notes\2026-07-01_f3e_youtube-shopping-cowork-bootstrap.md",
            r"02-F3-Energy\projects\youtube-shopping\_notes\2026-06-30_f3e_chrome-agent-prompts-youtube-shopping.md",
            r"02-F3-Energy\projects\youtube-shopping\_notes\2026-07-03_f3e_youtube-fresh-session-bootstrap.md",
            r"02-F3-Energy\projects\walmart-marketplace\_notes\2026-06-26_f3e_walmart-relaunch-KICKOFF-PROMPT.md",
            r"02-F3-Energy\projects\walmart-marketplace\_notes\2026-06-26_f3e_walmart-phase0-audit-chrome-prompt.md",
            r"02-F3-Energy\projects\walmart-marketplace\_notes\2026-07-01_f3e_walmart-RESUME-PROMPT.md",
            r"02-F3-Energy\projects\walmart-marketplace\_notes\2026-07-06_f3e_RESUME-PROMPT-walmart-wfs-golive.md",
            r"02-F3-Energy\projects\walmart-marketplace\_notes\2026-07-07_f3e_RESUME-PROMPT-walmart-wfs-launch.md",
            r"02-F3-Energy\projects\walmart-marketplace\_notes\2026-07-07_f3e_RESUME-PROMPT-walmart-wfs-inbound.md",
            r"02-F3-Energy\projects\target-plus\_notes\2026-06-26_f3e_target-plus-launch-KICKOFF-PROMPT.md",
            r"02-F3-Energy\projects\target-plus\_notes\2026-06-26_chrome-agent_target-plus-application-recon.md",
            r"02-F3-Energy\projects\rangeme\2026-06-27_rangeme-session-bootstrap.md",
            r"02-F3-Energy\projects\rangeme\2026-06-28_f3e_rangeme-phase2-progress-and-RESUME.md",
            r"02-F3-Energy\projects\rangeme\2026-07-01_f3e_rangeme-cowork-resume-prompt.md",
            r"02-F3-Energy\projects\rangeme\2026-07-02_f3e_RESUME-PROMPT-rangeme-finish-everything.md",
            r"02-F3-Energy\projects\events-sponsorship-pipeline\2026-06-27_f3e_kickoff-prompt-events-sponsorship-pipeline.md",
            r"02-F3-Energy\projects\ai-visibility\_notes\2026-07-02_f3e_RESUME-PROMPT-ai-visibility-execution.md",
            r"02-F3-Energy\projects\ai-visibility\_notes\2026-07-03_f3e_RESUME-PROMPT-ai-visibility-execution.md",
            r"02-F3-Energy\projects\ai-visibility\_notes\2026-07-03_f3e_cora-engine-CODE-SESSION-PROMPT.md",
            r"02-F3-Energy\projects\ai-visibility\_notes\2026-07-07_f3e_cora-code-prompt-otterly-citations-400-fix.md",
            r"02-F3-Energy\projects\ai-visibility\_notes\2026-07-07_f3e_RESUME-PROMPT-ai-visibility.md",
            r"02-F3-Energy\projects\launch-readiness\2026-06-30_f3e_chrome-agent-prompts-tracking-fixes.md",
            r"02-F3-Energy\projects\launch-readiness\2026-06-30_f3e_launch-readiness-audit.md",
            r"02-F3-Energy\projects\launch-readiness\2026-06-30_f3e_launch-readiness-RESUME.md",
            r"02-F3-Energy\projects\launch-readiness\2026-07-01_f3e_launch-verification-and-staged-actions.md",
            r"02-F3-Energy\projects\launch-readiness\_notes\2026-07-08_f3e_RESUME-PROMPT-shopify-storefront-build.md",
            r"02-F3-Energy\2026-06-11_f3e_go-live-tracking-audit.md",
            r"02-F3-Energy\2026-06-12_f3e_chrome-agent-prompts-tracking-golive.md",
            r"02-F3-Energy\2026-06-20_f3e_shopify-build-cowork-bootstrap.md",
        ],
        "keep": [],
        "expected": "37",
        "purge": True,
    },
    {
        "id": "1h-small-entity",
        "section": "1h -- small-entity one-offs (HJRG/OSN/HJRP rogers-ranch; memory stub kept per LEAK-2)",
        "globs": [],
        "explicit": [
            r"01-HJR-Global\_notes\2026-06-01-fireflies-finance-weekly.md",
            r"01-HJR-Global\_notes\2026-06-22-fireflies-finance-weekly.md",
            r"09-One-Stop-Nutrition\projects\staff-scheduling\OSN-STAFF-SCHEDULING_cowork-session-prompt.md",
            r"09-One-Stop-Nutrition\projects\staff-scheduling\code-session-handoff-2026-06-05.md",
            r"09-One-Stop-Nutrition\_notes\2026-05-19-fireflies-osn-weekly.md",
            r"06-HJR-Properties\rogers-ranch\_claude-ai-launch-strategy-orientation-prompt.md",
            r"06-HJR-Properties\rogers-ranch\website\ROGERS-RANCH-WEB_cowork-session-prompt.md",
            r"06-HJR-Properties\rogers-ranch\website\ROGERS-RANCH-WEB_code-session-prompt.md",
            r"06-HJR-Properties\rogers-ranch\website\ROGERS-RANCH-WEB_claude-ai-prompt.md",
            r"06-HJR-Properties\rogers-ranch\projects\launch\_notes\claude-ai-session-001-2026-05-22.md",
        ],
        "keep": [],
        "expected": "10",
        "purge": True,
    },
    {
        "id": "1i-shared-top",
        "section": "1i -- _shared top-level dated one-time docs + 2 non-evergreen playbooks",
        "globs": [],
        "explicit": [
            r"_shared\2026-06-06_fndr_hubspot-portal-fix-prompts.md",
            r"_shared\bootstrap-new-machine-2026-05-12.md",
            r"_shared\bootstrap-new-machine-2026-05-16.md",
            r"_shared\daily-sync-architecture-recommendations-2026-05-12.md",
            r"_shared\financial-sources-discovery-2026-05-12.md",
            r"_shared\qbo-inflow-coverage-map-2026-05-10.md",
            r"_shared\research-framework-gap-audit-2026-05-10.md",
            r"_shared\slack-phase2-kickoff-companion-2026-05-13.md",
            r"_shared\slack-phase2-team-activation-prep-2026-05-12.md",
            r"_shared\slack-phase2-team-activation-prep-2026-05-13-v2.md",
            r"_shared\soak-period-plan-2026-05-12.md",
            r"_shared\starter-prompts.md",
            r"_shared\tessa-transition-asana-audit-2026-05-14.md",
            r"_shared\tommy-f3e-sales-operating-playbook-2026-05-14.md",
            r"_shared\playbooks\harrison-out-of-office-DRAFT.md",
            r"_shared\playbooks\chat-consolidation-2026-05-14.md",
        ],
        "keep": [],
        "expected": "16",
        "purge": True,
    },
    {
        "id": "1j-founder-top",
        "section": "1j -- 00-Founder top-level one-offs + 3 Pass-1 banner'd files + press PBJ note",
        "globs": [],
        "explicit": [
            r"00-Founder\2026-06-05_fndr_social-ad-accounts-audit-cowork-prompt.md",
            r"00-Founder\2026-06-13_fndr_hygiene-notion-flagged-items-code-prompt.md",
            r"00-Founder\2026-06-18_fndr_content-production-pipeline-COWORK-PROMPT.md",
            r"00-Founder\2026-06-22_fndr_asana-account-audit-session-kickoff.md",
            r"00-Founder\2026-06-22_fndr_fireflies-account-audit-session-kickoff.md",
            r"00-Founder\2026-06-22_fndr_fireflies-billing-chrome-agent-prompt.md",
            r"00-Founder\2026-06-22_fndr_fireflies-downgrade-split-plan.md",
            r"00-Founder\2026-06-22_fndr_fireflies-execution-chrome-agent-prompt.md",
            r"00-Founder\2026-06-22_fndr_fireflies-invoice-cost-audit.md",
            r"00-Founder\2026-06-30_fndr_fireflies-account-optimization-audit.md",
            r"00-Founder\2026-06-30_fndr_fireflies-resume-execution-chrome-agent-prompt.md",
            r"00-Founder\2026-07-01_fndr_fireflies-channels-rules-chrome-agent-prompt.md",
            r"00-Founder\2026-07-01_fndr_fireflies-next-session-bootstrap.md",
            r"00-Founder\2026-07-08_f3e_east-valley-tribune-fact-sheet-maryniak.md",
            r"00-Founder\2026-07-08_fndr_christina-azbigmedia-interview-brief.md",
            r"00-Founder\_session-captures\2026-07\2026-07-08_fndr_press-pipeline-review-and-tracker-update.md",
            r"00-Founder\projects\press\_notes\2026-06-12-fireflies-pbj-made-in-arizona.md",
        ],
        "keep": [],
        "expected": "17",
        "purge": True,
    },
    {
        "id": "2b-amazon-fba",
        "section": "2b -- F3E amazon FBA cancelled-inbound brief + generic nimbl brief (dedup with 1g)",
        "globs": [],
        "explicit": [
            r"02-F3-Energy\projects\amazon-store\2026-07-09_f3e_pure-nimbl-fba-inbound-brief-FBA19HLYWBBQ.md",
            r"02-F3-Energy\projects\amazon-store\2026-06-27_f3e_pure-nimbl-fba-inbound-brief.md",
        ],
        "keep": [],
        "expected": "2",
        "purge": True,
    },
    {
        "id": "2c-copa-bhrf-loose-dup",
        "section": "2c -- copa-bhrf loose duplicate (dedup; canonical kept in place; whole-folder purge handled specially)",
        "globs": [],
        "explicit": [
            COPA_LOOSE_DUP,
        ],
        "keep": [],
        "expected": "1",
        "purge": True,
    },
]


def _cfg() -> ArchiveConfig:
    """Build the archive config from THIS module's constants. Read at CALL time so
    tests that monkeypatch FOUNDER_OS_ROOT / ARCHIVE_ROOT / KEEP_SUBSTRINGS / etc.
    are honored by every wrapper below."""
    return ArchiveConfig(
        founder_os_root=FOUNDER_OS_ROOT,
        archive_root=ARCHIVE_ROOT,
        hold_segments=HOLD_SEGMENTS,
        keep_class_basenames=KEEP_CLASS_BASENAMES,
        keep_class_segments=KEEP_CLASS_SEGMENTS,
        keep_class_basename_substr=KEEP_CLASS_BASENAME_SUBSTR,
        class_exceptions=CLASS_EXCEPTIONS,
        keep_substrings=KEEP_SUBSTRINGS,
        scaffold_basenames=_SCAFFOLD_BASENAMES,
        drive_title_max_fileids=_DRIVE_TITLE_MAX_FILEIDS,
        copa_purge_glob=COPA_PURGE_GLOB,
        copa_drive_titles=COPA_DRIVE_TITLES,
        copa_loose_dup=COPA_LOOSE_DUP,
        batch=_BATCH,
    )


def _rel(path: Path) -> str:
    return kb_archive.rel(path, _cfg())


def _segments_lower(relpath: str) -> list[str]:
    return kb_archive.segments_lower(relpath)


def _is_keep_as_class(relpath: str) -> str | None:
    return kb_archive.is_keep_as_class(relpath, _cfg())


def _hold_reason(relpath: str) -> str | None:
    return kb_archive.hold_reason(relpath, _cfg())


def _is_keep_substr(relpath: str) -> str | None:
    return kb_archive.is_keep_substr(relpath, _cfg())


def expand_archive_set(clusters: list[dict]) -> tuple[list[str], dict, list[tuple[str, str]], list[tuple[str, str]]]:
    """Delegate to the shared core; translate the typed HOLD abort back to the
    historical SystemExit(2) contract this tool + its tests rely on."""
    try:
        return kb_archive.build_move_manifest(clusters, _cfg())
    except HoldGuardTripped:
        raise SystemExit(2)


# ─────────────────────────────────────────────────────────────────────────────
# KB purge selection (read-only SELECTs; work on either a ro or rw connection).
# ─────────────────────────────────────────────────────────────────────────────
def _chunk_ids_for_static(conn, relpaths: list[str]) -> list[str]:
    return kb_archive.chunk_ids_for_static(conn, relpaths, _cfg())


def select_static_purge(conn, archive_relpaths: list[str]) -> tuple[list[str], int, int]:
    return kb_archive.select_static_purge(conn, archive_relpaths, _cfg())


def select_drive_purge(conn, archive_relpaths: list[str]) -> tuple[list[str], list[dict], list[dict]]:
    return kb_archive.select_drive_purge(conn, archive_relpaths, _cfg())


def delete_chunks(conn, chunk_ids: list[str]) -> dict:
    return kb_archive.delete_chunks(conn, chunk_ids, _cfg())


# ─────────────────────────────────────────────────────────────────────────────
# Move phase.
# ─────────────────────────────────────────────────────────────────────────────
def plan_moves(archive_relpaths: list[str]) -> list[dict]:
    return kb_archive.plan_moves(archive_relpaths, _cfg())


def execute_moves(moves: list[dict]) -> None:
    kb_archive.execute_moves(moves, _cfg())


def revert(manifest_path: Path) -> int:
    return kb_archive.revert(manifest_path, _cfg())


def write_manifest(path: Path, *, mode: str, report: dict, moves: list[dict],
                   class_filtered, substr_filtered, static_ids, drive_ids,
                   moved_static, copa_static, drive_included, drive_skipped,
                   purge_enabled) -> None:
    kb_archive.write_manifest(
        path, _cfg(), mode=mode, report=report, moves=moves,
        class_filtered=class_filtered, substr_filtered=substr_filtered,
        static_ids=static_ids, drive_ids=drive_ids, moved_static=moved_static,
        copa_static=copa_static, drive_included=drive_included,
        drive_skipped=drive_skipped, purge_enabled=purge_enabled)


def _write(manifest, *, mode, report, moves, class_filtered, substr_filtered,
           static_ids, drive_ids, moved_static, copa_static, drive_included,
           drive_skipped, purge_enabled):
    write_manifest(manifest, mode=mode, report=report, moves=moves,
                   class_filtered=class_filtered, substr_filtered=substr_filtered,
                   static_ids=static_ids, drive_ids=drive_ids, moved_static=moved_static,
                   copa_static=copa_static, drive_included=drive_included,
                   drive_skipped=drive_skipped, purge_enabled=purge_enabled)


def main() -> int:
    ap = argparse.ArgumentParser(description="Founder-OS KB archive-move + purge (dry-run default).")
    ap.add_argument("--apply", action="store_true", help="Execute (default is a read-only dry-run).")
    ap.add_argument("--dry-run", action="store_true", help="Report only (this is the default).")
    ap.add_argument("--db", default=str(KB_DB_PATH), help="Path to the KB sqlite DB.")
    ap.add_argument("--skip-move", action="store_true", help="Skip the file move (purge only).")
    ap.add_argument("--skip-purge", action="store_true", help="Skip the KB purge (move only).")
    ap.add_argument("--from-manifest", metavar="MANIFEST",
                    help="Load the archive set + persisted purge chunk_ids from a prior manifest "
                         "instead of re-globbing the live tree. USE THIS to resume/finish a purge "
                         "after the files were already moved (the re-glob would miss them).")
    ap.add_argument("--revert", metavar="MANIFEST", help="Reverse the moves recorded in a manifest JSON.")
    args = ap.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    if args.revert:
        return revert(Path(args.revert))

    apply_changes = args.apply and not args.dry_run
    mode = "APPLY" if apply_changes else "DRY-RUN"

    if not FOUNDER_OS_ROOT.exists():
        log.error("Founder OS root not found: %s", FOUNDER_OS_ROOT)
        return 1
    db_path = Path(args.db)
    if not db_path.exists():
        log.error("KB database not found: %s", db_path)
        return 1

    log.info("=" * 72)
    log.info("Founder-OS KB archive+purge  mode=%s  db=%s", mode, db_path)
    if apply_changes:
        log.warning("APPLY MODE: ensure Cora is STOPPED and cora_kb.db is BACKED UP.")

    class_filtered: list = []
    substr_filtered: list = []

    if args.from_manifest:
        # RESUME / two-phase: read the persisted plan + purge chunk_ids from a prior
        # manifest. Does NOT re-glob (the tree may already be moved). Fixes the
        # under-purge on a purge-only second phase (D-051 CONFIRMED #1).
        loaded = json.loads(Path(args.from_manifest).read_text(encoding="utf-8"))
        moves = loaded["moves"]
        archive_relpaths = [m["src_rel"] for m in moves]
        report = loaded.get("clusters", {})
        pg = loaded.get("purge", {})
        static_ids = list(pg.get("static_chunk_ids", []))
        drive_ids = list(pg.get("drive_chunk_ids", []))
        moved_static = pg.get("static_md_from_moved_files", 0)
        copa_static = pg.get("copa_bhrf_static_chunks", 0)
        drive_included = pg.get("drive_copy_included", [])
        drive_skipped = pg.get("drive_copy_skipped_ambiguous", [])
        log.info("Loaded plan from manifest %s: %d files, %d static + %d drive chunks (NO re-glob).",
                 args.from_manifest, len(archive_relpaths), len(static_ids), len(drive_ids))
    else:
        # 1) Build the archive set from the LIVE tree (aborts on any HOLD leak).
        archive_relpaths, report, class_filtered, substr_filtered = expand_archive_set(ARCHIVE_CLUSTERS)
        log.info("Archive set: %d files across %d clusters "
                 "(KEEP-as-class filtered %d, KEEP-substring filtered %d).",
                 len(archive_relpaths), len(report), len(class_filtered), len(substr_filtered))
        for cid, r in report.items():
            log.info("   [%s] %d files (expected %s)", cid, r["count"], r["expected"])

        # 2) Purge selection (read-only SELECTs; safe while Cora is live). Must run
        #    against the PRE-MOVE tree -- the resolved chunk_ids are then persisted
        #    in the manifest so a later purge-only run reads them (not a re-glob).
        static_ids, drive_ids = [], []
        moved_static = copa_static = 0
        drive_included, drive_skipped = [], []
        if not args.skip_purge:
            ro = kb_archive.connect_ro(db_path)
            try:
                static_ids, moved_static, copa_static = select_static_purge(ro, archive_relpaths)
                drive_ids, drive_included, drive_skipped = select_drive_purge(ro, archive_relpaths)
            finally:
                ro.close()
            log.info("KB purge: static_md %d (moved %d + copa-bhrf %d), drive-copy %d "
                     "(included %d titles, skipped-ambiguous %d titles).",
                     len(static_ids), moved_static, copa_static, len(drive_ids),
                     len(drive_included), len(drive_skipped))
            for d in drive_skipped:
                log.info("   drive-copy SKIPPED: %s [%s]", d["title"], d["reason"])
        moves = plan_moves(archive_relpaths)

        # Guard: if the purge selection is being computed but the source files are
        # already gone (an accidental purge-only second phase WITHOUT --from-manifest),
        # the selection is INCOMPLETE -- warn loudly (the fix is --from-manifest).
        if apply_changes and not args.skip_purge:
            absent = sum(1 for m in moves if not (FOUNDER_OS_ROOT / m["src_rel"]).exists())
            if absent:
                log.warning("%d/%d source files are already ABSENT -- the purge selection is "
                            "likely INCOMPLETE. For a resumed/second-phase purge, re-run with "
                            "--from-manifest <prior-manifest.json>.", absent, len(moves))

    all_purge = sorted(set(static_ids) | set(drive_ids))

    # 3) Write the manifest EARLY (the plan + persisted purge chunk_ids) BEFORE any
    #    mutation, so a partial-move crash is always recoverable (D-051 CONFIRMED #2).
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    manifest = LOG_DIR / f"archive-founder-os-manifest-{ts}.json"
    _write(manifest, mode=mode, report=report, moves=moves,
           class_filtered=class_filtered, substr_filtered=substr_filtered,
           static_ids=static_ids, drive_ids=drive_ids, moved_static=moved_static,
           copa_static=copa_static, drive_included=drive_included,
           drive_skipped=drive_skipped, purge_enabled=not args.skip_purge)

    # 4) Execute the move (try/finally re-writes the manifest with per-move results,
    #    even if the move phase raises).
    if apply_changes and not args.skip_move:
        log.info("Moving %d files into _archive ...", len(moves))
        try:
            execute_moves(moves)
        finally:
            _write(manifest, mode=mode, report=report, moves=moves,
                   class_filtered=class_filtered, substr_filtered=substr_filtered,
                   static_ids=static_ids, drive_ids=drive_ids, moved_static=moved_static,
                   copa_static=copa_static, drive_included=drive_included,
                   drive_skipped=drive_skipped, purge_enabled=not args.skip_purge)
        moved_ok = sum(1 for m in moves if m.get("moved"))
        failed = [m["src_rel"] for m in moves if m.get("result", "").startswith("error")]
        log.info("Moved %d/%d files.", moved_ok, len(moves))
        if failed:
            log.error("%d file(s) FAILED to move (retryable -- re-run --apply): %s",
                      len(failed), failed[:10])

    # 5) Execute the purge (from the resolved/persisted chunk_ids).
    if apply_changes and not args.skip_purge:
        if all_purge:
            log.info("Purging %d chunks from knowledge_chunks + both vec tables ...", len(all_purge))
            rw = kb_archive.connect_rw(db_path)
            try:
                totals = delete_chunks(rw, all_purge)
                log.info("Deleted: %s", totals)
            finally:
                rw.close()
            log.info("Reclaim disk: .venv\\Scripts\\python.exe scripts\\reclaim_kb_space.py")
        else:
            log.info("No chunks to purge.")

    if not apply_changes:
        log.info("DRY-RUN complete -- nothing moved, nothing deleted. "
                 "Review the manifest, then re-run with --apply (Cora STOPPED + db backed up).")
    log.info("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
