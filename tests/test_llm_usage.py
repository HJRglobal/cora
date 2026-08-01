"""Tests for cora.llm_usage + the cora_health_report billing parse extension.

Slice 1 of the 2026-07-31 batch-API pilot: every direct anthropic call site
outside claude_client logs a uniform usage line; the health report's billing
parse buckets bot lines (no caller=) vs script lines (caller=) and estimates
script-side $ spend. These tests pin:
  * the exact line format (prefix identical to claude_client._log_usage);
  * round-trip compatibility with cora_health_report._USAGE_RE for legacy
    (bot), extended (model=/caller=), and batch (via=) lines;
  * the never-raises / never-logs-content contract;
  * recent_billing()'s bot-vs-script bucketing + per-caller $ estimate.
"""

import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import cora_health_report as chr_mod  # type: ignore  # noqa: E402

from cora.llm_usage import log_usage  # noqa: E402


def _resp(input_tokens=100, cache_create=10, cache_read=20, output=30,
          model="claude-haiku-4-5"):
    return SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=input_tokens,
            cache_creation_input_tokens=cache_create,
            cache_read_input_tokens=cache_read,
            output_tokens=output,
        ),
        model=model,
    )


def _emit(caplog, **kwargs):
    with caplog.at_level(logging.INFO, logger="cora.llm_usage"):
        log_usage(kwargs.pop("response", _resp()), **kwargs)
    lines = [r.getMessage() for r in caplog.records
             if "claude usage" in r.getMessage()]
    assert len(lines) == 1, f"expected exactly one usage line, got {lines}"
    return lines[0]


class TestLineFormat:
    def test_basic_line(self, caplog):
        line = _emit(caplog, caller="gap_autofill")
        assert line == ("claude usage iter=1 input=100 cache_create=10 "
                        "cache_read=20 output=30 model=claude-haiku-4-5 "
                        "caller=gap_autofill")

    def test_prefix_identical_to_claude_client_format(self, caplog):
        """The prefix (through output=) must be byte-identical to the bot line
        claude_client._log_usage emits, so one regex parses both."""
        line = _emit(caplog, caller="x")
        bot_line = ("claude usage iter=%d input=%d cache_create=%d "
                    "cache_read=%d output=%d" % (1, 100, 10, 20, 30))
        assert line.startswith(bot_line)

    def test_explicit_model_overrides_response(self, caplog):
        line = _emit(caplog, caller="x", model="claude-sonnet-5")
        assert " model=claude-sonnet-5 " in line

    def test_model_falls_back_to_response_attr(self, caplog):
        line = _emit(caplog, caller="x", response=_resp(model="claude-opus-5"))
        assert " model=claude-opus-5 " in line

    def test_via_suffix(self, caplog):
        line = _emit(caplog, caller="x", via="batch")
        assert line.endswith(" via=batch")

    def test_iteration_field(self, caplog):
        line = _emit(caplog, caller="x", iteration=3)
        assert "claude usage iter=3 " in line

    def test_whitespace_in_tokens_sanitized(self, caplog):
        line = _emit(caplog, caller="my caller", model="weird model")
        assert " model=weird_model " in line
        assert " caller=my_caller" in line

    def test_missing_model_renders_dash(self, caplog):
        resp = _resp()
        resp.model = ""
        line = _emit(caplog, caller="x", response=resp)
        assert " model=- " in line


class TestNeverRaises:
    def test_no_usage_attr_logs_nothing(self, caplog):
        with caplog.at_level(logging.INFO, logger="cora.llm_usage"):
            log_usage(SimpleNamespace(), caller="x")
            log_usage(None, caller="x")
        assert not [r for r in caplog.records if "claude usage" in r.getMessage()]

    def test_magicmock_response_never_raises(self):
        log_usage(MagicMock(), caller="x")  # mock ints coerce or skip silently

    def test_malformed_usage_never_raises(self):
        bad = SimpleNamespace(usage=SimpleNamespace(
            input_tokens="not-an-int", cache_creation_input_tokens=None,
            cache_read_input_tokens=object(), output_tokens=[]), model="m")
        log_usage(bad, caller="x")

    def test_never_logs_message_content(self, caplog):
        """The usage line must carry ONLY counters/ids -- no payload text."""
        resp = _resp()
        resp.content = [SimpleNamespace(type="text", text="SECRET-PAYLOAD")]
        line = _emit(caplog, caller="x", response=resp)
        assert "SECRET-PAYLOAD" not in line


