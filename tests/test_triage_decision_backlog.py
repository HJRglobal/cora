"""Fork 4 backfill: one-shot decision_capture backlog triage
(scripts/triage_decision_backlog.py). Dry-run default, stale-dismiss,
auto-expired re-arm, LEX/PHI never re-armed, fingerprint-abort."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

try:
    import triage_decision_backlog as tdb
    _IMPORT_OK = True
except Exception:  # noqa: BLE001
    _IMPORT_OK = False

pytestmark = pytest.mark.skipif(not _IMPORT_OK,
                                reason="cora imports unavailable on this mount")


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _row(uid, *, days_ago=30.0, state="PENDING", utype="decision_capture",
         desc="[F3E] Decision: something operational.", **over):
    r = {
        "update_id": uid, "update_type": utype, "description": desc,
        "payload": {}, "source_evidence": "", "confidence": "HIGH",
        "state": state, "proposed_at": _iso(days_ago), "resolved_at": None,
        "dm_message_ts": "", "dm_channel_id": "",
    }
    r.update(over)
    return r


def _write(path: Path, rows) -> None:
    path.write_text(
        "\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows) + "\n",
        encoding="utf-8")


def _run(ledger, tmp_path, *extra):
    return tdb.main(["--ledger", str(ledger), "--archive",
                     str(tmp_path / "no-archive.jsonl"),
                     "--manifest-dir", str(tmp_path)] + list(extra))


class TestDryRun:
    def test_dry_run_writes_nothing_but_manifests(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        _write(ledger, [_row("old", days_ago=30), _row("new", days_ago=2)])
        before = ledger.read_bytes()
        assert _run(ledger, tmp_path) == 0
        assert ledger.read_bytes() == before
        manifests = list(tmp_path.glob("triage-decision-backlog-manifest-*.json"))
        assert len(manifests) == 1
        m = json.loads(manifests[0].read_text(encoding="utf-8"))
        assert m["mode"] == "dry-run"
        assert m["dismiss_total"] == 1
        assert m["dismissed_update_ids"] == ["old"]
        assert m["keep_pending_total"] == 1

    def test_stale_floor_refused_without_force(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        _write(ledger, [_row("a")])
        assert _run(ledger, tmp_path, "--stale-days", "1") == 1


class TestApply:
    def test_apply_dismisses_stale_keeps_recent_and_foreign_types(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        _write(ledger, [
            _row("old", days_ago=30),                              # -> DISMISSED
            _row("new", days_ago=2),                               # kept (recent)
            _row("dmd", days_ago=40, dm_message_ts="1.2"),         # kept (surfaced)
            _row("op", days_ago=40, utype="hubspot_note"),         # kept (not our type)
            _row("done", days_ago=40, state="APPROVED"),           # kept (resolved)
            "{malformed line",                                     # preserved
        ])
        assert _run(ledger, tmp_path, "--apply") == 0
        lines = ledger.read_text(encoding="utf-8").splitlines()
        assert "{malformed line" in lines
        rows = {json.loads(l)["update_id"]: json.loads(l)
                for l in lines if l.strip() and not l.startswith("{malformed")}
        assert rows["old"]["state"] == "DISMISSED"
        assert rows["old"]["resolved_reason"] == "fork4_backfill_stale"
        for keep in ("new", "dmd", "op"):
            assert rows[keep]["state"] == "PENDING", keep
        assert rows["done"]["state"] == "APPROVED"
        baks = list(tmp_path.glob("ledger.jsonl.bak-*"))
        assert len(baks) == 1

    def test_rearm_recovers_auto_expired_only(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        _write(ledger, [
            _row("auto", days_ago=10, state="DISMISSED",
                 resolved_reason="expired_unrouted", resolved_at=_iso(2),
                 dm_message_ts=""),
            _row("harrison", days_ago=10, state="DISMISSED",
                 resolved_reason="one_tap_button", resolved_at=_iso(2)),
            _row("routed", days_ago=10, state="DISMISSED",
                 resolved_reason="routed_to_owner:U123", resolved_at=_iso(2)),
            _row("ancient", days_ago=60, state="DISMISSED",
                 resolved_reason="expired_unrouted", resolved_at=_iso(30)),
            _row("lex", days_ago=10, state="DISMISSED",
                 resolved_reason="expired_unrouted", resolved_at=_iso(2),
                 desc="[LEX-LLC] Decision: schedule change."),
        ])
        assert _run(ledger, tmp_path, "--apply", "--rearm") == 0
        rows = {json.loads(l)["update_id"]: json.loads(l)
                for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()}
        assert rows["auto"]["state"] == "PENDING"
        assert rows["auto"]["rearm_reason"] == "fork4_backfill_rearm"
        assert rows["auto"]["dm_message_ts"] == ""
        # Harrison-resolved, out-of-window, and LEX rows stay DISMISSED
        for stay in ("harrison", "routed", "ancient", "lex"):
            assert rows[stay]["state"] == "DISMISSED", stay

    def test_rearm_without_flag_is_inert(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        _write(ledger, [
            _row("auto", days_ago=10, state="DISMISSED",
                 resolved_reason="expired_unrouted", resolved_at=_iso(2)),
            _row("old", days_ago=30),  # something to apply so the rewrite runs
        ])
        assert _run(ledger, tmp_path, "--apply") == 0
        rows = {json.loads(l)["update_id"]: json.loads(l)
                for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()}
        assert rows["auto"]["state"] == "DISMISSED"

    def test_rearm_recovers_from_archive_too(self, tmp_path):
        """D-051 (rearm-window-defeated-by-3d-archive-rotation): rotate_resolved
        moves auto-dismissed rows to the archive after ~3d; --rearm must scan it
        and move recoverable rows back to the live ledger."""
        ledger = tmp_path / "ledger.jsonl"
        archive = tmp_path / "archive.jsonl"
        _write(ledger, [_row("live-old", days_ago=30)])  # something to apply
        _write(archive, [
            _row("arch-auto", days_ago=10, state="DISMISSED",
                 resolved_reason="expired_unrouted", resolved_at=_iso(2)),
            _row("arch-harrison", days_ago=10, state="DISMISSED",
                 resolved_reason="one_tap_button", resolved_at=_iso(2)),
            _row("arch-ancient", days_ago=60, state="DISMISSED",
                 resolved_reason="expired_unrouted", resolved_at=_iso(30)),
            _row("arch-lex", days_ago=10, state="DISMISSED",
                 resolved_reason="expired_unrouted", resolved_at=_iso(2),
                 desc="[LEX-LLC] Decision: schedule change."),
        ])
        rc = tdb.main(["--ledger", str(ledger), "--archive", str(archive),
                       "--manifest-dir", str(tmp_path), "--apply", "--rearm"])
        assert rc == 0
        live = {json.loads(l)["update_id"]: json.loads(l)
                for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()}
        # the archived auto-expired row moved to live as PENDING
        assert live["arch-auto"]["state"] == "PENDING"
        assert live["arch-auto"]["rearm_reason"] == "fork4_backfill_rearm"
        # ...and left the archive; the others stayed archived
        arch = {json.loads(l)["update_id"]: json.loads(l)
                for l in archive.read_text(encoding="utf-8").splitlines() if l.strip()}
        assert "arch-auto" not in arch
        for stay in ("arch-harrison", "arch-ancient", "arch-lex"):
            assert arch[stay]["state"] == "DISMISSED", stay
        assert list(tmp_path.glob("archive.jsonl.bak-*"))  # archive .bak taken

    def test_archive_rearm_never_duplicates_live_uid(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        archive = tmp_path / "archive.jsonl"
        _write(ledger, [_row("dup", days_ago=2),          # live PENDING copy
                        _row("live-old", days_ago=30)])   # something to apply
        _write(archive, [
            _row("dup", days_ago=10, state="DISMISSED",
                 resolved_reason="expired_unrouted", resolved_at=_iso(2)),
        ])
        rc = tdb.main(["--ledger", str(ledger), "--archive", str(archive),
                       "--manifest-dir", str(tmp_path), "--apply", "--rearm"])
        assert rc == 0
        uids = [json.loads(l)["update_id"]
                for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert uids.count("dup") == 1  # never resurrected alongside the live copy

    def test_nothing_to_do_leaves_ledger_untouched(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        _write(ledger, [_row("new", days_ago=1)])
        before = ledger.read_bytes()
        assert _run(ledger, tmp_path, "--apply") == 0
        assert ledger.read_bytes() == before
        assert not list(tmp_path.glob("ledger.jsonl.bak-*"))


class TestPredicates:
    def test_stale_pending_predicate(self):
        cutoff = datetime.now(timezone.utc) - timedelta(days=14)
        assert tdb._is_stale_pending(_row("a", days_ago=20), cutoff) is True
        assert tdb._is_stale_pending(_row("b", days_ago=2), cutoff) is False
        assert tdb._is_stale_pending(
            _row("c", days_ago=20, dm_message_ts="1.1"), cutoff) is False
        assert tdb._is_stale_pending(
            _row("d", days_ago=20, utype="asana_task"), cutoff) is False
        assert tdb._is_stale_pending(
            _row("e", days_ago=20, proposed_at="not-a-date"), cutoff) is False

    def test_rearmable_screens_lex_phi_fail_closed(self, monkeypatch):
        rearm_cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        clean = _row("a", state="DISMISSED", resolved_reason="expired_unrouted",
                     resolved_at=_iso(2))
        assert tdb._is_rearmable(clean, rearm_cutoff) == (True, "")
        phi = _row("b", state="DISMISSED", resolved_reason="expired_unrouted",
                   resolved_at=_iso(2),
                   desc="Decision: revise the patient care plan template.")
        ok, why = tdb._is_rearmable(phi, rearm_cutoff)
        assert ok is False and why == "phi"
        # screen import/exec failure -> fail closed
        import cora.decision_inbox as dimod
        monkeypatch.setattr(dimod, "screen_decision",
                            lambda u: (_ for _ in ()).throw(RuntimeError("x")))
        ok, why = tdb._is_rearmable(clean, rearm_cutoff)
        assert ok is False and why == "screen_error"
