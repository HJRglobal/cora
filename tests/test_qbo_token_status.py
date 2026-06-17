"""Tests for the QBO multi-realm token-validity monitor (Phase 3.3 / F-18).

Mocks the token store entirely -- makes NO Intuit or live Slack calls.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import qbo_token_status as mod  # noqa: E402
from cora.connectors import qbo_oauth  # noqa: E402

NOW = 1_780_000_000.0  # fixed epoch for determinism
DAY = 86400


def _tok(days_left=100, days_since_refresh=1, refresh="rt", error=None):
    t = {}
    if refresh is not None:
        t["refresh_token"] = refresh
    t["refresh_token_expires_at"] = NOW + days_left * DAY
    t["last_refreshed_at"] = NOW - days_since_refresh * DAY
    if error is not None:
        t["error"] = error
    return t


class TestEvaluate:
    def test_all_valid_no_failure(self):
        rows, has_failure = mod.evaluate({"F3E": _tok(), "HJRG": _tok(days_left=90)}, NOW)
        assert not has_failure
        assert all(r["status"] == "OK" for r in rows)

    def test_expired_realm_is_failure(self):
        rows, has_failure = mod.evaluate({"F3E": _tok(), "OSN": _tok(days_left=-2)}, NOW)
        assert has_failure
        assert next(r for r in rows if r["entity"] == "OSN")["status"] == "EXPIRED"

    def test_warn_window_is_not_failure(self):
        rows, has_failure = mod.evaluate({"F3E": _tok(days_left=10)}, NOW, warn_days=14)
        assert not has_failure
        assert rows[0]["status"] == "WARN"

    def test_missing_refresh_is_invalid(self):
        rows, has_failure = mod.evaluate({"F3E": _tok(refresh=None)}, NOW)
        assert has_failure
        assert rows[0]["status"] == "INVALID"

    def test_error_field_is_invalid(self):
        rows, has_failure = mod.evaluate({"F3E": _tok(error="refresh_token revoked")}, NOW)
        assert has_failure
        assert rows[0]["status"] == "INVALID"

    def test_stale_refresh_is_warn_only(self):
        # valid expiry but daily refresh hasn't run in 5d -> STALE (rotation may be failing)
        rows, has_failure = mod.evaluate({"F3E": _tok(days_left=90, days_since_refresh=5)}, NOW)
        assert not has_failure
        assert rows[0]["status"] == "STALE"


class TestMain:
    def _store(self, tmp_path, monkeypatch, data):
        f = tmp_path / "qbo-tokens.json"
        f.write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", f)
        return f

    def test_missing_file_exits_zero(self, tmp_path, monkeypatch):
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", tmp_path / "absent.json")
        assert mod.main([]) == 0

    def test_corrupt_json_exits_two(self, tmp_path, monkeypatch):
        f = tmp_path / "qbo-tokens.json"
        f.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(qbo_oauth, "_TOKEN_FILE", f)
        assert mod.main([]) == 2

    def test_expired_exits_one(self, tmp_path, monkeypatch):
        self._store(tmp_path, monkeypatch,
                    {"OSN": {"refresh_token": "rt", "refresh_token_expires_at": 1,
                             "last_refreshed_at": 1}})
        assert mod.main([]) == 1

    def test_alert_does_not_fire_when_all_valid(self, tmp_path, monkeypatch):
        self._store(tmp_path, monkeypatch,
                    {"F3E": {"refresh_token": "rt",
                             "refresh_token_expires_at": time.time() + 100 * DAY,
                             "last_refreshed_at": time.time()}})
        calls = []
        monkeypatch.setattr(mod, "_send_alert", lambda text, dry: calls.append(text))
        assert mod.main(["--alert"]) == 0
        assert calls == []

    def test_alert_fires_on_expired_and_passes_dry_run(self, tmp_path, monkeypatch):
        self._store(tmp_path, monkeypatch,
                    {"OSN": {"refresh_token": "rt", "refresh_token_expires_at": 1,
                             "last_refreshed_at": 1}})
        calls = []
        monkeypatch.setattr(mod, "_send_alert", lambda text, dry: calls.append((text, dry)))
        assert mod.main(["--alert", "--dry-run"]) == 1
        assert len(calls) == 1
        assert "OSN" in calls[0][0]
        assert calls[0][1] is True


class TestSendAlert:
    def test_dry_run_prints_not_sends(self, monkeypatch, capsys):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        mod._send_alert("hello", dry_run=True)
        assert "would DM" in capsys.readouterr().out

    def test_no_token_skips(self, monkeypatch, capsys):
        monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
        mod._send_alert("hello", dry_run=False)
        assert "SLACK_BOT_TOKEN not set" in capsys.readouterr().out
