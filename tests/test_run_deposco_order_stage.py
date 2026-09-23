"""Tests for the order-stage CLI (SONNET-HANDOFF step 5).

D-051 review, 2026-09-23: this is the actual human-facing tool (the CLI
Harrison runs per the 2026-09-09 ruling item 5) and had zero dedicated
tests -- none of its four documented exit codes, its --dry-run branch, its
unreadable-file branch, or its "card not delivered" warning were exercised.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from cora.deposco_orders import handler  # noqa: E402
from cora.deposco_orders import preflight  # noqa: E402

import run_deposco_order_stage as cli  # noqa: E402

VALID_SPEC_YAML = """
channel: wholesale
buyer_or_fc_code: GOTHAM
reference: "4471"
authored_by: Harrison
freight_terms: Prepaid
lines:
  - sku: PURE-Original
    qty: 208
    unit_price: "21.70"
"""

PASSED_PREFLIGHT = preflight.PreflightResult(
    passed=True, checked_at="2026-09-23T10:00:00-07:00",
    checks=[preflight.PreflightCheck("number_miss", True)],
)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CORA_DEPOSCO_PENDING_DIR", str(tmp_path / "pending"))
    monkeypatch.setenv("CORA_DEPOSCO_PUSH_LEDGER_PATH", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("CORA_DEPOSCO_DEMOTION_STATE_PATH", str(tmp_path / "demotion.json"))
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)


def _write_spec(tmp_path, text=VALID_SPEC_YAML):
    path = tmp_path / "order-spec.yaml"
    path.write_text(text, encoding="utf-8")
    return path


class TestDryRun:
    def test_dry_run_prints_the_payload_and_exits_zero(self, tmp_path, capsys):
        spec_path = _write_spec(tmp_path)
        code = cli.main(["--spec", str(spec_path), "--dry-run"])
        assert code == 0
        out = capsys.readouterr().out
        assert '"type": "Sales Order"' in out
        assert "nothing staged, nothing sent" in out

    def test_dry_run_stages_nothing(self, tmp_path, monkeypatch):
        spec_path = _write_spec(tmp_path)
        monkeypatch.setattr(
            cli.handler, "stage_order",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("dry-run must not stage")),
        )
        assert cli.main(["--spec", str(spec_path), "--dry-run"]) == 0


class TestSpecInvalidExitCode:
    def test_unreadable_file_exits_2(self, tmp_path, capsys):
        missing = tmp_path / "does-not-exist.yaml"
        code = cli.main(["--spec", str(missing)])
        assert code == 2
        assert "cannot read" in capsys.readouterr().out

    def test_invalid_yaml_syntax_exits_2(self, tmp_path, capsys):
        spec_path = _write_spec(tmp_path, "channel: [unterminated")
        code = cli.main(["--spec", str(spec_path)])
        assert code == 2
        assert "SPEC INVALID" in capsys.readouterr().out

    def test_failed_schema_validation_exits_2(self, tmp_path, capsys):
        spec_path = _write_spec(tmp_path, "channel: not-a-real-channel\n")
        code = cli.main(["--spec", str(spec_path)])
        assert code == 2
        assert "SPEC INVALID" in capsys.readouterr().out


class TestStageBlockedExitCode:
    def test_channel_stage_blocked_exits_3(self, tmp_path, monkeypatch, capsys):
        spec_path = _write_spec(tmp_path)
        monkeypatch.setattr(
            cli.handler, "stage_order",
            lambda *a, **k: handler.StageOutcome(ok=False, blocked_reason="last push was not clean"),
        )
        code = cli.main(["--spec", str(spec_path), "--env", "prod"])
        assert code == 3
        assert "STAGE BLOCKED" in capsys.readouterr().out


class TestLivePreflightFailedExitCode:
    def test_preflight_failure_exits_4(self, tmp_path, monkeypatch, capsys):
        spec_path = _write_spec(tmp_path)
        monkeypatch.setattr(
            cli.handler, "stage_order",
            lambda *a, **k: handler.StageOutcome(ok=False, errors=["PURE-Original: ATP short"]),
        )
        code = cli.main(["--spec", str(spec_path), "--env", "prod"])
        assert code == 4
        assert "LIVE PREFLIGHT FAILED" in capsys.readouterr().out


class TestSuccessfulStage:
    def test_a_clean_stage_exits_zero_and_reports_the_number(self, tmp_path, monkeypatch, capsys):
        spec_path = _write_spec(tmp_path)
        staged_entry = {
            "id": "deposco-abc123", "number": "F3E-W-GOTHAM-4471", "state": "STAGED",
            "dm_channel_id": "", "dm_message_ts": "",
        }
        monkeypatch.setattr(
            cli.handler, "stage_order",
            lambda *a, **k: handler.StageOutcome(
                ok=True, entry=staged_entry, card_fallback="fallback", card_blocks=[],
            ),
        )
        monkeypatch.setattr(cli.handler, "deliver_card", lambda *a, **k: staged_entry)
        code = cli.main(["--spec", str(spec_path), "--env", "prod"])
        assert code == 0
        out = capsys.readouterr().out
        assert "F3E-W-GOTHAM-4471" in out
        assert "WARNING" in out and "NOT delivered" in out

    def test_a_delivered_card_says_so_not_a_warning(self, tmp_path, monkeypatch, capsys):
        spec_path = _write_spec(tmp_path)
        staged_entry = {
            "id": "deposco-abc123", "number": "F3E-W-GOTHAM-4471", "state": "STAGED",
            "dm_channel_id": "D0HARRISON", "dm_message_ts": "1700000000.000001",
        }
        monkeypatch.setattr(
            cli.handler, "stage_order",
            lambda *a, **k: handler.StageOutcome(
                ok=True, entry=staged_entry, card_fallback="fallback", card_blocks=[],
            ),
        )
        monkeypatch.setattr(cli.handler, "deliver_card", lambda *a, **k: staged_entry)
        code = cli.main(["--spec", str(spec_path), "--env", "prod"])
        assert code == 0
        out = capsys.readouterr().out
        assert "Card delivered to Harrison's DM." in out
        assert "WARNING" not in out
