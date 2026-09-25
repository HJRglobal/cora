"""Code #15 RIDER B items 6+7 (cq-59c5048d0891): the holds-manifest proposer.

scripts/propose_rider_b_holds_manifest.py turns the HOLD-partition + HOLD-lane rows
of the folder-audit holds file into an ARCHIVE-only manifest that the Drive
apply.ps1 will accept, deciding each sha256 group AS A WHOLE. Every test builds a
fake Founder-OS root in tmp_path; nothing here touches G: or Downloads.
"""

from __future__ import annotations

import builtins
import csv
import hashlib
import io
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import propose_rider_b_holds_manifest as prop  # noqa: E402
from cora import drive_io, kb_exclusions  # noqa: E402
from cora import hygiene_drive_report as hdr  # noqa: E402

# The header line of manifest-dedup-2026-09.csv, byte for byte (read 2026-09-24).
DEDUP_HEADER = (b"\xef\xbb\xbfaction,source_relpath,dest_relpath,reason_code,sha256,bytes,"
                b"keep_relpath,is_md,top_level\n")


# ── fixture builder ──────────────────────────────────────────────────────────

class Case:
    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.root = tmp_path / "root"
        self.root.mkdir(parents=True)
        self.rows: list[dict] = []
        self.n = 0
        self.absent: set[str] = set()

    def path(self, rel: str) -> Path:
        return self.root.joinpath(*rel.split("\\"))

    def group(self, members: list[str], keep: str, *, reason: str = "HOLD-partition",
              absent: tuple[str, ...] = (), content: bytes | None = None) -> str:
        self.n += 1
        content = content if content is not None else f"content-{self.n}".encode()
        sha = hashlib.sha256(content).hexdigest().upper()
        for m in dict.fromkeys(members + [keep]):
            if m in absent:
                self.absent.add(m)
                continue
            p = self.path(m)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)
        for src in members:
            if src == keep:
                continue
            self.rows.append({
                "action": "HOLD", "source_relpath": src, "dest_relpath": "", "reason_code": reason,
                "sha256": sha, "bytes": str(len(content)), "keep_relpath": keep,
                "is_md": str(src.lower().endswith(".md")), "top_level": src.split("\\")[0],
            })
        return sha

    def write_inputs(self, *, baseline_overrides: dict[str, tuple[int, str]] | None = None,
                     baseline_omit: tuple[str, ...] = ()) -> dict[str, Path]:
        holds = self.tmp / "holds.csv"
        holds.write_bytes(hdr.render_manifest_csv(self.rows))
        snap = self.tmp / "snapshot.csv"
        snap.write_bytes(holds.read_bytes())
        ref = self.tmp / "manifest-dedup-ref.csv"
        ref.write_bytes(DEDUP_HEADER + b"DELETE,x\\desktop.ini,,D3-desktopini,,1,,False,x\n")
        base = self.tmp / "baseline.csv"
        buf = io.StringIO(newline="")
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["RelPath", "Bytes", "ModifiedUtc"])
        for p in self.root.rglob("*"):
            if not p.is_file():
                continue
            rel = "\\".join(p.relative_to(self.root).parts)
            if rel in baseline_omit:
                continue
            st = p.stat()
            val = (st.st_size, prop._utc_second(st.st_mtime))
            if baseline_overrides and rel in baseline_overrides:
                val = baseline_overrides[rel]
            w.writerow([rel, val[0], val[1]])
        base.write_bytes(b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8"))
        out = self.tmp / "out"
        out.mkdir(exist_ok=True)
        return {"holds": holds, "snapshot": snap, "ref": ref, "baseline": base, "out": out}

    def argv(self, paths: dict[str, Path], *extra: str, write: bool = False) -> list[str]:
        a = ["--holds", str(paths["holds"]), "--snapshot", str(paths["snapshot"]),
             "--root", str(self.root), "--baseline-inventory", str(paths["baseline"]),
             "--schema-ref", str(paths["ref"]), *extra]
        if write:
            a += ["--write", "--out-dir", str(paths["out"])]
        return a


def _read_manifest(p: Path) -> list[dict]:
    return hdr.parse_manifest_csv(p.read_bytes())


def _run(case: Case, *extra: str, write: bool = True, **kw) -> tuple[int, dict[str, Path]]:
    paths = case.write_inputs(**kw)
    rc = prop.main(case.argv(paths, *extra, write=write))
    return rc, paths


def _held_reasons(paths) -> dict[str, str]:
    return {r["source_relpath"]: r["reason_code"]
            for r in _read_manifest(paths["out"] / prop.OUT_HELD)}


def _archived(paths) -> dict[str, dict]:
    return {r["source_relpath"]: r for r in _read_manifest(paths["out"] / prop.OUT_ARCHIVE)}


ENT = "02-F3-Energy\\reports\\q3.pdf"
EA = "_shared\\email-attachments\\q3.pdf"
INV = "02-F3-Energy\\invoices\\inv-100.pdf"
RI_RECEIPTS = "01-HJR-Global\\accounting\\Receipts & Invoices Inbox\\2026-09-01_receipts_inv-100.pdf"
RI_ERIC = "01-HJR-Global\\accounting\\Receipts & Invoices Inbox\\2026-09-01_eric_inv-100.pdf"


# ── the apply.ps1 gate mirror + schema ──────────────────────────────────────

def test_archive_manifest_passes_the_apply_gates_and_matches_the_dedup_header(tmp_path):
    c = Case(tmp_path)
    c.group([ENT, EA], EA)                                              # EA keep flips to ENT
    c.group([INV, RI_RECEIPTS], RI_RECEIPTS)                            # RI archived against INV
    c.group(["08-Lexington-Services\\billing\\x.pdf", "02-F3-Energy\\b.pdf"], "02-F3-Energy\\b.pdf")
    c.group(["01-HJR-Global\\visibility-binder\\a.pdf", "01-HJR-Global\\a.pdf"], "01-HJR-Global\\a.pdf")
    rc, paths = _run(c)
    assert rc == 0
    data = (paths["out"] / prop.OUT_ARCHIVE).read_bytes()
    assert data.split(b"\n", 1)[0] + b"\n" == DEDUP_HEADER == hdr.MANIFEST_HEADER_BYTES
    assert b"\r" not in data
    rows = _read_manifest(paths["out"] / prop.OUT_ARCHIVE)
    assert rows, "the fixture must propose something or the gate test is vacuous"
    for r in rows:
        assert r["action"] == "ARCHIVE"                                  # gate 1: no HOLD/KEEP
        assert r["action"] != "DELETE"                                   # gate 3
        for col in hdr.MANIFEST_COLUMNS:                                 # gate 2
            assert not r[col].lower().startswith("08-"), col
        assert r["dest_relpath"] == "_archive\\dedup-2026-09\\" + r["source_relpath"]


def test_schema_reference_mismatch_refuses(tmp_path, capsys):
    c = Case(tmp_path)
    c.group([ENT, EA], EA)
    paths = c.write_inputs()
    paths["ref"].write_bytes(b"\xef\xbb\xbfaction,source_relpath,dest_relpath\n")
    assert prop.main(c.argv(paths, write=True)) == 3
    assert not any(paths["out"].iterdir())
    assert "header" in capsys.readouterr().out


# ── hold rules ───────────────────────────────────────────────────────────────

def test_lex_group_is_held_never_statted_and_never_written(tmp_path, monkeypatch):
    c = Case(tmp_path)
    lex = "08-Lexington-Services\\clients\\intake.pdf"
    c.group([lex, "02-F3-Energy\\intake.pdf"], "02-F3-Energy\\intake.pdf", reason="HOLD-lane")
    c.group([ENT, EA], EA)
    seen: list[str] = []
    real = drive_io.stat_info

    def spy(path, **kw):
        seen.append(str(path))
        return real(path, **kw)
    monkeypatch.setattr(drive_io, "stat_info", spy)
    rc, paths = _run(c)
    assert rc == 0
    assert not any("Lexington" in s for s in seen), "a LEX path must never be stat'd"
    for name in prop.OUT_NAMES:
        assert b"Lexington" not in (paths["out"] / name).read_bytes(), name
    assert "02-F3-Energy\\intake.pdf" not in _archived(paths)
    assert "(LEX rows counted, not written)" in (paths["out"] / prop.OUT_SUMMARY).read_text(encoding="utf-8")


def test_binder_group_is_held(tmp_path):
    c = Case(tmp_path)
    b = "02-F3-Energy\\visibility-binder\\contracts\\msa.pdf"
    c.group([EA, b], b)
    rc, paths = _run(c)
    assert rc == 0
    assert _held_reasons(paths) == {EA: "HOLD-rb-binder"}
    assert _archived(paths) == {}


def test_pinned_internal_and_personal_finances_straddle_are_held(tmp_path):
    c = Case(tmp_path)
    b1 = "_shared\\projects\\cora\\backups\\2026-09-01\\kb.db"
    b2 = "_shared\\projects\\cora\\backups\\2026-09-02\\kb.db"
    c.group([b1, b2], b1)
    pf = "00-Founder\\personal-finances\\2025\\statement.pdf"
    c.group(["00-Founder\\reports\\statement.pdf", pf], pf)
    rc, paths = _run(c)
    assert rc == 0
    held = _held_reasons(paths)
    assert held[b2] == "HOLD-rb-pinned-internal"
    assert held["00-Founder\\reports\\statement.pdf"] == "HOLD-rb-straddles-pinned"
    assert _archived(paths) == {}


def test_cross_entity_and_cross_entity_invoice_lane_are_held(tmp_path):
    c = Case(tmp_path)
    c.group(["02-F3-Energy\\reports\\x.pdf", "09-One-Stop-Nutrition\\reports\\x.pdf"],
            "02-F3-Energy\\reports\\x.pdf")
    c.group(["02-F3-Energy\\reports\\y.pdf", "09-One-Stop-Nutrition\\invoices\\y.pdf"],
            "02-F3-Energy\\reports\\y.pdf")
    rc, paths = _run(c)
    assert rc == 0
    held = _held_reasons(paths)
    assert held["09-One-Stop-Nutrition\\reports\\x.pdf"] == "HOLD-rb-cross-entity"
    assert held["09-One-Stop-Nutrition\\invoices\\y.pdf"] == "HOLD-rb-cross-entity-invoice-lane"
    assert _archived(paths) == {}


def test_not_in_ruling_lane_is_held(tmp_path):
    c = Case(tmp_path)
    c.group(["_shared\\meetings\\notes.pdf", ENT], ENT)
    rc, paths = _run(c)
    assert _held_reasons(paths) == {"_shared\\meetings\\notes.pdf": "HOLD-rb-not-in-ruling"}


def test_e7_old_items_guard_holds(tmp_path):
    c = Case(tmp_path)
    old = "05-HJR-Productions\\podcast\\ai-agent-hub\\Old Items\\clip.mp4"
    c.group([old, "05-HJR-Productions\\podcast\\clip.mp4"], old)
    rc, paths = _run(c, "--include-samepart")
    assert _held_reasons(paths) == {"05-HJR-Productions\\podcast\\clip.mp4": "HOLD-rb-e7-old-items"}


# ── keep choice ──────────────────────────────────────────────────────────────

def test_ea_keep_flips_to_the_entity_copy_and_the_old_keep_gets_an_archive_row(tmp_path):
    c = Case(tmp_path)
    sha = c.group([ENT], EA)            # today: ENT is the source row, EA the keep
    rc, paths = _run(c)
    assert rc == 0
    arch = _archived(paths)
    assert set(arch) == {EA}, "the old keep (no row today) is the one archived"
    row = arch[EA]
    assert row["keep_relpath"] == ENT
    assert row["reason_code"] == "RB6-lane-ea"
    assert row["sha256"] == sha and row["top_level"] == "_shared" and row["is_md"] == "False"


def test_ri_copy_archives_against_the_same_entity_invoice_copy(tmp_path):
    c = Case(tmp_path)
    c.group([INV], RI_RECEIPTS)
    rc, paths = _run(c)
    arch = _archived(paths)
    assert set(arch) == {RI_RECEIPTS}
    assert arch[RI_RECEIPTS]["keep_relpath"] == INV
    assert arch[RI_RECEIPTS]["reason_code"] == "RB6-lane-ri"


def test_ri_narrow_default_holds_non_receipts_copies_broad_archives_them(tmp_path):
    c = Case(tmp_path)
    c.group([RI_ERIC], INV)
    rc, paths = _run(c)
    assert rc == 0
    assert _held_reasons(paths) == {RI_ERIC: "HOLD-rb-ri-outside-narrow"}
    summary = (paths["out"] / prop.OUT_SUMMARY).read_text(encoding="utf-8")
    assert "| narrow (this proposal) | 0 | 0 | 0 | 0 |" in summary
    assert "| broad | 1 | 0 | 1 | 1 |" in summary          # both RI numbers, always

    c2 = Case(tmp_path / "broad")
    c2.group([RI_ERIC], INV)
    rc, paths2 = _run(c2, "--ri-scope", "broad")
    assert set(_archived(paths2)) == {RI_ERIC}


def test_same_partition_duplicates_hold_by_default_and_archive_with_the_flag(tmp_path):
    c = Case(tmp_path)
    a, b = "02-F3-Energy\\manufacturing\\spec.pdf", "02-F3-Energy\\manufacturing\\old\\spec.pdf"
    c.group([b], a)
    rc, paths = _run(c)
    assert _held_reasons(paths) == {b: "HOLD-rb-not-in-ruling-samepart"}

    c2 = Case(tmp_path / "flag")
    c2.group([b], a)
    rc, paths2 = _run(c2, "--include-samepart")
    arch = _archived(paths2)
    assert set(arch) == {b} and arch[b]["reason_code"] == "RB6-samepart" and arch[b]["keep_relpath"] == a


def test_keep_tie_break_prefers_the_unsuffixed_name(tmp_path):
    assert prop.pick_keep(["a\\x (1).pdf", "a\\x.pdf"], "zzz") == "a\\x.pdf"
    assert prop.pick_keep(["a\\x (1).pdf", "a\\x.pdf"], "a\\x (1).pdf") == "a\\x (1).pdf"


# ── re-verification against the disk ────────────────────────────────────────

def test_missing_member_is_listed_and_never_proposed(tmp_path):
    c = Case(tmp_path)
    c.group([ENT], EA, absent=(EA,))
    rc, paths = _run(c)
    assert rc == 0
    assert _archived(paths) == {}
    assert _held_reasons(paths) == {ENT: "HOLD-rb-member-missing"}
    misses = list(csv.DictReader(io.StringIO((paths["out"] / prop.OUT_MISSES).read_bytes().decode("utf-8-sig"))))
    assert [(m["role"], m["missing_relpath"]) for m in misses] == [("keep", EA)]


def test_a_member_changed_since_hashing_is_held(tmp_path):
    c = Case(tmp_path)
    c.group([ENT], EA)
    c.path(EA).write_bytes(b"a different length now")           # size drift
    rc, paths = _run(c)
    assert _held_reasons(paths) == {ENT: "HOLD-rb-changed-since-hash"}


def test_mtime_drift_and_absence_from_the_baseline_are_held(tmp_path):
    c = Case(tmp_path)
    c.group([ENT], EA)
    rc, paths = _run(c, baseline_overrides={EA: (len(b"content-1"), "2020-01-01T00:00:00Z")})
    assert _held_reasons(paths) == {ENT: "HOLD-rb-changed-since-hash"}

    c2 = Case(tmp_path / "omit")
    c2.group([ENT], EA)
    rc, paths2 = _run(c2, baseline_omit=(EA,))
    assert _held_reasons(paths2) == {ENT: "HOLD-rb-not-in-hash-baseline"}

    c3 = Case(tmp_path / "skip")
    c3.group([ENT], EA)
    rc, paths3 = _run(c3, "--skip-mtime-check", baseline_omit=(EA,))
    assert set(_archived(paths3)) == {EA}
    assert "mtime-check=OFF" in (paths3["out"] / prop.OUT_SUMMARY).read_text(encoding="utf-8")


def test_markdown_member_and_existing_dest_are_held(tmp_path):
    c = Case(tmp_path)
    c.group(["02-F3-Energy\\notes.md"], "_shared\\email-attachments\\notes.md")
    c.group([ENT], EA)
    dest = c.path("_archive\\dedup-2026-09\\" + EA)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"already there")
    rc, paths = _run(c)
    held = _held_reasons(paths)
    assert held["02-F3-Energy\\notes.md"] == "HOLD-rb-md-member"
    assert held[ENT] == "HOLD-rb-dest-exists"


