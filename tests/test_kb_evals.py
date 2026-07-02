"""WS-3 golden-set eval harness: loader, guard layer, L1/L2 logic, isolation,
auto-growth, and the poisoned-fixture proof that the harness CAN fail.
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

_SCRIPTS = str(Path(__file__).resolve().parents[1] / "scripts")
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

import run_kb_evals as rke  # noqa: E402
import cora.golden_set as gs  # noqa: E402

# run_kb_evals sets CORA_EVAL_MODE=1 at IMPORT (correct for the CLI: gates must
# be armed before any cora import). In pytest that import happens at COLLECTION
# and would leak eval mode into every other test in the session -- pop it here,
# at module import, with plain os.environ. (A monkeypatch.delenv in a fixture
# teardown does NOT work: monkeypatch's own teardown restores the var after
# undoing the delenv -- that exact leak broke 51 unrelated tests.)
os.environ.pop("CORA_EVAL_MODE", None)


@pytest.fixture(autouse=True)
def _eval_mode_cleanup():
    # Direct os.environ cleanup on both sides of every test in this module;
    # tests that need eval mode set it explicitly via monkeypatch.setenv.
    os.environ.pop("CORA_EVAL_MODE", None)
    yield
    os.environ.pop("CORA_EVAL_MODE", None)


# ── loader ───────────────────────────────────────────────────────────────────

class TestLoader:
    def test_repo_golden_set_parses_and_is_lex_clean(self):
        cases = rke.load_cases()
        assert len(cases) >= 20
        ids = [c["id"] for c in cases]
        assert len(ids) == len(set(ids)), "duplicate case ids"
        # PHI rule: no content case may target a LEX entity; LEX appears only
        # in must-refuse probes phrased WITHOUT client facts.
        for c in cases:
            if not c.get("guard"):
                assert not (c.get("entity") or "").upper().startswith("LEX"), c["id"]

    def test_only_id_filter(self):
        cases = rke.load_cases(only_id="guard-d064-pnl-blocked")
        assert len(cases) == 1


# ── guard layer (deterministic canon) ────────────────────────────────────────

class TestGuardLayer:
    def _case(self, cid):
        (case,) = rke.load_cases(only_id=cid)
        return case

    @pytest.mark.parametrize("cid", [
        "guard-d064-wholesale-price-pass",
        "guard-d064-invoice-status-pass",
        "guard-d064-deal-value-pass",
        "guard-d064-po-amount-pass",
        "guard-d064-pnl-blocked",
        "guard-d064-cash-blocked",
        "guard-d064-payroll-blocked",
        "guard-d064-bare-financials-blocked",
        "guard-firewall-lex-from-f3e",
        "guard-firewall-osn-from-f3e",
        "guard-firewall-own-entity-pass",
        "guard-phi-clinical-flagged",
        "guard-phi-clinical-wellness-pass",
    ])
    def test_seeded_guard_canon_passes(self, cid):
        ok, detail = rke.run_guard_case(self._case(cid))
        assert ok, f"{cid}: {detail}"

    def test_unknown_guard_kind_fails_loudly(self):
        ok, detail = rke.run_guard_case({"guard": "nope", "question": "x",
                                         "expect": "pass"})
        assert not ok and "unknown guard kind" in detail


# ── L1 content layer ─────────────────────────────────────────────────────────

class TestL1:
    def test_satisfied_by_static(self):
        with patch.object(rke, "load_cases"), \
             patch("cora.context_loader.load_context_parts",
                   return_value=("The tagline is Real energy for real life.", "")):
            ok, detail = rke.run_l1_case({
                "id": "x", "entity": "F3E", "question": "tagline?",
                "expect_substring": "real energy for REAL life",
            })
        assert ok and detail == "satisfied_by=static"

    def test_satisfied_by_kb(self):
        with patch("cora.context_loader.load_context_parts",
                   return_value=("", "chunk: MickyF3 is the code")):
            ok, detail = rke.run_l1_case({
                "id": "x", "entity": "F3E", "question": "code?",
                "expect_substring": "MickyF3",
            })
        assert ok and detail == "satisfied_by=kb"

    def test_poisoned_fixture_fails(self):
        # Acceptance #5: the harness must be able to FAIL. A case demanding
        # content that exists nowhere goes red.
        with patch("cora.context_loader.load_context_parts",
                   return_value=("some static", "some kb")):
            ok, detail = rke.run_l1_case({
                "id": "fixture-must-fail", "entity": "F3E",
                "question": "anything?",
                "expect_substring": "THIS-CONTENT-EXISTS-NOWHERE-12345",
            })
        assert ok is False
        assert "absent" in detail

    def test_context_error_fails_soft(self):
        with patch("cora.context_loader.load_context_parts",
                   side_effect=RuntimeError("db locked")):
            ok, detail = rke.run_l1_case({
                "id": "x", "entity": "F3E", "question": "q?",
                "expect_substring": "y",
            })
        assert ok is False and "context assembly failed" in detail


# ── L2 answer layer ──────────────────────────────────────────────────────────

class TestL2:
    def _run(self, reply, layer2):
        with patch("cora.context_loader.load_context_parts",
                   return_value=("static", "kb")), \
             patch("cora.prompt_loader.load_prompt", return_value="prompt"), \
             patch("cora.claude_client.generate_response", return_value=reply):
            return rke.run_l2_case({
                "id": "x", "entity": "F3E", "question": "q?",
                "layer2": layer2,
            })

    def test_must_contain_pass_and_fail(self):
        ok, _ = self._run("The code is MickyF3.", {"must_contain": ["mickyf3"]})
        assert ok
        ok, detail = self._run("No idea.", {"must_contain": ["mickyf3"]})
        assert not ok and "missing required content" in detail

    def test_must_not_contain(self):
        ok, detail = self._run("Standing ACTUALS says...",
                               {"must_not_contain": ["Standing ACTUALS"]})
        assert not ok and "forbidden" in detail

    def test_must_refuse_accepts_deflections(self):
        ok, _ = self._run("That's a legal matter. Reach Emily Stubbs.",
                          {"must_refuse": True})
        assert ok
        ok, _ = self._run("I'm not able to discuss that.", {"must_refuse": True})
        assert ok
        ok, detail = self._run("Sure! The client takes 5mg of...",
                               {"must_refuse": True})
        assert not ok

    def test_pipeline_error_fails_soft(self):
        with patch("cora.context_loader.load_context_parts",
                   side_effect=RuntimeError("boom")):
            ok, detail = rke.run_l2_case({"id": "x", "entity": "F3E",
                                          "question": "q?", "layer2": {}})
        assert not ok and "pipeline error" in detail


# ── isolation (lens R3) ──────────────────────────────────────────────────────

class TestIsolation:
    def test_eval_mode_offers_no_tools(self, monkeypatch):
        monkeypatch.setenv("CORA_EVAL_MODE", "1")
        from cora.tools.tool_dispatch import tools_for_entity
        assert tools_for_entity("FNDR", cross_entity=True) == []
        assert tools_for_entity("F3E") == []

    def test_eval_mode_dispatch_refuses(self, monkeypatch):
        monkeypatch.setenv("CORA_EVAL_MODE", "1")
        from cora.tools.tool_dispatch import dispatch
        out = dispatch("asana_get_my_tasks", {}, "U1", "F3E")
        assert "disabled in eval mode" in out

    def test_normal_mode_offers_tools(self, monkeypatch):
        monkeypatch.delenv("CORA_EVAL_MODE", raising=False)
        from cora.tools.tool_dispatch import tools_for_entity
        assert len(tools_for_entity("FNDR", cross_entity=True)) > 10

    def test_runner_has_no_forbidden_imports(self):
        # Grep-guard (test_no_raw_slack_post pattern): the runner must never
        # import the bot app, the semantic cache, the ledger writer, or
        # slack_sdk -- its ONLY egress is the sanitized summary post.
        src = (Path(_SCRIPTS) / "run_kb_evals.py").read_text(encoding="utf-8")
        for forbidden in ("from cora.app", "import cora.app",
                          "semantic_cache", "propose_update",
                          "slack_sdk", "WebClient"):
            assert forbidden not in src, f"run_kb_evals.py must not use {forbidden}"

    def test_runner_sets_eval_mode_after_dotenv_before_cora(self):
        # Ordering contract: AFTER load_dotenv(override=True) so a stray .env
        # line can't clobber the isolation flag, BEFORE the src path insert /
        # any cora import so every call-time gate is armed.
        src = (Path(_SCRIPTS) / "run_kb_evals.py").read_text(encoding="utf-8")
        set_at = src.index('os.environ["CORA_EVAL_MODE"]')
        assert src.index("load_dotenv(_REPO_ROOT") < set_at
        assert set_at < src.index('sys.path.insert(0, str(_REPO_ROOT / "src"))')


# ── summary / newly-failing state ────────────────────────────────────────────

class TestSummary:
    def test_newly_failing_and_fixed(self):
        results = [
            {"id": "a", "ok": True}, {"id": "b", "ok": False},
            {"id": "c", "ok": False}, {"id": "d", "ok": None},
        ]
        s = rke.summarize(results, prev_failing={"c", "z"})
        assert s["newly_failing"] == ["b"]
        assert s["fixed_since_last"] == ["z"]
        assert s["passed"] == 1 and s["failed"] == 2
        assert s["skipped_l2_only"] == 1

    def test_skipped_l2_case_keeps_failing_status(self):
        # Adversarial review LOW: a previously-failing L2-only case skipped by
        # a scheduled non---answers run must NOT be announced as "fixed".
        results = [{"id": "a", "ok": True}, {"id": "l2case", "ok": None}]
        s = rke.summarize(results, prev_failing={"l2case"})
        assert s["fixed_since_last"] == []
        assert "l2case" in s["failing_ids"]
        assert s["still_failing_unevaluated"] == ["l2case"]
        assert s["newly_failing"] == []

    def test_corpus_load_error_goes_red(self):
        s = rke.summarize([{"id": "a", "ok": True}], prev_failing=set(),
                          load_errors=["golden-set.yaml failed to parse: boom"])
        msg = rke.format_slack_summary(s)
        assert "corpus load ERROR" in msg
        assert "white_check_mark" not in msg

    def test_slack_summary_renders(self):
        s = rke.summarize([{"id": "a", "ok": True}], prev_failing=set())
        msg = rke.format_slack_summary(s)
        assert "KB evals" in msg and "1/1 passed" in msg

    def test_load_errors_surface_for_broken_hand_file(self, tmp_path, monkeypatch):
        # A broken HAND corpus is a harness error, never a silent shrink
        # (adversarial review MEDIUM).
        evals = tmp_path / "evals"
        evals.mkdir()
        (evals / "golden-set.yaml").write_text("cases: [unclosed",
                                               encoding="utf-8")
        monkeypatch.setattr(rke, "_EVALS_DIR", evals)
        errors: list[str] = []
        cases = rke.load_cases(load_errors=errors)
        assert cases == []
        assert errors and "failed to parse" in errors[0]

    def test_numeric_expect_substring_does_not_crash(self):
        with patch("cora.context_loader.load_context_parts",
                   return_value=("size is 2880 wide", "")):
            ok, detail = rke.run_l1_case({
                "id": "x", "entity": "F3E", "question": "size?",
                "expect_substring": 2880,   # unquoted YAML numeric scalar
            })
        assert ok is True


# ── golden-set auto-growth ───────────────────────────────────────────────────

class TestAutoGrowth:
    @pytest.fixture(autouse=True)
    def _auto_path(self, tmp_path, monkeypatch):
        self.path = tmp_path / "golden-set-auto.yaml"
        monkeypatch.setenv("GOLDEN_SET_AUTO_PATH", str(self.path))
        return self.path

    def _cases(self):
        return (yaml.safe_load(self.path.read_text(encoding="utf-8")) or {})["cases"]

    def test_known_answer_appends_l1_case(self):
        ok = gs.append_case_from_known_answer({
            "entity": "F3E", "gap_ts": "2026-07-01T12:00:00+00:00",
            "question": "who is the stove vendor?",
            "answer": "Acme Appliance in Tucson handles the stove order.",
        })
        assert ok
        (case,) = self._cases()
        assert case["id"].startswith("auto-ka-")
        assert case["entity"] == "F3E"
        assert "Acme Appliance" in case["expect_substring"]
        assert "layer2" not in case  # auto cases are L1-only

    def test_idempotent_by_id(self):
        payload = {"entity": "F3E", "gap_ts": "2026-07-01T12:00:00+00:00",
                   "question": "q?", "answer": "a value here"}
        assert gs.append_case_from_known_answer(payload) is True
        assert gs.append_case_from_known_answer(payload) is False
        assert len(self._cases()) == 1

    def test_note_appends(self):
        ok = gs.append_case_from_note({
            "entity": "OSN", "text": "The Gilbert store closes at 7pm Sundays.",
        })
        assert ok
        (case,) = self._cases()
        assert case["id"].startswith("auto-note-")
        assert case["source"] == "contributed_note_approval"

    def test_lex_never_enters_corpus(self):
        assert gs.append_case_from_known_answer({
            "entity": "LEX-LLC", "gap_ts": "x",
            "question": "q?", "answer": "a",
        }) is False
        assert not self.path.exists()

    def test_phi_screen_blocks(self, monkeypatch):
        monkeypatch.setattr(gs, "is_phi_risk", lambda t: True)
        assert gs.append_case_from_known_answer({
            "entity": "F3E", "gap_ts": "x", "question": "q?", "answer": "a",
        }) is False

    def test_corrupt_auto_file_never_overwritten(self):
        self.path.write_text("cases: [unclosed", encoding="utf-8")
        before = self.path.read_text(encoding="utf-8")
        assert gs.append_case_from_known_answer({
            "entity": "F3E", "gap_ts": "x", "question": "q?", "answer": "a value",
        }) is False
        assert self.path.read_text(encoding="utf-8") == before

    def test_empty_fields_skipped(self):
        assert gs.append_case_from_known_answer(
            {"entity": "F3E", "question": "", "answer": "a"}) is False
        assert gs.append_case_from_note({"entity": "F3E", "text": ""}) is False
