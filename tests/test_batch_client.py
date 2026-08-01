"""Tests for cora.batch_client (batch-API pilot slice 2).

Pins the fail-soft contract the nightly legs rely on: any batch-layer failure
degrades to per-item sync calls; a poisoned item never sinks its batch-mates;
the deadline is firm (cancel + fallback, never wait); results key by
custom_id regardless of order; log lines never carry message content.
"""

import logging
from types import SimpleNamespace

import pytest

from cora import batch_client
from cora.batch_client import batch_enabled, batch_generate

SECRET = "SECRET-TRANSCRIPT-CONTENT"


def _msg(text="ok", model="claude-haiku-4-5"):
    return SimpleNamespace(
        model=model,
        usage=SimpleNamespace(input_tokens=10, cache_creation_input_tokens=0,
                              cache_read_input_tokens=0, output_tokens=5),
        content=[SimpleNamespace(type="text", text=text)],
    )


def _ok(cid, text="ok"):
    return SimpleNamespace(custom_id=cid,
                           result=SimpleNamespace(type="succeeded",
                                                  message=_msg(text)))


def _err(cid, kind="errored"):
    return SimpleNamespace(custom_id=cid, result=SimpleNamespace(type=kind))


class FakeBatches:
    def __init__(self, statuses=("ended",), results=(), submit_exc=None,
                 cancel_exc=None, results_exc=None):
        self._statuses = list(statuses)
        self._results = list(results)
        self._submit_exc = submit_exc
        self._cancel_exc = cancel_exc
        self._results_exc = results_exc
        self.created_with = None
        self.cancelled = []
        self.retrieve_calls = 0

    def create(self, requests):
        if self._submit_exc:
            raise self._submit_exc
        self.created_with = requests
        return SimpleNamespace(id="msgbatch_test", processing_status="in_progress")

    def retrieve(self, batch_id):
        self.retrieve_calls += 1
        status = self._statuses.pop(0) if self._statuses else "in_progress"
        if isinstance(status, Exception):
            raise status
        return SimpleNamespace(id=batch_id, processing_status=status)

    def results(self, batch_id):
        if self._results_exc:
            raise self._results_exc
        yield from self._results

    def cancel(self, batch_id):
        if self._cancel_exc:
            raise self._cancel_exc
        self.cancelled.append(batch_id)


class FakeMessages:
    def __init__(self, batches, sync_exc=None):
        self.batches = batches
        self._sync_exc = sync_exc
        self.sync_calls = []

    def create(self, **params):
        self.sync_calls.append(params)
        if self._sync_exc:
            raise self._sync_exc
        return _msg("sync-result")


class FakeClient:
    def __init__(self, batches, sync_exc=None):
        self.messages = FakeMessages(batches, sync_exc=sync_exc)


def _reqs(n=2):
    return [{"custom_id": f"item-{i}",
             "params": {"model": "claude-haiku-4-5", "max_tokens": 10,
                        "messages": [{"role": "user", "content": SECRET}]}}
            for i in range(n)]


class TestHappyPath:
    def test_all_succeed_keyed_by_custom_id_any_order(self, caplog):
        fb = FakeBatches(results=[_ok("item-1", "one"), _ok("item-0", "zero")])
        client = FakeClient(fb)
        with caplog.at_level(logging.INFO):
            out = batch_generate(_reqs(2), caller="t", client=client,
                                 deadline_s=5, poll_interval_s=0)
        assert out["item-0"].content[0].text == "zero"
        assert out["item-1"].content[0].text == "one"
        assert client.messages.sync_calls == []  # no fallback needed
        usage = [r.getMessage() for r in caplog.records
                 if "claude usage" in r.getMessage()]
        assert len(usage) == 2 and all("via=batch" in u for u in usage)

    def test_empty_request_list(self):
        assert batch_generate([], caller="t", client=FakeClient(FakeBatches())) == {}

    def test_polls_until_ended(self):
        fb = FakeBatches(statuses=["in_progress", "in_progress", "ended"],
                         results=[_ok("item-0")])
        out = batch_generate(_reqs(1), caller="t", client=FakeClient(fb),
                             deadline_s=5, poll_interval_s=0)
        assert fb.retrieve_calls == 3
        assert out["item-0"] is not None

    def test_transient_poll_error_keeps_polling(self):
        fb = FakeBatches(statuses=[RuntimeError("503"), "ended"],
                         results=[_ok("item-0")])
        out = batch_generate(_reqs(1), caller="t", client=FakeClient(fb),
                             deadline_s=5, poll_interval_s=0)
        assert out["item-0"] is not None