def test_a_dest_over_max_path_under_the_apply_root_is_held(tmp_path):
    c = Case(tmp_path)
    c.group([ENT], EA)
    rc, paths = _run(c, "--apply-root", "G:\\" + "x" * 230)
    assert _held_reasons(paths) == {ENT: "HOLD-rb-dest-path-too-long"}
    c2 = Case(tmp_path / "default")
    c2.group([ENT], EA)
    rc, paths2 = _run(c2)             # measured against the real G: root, not the long tmp root
    assert set(_archived(paths2)) == {EA}


def test_empty_sha_rows_stay_held(tmp_path):
    c = Case(tmp_path)
    c.group([ENT], EA)
    c.rows.append({"action": "HOLD", "source_relpath": "02-F3-Energy\\nohash.pdf", "dest_relpath": "",
                   "reason_code": "HOLD-partition", "sha256": "", "bytes": "5",
                   "keep_relpath": "02-F3-Energy\\nohash2.pdf", "is_md": "False", "top_level": "02-F3-Energy"})
    rc, paths = _run(c)
    assert _held_reasons(paths)["02-F3-Energy\\nohash.pdf"] == "HOLD-rb-no-sha256"


def test_a_row_with_another_hold_reason_holds_its_group(tmp_path):
    c = Case(tmp_path)
    sha = c.group([ENT], EA)
    c.rows.append({**c.rows[0], "source_relpath": "02-F3-Energy\\reports\\q3 copy.pdf", "reason_code": "HOLD-dest"})
    c.path("02-F3-Energy\\reports\\q3 copy.pdf").write_bytes(b"content-1")
    rc, paths = _run(c, "--include-samepart")
    assert _held_reasons(paths) == {ENT: "HOLD-rb-other-hold-in-group"}


