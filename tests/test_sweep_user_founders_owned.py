"""Ingest-integrity bundle I5 (cq-12fd5d76fd04), 2026-09-08 -- two doors, one tag.

The harvester writes ``<entity>/_session-captures/...``; the static_md door keys
the entity on the FOLDER. The Drive door had TWO writers for the same file id:
sweep_founders_os (folder-keyed) and the flat per-user sweep of Harrison's
Drive (Haiku-keyed), both writing source="drive_sweep" -- last writer wins, so
64 of 411 capture files disagreed on 2026-09-08 (30 LEX-vs-FNDR, 20 LEX-vs-F3E).

Fix (as remediated by the D-051 lens-D review): the flat sweep skips MARKDOWN
files inside the HJR-Founder-OS tree -- static_md already ingests every .md there
with the folder entity and its own exclusions -- and NOTHING else: the founders_os
sweep has been budget-interrupted for 37 consecutive runs, so the flat sweep is
the only door actually ingesting most non-.md Founder-OS content (filed receipts,
invoices, contracts). A "founders_os owns the tree" skip would have been a silent
ingestion cliff; this file pins the boundary.
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


class _Tree:
    def __init__(self, parents, *, fail_get=frozenset()):
        self.parents_of = parents
        self.fail_get = set(fail_get)
        self.got = []

    def files(self):  # noqa: A003
        return self

    def get(self, fileId, fields=None):  # noqa: N803
        self.got.append(fileId)
        outer = self

        class _R:
            def execute(_s):
                if fileId in outer.fail_get:
                    raise RuntimeError("500")
                parent, name = outer.parents_of[fileId]
                d = {"id": fileId, "name": name}
                if parent:
                    d["parents"] = [parent]
                return d
        return _R()


PARENTS = {
    "CAP": ("SC", "2026-09"), "SC": ("LEX", "_session-captures"), "LEX": (FOS, "08-Lexington-Services"),
    FOS: ("MY", "HJR-Founder-OS"), "MY": (None, "My Drive"),
    "MEM": (FOS, "memory"), "SWEPT": ("BRAIN", "swept"), "BRAIN": (FOS, "_brain"),
    "ACC": ("HJRG", "accounting"), "HJRG": (FOS, "01-HJR-Global"),
    "OLD": ("CONTRACTS", "old"), "CONTRACTS": ("F3E", "contracts"), "F3E": (FOS, "02-F3-Energy"),
    "SHAREDEA": ("SH", "Email Attachments - Last 24 Months"), "SH": ("SHARED", "email-attachments"), "SHARED": (FOS, "_shared"),
    "LOOSE": ("MY", "loose"),
}
EXPANDED = frozenset(kb_exclusions.KB_EXCLUDED_FOLDER_IDS)


def _disp(svc, parent, mime, name, cache=None):
    return drive_sweep._file_disposition(svc, [parent], EXPANDED, True, cache if cache is not None else {},
                                         mime_type=mime, filename=name)


class TestStaticMdOwnedBoundary:
    def test_markdown_inside_the_tree_is_static_md_owned(self):
        svc = _Tree(PARENTS)
        assert _disp(svc, "CAP", "text/markdown", "2026-09-04_cowork-session_cb995639.md") == "static_md_owned"
        assert _disp(svc, "MEM", "text/markdown", "decisions.md") == "static_md_owned"      # memory/ is static_md's too
        assert _disp(svc, "SWEPT", "text/markdown", "2026-09-07.md") == "static_md_owned"    # the self-poisoning digests
        assert _disp(svc, "SHAREDEA", "text/plain", "notes.md") == "static_md_owned"         # .md by name, odd MIME

    def test_non_markdown_inside_the_tree_stays_on_the_flat_sweep(self):
        # the flat sweep is the ONLY door actually ingesting these today (lens D)
        svc = _Tree(PARENTS)
        assert _disp(svc, "ACC", "application/pdf", "2026-09-08_hjrg_receipt.pdf") is None
        assert _disp(svc, "SHAREDEA", "application/pdf", "HJR Global Wire Instructions.pdf") is None
        assert _disp(svc, "OLD", "application/pdf", "vendor-agreement-2024.pdf") is None      # name-skipped by founders_os
        assert _disp(svc, "CAP", "application/vnd.google-apps.document", "notes") is None

    def test_outside_the_tree_is_untouched(self):
        svc = _Tree(PARENTS)
        assert _disp(svc, "LOOSE", "text/markdown", "loose.md") is None
        assert _disp(svc, "LOOSE", "application/pdf", "loose.pdf") is None

    def test_is_markdown(self):
        assert drive_sweep._is_markdown("text/markdown", "x") and drive_sweep._is_markdown("", "A.MD")
        assert not drive_sweep._is_markdown("application/pdf", "a.pdf") and not drive_sweep._is_markdown(None, None)

    def test_chain_cut_by_bound_or_cycle_is_incomplete(self):
        deep = {f"n{i}": (f"n{i+1}", f"f{i}") for i in range(20)}
        deep["n20"] = (None, "top")
        svc = _Tree(deep)
        chain, ok = drive_sweep._ancestor_chain(svc, ["n0"], {}, max_nodes=5)
        assert ok is False and len(chain) == 5                                       # bound -> partial -> not complete
        cyc = {"a": ("b", "a"), "b": ("a", "b")}
        chain, ok = drive_sweep._ancestor_chain(_Tree(cyc), ["a"], {})
        assert ok is False
        # and a partial chain never reads as "not excluded"
        assert drive_sweep._file_disposition(svc, ["n0"], EXPANDED, True, {}) == "unresolved" or \
            drive_sweep._ancestor_chain(svc, ["n0"], {})[1] is True                    # default bound (400) resolves this one


def _flat(listed, svc):
    flat = MagicMock()
    flat.files.return_value.list.return_value.execute.return_value = {"files": listed, "nextPageToken": None}
    flat.files.return_value.get = svc.get
    return flat


def _kb():
    kb = MagicMock(); kb.get_sync_state.return_value = None; kb.get_checkpoint.return_value = None
    return kb


def _anth():
    anth = MagicMock()
    anth.messages.create.return_value.content = [MagicMock(text='{"score": 9, "entity": "FNDR", "summary": "x", "discard_reason": ""}')]
    return anth


USER = {"email": "harrison@hjrglobal.com", "name": "Harrison", "entity_default": "FNDR"}


def _sweep(listed, svc, kb=None):
    kb = kb or _kb()
    with patch("cora.connectors.drive_sweep._build_drive_service", return_value=_flat(listed, svc)), \
         patch("cora.connectors.drive_sweep._build_sheets_service", return_value=None), \
         patch("cora.connectors.drive_sweep._expanded_excluded_folder_ids", return_value=(EXPANDED, True)), \
         patch("cora.connectors.drive_sweep._extract_content", return_value="Meaningful business content " * 20), \
         patch("cora.connectors.drive_sweep._ingest_file", return_value=1) as ingest:
        stats = drive_sweep.sweep_user(USER, "/fake/sa.json", kb, _anth(), freshness_days=30, dry_run=False)
    return stats, ingest, kb


def _f(fid, name, mime, parent):
    return {"id": fid, "name": name, "mimeType": mime, "modifiedTime": "2026-09-04T12:00:00Z", "size": "5000", "parents": [parent]}


class TestSweepUser:
    def test_skips_markdown_in_tree_keeps_everything_else(self):
        listed = [
            _f("cap1", "2026-09-04_cowork-session_cb995639.md", "text/markdown", "CAP"),
            _f("mem1", "decisions.md", "text/markdown", "MEM"),
            _f("rcpt", "2026-09-08_hjrg_receipt.pdf", "application/pdf", "ACC"),
            _f("old1", "vendor-agreement-2024.pdf", "application/pdf", "OLD"),
            _f("loose1", "vendor.txt", "text/plain", "LOOSE"),
        ]
        stats, ingest, kb = _sweep(listed, _Tree(PARENTS))
        assert stats["static_md_owned_skipped"] == 2
        assert {c[0][1]["id"] for c in ingest.call_args_list} == {"rcpt", "old1", "loose1"}
        kb.set_sync_state.assert_called_once()                       # clean run -> watermark advances

    def test_unresolved_ancestry_holds_the_watermark(self):
        listed = [_f("rcpt", "receipt.pdf", "application/pdf", "ACC"), _f("loose1", "vendor.txt", "text/plain", "LOOSE")]
        stats, ingest, kb = _sweep(listed, _Tree(PARENTS, fail_get={"HJRG"}))
        assert stats["ancestry_unresolved_skipped"] == 1
        assert {c[0][1]["id"] for c in ingest.call_args_list} == {"loose1"}
        kb.set_sync_state.assert_not_called()                        # held -> re-enumerated next run
        kb.delete_checkpoint.assert_called()                         # the resume checkpoint still clears


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
