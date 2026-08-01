"""Slice-3 tests for the batch-API pilot adoption (2026-07-31).

Two legs:
  * channel_synthesis: batch-of-1 transport behind set_batch_transport()
    (module default OFF -- the scheduled runners opt in) + env kill switches.
  * session_capture: two-phase harvest -- collect pending sessions, ONE
    Message Batch for every distill, finalize in the original order with
    identical PHI routing / fail-closed semantics.
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
from cora import channel_synthesis as cs  # noqa: E402
from cora import session_capture as scap  # noqa: E402


def _batch_msg(text: str):
    return SimpleNamespace(
        model="claude-x", content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=1, cache_creation_input_tokens=0,
                              cache_read_input_tokens=0, output_tokens=1))


# ---------------------------------------------------------------------------
# channel_synthesis leg
# ---------------------------------------------------------------------------

class TestSynthesisBatchTransport:
    def test_module_default_is_sync(self, monkeypatch):
        """With no runner opt-in, _synthesize must never touch batch_generate
        (library consumers / existing tests keep the exact sync path)."""
        assert cs._USE_BATCH is False
        called = []
        monkeypatch.setattr(batch_client, "batch_generate",
                            lambda *a, **k: called.append(1) or {})
        import anthropic

        class _Client:
            def __init__(self, **kwargs):
                self.messages = SimpleNamespace(
                    create=lambda **kw: _batch_msg("*Moved* sync path"))
        monkeypatch.setattr(anthropic, "Anthropic", _Client)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        assert cs._synthesize("facts") == "*Moved* sync path"
        assert called == []

    def test_batch_transport_used_when_runner_opts_in(self, monkeypatch):
        monkeypatch.setattr(cs, "_USE_BATCH", True)
        monkeypatch.delenv("CORA_BATCH_DISABLE", raising=False)
        monkeypatch.delenv("CORA_BATCH_SYNTHESIS", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        seen = {}

        def _fake_batch(requests, *, caller, deadline_s, api_key, **kw):
            seen.update(requests=requests, caller=caller,
                        deadline_s=deadline_s, api_key=api_key)
            return {"synthesis-0": _batch_msg("*Moved* batch path")}
        monkeypatch.setattr(batch_client, "batch_generate", _fake_batch)

        assert cs._synthesize("THE FACTS") == "*Moved* batch path"
        assert seen["caller"] == "channel_synthesis"
        assert seen["requests"][0]["custom_id"] == "synthesis-0"
        params = seen["requests"][0]["params"]
        assert params["model"] == cs.sm.SONNET_MODEL
        assert params["max_tokens"] == cs.sm._SYNTH_MAX_TOKENS
        assert params["thinking"] == {"type": "disabled"}  # D-051 preserved
        assert params["messages"][0]["content"] == "THE FACTS"

    def test_batch_deadline_env_honored(self, monkeypatch):
        monkeypatch.setattr(cs, "_USE_BATCH", True)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("CORA_BATCH_SYNTHESIS_DEADLINE_S", "123")
        seen = {}

        def _fake_batch(requests, **kw):
            seen.update(kw)
            return {"synthesis-0": _batch_msg("*Moved* x")}
        monkeypatch.setattr(batch_client, "batch_generate", _fake_batch)
        cs._synthesize("facts")
        assert seen["deadline_s"] == 123.0

    def test_batch_total_failure_falls_back_to_fallback_memo(self, monkeypatch):
        """batch+sync both failed => None => caller's deterministic rollup."""
        monkeypatch.setattr(cs, "_USE_BATCH", True)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr(batch_client, "batch_generate",
                            lambda *a, **k: {"synthesis-0": None})
        assert cs._synthesize("facts") is None

    def test_env_kill_switch_forces_sync(self, monkeypatch):
        monkeypatch.setattr(cs, "_USE_BATCH", True)
        monkeypatch.setenv("CORA_BATCH_SYNTHESIS", "0")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        called = []
        monkeypatch.setattr(batch_client, "batch_generate",
                            lambda *a, **k: called.append(1) or {})
        import anthropic

        class _Client:
            def __init__(self, **kwargs):
                self.messages = SimpleNamespace(
                    create=lambda **kw: _batch_msg("*Moved* sync"))
        monkeypatch.setattr(anthropic, "Anthropic", _Client)
        assert cs._synthesize("facts") == "*Moved* sync"
        assert called == []

    def test_runners_opt_in_source_pin(self):
        """The scheduled runners are the batch opt-in point -- pin it so a
        refactor can't silently drop the pilot back to sync."""
        for runner in ("run_portfolio_synthesis.py", "run_entity_synthesis.py"):
            src = (_REPO_ROOT / "scripts" / runner).read_text(encoding="utf-8")
            assert "set_batch_transport(not args.no_batch)" in src, runner
        cap = (_REPO_ROOT / "scripts" / "run_session_capture.py"
               ).read_text(encoding="utf-8")
        assert "use_batch=not args.no_batch" in cap


