"""Code #15 S1 (cq-d9d0c92cc797): the session-capture harvest never hands an API token
to Haiku, the Message Batch, the note on G:, the KB, the ledger or a log line.

THE 9/11 MIRROR. An Asana PAT (v2, segments 1/16/16/32) was pasted into a Cowork
session inside a PowerShell Read-Host prompt. The flattened transcript put it at
~57k chars -- past the 24k distill cap, so it missed the prompt by LUCK; a strict-PHI
transcript gets a 60k cap and would have carried it. Three belts:
  (a) _extract_text redacts text blocks and each tool_result BEFORE its 400-char cut
      (into ParsedSession.distill_text -- session.text stays RAW for the PHI screens);
  (b) _build_distill_prompt redacts the whole transcript BEFORE text[:cap] (sync AND
      batch share it);
  (c) the distilled topic + the rendered note are redacted before the G: write, the KB
      ingest, the ledger row and the log lines (the Haiku-echo belt).

Every token is ASSEMBLED at runtime (the pre-commit hook greps staged files).
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

from cora import batch_client  # noqa: E402
from cora import phi_guard  # noqa: E402
from cora import secret_tokens as st  # noqa: E402
from cora import session_capture as scap  # noqa: E402

D16A = "1204" + "567890123456"
D16B = "1205" + "678901234567"
HEX32 = "0123456789abcdef" * 2
ASANA_V2 = "2/" + D16A + "/" + D16B + ":" + HEX32
SLACK_TAIL = "AbCdEfGhIjKlMnOpQrStUvWx"
SLACK_BOT = "xo" + "xb-" + "1234567890123-1234567890123-" + SLACK_TAIL
ANTHROPIC = "s" + "k-ant-api03-" + ("Ab1_" * 23) + "AA"
READ_HOST = "PS C:\\> $pat = Read-Host 'Paste your Asana PAT'\nPaste your Asana PAT: " + ASANA_V2
PHI_TRIGGER = "review the diagnosis notes for the client"     # trips the STRICT screen (60k cap)


def _leaks(text: str) -> bool:
    return any(s in text for s in (ASANA_V2, D16B, HEX32, SLACK_BOT, SLACK_TAIL, ANTHROPIC,
                                   "xo" + "xb-1234567890123"))


class _CapturingClient:
    """A fake Anthropic client that RECORDS every prompt it is handed and answers
    with a fixed distill body (optionally ECHOING a token back, like a model can)."""

    def __init__(self, body: dict):
        self._text = json.dumps(body)
        self.prompts: list[str] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        for m in kwargs.get("messages", []):
            self.prompts.append(m.get("content", ""))
        return SimpleNamespace(content=[SimpleNamespace(text=self._text)])


def _body(entity="FNDR", topic="rotated the asana credential", facts=None):
    return {"entity": entity, "topic": topic, "decisions": ["rotate it"],
            "facts": facts or ["the PAT lives in .env"], "action_items": [], "open_questions": []}


def _code_session(projects: Path, sid: str, turns: list[dict]) -> Path:
    sub = projects / "C--Users-Harri-code-cora"
    sub.mkdir(parents=True, exist_ok=True)
    f = sub / f"{sid}.jsonl"
    lines = [{"cwd": r"C:\Users\Harri\code\cora", "timestamp": "2026-09-11T12:00:00.000Z",
              "sessionId": sid, **turns[0]}] + [dict(t) for t in turns[1:]]
    f.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    old = scap._now_epoch() - 3600
    os.utime(f, (old, old))
    return f


def _cowork_session(root: Path, stem: str, lines: list[dict]) -> Path:
    sess = root / "13ef-ws" / "b9ec-agent" / f"local_{stem}"
    proj = sess / ".claude" / "projects" / "C--slug-outputs"
    proj.mkdir(parents=True, exist_ok=True)
    f = proj / "innr-0001.jsonl"
    f.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    old = scap._now_epoch() - 3600
    os.utime(f, (old, old))
    return sess


def _user(text: str) -> dict:
    return {"message": {"role": "user", "content": text}}


def _assistant(text: str) -> dict:
    return {"message": {"role": "assistant", "content": [{"type": "text", "text": text}]}}


class _FakeKB:
    def __init__(self):
        self.docs = []

    def upsert_documents(self, docs):
        self.docs.extend(docs)


# ── belt (a): the flattening ─────────────────────────────────────────────────
class TestExtractText:
    def test_default_flattening_is_unchanged(self):
        content = [{"type": "text", "text": "key " + SLACK_BOT}]
        assert scap._extract_text(content) == "key " + SLACK_BOT        # raw by default

    def test_text_blocks_and_strings_redacted(self):
        assert scap._extract_text("key " + SLACK_BOT, redact_tokens=True) == "key " + st.MARKER
        out = scap._extract_text([{"type": "text", "text": READ_HOST}], redact_tokens=True)
        assert not _leaks(out) and st.MARKER in out

    def test_tool_result_straddling_the_400_cut_leaves_no_partial_prefix(self):
        inner = "x" * 370 + " " + ANTHROPIC + " trailing output"   # the token spans the cut
        content = [{"type": "tool_result", "content": [{"type": "text", "text": inner}]}]
        raw = scap._extract_text(content)
        assert ("s" + "k-ant-api03-") in raw and ANTHROPIC not in raw     # the raw cut strands a prefix
        red = scap._extract_text(content, redact_tokens=True)
        assert ("s" + "k-ant-") not in red and "Ab1_" not in red and st.MARKER in red

    def test_parsers_keep_raw_text_and_build_the_redacted_twin(self, tmp_path):
        f = _code_session(tmp_path / "p", "sess-tok-0001", [_user(READ_HOST), _assistant("stored it")])
        s = scap.parse_transcript(f)
        assert ASANA_V2 in s.text                       # RAW: the PHI screens read this
        assert not _leaks(s.distill_text) and st.MARKER in s.distill_text
        assert s.distill_text.replace(st.MARKER, ASANA_V2) == s.text

        sess = _cowork_session(tmp_path / "cw", "tok00001", [
            {"type": "queue-operation", "content": "queued " + ASANA_V2},     # skipped by the parser
            {"type": "user", "uuid": "u1", **_user(READ_HOST)},
            {"type": "assistant", "uuid": "a1", **_assistant("ok")},
        ])
        c = scap.parse_cowork_session(sess)
        assert c.text.count(ASANA_V2) == 1              # the queue-operation copy never enters
        assert not _leaks(c.distill_text) and c.distill_text.count(st.MARKER) == 1


# ── belt (b): the prompt, sync + batch ───────────────────────────────────────
class TestDistillPrompt:
    def test_build_prompt_redacts_before_the_cap(self):
        text = "USER: " + READ_HOST
        prompt = scap._build_distill_prompt(text, "FNDR", phi=False)
        assert not _leaks(prompt) and st.MARKER in prompt

    def test_a_token_straddling_the_cap_leaves_no_prefix(self):
        text = "x" * (scap._MAX_INPUT_CHARS - 10) + " " + ASANA_V2
        prompt = scap._build_distill_prompt(text, "FNDR", phi=False)
        assert "2/" + D16A[:6] not in prompt and not _leaks(prompt)

    def test_redactor_failure_skips_the_distill(self, monkeypatch):
        monkeypatch.setattr(st, "_redact_counts", lambda t: (_ for _ in ()).throw(RuntimeError("x")))
        client = _CapturingClient(_body())
        assert scap.distill("USER: " + READ_HOST, "FNDR", phi=False, client=client) is None
        assert client.prompts == []                      # nothing was ever sent
        prompt = scap._build_distill_prompt("USER: " + READ_HOST, "FNDR", phi=False)
        assert not _leaks(prompt) and st.WITHHELD in prompt

    # D-051 r1 s1-seams#2: belt (a) fails closed at the SESSION level. A per-block
    # redactor error used to put WITHHELD in place of just that block; the whole-twin
    # guard re-scanned a marker with no shape (n == 0), so a PARTIAL transcript was
    # distilled, the note written and the session ledger-marked -- never retried.
    @staticmethod
    def _flaky_block(monkeypatch):
        real = st._redact_counts

        def _flaky(text):
            if "BOOM-BLOCK" in text:          # only a block carrying this text fails
                raise MemoryError("simulated per-block redactor failure")
            return real(text)
        monkeypatch.setattr(st, "_redact_counts", _flaky)

    _BOOM_TURNS = staticmethod(lambda: [
        _user("set up the payroll export"),
        _user("decide the BOOM-BLOCK plan: ship the payroll change on Friday"),
        _assistant("ok, noted"),
    ])

    def test_a_per_block_redactor_failure_withholds_the_whole_twin(self, tmp_path, monkeypatch):
        self._flaky_block(monkeypatch)
        f = _code_session(tmp_path / "p", "sess-boom-0001", self._BOOM_TURNS())
        s = scap.parse_transcript(f)
        assert "payroll change" in s.text                      # RAW text is untouched
        assert s.distill_text == st.WITHHELD                   # never a partial twin
        assert scap._redacted_transcript(scap._distill_input(s)) is None

        sess = _cowork_session(tmp_path / "cw", "boom0001", [
            {"type": "user", "uuid": "u1", **_user("decide the BOOM-BLOCK plan")},
            {"type": "assistant", "uuid": "a1", **_assistant("ok")},
            {"type": "user", "uuid": "u2", **_user("and ship it " + SLACK_BOT)},
        ])
        c = scap.parse_cowork_session(sess)
        assert c.distill_text == st.WITHHELD and SLACK_BOT in c.text

    def test_a_per_block_failure_distills_nothing_writes_nothing_and_retries(self, tmp_path,
                                                                             monkeypatch):
        self._flaky_block(monkeypatch)
        projects, fos, ledger = tmp_path / "p", tmp_path / "fos", tmp_path / "l.jsonl"
        _code_session(projects, "sess-boom-0002", self._BOOM_TURNS())
        client = _CapturingClient(_body())
        results = scap.harvest(lookback_hours=24, projects_root=projects, founder_os_root=fos,
                               ledger_path=ledger, anthropic_client=client)
        r = results[0]
        assert not r.distilled and r.note_path is None and r.skipped_reason == "distill_failed"
        assert client.prompts == []                            # nothing was ever sent
        assert not fos.exists() or not any(fos.rglob("*.md"))
        assert scap.load_captured_ids(ledger) == set()         # retries next run

    def test_a_per_block_failure_keeps_the_session_off_the_batch(self, tmp_path, monkeypatch):
        monkeypatch.delenv("CORA_BATCH_DISABLE", raising=False)
        monkeypatch.delenv("CORA_BATCH_CAPTURE", raising=False)
        self._flaky_block(monkeypatch)
        seen: dict = {}

        def _fake_batch(requests, *, caller, deadline_s, **kw):
            seen["requests"] = requests
            return {}
        monkeypatch.setattr(batch_client, "batch_generate", _fake_batch)
        boom = scap.parse_transcript(_code_session(tmp_path / "p1", "sess-boom-0003",
                                                   self._BOOM_TURNS()))
        ok = scap.parse_transcript(_code_session(tmp_path / "p2", "sess-ok-0003",
                                                 [_user("plain work " + SLACK_BOT), _assistant("ok")]))
        scap._batch_distill([(boom, scap.SURFACE, "sess-boom-0003"), (ok, scap.SURFACE, "sess-ok-0003")])
        prompts = [q["params"]["messages"][0]["content"] for q in seen["requests"]]
        assert len(prompts) == 1                               # only the clean session
        assert all("payroll" not in p and st.WITHHELD not in p and not _leaks(p) for p in prompts)

    def test_9_11_mirror_under_the_24k_cap(self, tmp_path):
        projects, fos, ledger = tmp_path / "p", tmp_path / "fos", tmp_path / "l.jsonl"
        _code_session(projects, "sess-911-0001", [_user("set up the asana identity"),
                                                  _user(READ_HOST), _assistant("done")])
        client = _CapturingClient(_body())
        results = scap.harvest(lookback_hours=24, projects_root=projects, founder_os_root=fos,
                               ledger_path=ledger, anthropic_client=client)
        assert results[0].distilled
        assert client.prompts and all(not _leaks(p) for p in client.prompts)
        assert any(st.MARKER in p for p in client.prompts)

    def test_9_11_mirror_at_57k_under_the_strict_phi_60k_cap(self, tmp_path):
        """The luck case: at ~57k chars the paste sits past the 24k cap but INSIDE the
        60k strict-PHI cap -- this exact transcript would have carried the PAT to Haiku."""
        projects, fos, ledger = tmp_path / "p", tmp_path / "fos", tmp_path / "l.jsonl"
        padding = [_user(PHI_TRIGGER)] + [_assistant("step " + "y" * 950) for _ in range(59)]
        _code_session(projects, "sess-911-0002", padding + [_user(READ_HOST), _assistant("ok")])
        s = scap.parse_transcript(next((projects).rglob("*.jsonl")))
        off = s.text.index(ASANA_V2)
        assert scap._MAX_INPUT_CHARS < off < scap._MAX_INPUT_CHARS_PHI     # the luck window
        assert phi_guard.is_phi_risk(s.text)                               # the 60k cap applies
        client = _CapturingClient(_body(entity="FNDR"))
        scap.harvest(lookback_hours=24, projects_root=projects, founder_os_root=fos,
                     ledger_path=ledger, anthropic_client=client)
        assert client.prompts
        assert all(not _leaks(p) for p in client.prompts)
        assert any(st.MARKER in p for p in client.prompts)

    def test_batch_requests_are_token_free(self, monkeypatch):
        monkeypatch.delenv("CORA_BATCH_DISABLE", raising=False)
        monkeypatch.delenv("CORA_BATCH_CAPTURE", raising=False)
        seen: dict = {}

        def _fake_batch(requests, *, caller, deadline_s, **kw):
            seen["requests"] = requests
            return {}
        monkeypatch.setattr(batch_client, "batch_generate", _fake_batch)
        raw = "USER: " + READ_HOST + "\n\nASSISTANT: ok"
        red = "USER: " + scap._extract_text(READ_HOST, redact_tokens=True) + "\n\nASSISTANT: ok"
        with_twin = scap.ParsedSession(session_id="s-b1", path=Path("x"), cwd=None,
                                       last_activity_epoch=0.0, started_iso="", ended_iso="",
                                       text=raw, n_turns=2, distill_text=red)
        hand_built = scap.ParsedSession(session_id="s-b2", path=Path("x"), cwd=None,
                                        last_activity_epoch=0.0, started_iso="", ended_iso="",
                                        text=raw, n_turns=2)       # no twin: belt (b) alone
        # a duck-typed session with NO distill_text attribute (a caller / test stub) must
        # not knock the whole batch over (the full suite caught exactly that)
        duck = SimpleNamespace(text=raw, cwd=None, session_id="s-b3")
        scap._batch_distill([(with_twin, scap.SURFACE, "s-b1"), (hand_built, scap.SURFACE, "s-b2"),
                             (duck, scap.SURFACE, "s-b3")])
        prompts = [r["params"]["messages"][0]["content"] for r in seen["requests"]]
        assert len(prompts) == 3
        assert all(not _leaks(p) and st.MARKER in p for p in prompts)


# ── belt (c): the Haiku-echo belt ────────────────────────────────────────────
class TestEchoBelt:
    def test_an_echoed_token_never_reaches_the_note_kb_ledger_or_logs(self, tmp_path, caplog):
        projects, fos, ledger = tmp_path / "p", tmp_path / "fos", tmp_path / "l.jsonl"
        _code_session(projects, "sess-echo-0001", [_user(READ_HOST), _assistant("ok")])
        client = _CapturingClient(_body(topic="stored PAT " + ASANA_V2,
                                        facts=["bot token is " + SLACK_BOT, "key " + ANTHROPIC]))
        kb = _FakeKB()
        with caplog.at_level("INFO"):
            results = scap.harvest(lookback_hours=24, projects_root=projects, founder_os_root=fos,
                                   ledger_path=ledger, anthropic_client=client, with_kb=True, kb=kb)
        r = results[0]
        assert r.distilled and r.note_path is not None
        note = r.note_path.read_text(encoding="utf-8")
        assert not _leaks(note) and note.count(st.MARKER) == 3
        assert f"stored PAT {st.MARKER}" in note.splitlines()[0]          # the header topic
        assert kb.docs and not _leaks(kb.docs[0].content) and not _leaks(kb.docs[0].title)
        assert kb.docs[0].title == f"Session capture — stored PAT {st.MARKER}"
        row = json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])
        assert not _leaks(json.dumps(row)) and row["topic"] == f"stored PAT {st.MARKER}"
        assert not _leaks(r.meta["topic"])
        assert not _leaks(" ".join(rec.getMessage() for rec in caplog.records))

    def test_note_redactor_failure_writes_nothing_and_retries(self, tmp_path, monkeypatch):
        projects, fos, ledger = tmp_path / "p", tmp_path / "fos", tmp_path / "l.jsonl"
        _code_session(projects, "sess-fail-0001", [_user("plain work"), _assistant("ok")])
        real = st.redact_secret_tokens

        def _fail_on_note(text):
            if text and text.startswith("## "):        # the rendered note only
                return st.WITHHELD, 1
            return real(text)
        monkeypatch.setattr(st, "redact_secret_tokens", _fail_on_note)
        results = scap.harvest(lookback_hours=24, projects_root=projects, founder_os_root=fos,
                               ledger_path=ledger, anthropic_client=_CapturingClient(_body()))
        assert results[0].skipped_reason == "token_redaction_failed" and results[0].note_path is None
        assert not fos.exists() or not any(fos.rglob("*.md"))
        assert scap.load_captured_ids(ledger) == set()             # retries next run


# ── the PHI screens keep reading the RAW text ────────────────────────────────
class TestPhiRoutingUnchanged:
    @pytest.mark.parametrize("extra,entity", [
        ("plain infra work", "F3E"),
        (PHI_TRIGGER, "LEX"),
        ("client Marcus Johnson was diagnosed with autism", "F3E"),     # value PHI -> quarantine
        (PHI_TRIGGER, "F3E"),
    ])
    def test_flags_identical_to_a_no_token_twin(self, tmp_path, extra, entity):
        out = {}
        for label, secret in (("tok", " " + READ_HOST + " " + ANTHROPIC), ("twin", "")):
            projects, fos, ledger = (tmp_path / label / "p", tmp_path / label / "fos",
                                     tmp_path / label / "l.jsonl")
            _code_session(projects, f"sess-{label}-0001", [_user(extra + secret), _assistant("ok")])
            s = scap.parse_transcript(next(projects.rglob("*.jsonl")))
            flags = (phi_guard.is_phi_risk(s.text), phi_guard.is_prose_phi_risk(s.text),
                     scap.route_capture(entity, s.text))
            res = scap.harvest(lookback_hours=24, projects_root=projects, founder_os_root=fos,
                               ledger_path=ledger, anthropic_client=_CapturingClient(_body(entity=entity)))
            out[label] = (flags, res[0].entity, res[0].phi, res[0].quarantined)
        assert out["tok"] == out["twin"]

    def test_screens_read_session_text_not_the_twin(self, tmp_path, monkeypatch):
        projects, fos, ledger = tmp_path / "p", tmp_path / "fos", tmp_path / "l.jsonl"
        _code_session(projects, "sess-raw-0001", [_user(READ_HOST), _assistant("ok")])
        seen: list[str] = []
        real_strict, real_prose = phi_guard.is_phi_risk, phi_guard.is_prose_phi_risk
        monkeypatch.setattr(phi_guard, "is_phi_risk", lambda t: seen.append(t) or real_strict(t))
        monkeypatch.setattr(phi_guard, "is_prose_phi_risk", lambda t: seen.append(t) or real_prose(t))
        scap.harvest(lookback_hours=24, projects_root=projects, founder_os_root=fos,
                     ledger_path=ledger, anthropic_client=_CapturingClient(_body()))
        assert seen and all(ASANA_V2 in t for t in seen)             # the RAW flattening