# ── group-level decisions ────────────────────────────────────────────────────

def test_every_archived_group_is_complete(tmp_path):
    c = Case(tmp_path)
    c.group([ENT, "02-F3-Energy\\invoices\\q3.pdf", RI_RECEIPTS], EA)
    rc, paths = _run(c)
    arch = _archived(paths)
    keeps = {r["keep_relpath"] for r in arch.values()}
    assert keeps == {ENT}
    assert set(arch) == {EA, "02-F3-Energy\\invoices\\q3.pdf", RI_RECEIPTS}


def test_gate_refuses_a_half_archived_group():
    g = prop.Group("S", [{"source_relpath": "a\\1", "keep_relpath": "a\\k", "reason_code": "HOLD-partition",
                          "bytes": "1", "is_md": "False", "top_level": "a"}], "a\\k", ["a\\1", "a\\2", "a\\k"],
                   decision="ARCHIVE", new_keep="a\\k", archived=[("a\\1", "ent")])
    res = prop.Result([g], [], 1, 1)
    with pytest.raises(prop.GateError, match="half-archived"):
        prop.check_gates(prop.archive_rows(res), [], res)


def test_gate_refuses_a_lex_row():
    row = {"action": "ARCHIVE", "source_relpath": "02-F3-Energy\\a.pdf",
           "dest_relpath": "_archive\\dedup-2026-09\\02-F3-Energy\\a.pdf", "reason_code": "RB6-samepart",
           "sha256": "S", "bytes": "1", "keep_relpath": "08-Lexington-Services\\a.pdf",
           "is_md": "False", "top_level": "02-F3-Energy"}
    with pytest.raises(prop.GateError, match="LEX"):
        prop.check_gates([row], [], prop.Result([], [], 0, 0))


