"""Code #15 RIDER B (cq-59c5048d0891) + consolidation-rider C13-14, 2026-09-24 --
the INGEST side, on the flat per-user sweep's ancestry walk.

  * item 1: a NON-.md file anywhere under HJR-Founder-OS/_archive (incl. the
    dedup-2026-09 child, which is deliberately NOT pinned separately) is
    ``excluded`` -- even in allowlist mode, whose allowlist IS the Founder-OS root;
  * item 2: the same for a personal-finances descendant; a sibling 00-Founder file
    still ingests;
  * C13-14: a file whose ancestor folder is NAMED ``_runs`` (exact, case-insensitive)
    is ``excluded``; ``test_runs`` / ``_runs-old`` are ordinary folders;
  * sweep_user never upserts an _archive pdf (the end-to-end door).

In-memory Drive (the tests/test_computers_roots_exclusion.py _Svc pattern); no network.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import kb_exclusions  # noqa: E402
from cora.connectors import drive_sweep  # noqa: E402

FOS = drive_sweep.FOUNDERS_OS_ROOT_ID
ARCHIVE = "16q7RfzibKms2rLvBKGIfaTSPBPUGYPaP"
DEDUP = "1TSUGC4hAHjgbHuExFqf_4-lXyXm7opq5"
PF = "1l7Hms6KwISUelnB-ItLAF9vms6K_Wd9s"
FNDR = "1P6ArPWh97bojlU5NtUnuRPRKLbXrhw6a"
ALLOW = frozenset({FOS})


class _Svc:
    """files().get by id over {id: (parent, name)}; ids in ``fail`` raise."""

    def __init__(self, parents, fail=frozenset()):
        self.parents_of, self.fail = parents, set(fail)
        self.got: list[str] = []

    def files(self):  # noqa: A003
        return self

    def get(self, fileId, fields=None):  # noqa: N803
        self.got.append(fileId)
        outer = self

        class _R:
            def execute(_s):
                if fileId in outer.fail:
                    raise RuntimeError("500")
                parent, name = outer.parents_of[fileId]
                d = {"id": fileId, "name": name}
                if parent:
                    d["parents"] = [parent]
                return d
        return _R()


def _tree():
    return {
        "MYDRIVE": (None, "My Drive"),
        FOS: ("MYDRIVE", "HJR-Founder-OS"),
        ARCHIVE: (FOS, "_archive"),
        DEDUP: (ARCHIVE, "dedup-2026-09"),
        "DEDUP_SUB": (DEDUP, "02-F3-Energy"),
        "ARCH_SHARED": (ARCHIVE, "_shared"),
        FNDR: (FOS, "00-Founder"),
        PF: (FNDR, "personal-finances"),
        "PF_2025": (PF, "2025"),
        "FNDR_PROJ": (FNDR, "projects"),
        "SHARED": (FOS, "_shared"),
        "MIRROR": ("SHARED", "claude-workspace-mirror"),
        "RUNS": ("MIRROR", "_runs"),
        "RUNS_TASK": ("RUNS", "fndr-weekly-review"),
        "RUNS_UPPER": ("MIRROR", "_Runs"),
        "TEST_RUNS": ("MIRROR", "test_runs"),
        "RUNS_OLD": ("MIRROR", "_runs-old"),
    }


def _disp(parent, *, allowlist=ALLOW, mime="application/pdf", name="x.pdf", svc=None):
    svc = svc or _Svc(_tree())
    # expanded=frozenset(): prove the WALK closes it (the expansion is only the fast path)
    return drive_sweep._file_disposition(svc, [parent], frozenset(), True, {},
                                         mime_type=mime, filename=name, allowlist=allowlist)


class TestArchivePin:
    def test_a_deep_dedup_file_is_excluded_by_the_parent_pin(self):
        assert DEDUP not in kb_exclusions.KB_EXCLUDED_FOLDER_IDS
        assert _disp("DEDUP_SUB") == "excluded"
        assert _disp(DEDUP) == "excluded"
        assert _disp("ARCH_SHARED") == "excluded"
        assert _disp(ARCHIVE) == "excluded"

    def test_excluded_in_denylist_mode_too(self):
        assert _disp("DEDUP_SUB", allowlist=None) == "excluded"

    def test_the_pin_wins_over_the_markdown_rule(self):
        # an archived .md would otherwise read static_md_owned; the pin is checked first
        assert _disp("DEDUP_SUB", mime="text/markdown", name="old.md") == "excluded"

    def test_an_unresolvable_archive_ancestry_still_fails_closed(self):
        assert _disp("DEDUP_SUB", svc=_Svc(_tree(), fail={DEDUP})) == "unresolved"


class TestPersonalFinancesPin:
    def test_a_descendant_is_excluded(self):
        assert _disp("PF_2025") == "excluded"
        assert _disp(PF) == "excluded"

    def test_a_sibling_00_founder_file_still_ingests(self):
        assert _disp("FNDR_PROJ") is None
        assert _disp(FNDR) is None


class TestRunsBelt:
    def test_a_file_under_runs_is_excluded(self):
        assert _disp("RUNS_TASK") == "excluded"
        assert _disp("RUNS") == "excluded"
        assert _disp("RUNS_UPPER") == "excluded"          # case-insensitive, like the static sibling
        assert _disp("RUNS_TASK", allowlist=None) == "excluded"

    def test_lookalikes_are_not_excluded(self):
        assert _disp("TEST_RUNS") is None
        assert _disp("RUNS_OLD") is None
        assert drive_sweep._is_runs_segment("_runs") and drive_sweep._is_runs_segment("_RUNS")
        for name in ("test_runs", "_runs-old", "runs", "", None):
            assert not drive_sweep._is_runs_segment(name), name

    def test_a_runs_file_counts_as_excluded_for_the_legacy_wrapper(self):
        svc = _Svc(_tree())
        assert drive_sweep._file_under_excluded_folder(svc, ["RUNS_TASK"], frozenset(), True, {}) is True


def test_sweep_user_never_upserts_an_archive_pdf():
    svc = _Svc(_tree())
    listed = [
        {"id": "arch1", "name": "old-deck.pdf", "mimeType": "application/pdf",
         "modifiedTime": "2026-09-20T00:00:00Z", "size": "5000", "parents": ["DEDUP_SUB"]},
        {"id": "pf1", "name": "scan.pdf", "mimeType": "application/pdf",
         "modifiedTime": "2026-09-20T00:00:00Z", "size": "5000", "parents": ["PF_2025"]},
        {"id": "live1", "name": "brief.pdf", "mimeType": "application/pdf",
         "modifiedTime": "2026-09-20T00:00:00Z", "size": "5000", "parents": ["FNDR_PROJ"]},
    ]
    flat = MagicMock()
    flat.files.return_value.list.return_value.execute.return_value = {"files": listed, "nextPageToken": None}
    flat.files.return_value.get = svc.get
    kb = MagicMock()
    kb.get_sync_state.return_value = None
    kb.get_checkpoint.return_value = None
    anth = MagicMock()
    anth.messages.create.return_value.content = [
        MagicMock(text='{"score": 9, "entity": "FNDR", "summary": "x", "discard_reason": ""}')]
    user = {"email": "harrison@hjrglobal.com", "name": "Harrison", "entity_default": "FNDR",
            "drive_sweep_mode": "allowlist", "drive_sweep_allowlist": [FOS]}
    with patch("cora.connectors.drive_sweep._build_drive_service", return_value=flat), \
         patch("cora.connectors.drive_sweep._build_sheets_service", return_value=None), \
         patch("cora.connectors.drive_sweep._expanded_excluded_folder_ids",
               return_value=(frozenset(kb_exclusions.KB_EXCLUDED_FOLDER_IDS), True)), \
         patch("cora.connectors.drive_sweep._extract_content", return_value="Meaningful business content " * 20), \
         patch("cora.connectors.drive_sweep._ingest_file", return_value=1) as ingest:
        stats = drive_sweep.sweep_user(user, "/fake/sa.json", kb, anth, freshness_days=30, dry_run=False)
    assert stats["dashboard_excluded_skipped"] == 2
    assert ingest.call_count == 1 and ingest.call_args[0][1]["id"] == "live1"
