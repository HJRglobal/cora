"""Tests for scripts/triage_proposed_updates.py (WS17-B Phase 0 bulk-dismiss).

Verifies the SAFETY contract: dry-run by default (no writes), known_answer /
efficiency / #info-for-cora generics are never dismissed, only PENDING rows are
touched, --apply makes a backup and flips state, and the manifest is written.
"""

import json
from datetime import datetime, timezone

import scripts.triage_proposed_updates as tpu


def _rec(uid, utype, state="PENDING", source=None, proposed="2026-06-01T00:00:00+00:00"):
    payload = {}
    if source is not None:
        payload["source"] = source
    return {
        "update_id": uid,
        "update_type": utype,
        "description": f"desc {uid}",
        "payload": payload,
        "state": state,
        "proposed_at": proposed,
        "resolved_at": None,
    }


def _write_ledger(path, records):
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


_DEFAULT = frozenset(tpu._DEFAULT_DISMISS_TYPES)


# ── Predicates ───────────────────────────────────────────────────────────────

def test_should_dismiss_operational_deadends():
    assert tpu._should_dismiss(_rec("a", "hubspot_note"), _DEFAULT)
    assert tpu._should_dismiss(_rec("b", "decision_capture"), _DEFAULT)
    assert tpu._should_dismiss(_rec("c", "generic"), _DEFAULT)


def test_protects_knowledge_types():
    assert not tpu._should_dismiss(_rec("d", "known_answer"), _DEFAULT)
    assert not tpu._should_dismiss(_rec("e", "efficiency"), _DEFAULT)


def test_protects_info_for_cora_generic():
    # A generic contributed via #info-for-cora is a human note — never bulk-dismissed.
    assert tpu._is_info_for_cora_generic(_rec("f", "generic", source="info-for-cora"))
    assert not tpu._should_dismiss(_rec("f", "generic", source="info-for-cora"), _DEFAULT)
    # A generic from drive_extractor (no info-for-cora source) IS dismissable.
    assert tpu._should_dismiss(_rec("g", "generic", source=None), _DEFAULT)


def test_only_pending_touched():
    assert not tpu._should_dismiss(_rec("h", "hubspot_note", state="APPROVED"), _DEFAULT)
    assert not tpu._should_dismiss(_rec("i", "hubspot_note", state="DISMISSED"), _DEFAULT)


def test_asana_task_not_in_default_set():
    # asana_task / task_close are NOT in the default dismiss set (Harrison opts in).
    assert not tpu._should_dismiss(_rec("j", "asana_task"), _DEFAULT)
    assert not tpu._should_dismiss(_rec("k", "task_close"), _DEFAULT)
    # ...but extensible via an explicit type set.
    assert tpu._should_dismiss(_rec("j", "asana_task"), frozenset({"asana_task"}))


# ── Dry-run leaves the ledger byte-identical ──────────────────────────────────

def test_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    ledger = tmp_path / "ledger.jsonl"
    records = [_rec("a", "hubspot_note"), _rec("b", "known_answer")]
    _write_ledger(ledger, records)
    before = ledger.read_bytes()

    monkeypatch.setattr("sys.argv", ["triage", "--ledger", str(ledger),
                                     "--manifest-dir", str(tmp_path)])
    assert tpu.main() == 0
    assert ledger.read_bytes() == before  # unchanged
    # A manifest was still written.
    manifests = list(tmp_path.glob("triage-manifest-*.json"))
    assert len(manifests) == 1
    man = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert man["mode"] == "dry-run"
    assert man["dismiss_total"] == 1
    assert man["dismissed_update_ids"] == ["a"]


# ── --apply flips PENDING dead-ends, keeps the rest, backs up first ───────────

def test_apply_dismisses_and_backs_up(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    records = [
        _rec("a", "hubspot_note"),                       # dismiss
        _rec("b", "decision_capture"),                   # dismiss
        _rec("c", "generic", source=None),               # dismiss (drive generic)
        _rec("d", "generic", source="info-for-cora"),    # KEEP (human note)
        _rec("e", "known_answer"),                        # KEEP
        _rec("f", "efficiency"),                          # KEEP
        _rec("g", "asana_task"),                          # KEEP (not default)
        _rec("h", "hubspot_note", state="APPROVED"),      # KEEP (not PENDING)
    ]
    _write_ledger(ledger, records)

    monkeypatch.setattr("sys.argv", ["triage", "--ledger", str(ledger),
                                     "--manifest-dir", str(tmp_path), "--apply"])
    assert tpu.main() == 0

    # A timestamped backup exists.
    baks = list(tmp_path.glob("ledger.jsonl.bak-*"))
    assert len(baks) == 1

    after = {r["update_id"]: r for r in
             (json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip())}
    assert after["a"]["state"] == "DISMISSED"
    assert after["a"]["resolved_reason"] == "bulk_triage_ws17b"
    assert after["b"]["state"] == "DISMISSED"
    assert after["c"]["state"] == "DISMISSED"
    # Kept:
    for keep in ("d", "e", "f", "g"):
        assert after[keep]["state"] == "PENDING", keep
    assert after["h"]["state"] == "APPROVED"


def test_apply_refuses_protected_types(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    _write_ledger(ledger, [_rec("a", "known_answer")])
    monkeypatch.setattr("sys.argv", ["triage", "--ledger", str(ledger),
                                     "--types", "known_answer", "--apply"])
    assert tpu.main() == 1  # refuses to dismiss a protected type


def test_apply_preserves_malformed_lines(tmp_path, monkeypatch):
    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text(
        json.dumps(_rec("a", "hubspot_note")) + "\n"
        + "{ this is not valid json\n"
        + json.dumps(_rec("b", "known_answer")) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.argv", ["triage", "--ledger", str(ledger),
                                     "--manifest-dir", str(tmp_path), "--apply"])
    assert tpu.main() == 0
    lines = [l for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert any("this is not valid json" in l for l in lines)  # malformed line preserved
    assert len(lines) == 3