class TestHealthReportRegexRoundTrip:
    def test_legacy_bot_line_parses_without_caller(self):
        m = chr_mod._USAGE_RE.search(
            "2026-07-31 [INFO] cora.claude_client -- claude usage iter=2 "
            "input=37589 cache_create=0 cache_read=68706 output=512")
        assert m is not None
        assert m.group(2) == "37589" and m.group(5) == "512"
        assert m.group("caller") is None and m.group("model") is None

    def test_extended_line_parses_with_model_and_caller(self, caplog):
        line = _emit(caplog, caller="session_capture")
        m = chr_mod._USAGE_RE.search(line)
        assert m is not None
        assert (int(m.group(2)), int(m.group(3)), int(m.group(4)),
                int(m.group(5))) == (100, 10, 20, 30)
        assert m.group("model") == "claude-haiku-4-5"
        assert m.group("caller") == "session_capture"

    def test_via_line_still_parses(self, caplog):
        line = _emit(caplog, caller="x", via="sync-fallback")
        m = chr_mod._USAGE_RE.search(line)
        assert m is not None and m.group("caller") == "x"


class TestRecentBillingBuckets:
    def _write(self, path: Path, lines: list[str]) -> None:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_bot_and_script_buckets_split(self, tmp_path, monkeypatch):
        monkeypatch.setattr(chr_mod, "LOGS_DIR", tmp_path)
        bot = ("2026-07-30 10:00:00 [INFO] cora.claude_client -- claude usage "
               "iter=1 input=1000 cache_create=0 cache_read=500 output=200")
        script_in_cora = ("2026-07-30 06:31:00 [INFO] cora.llm_usage -- claude usage "
                          "iter=1 input=4000 cache_create=0 cache_read=0 output=800 "
                          "model=claude-sonnet-5 caller=channel_synthesis")
        self._write(tmp_path / "cora-2026-07-30.log", [bot, script_in_cora])
        self._write(tmp_path / "session-capture-2026-07-30.log", [
            "2026-07-30 05:15:00 INFO cora.llm_usage: claude usage iter=1 "
            "input=20000 cache_create=0 cache_read=0 output=1000 "
            "model=claude-haiku-4-5 caller=session_capture",
            "2026-07-30 05:15:30 INFO cora.llm_usage: claude usage iter=1 "
            "input=10000 cache_create=0 cache_read=0 output=500 "
            "model=claude-haiku-4-5 caller=session_capture via=batch",
        ])
        # A non-dated log never enters the window.
        self._write(tmp_path / "random.log", [script_in_cora])

        b = chr_mod.recent_billing(3)
        # Bot bucket: ONLY the caller-less cora-*.log line.
        assert b["usage_lines"] == 1
        assert b["median_input"] == 1000
        # Script bucket: 1 synthesis + 2 capture lines.
        assert b["script_lines"] == 3
        su = b["script_usage"]
        assert su["session_capture"]["calls"] == 2
        assert su["session_capture"]["input"] == 30000
        assert su["channel_synthesis"]["calls"] == 1
        # $ estimate: sonnet 4000*3 + 800*15 = 0.024; haiku 30000*1 + 1500*5 = 0.0375
        assert su["channel_synthesis"]["est_usd"] == pytest.approx(0.024, abs=1e-4)
        assert su["session_capture"]["est_usd"] == pytest.approx(0.0375, abs=1e-4)
        assert b["script_est_usd"] == pytest.approx(0.0615, abs=1e-4)
        assert b["script_est_incomplete"] is False

    def test_unknown_model_marks_estimate_incomplete(self, tmp_path, monkeypatch):
        monkeypatch.setattr(chr_mod, "LOGS_DIR", tmp_path)
        self._write(tmp_path / "cora-2026-07-30.log", [
            "x claude usage iter=1 input=100 cache_create=0 cache_read=0 "
            "output=10 model=mystery-model caller=foo",
        ])
        b = chr_mod.recent_billing(1)
        assert b["script_est_incomplete"] is True
        assert b["script_usage"]["foo"]["est_usd"] == 0.0

    def test_empty_logs_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(chr_mod, "LOGS_DIR", tmp_path / "nope")
        b = chr_mod.recent_billing(3)
        assert b["usage_lines"] == 0 and b["script_lines"] == 0


class TestD047Purity:
    def test_llm_usage_imports_no_bot_modules(self):
        """llm_usage must stay stdlib-only so D-047 standalone modules
        (channel_synthesis / strategy_memo / friction_mining / session_capture)
        can import it without pulling bot-process modules. AST-parse the
        actual import statements (the docstring may NAME those modules)."""
        import ast
        src = (Path(__file__).parent.parent / "src" / "cora" / "llm_usage.py"
               ).read_text(encoding="utf-8")
        imported: set[str] = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
        assert imported <= {"logging", "typing", "__future__"}, imported
