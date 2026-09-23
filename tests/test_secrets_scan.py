"""Secrets scan (DR/VM step 1, kickoff section 4 #5; charter D3: key NAMES only, never values).

Contract under test:
  * every known token shape is a hit; the same shape wrapped in placeholder words is not;
  * a `KEY=value` line whose KEY is in .env.example is a hit unless the value is a
    placeholder / flag / path / short literal;
  * the live-.env BELT flags a real value wherever it appears and reports the KEY NAME,
    never the value (the rendered hit line must not contain it);
  * the CI guard: deployment/, docs/ (if present), README.md, CLAUDE.md, the manifest
    generator and its emitted files carry ZERO hits (shape + env-assignment rules; the
    live belt too when a .env exists on this host).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import secrets_scan as ss  # noqa: E402

_KEYS = {"SLACK_BOT_TOKEN", "OPENAI_API_KEY", "HEALTH_PING_URL", "QBO_ENVIRONMENT", "CORA_MCP_HTTP_KEY"}


class TestShapes:
    def test_real_looking_tokens_hit_and_placeholders_do_not(self):
        # Fixtures are ASSEMBLED at runtime: the repo's .githooks/pre-commit greps staged
        # files for live key prefixes (the Slack bot/app token prefixes followed by a digit,
        # the Anthropic key prefix, the PEM header ...), so a real-looking literal at rest
        # would block the commit -- and rightly so. The joined strings still match the
        # scanner's shapes.
        real = "\n".join([
            "token: " + "xoxb-" + "1234567890123-1234567890123-AbCdEfGhIjKlMnOpQrStUvWx",
            "app: " + "xapp-1-" + "A0123456789-1234567890123-abcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcdefabcd",
            "g: AIzaSyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r",
            "sk: " + "sk-ant-" + "api03-abcdefghijklmnopqrstuvwxyz0123456789",
            "aws: AKIAIOSFODNN7EXAMPLQ",
            "asana: 2/1234567890123456:abcdefghijklmnopqrstuvwxyz012345",
            "ping: https://hc-ping.com/12345678-1234-1234-1234-123456789abc",
            "-----BEGIN " + "PRIVATE KEY-----",
        ])
        kinds = sorted(h.kind + ":" + h.key for h in ss.scan_text(real, "x.md", env_keys=set()))
        assert kinds == sorted(["shape:slack-token", "shape:slack-app-token", "shape:google-api-key",
                                "shape:anthropic-openai-key", "shape:aws-access-key", "shape:asana-pat",
                                "shape:healthchecks-ping", "shape:pem-private-key"])
        fake = "\n".join([
            "SLACK_BOT_TOKEN=xoxb-your-bot-token-here",
            "xoxb-test-dummy-token-for-ci",
            "sk-ant-test-dummy-key-for-ci",
            "https://hc-ping.com/<uuid>",
            "AIza...",
        ])
        assert ss.scan_text(fake, "x.md", env_keys=_KEYS) == []

    def test_env_assignment_rule(self):
        text = "\n".join([
            "OPENAI_API_KEY=<value>",                  # placeholder
            "QBO_ENVIRONMENT=production",              # literal
            "CORA_MCP_HTTP_KEY=data/state/mcp-tls/key.pem",   # path
            "HEALTH_PING_URL=https://hc-ping.com/your-uuid-here",   # placeholder word
            "OPENAI_API_KEY=abcdefghijklmnopqrstuvwxyz0123456789",  # REAL-looking -> hit
            "NOT_A_KNOWN_KEY=abcdefghijklmnopqrstuvwxyz",           # not in .env.example -> ignored
            "INFLUENCER_SCAN_NOTIFY_CHANNEL=C0B6GT3117Y",            # identifier key, id value -> fine
            "HUBSPOT_PORTAL_ID=246351746",                           # identifier -> fine
            "CORA_DRIVE_ROOT_FOLDER_ID=1I7zWcCIAOx7zdzIXcxx6WTLk1K40eizj",   # Drive id under an _ID key -> fine
        ])
        keys = _KEYS | {"INFLUENCER_SCAN_NOTIFY_CHANNEL", "HUBSPOT_PORTAL_ID", "CORA_DRIVE_ROOT_FOLDER_ID"}
        hits = ss.scan_text(text, "x.md", env_keys=keys)
        assert [(h.kind, h.key, h.line) for h in hits] == [("env-assignment", "OPENAI_API_KEY", 5)]

    def test_live_belt_names_the_key_never_the_value(self):
        live = {"SLACK_BOT_TOKEN": "supersecretvalue-abcdef123456"}
        hits = ss.scan_text("the token is supersecretvalue-abcdef123456 ok", "r.md", env_keys=set(), live_values=live)
        assert len(hits) == 1 and hits[0].kind == "live-env-value" and hits[0].key == "SLACK_BOT_TOKEN"
        assert "supersecretvalue" not in hits[0].render()

    def test_live_secret_values_selects_secret_shaped_keys_only(self, tmp_path):
        env = tmp_path / ".env"
        env.write_text("\n".join([
            "SLACK_BOT_TOKEN=xoxb-real-looking-1234567890",
            "HUBSPOT_PORTAL_ID=246351746",            # _ID -> excluded (non-secret)
            "CORA_MCP_HTTP_KEY=data/state/mcp-tls/k.pem",   # path -> excluded
            "QBO_ENVIRONMENT=production",             # literal -> excluded
            "SHORT_SECRET=abc",                       # < 12 chars -> excluded
            "MAKE_SALES_DECK_WEBHOOK_URL=https://hook.us1.make.com/abcdefghijklmnop",
        ]), encoding="utf-8")
        vals = ss.live_secret_values(env)
        assert set(vals) == {"SLACK_BOT_TOKEN", "MAKE_SALES_DECK_WEBHOOK_URL"}

    def test_env_example_keys_include_commented_and_empty_entries(self, tmp_path):
        ex = tmp_path / ".env.example"
        ex.write_text("# SLACK_USER_TOKEN=<value>\nOPENAI_API_KEY=<value>\nEMPTY_DOCUMENTED_KEY=\n# comment\n", encoding="utf-8")
        assert ss.env_example_keys(ex) == {"SLACK_USER_TOKEN", "OPENAI_API_KEY", "EMPTY_DOCUMENTED_KEY"} | set(ss.EXTRA_SECRET_KEYS)

    def test_the_passphrase_is_a_secret_whatever_its_shape(self, tmp_path):
        """CORA_BACKUP_PASSPHRASE decrypts every other secret; it has no token shape and may be
        short. It is a hit on a line, inline, and in the belt (D-051 lens B HIGH)."""
        keys = ss.env_example_keys(tmp_path / "absent.env.example")     # EXTRA keys survive an absent example file
        assert "CORA_BACKUP_PASSPHRASE" in keys
        text = "\n".join([
            "CORA_BACKUP_PASSPHRASE=correct horse battery",              # anchored
            "- set `CORA_BACKUP_PASSPHRASE=horsebattery9` in the User scope",  # inline
            "| CORA_BACKUP_PASSPHRASE | see the password manager |",     # a NAME in a table cell: fine",
        ])
        hits = ss.scan_text(text, "d.md", env_keys=keys)
        assert [(h.line, h.key) for h in hits] == [(1, "CORA_BACKUP_PASSPHRASE"), (2, "CORA_BACKUP_PASSPHRASE")]
        env = tmp_path / ".env"
        env.write_text("CORA_BACKUP_PASSPHRASE=short8ch\nDEPOSCO_PROD_PASS=Wh4le9xQ\nSLACK_BOT_TOKEN=abc\n", encoding="utf-8")
        vals = ss.live_secret_values(env)
        assert set(vals) == {"CORA_BACKUP_PASSPHRASE", "DEPOSCO_PROD_PASS"}    # 8-char passwords are in the belt; a 3-char token is noise

    def test_inline_assignments_in_bullets_code_and_tables_are_hits_for_secret_keys(self):
        keys = {"DEPOSCO_PROD_PASS", "HUBSPOT_PORTAL_ID", "OPENAI_API_KEY"}
        text = "\n".join([
            "- DEPOSCO_PROD_PASS=Wh4le9xQz1 is the production password",
            "set `OPENAI_API_KEY=abcdefghijklmnopqrstuvwxyz` in .env",
            "| DEPOSCO_PROD_PASS | Wh4le9xQz1 |",          # no '=': the belt catches a live value, rule 2 does not
            "- HUBSPOT_PORTAL_ID=246351746 (identifier, fine inline)",
            "INSTAGRAM_F3E_ACCESS_TOKEN=<long-lived token from Task 3>",   # placeholder split at its space
        ])
        hits = ss.scan_text(text, "r.md", env_keys=keys | {"INSTAGRAM_F3E_ACCESS_TOKEN"})
        assert [(h.line, h.key) for h in hits] == [(1, "DEPOSCO_PROD_PASS"), (2, "OPENAI_API_KEY")]


class TestTestPathsAreBeltOnly:
    """The hook's second gate scans STAGED files, which include test files whose fixtures are
    secret-SHAPED by construction (this very file). Under tests/ only the live-.env belt applies;
    everywhere else all three rules apply; a LIVE value under tests/ still blocks."""

    def test_is_test_path_both_separators(self):
        assert ss.is_test_path("tests/test_x.py") and ss.is_test_path("tests\\sub\\test_y.py")
        assert not ss.is_test_path("deployment/tests.md") and not ss.is_test_path("scripts/test_helper.py")

    def test_shaped_fixture_under_tests_is_not_a_hit_but_is_under_deployment(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ss, "_REPO_ROOT", tmp_path)
        fixture = "aws: AKIAIOSFODNN7EXAMPLQ\nOPENAI_API_KEY=abcdefghijklmnopqrstuvwxyz0123456789\n"
        (tmp_path / "tests").mkdir(); (tmp_path / "deployment").mkdir()
        (tmp_path / "tests" / "test_fixture.py").write_text(fixture, encoding="utf-8")
        (tmp_path / "deployment" / "runbook.md").write_text(fixture, encoding="utf-8")
        hits = ss.scan_paths([tmp_path / "tests", tmp_path / "deployment"], env_belt=False, env_keys={"OPENAI_API_KEY"})
        assert {h.path.replace("\\", "/") for h in hits} == {"deployment/runbook.md"}
        assert sorted(h.kind for h in hits) == ["env-assignment", "shape"]

    def test_live_value_under_tests_still_blocks_and_names_the_key_only(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ss, "_REPO_ROOT", tmp_path)
        env = tmp_path / ".env"
        env.write_text("SLACK_BOT_TOKEN=live-token-value-Zq8x1kLm3pQ\n", encoding="utf-8")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_leak.py").write_text("TOKEN = 'live-token-value-Zq8x1kLm3pQ'\n", encoding="utf-8")
        hits = ss.scan_paths([tmp_path / "tests"], env_belt=True, env_file=env, env_keys=set())
        assert [(h.kind, h.key) for h in hits] == [("live-env-value", "SLACK_BOT_TOKEN")]
        assert "Zq8x1kLm3pQ" not in hits[0].render()


class TestCIGuard:
    def test_deploy_docs_and_manifests_carry_no_secret_values(self):
        roots = [REPO / "deployment", REPO / "README.md", REPO / "CLAUDE.md",
                 REPO / "scripts" / "generate_task_estate_manifest.py",
                 REPO / "scripts" / "dr_manifest_probes.py", REPO / "scripts" / "secrets_scan.py"]
        docs = REPO / "docs"
        if docs.exists():
            roots.append(docs)
        roots += [p for p in (REPO / "scripts" / "stage_vm_step2_card.py", REPO / "scripts" / "register_estate_placeholder") if p.exists()]
        hits = ss.scan_paths([r for r in roots if r.exists()], env_belt=True)
        assert hits == [], "\n".join(h.render() for h in hits)

    def test_live_belt_is_armed_wherever_a_live_env_resolves(self):
        """A worktree has no .env; LIVE_ENV falls back to the live checkout's. Wherever that file
        exists the belt must carry keys -- a vacuous belt (0 keys) is a silent no-op, not a pass."""
        if not ss.LIVE_ENV.exists():
            pytest.skip("no live .env on this host (CI): the belt is documented as inert here")
        assert len(ss.live_secret_values(ss.LIVE_ENV)) > 0
