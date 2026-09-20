"""Code #13 slice 8 -- scripts/drive_sweep_allowlist_manifest.py, the READ-ONLY
migration dry run for the allowlist sweep mode (D-303).

Fake sqlite (plain tables, no vec extension) + a fake Drive service (the _Tree
pattern with file metadata, 404s, trashed rows and a failing folder). The script
is never run live here; every write goes to tmp_path.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.connectors import drive_sweep  # noqa: E402

FOS = drive_sweep.FOUNDERS_OS_ROOT_ID
ACCOUNT = "harrison@hjrglobal.com"


def _load():
    path = _REPO_ROOT / "scripts" / "drive_sweep_allowlist_manifest.py"
    spec = importlib.util.spec_from_file_location("drive_sweep_allowlist_manifest", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# ── fakes ────────────────────────────────────────────────────────────────────
class _NotFound(Exception):
    """Shaped like googleapiclient's HttpError enough for the 404 probe."""
    class _Resp:
        status = 404
    resp = _Resp()


class _Drive:
    """files().get(fileId, fields) over {id: {"name", "parents", "mime", "trashed"}}; ids in
    ``missing`` raise a 404-shaped error, ids in ``fail`` raise a plain 500."""

    def __init__(self, meta, *, missing=frozenset(), fail=frozenset()):
        self.meta = meta
        self.missing = set(missing)
        self.fail = set(fail)
        self.got: list[str] = []

    def files(self):  # noqa: A003
        return self

    def get(self, fileId, fields=None):  # noqa: N803
        self.got.append(fileId)
        outer = self

        class _R:
            def execute(_s):
                if fileId in outer.missing:
                    raise _NotFound("404")
                if fileId in outer.fail:
                    raise RuntimeError("500")
                m = outer.meta[fileId]
                d = {"id": fileId, "name": m["name"], "mimeType": m.get("mime", "application/pdf"),
                     "trashed": bool(m.get("trashed", False))}
                if m.get("parents"):
                    d["parents"] = list(m["parents"])
                return d
        return _R()


def _folder(name, parent):
    return {"name": name, "parents": [parent] if parent else [], "mime": "application/vnd.google-apps.folder"}


def _file(name, parent, **kw):
    return {"name": name, "parents": [parent] if parent else [], **kw}


META = {
    "MY": _folder("My Drive", None),
    FOS: _folder("HJR-Founder-OS", "MY"),
    "HJRG": _folder("01-HJR-Global", FOS),
    "ACC": _folder("accounting", "HJRG"),
    "DL": _folder("Downloads", "MY"),
    "DLSUB": _folder("2025", "DL"),
    "DLSUB2": _folder("scans", "DL"),
    "BAD": _folder("bad", "MY"),
    # files
    "f_in": _file("velvet-harbor-receipt.pdf", "ACC"),
    "f_dl_sub": _file("copper-meadow-scan.pdf", "DLSUB"),
    "f_dl_sub_b": _file("granite-lake-form.pdf", "DLSUB"),
    "f_dl_sub2": _file("saffron-brook-memo.pdf", "DLSUB2"),
    "f_dl_direct": _file("marble-fern-invoice.pdf", "DL"),
    "f_root": _file("quartz-lantern-brief.pdf", "MY"),
    "f_trash": _file("lilac-summit-list.pdf", "DL", trashed=True),
    "f_err": _file("cobalt-ridge-invoice.pdf", "BAD"),
    "f_orphan": _file("amber-tide-notes.pdf", None),
}
ROWS = [  # (file_id, chunks, user_email)
    ("f_in", 5, ACCOUNT), ("f_dl_sub", 3, ACCOUNT), ("f_dl_sub_b", 1, ACCOUNT), ("f_dl_sub2", 2, ACCOUNT),
    ("f_dl_direct", 2, ACCOUNT), ("f_root", 1, ACCOUNT), ("f_gone", 4, ACCOUNT), ("f_trash", 1, ACCOUNT),
    ("f_err", 1, ACCOUNT), ("f_orphan", 1, ACCOUNT),
    ("f_other", 9, "hannah@hjrglobal.com"),               # another account's row: never selected
]


def _conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE knowledge_chunks (chunk_id TEXT PRIMARY KEY, source TEXT, source_id TEXT, "
              "title TEXT, metadata TEXT)")
    n = 0
    for fid, chunks, email in ROWS:
        for i in range(chunks):
            n += 1
            c.execute("INSERT INTO knowledge_chunks VALUES (?,?,?,?,?)",
                      (f"c{n}", "drive_sweep", fid, META.get(fid, {}).get("name", fid), json.dumps({"user_email": email})))
    # a founders_os writer row for the same file id must not count as the flat sweep's
    c.execute("INSERT INTO knowledge_chunks VALUES (?,?,?,?,?)",
              ("cfo", "drive_sweep", "f_in", "x", json.dumps({"user_email": "founders_os@hjrglobal.com"})))
    # a non-drive source with the same id never counts
    c.execute("INSERT INTO knowledge_chunks VALUES (?,?,?,?,?)",
              ("cst", "static_md", "f_in", "x", json.dumps({"user_email": ACCOUNT})))
    return c


