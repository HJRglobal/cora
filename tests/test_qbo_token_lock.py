"""A5 S0 — cross-process locking around the QBO token read-modify-write.

The hazard being pinned: the token store is ONE json file holding ALL realms.
`_set_entity_tokens` loads the whole map, mutates one realm, and saves it back.
Two processes refreshing DIFFERENT realms concurrently each read the same
snapshot, and the second save silently drops the first realm's ROTATED refresh
token -- unrecoverable, since Intuit invalidates the old token on rotation.

`_save_all_tokens` itself has been atomic (temp file + replace) since 2026-05-19;
these tests are about the cycle AROUND it, not partial writes.
"""

import json
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

import cora.connectors.qbo_oauth as qbo_oauth
from cora.connectors.qbo_oauth import QboAuthError, _set_entity_tokens

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _entry(token: str, realm: str) -> dict:
    return {
        "realm_id": realm,
        "access_token": f"at-{token}",
        "refresh_token": f"rt-{token}",
        "access_token_expires_at": 1_800_000_000,
        "refresh_token_expires_at": 1_900_000_000,
        "last_refreshed_at": 1_700_000_000,
        "environment": "production",
    }


# ── lock plumbing ────────────────────────────────────────────────────────────

class TestLockPlumbing:
    def test_lock_path_derives_from_token_file_at_call_time(self, tmp_path, monkeypatch):
        """A module-level lock constant would make every test share the REAL repo
        lock file. The path must follow a redirected _TOKEN_FILE."""
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", tmp_path / "qbo-tokens.json")
        assert qbo_oauth._lock_path() == tmp_path / "qbo-tokens.json.lock"

        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", tmp_path / "other" / "t.json")
        assert qbo_oauth._lock_path() == tmp_path / "other" / "t.json.lock"

    def test_lock_creates_parent_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", tmp_path / "deep" / "qbo.json")
        with qbo_oauth._token_file_lock():
            pass
        assert (tmp_path / "deep").is_dir()

    def test_lock_is_released_after_the_block(self, tmp_path, monkeypatch):
        """Re-entering must not deadlock or time out -- otherwise the first refresh
        of the day would wedge every later one."""
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", tmp_path / "qbo.json")
        for _ in range(3):
            with qbo_oauth._token_file_lock(timeout=2.0):
                pass

    def test_lock_released_even_when_the_block_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", tmp_path / "qbo.json")
        with pytest.raises(RuntimeError):
            with qbo_oauth._token_file_lock(timeout=2.0):
                raise RuntimeError("boom")
        # Still acquirable.
        with qbo_oauth._token_file_lock(timeout=2.0):
            pass

    def test_timeout_is_env_tunable(self, monkeypatch):
        monkeypatch.setenv("QBO_TOKEN_LOCK_TIMEOUT_SEC", "3.5")
        assert qbo_oauth._lock_timeout() == 3.5

    @pytest.mark.parametrize("raw", ["", "not-a-number", "0", "-1"])
    def test_timeout_falls_back_on_garbage(self, monkeypatch, raw):
        monkeypatch.setenv("QBO_TOKEN_LOCK_TIMEOUT_SEC", raw)
        assert qbo_oauth._lock_timeout() == qbo_oauth._DEFAULT_LOCK_TIMEOUT_SEC


# ── the named S0 regression: two concurrent refresh cycles, different realms ──