# ── dry-run, file of record, write guards ───────────────────────────────────

def test_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    c = Case(tmp_path)
    c.group([ENT], EA)
    paths = c.write_inputs()
    real_open = builtins.open

    def guarded_open(file, mode="r", *a, **kw):
        if any(ch in mode for ch in "wax+"):
            raise AssertionError(f"dry-run opened {file!r} for writing")
        return real_open(file, mode, *a, **kw)
    monkeypatch.setattr(builtins, "open", guarded_open)
    for name in ("write_text_atomic", "write_bytes_atomic", "append_text"):
        monkeypatch.setattr(drive_io, name, lambda *a, **k: (_ for _ in ()).throw(AssertionError(name)))
    assert prop.main(c.argv(paths)) == 0
    monkeypatch.setattr(builtins, "open", real_open)
    assert not any(paths["out"].iterdir())
    out = capsys.readouterr().out
    assert "DRY-RUN: nothing written" in out and "1 groups -> 1 files" in out


def test_the_planning_file_of_record_wins_over_a_differing_snapshot(tmp_path, capsys):
    c = Case(tmp_path)
    c.group([ENT], EA)
    paths = c.write_inputs()
    paths["snapshot"].write_bytes(hdr.render_manifest_csv([]))       # a stale, different snapshot
    assert prop.main(c.argv(paths, write=True)) == 0
    assert "WARN" in capsys.readouterr().out
    summary = (paths["out"] / prop.OUT_SUMMARY).read_text(encoding="utf-8")
    assert "DIFFERS" in summary and "the file of record wins" in summary
    assert set(_archived(paths)) == {EA}, "rows come from the file of record, not the snapshot"


