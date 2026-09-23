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
    upload = MagicMock(
        side_effect=lambda folder_id, name, content_, mime, content_md5=None:
            ("file1", "https://drive/file1", name))
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
# Always-named-folder invariant (WS9): an invalid classification is SKIPPED,
# never filed to root / a limbo folder.
# ─────────────────────────────────────────────────────────────────────────────

class TestAlwaysNamedFolderInvariant:
    def test_unknown_entity_is_skipped_not_uploaded(self, tmp_path, monkeypatch):
        _set_ledger_paths(tmp_path, monkeypatch)
        upload = _patch_pipeline(
            monkeypatch, meta=_meta(),
            decisions=_decisions(entity="ACME", subfolder="legal"),
        )
        out = af.process_email("harrison@hjrglobal.com", "m1",
                               content_ledger={}, seen_messages=set())
        assert out == []
        assert upload.call_count == 0

    def test_unknown_subfolder_is_skipped_not_uploaded(self, tmp_path, monkeypatch):
        _set_ledger_paths(tmp_path, monkeypatch)
        upload = _patch_pipeline(
            monkeypatch, meta=_meta(),
            decisions=_decisions(entity="OSN", subfolder="todo"),
        )
        out = af.process_email("harrison@hjrglobal.com", "m1",
                               content_ledger={}, seen_messages=set())
        assert out == []
        assert upload.call_count == 0

    def test_valid_classification_targets_named_entity_folder(self, tmp_path, monkeypatch):
        _set_ledger_paths(tmp_path, monkeypatch)
        seen_segments = {}

        def _ensure(segs):
            seen_segments["segs"] = list(segs)
            return "folder-" + "/".join(segs)

        monkeypatch.setattr(af, "get_message", lambda u, m: {"id": m})
        monkeypatch.setattr(af, "parse_message_metadata", lambda msg: _meta())
        monkeypatch.setattr(af, "classify_attachments",
                            lambda meta_, atts, entity_hint=None: _decisions(entity="OSN", subfolder="legal"))
        monkeypatch.setattr(af, "download_attachment", lambda u, m, a: b"BYTES")
        monkeypatch.setattr(af, "ensure_folder_path", _ensure)
        upload = MagicMock(
            side_effect=lambda folder_id, name, content_, mime, content_md5=None:
                ("file1", "https://drive/file1", name))
        monkeypatch.setattr(af, "upload_file", upload)

        out = af.process_email("harrison@hjrglobal.com", "m1",
                               content_ledger={}, seen_messages=set())
        assert len(out) == 1
        # Folder is a named, non-empty 2-segment path under an entity folder.
        segs = seen_segments["segs"]
        assert len(segs) == 2 and all(s for s in segs)
        assert segs[1] == "legal"
        assert upload.call_count == 1


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


class _FakeDriveSvcNameHit(_FakeDriveSvc):
    """Like _FakeDriveSvc but the name-dedup list returns the given candidates."""

    def __init__(self, name_hits):
        super().__init__()
        self._name_hits = name_hits

    def execute(self):
        op, _ = self._op
        if op == "list":
            return {"files": self._name_hits}
        return {"id": "NEW", "webViewLink": "newlink"}


