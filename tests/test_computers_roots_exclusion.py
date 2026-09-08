"""Ingest-integrity bundle I3 (cq-a0da505f8e5f; cq-bd6eab1fcb44 folded), 2026-09-08.

Google Drive for Desktop backs up BOTH PCs (Desktop / Documents / Downloads) into
Drive "Computers", and the flat per-user sweep of Harrison's Drive enumerated
them -- the located door of cq-b80c5bc5be7a (Cowork task SKILL.md bodies in the
KB). The roots are PARENTLESS, so the subfolder EXPANSION cannot see them; they
are pinned WALK-ONLY and closed by an ancestry walk that fails closed.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import kb_exclusions  # noqa: E402
from cora.connectors import drive_sweep  # noqa: E402

ROOT_A = "1cdDb9jvDhoOz1vliE-tmVaks01ey1AJj"   # HJR Always-On Desktop
ROOT_B = "1xmXreU4eKvcpAsj7Ic3fiwJO_ySidZ0F"   # Harrison Laptop
CAPITAL = "1BZI6v5pmpgrt7G2dPsAib3u3S-HqB7ZP"
FOS_ROOT = drive_sweep.FOUNDERS_OS_ROOT_ID


# ── pins ─────────────────────────────────────────────────────────────────────
class TestPins:
    def test_both_roots_pinned_and_walk_only(self):
        for r in (ROOT_A, ROOT_B):
            assert r in kb_exclusions.KB_EXCLUDED_FOLDER_IDS
            assert r in kb_exclusions.KB_EXCLUDED_WALK_ONLY_IDS
            assert kb_exclusions.is_excluded_folder(r)
        assert kb_exclusions.KB_EXCLUDED_WALK_ONLY_IDS <= kb_exclusions.KB_EXCLUDED_FOLDER_IDS
        assert kb_exclusions.KB_EXCLUDED_WALK_ONLY_IDS == frozenset({ROOT_A, ROOT_B})

    def test_every_pin_has_a_label(self):
        assert set(kb_exclusions.KB_EXCLUDED_FOLDER_LABELS) == set(kb_exclusions.KB_EXCLUDED_FOLDER_IDS)
        assert all(v.strip() for v in kb_exclusions.KB_EXCLUDED_FOLDER_LABELS.values())
        assert "Always-On Desktop" in kb_exclusions.KB_EXCLUDED_FOLDER_LABELS[ROOT_A]
        assert "Laptop" in kb_exclusions.KB_EXCLUDED_FOLDER_LABELS[ROOT_B]

    def test_dashboard_set_is_exactly_the_four_stores(self):
        assert kb_exclusions.KB_DASHBOARD_FOLDER_IDS == frozenset({
            "1INi4fLXG23xao-d_yf56Wrbrah54pIBB", "1BZI6v5pmpgrt7G2dPsAib3u3S-HqB7ZP",
            "1NPBNBfx3MMjqQM_WnmL6jOJSaRAQf752", "1HEHpMWgkJkHmV1wfWIiT5OhBI0p5p2P-",
        })
        assert kb_exclusions.KB_DASHBOARD_FOLDER_IDS < kb_exclusions.KB_EXCLUDED_FOLDER_IDS
        assert not (kb_exclusions.KB_DASHBOARD_FOLDER_IDS & kb_exclusions.KB_EXCLUDED_WALK_ONLY_IDS)


# ── expansion never descends into a walk-only root ───────────────────────────
class _Svc:
    """files().list by parent + files().get by id over an in-memory tree.
    children: {folder: [(id, name, is_folder)]}; parents: {id: (parent, name)}."""

    def __init__(self, children, parents, *, fail_get=frozenset()):
        self.children, self.parents_of, self.fail_get = children, parents, set(fail_get)
        self.listed: list[str] = []
        self.got: list[str] = []

    def files(self):  # noqa: A003
        return self

    def list(self, **kw):
        import re
        m = re.search(r"'([^']+)' in parents", kw.get("q", ""))
        parent = m.group(1) if m else None
        self.listed.append(parent)
        only_folders = "mimeType = 'application/vnd.google-apps.folder'" in kw.get("q", "")
        outer = self

        class _R:
            def execute(_s):
                kids = outer.children.get(parent, [])
                if only_folders:
                    kids = [k for k in kids if k[2]]
                return {"files": [{"id": i, "name": n, "mimeType": "application/vnd.google-apps.folder" if f else "text/plain"}
                                  for i, n, f in kids], "nextPageToken": None}
        return _R()

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


def _computers_tree():
    children = {
        ROOT_A: [("DESK_A", "Desktop", True), ("DOCS_A", "Documents", True), ("DL_A", "Downloads", True)],
        "DOCS_A": [("CLAUDE_A", "Claude", True)],
        "CLAUDE_A": [("SCHED_A", "Scheduled", True)],
        "SCHED_A": [("TASK_A", "fndr-weekly-task-verdict-review", True)],
        "TASK_A": [("skill_a", "SKILL.md", False)],
        CAPITAL: [("CAP_NOTES", "_notes", True)],
        "CAP_NOTES": [],
        FOS_ROOT: [("HJRG", "01-HJR-Global", True), ("MEM", "memory", True), ("SHARED", "_shared", True)],
        "HJRG": [("ACC", "accounting", True)],
    }
    parents = {
        ROOT_A: (None, "HJR Always-On Desktop"), "DESK_A": (ROOT_A, "Desktop"), "DOCS_A": (ROOT_A, "Documents"),
        "DL_A": (ROOT_A, "Downloads"), "CLAUDE_A": ("DOCS_A", "Claude"), "SCHED_A": ("CLAUDE_A", "Scheduled"),
        "TASK_A": ("SCHED_A", "fndr-weekly-task-verdict-review"),
        CAPITAL: ("F3E_PROJ", "capital-raise"), "F3E_PROJ": ("F3E", "projects"), "F3E": (FOS_ROOT, "02-F3-Energy"),
        "CAP_NOTES": (CAPITAL, "_notes"),
        FOS_ROOT: ("MYDRIVE", "HJR-Founder-OS"), "MYDRIVE": (None, "My Drive"),
        "HJRG": (FOS_ROOT, "01-HJR-Global"), "ACC": ("HJRG", "accounting"),
        "MEM": (FOS_ROOT, "memory"), "SHARED": (FOS_ROOT, "_shared"),
        "LOOSE": ("MYDRIVE", "loose-folder"),
    }
    return children, parents


class TestExpansionAndWalk:
    def test_expansion_skips_walk_only_roots_but_keeps_them_in_the_set(self):
        ch, pa = _computers_tree()
        svc = _Svc(ch, pa)
        base = frozenset({ROOT_A, ROOT_B, CAPITAL})
        expanded, complete = drive_sweep._expanded_excluded_folder_ids(svc, base)
        assert complete is True
        assert {ROOT_A, ROOT_B, CAPITAL, "CAP_NOTES"} <= expanded          # roots present, capital descended
        assert "DESK_A" not in expanded and "DOCS_A" not in expanded       # never descended into the root
        assert ROOT_A not in svc.listed and ROOT_B not in svc.listed
        assert CAPITAL in svc.listed

    def test_deep_file_under_a_computers_root_is_excluded_even_when_expansion_complete(self):
        ch, pa = _computers_tree()
        svc = _Svc(ch, pa)
        expanded, complete = drive_sweep._expanded_excluded_folder_ids(svc, kb_exclusions.KB_EXCLUDED_FOLDER_IDS)
        cache: dict = {}
        # .../Documents/Claude/Scheduled/<task>/SKILL.md -- 4 levels below the parentless root
        assert drive_sweep._file_disposition(svc, ["TASK_A"], expanded, complete, cache) == "excluded"
        assert drive_sweep._file_under_excluded_folder(svc, ["TASK_A"], expanded, complete, cache) is True
        # a Desktop file (direct child of the root's child)
        assert drive_sweep._file_disposition(svc, ["DESK_A"], expanded, complete, cache) == "excluded"
        # cached per folder: a second file in the same task folder costs no new lookups
        n = len(svc.got)
        assert drive_sweep._file_disposition(svc, ["TASK_A"], expanded, complete, cache) == "excluded"
        assert len(svc.got) == n

    def test_unrelated_and_root_level_files_pass(self):
        ch, pa = _computers_tree()
        svc = _Svc(ch, pa)
        expanded = frozenset(kb_exclusions.KB_EXCLUDED_FOLDER_IDS)
        assert drive_sweep._file_disposition(svc, ["LOOSE"], expanded, True, {}) is None   # My Drive loose folder
        assert drive_sweep._file_disposition(svc, [], expanded, True, {}) is None          # no parents at all
        assert drive_sweep._file_disposition(svc, None, expanded, True, {}) is None

    def test_unresolvable_ancestry_fails_closed(self):
        ch, pa = _computers_tree()
        svc = _Svc(ch, pa, fail_get={"CLAUDE_A"})
        expanded = frozenset(kb_exclusions.KB_EXCLUDED_FOLDER_IDS)
        cache: dict = {}
        assert drive_sweep._file_disposition(svc, ["TASK_A"], expanded, True, cache) == "unresolved"
        assert drive_sweep._file_under_excluded_folder(svc, ["TASK_A"], expanded, True, cache) is True
        assert drive_sweep._any_ancestor_excluded(svc, ["TASK_A"], cache) is True
        assert "CLAUDE_A" not in cache          # the failure is not cached -> the next file retries

    def test_legacy_parents_only_cache_shape_is_upgraded(self):
        ch, pa = _computers_tree()
        svc = _Svc(ch, pa)
        cache = {"TASK_A": ["SCHED_A"]}          # the pre-I3 cache stored parents lists
        chain, ok = drive_sweep._ancestor_chain(svc, ["TASK_A"], cache)
        assert ok and [fid for fid, _ in chain][-1] == ROOT_A
        assert isinstance(cache["TASK_A"], dict)

    def test_sweep_user_skips_a_computers_file_and_never_upserts(self):
        from unittest.mock import MagicMock, patch
        ch, pa = _computers_tree()
        svc = _Svc(ch, pa)
        # the flat sweep lists Harrison's whole Drive: one Computers file, one loose file
        listed = [
            {"id": "skill_a", "name": "SKILL.md", "mimeType": "text/plain", "modifiedTime": "2026-09-04T14:54:46Z",
             "size": "5000", "parents": ["TASK_A"]},
            {"id": "loose1", "name": "vendor-notes.txt", "mimeType": "text/plain", "modifiedTime": "2026-09-04T00:00:00Z",
             "size": "5000", "parents": ["LOOSE"]},
        ]
        flat = MagicMock()
        flat.files.return_value.list.return_value.execute.return_value = {"files": listed, "nextPageToken": None}
        flat.files.return_value.get = svc.get          # ancestry lookups answered by the tree
        kb = MagicMock(); kb.get_sync_state.return_value = None; kb.get_checkpoint.return_value = None
        kb.upsert_documents = MagicMock(return_value=1)
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
        assert stats["dashboard_excluded_skipped"] == 1
        assert ingest.call_count == 1                                  # only the loose file
        assert ingest.call_args[0][1]["id"] == "loose1"


# ── purge_dashboard_kb walks only the dashboard set (cq-bd6eab1fcb44) ─────────
def _load(name):
    path = _REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_dashboard_purge_walks_only_dashboard_ids(monkeypatch):
    purge = _load("purge_dashboard_kb")
    ch = {fid: [] for fid in kb_exclusions.KB_EXCLUDED_FOLDER_IDS}
    svc = _Svc(ch, {})
    import cora.connectors.drive_connector as dc
    monkeypatch.setattr(dc, "_build_drive_service", lambda: svc)
    purge._drive_excluded_file_ids()
    assert set(svc.listed) == set(kb_exclusions.KB_DASHBOARD_FOLDER_IDS)
    assert ROOT_A not in svc.listed and "1YNObhKwo8RITgrRbw3MFpf-0hIiLWTx9" not in svc.listed
    src = (_REPO_ROOT / "scripts" / "purge_dashboard_kb.py").read_text(encoding="utf-8")
    assert "list(KB_EXCLUDED_FOLDER_IDS)" not in src


# ── purge_cora_internal_kb: the Computers-root gate ──────────────────────────
class _FakeDrive:
    """files().list / files().get over an in-memory tree (the purge tests' shape)."""
    FOLDER = "application/vnd.google-apps.folder"

    def __init__(self, children, meta):
        self.children, self.meta = children, meta

    def files(self):  # noqa: A003
        return self

    def list(self, **kw):
        import re
        m = re.search(r"'([^']+)' in parents", kw.get("q", ""))
        parent = m.group(1) if m else None
        outer = self

        class _R:
            def execute(_s):
                return {"files": [{"id": i, "name": n, "mimeType": mm} for i, n, mm in outer.children.get(parent, [])],
                        "nextPageToken": None}
        return _R()

    def get(self, fileId, fields=None):  # noqa: N803
        outer = self

        class _R:
            def execute(_s):
                name, mime, parent = outer.meta[fileId]
                d = {"id": fileId, "name": name, "mimeType": mime}
                if parent:
                    d["parents"] = [parent]
                return d
        return _R()


def _computers_drive():
    F = _FakeDrive.FOLDER
    children = {
        ROOT_A: [("DESK", "Desktop", F), ("DOCS", "Documents", F), ("DL", "Downloads", F)],
        "DESK": [("f1", "notes.md", "text/markdown")],
        "DOCS": [("f2", "Wire Instructions for HJR Global.docx", "application/vnd.openxmlformats")],
        "DL": [],
        "MYDRIVE": [("HJRFOS", "HJR-Founder-OS", F)],
        "UNPINNED": [("f9", "x.md", "text/markdown")],
    }
    meta = {
        ROOT_A: ("HJR Always-On Desktop", F, None), "DESK": ("Desktop", F, ROOT_A), "DOCS": ("Documents", F, ROOT_A),
        "DL": ("Downloads", F, ROOT_A), "MYDRIVE": ("My Drive", F, None), "UNPINNED": ("Some Root", F, None),
        drive_sweep.FOUNDERS_OS_ROOT_ID: ("HJR-Founder-OS", F, "MYDRIVE"),
        "f1": ("notes.md", "text/markdown", "DESK"),
    }
    return children, meta


def _conn():
    import sqlite3
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE knowledge_chunks (chunk_id TEXT, source TEXT, source_id TEXT, title TEXT)")
    c.executemany("INSERT INTO knowledge_chunks VALUES (?,?,?,?)", [
        ("c1", "drive_sweep", "f1", "notes.md"), ("c2", "drive_sweep", "f2", "Wire Instructions for HJR Global.docx"),
        ("x1", "drive_sweep", "f9", "x.md"),
    ])
    return c


class TestComputersRootGate:
    def test_gate_predicate(self):
        purge = _load("purge_cora_internal_kb")
        chain1 = [(ROOT_A, "HJR Always-On Desktop")]
        assert purge._computers_root_ok(ROOT_A, chain1, "MYDRIVE") is True
        assert purge._computers_root_ok(ROOT_A, chain1, ROOT_A) is False           # equals the My Drive root
        assert purge._computers_root_ok(ROOT_A, chain1, None) is False             # root id unknown
        assert purge._computers_root_ok("UNPINNED", [("UNPINNED", "Some Root")], "MYDRIVE") is False
        assert purge._computers_root_ok(purge._FOUNDERS_OS_ROOT_ID, [(purge._FOUNDERS_OS_ROOT_ID, "HJR-Founder-OS")], "MYDRIVE") is False
        assert purge._computers_root_ok(ROOT_A, [(ROOT_A, "x"), ("P", "parent")], "MYDRIVE") is False   # has a parent

    def test_root_admitted_only_with_the_flag_and_matching_leaf(self, tmp_path):
        purge = _load("purge_cora_internal_kb")
        ch, meta = _computers_drive()
        c = _conn()
        with pytest.raises(RuntimeError, match="REFUSED.*root/top-level"):
            purge.run_folder_mode(c, _FakeDrive(ch, meta), [ROOT_A], tmp_path)
        with pytest.raises(RuntimeError, match="REFUSED"):
            purge.run_folder_mode(c, _FakeDrive(ch, meta), [ROOT_A], tmp_path, computers_root=True, my_drive_root_id=None)
        with pytest.raises(RuntimeError, match="expect-leaf"):
            purge.run_folder_mode(c, _FakeDrive(ch, meta), [ROOT_A], tmp_path, computers_root=True,
                                  my_drive_root_id="MYDRIVE", expect_leaf="Desktop")
        ids, files_hit, chunks_hit, complete, sels = purge.run_folder_mode(
            c, _FakeDrive(ch, meta), [ROOT_A], tmp_path, computers_root=True, my_drive_root_id="MYDRIVE",
            expect_leaf="HJR Always-On Desktop")
        assert complete and set(ids) == {"c1", "c2"} and files_hit == 2 and chunks_hit == 2
        assert "x1" not in ids
        manifest = (tmp_path / f"purge-cora-internal-folder-{ROOT_A}.txt").read_text(encoding="utf-8")
        assert "HJR Always-On Desktop" in manifest and "Wire Instructions" in manifest

    def test_unpinned_parentless_and_my_drive_root_stay_refused(self, tmp_path):
        purge = _load("purge_cora_internal_kb")
        ch, meta = _computers_drive()
        c = _conn()
        with pytest.raises(RuntimeError, match="REFUSED"):
            purge.run_folder_mode(c, _FakeDrive(ch, meta), ["UNPINNED"], tmp_path, computers_root=True,
                                  my_drive_root_id="MYDRIVE", expect_leaf="Some Root")
        with pytest.raises(RuntimeError, match="REFUSED"):
            purge.run_folder_mode(c, _FakeDrive(ch, meta), ["MYDRIVE"], tmp_path, computers_root=True,
                                  my_drive_root_id="MYDRIVE", expect_leaf="My Drive")
        with pytest.raises(RuntimeError, match="ROOT"):
            purge.run_folder_mode(c, _FakeDrive(ch, meta), [purge._FOUNDERS_OS_ROOT_ID], tmp_path,
                                  computers_root=True, my_drive_root_id="MYDRIVE", expect_leaf="HJR-Founder-OS")

    def test_drive_service_impersonation_uses_the_dwd_builder(self, monkeypatch, tmp_path):
        purge = _load("purge_cora_internal_kb")
        sa = tmp_path / "sa.json"; sa.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", str(sa))
        calls = {}
        monkeypatch.setattr(drive_sweep, "_build_drive_service", lambda p, email: calls.setdefault("dwd", (p, email)))
        monkeypatch.setattr(drive_sweep, "_build_sa_drive_service_direct", lambda p: calls.setdefault("direct", p))
        purge._drive_service("harrison@hjrglobal.com")
        purge._drive_service()
        assert calls["dwd"] == (str(sa), "harrison@hjrglobal.com") and calls["direct"] == str(sa)
