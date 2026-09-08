"""Ingest-integrity bundle I5 (cq-12fd5d76fd04), 2026-09-08 -- two doors, one tag.

The harvester writes ``<entity>/_session-captures/...``; the static_md door keys
the entity on the FOLDER. The Drive door had TWO writers for the same file id:
sweep_founders_os (folder-keyed) and the flat per-user sweep of Harrison's
Drive (Haiku-keyed), both writing source="drive_sweep" -- last writer wins, so
64 of 411 capture files disagreed on 2026-09-08 (30 LEX-vs-FNDR, 20 LEX-vs-F3E).
Fix: the flat sweep SKIPS any file whose ancestry runs through an entity-mapped
top folder of the HJR-Founder-OS tree -- founders_os owns those.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import kb_exclusions  # noqa: E402
from cora.connectors import drive_sweep  # noqa: E402

FOS = drive_sweep.FOUNDERS_OS_ROOT_ID


def _chain(*pairs):
    return list(pairs)


class TestOwnerEntity:
    def test_entity_mapped_top_folder_is_owned(self):
        assert drive_sweep._founders_os_owner_entity(_chain(("CAP", "2026-09"), ("SC", "_session-captures"),
                                                            ("LEX", "08-Lexington-Services"), (FOS, "HJR-Founder-OS"),
                                                            ("MY", "My Drive"))) == "LEX"
        assert drive_sweep._founders_os_owner_entity(_chain(("EA", "Email Attachments - Last 24 Months"),
                                                            ("EAT", "email-attachments"), ("SH", "_shared"),
                                                            (FOS, "HJR-Founder-OS"))) == "FNDR"
        assert drive_sweep._founders_os_owner_entity(_chain(("X", "x"), ("F3E", "02-F3-Energy"), (FOS, "HJR-Founder-OS"))) == "F3E"

    def test_unmapped_top_folder_and_root_level_are_not_owned(self):
        # memory/ and _brain/ are unmapped -> founders_os skips them -> the flat sweep keeps covering them
        assert drive_sweep._founders_os_owner_entity(_chain(("MEM", "memory"), (FOS, "HJR-Founder-OS"))) is None
        assert drive_sweep._founders_os_owner_entity(_chain(("BR", "_brain"), (FOS, "HJR-Founder-OS"))) is None
        # a file whose direct parent IS the root
        assert drive_sweep._founders_os_owner_entity(_chain((FOS, "HJR-Founder-OS"), ("MY", "My Drive"))) is None
        # not in the tree at all
        assert drive_sweep._founders_os_owner_entity(_chain(("L", "loose"), ("MY", "My Drive"))) is None
        assert drive_sweep._founders_os_owner_entity([]) is None


class _Tree:
    def __init__(self, parents):
        self.parents_of = parents
        self.got = []

    def files(self):  # noqa: A003
        return self

    def get(self, fileId, fields=None):  # noqa: N803
        self.got.append(fileId)
        outer = self

        class _R:
            def execute(_s):
                parent, name = outer.parents_of[fileId]
                d = {"id": fileId, "name": name}
                if parent:
                    d["parents"] = [parent]
                return d
        return _R()


PARENTS = {
    "CAP": ("SC", "2026-09"), "SC": ("LEX", "_session-captures"), "LEX": (FOS, "08-Lexington-Services"),
    FOS: ("MY", "HJR-Founder-OS"), "MY": (None, "My Drive"),
    "MEM": (FOS, "memory"), "LOOSE": ("MY", "loose"),
    "SHAREDEA": ("SH", "Email Attachments - Last 24 Months"), "SH": ("SHARED", "email-attachments"), "SHARED": (FOS, "_shared"),
}


class TestDisposition:
    def test_founders_os_owned_vs_kept(self):
        svc = _Tree(PARENTS)
        expanded = frozenset(kb_exclusions.KB_EXCLUDED_FOLDER_IDS)
        cache: dict = {}
        assert drive_sweep._file_disposition(svc, ["CAP"], expanded, True, cache) == "founders_os_owned"
        assert drive_sweep._file_disposition(svc, ["SHAREDEA"], expanded, True, cache) == "founders_os_owned"
        assert drive_sweep._file_disposition(svc, ["MEM"], expanded, True, cache) is None      # unmapped: flat sweep keeps it
        assert drive_sweep._file_disposition(svc, ["LOOSE"], expanded, True, cache) is None

    def test_sweep_user_skips_owned_files_and_counts(self):
        svc = _Tree(PARENTS)
        listed = [
            {"id": "cap1", "name": "2026-09-04_cowork-session_cb995639.md", "mimeType": "text/markdown",
             "modifiedTime": "2026-09-04T12:00:00Z", "size": "5000", "parents": ["CAP"]},
            {"id": "mem1", "name": "decisions.md", "mimeType": "text/markdown",
             "modifiedTime": "2026-09-04T12:00:00Z", "size": "5000", "parents": ["MEM"]},
            {"id": "loose1", "name": "vendor.txt", "mimeType": "text/plain",
             "modifiedTime": "2026-09-04T12:00:00Z", "size": "5000", "parents": ["LOOSE"]},
        ]
        flat = MagicMock()
        flat.files.return_value.list.return_value.execute.return_value = {"files": listed, "nextPageToken": None}
        flat.files.return_value.get = svc.get
        kb = MagicMock(); kb.get_sync_state.return_value = None; kb.get_checkpoint.return_value = None
        anth = MagicMock()
        anth.messages.create.return_value.content = [MagicMock(text='{"score": 9, "entity": "FNDR", "summary": "x", "discard_reason": ""}')]
        user = {"email": "harrison@hjrglobal.com", "name": "Harrison", "entity_default": "FNDR"}
        with patch("cora.connectors.drive_sweep._build_drive_service", return_value=flat), \
             patch("cora.connectors.drive_sweep._build_sheets_service", return_value=None), \
             patch("cora.connectors.drive_sweep._expanded_excluded_folder_ids",
                   return_value=(frozenset(kb_exclusions.KB_EXCLUDED_FOLDER_IDS), True)), \
             patch("cora.connectors.drive_sweep._extract_content", return_value="Meaningful business content " * 20), \
             patch("cora.connectors.drive_sweep._ingest_file", return_value=1) as ingest:
            stats = drive_sweep.sweep_user(user, "/fake/sa.json", kb, anth, freshness_days=30, dry_run=False)
        assert stats["founders_os_owned_skipped"] == 1
        ingested = {c[0][1]["id"] for c in ingest.call_args_list}
        assert ingested == {"mem1", "loose1"}          # the capture file is founders_os's; memory/ + loose stay


# ── the parity probe (read-only acceptance check) ────────────────────────────
def _load_probe():
    path = _REPO_ROOT / "scripts" / "probe_two_door_entity_parity.py"
    spec = importlib.util.spec_from_file_location("probe_two_door_entity_parity", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_probe_measures_disagreements_only_for_harvester_files():
    probe = _load_probe()
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE knowledge_chunks (chunk_id TEXT, source TEXT, source_id TEXT, entity TEXT, title TEXT)")
    c.executemany("INSERT INTO knowledge_chunks VALUES (?,?,?,?,?)", [
        ("s1", "static_md", r"08-Lexington-Services\_session-captures\2026-09\2026-09-04_cowork-session_cb995639.md", "LEX", "t"),
        ("d1", "drive_sweep", "fileA", "FNDR", "2026-09-04_cowork-session_cb995639.md"),
        ("s2", "static_md", "02-F3-Energy/_session-captures/2026-08/2026-08-29_cowork-session_64e31cab.md", "F3E", "t"),
        ("d2", "drive_sweep", "fileB", "F3E", "2026-08-29_cowork-session_64e31cab.md"),
        ("s3", "static_md", "08-Lexington-Services/_session-captures/2026-09/2026-09-03_lex_recap.md", "LEX", "t"),  # hand-written: ignored
        ("d3", "drive_sweep", "fileC", "FNDR", "2026-09-03_lex_recap.md"),
    ])
    m = probe.measure(c)
    assert m["static_files"] == 2 and m["drive_files"] == 2 and m["both"] == 2
    assert len(m["disagreements"]) == 1 and m["disagreements"][0][0] == "2026-09-04_cowork-session_cb995639.md"
    assert m["pairs"][(("LEX",), ("FNDR",))] == 1
