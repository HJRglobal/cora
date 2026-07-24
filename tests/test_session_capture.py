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


# ---------------------------------------------------------------------------
# Slice 2: Cowork desktop store harvesting
# ---------------------------------------------------------------------------

def _cowork_session(root: Path, uuid_stem: str, *, inner: str = "innr-0001",
                    slug: str = "C--slug-outputs",
                    text_extra: str = "",
                    cwd: str = r"C:\Users\Harri\AppData\...\local_x\outputs",
                    extra_lines: list[dict] | None = None,
                    subagent: bool = False, mtime_ago: float = 3600.0) -> Path:
    """Build a fake Cowork agent-mode session dir under root and return the dir."""
    ws = root / "13ef-ws" / "b9ec-agent"
    sess = ws / f"local_{uuid_stem}"
    proj = sess / ".claude" / "projects" / slug
    proj.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "queue-operation", "sessionId": inner, "content": "noise"},
        {"type": "user", "uuid": "u1", "cwd": cwd,
         "timestamp": "2026-07-23T01:00:00.000Z",
         "message": {"role": "user", "content": f"do cowork work {text_extra}"}},
        {"type": "assistant", "uuid": "a1", "timestamp": "2026-07-23T01:05:00.000Z",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "ok finished cowork"}]}},
        # A result summary line -- MUST be skipped (duplicate of per-message data).
        {"type": "result", "uuid": "r1",
         "message": {"role": "assistant", "content": "DUPLICATE SUMMARY leak"}},
    ]
    if extra_lines:
        lines.extend(extra_lines)
    f = proj / f"{inner}.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    old = scap._now_epoch() - mtime_ago
    os.utime(f, (old, old))
    if subagent:
        sub = proj / inner / "subagents"
        sub.mkdir(parents=True, exist_ok=True)
        sf = sub / "agent-deadbeef.jsonl"
        sf.write_text(json.dumps(
            {"type": "user", "uuid": "s1",
             "message": {"role": "user", "content": "SUBAGENT should be skipped"}}),
            encoding="utf-8")
        os.utime(sf, (old, old))
    return sess


class TestCoworkDiscovery:
    def test_override_root(self, tmp_path, monkeypatch):
        monkeypatch.setenv("COWORK_SESSIONS_ROOT", str(tmp_path))
        assert scap._discover_cowork_roots() == [tmp_path]

    def test_iter_yields_only_local_dirs_with_projects(self, tmp_path):
        _cowork_session(tmp_path, "aaaa1111")
        # A non-local_ dir (e.g. "rpm") and a local_ dir without projects: skipped.
        (tmp_path / "13ef-ws" / "b9ec-agent" / "rpm").mkdir(parents=True, exist_ok=True)
        (tmp_path / "13ef-ws" / "b9ec-agent" / "local_empty").mkdir(parents=True)
        found = list(scap.iter_cowork_session_dirs([tmp_path]))
        names = {p.name for p in found}
        assert names == {"local_aaaa1111"}

    def test_transcripts_skip_subagents(self, tmp_path):
        sess = _cowork_session(tmp_path, "bbbb2222", subagent=True)
        tf = scap._cowork_transcripts_for(sess)
        assert len(tf) == 1
        assert "subagents" not in str(tf[0])


class TestParseCoworkSession:
    def test_basic_merge_skips_result_and_queue(self, tmp_path):
        sess = _cowork_session(tmp_path, "cccc3333")
        s = scap.parse_cowork_session(sess)
        assert s is not None
        assert s.session_id == "local_cccc3333"
        assert s.n_turns == 2                       # user + assistant only
        assert "do cowork work" in s.text
        assert "ok finished cowork" in s.text
        assert "DUPLICATE SUMMARY leak" not in s.text   # type:result skipped
        assert "noise" not in s.text                    # queue-operation skipped

    def test_dedup_by_uuid_across_transcripts(self, tmp_path):
        # Second transcript in the SAME dir repeats uuid u1/a1 (a resume) + adds a2.
        extra = [
            {"type": "user", "uuid": "u1", "timestamp": "2026-07-23T02:00:00.000Z",
             "message": {"role": "user", "content": "do cowork work"}},
            {"type": "assistant", "uuid": "a2", "timestamp": "2026-07-23T02:05:00.000Z",
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "second-run reply"}]}},
        ]
        sess = _cowork_session(tmp_path, "dddd4444")
        # write a second transcript file (newer) in the same slug dir
        proj = sess / ".claude" / "projects" / "C--slug-outputs"
        f2 = proj / "innr-0002.jsonl"
        f2.write_text("\n".join(json.dumps(x) for x in extra), encoding="utf-8")
        old = scap._now_epoch() - 1800
        os.utime(f2, (old, old))
        s = scap.parse_cowork_session(sess)
        # u1 appears twice but is deduped; a1 + a2 are distinct.
        assert s.text.count("do cowork work") == 1
        assert "second-run reply" in s.text
        assert s.n_turns == 3                # u1, a1, a2 (dup u1 dropped)

    def test_text_bound_stops_early(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scap, "_COWORK_MAX_TEXT_CHARS", 50)
        big = [{"type": "assistant", "uuid": f"x{i}",
                "message": {"role": "assistant",
                            "content": [{"type": "text", "text": "A" * 40}]}}
               for i in range(10)]
        sess = _cowork_session(tmp_path, "eeee5555", extra_lines=big)
        s = scap.parse_cowork_session(sess)
        # Stops accumulating once the cap is crossed -> far fewer than 12 turns.
        assert s.n_turns < 12

    def test_no_transcripts_returns_none(self, tmp_path):
        empty = tmp_path / "13ef-ws" / "b9ec-agent" / "local_ffff6666"
        (empty / ".claude" / "projects").mkdir(parents=True)
        assert scap.parse_cowork_session(empty) is None


