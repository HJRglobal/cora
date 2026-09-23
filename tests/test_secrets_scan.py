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

    def test_env_example_keys_include_commented_entries(self, tmp_path):
        ex = tmp_path / ".env.example"
        ex.write_text("# SLACK_USER_TOKEN=<value>\nOPENAI_API_KEY=<value>\n# comment\n", encoding="utf-8")
        assert ss.env_example_keys(ex) == {"SLACK_USER_TOKEN", "OPENAI_API_KEY"}


class TestCIGuard:
    def test_deploy_docs_and_manifests_carry_no_secret_values(self):
        roots = [REPO / "deployment", REPO / "README.md", REPO / "CLAUDE.md",
                 REPO / "scripts" / "generate_task_estate_manifest.py",
                 REPO / "scripts" / "dr_manifest_probes.py", REPO / "scripts" / "secrets_scan.py"]
        docs = REPO / "docs"
        if docs.exists():
            roots.append(docs)
        hits = ss.scan_paths([r for r in roots if r.exists()], env_belt=True)
        assert hits == [], "\n".join(h.render() for h in hits)
