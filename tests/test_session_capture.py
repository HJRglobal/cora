"""Tests for Universal Session Capture (session_capture.py, 2026-06-09).

Covers: transcript parsing, text flattening, entity inference (cwd + distill),
note rendering schema, dedup ledger, distillation parse robustness, and the
end-to-end harvest (entity tagging, PHI->LEX forcing, dry-run, dedup) using an
injected fake Anthropic client (no network).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import session_capture as scap  # noqa: E402


# ---------------------------------------------------------------------------
# Fake Anthropic client
# ---------------------------------------------------------------------------

class _FakeClient:
    """Returns a fixed JSON body from messages.create()."""

    def __init__(self, body: dict):
        self._text = json.dumps(body)
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        return SimpleNamespace(content=[SimpleNamespace(text=self._text)])


def _distilled_body(entity="F3E", topic="did a thing"):
    return {
        "entity": entity,
        "topic": topic,
        "decisions": ["locked X"],
        "facts": ["Y is true"],
        "action_items": ["do Z (Harrison)"],
        "open_questions": ["what about W?"],
    }


# ---------------------------------------------------------------------------
# _extract_text
# ---------------------------------------------------------------------------

def test_extract_text_string():
    assert scap._extract_text("hello") == "hello"


def test_extract_text_blocks():
    content = [
        {"type": "thinking", "thinking": "secret"},
        {"type": "text", "text": "visible answer"},
        {"type": "tool_use", "name": "Bash"},
    ]
    out = scap._extract_text(content)
    assert "visible answer" in out
    assert "[tool: Bash]" in out
    assert "secret" not in out


# ---------------------------------------------------------------------------
# entity_from_cwd / entity_folder
# ---------------------------------------------------------------------------

def test_entity_from_cwd_cora_repo_is_fndr():
    assert scap.entity_from_cwd(r"C:\Users\Harri\code\cora") == "FNDR"


def test_entity_from_cwd_lex_folder(monkeypatch):
    monkeypatch.setattr(scap, "FOUNDER_OS_ROOT", Path(r"G:\My Drive\HJR-Founder-OS"))
    assert scap.entity_from_cwd(r"G:\My Drive\HJR-Founder-OS\08-Lexington-Services\x") == "LEX"
    assert scap.entity_from_cwd(r"G:\My Drive\HJR-Founder-OS\02-F3-Energy") == "F3E"


def test_entity_from_cwd_none():
    assert scap.entity_from_cwd(None) == "FNDR"


def test_entity_folder_mapping():
    assert scap.entity_folder("LEX") == "08-Lexington-Services"
    assert scap.entity_folder("LEX-LLC") == "08-Lexington-Services"
    assert scap.entity_folder("F3E") == "02-F3-Energy"
    assert scap.entity_folder("WAT") == "00-Founder"


# ---------------------------------------------------------------------------
# _parse_distilled
# ---------------------------------------------------------------------------

def test_parse_distilled_valid():
    out = scap._parse_distilled(json.dumps(_distilled_body("OSN")), "FNDR")
    assert out["entity"] == "OSN"
    assert out["decisions"] == ["locked X"]


def test_parse_distilled_fenced():
    raw = "```json\n" + json.dumps(_distilled_body("UFL")) + "\n```"
    out = scap._parse_distilled(raw, "FNDR")
    assert out["entity"] == "UFL"


def test_parse_distilled_invalid_entity_falls_back():
    out = scap._parse_distilled(json.dumps(_distilled_body("NOPE")), "HJRP")
    assert out["entity"] == "HJRP"


def test_parse_distilled_garbage_returns_none():
    assert scap._parse_distilled("not json at all", "FNDR") is None


def test_parse_distilled_normalizes_case():
    out = scap._parse_distilled(json.dumps(_distilled_body("f3e")), "FNDR")
    assert out["entity"] == "F3E"


# ---------------------------------------------------------------------------
# note rendering + path
# ---------------------------------------------------------------------------

def _fake_session(sid="abcd1234-aaaa", cwd=r"C:\Users\Harri\code\cora"):
    return scap.ParsedSession(
        session_id=sid, path=Path("x.jsonl"), cwd=cwd,
        last_activity_epoch=0.0, started_iso=None, ended_iso=None,
        text="USER: hi\n\nASSISTANT: ok", n_turns=2,
    )


def test_render_note_schema():
    note = scap.render_note(_distilled_body("F3E"), _fake_session(), "2026-06-09", phi=False)
    assert note.startswith("## 2026-06-09 — code-session — F3E — did a thing")
    assert "- Decisions:" in note
    assert "- Facts learned:" in note
    assert "- Action items:" in note
    assert "- Open questions:" in note
    assert "- Source session id: abcd1234-aaaa" in note
    assert "- PHI:" not in note


def test_render_note_phi_line():
    note = scap.render_note(_distilled_body("LEX"), _fake_session(), "2026-06-09", phi=True)
    assert "- PHI: yes" in note


def test_note_path_structure(tmp_path):
    p = scap.note_path_for("LEX", scap.datetime(2026, 6, 9, tzinfo=scap.timezone.utc),
                            "deadbeef-1111", root=tmp_path)
    assert p.parent.name == "2026-06"
    assert p.parent.parent.name == "_session-captures"
    assert "08-Lexington-Services" in str(p)
    assert p.name == "2026-06-09_code-session_deadbeef.md"


# ---------------------------------------------------------------------------
# parse_transcript
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, lines: list[dict]):
    path.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")


def test_parse_transcript_basic(tmp_path):
    f = tmp_path / "11112222-3333.jsonl"
    _write_jsonl(f, [
        {"type": "system", "sessionId": "11112222-3333"},
        {"cwd": r"C:\Users\Harri\code\cora", "timestamp": "2026-06-09T01:00:00.000Z",
         "message": {"role": "user", "content": "build the thing"}},
        {"timestamp": "2026-06-09T01:05:00.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]}},
    ])
    s = scap.parse_transcript(f)
    assert s is not None
    assert s.session_id == "11112222-3333"
    assert s.cwd == r"C:\Users\Harri\code\cora"
    assert s.n_turns == 2
    assert "build the thing" in s.text
    assert "done" in s.text


def test_parse_transcript_empty_returns_none(tmp_path):
    f = tmp_path / "empty.jsonl"
    _write_jsonl(f, [{"type": "system", "sessionId": "x"}])
    assert scap.parse_transcript(f) is None


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------

def test_ledger_roundtrip(tmp_path):
    ledger = tmp_path / "ledger.jsonl"
    assert scap.load_captured_ids(ledger) == set()
    scap.append_ledger({"session_id": "s1"}, ledger)
    scap.append_ledger({"session_id": "s2"}, ledger)
    assert scap.load_captured_ids(ledger) == {"s1", "s2"}


# ---------------------------------------------------------------------------
# distill (injected client)
# ---------------------------------------------------------------------------

def test_distill_with_injected_client():
    out = scap.distill("USER: hi\nASSISTANT: ok", "FNDR", phi=False,
                       client=_FakeClient(_distilled_body("BDM")))
    assert out is not None
    assert out["entity"] == "BDM"


# ---------------------------------------------------------------------------
# harvest end-to-end
# ---------------------------------------------------------------------------

def _setup_session_file(projects_root: Path, sid: str, text_extra: str = "",
                        cwd: str = r"C:\Users\Harri\code\cora") -> Path:
    sub = projects_root / "C--Users-Harri-code-cora"
    sub.mkdir(parents=True, exist_ok=True)
    f = sub / f"{sid}.jsonl"
    _write_jsonl(f, [
        {"cwd": cwd, "timestamp": "2026-06-09T01:00:00.000Z",
         "message": {"role": "user", "content": f"do work {text_extra}"}},
        {"timestamp": "2026-06-09T01:05:00.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "ok finished"}]}},
    ])
    # Make it old enough to pass the settle window (older than 30 min).
    old = scap._now_epoch() - 3600
    os.utime(f, (old, old))
    return f


def test_harvest_writes_note_and_dedups(tmp_path):
    projects = tmp_path / "projects"
    fos = tmp_path / "founder-os"
    ledger = tmp_path / "ledger.jsonl"
    _setup_session_file(projects, "sess-0001-aaaa")

    results = scap.harvest(
        lookback_hours=24, dry_run=False, projects_root=projects,
        founder_os_root=fos, ledger_path=ledger,
        anthropic_client=_FakeClient(_distilled_body("F3E", "shipped feature")),
    )
    assert len(results) == 1
    r = results[0]
    assert r.distilled and r.note_path is not None
    assert r.entity == "F3E"
    assert r.note_path.exists()
    assert "02-F3-Energy" in str(r.note_path)
    assert "shipped feature" in r.note_path.read_text(encoding="utf-8")
    assert scap.load_captured_ids(ledger) == {"sess-0001-aaaa"}

    # Second run: already in ledger -> no re-capture.
    results2 = scap.harvest(
        lookback_hours=24, dry_run=False, projects_root=projects,
        founder_os_root=fos, ledger_path=ledger,
        anthropic_client=_FakeClient(_distilled_body("F3E")),
    )
    assert results2 == []


def test_harvest_phi_forces_lex(tmp_path):
    projects = tmp_path / "projects"
    fos = tmp_path / "founder-os"
    ledger = tmp_path / "ledger.jsonl"
    # "care plan" trips phi_guard.is_phi_risk -> force LEX routing.
    _setup_session_file(projects, "sess-phi-bbbb", text_extra="review the care plan")

    results = scap.harvest(
        lookback_hours=24, dry_run=False, projects_root=projects,
        founder_os_root=fos, ledger_path=ledger,
        anthropic_client=_FakeClient(_distilled_body("F3E", "phi work")),
    )
    assert len(results) == 1
    r = results[0]
    assert r.phi is True
    assert r.entity == "LEX"
    assert "08-Lexington-Services" in str(r.note_path)
    assert "- PHI: yes" in r.note_path.read_text(encoding="utf-8")


def test_harvest_dry_run_writes_nothing(tmp_path):
    projects = tmp_path / "projects"
    fos = tmp_path / "founder-os"
    ledger = tmp_path / "ledger.jsonl"
    _setup_session_file(projects, "sess-dry-cccc")

    results = scap.harvest(
        lookback_hours=24, dry_run=True, projects_root=projects,
        founder_os_root=fos, ledger_path=ledger,
        anthropic_client=_FakeClient(_distilled_body("OSN")),
    )
    assert len(results) == 1
    assert not fos.exists() or not any(fos.rglob("*.md"))
    assert scap.load_captured_ids(ledger) == set()


def test_harvest_distill_failure_not_marked(tmp_path):
    projects = tmp_path / "projects"
    fos = tmp_path / "founder-os"
    ledger = tmp_path / "ledger.jsonl"
    _setup_session_file(projects, "sess-fail-dddd")

    # Garbage body -> _parse_distilled returns None -> fail-closed skip.
    bad_client = SimpleNamespace(
        messages=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(content=[SimpleNamespace(text="garbage")])
        )
    )
    results = scap.harvest(
        lookback_hours=24, dry_run=False, projects_root=projects,
        founder_os_root=fos, ledger_path=ledger, anthropic_client=bad_client,
    )
    assert len(results) == 1
    assert results[0].distilled is False
    assert results[0].skipped_reason == "distill_failed"
    # Not marked captured -> will retry next run.
    assert scap.load_captured_ids(ledger) == set()