class TestSurfaceParam:
    def test_render_note_cowork_surface(self):
        note = scap.render_note(_distilled_body("F3E"), _fake_session(), "2026-07-23",
                                phi=False, surface=scap.SURFACE_COWORK)
        assert note.startswith("## 2026-07-23 — cowork-session — F3E — did a thing")

    def test_note_path_cowork_surface_and_clean_short(self, tmp_path):
        p = scap.note_path_for("F3E", scap.datetime(2026, 7, 23, tzinfo=scap.timezone.utc),
                               "local_c25465c2-b7a8", root=tmp_path,
                               surface=scap.SURFACE_COWORK)
        # "local_" prefix stripped from the filename short; cowork surface tag.
        assert p.name == "2026-07-23_cowork-session_c25465c2.md"


class TestHarvestCowork:
    def test_cowork_capture_writes_note_and_dedups(self, tmp_path):
        store = tmp_path / "cowork"
        fos = tmp_path / "founder-os"
        ledger = tmp_path / "ledger.jsonl"
        _cowork_session(store, "1111aaaa")

        results = scap.harvest(
            lookback_hours=24, dry_run=False,
            projects_root=tmp_path / "empty-code",   # no code sessions
            founder_os_root=fos, ledger_path=ledger,
            anthropic_client=_FakeClient(_distilled_body("F3E", "cowork thing")),
            include_cowork=True, cowork_roots=[store],
        )
        assert len(results) == 1
        r = results[0]
        assert r.distilled and r.note_path is not None
        assert "02-F3-Energy" in str(r.note_path)
        assert "cowork-session" in r.note_path.name
        assert "cowork-session" in r.note_path.read_text(encoding="utf-8")
        assert scap.load_captured_ids(ledger) == {"cowork:local_1111aaaa"}

        # Second run: already in ledger (cowork: key) -> no re-capture.
        results2 = scap.harvest(
            lookback_hours=24, dry_run=False,
            projects_root=tmp_path / "empty-code",
            founder_os_root=fos, ledger_path=ledger,
            anthropic_client=_FakeClient(_distilled_body("F3E")),
            include_cowork=True, cowork_roots=[store],
        )
        assert results2 == []

    def test_cowork_phi_forces_lex(self, tmp_path):
        store = tmp_path / "cowork"
        fos = tmp_path / "founder-os"
        ledger = tmp_path / "ledger.jsonl"
        _cowork_session(store, "2222bbbb", text_extra="review the care plan")
        results = scap.harvest(
            lookback_hours=24, dry_run=False,
            projects_root=tmp_path / "empty-code",
            founder_os_root=fos, ledger_path=ledger,
            anthropic_client=_FakeClient(_distilled_body("F3E", "phi cowork")),
            include_cowork=True, cowork_roots=[store],
        )
        assert len(results) == 1
        assert results[0].phi is True
        assert results[0].entity == "LEX"
        assert "08-Lexington-Services" in str(results[0].note_path)
        assert "- PHI: yes" in results[0].note_path.read_text(encoding="utf-8")

    def test_cowork_disabled_by_default(self, tmp_path):
        """Module default include_cowork=False: a real store on the host must NOT
        be harvested unless the caller opts in (protects unit tests + other callers)."""
        store = tmp_path / "cowork"
        fos = tmp_path / "founder-os"
        ledger = tmp_path / "ledger.jsonl"
        _cowork_session(store, "3333cccc")
        results = scap.harvest(
            lookback_hours=24, dry_run=False,
            projects_root=tmp_path / "empty-code",
            founder_os_root=fos, ledger_path=ledger,
            anthropic_client=_FakeClient(_distilled_body("F3E")),
            cowork_roots=[store],   # provided, but include_cowork defaults False
        )
        assert results == []

    def test_cowork_dry_run_writes_nothing(self, tmp_path):
        store = tmp_path / "cowork"
        fos = tmp_path / "founder-os"
        ledger = tmp_path / "ledger.jsonl"
        _cowork_session(store, "4444dddd")
        results = scap.harvest(
            lookback_hours=24, dry_run=True,
            projects_root=tmp_path / "empty-code",
            founder_os_root=fos, ledger_path=ledger,
            anthropic_client=_FakeClient(_distilled_body("OSN")),
            include_cowork=True, cowork_roots=[store],
        )
        assert len(results) == 1
        assert not fos.exists() or not any(fos.rglob("*.md"))
        assert scap.load_captured_ids(ledger) == set()

    def test_cowork_budget_cap(self, tmp_path):
        store = tmp_path / "cowork"
        fos = tmp_path / "founder-os"
        ledger = tmp_path / "ledger.jsonl"
        for i in range(3):
            _cowork_session(store, f"cap{i}5555")
        results = scap.harvest(
            lookback_hours=24, dry_run=True,
            projects_root=tmp_path / "empty-code",
            founder_os_root=fos, ledger_path=ledger,
            anthropic_client=_FakeClient(_distilled_body("F3E")),
            include_cowork=True, cowork_roots=[store], max_cowork_sessions=2,
        )
        assert len(results) == 2

    def test_code_and_cowork_both_captured(self, tmp_path):
        projects = tmp_path / "projects"
        store = tmp_path / "cowork"
        fos = tmp_path / "founder-os"
        ledger = tmp_path / "ledger.jsonl"
        _setup_session_file(projects, "code-6666-eeee")
        _cowork_session(store, "6666ffff")
        results = scap.harvest(
            lookback_hours=24, dry_run=False, projects_root=projects,
            founder_os_root=fos, ledger_path=ledger,
            anthropic_client=_FakeClient(_distilled_body("F3E", "both")),
            include_cowork=True, cowork_roots=[store],
        )
        ids = scap.load_captured_ids(ledger)
        assert "code-6666-eeee" in ids
        assert "cowork:local_6666ffff" in ids
        surfaces = {r.meta.get("surface") for r in results}
        assert surfaces == {"code-session", "cowork-session"}