class TestUploadFileDedup:
    def test_name_match_skips_upload(self, monkeypatch):
        # No content_md5 (the finance_receipts path): ANY name hit is treated as
        # the same document — legacy semantics, pinned.
        svc = MagicMock()
        # name query returns an existing file
        svc.files.return_value.list.return_value.execute.return_value = {
            "files": [{"id": "existing", "webViewLink": "u"}]
        }
        monkeypatch.setattr(dc, "_build_drive_service", lambda *a, **k: svc)
        fid, link, name = dc.upload_file("folder1", "x.pdf", b"bytes", "application/pdf")
        assert fid == "existing"
        assert name == "x.pdf"
        svc.files.return_value.create.assert_not_called()

    def test_md5_match_under_different_name_skips_upload(self, monkeypatch):
        svc = _FakeDriveSvc()
        monkeypatch.setattr(dc, "_build_drive_service", lambda *a, **k: svc)
        monkeypatch.setattr(dc, "list_folder_files_with_md5",
                            lambda fid: [{"id": "dup", "name": "other.pdf",
                                          "md5Checksum": "abc", "webViewLink": "duplink"}])
        fid, link, name = dc.upload_file("folder1", "new-name.pdf", b"bytes",
                                         "application/pdf", content_md5="abc")
        assert fid == "dup" and link == "duplink"
        assert name == "other.pdf"  # truthful: the content lives under that name
        assert svc.created == []  # never uploaded

    def test_no_match_uploads(self, monkeypatch):
        svc = _FakeDriveSvc()
        monkeypatch.setattr(dc, "_build_drive_service", lambda *a, **k: svc)
        monkeypatch.setattr(dc, "list_folder_files_with_md5", lambda fid: [])
        fid, link, name = dc.upload_file("folder1", "fresh.pdf", b"bytes",
                                         "application/pdf", content_md5="zzz")
        assert fid == "NEW"
        assert name == "fresh.pdf"
        assert len(svc.created) == 1

    # cq-349a17012ddf: md5-verify inside the name dedup ------------------------

    def test_name_hit_md5_match_skips_upload(self, monkeypatch):
        svc = _FakeDriveSvcNameHit(
            [{"id": "existing", "webViewLink": "u", "md5Checksum": "abc"}])
        monkeypatch.setattr(dc, "_build_drive_service", lambda *a, **k: svc)
        fid, link, name = dc.upload_file("folder1", "x.pdf", b"bytes",
                                         "application/pdf", content_md5="abc")
        assert fid == "existing"
        assert name == "x.pdf"
        assert svc.created == []

    def test_name_hit_md5_match_scans_all_candidates(self, monkeypatch):
        # The matching candidate is NOT first — must dedup to IT, never existing[0].
        svc = _FakeDriveSvcNameHit([
            {"id": "wrong", "webViewLink": "w", "md5Checksum": "OTHER"},
            {"id": "right", "webViewLink": "r", "md5Checksum": "abc"},
        ])
        monkeypatch.setattr(dc, "_build_drive_service", lambda *a, **k: svc)
        fid, link, name = dc.upload_file("folder1", "x.pdf", b"bytes",
                                         "application/pdf", content_md5="abc")
        assert fid == "right" and link == "r"
        assert svc.created == []

    def test_name_hit_md5_mismatch_uploads_uniquified(self, monkeypatch):
        # The cross-message collision class: same name, DIFFERENT bytes must
        # upload as a distinct file under a uniquified name — never alias.
        svc = _FakeDriveSvcNameHit(
            [{"id": "existing", "webViewLink": "u", "md5Checksum": "OTHER"}])
        monkeypatch.setattr(dc, "_build_drive_service", lambda *a, **k: svc)
        monkeypatch.setattr(dc, "list_folder_files_with_md5",
                            lambda fid: [{"id": "existing", "name": "x.pdf",
                                          "md5Checksum": "OTHER", "webViewLink": "u"}])
        fid, link, name = dc.upload_file("folder1", "x.pdf", b"different-bytes",
                                         "application/pdf", content_md5="abc")
        assert fid == "NEW"
        assert name == "x-2.pdf"
        assert len(svc.created) == 1
        assert svc.created[0]["body"]["name"] == "x-2.pdf"

    def test_name_hit_md5_mismatch_uniquify_skips_taken_suffixes(self, monkeypatch):
        # x.pdf AND x-2.pdf already exist with other content -> lands on x-3.pdf.
        svc = _FakeDriveSvcNameHit(
            [{"id": "existing", "webViewLink": "u", "md5Checksum": "OTHER"}])
        monkeypatch.setattr(dc, "_build_drive_service", lambda *a, **k: svc)
        monkeypatch.setattr(dc, "list_folder_files_with_md5",
                            lambda fid: [
                                {"id": "existing", "name": "x.pdf",
                                 "md5Checksum": "OTHER", "webViewLink": "u"},
                                {"id": "e2", "name": "x-2.pdf",
                                 "md5Checksum": "OTHER2", "webViewLink": "u2"},
                            ])
        fid, link, name = dc.upload_file("folder1", "x.pdf", b"different-bytes",
                                         "application/pdf", content_md5="abc")
        assert fid == "NEW"
        assert name == "x-3.pdf"

    def test_name_hit_mismatch_still_md5_dedups_against_folder(self, monkeypatch):
        # Name collision with different bytes, but the SAME content already
        # exists under another name -> layer 2 wins, no upload.
        svc = _FakeDriveSvcNameHit(
            [{"id": "existing", "webViewLink": "u", "md5Checksum": "OTHER"}])
        monkeypatch.setattr(dc, "_build_drive_service", lambda *a, **k: svc)
        monkeypatch.setattr(dc, "list_folder_files_with_md5",
                            lambda fid: [{"id": "dup", "name": "renamed.pdf",
                                          "md5Checksum": "abc", "webViewLink": "duplink"}])
        fid, link, name = dc.upload_file("folder1", "x.pdf", b"bytes",
                                         "application/pdf", content_md5="abc")
        assert fid == "dup" and name == "renamed.pdf"
        assert svc.created == []