def test_write_is_create_only_and_refuses_a_missing_or_in_tree_out_dir(tmp_path, capsys):
    c = Case(tmp_path)
    c.group([ENT], EA)
    paths = c.write_inputs()
    assert prop.main(c.argv(paths, write=True)) == 0
    before = {p.name: p.read_bytes() for p in paths["out"].iterdir()}
    assert prop.main(c.argv(paths, write=True)) == 2                     # refuses to clobber
    assert {p.name: p.read_bytes() for p in paths["out"].iterdir()} == before
    missing = tmp_path / "nope"
    assert prop.main(c.argv(paths) + ["--write", "--out-dir", str(missing)]) == 2
    assert not missing.exists(), "the out-dir is never created"
    inside = c.root / "_shared"
    inside.mkdir(exist_ok=True)
    assert prop.main(c.argv(paths) + ["--write", "--out-dir", str(inside)]) == 2
    assert not any((inside / n).exists() for n in prop.OUT_NAMES)


def test_summary_is_counts_only(tmp_path):
    c = Case(tmp_path)
    c.group([ENT], EA)
    c.group([INV], RI_RECEIPTS)
    c.group(["00-Founder\\reports\\statement.pdf"], "00-Founder\\personal-finances\\s.pdf")
    rc, paths = _run(c)
    summary = (paths["out"] / prop.OUT_SUMMARY).read_text(encoding="utf-8")
    for r in c.rows:
        assert r["source_relpath"] not in summary and r["keep_relpath"] not in summary


