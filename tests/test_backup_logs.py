"""Tests for the DR backup hardening: encrypted secrets, feature-DB backup, offsite verify.

Layer A: pure logic with mocks/temp dirs. No network, no live KB.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).parents[1] / "scripts"


def _load(mod_name: str):
    try:
        sys.path.insert(0, str(_SCRIPTS))
        return __import__(mod_name)
    except Exception:
        pytest.skip(f"{mod_name} not importable")


# ── secrets encryption round-trip ───────────────────────────────────────────

class TestSecretsCrypto:
    def test_encrypt_decrypt_roundtrip(self):
        bl = _load("backup_logs")
        rs = _load("restore_secrets")
        plaintext = b"SLACK_TOKEN=xoxb-secret\nOPENAI_API_KEY=sk-123\n"
        blob = bl._encrypt_bytes(plaintext, "correct horse battery staple")
        # salt(16) is prepended, so the blob is larger than the token alone
        assert len(blob) > 16
        out = rs._decrypt_bytes(blob, "correct horse battery staple")
        assert out == plaintext

    def test_wrong_passphrase_fails(self):
        bl = _load("backup_logs")
        rs = _load("restore_secrets")
        blob = bl._encrypt_bytes(b"top secret", "right-pass")
        with pytest.raises(Exception):
            rs._decrypt_bytes(blob, "wrong-pass")

    def test_distinct_salts_per_call(self):
        bl = _load("backup_logs")
        a = bl._encrypt_bytes(b"x", "p")
        b = bl._encrypt_bytes(b"x", "p")
        assert a[:16] != b[:16]  # random salt each time


# ── secret-file collection ──────────────────────────────────────────────────

class TestCollectSecretFiles:
    def test_includes_sa_json_when_present(self, tmp_path, monkeypatch):
        bl = _load("backup_logs")
        sa = tmp_path / "cora-calendar-sa.json"
        sa.write_text("{}")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", str(sa))
        items = bl._collect_secret_files()
        names = [arc for arc, _ in items]
        assert "cora-calendar-sa.json" in names

    def test_skips_missing_sa_json(self, monkeypatch):
        bl = _load("backup_logs")
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "/nonexistent/path/x.json")
        items = bl._collect_secret_files()
        assert all(arc != "x.json" for arc, _ in items)


# ── backup_secrets gating (never writes plaintext) ──────────────────────────

class TestBackupSecretsGating:
    def test_skips_without_passphrase(self, tmp_path, monkeypatch):
        bl = _load("backup_logs")
        monkeypatch.delenv("CORA_BACKUP_PASSPHRASE", raising=False)
        monkeypatch.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", str(tmp_path / "missing.json"))
        # Force a secret to exist so we reach the passphrase gate
        monkeypatch.setattr(bl, "ENV_PATH", tmp_path / ".env")
        (tmp_path / ".env").write_text("X=1")
        status = bl.backup_secrets(tmp_path, dry_run=False)
        assert status == "no-passphrase"
        # nothing encrypted was written
        assert not list(tmp_path.glob("secrets-*.enc"))

    def test_writes_encrypted_blob_with_passphrase(self, tmp_path, monkeypatch):
        bl = _load("backup_logs")
        monkeypatch.setenv("CORA_BACKUP_PASSPHRASE", "pw")
        monkeypatch.setattr(bl, "ENV_PATH", tmp_path / ".env")
        (tmp_path / ".env").write_text("SECRET=abc")
        monkeypatch.delenv("GOOGLE_SERVICE_ACCOUNT_JSON", raising=False)
        dest = tmp_path / "dest"
        dest.mkdir()
        status = bl.backup_secrets(dest, dry_run=False)
        assert status == "ok"
        blobs = list(dest.glob("secrets-*.enc"))
        assert len(blobs) == 1
        # and it round-trips back to the original .env content
        rs = _load("restore_secrets")
        import io, tarfile
        plain = rs._decrypt_bytes(blobs[0].read_bytes(), "pw")
        tar = tarfile.open(fileobj=io.BytesIO(plain), mode="r")
        assert tar.extractfile(".env").read() == b"SECRET=abc"


# ── offsite verification (the loud-failure guard) ───────────────────────────

class TestVerifyOffsite:
    def test_fail_when_kb_step_failed(self, tmp_path):
        bl = _load("backup_logs")
        assert bl.verify_offsite(tmp_path, kb_ok=False, dry_run=False) is False

    def test_fail_when_dst_missing(self, tmp_path):
        bl = _load("backup_logs")
        assert bl.verify_offsite(tmp_path, kb_ok=True, dry_run=False) is False

    def test_fail_when_dst_empty(self, tmp_path):
        bl = _load("backup_logs")
        (tmp_path / "cora_kb.db").write_bytes(b"")
        assert bl.verify_offsite(tmp_path, kb_ok=True, dry_run=False) is False

    def test_pass_when_dst_present(self, tmp_path):
        bl = _load("backup_logs")
        (tmp_path / "cora_kb.db").write_bytes(b"x" * 1024)
        assert bl.verify_offsite(tmp_path, kb_ok=True, dry_run=False) is True

    def test_dry_run_always_passes(self, tmp_path):
        bl = _load("backup_logs")
        assert bl.verify_offsite(tmp_path, kb_ok=False, dry_run=True) is True


# ── feature-DB backup excludes the main KB ──────────────────────────────────

class TestBackupFeatureDbs:
    def test_excludes_cora_kb_and_copies_others(self, tmp_path, monkeypatch):
        bl = _load("backup_logs")
        data = tmp_path / "data"
        data.mkdir()
        for name in ("cora_kb.db", "influencer_tracker.db", "hubspot_deal_snapshots.db"):
            conn = sqlite3.connect(str(data / name))
            conn.execute("CREATE TABLE t (a)")
            conn.commit()
            conn.close()
        monkeypatch.setattr(bl, "DATA_DIR", data)
        dest = tmp_path / "dest"
        dest.mkdir()
        count = bl.backup_feature_dbs(dest, dry_run=False)
        assert count == 2  # cora_kb.db excluded
        assert (dest / "influencer_tracker.db").exists()
        assert not (dest / "cora_kb.db").exists()