# ─────────────────────────────────────────────────────────────────────────────
# cq-f583932a625e: within-message collision uniquify + truthful collapse
# reporting + digest dedup
# ─────────────────────────────────────────────────────────────────────────────

def _multi_meta(n=4, message_id="mm1", rfc="<multi@host>"):
    m = _meta(message_id=message_id, rfc=rfc, subject="4 store invoices")
    m["attachments"] = [
        {"filename": f"store{i}.pdf", "mime_type": "application/pdf",
         "size": 100000, "attachment_id": f"att{i}", "data": None}
        for i in range(n)
    ]
    return m


def _multi_decisions(n=4, desc="american-fitness-az-invoice"):
    # The 7/27 shape: N sibling attachments, IDENTICAL description slug.
    return [{"action": "file", "entity": "OSN", "subfolder": "invoices",
             "description": desc, "filename": f"store{i}.pdf",
             "reason": f"invoice for store {i}"} for i in range(n)]


class TestSiblingCollisionUniquify:
    def test_same_slug_siblings_upload_as_distinct_files(self, tmp_path, monkeypatch):
        """The 7/27 regression shape: 4 different-bytes attachments classified to
        ONE slug must produce 4 uploads under 4 DISTINCT canonical names and 4
        results rows with distinct file_ids -- never a silent collapse that
        discards 3 real invoices."""
        _set_ledger_paths(tmp_path, monkeypatch)
        meta = _multi_meta(4)
        monkeypatch.setattr(af, "get_message", lambda u, m: {"id": m})
        monkeypatch.setattr(af, "parse_message_metadata", lambda msg: meta)
        monkeypatch.setattr(af, "classify_attachments",
                            lambda meta_, atts, entity_hint=None: _multi_decisions(4))
        # Different bytes per attachment (different md5s -> content ledger passes all).
        monkeypatch.setattr(af, "download_attachment",
                            lambda u, m, a: f"BYTES-{a['attachment_id']}".encode())
        monkeypatch.setattr(af, "ensure_folder_path", lambda segs: "fold")
        uploaded_names: list[str] = []

        def fake_upload(folder_id, name, content, mime, content_md5=None):
            uploaded_names.append(name)
            return (f"file-{len(uploaded_names)}", f"https://drive/{len(uploaded_names)}", name)

        monkeypatch.setattr(af, "upload_file", fake_upload)
        results = af.process_email("harrison@hjrglobal.com", "mm1",
                                   content_ledger={}, seen_messages=set())
        assert len(results) == 4
        assert len(set(uploaded_names)) == 4          # all canonical names distinct
        assert len({r["file_id"] for r in results}) == 4
        # Uniquified names keep the extension.
        assert all(n.endswith(".pdf") for n in uploaded_names)

    def test_cross_message_collision_uniquified_name_kept_truthful(self, tmp_path, monkeypatch):
        """cq-349a17012ddf acceptance shape: when upload_file uniquifies a
        cross-message name collision (same canonical, DIFFERENT bytes), the
        ledger row + digest row must carry the name that actually landed in
        Drive, and the filing must be reported as a real new archive."""
        _set_ledger_paths(tmp_path, monkeypatch)
        upload = _patch_pipeline(monkeypatch, meta=_meta(), decisions=_decisions(),
                                 content=b"SECOND-DOC-DIFFERENT-BYTES")
        # Simulate the new upload_file contract: the requested name collided
        # with different content, so the file landed under a -2 suffix.
        def _uniquified(folder_id, name, content_, mime, content_md5=None):
            stem, dot, ext = name.rpartition(".")
            return ("file-2", "https://drive/2", f"{stem}-2.{ext}" if dot else f"{name}-2")
        upload.side_effect = _uniquified
        ledger: dict = {}
        results = af.process_email("harrison@hjrglobal.com", "m1",
                                   content_ledger=ledger, seen_messages=set())
        assert len(results) == 1
        row = results[0]
        assert row["file_id"] == "file-2"
        assert row["canonical_filename"].endswith("-2.pdf")
        assert row["drive_path"].endswith(row["canonical_filename"])
        # Ledger row is truthful to the Drive name too.
        import hashlib
        md5 = hashlib.md5(b"SECOND-DOC-DIFFERENT-BYTES").hexdigest()
        assert ledger[md5]["canonical"].endswith("-2.pdf")
        assert ledger[md5]["drive_path"].endswith("-2.pdf")

    def test_cross_message_collapse_not_reported_as_new_filing(self, tmp_path, monkeypatch):
        """upload_file returning an ALREADY-LEDGERED file_id (a name/md5 collapse
        onto an existing Drive file) must record the md5 alias but emit NO results
        row -- the digest previously re-listed the same file with a fresh AI
        description."""
        _set_ledger_paths(tmp_path, monkeypatch)
        upload = _patch_pipeline(monkeypatch, meta=_meta(), decisions=_decisions(),
                                 content=b"NEW-BYTES-SAME-NAME")
        upload.side_effect = (
            lambda folder_id, name, content_, mime, content_md5=None:
                ("file-EXISTING", "https://drive/existing", name))
        ledger = {"aaaa": {"md5": "aaaa", "file_id": "file-EXISTING",
                           "drive_path": "x", "filed_at": int(time.time())}}
        results = af.process_email("harrison@hjrglobal.com", "m1",
                                   content_ledger=ledger, seen_messages=set())
        assert results == []                          # no digest row
        # The alias was still recorded so re-sends keep deduping.
        import hashlib
        md5 = hashlib.md5(b"NEW-BYTES-SAME-NAME").hexdigest()
        assert md5 in ledger and ledger[md5]["file_id"] == "file-EXISTING"