# ---------------------------------------------------------------------------
# session_capture leg
# ---------------------------------------------------------------------------

def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n",
                    encoding="utf-8")


def _setup_session_file(projects_root: Path, sid: str) -> Path:
    sub = projects_root / "C--Users-Harri-code-cora"
    sub.mkdir(parents=True, exist_ok=True)
    f = sub / f"{sid}.jsonl"
    _write_jsonl(f, [
        {"cwd": r"C:\Users\Harri\code\cora",
         "timestamp": "2026-07-30T01:00:00.000Z",
         "message": {"role": "user", "content": "do work"}},
        {"timestamp": "2026-07-30T01:05:00.000Z",
         "message": {"role": "assistant",
                     "content": [{"type": "text", "text": "ok finished"}]}},
    ])
    old = scap._now_epoch() - 3600
    os.utime(f, (old, old))
    return f


def _distilled(entity="F3E", topic="batch thing"):
    return {"entity": entity, "topic": topic, "decisions": [], "facts": ["f"],
            "action_items": [], "open_questions": []}


def _session(sid="s1", text="USER: hi", cwd=r"C:\Users\Harri\code\cora"):
    return scap.ParsedSession(session_id=sid, path=Path("x"), cwd=cwd,
                              last_activity_epoch=0.0, started_iso="",
                              ended_iso="", text=text, n_turns=1)


class TestHarvestBatch:
    def test_batch_predistill_feeds_finalize(self, tmp_path, monkeypatch):
        projects, fos, ledger = (tmp_path / "p", tmp_path / "fos",
                                 tmp_path / "l.jsonl")
        _setup_session_file(projects, "sess-batch-0001")
        calls = []

        def _fake_batch_distill(pending):
            calls.append(pending)
            return {lk: _distilled("F3E", "batch-distilled note")
                    for _s, _surf, lk in pending}
        monkeypatch.setattr(scap, "_batch_distill", _fake_batch_distill)

        results = scap.harvest(
            lookback_hours=24, dry_run=False, projects_root=projects,
            founder_os_root=fos, ledger_path=ledger,
            anthropic_client=None, use_batch=True)
        assert len(calls) == 1 and len(calls[0]) == 1
        assert len(results) == 1
        assert results[0].distilled and results[0].note_path.exists()
        assert "batch-distilled note" in results[0].note_path.read_text(
            encoding="utf-8")
        assert scap.load_captured_ids(ledger) == {"sess-batch-0001"}

    def test_batch_failed_item_is_fail_closed(self, tmp_path, monkeypatch):
        """None from the batch (both transports failed) == distill_failed:
        not written, not ledger-marked, retries next run."""
        projects, fos, ledger = (tmp_path / "p", tmp_path / "fos",
                                 tmp_path / "l.jsonl")
        _setup_session_file(projects, "sess-batch-0002")
        monkeypatch.setattr(
            scap, "_batch_distill",
            lambda pending: {lk: None for _s, _surf, lk in pending})
        results = scap.harvest(
            lookback_hours=24, dry_run=False, projects_root=projects,
            founder_os_root=fos, ledger_path=ledger,
            anthropic_client=None, use_batch=True)
        assert len(results) == 1
        assert results[0].skipped_reason == "distill_failed"
        assert scap.load_captured_ids(ledger) == set()

    def test_empty_batch_result_falls_back_to_sync_distill(
            self, tmp_path, monkeypatch):
        projects, fos, ledger = (tmp_path / "p", tmp_path / "fos",
                                 tmp_path / "l.jsonl")
        _setup_session_file(projects, "sess-batch-0003")
        monkeypatch.setattr(scap, "_batch_distill", lambda pending: {})
        sync_calls = []

        def _fake_distill(text, default_entity, *, phi, client=None):
            sync_calls.append(default_entity)
            return _distilled("F3E", "sync fallback note")
        monkeypatch.setattr(scap, "distill", _fake_distill)
        results = scap.harvest(
            lookback_hours=24, dry_run=False, projects_root=projects,
            founder_os_root=fos, ledger_path=ledger,
            anthropic_client=None, use_batch=True)
        assert sync_calls == ["FNDR"]
        assert results[0].distilled

    def test_injected_client_bypasses_batch(self, tmp_path, monkeypatch):
        """A bespoke caller that injects its own client keeps pure sync --
        batching only activates on the runner path (anthropic_client=None)."""
        projects, fos, ledger = (tmp_path / "p", tmp_path / "fos",
                                 tmp_path / "l.jsonl")
        _setup_session_file(projects, "sess-batch-0004")
        monkeypatch.setattr(
            scap, "_batch_distill",
            lambda pending: (_ for _ in ()).throw(AssertionError("batched!")))

        class _FakeClient:
            def __init__(self):
                self.messages = SimpleNamespace(create=self._create)

            def _create(self, **kwargs):
                return SimpleNamespace(content=[SimpleNamespace(
                    text=json.dumps(_distilled("F3E", "injected sync")))])
        results = scap.harvest(
            lookback_hours=24, dry_run=False, projects_root=projects,
            founder_os_root=fos, ledger_path=ledger,
            anthropic_client=_FakeClient(), use_batch=True)
        assert results[0].distilled

    def test_default_harvest_signature_unchanged(self, tmp_path, monkeypatch):
        """use_batch defaults OFF: harvest() without the kwarg never calls
        _batch_distill (back-compat for every existing caller/test)."""
        projects, fos, ledger = (tmp_path / "p", tmp_path / "fos",
                                 tmp_path / "l.jsonl")
        _setup_session_file(projects, "sess-batch-0005")
        monkeypatch.setattr(
            scap, "_batch_distill",
            lambda pending: (_ for _ in ()).throw(AssertionError("batched!")))
        monkeypatch.setattr(scap, "distill",
                            lambda *a, **k: _distilled("F3E", "plain"))
        results = scap.harvest(
            lookback_hours=24, dry_run=False, projects_root=projects,
            founder_os_root=fos, ledger_path=ledger, anthropic_client=None)
        assert results[0].distilled


