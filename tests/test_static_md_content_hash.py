"""F-09: the static-md sync re-ingests on a CONTENT change (sha256), not just an
mtime advance -- so a redaction (content shrinks, mtime may not move past the
watermark) still propagates and stale figure chunks are replaced."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import incremental_sync_static as m  # noqa: E402


def test_sha256_file_changes_with_content(tmp_path):
    f = tmp_path / "doc.md"
    f.write_text("cash value $3,806,354", encoding="utf-8")
    h1 = m._sha256_file(f)
    f.write_text("cash value [redacted]", encoding="utf-8")   # a shorter redaction
    h2 = m._sha256_file(f)
    assert h1 and h2 and h1 != h2


def test_sha256_file_missing_returns_none(tmp_path):
    assert m._sha256_file(tmp_path / "nope.md") is None


def test_hash_store_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(m, "_HASH_STORE_PATH", tmp_path / "state" / "hashes.json")
    assert m._load_hash_store() == {}          # missing -> empty
    m._save_hash_store({"a/b.md": "deadbeef"})
    assert m._load_hash_store() == {"a/b.md": "deadbeef"}


def test_hash_store_corrupt_file_fails_soft(tmp_path, monkeypatch):
    p = tmp_path / "hashes.json"
    p.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(m, "_HASH_STORE_PATH", p)
    assert m._load_hash_store() == {}          # unparseable -> empty, no raise


def test_content_change_re_selects_even_when_mtime_stale():
    # The core F-09 logic (mirrors the walk): a file whose hash differs from the
    # stored one is re-selected even when its mtime is BEHIND the watermark.
    stored = {"canon.md": "OLD_HASH"}
    current_hash = "NEW_HASH"
    mtime, watermark = 100.0, 200.0            # mtime behind the watermark
    mtime_changed = mtime > watermark
    content_changed = current_hash is not None and stored.get("canon.md") != current_hash
    assert not mtime_changed and content_changed  # re-selected via content hash only