class TestCoworkScheduledTaskGate:
    def test_scheduled_task_first_turn_detected(self):
        s = scap.ParsedSession(
            session_id="local_x", path=Path("x"), cwd=None, last_activity_epoch=0.0,
            started_iso=None, ended_iso=None, n_turns=1,
            text='USER: <scheduled-task name="fndr-daily-synthesis-persist" '
                 'file="C:\\x">\n\nASSISTANT: done')
        assert scap._is_scheduled_task_session(s) is True

    def test_normal_session_not_flagged(self):
        s = scap.ParsedSession(
            session_id="local_x", path=Path("x"), cwd=None, last_activity_epoch=0.0,
            started_iso=None, ended_iso=None, n_turns=1,
            text="USER: Let's discuss the <scheduled-task> concept in the abstract")
        assert scap._is_scheduled_task_session(s) is False

    def test_harvest_skips_scheduled_task_no_distill(self, tmp_path):
        store = tmp_path / "cowork"
        fos = tmp_path / "founder-os"
        ledger = tmp_path / "ledger.jsonl"
        # A real work session + a scheduled-task automation session.
        _cowork_session(store, "aaaa1111")
        _cowork_session(store, "bbbb2222", extra_lines=None)
        # Overwrite bbbb2222's transcript so its FIRST user turn is a scheduled task.
        proj = (store / "13ef-ws" / "b9ec-agent" / "local_bbbb2222"
                / ".claude" / "projects" / "C--slug-outputs")
        f = proj / "innr-0001.jsonl"
        f.write_text(json.dumps(
            {"type": "user", "uuid": "u1",
             "message": {"role": "user",
                         "content": '<scheduled-task name="cora-knowledge-review" '
                                    'file="C:\\x">run it'}}),
            encoding="utf-8")
        old = scap._now_epoch() - 3600
        os.utime(f, (old, old))

        # A distill client that RAISES if ever called on the scheduled task would
        # be ideal; here we assert the scheduled-task result is a skip, not a write.
        results = scap.harvest(
            lookback_hours=24, dry_run=False, projects_root=tmp_path / "empty",
            founder_os_root=fos, ledger_path=ledger,
            anthropic_client=_FakeClient(_distilled_body("F3E", "real work")),
            include_cowork=True, cowork_roots=[store],
        )
        by_reason = {r.session_id: r.skipped_reason for r in results}
        assert by_reason.get("local_bbbb2222") == "scheduled_task"
        # The real session still captured; the scheduled task never entered the ledger.
        ids = scap.load_captured_ids(ledger)
        assert "cowork:local_aaaa1111" in ids
        assert "cowork:local_bbbb2222" not in ids


class TestSessionCaptureRunnerWiring:
    def test_runner_enables_cowork_with_optout(self):
        src = (_REPO_ROOT / "scripts" / "run_session_capture.py").read_text(
            encoding="utf-8")
        assert "include_cowork=not args.no_cowork" in src
        assert "--no-cowork" in src
        assert "--max-cowork-sessions" in src
