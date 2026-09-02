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
    # These exercise the --include-kb path (KB verification). The default
    # (KB-excluded) verification is covered by test_backup_kb_excluded.py.
    def test_fail_when_kb_step_failed(self, tmp_path):
        bl = _load("backup_logs")
        assert bl.verify_offsite(tmp_path, False, include_kb=True, dry_run=False) is False

    def test_fail_when_dst_missing(self, tmp_path):
        bl = _load("backup_logs")
        assert bl.verify_offsite(tmp_path, True, include_kb=True, dry_run=False) is False

    def test_fail_when_dst_empty(self, tmp_path):
        bl = _load("backup_logs")
        (tmp_path / "cora_kb.db").write_bytes(b"")
        assert bl.verify_offsite(tmp_path, True, include_kb=True, dry_run=False) is False

    def test_pass_when_dst_present(self, tmp_path):
        bl = _load("backup_logs")
        (tmp_path / "cora_kb.db").write_bytes(b"x" * 1024)
        assert bl.verify_offsite(tmp_path, True, include_kb=True, dry_run=False) is True

    def test_dry_run_always_passes(self, tmp_path):
        bl = _load("backup_logs")
        assert bl.verify_offsite(tmp_path, False, include_kb=True, dry_run=True) is True


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


# -- background I/O priority (2026-09-02) -----------------------------------
#
# The 1pm backup streams a multi-GB KB copy to Drive while Harrison is at the
# machine. Task Scheduler's Priority only lowers the CPU class; background
# processing mode lowers CPU *and disk*, which is what this job saturates.

class TestBackgroundIoMode:
    def test_enter_then_leave_in_order(self, monkeypatch, capsys):
        bl = _load("backup_logs")
        calls = []
        monkeypatch.setattr(bl, "_set_process_background", lambda enter: calls.append(enter) or True)
        monkeypatch.setattr(bl, "_background_mode_entered", False, raising=False)
        bl.enter_background_mode()
        bl.leave_background_mode()
        assert calls == [True, False]
        out = capsys.readouterr().out
        assert "Background I/O mode: ON" in out
        assert "Background I/O mode: OFF" in out

    def test_leave_is_a_noop_when_enter_failed(self, monkeypatch):
        """END outside background mode fails with ERROR_PROCESS_MODE_NOT_BACKGROUND,
        so it must only be attempted if BEGIN actually took effect."""
        bl = _load("backup_logs")
        calls = []

        def fake(enter):
            calls.append(enter)
            return False  # BEGIN did not take effect

        monkeypatch.setattr(bl, "_set_process_background", fake)
        monkeypatch.setattr(bl, "_background_mode_entered", False, raising=False)
        bl.enter_background_mode()
        bl.leave_background_mode()
        assert calls == [True], "END must not be called when BEGIN failed"

    def test_background_mode_is_restored_even_when_the_run_raises(self, monkeypatch):
        """main() must leave the process out of background mode on ANY exit
        path -- a leaked background mode would slow whatever runs next."""
        bl = _load("backup_logs")
        calls = []
        monkeypatch.setattr(bl, "_set_process_background", lambda enter: calls.append(enter) or True)
        monkeypatch.setattr(bl, "_background_mode_entered", False, raising=False)
        monkeypatch.setattr(bl, "parse_args", lambda: object())

        def boom(_args):
            raise RuntimeError("drive unmounted")

        monkeypatch.setattr(bl, "_run", boom)
        with pytest.raises(RuntimeError):
            bl.main()
        assert calls == [True, False], "background mode leaked on the exception path"

    def test_main_returns_the_run_result(self, monkeypatch):
        bl = _load("backup_logs")
        monkeypatch.setattr(bl, "_set_process_background", lambda enter: True)
        monkeypatch.setattr(bl, "_background_mode_entered", False, raising=False)
        monkeypatch.setattr(bl, "parse_args", lambda: object())
        monkeypatch.setattr(bl, "_run", lambda _args: 1)
        assert bl.main() == 1

    def test_mode_constants_match_the_win32_values(self):
        bl = _load("backup_logs")
        assert bl._PROCESS_MODE_BACKGROUND_BEGIN == 0x00100000
        assert bl._PROCESS_MODE_BACKGROUND_END == 0x00200000

    def test_no_op_off_windows(self, monkeypatch):
        bl = _load("backup_logs")
        monkeypatch.setattr(bl.os, "name", "posix")
        assert bl._set_process_background(True) is False

    @pytest.mark.skipif(sys.platform != "win32", reason="Win32 background mode")
    def test_real_win32_call_succeeds_both_ways(self):
        """Regression pin for the ctypes declaration bug: without an explicit
        restype on GetCurrentProcess the pseudo-handle is truncated to 32 bits
        and SetPriorityClass returns FALSE, so the whole feature silently did
        nothing."""
        bl = _load("backup_logs")
        assert bl._set_process_background(True) is True, "BEGIN failed -- check ctypes argtypes"
        assert bl._set_process_background(False) is True, "END failed"
