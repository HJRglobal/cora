"""Tests for scripts/expire_stale_operational_updates.py (Fold 3, 2026-07-10).

Covers the stale-operational bulk-expiry: the _should_expire predicate (allowlist,
age cutoff, dm_message_ts skip, protected/knowledge exclusion, fail-safe), dry-run
(manifest + sensitivity table, ledger untouched), --apply (.bak, DISMISSED/
expired_bulk flip, non-target rows untouched, malformed preserved), the
concurrent-append fingerprint ABORT, and the protected-type refusal.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import expire_stale_operational_updates as mod  # noqa: E402


def _iso(days_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def _row(**over) -> dict:
    r = {
        "update_id": over.get("update_id", "id-x"),
        "update_type": "hubspot_note",
        "description": "some operational nudge",
        "payload": {},
        "state": "PENDING",
        "proposed_at": _iso(20),
        "resolved_at": None,
        "dm_message_ts": "",
        "dm_channel_id": "",
    }
    r.update(over)
    return r


def _write_ledger(path: Path, rows: list) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for r in rows:
            if isinstance(r, str):
                fh.write(r + "\n")
            else:
                fh.write(json.dumps(r) + "\n")


# ── _should_expire ────────────────────────────────────────────────────────────

class TestShouldExpire:
    def _cutoff(self):
        return datetime.now(timezone.utc) - timedelta(days=14)

    TARGETS = frozenset(mod._DEFAULT_EXPIRE_TYPES)

    def test_stale_operational_pending_expires(self):
        assert mod._should_expire(_row(proposed_at=_iso(20)), self.TARGETS, self._cutoff()) is True

    def test_recent_row_kept(self):
        assert mod._should_expire(_row(proposed_at=_iso(3)), self.TARGETS, self._cutoff()) is False

    def test_dm_surfaced_row_kept(self):
        assert mod._should_expire(_row(dm_message_ts="123.456"), self.TARGETS, self._cutoff()) is False

    def test_non_pending_kept(self):
        assert mod._should_expire(_row(state="DISMISSED"), self.TARGETS, self._cutoff()) is False
        assert mod._should_expire(_row(state="APPROVED"), self.TARGETS, self._cutoff()) is False

    def test_protected_type_kept(self):
        for t in ("known_answer", "efficiency", "generic", "founder"):
            assert mod._should_expire(_row(update_type=t), self.TARGETS, self._cutoff()) is False

    def test_non_target_type_kept(self):
        assert mod._should_expire(_row(update_type="something_else"), self.TARGETS, self._cutoff()) is False

    def test_info_for_cora_generic_kept(self):
        r = _row(update_type="generic", payload={"source": "info-for-cora"})
        assert mod._should_expire(r, self.TARGETS, self._cutoff()) is False

    def test_unparseable_proposed_at_kept(self):
        assert mod._should_expire(_row(proposed_at="not-a-date"), self.TARGETS, self._cutoff()) is False

    def test_all_four_target_types_expire(self):
        for t in mod._DEFAULT_EXPIRE_TYPES:
            assert mod._should_expire(_row(update_type=t, proposed_at=_iso(20)),
                                      self.TARGETS, self._cutoff()) is True


# ── dry-run ────────────────────────────────────────────────────────────────────

class TestDryRun:
    def test_dry_run_writes_manifest_leaves_ledger(self, tmp_path, capsys):
        ledger = tmp_path / "ledger.jsonl"
        rows = [
            _row(update_id="a", update_type="hubspot_note", proposed_at=_iso(20)),   # expire
            _row(update_id="b", update_type="asana_task", proposed_at=_iso(3)),      # recent
            _row(update_id="c", update_type="known_answer", proposed_at=_iso(30)),   # protected
        ]
        _write_ledger(ledger, rows)
        before = ledger.read_bytes()
        rc = mod.main(["--ledger", str(ledger), "--manifest-dir", str(tmp_path),
                       "--cutoff-days", "14"])
        assert rc == 0
        assert ledger.read_bytes() == before  # untouched
        manifests = list(tmp_path.glob("expire-stale-operational-manifest-*.json"))
        assert len(manifests) == 1
        m = json.loads(manifests[0].read_text(encoding="utf-8"))
        assert m["expire_total"] == 1
        assert m["expired_update_ids"] == ["a"]
        assert m["resolved_reason"] == "expired_bulk"
        assert m["terminal_state"] == "DISMISSED"
        out = capsys.readouterr().out
        assert "Cutoff sensitivity" in out
        assert "DRY-RUN" in out

    def test_sensitivity_counts(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        rows = [
            _row(update_id="d8", proposed_at=_iso(8)),    # >7d only
            _row(update_id="d15", proposed_at=_iso(15)),  # >7,10,14
            _row(update_id="d25", proposed_at=_iso(25)),  # >7,10,14,21
        ]
        _write_ledger(ledger, rows)
        mod.main(["--ledger", str(ledger), "--manifest-dir", str(tmp_path)])
        m = json.loads(list(tmp_path.glob("*.json"))[0].read_text(encoding="utf-8"))
        s = m["cutoff_sensitivity"]
        assert s["7"] == 3
        assert s["10"] == 2
        assert s["14"] == 2
        assert s["21"] == 1
        assert s["30"] == 0


# ── apply ──────────────────────────────────────────────────────────────────────

class TestApply:
    def test_apply_flips_only_stale_targets(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        rows = [
            _row(update_id="a", update_type="hubspot_note", proposed_at=_iso(20)),      # -> DISMISSED
            _row(update_id="b", update_type="task_close", proposed_at=_iso(2)),         # recent, kept
            _row(update_id="c", update_type="known_answer", proposed_at=_iso(40)),      # protected, kept
            _row(update_id="d", update_type="asana_task", proposed_at=_iso(20),
                 dm_message_ts="99.9"),                                                 # surfaced, kept
            _row(update_id="e", update_type="decision_capture", proposed_at=_iso(60)),  # -> DISMISSED
            "{malformed json line",                                                     # preserved
        ]
        _write_ledger(ledger, rows)
        rc = mod.main(["--ledger", str(ledger), "--manifest-dir", str(tmp_path),
                       "--cutoff-days", "14", "--apply"])
        assert rc == 0
        # .bak created
        baks = list(tmp_path.glob("ledger.jsonl.bak-*"))
        assert len(baks) == 1
        # re-read the ledger
        lines = ledger.read_text(encoding="utf-8").splitlines()
        assert "{malformed json line" in lines  # preserved verbatim
        parsed = {json.loads(l)["update_id"]: json.loads(l)
                  for l in lines if l.strip() and not l.startswith("{malformed")}
        assert parsed["a"]["state"] == "DISMISSED"
        assert parsed["a"]["resolved_reason"] == "expired_bulk"
        assert parsed["a"]["resolved_at"]
        assert parsed["e"]["state"] == "DISMISSED"
        # untouched
        assert parsed["b"]["state"] == "PENDING"
        assert parsed["c"]["state"] == "PENDING"
        assert parsed["d"]["state"] == "PENDING"

    def test_apply_nothing_to_expire_is_noop(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        _write_ledger(ledger, [_row(update_id="fresh", proposed_at=_iso(1))])
        before = ledger.read_bytes()
        rc = mod.main(["--ledger", str(ledger), "--manifest-dir", str(tmp_path),
                       "--cutoff-days", "14", "--apply"])
        assert rc == 0
        assert ledger.read_bytes() == before
        assert list(tmp_path.glob("ledger.jsonl.bak-*")) == []

    def test_apply_aborts_on_concurrent_append(self, tmp_path, monkeypatch):
        """If the ledger's (mtime,size) changes between load and rewrite, ABORT --
        never clobber a fresh contribution that landed mid-apply."""
        ledger = tmp_path / "ledger.jsonl"
        _write_ledger(ledger, [_row(update_id="a", proposed_at=_iso(20))])

        orig = mod._sensitivity_table

        def _mutating(records, types, now):
            # Simulate a live producer appending between load_fp and the re-stat.
            with ledger.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(_row(update_id="late", update_type="known_answer",
                                         proposed_at=_iso(0))) + "\n")
            return orig(records, types, now)

        monkeypatch.setattr(mod, "_sensitivity_table", _mutating)
        rc = mod.main(["--ledger", str(ledger), "--manifest-dir", str(tmp_path),
                       "--cutoff-days", "14", "--apply"])
        assert rc == 1
        assert list(tmp_path.glob("ledger.jsonl.bak-*")) == []
        # the appended row survives; nothing was expired
        states = [json.loads(l)["state"] for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert states.count("PENDING") == 2  # original + late, neither flipped


# ── guards ────────────────────────────────────────────────────────────────────

class TestGuards:
    def test_protected_type_in_types_refused(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        _write_ledger(ledger, [_row()])
        rc = mod.main(["--ledger", str(ledger), "--manifest-dir", str(tmp_path),
                       "--types", "hubspot_note,known_answer"])
        assert rc == 1

    def test_missing_ledger_errors(self, tmp_path):
        rc = mod.main(["--ledger", str(tmp_path / "nope.jsonl"),
                       "--manifest-dir", str(tmp_path)])
        assert rc == 1

    def test_negative_cutoff_errors(self, tmp_path):
        ledger = tmp_path / "ledger.jsonl"
        _write_ledger(ledger, [_row()])
        rc = mod.main(["--ledger", str(ledger), "--manifest-dir", str(tmp_path),
                       "--cutoff-days", "-1"])
        assert rc == 1
