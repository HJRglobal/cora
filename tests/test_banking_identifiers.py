"""Ingest-integrity bundle I1 (cq-c89cfab00b1f): banking identifiers are redacted
at KB-chunk EGRESS -- the shared chunk renderer (Slack path + MCP text) AND the
MCP structured results -- and the deep link survives.

The 9/4 fixture is SHAPED like the chunk the probe returned (bank name, an
ABA/Routing label, a SWIFT label, a beneficiary account label); the numbers are
synthetic. Assertions are on the redaction marker and on the ABSENCE of the
synthetic values -- the real identifiers are never pasted into this file.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import banking_identifiers as bi  # noqa: E402
from cora import context_loader as cl  # noqa: E402
from cora import mcp_server  # noqa: E402
from cora.knowledge_base.store import SearchResult  # noqa: E402

FAKE_ROUTING = "123456789"
FAKE_SWIFT = "TRADUS44XXX"
FAKE_ACCOUNT = "9876543210123"
FAKE_IBAN = "GB29NWBK60161331926819"

WIRE_CHUNK = (
    "Tradition Capital Bank\n"
    "Wire Instructions for HJR Global\n"
    f"ABA/Routing Number: {FAKE_ROUTING}\n"
    f"SWIFT Code: {FAKE_SWIFT}\n"
    "Beneficiary: HJR Global LLC\n"
    f"Beneficiary Account Number: {FAKE_ACCOUNT}\n"
    "Reference: capital contribution\n"
)
DEEP_LINK = "https://drive.google.com/file/d/1WHCdRt1fUPnCUTi_AkSLvuC/view"


def _res(content: str, **kw) -> SearchResult:
    base = dict(chunk_id="c1", source="drive_sweep", source_id="1WHCdRt1fUPnCUTi_AkSLvuC",
                entity="FNDR", title="HJR Global Wire Instructions.pdf", content=content,
                deep_link=DEEP_LINK, date_modified=None, distance=0.3)
    base.update(kw)
    return SearchResult(**base)


# ── the redactor itself ──────────────────────────────────────────────────────
class TestRedactor:
    def test_wire_chunk_values_redacted_labels_kept(self):
        out, n = bi.redact_banking_identifiers(WIRE_CHUNK)
        assert n == 3
        assert out.count(bi.MARKER) == 3
        for secret in (FAKE_ROUTING, FAKE_SWIFT, FAKE_ACCOUNT):
            assert secret not in out
        # the labels and the non-identifier lines survive so the reader knows
        # WHICH document to open
        assert "ABA/Routing Number: " + bi.MARKER in out
        assert "SWIFT Code: " + bi.MARKER in out
        assert "Beneficiary Account Number: " + bi.MARKER in out
        assert "Tradition Capital Bank" in out
        assert "Beneficiary: HJR Global LLC" in out
        assert "Reference: capital contribution" in out

    @pytest.mark.parametrize("label", [
        "Routing #: {r}", "ABA {r}", "RTN: {r}", "Routing Number (ACH): {r}",
        "routing number - {r}", "ABA Routing No. {r}",
    ])
    def test_routing_label_shapes(self, label):
        text = "Bank wire details. " + label.format(r=FAKE_ROUTING)
        out, n = bi.redact_banking_identifiers(text)
        assert n == 1 and FAKE_ROUTING not in out and bi.MARKER in out

    def test_routing_requires_exactly_nine_digits(self):
        for bad in ("12345678", "1234567890"):
            out, n = bi.redact_banking_identifiers(f"Routing: {bad}")
            assert n == 0 and out == f"Routing: {bad}"

    @pytest.mark.parametrize("label", ["SWIFT: {s}", "SWIFT/BIC {s}", "BIC code: {s}", "swift code - {s}"])
    def test_swift_label_shapes(self, label):
        for code in ("TRADUS44", FAKE_SWIFT):
            text = label.format(s=code)
            out, n = bi.redact_banking_identifiers(text)
            assert n == 1 and code not in out, text

    def test_swift_code_must_be_uppercase_shape(self):
        # the adjective "swift" followed by ordinary prose is never a code
        text = "a swift reply: thanks all, will do"
        assert bi.redact_banking_identifiers(text) == (text, 0)

    def test_iban(self):
        text = f"Please wire to IBAN: {FAKE_IBAN} and confirm."
        out, n = bi.redact_banking_identifiers(text)
        assert n == 1 and FAKE_IBAN not in out
        assert out.endswith(f"IBAN: {bi.MARKER} and confirm.")

    @pytest.mark.parametrize("label", [
        "Account Number: {a}", "Account #: {a}", "Acct No. {a}", "account: {a}",
        "Beneficiary Account Number: {a}", "A/C {a}", "Account ID: {a}",
    ])
    def test_account_label_shapes_when_banking_cue_present(self, label):
        text = "Bank wire instructions.\n" + label.format(a=FAKE_ACCOUNT)
        out, n = bi.redact_banking_identifiers(text)
        assert n == 1 and FAKE_ACCOUNT not in out, text

    def test_account_with_dashes_and_spaces(self):
        text = "Huntington bank statement. Account # 003-0870120-502 for the month."
        out, n = bi.redact_banking_identifiers(text)
        assert n == 1 and "003-0870120-502" not in out
        assert "Account # " + bi.MARKER + " for the month." in out

    # ── negative controls: byte-identical ───────────────────────────────────
    @pytest.mark.parametrize("text", [
        "HubSpot portal 246351746 -- F3E Retail pipeline 2313722582.",
        "HubSpot account 246351746 is the live portal.",          # account label, NO banking cue
        "Asana project [F3E] Sales Pipeline 1214824237490027; team 1209079638382203.",
        "Invoice #INV-2026-04-7413 for $12,340.00 is overdue (order 123456789).",
        "Call Tommy at 480-555-1234 or 4805551234.",
        "Chase checking account ending in 4321 was reconciled.",  # last-4 below the floor
        "The checking account balance 1234567 grew 12% QoQ.",     # 'balance' is not a label
        "Q3 revenue $1,234,567 landed in the operating account.",
        "Wire the samples to the warehouse by Friday; routing the pallets via Nimbl.",
        "The Arc chapter and the ABA therapy provider list (2026 update).",
        "Bank of America branch hours: 9-5. Savings rate 4.25%.",
        "",
    ])
    def test_controls_unchanged(self, text):
        out, n = bi.redact_banking_identifiers(text)
        assert n == 0
        assert out == text

    def test_zero_redactions_means_identity(self):
        # the contract the renderer relies on: n == 0 <=> untouched bytes
        text = "Ordinary prose about the F3E launch and the Gotham agreement."
        out, n = bi.redact_banking_identifiers(text)
        assert (out, n) == (text, 0)

    def test_fail_closed_on_internal_error(self, monkeypatch):
        def boom(*_a, **_k):
            raise RuntimeError("regex engine exploded")
        monkeypatch.setattr(bi, "_sub_group1", boom)
        out, n = bi.redact_banking_identifiers(WIRE_CHUNK)
        assert out == bi.WITHHELD and n == 1

    def test_has_banking_identifier_probe(self):
        assert bi.has_banking_identifier(WIRE_CHUNK) is True
        assert bi.has_banking_identifier("plain prose") is False

    def test_no_redos_on_adversarial_chunk(self):
        # 200 KB of cue words with near-miss digit runs, plus a long run of
        # dashes/spaces after an 'account' label (the bounded-class edge).
        near_miss = ("routing routing routing 12345678 aba 1234 swift ABCDEF1 account "
                     "account " + "- " * 40 + "\n") * 2000
        t0 = time.perf_counter()
        bi.redact_banking_identifiers(near_miss)
        assert time.perf_counter() - t0 < 2.0


# ── renderer wiring: the SHARED chunk renderer (Slack path + MCP text) ────────
class TestRendererWiring:
    def test_format_kb_chunks_redacts_and_keeps_deep_link(self):
        block = cl._format_kb_chunks([_res(WIRE_CHUNK)])
        for secret in (FAKE_ROUTING, FAKE_SWIFT, FAKE_ACCOUNT):
            assert secret not in block
        assert block.count(bi.MARKER) == 3
        assert DEEP_LINK in block            # the deep link survives
        assert "HJR Global Wire Instructions.pdf" in block

    def test_format_kb_chunks_plain_chunk_unchanged(self):
        plain = "Gotham DSD broker agreement: 12% margin, net-30, exclusive AZ territory."
        block = cl._format_kb_chunks([_res(plain, source="static_md", title="agreement.md")])
        assert plain in block and bi.MARKER not in block

    def test_format_kb_chunks_logs_a_warning(self, caplog):
        with caplog.at_level("WARNING", logger="cora.context_loader"):
            cl._format_kb_chunks([_res(WIRE_CHUNK)])
        msgs = [r.getMessage() for r in caplog.records if "banking-identifier" in r.getMessage()]
        assert msgs, "a redaction must leave a WARN trace"
        # the WARN names the chunk, never the identifiers
        assert FAKE_ROUTING not in " ".join(msgs)
        assert FAKE_ACCOUNT not in " ".join(msgs)


# ── the MCP plugin path (cq-c89cfab00b1f's reported surface) ────────────────
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
    def test_kb_search_structured_content_and_text_are_redacted(self, monkeypatch):
        # founder-owned drive_sweep chunk -> passes Tier-1 unstripped (the 9/4 shape)
        r = _res(WIRE_CHUNK, metadata={"user_email": "harrison@hjrglobal.com"})
        _wire(monkeypatch, [r])
        out = mcp_server.kb_search("wire instructions HJR Global routing", entity="FNDR", limit=5)
        assert out["count"] == 1
        row = out["results"][0]
        for secret in (FAKE_ROUTING, FAKE_SWIFT, FAKE_ACCOUNT):
            assert secret not in row["content"]
            assert secret not in out["text"]
        assert row["content"].count(bi.MARKER) == 3
        assert row["banking_redactions"] == 3
        assert out["banking_redactions"] == 3
        assert row["deep_link"] == DEEP_LINK       # the deep link survives
        assert DEEP_LINK in out["text"]

    def test_kb_search_plain_chunk_passes_untouched(self, monkeypatch):
        plain = "F3E launch checklist: Sprouts appeal filed, Mood STA built to pre-charge."
        r = _res(plain, source="static_md", title="launch.md", entity="F3E",
                 metadata={"user_email": "harrison@hjrglobal.com"})
        _wire(monkeypatch, [r])
        out = mcp_server.kb_search("launch", entity="F3E", limit=5)
        assert out["results"][0]["content"] == plain
        assert out["results"][0]["banking_redactions"] == 0
        assert out["banking_redactions"] == 0
        assert bi.MARKER not in out["text"]

    def test_result_dict_redacts_directly(self):
        row = mcp_server._result_dict(_res(WIRE_CHUNK))
        assert FAKE_ACCOUNT not in row["content"] and row["banking_redactions"] == 3