class TestDigestDedup:
    def _summaries(self):
        item = lambda fid, reason: {  # noqa: E731
            "canonical_filename": f"{fid}.pdf", "drive_path": f"HJR/x/{fid}.pdf",
            "web_link": f"https://drive/{fid}", "file_id": fid, "reason": reason,
        }
        return [{
            "email": "a@x.com", "filed": 3,
            "filed_items": [item("F1", "first description"),
                            item("F1", "second different description"),
                            item("F1", "third different description")],
        }, {
            "email": "b@x.com", "filed": 1,
            "filed_items": [item("F2", "unique file")],
        }]

    def test_one_digest_line_per_drive_file_first_description_wins(self, monkeypatch):
        posted = {}

        class FakeClient:
            def __init__(self, token):
                pass

            def chat_postMessage(self, channel, text):
                posted["text"] = text

        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setattr(af, "_SlackWebClient", FakeClient)
        assert af.post_slack_summary(self._summaries()) is True
        text = posted["text"]
        assert text.count("https://drive/F1") == 1     # one line per file
        assert "first description" in text
        assert "second different description" not in text
        assert "2 file(s) archived" in text            # honest deduped headline

    def test_all_duplicates_skips_post(self, monkeypatch):
        # If dedup empties everything (all rows repeat one already-listed key
        # within the run), the headline count reflects the deduped truth.
        posted = {}

        class FakeClient:
            def __init__(self, token):
                pass

            def chat_postMessage(self, channel, text):
                posted["text"] = text

        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setattr(af, "_SlackWebClient", FakeClient)
        summaries = self._summaries()
        summaries[1]["filed_items"] = []               # only the F1 triplet remains
        af.post_slack_summary(summaries)
        assert posted["text"].count("F1.pdf") == 1
        assert "1 file(s) archived" in posted["text"]


# ─────────────────────────────────────────────────────────────────────────────
# S6c (Code #14, cq-a5b3e6a2e844): truncated classifier JSON -> scaled budget,
# ONE compact retry, then a message-ledger quarantine surfaced as a COUNT.
# Synthetic filenames only (never the live LEX ones from the 9/18 log).
# ─────────────────────────────────────────────────────────────────────────────

from types import SimpleNamespace  # noqa: E402

_TRUNCATED = ('{\n  "attachments": [\n    {\n      "filename": "a.pdf",\n'
              '      "action": "file",\n      "description": "end-of-business-statem')
_GOOD = ('{"attachments":[{"filename":"a.pdf","action":"file","entity":"OSN",'
         '"subfolder":"reports","description":"monthly report","reason":"a report"}]}')


