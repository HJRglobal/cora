"""Step 7.5 reconcile for the claude-workspace-mirror bundle (2026-09-03 pin commit).

The cq-11e9abda254a transition ("D-057 parent pin + purge selector") is gated on
the pin actually being present in KB_EXCLUDED_FOLDER_IDS on the checked-out tree,
so a merge that somehow lacked the pin could never mark the seed SHIPPED (the
inverse of the 7/31 stale-positive incident). Dry-run only; nothing is written.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load():
    spec = importlib.util.spec_from_file_location(
        "reconcile_claude_mirror",
        _REPO_ROOT / "scripts" / "reconcile_code_queue_claude_mirror_2026-09-03.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_both_seeds_listed_and_the_pin_id_matches_kb_exclusions():
    mod = _load()
    from cora.kb_exclusions import KB_EXCLUDED_FOLDER_IDS
    assert set(mod.SHIPPED) == {"cq-621dfad586aa", "cq-11e9abda254a"}
    assert mod.CORA_WORKSPACE_FOLDER_ID == "1YNObhKwo8RITgrRbw3MFpf-0hIiLWTx9"
    assert mod.CORA_WORKSPACE_FOLDER_ID in KB_EXCLUDED_FOLDER_IDS
    assert mod._pin_present() is None


def _approved(cq_id):
    return {"id": cq_id, "status": "APPROVED"}


def test_dry_run_reports_both_and_blocks_when_the_pin_is_absent(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item", _approved)
    # dry-run must never reach the ledger: make the write path explode if touched
    monkeypatch.setattr(mod.code_queue, "process_queue_action",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("dry-run wrote!")))
    assert mod.main([]) == 0
    out = capsys.readouterr().out
    assert "would mark SHIPPED  cq-621dfad586aa" in out
    assert "would mark SHIPPED  cq-11e9abda254a" in out
    assert "BLOCKED" not in out
    # a tree WITHOUT the pin blocks the pin+purge seed only, with rc 1, even on dry-run
    monkeypatch.setattr(mod, "KB_EXCLUDED_FOLDER_IDS", frozenset())
    assert mod.main([]) == 1
    out = capsys.readouterr().out
    assert "BLOCKED  cq-11e9abda254a" in out and "has not landed" in out
    assert "would mark SHIPPED  cq-621dfad586aa" in out


def test_apply_label_follows_the_real_outcome(capsys, monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item", _approved)
    calls = []

    def fake_action(action_id, cq_id, actor_id):
        calls.append((action_id, cq_id, actor_id))
        return ("shipped", "ok") if cq_id == "cq-621dfad586aa" else ("error", "gone")

    monkeypatch.setattr(mod.code_queue, "process_queue_action", fake_action)
    assert mod.main(["--apply"]) == 1
    out = capsys.readouterr().out
    assert "SHIPPED  cq-621dfad586aa" in out
    assert "NOT SHIPPED  cq-11e9abda254a" in out and "error: gone" in out
    assert all(a == mod.code_queue.ACTION_MARK_SHIPPED and u == mod.HARRISON_ID for a, _, u in calls)


def test_emoji_success_message_survives_a_cp1252_redirected_stdout(monkeypatch):
    # D-051 pin lens MED-1: the real success message is "🚢 Marked shipped."; on this
    # host a redirected stdout is cp1252 and used to raise AFTER the ledger write,
    # printing FAILED with rc 1 for a transition that had succeeded.
    import io
    mod = _load()
    monkeypatch.setattr(mod.code_queue, "get_item", _approved)
    monkeypatch.setattr(mod.code_queue, "process_queue_action",
                        lambda *a, **k: ("shipped", "\U0001F6A2 Marked shipped."))
    buf = io.BytesIO()
    fake_stdout = io.TextIOWrapper(buf, encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    rc = mod.main(["--apply"])
    fake_stdout.flush()
    out = buf.getvalue().decode("cp1252")
    assert rc == 0
    assert out.count("SHIPPED  cq-") == 2 and "FAILED" not in out and "Marked shipped." in out


def test_rerun_skips_already_shipped_and_refuses_terminal_rows(capsys, monkeypatch):
    mod = _load()
    status = {"cq-621dfad586aa": "SHIPPED", "cq-11e9abda254a": "DISMISSED"}
    monkeypatch.setattr(mod.code_queue, "get_item", lambda cq: {"id": cq, "status": status[cq]})
    monkeypatch.setattr(mod.code_queue, "process_queue_action",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not be called")))
    assert mod.main(["--apply"]) == 1
    out = capsys.readouterr().out
    assert "SKIP (already shipped)  cq-621dfad586aa" in out
    assert "NOT SHIPPED  cq-11e9abda254a" in out and "DISMISSED" in out
    # and an id that no longer exists is reported, never written
    monkeypatch.setattr(mod.code_queue, "get_item", lambda cq: None)
    assert mod.main(["--apply"]) == 1
    assert "missing id" in capsys.readouterr().out
