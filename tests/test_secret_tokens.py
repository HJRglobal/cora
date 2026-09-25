"""Code #15 S1 (cq-d9d0c92cc797): API-token shapes are redacted at KB-chunk EGRESS --
every seam the banking belt covers (the shared renderer, the MCP structured rows, the
Tier-2 renderers, the briefing snippets, the nightly Drive digest, the phantom-rail
snippet) -- tokens FIRST, then banking.

EVERY token fixture below is ASSEMBLED at runtime ("xo" + "xb-", "s" + "k-ant-",
"2/" + gid ...): the repo's pre-commit hook greps staged files for live key prefixes,
and a literal would (rightly) block the commit. Assertions are on the MARKER and the
ABSENCE of the assembled values; nothing here is a real credential.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from cora import banking_identifiers as bi  # noqa: E402
from cora import context_loader as cl  # noqa: E402
from cora import drive_materializer  # noqa: E402
from cora import finance_receipts  # noqa: E402
from cora import historical_access  # noqa: E402
from cora import mcp_server  # noqa: E402
from cora import secret_tokens as st  # noqa: E402
from cora import slack_egress  # noqa: E402
from cora.knowledge_base.store import SearchResult  # noqa: E402
from cora.reply_formatter import redact_links_and_ids  # noqa: E402

import secrets_scan as ss  # noqa: E402

# ── assembled fixtures ───────────────────────────────────────────────────────
D16A = "1204" + "567890123456"
D16B = "1205" + "678901234567"
HEX32 = "0123456789abcdef" * 2
SLACK_TAIL = "AbCdEfGhIjKlMnOpQrStUvWx"

ASANA_V2 = "2/" + D16A + "/" + D16B + ":" + HEX32          # the 9/11 shape (1,16,16,32)
ASANA_V1 = "1/" + D16A + ":" + HEX32
SLACK_BOT = "xo" + "xb-" + "1234567890123-1234567890123-" + SLACK_TAIL
SLACK_USER = "xo" + "xp-" + "1234567890-1234567890-1234567890123-" + HEX32
SLACK_LEGACY_A = "xo" + "xa-2-" + "1234567890-1234567890123-" + HEX32
SLACK_9DIGIT = "xo" + "xb-" + "123456789-1234567890123-" + SLACK_TAIL   # legacy 9-digit team segment
OPENAI_LEGACY = "s" + "k-" + ("Ab1" * 16)
OPENAI_PROJ = "s" + "k-proj-" + ("Ab1_-" * 30)
OPENAI_SVC = "s" + "k-svcacct-" + ("Ab1_-" * 30)
OPENAI_ADMIN = "s" + "k-admin-" + ("Ab1_-" * 30)
OPENAI_NONE = "s" + "k-None-" + ("Ab1_-" * 30)
ANTHROPIC = "s" + "k-ant-api03-" + ("Ab1_" * 23) + "AA"
ANTHROPIC_ADMIN = "s" + "k-ant-admin01-" + ("Ab1_" * 23) + "AA"
GOOGLE = "AI" + "za" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q"

POSITIVES = {
    "asana_v2": (ASANA_V2, "asana-pat"),
    "asana_v1": (ASANA_V1, "asana-pat"),
    "slack_bot": (SLACK_BOT, "slack-token"),
    "slack_user": (SLACK_USER, "slack-token"),
    "slack_legacy_a": (SLACK_LEGACY_A, "slack-token"),
    "slack_9digit": (SLACK_9DIGIT, "slack-token"),
    "openai_legacy": (OPENAI_LEGACY, "sk-key"),
    "openai_proj": (OPENAI_PROJ, "sk-key"),
    "openai_svcacct": (OPENAI_SVC, "sk-key"),
    "openai_admin": (OPENAI_ADMIN, "sk-key"),
    "openai_none": (OPENAI_NONE, "sk-key"),
    "anthropic": (ANTHROPIC, "sk-key"),
    "anthropic_admin": (ANTHROPIC_ADMIN, "sk-key"),
    "google": (GOOGLE, "google-api-key"),
}

# the FALSE-POSITIVE set from the S1 recon (every one was measured clean on the live KB)
FP_SET = [
    "commit 11f6ad8ec52a2984abaafd7c3b516503785c2072 on main",
    "ab00c69 fix(d051-r4): auditor",
    "chunk 9c2e7a41-5b3d-4f60-8e21-0d4c6b7a9f13 and 2b1f0c1e-0e4d-4a8a-9b1c-7d5f6e3a2b10",
    "sha256 2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881",
    # Asana task URLs: the old /0/<gid ending 2>/<gid>/f is the exact shape the hook's
    # prefix screen false-positives on (69 live gmail chunks); assembled so this file
    # does not trip that screen itself.
    "https://app.asana.com/0/1204525841563032" + "/" + "1218516997344812/f",
    "https://app.asana.com/1/682743441507584/project/1215470928454227/task/1218344765000648",
    "2/" + D16A + "/" + D16B + ":abc",                     # a colon, but no 32-char secret
    "task-" + "abcdefghij0123456789ABCD",
    "desk-" + "0123456789abcdefghijklm",
    "sk-" + "learn-pipeline-integration-tests-for-models",
    "sk-" + "learn-pipeline-v2-integration-tests",
    "sk-" + "learn-0-24-2-upgrade-notes-and-caveats",
    "sk-12345",
    "sk-" + "abcdefghijklmnopqrstuvwxyzabcdefgh",           # 34 letters, no digit
    "high-risk-A1b2C3d4E5f6G7h8I9j0K1",
    "xoxo-hugs-and-kisses-forever-and-ever",
    "xo" + "xp-2-factor-auth-guide-for-teams",
    "Slack ids C0B6GT3117Y U0B2RM2JYJ1 D0B4CTD3B09",
    "AIzaSyShort",
    "AI" + "za" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8S9",   # 44 chars: not exactly 35
    "0123456789abcdef0123456789abcdef01234567",
    "dGhpcyBpcyBhIGJhc2U2NCBibG9iIHdpdGggc2stIGluc2lkZQ==",
    "Drive 1TSUGC4hAHjgbHuExFqf_4-lXyXm7opq5",
    "ABA/Routing Number: 123456789 Account Number: 9876543210123",
    "xo" + "xb-your-token-here",
    "xo" + "xb-1234-your-token-here",
    "HubSpot portal 246351746, Asana workspace 682743441507584.",
    st.MARKER,
    "",
]


# ── the redactor itself ──────────────────────────────────────────────────────
class TestRedactor:
    @pytest.mark.parametrize("name", sorted(POSITIVES))
    def test_each_shape_is_redacted(self, name):
        tok, shape = POSITIVES[name]
        out, n = st.redact_secret_tokens(f"before {tok} after")
        assert (out, n) == (f"before {st.MARKER} after", 1)
        assert st.count_by_shape(f"x {tok} y") == {shape: 1}
        assert st.has_secret_token(tok) is True

    @pytest.mark.parametrize("pre,post", [
        ('"', '"'), ("'", "'"), ("`", "`"), ("TOKEN=", ""), ("token: ", "\n"),
        ("\n", "\n"), ("(", ")"), ("<", ">"), ("[", "]"), ("key=", "&x=1"), ("", ","),
        ("=", ";"), (":", "."),
    ])
    @pytest.mark.parametrize("name", sorted(POSITIVES))
    def test_adjacent_text_is_byte_identical(self, name, pre, post):
        tok, _shape = POSITIVES[name]
        text = f"a {pre}{tok}{post} b"
        out, n = st.redact_secret_tokens(text)
        assert (out, n) == (f"a {pre}{st.MARKER}{post} b", 1)

    def test_asana_left_edge_admits_a_letter_or_a_json_escape(self):
        # the Asana leg's left edge is "not a digit" ONLY: a JSON-escaped newline, a
        # word glued in front ("PAT2/...") still redact
        for pre in ("\\n", "PAT", "pat=", "x"):
            out, n = st.redact_secret_tokens(pre + ASANA_V2)
            assert (out, n) == (pre + st.MARKER, 1), pre
        # a digit glued in front is a different number, not this token
        assert st.redact_secret_tokens("9" + ASANA_V2)[1] == 0

    def test_the_9_11_read_host_shape(self):
        # the paste was a PowerShell Read-Host prompt echoed into a Cowork session
        text = ("PS C:\\> $pat = Read-Host 'Paste your Asana PAT'\n"
                "Paste your Asana PAT: " + ASANA_V2 + "\n"
                "PS C:\\> $env:ASANA_PAT = '" + ASANA_V2 + "'\n")
        out, n = st.redact_secret_tokens(text)
        assert n == 2
        assert ASANA_V2 not in out and D16B not in out and HEX32 not in out
        assert out == ("PS C:\\> $pat = Read-Host 'Paste your Asana PAT'\n"
                       "Paste your Asana PAT: " + st.MARKER + "\n"
                       "PS C:\\> $env:ASANA_PAT = '" + st.MARKER + "'\n")

    def test_several_tokens_idempotent_and_counted(self):
        text = f"a {SLACK_BOT} b {ANTHROPIC} c {GOOGLE} d {ASANA_V2} e {OPENAI_PROJ} f"
        out, n = st.redact_secret_tokens(text)
        assert n == 5 and out.count(st.MARKER) == 5
        assert st.count_by_shape(text) == {"asana-pat": 1, "slack-token": 1, "sk-key": 2,
                                           "google-api-key": 1}
        assert sum(st.count_by_shape(text).values()) == n
        # idempotent: the marker carries no shape
        assert st.redact_secret_tokens(out) == (out, 0)
        assert st.count_by_shape(out) == {}

    @pytest.mark.parametrize("text", FP_SET)
    def test_false_positive_set_is_byte_identical(self, text):
        assert st.redact_secret_tokens(text) == (text, 0)
        assert st.count_by_shape(text) == {}

    def test_none_and_empty(self):
        assert st.redact_secret_tokens(None) == ("", 0)
        assert st.redact_secret_tokens("") == ("", 0)
        assert st.redact_title(None) == ""
        assert st.redact_chunk_egress(None) == ("", 0, 0)
        assert st.count_by_shape(None) == {}

    def test_fail_closed_on_internal_error(self, monkeypatch):
        def boom(_text):
            raise RuntimeError("regex engine exploded")
        monkeypatch.setattr(st, "_redact_counts", boom)
        assert st.redact_secret_tokens("x " + SLACK_BOT) == (st.WITHHELD, 1)
        assert st.redact_chunk_egress("x " + SLACK_BOT) == (st.WITHHELD, 0, 1)
        assert st.count_by_shape("anything") == {"unscannable": 1}
        with pytest.raises(RuntimeError):
            st.redact_secret_tokens_strict("x " + SLACK_BOT)

    def test_strict_matches_the_safe_path_when_it_works(self):
        text = f"k={ANTHROPIC} t={SLACK_USER}"
        assert st.redact_secret_tokens_strict(text) == st.redact_secret_tokens(text)
        clean = "plain prose"
        assert st.redact_secret_tokens_strict(clean)[0] is clean

    def test_no_redos_on_adversarial_inputs(self):
        adversarial = [
            "s" + "k-" + "a" * 200_000,
            "s" + "k-proj-" + "a" * 200_000,
            "xo" + "xb-" + "1-" * 100_000,
            ("xo" + "xb-1-" + "a" * 15 + "-") * 9_000,
            "2/" + "1" * 200_000,
            ("2/" + "1" * 20 + "/" + "1" * 20 + ":" + "a" * 31 + " ") * 3_000,
            "AI" + "za" + "-" * 200_000,
            ("sk-" + "a-" * 50) * 2_000,
            (" sk-" + "a" * 31 + "1") * 6_000,
            ("xo" + "xb-1-" + "a" * 250 + " ") * 800,
        ]
        for s in adversarial:
            t0 = time.perf_counter()
            st.redact_secret_tokens(s)
            st.count_by_shape(s)
            assert time.perf_counter() - t0 < 2.0, len(s)


class TestComposedEgress:
    def test_tokens_first_then_banking(self):
        # a `routing` cue within 40 chars of a legacy 9-digit Slack segment: the banking
        # routing leg would put ITS marker inside the token, the token regex would then
        # no longer match, and the secret tail would survive
        text = "Bank routing " + SLACK_9DIGIT + " end"
        banking_first = st.redact_secret_tokens(bi.redact_banking_identifiers(text)[0])[0]
        assert SLACK_TAIL in banking_first          # why the order matters
        out, n_bank, n_tok = st.redact_chunk_egress(text)
        assert (out, n_bank, n_tok) == ("Bank routing " + st.MARKER + " end", 0, 1)

    def test_clean_text_is_identity_and_banking_is_unchanged(self):
        plain = "Gotham DSD broker agreement: 12% margin, net-30."
        assert st.redact_chunk_egress(plain) == (plain, 0, 0)
        assert st.redact_chunk_egress(plain)[0] is plain
        wire = ("Wire Instructions\nABA/Routing Number: 123456789\n"
                "Beneficiary Account Number: 9876543210123\n")
        assert st.redact_chunk_egress(wire) == (*bi.redact_banking_identifiers(wire), 0)

    def test_both_legs_count_separately(self):
        text = "Bank wire. Routing: 123456789\nDeploy key: " + ANTHROPIC
        out, n_bank, n_tok = st.redact_chunk_egress(text)
        assert (n_bank, n_tok) == (1, 1)
        assert "123456789" not in out and ANTHROPIC not in out
        assert bi.MARKER in out and st.MARKER in out


# ── renderer wiring (the seven banking-belt seams) ───────────────────────────
DEEP_LINK = "https://drive.google.com/file/d/1TokFixtureDocId/view"
HOT_TITLE = "Deploy notes " + SLACK_BOT
WIRE_CHUNK = ("Tradition Capital Bank\nWire Instructions for HJR Global\n"
              "ABA/Routing Number: 123456789\nSWIFT Code (For International Wires): TRADUS44XXX\n"
              "Beneficiary Account Number: 9876543210123\n")


def _res(content: str, **kw) -> SearchResult:
    base = dict(chunk_id="chunk-tok-1", source="drive_sweep", source_id="1TokFixtureDocId",
                entity="FNDR", title="Deploy notes.md", content=content,
                deep_link=DEEP_LINK, date_modified=None, distance=0.3)
    base.update(kw)
    return SearchResult(**base)


def _secret_free(text: str) -> bool:
    return not any(tok in text for tok, _ in POSITIVES.values()) and SLACK_TAIL not in text \
        and HEX32 not in text


class TestSharedRenderer:
    def test_body_redacted_deep_link_kept(self):
        block = cl._format_kb_chunks([_res("the bot token is " + SLACK_BOT + " and key " + GOOGLE)])
        assert _secret_free(block)
        assert block.count(st.MARKER) == 2
        assert DEEP_LINK in block

    def test_title_header_link_label_and_warn_are_token_free(self, caplog):
        with caplog.at_level("WARNING", logger="cora.context_loader"):
            block = cl._format_kb_chunks([_res("plain body", title=HOT_TITLE)])
        assert _secret_free(block)
        assert f"| Deploy notes {st.MARKER} |" in block
        assert f"<{DEEP_LINK}|Deploy notes {st.MARKER}>" in block
        warns = [r.getMessage() for r in caplog.records if "api-token redaction" in r.getMessage()]
        assert warns and all("chunk-tok-1" in w for w in warns)
        # keyed on the chunk id, NEVER the title (D-082: the live target is a LEX row)
        assert not any("Deploy notes" in w for w in warns)
        assert _secret_free(" ".join(r.getMessage() for r in caplog.records))

    def test_banking_warn_semantics_unchanged(self, caplog):
        with caplog.at_level("WARNING", logger="cora.context_loader"):
            block = cl._format_kb_chunks([_res(WIRE_CHUNK + "Deploy key: " + ANTHROPIC)])
        assert block.count(bi.MARKER) == 3 and block.count(st.MARKER) == 1
        bank = [r.getMessage() for r in caplog.records if "banking-identifier" in r.getMessage()]
        assert bank and "3 identifier(s)" in bank[0]

    def test_plain_chunk_unchanged(self):
        plain = "Gotham DSD broker agreement: 12% margin, net-30, exclusive AZ territory."
        block = cl._format_kb_chunks([_res(plain, source="static_md", title="agreement.md")])
        assert plain in block and st.MARKER not in block


class _FakeKB:
    def __init__(self, results):
        self._results = list(results)

    def search(self, query, **kwargs):
        return list(self._results)


def _wire(monkeypatch, results):
    monkeypatch.setattr(mcp_server, "_get_ro_kb", lambda: _FakeKB(results))
    monkeypatch.setattr(mcp_server, "_embed", lambda q: [0.0] * 1536)
    monkeypatch.setattr(mcp_server, "_founder_emails", lambda: frozenset({"harrison@hjrglobal.com"}))


class TestMcpPath:
    def test_kb_search_rows_text_and_counts(self, monkeypatch):
        r = _res(WIRE_CHUNK + "Deploy key: " + ANTHROPIC, title=HOT_TITLE,
                 metadata={"user_email": "harrison@hjrglobal.com"})
        _wire(monkeypatch, [r])
        out = mcp_server.kb_search("deploy key", entity="FNDR", limit=5)
        row = out["results"][0]
        assert _secret_free(row["content"]) and _secret_free(row["title"]) and _secret_free(out["text"])
        assert row["banking_redactions"] == 3          # banking semantics UNCHANGED
        assert row["token_redactions"] == 2            # additive: body + title
        assert out["banking_redactions"] == 3 and out["token_redactions"] == 2
        assert row["deep_link"] == DEEP_LINK

    def test_plain_row_has_zero_token_redactions(self, monkeypatch):
        plain = "F3E launch checklist: Sprouts appeal filed."
        r = _res(plain, source="static_md", title="launch.md", entity="F3E",
                 metadata={"user_email": "harrison@hjrglobal.com"})
        _wire(monkeypatch, [r])
        out = mcp_server.kb_search("launch", entity="F3E", limit=5)
        assert out["results"][0]["content"] == plain
        assert out["results"][0]["token_redactions"] == 0 and out["token_redactions"] == 0

    def test_result_dict_log_is_info_and_keyed_on_chunk_id(self, caplog):
        with caplog.at_level("INFO", logger="cora.mcp_server"):
            row = mcp_server._result_dict(_res("body " + OPENAI_PROJ, title=HOT_TITLE))
        assert row["token_redactions"] == 2 and row["banking_redactions"] == 0
        recs = [r for r in caplog.records if "MCP api-token redaction" in r.getMessage()]
        assert recs and all(r.levelname == "INFO" for r in recs)
        msg = " ".join(r.getMessage() for r in recs)
        assert "chunk-tok-1" in msg and "Deploy notes" not in msg and _secret_free(msg)


class TestOtherRenderers:
    def test_format_owned_chunks(self, caplog):
        r = _res("body " + SLACK_USER, source="gmail", title=HOT_TITLE,
                 metadata={"user_email": "harrison@hjrglobal.com"})
        with caplog.at_level("WARNING", logger="cora.historical_access"):
            text = historical_access.format_owned_chunks([r], "your")
        assert _secret_free(text) and st.MARKER in text and DEEP_LINK in text
        warns = [x.getMessage() for x in caplog.records if "api-token redaction" in x.getMessage()]
        assert warns and "chunk-tok-1" in warns[0] and "Deploy notes" not in warns[0]

    def test_format_finance_chunks(self, caplog):
        r = _res("Invoice 4471 -- portal key " + GOOGLE, source="gmail", title=HOT_TITLE,
                 metadata={"user_email": "payables@lexingtonservices.com"})
        with caplog.at_level("WARNING", logger="cora.finance_receipts"):
            text = finance_receipts.format_finance_chunks([r], "all monitored mailboxes",
                                                          {r.source_id: "https://drive.google.com/filed"})
        assert _secret_free(text) and st.MARKER in text
        assert "Filed: https://drive.google.com/filed" in text
        warns = [x.getMessage() for x in caplog.records if "api-token redaction" in x.getMessage()]
        assert warns and "chunk-tok-1" in warns[0] and "Deploy notes" not in warns[0]

    def test_briefing_redacts_before_the_400_slice(self):
        spec = importlib.util.spec_from_file_location(
            "run_daily_briefing", _REPO_ROOT / "scripts" / "run_daily_briefing.py")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        # the token STRADDLES the raw 400-char cut: slicing first would leave a partial
        # prefix no regex can match any more
        body = "x" * 380 + " " + SLACK_BOT + " tail"
        lines = mod._chunk_context_lines([
            {"source": "gmail", "entity": "FNDR", "title": HOT_TITLE, "content": body},
        ])
        assert _secret_free(lines[0])
        assert ("xo" + "xb-") not in lines[0] and "1234567890123" not in lines[0]
        assert lines[0].startswith(f"[GMAIL/FNDR] Deploy notes {st.MARKER}:")

    def test_materializer_source_block(self):
        block = drive_materializer._build_source_block([
            {"title": HOT_TITLE, "content": "the key is " + ANTHROPIC_ADMIN},
            {"title": "clean", "content": "Sprouts appeal filed."},
        ])
        assert _secret_free(block) and st.MARKER in block and "Sprouts appeal filed." in block

    def test_phantom_snippet_scrub_runs_tokens_before_the_id_pass(self):
        text = "Done -- paste " + ASANA_V2 + " into the task."
        # the id pass alone strips the gids and strands the 32-char secret (why the
        # token leg must run FIRST)
        assert HEX32 in st.redact_secret_tokens(redact_links_and_ids(text))[0]
        out = slack_egress._scrub_for_snippet(text)
        assert _secret_free(out) and st.MARKER in out
        out2 = slack_egress._scrub_for_snippet("bot token " + SLACK_BOT)
        assert _secret_free(out2) and st.MARKER in out2


# ── drift: every redactor positive is a secrets_scan hit ─────────────────────
class TestSecretsScanDrift:
    @pytest.mark.parametrize("name", sorted(POSITIVES))
    def test_every_positive_is_a_scan_hit(self, name):
        tok, _shape = POSITIVES[name]
        hits = ss.scan_text(f"value: {tok}\n", "drift.md", env_keys=set(), live_values={})
        assert any(h.kind == "shape" for h in hits), name

    def test_escaped_newline_asana_is_a_scan_hit(self):
        hits = ss.scan_text("{\"t\": \"x\\n" + ASANA_V2 + "\"}", "drift.json", env_keys=set(), live_values={})
        assert [h.key for h in hits] == ["asana-pat"]
