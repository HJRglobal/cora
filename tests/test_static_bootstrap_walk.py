"""S2 (2026-09-03, knowledge-parity audit G5): the static walk now ingests the
exact filename ``bootstrap.txt`` alongside ``*.md`` -- and NOTHING else that is
not ``.md``. Every candidate runs through the same exclusion chain and the same
entity classifier, in both the incremental sync and the full rebuild."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import incremental_sync_static as inc  # noqa: E402
import migrate_static_md as mig  # noqa: E402


def _tree(tmp_path: Path) -> Path:
    root = tmp_path / "HJR-Founder-OS"
    (root / "02-F3-Energy" / "projects" / "pure-launch").mkdir(parents=True)
    (root / "02-F3-Energy" / "projects" / "pure-launch" / "bootstrap.txt").write_text(
        "Pure launch bootstrap: read CLAUDE.md first.", encoding="utf-8")
    (root / "02-F3-Energy" / "projects" / "pure-launch" / "env.txt").write_text("SECRET=1", encoding="utf-8")
    (root / "02-F3-Energy" / "projects" / "pure-launch" / "notes.txt").write_text("scratch", encoding="utf-8")
    (root / "02-F3-Energy" / "projects" / "pure-launch" / "brief.md").write_text("# Brief", encoding="utf-8")
    (root / "_shared" / "projects" / "cora").mkdir(parents=True)
    (root / "_shared" / "projects" / "cora" / "bootstrap.txt").write_text("cora bootstrap", encoding="utf-8")
    (root / "08-Lexington-Services" / "projects" / "copa-bhrf").mkdir(parents=True)
    (root / "08-Lexington-Services" / "projects" / "copa-bhrf" / "bootstrap.txt").write_text("nda", encoding="utf-8")
    (root / "_shared" / "projects" / "gmail-deep-dive").mkdir(parents=True)
    (root / "_shared" / "projects" / "gmail-deep-dive" / "bootstrap.txt").write_text("gmail bootstrap", encoding="utf-8")
    return root


def test_candidates_include_bootstrap_txt_and_no_other_txt(tmp_path):
    root = _tree(tmp_path)
    names = sorted(p.name for p in inc.iter_static_candidates(root))
    assert "bootstrap.txt" in names
    assert "brief.md" in names
    assert "env.txt" not in names
    assert "notes.txt" not in names
    # exactly the one non-md name is walked
    assert set(n for n in names if not n.endswith(".md")) == {"bootstrap.txt"}


def test_bootstrap_under_cora_workspace_and_copa_are_excluded(tmp_path):
    root = _tree(tmp_path)
    cora = root / "_shared" / "projects" / "cora" / "bootstrap.txt"
    copa = root / "08-Lexington-Services" / "projects" / "copa-bhrf" / "bootstrap.txt"
    keep = root / "_shared" / "projects" / "gmail-deep-dive" / "bootstrap.txt"
    assert inc.is_static_excluded(cora)
    assert inc.is_static_excluded(copa)
    assert not inc.is_static_excluded(keep)


def test_file_to_document_classifies_and_titles_bootstrap(tmp_path, monkeypatch):
    root = _tree(tmp_path)
    monkeypatch.setattr(inc, "FOUNDER_OS_ROOT", root)
    doc = inc.file_to_document(root / "02-F3-Energy" / "projects" / "pure-launch" / "bootstrap.txt")
    assert doc is not None
    assert doc.entity == "F3E"
    assert doc.source == "static_md"
    assert doc.source_id.replace("\\", "/") == "02-F3-Energy/projects/pure-launch/bootstrap.txt"
    assert doc.title == "Pure Launch Bootstrap"          # never a bare "Bootstrap" x28
    assert doc.metadata.get("kind") == "bootstrap"
    # _shared/... lands FNDR by the existing default
    shared = inc.file_to_document(root / "_shared" / "projects" / "gmail-deep-dive" / "bootstrap.txt")
    assert shared is not None and shared.entity == "FNDR"
    # the Cora-workspace copy is refused by file_to_document too (belt for the walk filter)
    assert inc.file_to_document(root / "_shared" / "projects" / "cora" / "bootstrap.txt") is None


def test_md_title_unchanged(tmp_path, monkeypatch):
    root = _tree(tmp_path)
    monkeypatch.setattr(inc, "FOUNDER_OS_ROOT", root)
    doc = inc.file_to_document(root / "02-F3-Energy" / "projects" / "pure-launch" / "brief.md")
    assert doc is not None and doc.title == "Brief" and "kind" not in doc.metadata


def test_full_rebuild_walks_the_same_candidates(tmp_path, monkeypatch):
    """migrate_static_md.discover_files must not drift from the incremental walk."""
    root = _tree(tmp_path)
    monkeypatch.setattr(mig, "FOUNDER_OS_ROOT", root)
    found = {p.relative_to(root).as_posix() for p in mig.discover_files()}
    assert "02-F3-Energy/projects/pure-launch/bootstrap.txt" in found
    assert "_shared/projects/gmail-deep-dive/bootstrap.txt" in found
    assert "02-F3-Energy/projects/pure-launch/brief.md" in found
    assert not any(p.endswith("env.txt") or p.endswith("notes.txt") for p in found)
    assert "_shared/projects/cora/bootstrap.txt" not in found


def test_exact_filename_list_is_only_bootstrap():
    """Guard against a future 'just add *.txt' -- the design forbids it."""
    assert inc.STATIC_EXACT_FILENAMES == ("bootstrap.txt",)