def _report(mod, tmp_path, *, limit=0, cache=None, drive=None):
    drive = drive or _Drive(META, missing={"f_gone"}, fail={"BAD"})
    cache = {} if cache is None else cache
    rep = mod.build_report(_conn(), drive, account=ACCOUNT, allowlist=frozenset({FOS}), folder_cache=cache,
                           limit=limit, cache_path=tmp_path / "cache.json")
    return rep, drive, cache


# ── tests ────────────────────────────────────────────────────────────────────
class TestKbSide:
    def test_selects_only_the_accounts_flat_sweep_rows_grouped_by_file(self):
        mod = _load()
        files = mod.load_kb_files(_conn(), ACCOUNT)
        by_id = {f["file_id"]: f["chunks"] for f in files}
        assert by_id["f_in"] == 5 and "f_other" not in by_id and len(by_id) == 10
        assert files[0]["file_id"] == "f_in"                                  # largest first

    def test_limit_takes_the_n_largest(self):
        mod = _load()
        assert [f["file_id"] for f in mod.load_kb_files(_conn(), ACCOUNT, limit=2)] == ["f_in", "f_gone"]

    def test_legacy_chunk_suffix_folds_into_the_bare_id(self):
        mod = _load()
        c = _conn()
        c.execute("INSERT INTO knowledge_chunks VALUES (?,?,?,?,?)",
                  ("cl", "drive_sweep", "f_in:chunk7", "x", json.dumps({"user_email": ACCOUNT})))
        assert {f["file_id"]: f["chunks"] for f in mod.load_kb_files(c, ACCOUNT)}["f_in"] == 6


class TestClassification:
    def test_buckets_and_groups(self, tmp_path):
        mod = _load()
        rep, _, _ = _report(mod, tmp_path)
        b = {e["file_id"]: e["bucket"] for e in rep["entries"]}
        assert b["f_in"] == mod.IN_ALLOWLIST
        assert {b[k] for k in ("f_dl_sub", "f_dl_sub_b", "f_dl_sub2", "f_dl_direct", "f_root", "f_orphan")} == {mod.OUTSIDE}
        assert {b[k] for k in ("f_gone", "f_trash", "f_err")} == {mod.UNRESOLVED}
        assert rep["totals"][mod.OUTSIDE] == {"files": 6, "chunks": 10}
        assert rep["totals"][mod.UNRESOLVED] == {"files": 3, "chunks": 6}
        assert rep["totals"][mod.IN_ALLOWLIST] == {"files": 1, "chunks": 5}

    def test_top_level_group_is_the_child_of_my_drive_with_subtotals(self, tmp_path):
        mod = _load()
        rep, _, _ = _report(mod, tmp_path)
        dl = rep["groups"]["DL"]
        assert dl["name"] == "Downloads" and dl["files"] == 4 and dl["chunks"] == 8       # f_trash NOT counted (stale)
        assert dl["purge_folders"]["DLSUB"] == {"id": "DLSUB", "name": "2025", "files": 2, "chunks": 4}
        assert dl["purge_folders"]["DLSUB2"]["chunks"] == 2
        assert dl["unreachable_files"] == 1 and dl["unreachable_chunks"] == 2             # f_dl_direct (depth-2 folder)
        assert rep["groups"][mod.GROUP_ROOT_LOOSE]["files"] == 1
        assert rep["groups"][mod.GROUP_NO_PARENTS]["files"] == 1

    def test_unresolved_reasons(self, tmp_path):
        mod = _load()
        rep, _, _ = _report(mod, tmp_path)
        st = {e["file_id"]: (e["status"], e["reason"]) for e in rep["entries"]}
        assert st["f_gone"][0] == "missing" and "404" in st["f_gone"][1]
        assert st["f_trash"][0] == "trashed"
        assert st["f_err"][0] == "error"

    def test_purge_depth_floor_mirrors_the_purge_script(self):
        """The manifest's reachability rule must equal the gate it emits lines for."""
        mod = _load()
        sys.path.insert(0, str(_REPO_ROOT / "scripts"))
        import purge_cora_internal_kb as purge  # noqa: E402
        assert mod.PURGE_MIN_CHAIN_DEPTH == purge._MIN_CHAIN_DEPTH
        assert mod.DEFAULT_ACCOUNT == ACCOUNT