class TestFallbacks:
    def test_submit_failure_falls_back_sync_for_all(self, caplog):
        fb = FakeBatches(submit_exc=RuntimeError("400 too big"))
        client = FakeClient(fb)
        with caplog.at_level(logging.INFO):
            out = batch_generate(_reqs(3), caller="t", client=client,
                                 deadline_s=5, poll_interval_s=0)
        assert len(client.messages.sync_calls) == 3
        assert all(m is not None for m in out.values())
        usage = [r.getMessage() for r in caplog.records
                 if "claude usage" in r.getMessage()]
        assert len(usage) == 3 and all("via=sync-fallback" in u for u in usage)

    def test_poisoned_item_falls_back_alone(self):
        """D-051 #3: one errored item never sinks its batch-mates."""
        fb = FakeBatches(results=[_ok("item-0"), _err("item-1")])
        client = FakeClient(fb)
        out = batch_generate(_reqs(2), caller="t", client=client,
                             deadline_s=5, poll_interval_s=0)
        assert out["item-0"].content[0].text == "ok"
        assert out["item-1"].content[0].text == "sync-result"
        assert len(client.messages.sync_calls) == 1
        assert client.messages.sync_calls[0]["messages"][0]["content"] == SECRET

    def test_expired_and_canceled_items_fall_back(self):
        fb = FakeBatches(results=[_err("item-0", "expired"),
                                  _err("item-1", "canceled")])
        client = FakeClient(fb)
        out = batch_generate(_reqs(2), caller="t", client=client,
                             deadline_s=5, poll_interval_s=0)
        assert len(client.messages.sync_calls) == 2
        assert all(m is not None for m in out.values())

    def test_unreported_item_falls_back(self):
        fb = FakeBatches(results=[_ok("item-0")])  # item-1 never reported
        client = FakeClient(fb)
        out = batch_generate(_reqs(2), caller="t", client=client,
                             deadline_s=5, poll_interval_s=0)
        assert out["item-1"].content[0].text == "sync-result"

    def test_results_iteration_error_falls_back_all(self):
        fb = FakeBatches(results_exc=RuntimeError("stream died"))
        client = FakeClient(fb)
        out = batch_generate(_reqs(2), caller="t", client=client,
                             deadline_s=5, poll_interval_s=0)
        assert len(client.messages.sync_calls) == 2
        assert all(m is not None for m in out.values())

    def test_deadline_cancels_and_falls_back(self, caplog):
        fb = FakeBatches(statuses=["in_progress"] * 50)
        client = FakeClient(fb)
        with caplog.at_level(logging.WARNING):
            out = batch_generate(_reqs(2), caller="t", client=client,
                                 deadline_s=0.01, poll_interval_s=0)
        assert fb.cancelled == ["msgbatch_test"]
        assert len(client.messages.sync_calls) == 2
        assert all(m is not None for m in out.values())
        assert any("DEADLINE" in r.getMessage() for r in caplog.records)

    def test_deadline_cancel_failure_still_falls_back(self):
        fb = FakeBatches(statuses=["in_progress"] * 50,
                         cancel_exc=RuntimeError("409"))
        client = FakeClient(fb)
        out = batch_generate(_reqs(1), caller="t", client=client,
                             deadline_s=0.01, poll_interval_s=0)
        assert out["item-0"] is not None

    def test_sync_fallback_disabled_returns_none(self):
        fb = FakeBatches(submit_exc=RuntimeError("down"))
        client = FakeClient(fb)
        out = batch_generate(_reqs(2), caller="t", client=client,
                             deadline_s=5, poll_interval_s=0,
                             sync_fallback=False)
        assert out == {"item-0": None, "item-1": None}
        assert client.messages.sync_calls == []

    def test_both_transports_fail_returns_none(self):
        fb = FakeBatches(submit_exc=RuntimeError("down"))
        client = FakeClient(fb, sync_exc=RuntimeError("also down"))
        out = batch_generate(_reqs(1), caller="t", client=client,
                             deadline_s=5, poll_interval_s=0)
        assert out == {"item-0": None}