class TestBatchDistillHelper:
    def test_builds_opaque_ids_and_maps_back(self, monkeypatch):
        monkeypatch.delenv("CORA_BATCH_DISABLE", raising=False)
        monkeypatch.delenv("CORA_BATCH_CAPTURE", raising=False)
        seen = {}

        def _fake_batch(requests, *, caller, deadline_s, **kw):
            seen.update(requests=requests, caller=caller, deadline_s=deadline_s)
            return {
                "item-0": _batch_msg(json.dumps(_distilled("OSN", "zero"))),
                "item-1": None,  # both transports failed for this one
            }
        monkeypatch.setattr(batch_client, "batch_generate", _fake_batch)
        pending = [
            (_session("sess-aaaa-1111", text="USER: first"), scap.SURFACE,
             "sess-aaaa-1111"),
            (_session("cw-2", text="USER: second"), scap.SURFACE_COWORK,
             "cowork:cw-2"),
        ]
        out = scap._batch_distill(pending)
        assert seen["caller"] == "session_capture"
        cids = [r["custom_id"] for r in seen["requests"]]
        assert cids == ["item-0", "item-1"]  # opaque: no session ids / PHI
        assert all("sess-aaaa" not in c for c in cids)
        assert seen["requests"][0]["params"]["model"] == scap._HAIKU_MODEL
        assert seen["requests"][0]["params"]["max_tokens"] == 1500
        assert "USER: first" in seen["requests"][0]["params"]["messages"][0]["content"]
        assert out["sess-aaaa-1111"]["entity"] == "OSN"
        assert out["cowork:cw-2"] is None

    def test_leg_flag_disables_batch(self, monkeypatch):
        monkeypatch.setenv("CORA_BATCH_CAPTURE", "0")
        monkeypatch.setattr(
            batch_client, "batch_generate",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")))
        assert scap._batch_distill([(_session(), scap.SURFACE, "k")]) == {}

    def test_helper_error_returns_empty_never_raises(self, monkeypatch):
        monkeypatch.delenv("CORA_BATCH_CAPTURE", raising=False)
        monkeypatch.setattr(
            batch_client, "batch_generate",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        assert scap._batch_distill([(_session(), scap.SURFACE, "k")]) == {}

    def test_phi_session_gets_phi_cap(self, monkeypatch):
        """A PHI-flagged transcript keeps the LARGER input cap in the batch
        prompt, exactly as the sync distill path does."""
        monkeypatch.delenv("CORA_BATCH_CAPTURE", raising=False)
        monkeypatch.setattr(scap.phi_guard, "is_phi_risk", lambda t: True)
        long_text = "x" * (scap._MAX_INPUT_CHARS + 5000)
        seen = {}

        def _fake_batch(requests, **kw):
            seen["prompt"] = requests[0]["params"]["messages"][0]["content"]
            return {"item-0": None}
        monkeypatch.setattr(batch_client, "batch_generate", _fake_batch)
        scap._batch_distill([(_session(text=long_text), scap.SURFACE, "k")])
        # Under the non-PHI cap the transcript would have been truncated to
        # _MAX_INPUT_CHARS; the PHI cap admits the full text.
        assert "x" * (scap._MAX_INPUT_CHARS + 1) in seen["prompt"]

    def test_deadline_env_honored(self, monkeypatch):
        monkeypatch.delenv("CORA_BATCH_CAPTURE", raising=False)
        monkeypatch.setenv("CORA_BATCH_CAPTURE_DEADLINE_S", "300")
        seen = {}

        def _fake_batch(requests, *, deadline_s, **kw):
            seen["deadline"] = deadline_s
            return {}
        monkeypatch.setattr(batch_client, "batch_generate", _fake_batch)
        scap._batch_distill([(_session(), scap.SURFACE, "k")])
        assert seen["deadline"] == 300.0
