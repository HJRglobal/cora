"""WS-BACKUP: cora_kb.db (regenerable, ~6 GB) is NOT backed up to Drive by default.

Backing it up daily was large Drive cost for ~zero DR value (rebuildable from
connectors -- see deployment/kb-rebuild.md). --include-kb is an explicit opt-in.
"""

import sys
from pathlib import Path


def _load():
    repo = Path(__file__).resolve().parents[1]
    sd = str(repo / "scripts")
    if sd not in sys.path:
        sys.path.insert(0, sd)
    import backup_logs  # noqa: PLC0415
    return backup_logs


def test_kb_excluded_by_default(tmp_path):
    bl = _load()
    dest = tmp_path / "2026-06-19"
    dest.mkdir()
    status = bl.backup_kb_database(dest, dry_run=False, include_kb=False)
    assert status is None                            # intentionally skipped
    assert not (dest / "cora_kb.db").exists()        # no ~6 GB copy written


def test_kb_included_when_flagged(tmp_path, monkeypatch):
    bl = _load()
    fake_kb = tmp_path / "cora_kb.db"
    fake_kb.write_bytes(b"x" * 100)
    monkeypatch.setattr(bl, "KB_DB_PATH", fake_kb)
    dest = tmp_path / "d"
    dest.mkdir()
    status = bl.backup_kb_database(dest, dry_run=True, include_kb=True)
    assert status is True                             # would back up the KB


def test_verify_offsite_passes_when_small_set_landed(tmp_path):
    bl = _load()
    dest = tmp_path / "d"
    dest.mkdir()
    (dest / "knowledge-gaps.jsonl").write_text("{}", encoding="utf-8")
    assert bl.verify_offsite(dest, None, include_kb=False, dry_run=False) is True


def test_verify_offsite_fails_on_empty_dir(tmp_path):
    bl = _load()
    dest = tmp_path / "d"
    dest.mkdir()
    assert bl.verify_offsite(dest, None, include_kb=False, dry_run=False) is False