class TestValidation:
    def test_duplicate_custom_id_raises(self):
        reqs = _reqs(1) + _reqs(1)
        with pytest.raises(ValueError, match="duplicate"):
            batch_generate(reqs, caller="t", client=FakeClient(FakeBatches()))

    @pytest.mark.parametrize("bad", ["has:colon", "has space", "", "x" * 65, None])
    def test_invalid_custom_id_raises(self, bad):
        reqs = [{"custom_id": bad, "params": {"model": "m"}}]
        with pytest.raises(ValueError, match="custom_id"):
            batch_generate(reqs, caller="t", client=FakeClient(FakeBatches()))

    def test_missing_params_raises(self):
        with pytest.raises(ValueError, match="params"):
            batch_generate([{"custom_id": "a"}], caller="t",
                           client=FakeClient(FakeBatches()))


class TestNoContentLogging:
    def test_payload_and_results_never_logged(self, caplog):
        """D-051 #4 (local half): ids/counts/usage only -- never content."""
        fb = FakeBatches(results=[_ok("item-0", SECRET), _err("item-1")])
        client = FakeClient(fb)
        with caplog.at_level(logging.DEBUG):
            batch_generate(_reqs(2), caller="t", client=client,
                           deadline_s=5, poll_interval_s=0)
        assert SECRET not in caplog.text

    def test_no_content_logged_on_deadline_path(self, caplog):
        fb = FakeBatches(statuses=["in_progress"] * 50)
        client = FakeClient(fb)
        with caplog.at_level(logging.DEBUG):
            batch_generate(_reqs(1), caller="t", client=client,
                           deadline_s=0.01, poll_interval_s=0)
        assert SECRET not in caplog.text


class TestBatchEnabled:
    def test_default_on(self, monkeypatch):
        monkeypatch.delenv("CORA_BATCH_DISABLE", raising=False)
        monkeypatch.delenv("CORA_BATCH_SYNTHESIS", raising=False)
        assert batch_enabled("CORA_BATCH_SYNTHESIS") is True

    @pytest.mark.parametrize("off", ["0", "off", "false", "no", "OFF"])
    def test_leg_flag_disables(self, monkeypatch, off):
        monkeypatch.delenv("CORA_BATCH_DISABLE", raising=False)
        monkeypatch.setenv("CORA_BATCH_SYNTHESIS", off)
        assert batch_enabled("CORA_BATCH_SYNTHESIS") is False

    def test_global_kill_switch_wins(self, monkeypatch):
        monkeypatch.setenv("CORA_BATCH_DISABLE", "1")
        monkeypatch.setenv("CORA_BATCH_SYNTHESIS", "1")
        assert batch_enabled("CORA_BATCH_SYNTHESIS") is False

    def test_global_switch_off_value_is_noop(self, monkeypatch):
        monkeypatch.setenv("CORA_BATCH_DISABLE", "0")
        monkeypatch.delenv("CORA_BATCH_SYNTHESIS", raising=False)
        assert batch_enabled("CORA_BATCH_SYNTHESIS") is True

    def test_leg_flag_truthy_enables(self, monkeypatch):
        monkeypatch.delenv("CORA_BATCH_DISABLE", raising=False)
        monkeypatch.setenv("CORA_BATCH_SYNTHESIS", "1")
        assert batch_enabled("CORA_BATCH_SYNTHESIS") is True


class TestD047Purity:
    def test_top_level_imports_are_clean(self):
        """batch_client must be importable by D-047 standalone modules:
        no bot-process modules, and anthropic only lazily (inside functions)."""
        import ast
        from pathlib import Path
        src = (Path(__file__).parent.parent / "src" / "cora" / "batch_client.py"
               ).read_text(encoding="utf-8")
        tree = ast.parse(src)
        top: set[str] = set()
        for node in tree.body:  # top-level statements only
            if isinstance(node, ast.Import):
                top.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                top.add(node.module or "")
        assert top <= {"logging", "os", "re", "time", "typing", "__future__",
                       "llm_usage"}, top