class TestManifestText:
    def test_sections_and_purge_lines(self, tmp_path):
        mod = _load()
        rep, _, _ = _report(mod, tmp_path)
        path = mod.write_manifest(rep, tmp_path / "out")
        text = path.read_text(encoding="utf-8")
        assert path.name.startswith(mod.MANIFEST_PREFIX) and path.parent == tmp_path / "out"
        for section in ("CANDIDATES TO ADD TO THE ALLOWLIST", "PURGE LINES", "UNRESOLVED / STALE KB ROWS",
                        "IN THE ALLOWLIST (kept)"):
            assert section in text
        # per-CHILD purge lines through the UNCHANGED gate, dry-run shape (no --apply)
        assert mod.purge_line("DLSUB", "2025", ACCOUNT) in text
        assert mod.purge_line("DLSUB2", "scans", ACCOUNT) in text
        assert "--apply" not in mod.purge_line("DLSUB", "2025", ACCOUNT)
        assert "--expect-leaf \"2025\" --impersonate harrison@hjrglobal.com" in text
        # the top-level folder itself is flagged refused; loose files flagged unreachable
        assert "Downloads [DL] is depth-2" in text and "REFUSED by the gate" in text
        assert "unreachable by the gate -- move in Drive first" in text
        assert "(loose files directly under My Drive)" in text
        # candidates: the yaml line to paste, per out-of-tree top-level folder
        assert '- "DL"   # Downloads' in text
        # stale rows never appear as a purge line
        assert "f_trash" not in text.split("PURGE LINES", 1)[1].split("UNRESOLVED / STALE", 1)[0]
        assert "lilac-summit-list.pdf" in text.split("UNRESOLVED / STALE", 1)[1]
        assert "path: My Drive / HJR-Founder-OS / 01-HJR-Global / accounting" in text
        assert text.isascii()

    def test_never_purges_the_allowlisted_or_root_folders(self, tmp_path):
        mod = _load()
        rep, _, _ = _report(mod, tmp_path)
        text = mod.render_manifest(rep)
        lines = [l for l in text.splitlines() if l.startswith(".venv")]
        assert lines and all(f"--folder-id {x} " not in l for l in lines for x in (FOS, "MY", "DL", "ACC", "HJRG"))


class TestFolderCache:
    def test_cache_persists_and_makes_a_rerun_skip_folder_lookups(self, tmp_path):
        mod = _load()
        rep, drive, cache = _report(mod, tmp_path)
        saved = mod.load_cache(tmp_path / "cache.json")
        assert saved["DLSUB"] == {"parents": ["DL"], "name": "2025"} and "MY" in saved
        assert "BAD" not in saved                                            # a failed lookup is never cached
        drive2 = _Drive(META, missing={"f_gone"}, fail={"BAD"})
        mod.build_report(_conn(), drive2, account=ACCOUNT, allowlist=frozenset({FOS}), folder_cache=saved,
                         limit=0, cache_path=tmp_path / "cache.json")
        folder_gets = [g for g in drive2.got if g in ("MY", FOS, "HJRG", "ACC", "DL", "DLSUB", "DLSUB2")]
        assert folder_gets == []                                             # only the files themselves were fetched

    def test_corrupt_cache_starts_empty(self, tmp_path):
        mod = _load()
        p = tmp_path / "c.json"
        p.write_text("{not json", encoding="utf-8")
        assert mod.load_cache(p) == {}
        assert mod.load_cache(tmp_path / "absent.json") == {}


class TestCli:
    def test_db_and_out_dir_are_required_and_defaults(self):
        mod = _load()
        with pytest.raises(SystemExit):
            mod.build_parser().parse_args([])
        with pytest.raises(SystemExit):
            mod.build_parser().parse_args(["--db", "x.db"])
        a = mod.build_parser().parse_args(["--db", "x.db", "--out-dir", "o"])
        assert a.account == ACCOUNT and a.limit == 0 and a.cache is None and a.allowlist_id == []

    def test_allowlist_source_reads_the_roster_row_or_defaults(self, tmp_path):
        mod = _load()
        allow, src = mod.allowlist_for_account(_REPO_ROOT / "data" / "maps" / "monitored-email-accounts.yaml", ACCOUNT)
        assert allow == frozenset({FOS}) and "drive_sweep_allowlist" in src
        allow2, src2 = mod.allowlist_for_account(tmp_path / "missing.yaml", ACCOUNT)
        assert allow2 == frozenset({FOS}) and src2.startswith("default")
        allow3, src3 = mod.allowlist_for_account(_REPO_ROOT / "data" / "maps" / "monitored-email-accounts.yaml",
                                                 "hannah@hjrglobal.com")
        assert allow3 == frozenset({FOS}) and src3.startswith("default")    # a denylist account has no row allowlist

    def test_limit_audits_only_the_largest(self, tmp_path):
        mod = _load()
        rep, _, _ = _report(mod, tmp_path, limit=2)
        assert rep["files_total"] == 2 and {e["file_id"] for e in rep["entries"]} == {"f_in", "f_gone"}
        assert "LIMIT: only the 2 largest" in mod.render_manifest(rep)
