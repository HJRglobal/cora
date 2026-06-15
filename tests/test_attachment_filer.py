"""Tests for the email attachment auto-filer dedup + crash-safety fixes (2026-06-14).

Acceptance criteria from the fix:
  * A message processed once files each attachment once; a second pass files zero
    (message-ledger short-circuit, no re-classify).
  * The same content arriving via a DIFFERENT email is filed once (md5 ledger).
  * The canonical filename's date prefix comes from the email Date header, not
    the run date.
  * --dry-run writes nothing (no upload, no ledger rows).
  * upload_file skips an existing file by name OR by content md5.
  * run_filer advances + persists the watermark per account, and never advances
    it for a list-failed or budget-hit account.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

import cora.connectors.attachment_filer as af
import cora.connectors.drive_connector as dc


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

# 2026-06-12 12:00:00 UTC — a fixed email Date, distinct from "today".
_EMAIL_DATE_TS = 1749729600


def _set_ledger_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("FILER_CONTENT_LEDGER_PATH", str(tmp_path / "content.jsonl"))
    monkeypatch.setenv("FILER_MESSAGE_LEDGER_PATH", str(tmp_path / "message.jsonl"))


def _meta(message_id="m1", rfc="<msg1@host>", subject="Signed doc"):
    return {
        "message_id": message_id,
        "rfc_message_id": rfc,
        "thread_id": "t1",
        "from": "Sender <s@x.com>",
        "to": "harrison@hjrglobal.com",
        "subject": subject,
        "date_ts": _EMAIL_DATE_TS,
        "snippet": "please see attached",
        "labels": [],
        "attachments": [
            {"filename": "doc.pdf", "mime_type": "application/pdf",
             "size": 200000, "attachment_id": "att1", "data": None},
        ],
    }


def _decisions(entity="OSN", subfolder="legal", desc="osn-guarantee"):
    return [{
        "action": "file", "entity": entity, "subfolder": subfolder,
        "description": desc, "filename": "doc.pdf", "reason": "signed agreement",
    }]


def _patch_pipeline(monkeypatch, *, meta, decisions, content=b"THE-PDF-BYTES"):
    """Patch the whole Gmail→Drive pipeline. Returns the upload MagicMock."""
    monkeypatch.setattr(af, "get_message", lambda u, m: {"id": m})
    monkeypatch.setattr(af, "parse_message_metadata", lambda msg: meta)
    monkeypatch.setattr(af, "classify_attachments",
                        lambda meta_, atts, entity_hint=None: decisions)
    monkeypatch.setattr(af, "download_attachment", lambda u, m, a: content)
    monkeypatch.setattr(af, "ensure_folder_path", lambda segs: "folder-" + "/".join(segs))
    upload = MagicMock(return_value=("file1", "https://drive/file1"))
    monkeypatch.setattr(af, "upload_file", upload)
    return upload


# ─────────────────────────────────────────────────────────────────────────────
# process_email — message-level + content-level dedup
# ─────────────────────────────────────────────────────────────────────────────

class TestProcessEmailDedup:
    def test_files_once_then_zero_on_second_pass(self, tmp_path, monkeypatch):
        _set_ledger_paths(tmp_path, monkeypatch)
        upload = _patch_pipeline(monkeypatch, meta=_meta(), decisions=_decisions())
        ledger, seen = {}, set()

        first = af.process_email("harrison@hjrglobal.com", "m1",
                                 content_ledger=ledger, seen_messages=seen)
        assert len(first) == 1
        assert upload.call_count == 1

        # Second pass — same message: short-circuits before classify, files nothing.
        second = af.process_email("harrison@hjrglobal.com", "m1",
                                  content_ledger=ledger, seen_messages=seen)
        assert second == []
        assert upload.call_count == 1  # not called again

    def test_same_content_via_different_message_deduped(self, tmp_path, monkeypatch):
        """The OSN case: same PDF arrives as the original + a 'Fwd:' (two msg-ids,
        two dates, slightly different names) — filed exactly once."""
        _set_ledger_paths(tmp_path, monkeypatch)
        same_bytes = b"IDENTICAL-SIGNED-PDF"
        ledger, seen = {}, set()

        # Email 1 — original Dropbox-Sign notice
        _patch_pipeline(monkeypatch, meta=_meta("m1", "<orig@sign>"),
                        decisions=_decisions(desc="osn-guarantee"), content=same_bytes)
        r1 = af.process_email("harrison@hjrglobal.com", "m1",
                              content_ledger=ledger, seen_messages=seen)
        assert len(r1) == 1

        # Email 2 — Micah's Fwd of the same PDF, different name + folder
        upload2 = _patch_pipeline(monkeypatch, meta=_meta("m2", "<fwd@bigd>"),
                                  decisions=_decisions(subfolder="contracts",
                                                       desc="osn-guarantee-signed"),
                                  content=same_bytes)
        r2 = af.process_email("harrison@hjrglobal.com", "m2",
                              content_ledger=ledger, seen_messages=seen)
        assert r2 == []                 # content already filed → skipped
        assert upload2.call_count == 0  # no second upload

    def test_md5_preseeded_skips_upload(self, tmp_path, monkeypatch):
        _set_ledger_paths(tmp_path, monkeypatch)
        content = b"already-on-drive"
        import hashlib
        md5 = hashlib.md5(content).hexdigest()
        upload = _patch_pipeline(monkeypatch, meta=_meta(), decisions=_decisions(),
                                 content=content)
        ledger = {md5: {"md5": md5, "drive_path": "x", "filed_at": int(time.time())}}
        res = af.process_email("harrison@hjrglobal.com", "m1",
                               content_ledger=ledger, seen_messages=set())
        assert res == []
        assert upload.call_count == 0

    def test_message_recorded_after_processing(self, tmp_path, monkeypatch):
        _set_ledger_paths(tmp_path, monkeypatch)
        _patch_pipeline(monkeypatch, meta=_meta(rfc="<rec@host>"), decisions=_decisions())
        seen = set()
        af.process_email("harrison@hjrglobal.com", "m1",
                         content_ledger={}, seen_messages=seen)
        assert "<rec@host>" in seen

    def test_empty_rfc_id_uses_gmail_fallback_key(self, tmp_path, monkeypatch):
        _set_ledger_paths(tmp_path, monkeypatch)
        _patch_pipeline(monkeypatch, meta=_meta(message_id="gidX", rfc=""),
                        decisions=_decisions())
        seen = set()
        af.process_email("harrison@hjrglobal.com", "gidX",
                         content_ledger={}, seen_messages=seen)
        assert "gmail:gidX" in seen


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic naming + dry-run
# ─────────────────────────────────────────────────────────────────────────────

class TestNamingAndDryRun:
    def test_filename_date_prefix_from_email_not_run_date(self, tmp_path, monkeypatch):
        from datetime import datetime, timezone
        _set_ledger_paths(tmp_path, monkeypatch)
        upload = _patch_pipeline(monkeypatch, meta=_meta(), decisions=_decisions())
        af.process_email("harrison@hjrglobal.com", "m1",
                         content_ledger={}, seen_messages=set())
        # upload_file(folder_id, canonical, content, mime, content_md5=...)
        canonical = upload.call_args.args[1]
        # Prefix must be the EMAIL's Date (a fixed past ts), never the run date.
        expected = datetime.fromtimestamp(_EMAIL_DATE_TS, tz=timezone.utc).strftime("%Y-%m-%d")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert expected != today  # guard: the fixture date is genuinely not today
        assert canonical == f"{expected}_osn_osn-guarantee.pdf"

    def test_dry_run_writes_nothing(self, tmp_path, monkeypatch):
        _set_ledger_paths(tmp_path, monkeypatch)
        upload = _patch_pipeline(monkeypatch, meta=_meta(), decisions=_decisions())
        seen = set()
        res = af.process_email("harrison@hjrglobal.com", "m1", dry_run=True,
                               content_ledger={}, seen_messages=seen)
        assert len(res) == 1 and res[0]["dry_run"] is True
        assert upload.call_count == 0
        assert seen == set()  # message NOT recorded in dry-run
        assert not (tmp_path / "content.jsonl").exists()
        assert not (tmp_path / "message.jsonl").exists()


# ─────────────────────────────────────────────────────────────────────────────
# process_account — budget + watermark robustness
# ─────────────────────────────────────────────────────────────────────────────

class TestProcessAccountBudget:
    def test_budget_hit_stops_before_processing(self, tmp_path, monkeypatch):
        _set_ledger_paths(tmp_path, monkeypatch)
        monkeypatch.setattr(af, "list_messages_with_attachments",
                            lambda u, ts: ["m1", "m2"])
        pe = MagicMock(return_value=[])
        monkeypatch.setattr(af, "process_email", pe)
        summary = af.process_account(
            {"email": "harrison@hjrglobal.com"}, {},
            content_ledger={}, seen_messages=set(),
            deadline=time.time() - 1,  # already past
        )
        assert summary["budget_hit"] is True
        assert pe.call_count == 0

    def test_list_failure_flags_account(self, tmp_path, monkeypatch):
        _set_ledger_paths(tmp_path, monkeypatch)
        def _boom(u, ts):
            raise af.GmailReaderError("403")
        monkeypatch.setattr(af, "list_messages_with_attachments", _boom)
        summary = af.process_account(
            {"email": "x@x.com"}, {}, content_ledger={}, seen_messages=set(),
        )
        assert summary["list_failed"] is True
        assert summary["errors"] == 1

    def test_per_message_error_does_not_flag_list_failed(self, tmp_path, monkeypatch):
        _set_ledger_paths(tmp_path, monkeypatch)
        monkeypatch.setattr(af, "list_messages_with_attachments", lambda u, ts: ["m1"])
        monkeypatch.setattr(af, "process_email",
                            MagicMock(side_effect=RuntimeError("boom")))
        summary = af.process_account(
            {"email": "x@x.com"}, {}, content_ledger={}, seen_messages=set(),
        )
        assert summary["errors"] == 1
        assert summary["list_failed"] is False  # must NOT freeze the watermark


# ─────────────────────────────────────────────────────────────────────────────
# run_filer — incremental per-account watermark persistence
# ─────────────────────────────────────────────────────────────────────────────

class TestRunFilerWatermarks:
    def _read_wm(self, path):
        return json.loads(path.read_text()) if path.exists() else {}

    def test_clean_account_advances_and_saves_watermark(self, tmp_path, monkeypatch):
        _set_ledger_paths(tmp_path, monkeypatch)
        wm_path = tmp_path / "wm.json"
        monkeypatch.setattr(af, "_WATERMARKS_PATH", wm_path)
        monkeypatch.setattr(af, "process_account", lambda acct, wm, **kw: {
            "email": acct["email"], "messages_scanned": 1, "filed": 1, "skipped": 0,
            "errors": 0, "filed_items": [], "list_failed": False, "budget_hit": False,
        })
        af.run_filer(accounts=[{"email": "a@x.com"}, {"email": "b@x.com"}])
        saved = self._read_wm(wm_path)
        assert "a@x.com" in saved and "b@x.com" in saved

    def test_list_failed_account_does_not_advance(self, tmp_path, monkeypatch):
        _set_ledger_paths(tmp_path, monkeypatch)
        wm_path = tmp_path / "wm.json"
        monkeypatch.setattr(af, "_WATERMARKS_PATH", wm_path)
        monkeypatch.setattr(af, "process_account", lambda acct, wm, **kw: {
            "email": acct["email"], "messages_scanned": 0, "filed": 0, "skipped": 0,
            "errors": 1, "filed_items": [], "list_failed": True, "budget_hit": False,
        })
        af.run_filer(accounts=[{"email": "fail@x.com"}])
        assert "fail@x.com" not in self._read_wm(wm_path)

    def test_budget_hit_account_does_not_advance(self, tmp_path, monkeypatch):
        _set_ledger_paths(tmp_path, monkeypatch)
        wm_path = tmp_path / "wm.json"
        monkeypatch.setattr(af, "_WATERMARKS_PATH", wm_path)
        monkeypatch.setattr(af, "process_account", lambda acct, wm, **kw: {
            "email": acct["email"], "messages_scanned": 5, "filed": 2, "skipped": 0,
            "errors": 0, "filed_items": [], "list_failed": False, "budget_hit": True,
        })
        af.run_filer(accounts=[{"email": "partial@x.com"}])
        assert "partial@x.com" not in self._read_wm(wm_path)


# ─────────────────────────────────────────────────────────────────────────────
# reconcile — seed ledger from existing Drive files
# ─────────────────────────────────────────────────────────────────────────────

class TestReconcile:
    def test_seeds_ledger_from_drive(self, tmp_path, monkeypatch):
        _set_ledger_paths(tmp_path, monkeypatch)

        def fake_resolve(segs):
            return "leaf" if segs == ["09-One-Stop-Nutrition", "legal"] else None

        def fake_list(folder_id):
            if folder_id == "leaf":
                return [{"id": "f1", "name": "x.pdf", "md5Checksum": "abc123",
                         "webViewLink": "u1"}]
            return []

        monkeypatch.setattr(af, "resolve_folder_path", fake_resolve)
        monkeypatch.setattr(af, "list_folder_files_with_md5", fake_list)

        stats = af.reconcile_ledger_from_drive(entities=["OSN"])
        assert stats["seeded"] == 1
        assert "abc123" in af.filer_ledger.load_content_ledger()

    def test_reconcile_then_live_run_skips(self, tmp_path, monkeypatch):
        """End-to-end: reconcile seeds md5, then the live email is deduped."""
        _set_ledger_paths(tmp_path, monkeypatch)
        content = b"the-canonical-bytes"
        import hashlib
        md5 = hashlib.md5(content).hexdigest()

        monkeypatch.setattr(af, "resolve_folder_path",
                            lambda segs: "leaf" if segs == ["09-One-Stop-Nutrition", "legal"] else None)
        monkeypatch.setattr(af, "list_folder_files_with_md5",
                            lambda fid: [{"id": "f1", "name": "x.pdf",
                                          "md5Checksum": md5, "webViewLink": "u"}] if fid == "leaf" else [])
        af.reconcile_ledger_from_drive(entities=["OSN"])

        upload = _patch_pipeline(monkeypatch, meta=_meta(), decisions=_decisions(),
                                 content=content)
        ledger = af.filer_ledger.load_content_ledger()
        res = af.process_email("harrison@hjrglobal.com", "m1",
                               content_ledger=ledger, seen_messages=set())
        assert res == []
        assert upload.call_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# drive_connector.upload_file — name + md5 dedup
# ─────────────────────────────────────────────────────────────────────────────

class _FakeDriveSvc:
    """Minimal Drive service: name-list returns empty, create returns a new file."""

    def __init__(self):
        self.created = []
        self._op = None

    def files(self):
        return self

    def list(self, **kw):
        self._op = ("list", kw)
        return self

    def create(self, **kw):
        self._op = ("create", kw)
        self.created.append(kw)
        return self

    def execute(self):
        op, _ = self._op
        if op == "list":
            return {"files": []}
        return {"id": "NEW", "webViewLink": "newlink"}


class TestUploadFileDedup:
    def test_name_match_skips_upload(self, monkeypatch):
        svc = MagicMock()
        # name query returns an existing file
        svc.files.return_value.list.return_value.execute.return_value = {
            "files": [{"id": "existing", "webViewLink": "u"}]
        }
        monkeypatch.setattr(dc, "_build_drive_service", lambda *a, **k: svc)
        fid, link = dc.upload_file("folder1", "x.pdf", b"bytes", "application/pdf")
        assert fid == "existing"
        svc.files.return_value.create.assert_not_called()

    def test_md5_match_under_different_name_skips_upload(self, monkeypatch):
        svc = _FakeDriveSvc()
        monkeypatch.setattr(dc, "_build_drive_service", lambda *a, **k: svc)
        monkeypatch.setattr(dc, "list_folder_files_with_md5",
                            lambda fid: [{"id": "dup", "name": "other.pdf",
                                          "md5Checksum": "abc", "webViewLink": "duplink"}])
        fid, link = dc.upload_file("folder1", "new-name.pdf", b"bytes",
                                   "application/pdf", content_md5="abc")
        assert fid == "dup" and link == "duplink"
        assert svc.created == []  # never uploaded

    def test_no_match_uploads(self, monkeypatch):
        svc = _FakeDriveSvc()
        monkeypatch.setattr(dc, "_build_drive_service", lambda *a, **k: svc)
        monkeypatch.setattr(dc, "list_folder_files_with_md5", lambda fid: [])
        fid, link = dc.upload_file("folder1", "fresh.pdf", b"bytes",
                                   "application/pdf", content_md5="zzz")
        assert fid == "NEW"
        assert len(svc.created) == 1
