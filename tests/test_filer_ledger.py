"""Tests for the crash-safe email-filer idempotency ledgers (2026-06-14).

The filer's dedup state never persisted in production (the run was killed by the
Task Scheduler 15-min limit before the end-of-run save). These ledgers are
append-only JSONL so a kill loses at most the in-flight line, and content is
keyed on md5 so the same bytes arriving via different emails dedup.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.connectors import filer_ledger as fl


def _set_paths(tmp_path, monkeypatch):
    cpath = tmp_path / "content.jsonl"
    mpath = tmp_path / "message.jsonl"
    monkeypatch.setenv("FILER_CONTENT_LEDGER_PATH", str(cpath))
    monkeypatch.setenv("FILER_MESSAGE_LEDGER_PATH", str(mpath))
    return cpath, mpath


def _data_rows(path: Path):
    return [
        json.loads(l)
        for l in path.read_text(encoding="utf-8").splitlines()
        if l.strip() and "_schema" not in l
    ]


# ─────────────────────────────────────────────────────────────────────────────
# make_msg_key
# ─────────────────────────────────────────────────────────────────────────────

class TestMakeMsgKey:
    def test_prefers_rfc_message_id(self):
        assert fl.make_msg_key("<abc@host>", "gid1") == "<abc@host>"

    def test_falls_back_to_gmail_id_when_rfc_empty(self):
        assert fl.make_msg_key("", "gid1") == "gmail:gid1"

    def test_falls_back_when_rfc_whitespace(self):
        assert fl.make_msg_key("   ", "gid1") == "gmail:gid1"

    def test_empty_both_returns_empty(self):
        assert fl.make_msg_key("", "") == ""


# ─────────────────────────────────────────────────────────────────────────────
# Content ledger
# ─────────────────────────────────────────────────────────────────────────────

class TestContentLedger:
    def test_missing_file_loads_empty(self, tmp_path, monkeypatch):
        _set_paths(tmp_path, monkeypatch)
        assert fl.load_content_ledger() == {}

    def test_append_then_record_hit(self, tmp_path, monkeypatch):
        _set_paths(tmp_path, monkeypatch)
        ledger = {}
        assert fl.append_content(
            ledger, "md5abc", file_id="f1", web_link="u1",
            drive_path="09-One-Stop-Nutrition/legal/x.pdf", canonical="x.pdf",
            sha256="sha", source_email="harrison@hjrglobal.com",
        ) is True
        rec = fl.content_record(ledger, "md5abc")
        assert rec is not None and rec["file_id"] == "f1"
        assert fl.content_record(ledger, "other") is None

    def test_append_persists_to_disk_and_reloads(self, tmp_path, monkeypatch):
        cpath, _ = _set_paths(tmp_path, monkeypatch)
        ledger = {}
        fl.append_content(ledger, "md5abc", file_id="f1", web_link="u1",
                          drive_path="p", canonical="x.pdf")
        # Simulate a fresh run: reload from disk
        reloaded = fl.load_content_ledger()
        assert "md5abc" in reloaded
        assert reloaded["md5abc"]["file_id"] == "f1"
        # crash-safe: exactly one data row on disk
        assert len(_data_rows(cpath)) == 1

    def test_empty_md5_not_recorded(self, tmp_path, monkeypatch):
        _set_paths(tmp_path, monkeypatch)
        ledger = {}
        assert fl.append_content(ledger, "", file_id="f", web_link="",
                                 drive_path="p", canonical="x") is False
        assert ledger == {}

    def test_ttl_prunes_old_rows(self, tmp_path, monkeypatch):
        cpath, _ = _set_paths(tmp_path, monkeypatch)
        old_ts = int(time.time()) - 400 * 86400
        rows = [
            {"_schema": "x"},
            {"md5": "stale", "filed_at": old_ts, "file_id": "old"},
            {"md5": "fresh", "filed_at": int(time.time()), "file_id": "new"},
        ]
        cpath.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        loaded = fl.load_content_ledger()
        assert "fresh" in loaded
        assert "stale" not in loaded

    def test_last_row_wins(self, tmp_path, monkeypatch):
        cpath, _ = _set_paths(tmp_path, monkeypatch)
        now = int(time.time())
        rows = [
            {"md5": "k", "filed_at": now - 10, "file_id": "first"},
            {"md5": "k", "filed_at": now, "file_id": "second"},
        ]
        cpath.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        assert fl.load_content_ledger()["k"]["file_id"] == "second"


# ─────────────────────────────────────────────────────────────────────────────
# Message ledger
# ─────────────────────────────────────────────────────────────────────────────

class TestMessageLedger:
    def test_missing_file_loads_empty(self, tmp_path, monkeypatch):
        _set_paths(tmp_path, monkeypatch)
        assert fl.load_message_ledger() == set()

    def test_record_then_done(self, tmp_path, monkeypatch):
        _set_paths(tmp_path, monkeypatch)
        seen = set()
        assert fl.message_done(seen, "<m1>") is False
        assert fl.record_message_done(seen, "<m1>", filed=2, skipped=1, subject="hi") is True
        assert fl.message_done(seen, "<m1>") is True

    def test_record_persists_and_reloads(self, tmp_path, monkeypatch):
        _set_paths(tmp_path, monkeypatch)
        seen = set()
        fl.record_message_done(seen, "<m1>")
        assert "<m1>" in fl.load_message_ledger()

    def test_empty_key_not_recorded(self, tmp_path, monkeypatch):
        _set_paths(tmp_path, monkeypatch)
        seen = set()
        assert fl.record_message_done(seen, "") is False
        assert seen == set()

    def test_ttl_prunes_old_messages(self, tmp_path, monkeypatch):
        _, mpath = _set_paths(tmp_path, monkeypatch)
        old_ts = int(time.time()) - 200 * 86400
        rows = [
            {"msg_key": "<old>", "filed_at": old_ts},
            {"msg_key": "<new>", "filed_at": int(time.time())},
        ]
        mpath.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        loaded = fl.load_message_ledger()
        assert "<new>" in loaded and "<old>" not in loaded

    def test_corrupt_lines_skipped(self, tmp_path, monkeypatch):
        _, mpath = _set_paths(tmp_path, monkeypatch)
        mpath.write_text(
            '{"_schema":"x"}\nnot json\n{"msg_key":"<ok>","filed_at":%d}\n' % int(time.time()),
            encoding="utf-8",
        )
        assert fl.load_message_ledger() == {"<ok>"}