# ── the pinned-container list tracks kb_exclusions ──────────────────────────

def test_pinned_containers_cover_every_in_tree_kb_pin():
    prefixes = {p.lower() for p, _ in hdr.PINNED_CONTAINERS}
    in_tree = []
    for label in kb_exclusions.KB_EXCLUDED_FOLDER_LABELS.values():
        path = label.split(" (", 1)[0].strip()
        if path.startswith(("0", "_shared")):
            in_tree.append(path.replace("/", "\\").lower())
    assert in_tree, "no in-tree pins found -- the rail would be vacuous"
    missing = [p for p in in_tree if p not in prefixes]
    assert not missing, f"KB-pinned containers missing from hygiene_drive_report.PINNED_CONTAINERS: {missing}"
    for rider_b in ("_archive", "00-founder\\personal-finances"):
        assert rider_b in prefixes


def test_classify_zones():
    z = lambda p, s="narrow": prop.classify(p, ri_scope=s)[0]   # noqa: E731
    assert z("08-Lexington-Services\\a.pdf") == "LEX"
    assert z("_archive\\dedup-2026-09\\x.pdf") == "PIN"
    assert z("02-F3-Energy\\projects\\capital-raise\\deck.pdf") == "PIN"
    assert z(RI_RECEIPTS) == "LANE" and z(RI_ERIC) == "RI-OUTSIDE" and z(RI_ERIC, "broad") == "LANE"
    assert z(INV) == "LANE" and z(EA) == "LANE" and z(ENT) == "ENT"
    assert z("_brain\\known.md") == "OLANE" and z("loose.pdf") == "OTHER"
    # the proposer's LEX rule is the PARTITION (apply.ps1 gate 2 + archived LEX copies);
    # a Lexington-NAMED HJRG accounting doc is an ordinary entity file here
    assert z("_archive\\08-Lexington-Services\\a.pdf") == "LEX"
    assert z("01-HJR-Global\\accounting\\Lexington Entities\\a.pdf") == "ENT"
