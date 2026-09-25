"""Code #15 RIDER B (cq-59c5048d0891) -- the PURGE side: the rb* lanes of the ONE
combined script (scripts/purge_kb_code15_2026-09.py) and the id-only / drive_sweep-only
modes added to scripts/purge_cora_internal_kb.py (its gates unchanged).

Contract under test:
  * kb-purge-rows.csv: a strict parse (BOM, LF/CRLF, exact header + actions, no
    absolute / '..' / '/' path, no duplicate) whose sha256 is recorded; static_md rows
    by EXACT source_id (a path carrying GLOB/LIKE metacharacters selects only itself);
    the on-disk existence gate PURGEs only an ABSENT old path (a MOVE also needs its
    new path live with rows) and HOLDs the rest with the why;
  * rb3_archived_nonmd: the UNCHANGED positive-leaf gate on dedup-2026-09 selects
    drive_sweep rows only (drive_asset KEPT + counted), never the rest of _archive,
    HOLDS LEX rows, and writes an ID-ONLY reviewed manifest;
  * rb2 / rb4: recorded no-ops at 0; rb4 selects only exact desktop.ini basenames;
  * rb8: exactly the three constant ids, drive_sweep only (legacy ``<id>:chunkN``
    included), a lookalike id untouched; drift / a failed positive gate / a flat-sweep
    re-ingest STOP the lane;
  * the apply re-verifies each lane BEFORE any write (CSV sha + existence, the folder
    gate + moved-out files, the UFL gate) and writes the APPLIED item record after;
  * no record, manifest or log line carries a title, a Drive name or a path (D-082).

In-memory Drive + a tmp Founder-OS tree + a plain sqlite KB; no network.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

import purge_cora_internal_kb as pci  # noqa: E402
from cora.connectors import drive_sweep  # noqa: E402

_SCRIPT = _REPO / "scripts" / "purge_kb_code15_2026-09.py"


def _load():
    name = "purge_kb_code15_2026_09"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pk = _load()

FOS = drive_sweep.FOUNDERS_OS_ROOT_ID
ARCHIVE, DEDUP, PF = "16q7RfzibKms2rLvBKGIfaTSPBPUGYPaP", "1TSUGC4hAHjgbHuExFqf_4-lXyXm7opq5", "1l7Hms6KwISUelnB-ItLAF9vms6K_Wd9s"
U1, U2, U3 = "10EP-1JvWE99n7yfKxP_GsO4cBrIR6etc", "1F76_2VshuRep2BIc2ft2uhQy2dcaVWmJ", "1m_cstl-LjxnLNIxjfHvIFR3xtoBHgb0_"
FOLDER = "application/vnd.google-apps.folder"
FLAT_WM = int(datetime(2026, 9, 24, 13, 1, 23, tzinfo=timezone.utc).timestamp())

# Planted secrets -- titles / names that must NEVER reach a record, manifest or log.
LEX_TITLE = "Lexington client intake roster.pdf"
UFL_TITLE = "UFL cap table and equity ledger.xlsx"
ARCH_NAME = "archived-duplicate-board-deck.pdf"
SECRET_WORDS = (LEX_TITLE, UFL_TITLE, ARCH_NAME, "cap table", "client intake", "board-deck",
                "hygiene-drive-moves", "moves-copy", "personal-finances-statement", "Lexington")
SLACK_BOT = "xo" + "xb-" + "1234567890123-1234567890123-" + "AbCdEfGhIjKlMnOpQrStUvWx"

# kb-purge-rows.csv relpaths (backslash, as the static_md source_id stores them)
R_ARCHIVE_OLD = r"2026-09-12-hygiene-drive-moves.md"
R_ARCHIVE_NEW = r"_archive\dedup-2026-09\2026-09-12-hygiene-drive-moves.md"
R_MOVE_OLD = r"_shared\hygiene-pending-moves (1)\2026-08-29-hygiene-drive-moves.md"
R_MOVE_NEW = r"_shared\hygiene-pending-moves\2026-08-29-hygiene-drive-moves.md"
R_META_OLD = r"_shared\odd [1] folder\a*b?-moves-copy.md"          # GLOB metacharacters
R_META_NEW = r"_shared\hygiene-pending-moves\ab-moves-copy.md"
R_GONE_OLD = r"_shared\2026-08-29-hygiene-drive-moves.md"
R_GONE_NEW = r"_shared\hygiene-pending-moves\never-landed.md"      # MOVE target absent


# ── fakes ────────────────────────────────────────────────────────────────────
class _Drive:
    """files().list by parent (the positive-leaf gate's BFS) + files().get by id over
    {id: {name, mime, parents, trashed, modifiedTime}}."""

    def __init__(self, meta):
        self.meta = meta
        self.listed: list[str] = []

    def files(self):  # noqa: A003
        return self

    def list(self, **kw):
        import re
        m = re.search(r"'([^']+)' in parents", kw.get("q", ""))
        parent = m.group(1) if m else None
        self.listed.append(parent)
        outer = self

        class _R:
            def execute(_s):
                kids = [{"id": i, "name": d["name"], "mimeType": d.get("mime", "application/pdf")}
                        for i, d in outer.meta.items()
                        if (d.get("parents") or [None])[0] == parent and not d.get("trashed")]
                return {"files": kids, "nextPageToken": None}
        return _R()

    def get(self, fileId, fields=None):  # noqa: N803
        outer = self

        class _R:
            def execute(_s):
                if fileId not in outer.meta:
                    raise RuntimeError(f"404 {fileId}")
                d = outer.meta[fileId]
                out = {"id": fileId, "name": d["name"], "mimeType": d.get("mime", "application/pdf"),
                       "trashed": bool(d.get("trashed")), "modifiedTime": d.get("modifiedTime", "2026-05-22T00:00:00Z")}
                if d.get("parents"):
                    out["parents"] = list(d["parents"])
                return out
        return _R()


def _f(name, parent, **kw):
    return {"name": name, "parents": [parent], **kw}


def _folder(name, parent):
    return {"name": name, "parents": [parent] if parent else [], "mime": FOLDER}


def _meta():
    return {
        FOS: _folder("HJR-Founder-OS", None),                  # the direct-SA view: the root is parentless
        ARCHIVE: _folder("_archive", FOS),
        DEDUP: _folder("dedup-2026-09", ARCHIVE),
        "DSUB": _folder("02-F3-Energy", DEDUP),
        "ASHARED": _folder("_shared", ARCHIVE),
        "FNDR": _folder("00-Founder", FOS),
        PF: _folder("personal-finances", "FNDR"),
        "PF25": _folder("2025", PF),
        "U04": _folder("04-UFL", FOS),
        "UMAN": _folder("manufacturing", "U04"),
        "UVB": _folder("visibility-binder", "U04"),
        # files
        "fa1": _f(ARCH_NAME, "DSUB"),
        "fa2": _f("dup-2.pdf", DEDUP),
        "fa_lex": _f(LEX_TITLE, "DSUB"),
        "fa_asset_only": _f("dup-asset.pdf", DEDUP),
        "fs1": _f("shared-twin.md", "ASHARED", mime="text/markdown"),     # _archive but NOT dedup: out of scope
        "fpf1": _f("personal-finances-statement.pdf", "PF25"),
        U1: _f(UFL_TITLE, "UMAN"),
        U2: _f("UFL equity 2.xlsx", "UMAN"),
        U3: _f("UFL signed envelope.pdf", "UVB"),
    }


def _mkdb(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    dbp = tmp_path / "kb.db"
    c = sqlite3.connect(dbp)
    c.executescript("""
        CREATE TABLE knowledge_chunks (
            chunk_id TEXT PRIMARY KEY, source TEXT NOT NULL, source_id TEXT NOT NULL,
            entity TEXT NOT NULL, date_created INTEGER, date_modified INTEGER, author TEXT,
            title TEXT, content TEXT NOT NULL, deep_link TEXT, metadata TEXT,
            ingested_at INTEGER NOT NULL, sub_entity TEXT);
        CREATE TABLE knowledge_vec_f32 (chunk_id TEXT PRIMARY KEY, embedding BLOB);
        CREATE TABLE sync_state (source TEXT PRIMARY KEY, last_sync_at INTEGER NOT NULL,
                                 last_source_modified INTEGER);
    """)
    rows = [
        # rb3_archived_nonmd -- under dedup-2026-09
        ("a1", "drive_sweep", "fa1", "F3E", ARCH_NAME, "archived deck body " + SLACK_BOT),   # S1 overlap
        ("a2", "drive_sweep", "fa1", "F3E", ARCH_NAME, "archived deck body 2"),
        ("a3", "drive_sweep", "fa2", "HJRG", "dup-2.pdf", "dup body"),
        ("a4", "drive_sweep", "fa2:chunk1", "HJRG", "dup-2.pdf", "dup body legacy id"),
        ("a5", "drive_sweep", "fa_lex", "LEX", LEX_TITLE, "lex body"),
        ("a6", "drive_asset", "fa1", "F3E", ARCH_NAME, "asset card"),
        ("a7", "drive_asset", "fa_asset_only", "F3E", "dup-asset.pdf", "asset card only"),
        # _archive but not dedup (option B is OUT) -- must survive
        ("s1", "drive_sweep", "fs1", "FNDR", "shared-twin.md", "shared twin"),
        # a static_md row that shares a Drive id string -- never a folder-lane row
        ("x1", "static_md", "fa1", "FNDR", "not-a-drive-row", "static body"),
        # kb-purge-rows.csv static_md rows
        ("c1", "static_md", R_ARCHIVE_OLD, "FNDR", "hygiene moves", "old root copy"),
        ("c2", "static_md", R_MOVE_OLD, "FNDR", "hygiene moves", "old move a"),
        ("c3", "static_md", R_MOVE_OLD, "FNDR", "hygiene moves", "old move b"),
        ("c4", "static_md", R_MOVE_NEW, "FNDR", "hygiene moves", "new move"),
        ("c5", "static_md", R_META_OLD, "FNDR", "moves copy", "glob old"),
        ("c6", "static_md", R_META_NEW, "FNDR", "moves copy", "glob new"),
        ("c7", "static_md", R_GONE_OLD, "FNDR", "hygiene moves", "gone old"),
        # lookalikes that must never be selected by the exact match
        ("k1", "static_md", r"_shared\odd [1] folder\aXbY-moves-copy.md", "FNDR", "x", "glob lookalike"),
        ("k2", "static_md", r"Xshared\hygiene-pending-moves (1)\2026-08-29-hygiene-drive-moves.md", "FNDR", "x", "Xshared"),
        ("k3", "static_md", R_MOVE_OLD.upper(), "FNDR", "x", "case variant"),
        # an unrelated live row
        ("live1", "gmail", "m1", "FNDR", "Weekly update", "live body"),
    ]
    # rb8 -- 25 / 25 / 10 drive_sweep rows (one legacy <id>:chunkN) + 1 drive_asset card each
    for fid, n in ((U1, 25), (U2, 25), (U3, 10)):
        for i in range(n):
            sid = f"{fid}:chunk{i}" if i == 3 else fid
            rows.append((f"u-{fid[:4]}-{i}", "drive_sweep", sid, "UFL", UFL_TITLE, f"ufl body {i}"))
        rows.append((f"ua-{fid[:4]}", "drive_asset", fid, "UFL", UFL_TITLE, "ufl asset card"))
    rows.append(("u-lookalike", "drive_sweep", U1 + "X", "UFL", UFL_TITLE, "prefix-sharing id"))
    for cid, src, sid, ent, title, content in rows:
        c.execute("INSERT INTO knowledge_chunks (chunk_id, source, source_id, entity, title, content, ingested_at) "
                  "VALUES (?,?,?,?,?,?,1)", (cid, src, sid, ent, title, content))
        c.execute("INSERT INTO knowledge_vec_f32 VALUES (?, ?)", (cid, b"\x00"))
    c.execute("INSERT INTO sync_state VALUES ('drive_sweep_harrison@hjrglobal.com', ?, NULL)", (FLAT_WM,))
    c.execute("INSERT INTO sync_state VALUES ('founders_os_F3E_x', 1780535380, NULL)")
    c.commit()
    c.close()
    return dbp


def _root(tmp_path: Path) -> Path:
    """The Founder-OS tree on disk: the ARCHIVE row's OLD path is still live (HELD),
    the MOVE rows' old paths are gone and their new paths are live. It carries the
    real root's anchor folders (00-Founder, _shared): the gate refuses a root without."""
    root = tmp_path / "HJR-Founder-OS"
    for anchor in ("00-Founder", "_shared"):
        (root / anchor).mkdir(parents=True, exist_ok=True)
    for rel in (R_ARCHIVE_OLD, R_MOVE_NEW, R_META_NEW):
        p = root / Path(rel.replace("\\", "/"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
    return root


CSV_ROWS = [(R_ARCHIVE_OLD, R_ARCHIVE_NEW, "ARCHIVE"), (R_MOVE_OLD, R_MOVE_NEW, "MOVE"),
            (R_META_OLD, R_META_NEW, "MOVE"), (R_GONE_OLD, R_GONE_NEW, "MOVE")]


def _csv(path: Path, rows=CSV_ROWS, *, bom=True, eol="\n", header="old_relpath,new_relpath,action") -> Path:
    body = eol.join([header] + [",".join(f'"{c}"' if ("," in c) else c for c in r) for r in rows]) + eol
    path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + body.encode("utf-8"))
    return path


class _Env:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.db = _mkdb(tmp_path / "kb")
        self.root = _root(tmp_path)
        self.csv = _csv(tmp_path / "kb-purge-rows.csv")
        self.hash_store = tmp_path / "hashes.json"
        self.hash_store.write_text(json.dumps({R_ARCHIVE_OLD: "d1", R_MOVE_OLD: "d2"}), encoding="utf-8")
        self.out = tmp_path / "out"
        self.drive = _Drive(_meta())

    def args(self, *extra, csv=True):
        a = ["--db", str(self.db), "--out-dir", str(self.out), "--founder-root", str(self.root),
             "--hash-store", str(self.hash_store)]
        if csv:
            a += ["--csv", str(self.csv)]
        return a + list(extra)

    def dry(self, *extra, csv=True) -> tuple[Path, dict]:
        before = set(self.out.glob("kb-purge-code15-INTENT-*.json")) if self.out.exists() else set()
        assert pk.main(self.args(*extra, csv=csv)) == 0
        new = [p for p in self.out.glob("kb-purge-code15-INTENT-*.json") if p not in before]
        assert len(new) == 1
        return new[0], json.loads(new[0].read_text(encoding="utf-8"))

    def apply(self, intent: Path, *extra, csv=True) -> int:
        return pk.main(self.args("--apply", "--manifest", str(intent), *extra, csv=csv))

    def ids(self) -> set[str]:
        c = sqlite3.connect(self.db)
        try:
            return {r[0] for r in c.execute("SELECT chunk_id FROM knowledge_chunks")}
        finally:
            c.close()

    def vec_ids(self) -> set[str]:
        c = sqlite3.connect(self.db)
        try:
            return {r[0] for r in c.execute("SELECT chunk_id FROM knowledge_vec_f32")}
        finally:
            c.close()

    def digest(self) -> str:
        return hashlib.sha256(self.db.read_bytes()).hexdigest()


@pytest.fixture(autouse=True)
def _offline_drive(monkeypatch):
    """No test here may reach a real Drive: the default factory is replaced by one
    that raises; a test that needs Drive installs its in-memory fake."""
    def _no_drive():
        raise RuntimeError("real Drive disabled in tests")
    monkeypatch.setattr(pk, "DRIVE_SERVICE_FACTORY", _no_drive)


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = _Env(tmp_path)
    monkeypatch.setattr(pk, "DRIVE_SERVICE_FACTORY", lambda: e.drive)
    return e


def _no_text(blob: str) -> None:
    for w in SECRET_WORDS:
        assert w not in blob, w
    for rel in (R_ARCHIVE_OLD, R_MOVE_OLD, R_META_OLD, R_GONE_OLD, R_MOVE_NEW):
        assert rel not in blob and rel.replace("\\", "\\\\") not in blob, rel
    assert SLACK_BOT not in blob


# ── kb-purge-rows.csv parse ──────────────────────────────────────────────────
class TestCsvParse:
    def test_bom_lf_and_bom_crlf_and_no_bom_parse_identically(self, tmp_path):
        a, sha_a = pk.parse_purge_rows_csv(_csv(tmp_path / "a.csv"))
        b, sha_b = pk.parse_purge_rows_csv(_csv(tmp_path / "b.csv", eol="\r\n"))
        c, _ = pk.parse_purge_rows_csv(_csv(tmp_path / "c.csv", bom=False))
        assert [(r.index, r.old, r.new, r.action) for r in a] == [(r.index, r.old, r.new, r.action) for r in b] \
            == [(r.index, r.old, r.new, r.action) for r in c]
        assert [r.index for r in a] == [1, 2, 3, 4] and a[0].old == R_ARCHIVE_OLD
        assert sha_a == hashlib.sha256((tmp_path / "a.csv").read_bytes()).hexdigest() and sha_a != sha_b

    def test_a_trailing_blank_line_is_fine(self, tmp_path):
        p = _csv(tmp_path / "t.csv")
        p.write_bytes(p.read_bytes() + b"\n")
        assert len(pk.parse_purge_rows_csv(p)[0]) == 4

    @pytest.mark.parametrize("rows,header", [
        ([("a.md", "b.md", "DELETE")], None),                         # unknown action
        ([("a.md", "b.md", "move")], None),                           # actions are exact
        ([("C:\\x\\a.md", "b.md", "MOVE")], None),                    # absolute (drive letter)
        ([("\\x\\a.md", "b.md", "MOVE")], None),                      # absolute (root-relative)
        ([("_shared\\..\\a.md", "b.md", "MOVE")], None),              # '..'
        ([("_shared/a.md", "b.md", "MOVE")], None),                   # '/' is never rewritten
        ([("_shared\\\\a.md", "b.md", "MOVE")], None),                # empty segment
        ([(" a.md", "b.md", "MOVE")], None),                          # surrounding whitespace
        ([("a.md", "a.md", "MOVE")], None),                           # old == new
        ([("a.md", "b.md", "MOVE"), ("a.md", "c.md", "MOVE")], None),  # duplicate old
        ([("", "", "")], None),                                       # a row of empty fields
        ([], None),                                                   # no data rows
        ([("a.md", "b.md", "MOVE")], "old,new,action"),               # wrong header
        ([("a.md", "b.md", "MOVE")], "old_relpath,new_relpath,action,extra"),
    ])
    def test_refusals(self, tmp_path, rows, header):
        p = _csv(tmp_path / "bad.csv", rows, header=header or "old_relpath,new_relpath,action")
        with pytest.raises(pk.CsvRefused) as ei:
            pk.parse_purge_rows_csv(p)
        assert "a.md" not in str(ei.value) and "_shared" not in str(ei.value)   # row index + rule, never the path

    def test_extra_and_missing_columns_refused(self, tmp_path):
        p = tmp_path / "x.csv"
        p.write_bytes(b"old_relpath,new_relpath,action\na.md,b.md,MOVE,oops\n")
        with pytest.raises(pk.CsvRefused, match="extra"):
            pk.parse_purge_rows_csv(p)
        p.write_bytes(b"old_relpath,new_relpath,action\na.md,b.md\n")
        with pytest.raises(pk.CsvRefused, match="missing"):
            pk.parse_purge_rows_csv(p)


# ── rb3_static_old_paths ─────────────────────────────────────────────────────
class TestStaticOldPaths:
    def test_existence_gate_dispositions(self, env):
        _, data = env.dry()
        lane = data["lanes"]["rb3_static_old_paths"]
        assert lane["stops"] == []
        rows = {r["row"]: r for r in lane["counts"]["rows"]}
        assert rows[1]["disposition"] == "HELD" and rows[1]["old_exists"] is True     # ARCHIVE not applied
        assert "LIVE" in rows[1]["reason"] and rows[1]["hash_store_key_present"] is True
        assert rows[2]["disposition"] == "PURGE" and rows[2]["new_chunks"] == 1
        assert rows[3]["disposition"] == "PURGE"                                       # the GLOB-metachar path
        assert rows[4]["disposition"] == "HELD" and "target absent" in rows[4]["reason"]
        assert sorted(lane["chunk_ids"]) == ["c2", "c3", "c5"]                         # exact matches only
        held = {h["row"]: h for h in lane["holds"]}
        assert held[1]["chunk_ids"] == ["c1"] and held[4]["chunk_ids"] == ["c7"]
        assert lane["counts"]["csv_sha256"] == hashlib.sha256(env.csv.read_bytes()).hexdigest()
        assert lane["counts"]["by_disposition"] == {"PURGE": 2, "HELD": 2, "NO_ROWS": 0}
        assert lane["files"] == 2

    def test_exact_match_never_cross_selects(self, env):
        conn = sqlite3.connect(env.db)
        try:
            assert pk._static_ids(conn, R_META_OLD) == ["c5"]          # [1], *, ? are literal
            assert sorted(pk._static_ids(conn, R_MOVE_OLD)) == ["c2", "c3"]   # not Xshared, not the case variant
            assert pk._static_ids(conn, R_MOVE_OLD.replace("_shared", "_Shared")) == []   # case-sensitive
            assert pk._static_ids(conn, R_MOVE_OLD.upper()) == ["k3"]                   # only its own row
        finally:
            conn.close()

    def test_a_move_whose_target_has_no_rows_is_held_and_zero_matches_reported(self, env, tmp_path):
        env.csv = _csv(tmp_path / "v.csv", [(R_MOVE_OLD, r"_shared\landed-but-not-ingested.md", "MOVE"),
                                             (r"_shared\no-such-row.md", r"_shared\x.md", "MOVE")])
        (env.root / "_shared" / "landed-but-not-ingested.md").write_text("x", encoding="utf-8")
        _, data = env.dry()
        rows = {r["row"]: r for r in data["lanes"]["rb3_static_old_paths"]["counts"]["rows"]}
        assert rows[1]["disposition"] == "HELD" and "no static_md rows" in rows[1]["reason"]
        assert rows[2]["disposition"] == "NO_ROWS" and "case or separator" in rows[2]["reason"]
        assert data["lanes"]["rb3_static_old_paths"]["chunk_ids"] == []

    def test_no_csv_or_missing_root_stops(self, env):
        _, data = env.dry(csv=False)
        assert "no --csv" in data["lanes"]["rb3_static_old_paths"]["stops"][0]["reason"]
        env.root = env.tmp / "no-such-root"
        _, data = env.dry()
        assert "Founder-OS root" in data["lanes"]["rb3_static_old_paths"]["stops"][0]["reason"]

    def test_a_refused_csv_stops_the_lane(self, env, tmp_path):
        env.csv = _csv(tmp_path / "bad.csv", [("a.md", "b.md", "DELETE")])
        _, data = env.dry()
        assert data["lanes"]["rb3_static_old_paths"]["stops"][0]["reason"].startswith("CSV refused: row 1")

    # D-051 r1 purge#2: a wrong root / a gone mount must STOP, never read as ABSENT.
    @pytest.mark.parametrize("where", ["unrelated", "sub-folder"])
    def test_a_root_without_its_anchor_folders_stops_the_lane(self, env, where):
        if where == "unrelated":
            env.root = env.tmp / "some-other-existing-dir"
            env.root.mkdir()
        else:
            env.root = env.root / "_shared"                          # a typo'd --founder-root one level down
        _, data = env.dry()
        lane = data["lanes"]["rb3_static_old_paths"]
        assert lane["chunk_ids"] == [] and lane["stops"], lane["counts"].get("rows")
        assert "anchor folders" in lane["stops"][0]["reason"] and "Founder-OS root" in lane["stops"][0]["reason"]

    def test_a_mount_gone_mid_gate_stops_instead_of_reading_absent(self, env, monkeypatch):
        from cora import drive_io
        real_exists, real_static_ids = drive_io.exists, pk._static_ids
        gone = {"on": False}

        def _exists(path, **kw):                    # Path.exists on a gone drive letter: False, no raise
            return False if gone["on"] else real_exists(path, **kw)

        def _static_ids(conn, relpath):              # the root check passed; the mount drops as the rows start
            gone["on"] = True
            return real_static_ids(conn, relpath)
        monkeypatch.setattr(drive_io, "exists", _exists)
        monkeypatch.setattr(pk, "_static_ids", _static_ids)
        _, data = env.dry("--lanes", "rb3_static_old_paths")
        lane = data["lanes"]["rb3_static_old_paths"]
        assert lane["chunk_ids"] == [], "row 1 (ARCHIVE, old path LIVE) was planned on absence alone"
        assert lane["stops"] and "mount unavailable" in lane["stops"][0]["reason"]

    def test_an_apply_under_a_different_root_refuses(self, env):
        import shutil
        intent, data = env.dry("--lanes", "rb3_static_old_paths")
        assert sorted(data["lanes"]["rb3_static_old_paths"]["chunk_ids"]) == ["c2", "c3", "c5"]
        other = env.tmp / "copy-of-the-tree"
        shutil.copytree(env.root, other)                        # same files, same anchors -- a different root
        real = env.root
        env.root = other
        digest = env.digest()
        assert env.apply(intent, "--lanes", "rb3_static_old_paths") == 1 and env.digest() == digest
        assert {"c2", "c3", "c5"} <= env.ids()
        counts = data["lanes"]["rb3_static_old_paths"]["counts"]
        assert counts["founder_root_sha256"] == pk.founder_root_digest(real) != pk.founder_root_digest(other)
        _no_text(json.dumps(data))
        env.root = real
        assert env.apply(intent, "--lanes", "rb3_static_old_paths") == 0 and {"c2", "c3", "c5"}.isdisjoint(env.ids())


# ── rb3_archived_nonmd + rb2_personal_finances ───────────────────────────────
class TestFolderLanes:
    def test_dedup_selects_drive_sweep_only_keeps_asset_holds_lex(self, env):
        _, data = env.dry()
        lane = data["lanes"]["rb3_archived_nonmd"]
        assert lane["stops"] == []
        assert sorted(lane["chunk_ids"]) == ["a1", "a2", "a3", "a4"]       # legacy fa2:chunk1 included
        assert [h["chunk_id"] for h in lane["holds"]] == ["a5"]            # LEX held
        c = lane["counts"]
        assert c["kept_by_source"] == {"drive_asset": {"chunks": 2, "files": 2}}
        assert c["chain_ids"] == [DEDUP, ARCHIVE, FOS] and c["chain_depth"] == 3 and c["complete"] is True
        assert c["by_source_entity"] == {"drive_sweep": {"F3E": 2, "HJRG": 2}} and c["held_lex_chunks"] == 1
        assert lane["files"] == 2
        # never the rest of _archive (option B is OUT) and never a static_md row
        assert "s1" not in lane["chunk_ids"] and "x1" not in lane["chunk_ids"]
        assert "ASHARED" not in env.drive.listed

    def test_release_lex_plans_the_lex_row(self, env):
        _, data = env.dry("--release-lex", "rb3_archived_nonmd")
        lane = data["lanes"]["rb3_archived_nonmd"]
        assert "a5" in lane["chunk_ids"] and lane["holds"] == []

    def test_the_reviewed_manifest_is_id_only(self, env):
        intent, data = env.dry()
        fdir = intent.parent / data["folder_manifest_dir"]
        man = (fdir / f"purge-cora-internal-folder-{DEDUP}.txt").read_text(encoding="utf-8")
        assert "ID-ONLY" in man and f"{DEDUP} <- {ARCHIVE} <- {FOS}" in man
        assert "dedup-2026-09" not in man and "_archive (" not in man
        # the REVIEWED set is every file the gate selected -- incl. the LEX file whose row the lane HOLDS
        assert pci.parse_manifest_file_ids(fdir / f"purge-cora-internal-folder-{DEDUP}.txt") == {"fa1", "fa2", "fa_lex"}
        _no_text(man)

    def test_personal_finances_is_a_recorded_no_op(self, env):
        _, data = env.dry()
        lane = data["lanes"]["rb2_personal_finances"]
        assert lane["stops"] == [] and lane["chunk_ids"] == []
        assert lane["counts"]["expected_chunks"] == 0 and lane["counts"]["matches_expectation"] is True
        assert lane["counts"]["files_enumerated"] == 1 and lane["counts"]["sources_selected"] == ["drive_sweep", "drive_asset"]

    def test_a_leaf_mismatch_stops_without_naming_the_folder(self, env, monkeypatch):
        spec = pk.FolderLaneSpec(lane="rb3_archived_nonmd", item="t", folder_id=DEDUP,
                                 expect_leaf="not-the-leaf", sources=("drive_sweep",))
        ctx = pk.RunContext(folder_dir=env.tmp / "fd", drive_factory=lambda: env.drive)
        conn = sqlite3.connect(env.db)
        try:
            plan = pk.select_folder_lane(spec, conn, ctx)
        finally:
            conn.close()
        reason = plan.stops[0]["reason"]
        assert "does not match" in reason and "dedup-2026-09" not in reason and DEDUP in reason

    def test_drive_unavailable_stops_only_the_drive_lanes(self, env, monkeypatch):
        def _down():
            raise ConnectionError("dns")
        monkeypatch.setattr(pk, "DRIVE_SERVICE_FACTORY", _down)
        intent, data = env.dry()
        for name in ("rb2_personal_finances", "rb3_archived_nonmd", "rb8_ufl_equity"):
            assert data["lanes"][name]["stops"][0]["reason"] == "Drive service unavailable (ConnectionError)"
        assert data["lanes"]["rb3_static_old_paths"]["stops"] == [] and data["lanes"]["rb4_desktop_ini"]["stops"] == []
        digest = env.digest()
        assert env.apply(intent) == 1 and env.digest() == digest       # a lane with stops refuses the all-lanes apply
        assert env.apply(intent, "--lanes", "rb3_static_old_paths,rb4_desktop_ini") == 0
        assert {"c2", "c3", "c5"}.isdisjoint(env.ids()) and "a1" in env.ids()

    def test_the_combined_script_never_runs_the_always_on_passes(self, env, monkeypatch):
        def _boom(*a, **k):
            raise AssertionError("purge_cora_internal_kb's always-on passes / main() must never run here")
        for fn in ("main", "target_static_md", "target_drive_doc_copies", "target_notes", "delete_chunks"):
            monkeypatch.setattr(pci, fn, _boom)
        intent, _ = env.dry()
        assert env.apply(intent, "--lanes", "rb3_archived_nonmd,rb2_personal_finances") == 0


# ── rb4_desktop_ini ──────────────────────────────────────────────────────────
class TestDesktopIni:
    def test_zero_on_a_clean_seed(self, env):
        _, data = env.dry()
        lane = data["lanes"]["rb4_desktop_ini"]
        assert lane["chunk_ids"] == [] and lane["counts"]["matches_expectation"] is True

    def test_only_exact_basenames_in_the_drive_and_static_sources(self, env):
        c = sqlite3.connect(env.db)
        for cid, src, sid, title in (("d1", "drive_sweep", "fx1", "Desktop.INI"), ("d2", "drive_asset", "fx2", "desktop.ini"),
                                     ("d3", "static_md", r"00-Founder\x\desktop.ini", "t"),
                                     ("d4", "drive_sweep", "fx3", "desktop.ini.bak"), ("d5", "gmail", "m9", "desktop.ini"),
                                     ("d6", "static_md", r"00-Founder\desktop.initial.md", "t")):
            c.execute("INSERT INTO knowledge_chunks (chunk_id, source, source_id, entity, title, content, ingested_at) "
                      "VALUES (?,?,?,'FNDR',?,'x',1)", (cid, src, sid, title))
        c.commit()
        c.close()
        _, data = env.dry()
        lane = data["lanes"]["rb4_desktop_ini"]
        assert sorted(lane["chunk_ids"]) == ["d1", "d2", "d3"] and lane["counts"]["matches_expectation"] is False


# ── rb8_ufl_equity ───────────────────────────────────────────────────────────
class TestUflIds:
    def test_selects_exactly_the_drive_sweep_rows_of_the_three_ids(self, env):
        _, data = env.dry()
        lane = data["lanes"]["rb8_ufl_equity"]
        assert lane["stops"] == []
        assert len(lane["chunk_ids"]) == 60 and all(c.startswith("u-") for c in lane["chunk_ids"])
        assert "u-lookalike" not in lane["chunk_ids"]
        assert not any(c.startswith("ua-") for c in lane["chunk_ids"])          # drive_asset KEPT
        per = {r["file_id"]: r for r in lane["counts"]["per_file"]}
        assert per[U1]["drive_sweep_chunks"] == 25 and per[U1]["drive_asset_kept"] == 1
        assert per[U3]["in_04_ufl"] is True and per[U3]["flat_next_pass_reingests"] is False
        ra = lane["counts"]["reingest_analysis"]
        assert ra["flat_sweep_next_pass"].startswith("NO") and ra["tree_walk"].startswith("LATENT")
        assert ra["exclusion_built"] is False and lane["counts"]["founders_os_ufl_watermark_utc"] is None

    def test_count_drift_stops(self, env):
        c = sqlite3.connect(env.db)
        c.execute("DELETE FROM knowledge_chunks WHERE chunk_id = ?", (f"u-{U2[:4]}-0",))
        c.commit()
        c.close()
        _, data = env.dry()
        reasons = [s["reason"] for s in data["lanes"]["rb8_ufl_equity"]["stops"]]
        assert any(U2 in r and "drift" in r and "expected 25, found 24" in r for r in reasons)

    @pytest.mark.parametrize("mutate,why", [
        (lambda m: m[U1].update(trashed=True), "trashed"),
        (lambda m: m[U1].update(mime=FOLDER), "folder"),
        (lambda m: m[U1].update(parents=["FNDR"]), "04-UFL"),
    ])
    def test_a_failed_positive_gate_stops(self, env, mutate, why):
        mutate(env.drive.meta)
        _, data = env.dry()
        reasons = [s["reason"] for s in data["lanes"]["rb8_ufl_equity"]["stops"]]
        assert any(U1 in r and why in r for r in reasons), reasons

    def test_a_file_the_flat_sweep_would_reingest_stops(self, env):
        env.drive.meta[U2]["modifiedTime"] = "2026-09-24T20:00:00Z"            # after the watermark
        _, data = env.dry()
        reasons = [s["reason"] for s in data["lanes"]["rb8_ufl_equity"]["stops"]]
        assert any(U2 in r and "NEXT pass" in r for r in reasons)


# ── the combined apply ───────────────────────────────────────────────────────
class TestApply:
    def test_end_to_end_deletes_exactly_the_plan_and_records_the_items(self, env, caplog):
        caplog.set_level(logging.INFO)
        intent, data = env.dry()
        assert data["union"]["redact_also_deleted"] == 1                   # a1 carries a token: DELETED, not redacted
        assert env.apply(intent) == 0
        ids = env.ids()
        deleted = {"a1", "a2", "a3", "a4", "c2", "c3", "c5"} | {c for c in data["lanes"]["rb8_ufl_equity"]["chunk_ids"]}
        assert deleted.isdisjoint(ids) and deleted.isdisjoint(env.vec_ids())       # vectors cascaded
        for keep in ("a5", "a6", "a7", "s1", "x1", "c1", "c4", "c6", "c7", "k1", "k2", "k3", "live1",
                     "u-lookalike", f"ua-{U1[:4]}"):
            assert keep in ids, keep
        rec = json.loads(sorted(env.out.glob("kb-purge-code15-APPLIED-*.json"))[0].read_text(encoding="utf-8"))
        ir = rec["item_record"]
        assert ir["rb3_archived_nonmd"]["status"] == "APPLIED" and ir["rb3_archived_nonmd"]["deleted"] == 4
        assert ir["rb3_archived_nonmd"]["holds"] == 1
        assert ir["rb3_static_old_paths"]["deleted"] == 3 and len(ir["rb3_static_old_paths"]["held_why"]) == 2
        assert ir["rb2_personal_finances"]["status"].startswith("APPLIED (no-op")
        assert ir["rb4_desktop_ini"]["status"].startswith("APPLIED (no-op")
        assert ir["rb8_ufl_equity"]["deleted"] == 60
        assert rec["verified"]["rb3_archived_nonmd"] == "verified"
        assert rec["verified"]["rb2_personal_finances"] == "skipped (0 planned)"
        assert rec["outcome"]["_delete_union"]["remaining_after"] == 0
        # D-082: no title, name or path in any record, manifest or log line
        for p in list(env.out.rglob("*")):
            if p.is_file():
                _no_text(p.read_text(encoding="utf-8"))
        _no_text(caplog.text)

    def test_apply_without_the_csv_refuses_and_writes_nothing(self, env):
        intent, _ = env.dry()
        digest = env.digest()
        assert env.apply(intent, csv=False) == 1 and env.digest() == digest

    def test_a_changed_csv_refuses(self, env):
        intent, _ = env.dry()
        env.csv.write_bytes(env.csv.read_bytes() + b"x.md,y.md,MOVE\n")
        digest = env.digest()
        assert env.apply(intent) == 1 and env.digest() == digest

    def test_an_old_path_that_came_back_refuses(self, env):
        intent, _ = env.dry()
        p = env.root / Path(R_MOVE_OLD.replace("\\", "/"))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("restored", encoding="utf-8")
        digest = env.digest()
        assert env.apply(intent) == 1 and env.digest() == digest

    def test_a_file_moved_out_of_the_folder_refuses(self, env):
        intent, _ = env.dry()
        env.drive.meta["fa1"]["parents"] = ["FNDR"]                          # restored to the live tree
        digest = env.digest()
        assert env.apply(intent, "--lanes", "rb3_archived_nonmd") == 1 and env.digest() == digest

    def test_a_new_file_in_the_folder_refuses_unless_accept_delta(self, env):
        intent, _ = env.dry()
        env.drive.meta["fa_new"] = _f("late.pdf", DEDUP)
        c = sqlite3.connect(env.db)
        c.execute("INSERT INTO knowledge_chunks (chunk_id, source, source_id, entity, title, content, ingested_at) "
                  "VALUES ('anew','drive_sweep','fa_new','F3E','late.pdf','late',1)")
        c.commit()
        c.close()
        digest = env.digest()
        assert env.apply(intent, "--lanes", "rb3_archived_nonmd") == 1 and env.digest() == digest
        assert env.apply(intent, "--lanes", "rb3_archived_nonmd", "--accept-delta") == 0
        ids = env.ids()
        assert "anew" in ids and "a1" not in ids                          # only the REVIEWED ids are deleted

    def test_a_ufl_gate_failing_at_apply_refuses(self, env):
        intent, _ = env.dry()
        env.drive.meta[U3]["trashed"] = True
        digest = env.digest()
        assert env.apply(intent, "--lanes", "rb8_ufl_equity") == 1 and env.digest() == digest

    @pytest.mark.parametrize("lane,cid", [("rb4_desktop_ini", "live1"), ("rb3_static_old_paths", "c4"),
                                          ("rb8_ufl_equity", "u-lookalike"), ("rb3_archived_nonmd", "s1"),
                                          ("rb2_personal_finances", "live1")])
    def test_an_edited_intent_cannot_smuggle_a_live_chunk(self, env, lane, cid):
        # the digest is forged correctly, so only the lane's own selector re-run can refuse it
        intent, data = env.dry()
        c = sqlite3.connect(env.db)
        content, title = c.execute("SELECT content, title FROM knowledge_chunks WHERE chunk_id=?", (cid,)).fetchone()
        c.close()
        data["lanes"][lane]["chunk_ids"].append(cid)
        data["lanes"][lane]["pre_sha256"][cid] = pk.chunk_digest(content, title)
        intent.write_text(json.dumps(data), encoding="utf-8")
        digest = env.digest()
        assert env.apply(intent, "--lanes", lane) == 1 and env.digest() == digest
        assert cid in env.ids()

    # D-051 r1 purge#0: the LEX hold is re-decided at --apply from the CURRENT entity --
    # the folder lanes' selector re-run admits LEX rows (the hold is applied after it).
    def test_a_planned_row_retagged_to_lex_refuses_without_release(self, env):
        intent, data = env.dry()
        assert "a3" in data["lanes"]["rb3_archived_nonmd"]["chunk_ids"]
        c = sqlite3.connect(env.db)
        c.execute("UPDATE knowledge_chunks SET entity='LEX' WHERE chunk_id='a3'")
        c.commit()
        c.close()
        digest = env.digest()
        assert env.apply(intent, "--lanes", "rb3_archived_nonmd") == 1 and env.digest() == digest
        assert "a3" in env.ids()

    def test_an_edited_intent_cannot_smuggle_the_held_lex_row(self, env):
        intent, data = env.dry()
        assert [h["chunk_id"] for h in data["lanes"]["rb3_archived_nonmd"]["holds"]] == ["a5"]
        c = sqlite3.connect(env.db)
        content, title = c.execute("SELECT content, title FROM knowledge_chunks WHERE chunk_id='a5'").fetchone()
        c.close()
        data["lanes"]["rb3_archived_nonmd"]["chunk_ids"].append("a5")
        data["lanes"]["rb3_archived_nonmd"]["pre_sha256"]["a5"] = pk.chunk_digest(content, title)
        intent.write_text(json.dumps(data), encoding="utf-8")
        digest = env.digest()
        assert env.apply(intent, "--lanes", "rb3_archived_nonmd") == 1 and env.digest() == digest
        assert "a5" in env.ids()

    def test_a_released_lex_row_is_applied_and_counted(self, env):
        intent, data = env.dry("--release-lex", "rb3_archived_nonmd")
        assert "a5" in data["lanes"]["rb3_archived_nonmd"]["chunk_ids"]
        assert env.apply(intent, "--lanes", "rb3_archived_nonmd", "--release-lex", "rb3_archived_nonmd") == 0
        assert "a5" not in env.ids()
        rec = json.loads(sorted(env.out.glob("kb-purge-code15-APPLIED-*.json"))[0].read_text(encoding="utf-8"))
        assert rec["lex_partition_gate"]["lex_chunks_in_released_lanes"] == {"rb3_archived_nonmd": 1}

    def test_a_zero_planned_lane_is_not_reverified(self, env, monkeypatch):
        intent, _ = env.dry()

        def _down():
            raise ConnectionError("dns")
        monkeypatch.setattr(pk, "DRIVE_SERVICE_FACTORY", _down)
        assert env.apply(intent, "--lanes", "rb2_personal_finances,rb4_desktop_ini") == 0


# ── purge_cora_internal_kb: the id-only / drive_sweep-only modes ─────────────
def _plain_conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE knowledge_chunks (chunk_id TEXT PRIMARY KEY, source TEXT, source_id TEXT, title TEXT)")
    c.executemany("INSERT INTO knowledge_chunks VALUES (?,?,?,?)", [
        ("p1", "drive_sweep", "fa1", ARCH_NAME), ("p2", "drive_asset", "fa1", ARCH_NAME),
        ("p3", "drive_sweep", "fa2:chunk2", "dup-2.pdf"), ("p4", "static_md", "fa1", "x"),
    ])
    return c


class TestPciModes:
    def test_sources_restriction_selects_one_and_counts_the_other(self):
        c = _plain_conn()
        ids, hits, kept = pci.target_folder_descendants_ex(c, {"fa1": ARCH_NAME, "fa2": "d"}, sources=("drive_sweep",))
        assert sorted(ids) == ["p1", "p3"] and kept == {"drive_asset": {"chunks": 1, "files": 1}}
        assert pci.target_folder_descendants(c, {"fa1": "n"}) == (["p1", "p2"], {"fa1": ("n", 2)})   # default unchanged
        with pytest.raises(RuntimeError):
            pci.target_folder_descendants(c, {"fa1": "n"}, sources=("static_md",))
        with pytest.raises(RuntimeError):
            pci.target_folder_descendants(c, {"fa1": "n"}, sources=())

    def test_id_only_manifest_parses_back_and_carries_no_names(self, tmp_path):
        p = tmp_path / "m.txt"
        chain = [("dedup-2026-09", DEDUP), ("_archive", ARCHIVE), ("HJR-Founder-OS", FOS)]
        pci.write_folder_manifest(p, folder_id=DEDUP, chain=chain, complete=True, n_descendants=3,
                                  hits={"fa1": (ARCH_NAME, 2), "fa2": (LEX_TITLE, 1)},
                                  allowlisted=["code-session-backlog.md"], names=False,
                                  sources=("drive_sweep",), kept={"drive_asset": {"chunks": 1, "files": 1}})
        text = p.read_text(encoding="utf-8")
        assert ARCH_NAME not in text and LEX_TITLE not in text and "dedup-2026-09" not in text
        assert "code-session-backlog.md" not in text and "allowlisted basenames selected: 1" in text
        assert pci.parse_manifest_file_ids(p) == {"fa1", "fa2"}
        named = tmp_path / "n.txt"
        pci.write_folder_manifest(named, folder_id=DEDUP, chain=chain, complete=True, n_descendants=3,
                                  hits={"fa1": (ARCH_NAME, 2)})
        assert ARCH_NAME in named.read_text(encoding="utf-8")           # the default is unchanged

    def test_id_only_dump_has_no_titles(self, tmp_path):
        c = _plain_conn()
        p = tmp_path / "d.json"
        assert pci.dump_selected_rows(c, ["p1", "p4"], p, names=False) == 2
        text = p.read_text(encoding="utf-8")
        assert ARCH_NAME not in text and '"title"' not in text and '"id_only": true' in text
        rows = {r["chunk_id"]: r for r in json.loads(text)["rows"]}
        assert rows["p1"]["source_id"] == "fa1" and "source_id" not in rows["p4"]    # a path-shaped id is digested

    def test_run_folder_mode_id_only_logs_no_names(self, tmp_path, caplog):
        caplog.set_level(logging.INFO)
        c = _plain_conn()
        drive = _Drive(_meta())
        _ids, files, chunks, complete, sels = pci.run_folder_mode(
            c, drive, [DEDUP], tmp_path, expect_leaf="dedup-2026-09", sources=("drive_sweep",), names=False)
        assert complete and files == 2 and chunks == 2 and sels[0].kept == {"drive_asset": {"chunks": 1, "files": 1}}
        assert ARCH_NAME not in caplog.text and "dedup-2026-09 (" not in caplog.text and "_archive (" not in caplog.text
        with pytest.raises(RuntimeError) as ei:
            pci.run_folder_mode(c, drive, [DEDUP], tmp_path, expect_leaf="wrong", names=False)
        assert "dedup-2026-09" not in str(ei.value).split("--expect-leaf 'wrong'", 1)[1]


# ── deployment/runbook.md: the ONE stop-window section ───────────────────────
def test_runbook_stop_window_section_is_ordered_and_its_powershell_is_ascii():
    text = (_REPO / "deployment" / "runbook.md").read_text(encoding="utf-8")
    head = "## Code #15 combined KB purge (S1 + RIDER B)"
    assert text.count(head) == 1
    section = text.split(head, 1)[1].split("\n## ", 1)[0]
    blocks = [b.split("```", 1)[0] for b in section.split("```powershell")[1:]]
    assert len(blocks) == 2 and all(b.isascii() for b in blocks)        # D-016: pasted PS stays ASCII
    window = blocks[1]
    steps = ['Disable-ScheduledTask -TaskName "cowork-cora-service"', "Start-Sleep 310",
             r"Copy-Item data\cora_kb.db", "purge_kb_code15_2026-09.py --apply --manifest",
             "reclaim_kb_space.py", 'Enable-ScheduledTask -TaskName "cowork-cora-service"',
             "restart-cora.ps1", 'Enable-ScheduledTask -TaskName "cora-watchdog"',
             "cora-instances.jsonl", "egress-rails-armed.json"]
    at = [window.index(s) for s in steps]
    assert at == sorted(at), dict(zip(steps, at))
    assert "2026-09-10T08:45:06" in window and "--csv" in window and "--csv" in blocks[0]