class TestConcurrentRefreshCrossProcess:
    """The real thing: two OS processes, genuinely concurrent."""

    def _child(self, token_file: Path, entity: str, token: str, hold_sec: float) -> str:
        return textwrap.dedent(f"""
            import sys, time, json
            from pathlib import Path
            sys.path.insert(0, r"{_REPO_ROOT / 'src'}")
            import cora.connectors.qbo_oauth as q
            q._TOKEN_FILE = Path(r"{token_file}")

            # Widen the read-modify-write window so the interleaving that loses a
            # token is near-certain WITHOUT the lock. Under the lock this delay is
            # inside the critical section, so the other process simply waits.
            _real_load = q._load_all_tokens
            def slow_load():
                data = _real_load()
                time.sleep({hold_sec})
                return data
            q._load_all_tokens = slow_load

            q._set_entity_tokens("{entity}", {{
                "realm_id": "realm-{entity}",
                "access_token": "at-{token}",
                "refresh_token": "rt-{token}",
                "access_token_expires_at": 1800000000,
                "refresh_token_expires_at": 1900000000,
                "last_refreshed_at": 1700000000,
                "environment": "production",
            }})
            print("OK")
        """)

    def test_both_realms_persist_their_rotated_tokens(self, tmp_path):
        token_file = tmp_path / "qbo-tokens.json"
        token_file.parent.mkdir(parents=True, exist_ok=True)
        # Pre-existing store both children will load.
        token_file.write_text(json.dumps({"SEED": _entry("seed", "realm-seed")}), encoding="utf-8")

        procs = [
            subprocess.Popen(
                [sys.executable, "-c", self._child(token_file, ent, tok, 0.6)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for ent, tok in (("F3E", "f3e-rotated"), ("HJRG", "hjrg-rotated"))
        ]
        outs = [p.communicate(timeout=90) for p in procs]
        for (out, err), p in zip(outs, procs):
            assert p.returncode == 0, f"child failed: {err[-800:]}"
            assert "OK" in out

        saved = json.loads(token_file.read_text(encoding="utf-8"))

        # THE ASSERTION: neither realm's rotated refresh token was dropped.
        assert saved["F3E"]["refresh_token"] == "rt-f3e-rotated"
        assert saved["HJRG"]["refresh_token"] == "rt-hjrg-rotated"
        # And the untouched realm survived both merges.
        assert saved["SEED"]["refresh_token"] == "rt-seed"

    def test_a_holder_blocks_a_second_process_until_release(self, tmp_path):
        """Proves the lock is actually cross-PROCESS, not just cross-thread:
        an in-process-only lock would let the child write immediately."""
        token_file = tmp_path / "qbo-tokens.json"
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text("{}", encoding="utf-8")

        child = textwrap.dedent(f"""
            import sys, time
            from pathlib import Path
            sys.path.insert(0, r"{_REPO_ROOT / 'src'}")
            import cora.connectors.qbo_oauth as q
            q._TOKEN_FILE = Path(r"{token_file}")
            start = time.monotonic()
            with q._token_file_lock(timeout=30):
                pass
            print(f"WAITED={{time.monotonic() - start:.2f}}")
        """)

        import cora.connectors.qbo_oauth as q

        original = q._TOKEN_FILE
        q._TOKEN_FILE = token_file
        try:
            with q._token_file_lock(timeout=10):
                proc = subprocess.Popen(
                    [sys.executable, "-c", child],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                time.sleep(1.5)
                assert proc.poll() is None, "child acquired the lock while it was held"
            out, err = proc.communicate(timeout=60)
        finally:
            q._TOKEN_FILE = original

        assert proc.returncode == 0, err[-800:]
        waited = float(out.strip().split("=")[1])
        assert waited >= 1.0, f"child did not actually block (waited {waited}s)"


class TestConcurrentRefreshInProcess:
    """Threads inside one process (the bot refreshing two realms on two request
    threads) must serialize too."""

    def test_threaded_writes_do_not_lose_a_realm(self, tmp_path, monkeypatch):
        token_file = tmp_path / "qbo-tokens.json"
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", token_file)

        real_load = qbo_oauth._load_all_tokens

        def slow_load():
            data = real_load()
            time.sleep(0.25)
            return data

        monkeypatch.setattr(qbo_oauth, "_load_all_tokens", slow_load)

        errors: list[BaseException] = []

        def write(entity: str, token: str) -> None:
            try:
                _set_entity_tokens(entity, _entry(token, f"realm-{entity}"))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [
            threading.Thread(target=write, args=(ent, tok))
            for ent, tok in (("F3E", "a"), ("HJRG", "b"), ("LEX", "c"))
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert not errors, errors
        saved = json.loads(token_file.read_text(encoding="utf-8"))
        assert set(saved) == {"F3E", "HJRG", "LEX"}
        assert saved["F3E"]["refresh_token"] == "rt-a"
        assert saved["HJRG"]["refresh_token"] == "rt-b"
        assert saved["LEX"]["refresh_token"] == "rt-c"


class TestFailClosed:
    def test_timeout_raises_rather_than_writing_unlocked(self, tmp_path, monkeypatch):
        """A skipped refresh is recoverable; a clobbered rotated refresh token is
        not. So the timeout path must raise, never fall through to a write."""
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", tmp_path / "qbo-tokens.json")

        def always_busy(fd):
            raise OSError("locked")

        monkeypatch.setattr(qbo_oauth, "_lock_fd", always_busy)

        with pytest.raises(QboAuthError, match="Timed out"):
            with qbo_oauth._token_file_lock(timeout=0.2):
                pytest.fail("body must not run when the lock was never acquired")

    def test_set_entity_tokens_writes_nothing_on_lock_timeout(self, tmp_path, monkeypatch):
        token_file = tmp_path / "qbo-tokens.json"
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(json.dumps({"F3E": _entry("original", "r1")}), encoding="utf-8")
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", token_file)
        monkeypatch.setattr(
            qbo_oauth, "_lock_fd", lambda fd: (_ for _ in ()).throw(OSError("locked"))
        )

        with pytest.raises(QboAuthError):
            _set_entity_tokens("F3E", _entry("clobber", "r1"))

        saved = json.loads(token_file.read_text(encoding="utf-8"))
        assert saved["F3E"]["refresh_token"] == "rt-original"


class TestWriteStillWorks:
    """Belt: the lock must not change the observable write contract."""

    def test_round_trip_unchanged(self, tmp_path, monkeypatch):
        token_file = tmp_path / "qbo-tokens.json"
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", token_file)
        _set_entity_tokens("F3E", _entry("x", "r1"))
        _set_entity_tokens("HJRG", _entry("y", "r2"))
        saved = json.loads(token_file.read_text(encoding="utf-8"))
        assert saved["F3E"]["realm_id"] == "r1"
        assert saved["HJRG"]["realm_id"] == "r2"

    def test_lock_file_is_not_the_token_file(self, tmp_path, monkeypatch):
        """A lock path that collided with the token file would truncate the store."""
        token_file = tmp_path / "qbo-tokens.json"
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", token_file)
        _set_entity_tokens("F3E", _entry("x", "r1"))
        assert qbo_oauth._lock_path() != token_file
        assert json.loads(token_file.read_text(encoding="utf-8"))["F3E"]["realm_id"] == "r1"