def _resp(text, stop_reason="end_turn"):
    return SimpleNamespace(content=[SimpleNamespace(text=text)], stop_reason=stop_reason,
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1))


class _FakeAnthropic:
    """Scripted messages.create; records every call's kwargs."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        self.calls.append(kw)
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]


def _install_fake_claude(monkeypatch, replies):
    fake = _FakeAnthropic(replies)
    monkeypatch.setattr(af.anthropic, "Anthropic", lambda **_kw: fake)
    import cora.llm_usage as lu
    monkeypatch.setattr(lu, "log_usage", lambda *a, **k: None)
    return fake


def _atts(n):
    return [{"filename": f"file{i}.pdf", "mime_type": "application/pdf", "size": 200000,
             "attachment_id": f"a{i}", "data": None} for i in range(n)]


class TestClassifierRetry:
    def test_first_attempt_max_tokens_scales_with_attachment_count(self, monkeypatch):
        fake = _install_fake_claude(monkeypatch, [_resp(_GOOD)])
        af.classify_attachments(_meta(), _atts(13))
        assert fake.calls[0]["max_tokens"] >= 2592
        assert af._first_attempt_max_tokens(1) == 672
        assert af._first_attempt_max_tokens(40) == 4096          # capped

    def test_truncated_then_compact_retry_succeeds(self, monkeypatch):
        fake = _install_fake_claude(
            monkeypatch, [_resp(_TRUNCATED, "max_tokens"), _resp(_GOOD)])
        decisions = af.classify_attachments(_meta(), _atts(2))
        assert decisions[0]["description"] == "monthly report"
        assert len(fake.calls) == 2
        assert fake.calls[1]["max_tokens"] == 8192 > fake.calls[0]["max_tokens"]
        retry_text = fake.calls[1]["messages"][0]["content"]
        assert "MINIFIED" in retry_text and "6 words" in retry_text and "10 words" in retry_text
        assert "MINIFIED" not in fake.calls[0]["messages"][0]["content"]

    def test_invalid_json_without_truncation_also_retries(self, monkeypatch):
        fake = _install_fake_claude(monkeypatch, [_resp("not json at all"), _resp(_GOOD)])
        assert af.classify_attachments(_meta(), _atts(1))
        assert len(fake.calls) == 2

    def test_two_failures_raise_unparseable(self, monkeypatch):
        fake = _install_fake_claude(monkeypatch, [_resp(_TRUNCATED, "max_tokens")])
        import pytest
        with pytest.raises(af.ClassificationUnparseable) as ei:
            af.classify_attachments(_meta(), _atts(13))
        assert isinstance(ei.value, af.AttachmentFilerError)      # old handlers still catch it
        assert len(fake.calls) == 2                               # exactly ONE retry

    def test_unusable_reply_is_never_logged_raw(self, monkeypatch, caplog):
        _install_fake_claude(monkeypatch, [_resp('{"SYNTHETIC-RAW-MARKER": ')])
        import logging
        import pytest
        with caplog.at_level(logging.DEBUG, logger="cora.connectors.attachment_filer"):
            with pytest.raises(af.ClassificationUnparseable):
                af.classify_attachments(_meta(subject="SYNTHETIC-SUBJECT-MARKER"), _atts(1))
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "SYNTHETIC-RAW-MARKER" not in logged
        assert "SYNTHETIC-SUBJECT-MARKER" not in logged
        assert "len=" in logged                                   # shape hint only


def _message_rows(tmp_path):
    p = tmp_path / "message.jsonl"
    if not p.exists():
        return []
    rows = [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]
    return [r for r in rows if "msg_key" in r]


def _patch_unparseable_pipeline(monkeypatch, meta):
    """Real process_email + REAL classify_attachments, Claude always truncating."""
    monkeypatch.setattr(af, "get_message", lambda u, m: {"id": m})
    monkeypatch.setattr(af, "parse_message_metadata", lambda msg: meta)
    upload = MagicMock()
    monkeypatch.setattr(af, "upload_file", upload)
    fake = _install_fake_claude(monkeypatch, [_resp(_TRUNCATED, "max_tokens")])
    return fake, upload


class TestUnparseableQuarantine:
    def test_unparseable_quarantined_in_message_ledger(self, tmp_path, monkeypatch):
        _set_ledger_paths(tmp_path, monkeypatch)
        _, upload = _patch_unparseable_pipeline(monkeypatch, _meta())
        outcome: dict = {}
        res = af.process_email("payables@hjrglobal.com", "m1", content_ledger={},
                               seen_messages=set(), outcome=outcome)
        assert res == [] and outcome == {"unparseable": True}
        assert upload.call_count == 0
        (row,) = _message_rows(tmp_path)
        assert row["reason"] == "classification_unparseable"
        assert row["filed"] == 0 and row["msg_key"] == "<msg1@host>"

    def test_quarantine_lex_row_carries_no_subject(self, tmp_path, monkeypatch):
        _set_ledger_paths(tmp_path, monkeypatch)
        _patch_unparseable_pipeline(monkeypatch, _meta(subject="Synthetic statement"))
        af.process_email("payables@example.com", "m1", entity_hint="LEX",
                         content_ledger={}, seen_messages=set())
        (row,) = _message_rows(tmp_path)
        assert row["subject"] == "" and row["reason"] == "classification_unparseable"

    def test_quarantine_dedups_alias_copies_in_same_run_and_later_runs(self, tmp_path, monkeypatch):
        _set_ledger_paths(tmp_path, monkeypatch)
        fake, _ = _patch_unparseable_pipeline(monkeypatch, _meta())   # same rfc id in both boxes
        seen: set = set()
        af.process_email("payables@hjrglobal.com", "g1", content_ledger={}, seen_messages=seen)
        af.process_email("receipts@hjrglobal.com", "g2", content_ledger={}, seen_messages=seen)
        assert len(fake.calls) == 2                               # not 4
        from cora.connectors import filer_ledger
        assert "<msg1@host>" in filer_ledger.load_message_ledger()  # a later run skips too

    def test_quarantine_dry_run_writes_no_ledger_row(self, tmp_path, monkeypatch):
        _set_ledger_paths(tmp_path, monkeypatch)
        _patch_unparseable_pipeline(monkeypatch, _meta())
        seen: set = set()
        outcome: dict = {}
        af.process_email("payables@hjrglobal.com", "m1", dry_run=True, content_ledger={},
                         seen_messages=seen, outcome=outcome)
        assert outcome == {"unparseable": True}
        assert _message_rows(tmp_path) == [] and seen == set()

    def test_process_account_counts_unparseable_not_skipped(self, tmp_path, monkeypatch):
        _set_ledger_paths(tmp_path, monkeypatch)
        _patch_unparseable_pipeline(monkeypatch, _meta())
        monkeypatch.setattr(af, "list_messages_with_attachments", lambda u, ts: ["m1"])
        summary = af.process_account({"email": "payables@hjrglobal.com"}, {},
                                     content_ledger={}, seen_messages=set())
        assert summary["unparseable"] == 1 and summary["skipped"] == 0

    def test_record_message_done_without_reason_is_unchanged(self, tmp_path, monkeypatch):
        _set_ledger_paths(tmp_path, monkeypatch)
        from cora.connectors import filer_ledger
        filer_ledger.record_message_done(set(), "<k@h>", filed=1, subject="s")
        (row,) = _message_rows(tmp_path)
        assert set(row) == {"msg_key", "filed", "skipped", "subject", "filed_at"}


class TestDigestUnparseable:
    def _fake(self, monkeypatch):
        posted: dict = {}

        class FakeClient:
            def __init__(self, token):
                pass

            def chat_postMessage(self, channel, text):
                posted["text"] = text

        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        monkeypatch.setattr(af, "_SlackWebClient", FakeClient)
        return posted

    def test_digest_surfaces_unparseable_count_even_with_zero_filed(self, monkeypatch):
        posted = self._fake(monkeypatch)
        summaries = [{"email": "payables@hjrglobal.com", "filed": 0, "filed_items": [],
                      "unparseable": 1, "subject": "SYNTHETIC-SUBJECT-MARKER"}]
        assert af.post_slack_summary(summaries) is True
        assert "1 message(s) quarantined" in posted["text"]
        assert "SYNTHETIC-SUBJECT-MARKER" not in posted["text"]
        assert "payables@hjrglobal.com" not in posted["text"]      # count only, no rows

    def test_digest_nothing_filed_nothing_quarantined_still_skips(self, monkeypatch):
        posted = self._fake(monkeypatch)
        assert af.post_slack_summary([{"email": "a@x.com", "filed_items": [],
                                       "unparseable": 0}]) is True
        assert posted == {}
