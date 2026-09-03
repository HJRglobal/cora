"""WS1-DRIVE: purge_cora_internal_kb must also catch Cora build/audit docs ingested
as Drive COPIES (drive_sweep/drive_asset), whose source_id is a Drive file id and
whose filename lives in `title`. The static_md-only purge missed these entirely.

Logic is tested against in-memory plain tables; the SQL is generic over chunk_id,
so no sqlite-vec extension is needed.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import purge_cora_internal_kb as purge  # noqa: E402


def _conn():
    c = sqlite3.connect(":memory:")
    c.execute(
        "CREATE TABLE knowledge_chunks "
        "(chunk_id TEXT PRIMARY KEY, source TEXT, source_id TEXT, title TEXT)"
    )
    c.execute("CREATE TABLE knowledge_vec_bin (chunk_id TEXT)")
    c.execute("CREATE TABLE knowledge_vec_f32 (chunk_id TEXT)")
    return c


def _seed(c):
    rows = [
        # drive_sweep copies -- source_id is a Drive file id, filename in title
        ("d1", "drive_sweep", "1ITRLIX_fileid", "2026-06-16_fndr_cora-rebuild-execution-log.md"),  # TARGETED
        ("d2", "drive_sweep", "1F6FO_fileid", "2026-06-16_fndr_cora-forensic-findings-report.md"),  # TARGETED
        ("d3", "drive_sweep", "1GZ_fileid", "cora-2026-06-06.log"),                                  # TARGETED (log)
        ("d4", "drive_asset", "1aX_fileid", "2026-06-16_fndr_cora-redesign-overhaul-proposal.md"),   # BROAD only
        ("d5", "drive_sweep", "1AX_fileid", "f3-brand-assets-cora-reference.md"),                     # LEGIT -> keep
        ("d6", "drive_sweep", "1zG_fileid", "2026-05-23_lex_cora-wishlist.md"),                       # LEGIT -> keep
        ("d7", "drive_sweep", "1cl_fileid", "02-F3-Energy CLAUDE.md"),                                # LEGIT -> keep
        # a drive_sweep row that happens to carry a real path source_id -> path rule
        ("d8", "drive_sweep", "_shared/projects/cora/design/x.md", "x.md"),                           # path -> purge
        # static_md (matched by source_id path, unchanged behavior)
        ("s1", "static_md", "_shared/projects/cora/CLAUDE.md", "CLAUDE.md"),                          # static path -> purge
        ("s2", "static_md", "02-F3-Energy/CLAUDE.md", "CLAUDE.md"),                                   # legit -> keep
        # other sources must never be scanned by the drive-copy pass
        ("g1", "gmail", "gmail:a@x:1", "Cora mentioned you in #Cora"),                                # keep
        ("k1", "slack", "slack:C0:1", "#cora-build thread 2026-05-27"),                               # keep
    ]
    c.executemany("INSERT INTO knowledge_chunks VALUES (?,?,?,?)", rows)
    c.executemany("INSERT INTO knowledge_vec_bin VALUES (?)", [(r[0],) for r in rows])
    c.executemany("INSERT INTO knowledge_vec_f32 VALUES (?)", [(r[0],) for r in rows])
    c.commit()


def test_targeted_selects_build_docs_and_logs_only():
    c = _conn(); _seed(c)
    ids, names = purge.target_drive_doc_copies(c, broad=False)
    assert set(ids) == {"d1", "d2", "d3", "d8"}        # execution-log, forensic, .log, path
    assert "d4" not in ids                              # proposal is broad-only
    assert "d5" not in ids and "d6" not in ids and "d7" not in ids  # legit spared


def test_broad_adds_ops_docs_but_still_spares_legit():
    c = _conn(); _seed(c)
    ids, _ = purge.target_drive_doc_copies(c, broad=True)
    assert "d4" in ids                                  # redesign-overhaul-proposal now caught
    assert {"d1", "d2", "d3", "d8"} <= set(ids)
    for keep in ("d5", "d6", "d7"):
        assert keep not in ids, keep                    # legit docs still spared


def test_drive_pass_never_touches_gmail_or_slack():
    c = _conn(); _seed(c)
    ids, _ = purge.target_drive_doc_copies(c, broad=True)
    assert "g1" not in ids and "k1" not in ids          # only drive_sweep/drive_asset scanned


def test_static_md_pass_unchanged():
    c = _conn(); _seed(c)
    ids, _ = purge.target_static_md(c)
    assert set(ids) == {"s1"}                           # cora project path; legit F3E CLAUDE spared


def test_delete_chunks_removes_from_all_three_tables():
    c = _conn(); _seed(c)
    drive_ids, _ = purge.target_drive_doc_copies(c, broad=False)
    static_ids, _ = purge.target_static_md(c)
    to_delete = list(drive_ids) + list(static_ids)
    totals = purge.delete_chunks(c, to_delete)
    assert totals["knowledge_chunks"] == len(to_delete)
    assert totals["knowledge_vec_bin"] == len(to_delete)
    assert totals["knowledge_vec_f32"] == len(to_delete)
    # legit rows survive
    remaining = {r[0] for r in c.execute("SELECT chunk_id FROM knowledge_chunks")}
    assert {"d5", "d6", "d7", "s2", "g1", "k1"} <= remaining
    assert not ({"d1", "d2", "d3", "d8", "s1"} & remaining)


# ═══════════════════════════════════════════════════════════════════════════════
# FOLDER MODE (2026-09-03, cq-11e9abda254a "D-057 IS LEAKING"): the title
# selector cannot reach the >=157 token-less files under _shared/projects/cora,
# so --folder-id enumerates the folder's descendant file ids (read-only BFS) and
# selects the Drive-copy chunks keyed on them. Tested against an in-memory Drive
# tree + plain tables; no network, no sqlite-vec.
# ═══════════════════════════════════════════════════════════════════════════════
import pytest  # noqa: E402

_FOLDER = "application/vnd.google-apps.folder"


class _FakeDrive:
    """files().list / files().get over an in-memory tree.

    children: {folder_id: [(id, name, mime), ...]}; meta: {id: (name, mime, parent)}.
    ``fail_on`` folder ids raise on listing (simulates an API error mid-walk);
    ``page_split`` folders return their children over two pages."""

    def __init__(self, children, meta, *, fail_on=frozenset(), page_split=frozenset()):
        self.children, self.meta = children, meta
        self.fail_on, self.page_split = set(fail_on), set(page_split)
        self.parents_listed: list[str] = []

    def files(self):  # noqa: A003
        return self

    def list(self, **kw):
        import re
        m = re.search(r"'([^']+)' in parents", kw.get("q", ""))
        parent = m.group(1) if m else None
        self.parents_listed.append(parent)
        outer = self

        class _Req:
            def execute(_self):
                if parent in outer.fail_on:
                    raise RuntimeError(f"boom listing {parent}")
                kids = [{"id": i, "name": n, "mimeType": mm} for i, n, mm in outer.children.get(parent, [])]
                if parent in outer.page_split and len(kids) > 1:
                    if "pageToken" not in kw:
                        return {"files": kids[:1], "nextPageToken": "p2"}
                    return {"files": kids[1:], "nextPageToken": None}
                return {"files": kids, "nextPageToken": None}

        return _Req()

    def get(self, fileId, fields=None):  # noqa: N803
        outer = self

        class _Req:
            def execute(_self):
                if fileId not in outer.meta:
                    raise RuntimeError(f"404 {fileId}")
                name, mime, parent = outer.meta[fileId]
                d = {"id": fileId, "name": name, "mimeType": mime}
                if parent:
                    d["parents"] = [parent]
                return d

        return _Req()


def _tree():
    """ROOT/_shared/projects/cora{f1,f2, design/{f3}, _archive/{f4}, _notes/{}} + a sibling
    project folder 'other' holding f9 (must never be selected)."""
    children = {
        "ROOT": [("SHARED", "_shared", _FOLDER)],
        "SHARED": [("PROJ", "projects", _FOLDER)],
        "PROJ": [("CORA", "cora", _FOLDER), ("OTHER", "other", _FOLDER)],
        "CORA": [("f1", "2026-09-01_cora_constitution-charter-v1.md", "text/markdown"),
                 ("f2", "2026-08-30_fndr_code-12-queue-metabolism-scope-draft-v0.9.md", "text/markdown"),
                 ("DESIGN", "design", _FOLDER), ("ARCH", "_archive", _FOLDER), ("NOTES", "_notes", _FOLDER)],
        "DESIGN": [("f3", "2026-09-02_cora_claude-workspace-mirror-design.md", "text/markdown")],
        "ARCH": [("f4", "old-roadmap.md", "text/markdown")],
        "NOTES": [],
        "OTHER": [("f9", "gmail-deep-dive-plan.md", "text/markdown")],
    }
    meta = {
        "ROOT": ("HJR-Founder-OS", _FOLDER, None),
        "SHARED": ("_shared", _FOLDER, "ROOT"),
        "PROJ": ("projects", _FOLDER, "SHARED"),
        "CORA": ("cora", _FOLDER, "PROJ"),
        "OTHER": ("other", _FOLDER, "PROJ"),
        "DESIGN": ("design", _FOLDER, "CORA"),
        "ARCH": ("_archive", _FOLDER, "CORA"),
        "NOTES": ("_notes", _FOLDER, "CORA"),
        "f1": ("2026-09-01_cora_constitution-charter-v1.md", "text/markdown", "CORA"),
    }
    return children, meta


def _seed_folder_rows(c):
    rows = [
        # Drive copies under the folder. c1 carries a cora_ token but no build keyword and
        # c2/c4 carry no cora token at all -> the title rule cannot see them (the leak
        # class). c3 IS title-visible (cora_ + the `mirror` keyword) -> the overlap case.
        ("c1", "drive_sweep", "f1", "2026-09-01_cora_constitution-charter-v1.md"),
        ("c2", "drive_sweep", "f2", "2026-08-30_fndr_code-12-queue-metabolism-scope-draft-v0.9.md"),
        ("c2b", "drive_sweep", "f2", "2026-08-30_fndr_code-12-queue-metabolism-scope-draft-v0.9.md"),  # 2nd chunk, same file
        ("c3", "drive_asset", "f3", "2026-09-02_cora_claude-workspace-mirror-design.md"),
        ("c4", "drive_sweep", "f4:chunk2", "old-roadmap.md"),                     # legacy suffixed source_id, MOVED into _archive
        ("c5", "drive_sweep", "1ITRLIX_fileid", "2026-06-16_fndr_cora-rebuild-execution-log.md"),  # title-only hit, outside folder
        # must NEVER be selected by the folder pass:
        ("x1", "drive_sweep", "f9", "gmail-deep-dive-plan.md"),                  # sibling project folder
        ("x2", "static_md", "f1", "not-a-drive-copy.md"),                        # wrong source, same id
        ("x3", "gmail", "f1", "Cora mentioned"),                                 # wrong source
        ("x4", "drive_sweep", "f1x", "lookalike-id.md"),                         # id is not an exact match
        ("x5", "drive_sweep", "CORA", "a-row-whose-source-id-is-the-folder-id.md"),  # folder ids never enter the file set
        ("x6", "drive_asset", "DESIGN", "subfolder-id-as-source-id.md"),
    ]
    c.executemany("INSERT INTO knowledge_chunks VALUES (?,?,?,?)", rows)
    c.executemany("INSERT INTO knowledge_vec_bin VALUES (?)", [(r[0],) for r in rows])
    c.executemany("INSERT INTO knowledge_vec_f32 VALUES (?)", [(r[0],) for r in rows])
    c.commit()


def test_folder_enumeration_walks_the_whole_subtree_including_archive():
    ch, meta = _tree()
    svc = _FakeDrive(ch, meta)
    names, complete = purge.enumerate_folder_files(svc, "CORA")
    assert complete is True
    assert set(names) == {"f1", "f2", "f3", "f4"}          # _archive/ is NOT name-skipped
    assert names["f4"] == "old-roadmap.md"
    assert "f9" not in names                                # sibling folder never entered
    assert "OTHER" not in svc.parents_listed


def test_folder_enumeration_paginates():
    ch, meta = _tree()
    svc = _FakeDrive(ch, meta, page_split={"CORA"})
    names, complete = purge.enumerate_folder_files(svc, "CORA")
    assert complete and set(names) == {"f1", "f2", "f3", "f4"}


def test_folder_enumeration_api_error_marks_incomplete_not_raise():
    ch, meta = _tree()
    svc = _FakeDrive(ch, meta, fail_on={"DESIGN"})
    names, complete = purge.enumerate_folder_files(svc, "CORA")
    assert complete is False                                # a floor, never the whole leak
    assert "f3" not in names and {"f1", "f2", "f4"} <= set(names)


def test_folder_enumeration_cap_marks_incomplete():
    ch, meta = _tree()
    names, complete = purge.enumerate_folder_files(_FakeDrive(ch, meta), "CORA", max_folders=2)
    assert complete is False
    assert set(names) == {"f1", "f2", "f3"}                 # CORA + DESIGN visited before the cap; a FLOOR


def test_folder_selector_keys_on_descendant_file_ids_only():
    c = _conn(); _seed_folder_rows(c)
    names = {"f1": "a", "f2": "b", "f3": "c", "f4": "d"}
    ids, hits = purge.target_folder_descendants(c, names)
    assert set(ids) == {"c1", "c2", "c2b", "c3", "c4"}
    assert hits["f2"] == ("b", 2)                            # two chunks, one file
    assert hits["f4"] == ("d", 1)                            # <id>:chunkN legacy form matched
    for keep in ("c5", "x1", "x2", "x3", "x4", "x5", "x6"):
        assert keep not in ids, keep                         # title-only / other source / lookalike / folder-id untouched
    assert purge.target_folder_descendants(c, {}) == ([], {})


def test_folder_and_title_selections_union_without_double_counting():
    c = _conn(); _seed(c); _seed_folder_rows(c)
    title_ids, _ = purge.target_drive_doc_copies(c, broad=True)
    folder_ids, _ = purge.target_folder_descendants(
        c, {"f1": "", "f2": "", "f3": "", "f4": "", "1ITRLIX_fileid": ""})
    # c3 (mirror design doc: cora_ token + `mirror` keyword), c5 (the execution log)
    # and _seed's d1 (same 1ITRLIX_fileid source id as c5 -- two chunks of one file)
    # are selected by BOTH passes -- the overlap the production union must fold.
    assert {"c3", "c5", "d1"} == set(title_ids) & set(folder_ids)
    selected, overlap = purge.select_for_delete([], title_ids, folder_ids, [])
    assert overlap == 3                                      # CHUNKS in >1 pass, measured
    assert len(selected) == len(set(selected)) == len(set(title_ids) | set(folder_ids))
    assert selected.count("c3") == 1 and selected.count("c5") == 1
    # first-seen order kept; a chunk in three passes still counts once and overlaps once
    sel2, ov2 = purge.select_for_delete(["a", "b"], ["b", "c"], ["a", "c", "d"], ["d"])
    assert sel2 == ["a", "b", "c", "d"] and ov2 == 4
    assert purge.select_for_delete([], [], [], []) == ([], 0)


def test_folder_apply_gate_refuses_only_an_incomplete_folder_enumeration():
    # MED-2 (tests lens): the destructive branch must have test execution.
    msg = purge.folder_apply_gate(True, False)
    assert msg and "REFUSING --apply" in msg and "floor" in msg
    assert purge.folder_apply_gate(True, True) is None       # complete -> proceed
    assert purge.folder_apply_gate(False, False) is None     # no folder mode -> the flag is moot


def test_resolve_folder_chain_walks_to_root_and_refuses_non_folders():
    ch, meta = _tree()
    svc = _FakeDrive(ch, meta)
    chain = purge.resolve_folder_chain(svc, "CORA")
    assert chain == [("cora", "CORA"), ("projects", "PROJ"), ("_shared", "SHARED"), ("HJR-Founder-OS", "ROOT")]
    assert purge.format_chain(chain).startswith("cora (CORA) <- projects (PROJ)")
    with pytest.raises(RuntimeError):
        purge.resolve_folder_chain(svc, "f1")                # a FILE id must never be walked as a subtree
    with pytest.raises(RuntimeError, match="404 nope"):
        purge.resolve_folder_chain(svc, "nope")              # the API error PROPAGATES (no catch in production)


def test_run_folder_mode_refuses_the_founders_os_root(tmp_path):
    c = _conn(); _seed_folder_rows(c)
    ch, meta = _tree()
    with pytest.raises(RuntimeError, match="ROOT"):
        purge.run_folder_mode(c, _FakeDrive(ch, meta), [purge._FOUNDERS_OS_ROOT_ID], tmp_path)


def test_root_id_constant_matches_drive_sweep():
    from cora.connectors import drive_sweep as ds
    assert purge._FOUNDERS_OS_ROOT_ID == ds.FOUNDERS_OS_ROOT_ID
    assert purge._GOOGLE_FOLDER_MIME == ds._GOOGLE_FOLDER_MIME


def test_run_folder_mode_writes_manifest_with_chain_and_rows(tmp_path):
    c = _conn(); _seed_folder_rows(c)
    ch, meta = _tree()
    ids, files_hit, chunks_hit, complete, sels = purge.run_folder_mode(c, _FakeDrive(ch, meta), ["CORA"], tmp_path)
    assert set(ids) == {"c1", "c2", "c2b", "c3", "c4"} and files_hit == 4 and chunks_hit == 5 and complete
    assert len(sels) == 1 and sels[0].folder_id == "CORA" and set(sels[0].chunk_ids) == set(ids)
    manifest = tmp_path / "purge-cora-internal-folder-CORA.txt"
    text = manifest.read_text(encoding="utf-8")
    assert "folder_id=CORA" in text
    assert "cora (CORA) <- projects (PROJ) <- _shared (SHARED) <- HJR-Founder-OS (ROOT)" in text
    assert "enumeration complete: True" in text
    assert "descendant files enumerated: 4" in text
    rows = [l for l in text.splitlines() if l.startswith("  ")]
    assert len(rows) == 4
    assert any(l == "  old-roadmap.md\tf4\t1" for l in rows)
    assert any(l.endswith("\tf2\t2") for l in rows)          # name, id, chunk count per row
    assert "f9" not in text and "1ITRLIX_fileid" not in text


def test_run_folder_mode_reports_incomplete(tmp_path):
    c = _conn(); _seed_folder_rows(c)
    ch, meta = _tree()
    ids, _, _, complete, _sels = purge.run_folder_mode(c, _FakeDrive(ch, meta, fail_on={"DESIGN"}), ["CORA"], tmp_path)
    assert complete is False and "c3" not in ids
    assert "enumeration complete: False" in (tmp_path / "purge-cora-internal-folder-CORA.txt").read_text(encoding="utf-8")


def test_delete_of_the_unioned_selection_spares_the_rest():
    c = _conn(); _seed_folder_rows(c)
    title_ids, _ = purge.target_drive_doc_copies(c, broad=True)                # c3 + c5
    folder_ids, _ = purge.target_folder_descendants(c, {"f1": "", "f2": "", "f3": "", "f4": ""})
    selected, overlap = purge.select_for_delete(title_ids, folder_ids)
    assert overlap == 1 and set(selected) == {"c1", "c2", "c2b", "c3", "c4", "c5"}
    totals = purge.delete_chunks(c, selected)
    assert totals["knowledge_chunks"] == 6
    remaining = {r[0] for r in c.execute("SELECT chunk_id FROM knowledge_chunks")}
    assert {"x1", "x2", "x3", "x4", "x5", "x6"} <= remaining
    assert not ({"c1", "c2", "c2b", "c3", "c4", "c5"} & remaining)


def test_root_refusal_survives_pasted_whitespace(tmp_path):
    c = _conn(); ch, meta = _tree()
    with pytest.raises(RuntimeError, match="ROOT"):
        purge.run_folder_mode(c, _FakeDrive(ch, meta), [" " + purge._FOUNDERS_OS_ROOT_ID + "\n"], tmp_path)


# ── D-051 remediation (purge lens 2026-09-03): the --apply gates ──────────────
def test_folder_ids_are_stripped_and_manifest_named_by_the_clean_id(tmp_path):
    c = _conn(); _seed_folder_rows(c)
    ch, meta = _tree()
    ids, files_hit, chunks_hit, complete, _sels = purge.run_folder_mode(c, _FakeDrive(ch, meta), ["  CORA\n"], tmp_path)
    assert (tmp_path / "purge-cora-internal-folder-CORA.txt").exists()
    assert files_hit == 4 and chunks_hit == 5


def test_nested_folder_ids_do_not_double_count(tmp_path):
    c = _conn(); _seed_folder_rows(c)
    ch, meta = _tree()
    ids, files_hit, chunks_hit, _, sels = purge.run_folder_mode(c, _FakeDrive(ch, meta), ["CORA", "DESIGN"], tmp_path)
    assert [x.folder_id for x in sels] == ["CORA", "DESIGN"]
    assert len(ids) == len(set(ids)) == 5 and files_hit == 4 and chunks_hit == 5


def test_root_and_top_level_chains_are_refused_in_every_mode(tmp_path):
    c = _conn(); _seed_folder_rows(c)
    ch, meta = _tree()
    with pytest.raises(RuntimeError, match="root/top-level"):
        purge.run_folder_mode(c, _FakeDrive(ch, meta), ["SHARED"], tmp_path)   # _shared <- ROOT = depth 2
    # depth 3 (projects) is allowed to enumerate on a dry-run; the eyeball sees the chain
    ids, *_ = purge.run_folder_mode(c, _FakeDrive(ch, meta), ["PROJ"], tmp_path)
    assert "x1" in ids                                                          # it reaches the sibling project too


def test_apply_requires_expect_leaf_and_it_must_match(tmp_path):
    c = _conn(); _seed_folder_rows(c)
    ch, meta = _tree()
    svc = _FakeDrive(ch, meta)
    with pytest.raises(RuntimeError, match="requires --expect-leaf"):
        purge.run_folder_mode(c, svc, ["CORA"], tmp_path, apply=True)
    with pytest.raises(RuntimeError, match="does not match"):
        purge.run_folder_mode(c, svc, ["CORA"], tmp_path, expect_leaf="projects")       # validated on dry-run too
    with pytest.raises(RuntimeError, match="does not match"):
        purge.run_folder_mode(c, svc, ["PROJ"], tmp_path, apply=True, expect_leaf="cora")  # a pasted sibling id


def test_apply_refuses_without_a_reviewed_dry_run_manifest(tmp_path):
    c = _conn(); _seed_folder_rows(c)
    ch, meta = _tree()
    with pytest.raises(RuntimeError, match="no reviewed dry-run manifest"):
        purge.run_folder_mode(c, _FakeDrive(ch, meta), ["CORA"], tmp_path, apply=True, expect_leaf="cora")
    assert not list(tmp_path.glob("*.applied-*"))


def test_apply_pins_to_the_reviewed_set_and_writes_nothing_itself(tmp_path):
    c = _conn(); _seed_folder_rows(c)
    ch, meta = _tree()
    svc = _FakeDrive(ch, meta)
    # 1. dry-run writes the reviewed manifest
    purge.run_folder_mode(c, svc, ["CORA"], tmp_path)
    reviewed = tmp_path / "purge-cora-internal-folder-CORA.txt"
    before = reviewed.read_text(encoding="utf-8")
    assert purge.parse_manifest_file_ids(reviewed) == {"f1", "f2", "f3", "f4"}
    # 2. apply with an unchanged set passes every gate, writes NOTHING (records come
    #    after the delete), leaves the reviewed file intact
    ids, _, _, complete, sels = purge.run_folder_mode(c, svc, ["CORA"], tmp_path, apply=True, expect_leaf="cora")
    assert set(ids) == {"c1", "c2", "c2b", "c3", "c4"} and complete and len(sels) == 1
    assert sorted(x.name for x in tmp_path.iterdir()) == ["purge-cora-internal-folder-CORA.txt"]
    assert reviewed.read_text(encoding="utf-8") == before
    # 3. a file that appeared since the eyeball is REFUSED by name ...
    ch["NOTES"].append(("f5", "landed-after-the-eyeball.md", "text/markdown"))
    c.execute("INSERT INTO knowledge_chunks VALUES ('c6','drive_sweep','f5','landed-after-the-eyeball.md')"); c.commit()
    with pytest.raises(RuntimeError, match="landed-after-the-eyeball.md"):
        purge.run_folder_mode(c, svc, ["CORA"], tmp_path, apply=True, expect_leaf="cora")
    # ... unless the operator accepts the delta explicitly
    ids, files_hit, chunks_hit, _, _ = purge.run_folder_mode(
        c, svc, ["CORA"], tmp_path, apply=True, expect_leaf="cora", accept_delta=True)
    assert "c6" in ids and files_hit == 5 and chunks_hit == 6


def test_apply_takes_exactly_one_folder(tmp_path):
    c = _conn(); _seed_folder_rows(c)
    ch, meta = _tree()
    svc = _FakeDrive(ch, meta)
    purge.run_folder_mode(c, svc, ["CORA", "DESIGN"], tmp_path)              # dry-run may take several
    with pytest.raises(RuntimeError, match="exactly ONE"):
        purge.run_folder_mode(c, svc, ["CORA", "DESIGN"], tmp_path, apply=True, expect_leaf="cora")
    assert not list(tmp_path.glob("*.applied-*"))


def test_apply_with_incomplete_enumeration_raises_before_anything_is_written(tmp_path):
    c = _conn(); _seed_folder_rows(c)
    ch, meta = _tree()
    purge.run_folder_mode(c, _FakeDrive(ch, meta), ["CORA"], tmp_path)         # reviewed manifest exists
    with pytest.raises(RuntimeError, match="did not complete"):
        purge.run_folder_mode(c, _FakeDrive(ch, meta, fail_on={"DESIGN"}), ["CORA"], tmp_path,
                              apply=True, expect_leaf="cora")
    assert sorted(x.name for x in tmp_path.iterdir()) == ["purge-cora-internal-folder-CORA.txt"]


def test_applied_records_are_written_after_the_delete_with_totals(tmp_path):
    c = _conn(); _seed_folder_rows(c)
    ch, meta = _tree()
    svc = _FakeDrive(ch, meta)
    purge.run_folder_mode(c, svc, ["CORA"], tmp_path)
    ids, _, _, _, sels = purge.run_folder_mode(c, svc, ["CORA"], tmp_path, apply=True, expect_leaf="cora")
    totals = purge.delete_chunks(c, ids)
    paths = purge.write_applied_records(sels, totals, tmp_path, "20260903T120000000000Z")
    assert [x.name for x in paths] == ["purge-cora-internal-folder-CORA.applied-20260903T120000000000Z.txt"]
    text = paths[0].read_text(encoding="utf-8")
    assert "files with KB chunks: 4" in text and text.rstrip().splitlines()[-1].startswith("# DELETED ")
    assert "'knowledge_chunks': 5" in text
    assert purge.parse_manifest_file_ids(paths[0]) == {"f1", "f2", "f3", "f4"}   # the footer is not a row
    # the reviewed manifest is untouched by all of this
    assert (tmp_path / "purge-cora-internal-folder-CORA.txt").exists()


def test_pre_delete_intent_dump_failure_raises(tmp_path):
    # The intent dump is the gate before delete_chunks in main(): if it cannot be
    # written the exception propagates and nothing is deleted.
    c = _conn(); _seed_folder_rows(c)
    ids, _ = purge.target_folder_descendants(c, {"f1": "", "f2": "", "f3": "", "f4": ""})
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(OSError):
        purge.dump_selected_rows(c, ids, blocker / "selected.json")
    assert c.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0] > 0


def test_applied_record_write_failure_propagates_to_the_caller(tmp_path, monkeypatch):
    c = _conn(); _seed_folder_rows(c)
    ch, meta = _tree()
    svc = _FakeDrive(ch, meta)
    purge.run_folder_mode(c, svc, ["CORA"], tmp_path)
    _, _, _, _, sels = purge.run_folder_mode(c, svc, ["CORA"], tmp_path, apply=True, expect_leaf="cora")
    monkeypatch.setattr(purge, "write_folder_manifest", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        purge.write_applied_records(sels, {"knowledge_chunks": 5}, tmp_path, "T")


def test_manifest_rows_stay_one_line_for_a_multiline_drive_name(tmp_path):
    m = tmp_path / "m.txt"
    purge.write_folder_manifest(m, folder_id="X", chain=[("x", "X"), ("y", "Y"), ("z", "Z")], complete=True,
                                n_descendants=2, hits={"idA": ("two\nline\r\nname.md", 1), "idB": ("plain.md", 2)})
    rows = [l for l in m.read_text(encoding="utf-8").splitlines() if l.startswith("  ")]
    assert len(rows) == 2
    assert purge.parse_manifest_file_ids(m) == {"idA", "idB"}


def test_expect_leaf_compare_tolerates_drive_side_whitespace_and_case(tmp_path):
    c = _conn(); _seed_folder_rows(c)
    ch, meta = _tree()
    meta["CORA"] = ("Cora ", _FOLDER, "PROJ")                    # a trailing space in the Drive name
    ids, *_ = purge.run_folder_mode(c, _FakeDrive(ch, meta), ["CORA"], tmp_path, expect_leaf="cora")
    assert "c1" in ids


def test_incomplete_search_flag_marks_enumeration_incomplete():
    ch, meta = _tree()

    class _Flagging(_FakeDrive):
        def list(self, **kw):
            req = super().list(**kw)
            inner = req.execute

            def execute():
                out = inner()
                out["incompleteSearch"] = True
                return out
            req.execute = execute
            return req

    names, complete = purge.enumerate_folder_files(_Flagging(ch, meta), "CORA")
    assert complete is False and set(names) == {"f1", "f2", "f3", "f4"}


def test_parse_manifest_file_ids_survives_a_tab_in_a_drive_name(tmp_path):
    m = tmp_path / "m.txt"
    purge.write_folder_manifest(m, folder_id="X", chain=[("x", "X"), ("y", "Y"), ("z", "Z")], complete=True,
                                n_descendants=2, hits={"idA": ("weird\tname.md", 1), "idB": ("plain.md", 2)})
    assert purge.parse_manifest_file_ids(m) == {"idA", "idB"}


def test_dump_selected_rows_records_every_selected_chunk(tmp_path):
    import json
    c = _conn(); _seed_folder_rows(c)
    ids, _ = purge.target_folder_descendants(c, {"f1": "", "f2": "", "f3": "", "f4": ""})
    out = tmp_path / "applied.json"
    n = purge.dump_selected_rows(c, ids, out)
    data = json.loads(out.read_text(encoding="utf-8"))
    assert n == data["count"] == 5
    assert {r["chunk_id"] for r in data["rows"]} == set(ids)
    assert all(set(r) == {"chunk_id", "source", "source_id", "title"} for r in data["rows"])


def test_allowlisted_basename_is_flagged_in_the_manifest_not_exempted(tmp_path):
    # Spec lens MED-1 (2026-09-03): the folder selector deliberately deletes the
    # drive_sweep twin of the allowlisted code-session-backlog.md (post-pin that
    # copy can never refresh; the static_md copy is allowlist-honoured and stays).
    # The choice is FLAGGED so the eyeball sees it.
    c = _conn(); _seed_folder_rows(c)
    c.execute("INSERT INTO knowledge_chunks VALUES ('c7','drive_sweep','f7','code-session-backlog.md')")
    c.execute("INSERT INTO knowledge_chunks VALUES ('s9','static_md','_shared/projects/cora/code-session-backlog.md','code-session-backlog.md')")
    c.commit()
    ch, meta = _tree()
    ch["CORA"].append(("f7", "code-session-backlog.md", "text/markdown"))
    ids, files_hit, chunks_hit, _, sels = purge.run_folder_mode(c, _FakeDrive(ch, meta), ["CORA"], tmp_path)
    assert "c7" in ids and "s9" not in ids                       # the drive_sweep twin only
    assert sels[0].allowlisted == ["code-session-backlog.md"]
    text = (tmp_path / "purge-cora-internal-folder-CORA.txt").read_text(encoding="utf-8")
    assert "# allowlisted basenames selected (drive_sweep twin only; the static_md copy stays): code-session-backlog.md" in text
    # and the static_md pass still spares the allowlisted view (unchanged contract)
    static_ids, _ = purge.target_static_md(c)
    assert "s9" not in static_ids


def test_manifest_header_says_none_when_nothing_allowlisted(tmp_path):
    c = _conn(); _seed_folder_rows(c)
    ch, meta = _tree()
    purge.run_folder_mode(c, _FakeDrive(ch, meta), ["CORA"], tmp_path)
    text = (tmp_path / "purge-cora-internal-folder-CORA.txt").read_text(encoding="utf-8")
    assert "allowlisted basenames selected (drive_sweep twin only; the static_md copy stays): none" in text
