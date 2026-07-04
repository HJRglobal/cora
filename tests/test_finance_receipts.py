"""Tests for the Tier 2-Finance receipt/invoice retrieval path.

Matrix from the 2026-06-09 spec: allowlist-only, channel-lock (only
#hjr-finance), content-type filter + non-financial refusal, any-mailbox for
allowlisted, PHI excluded, audit logging, proactive dedup/watermark,
auto-file to the configured folder — plus classifier precision cases.
"""

import json

import pytest

from cora import finance_receipts as fr
from cora import historical_access as ha
from cora.finance_doc_classifier import is_financial_document
from cora.knowledge_base.store import SearchResult

_FIN_CHANNEL = "CFINTEST"

_ALLOWLIST_YAML = f"""
users:
  - UJUSTIN
  - UERIC
  - UJERRY
channel_id: {_FIN_CHANNEL}
channel_name: hjr-finance
drive_folder_id: "FOLDER123"
"""

_ACCOUNTS_YAML = """
accounts:
  - email: hannah@hjrglobal.com
    name: Hannah Grant
    enabled: true
    dwd_eligible: true
    slack_user_id: UHANNAH
  - email: tommy@f3energy.com
    name: Tommy Anderson
    enabled: true
    dwd_eligible: true
    slack_user_id: UTOMMY
  - email: gone@f3energy.com
    name: Departed Person
    enabled: false
    dwd_eligible: true
"""


@pytest.fixture(autouse=True)
def config_fixture(tmp_path, monkeypatch):
    allowlist = tmp_path / "finance-allowlist.yaml"
    allowlist.write_text(_ALLOWLIST_YAML, encoding="utf-8")
    accounts = tmp_path / "accounts.yaml"
    accounts.write_text(_ACCOUNTS_YAML, encoding="utf-8")
    monkeypatch.setattr(fr, "_ALLOWLIST_PATH", allowlist)
    monkeypatch.setattr(fr, "_ACCOUNTS_PATH", accounts)
    monkeypatch.setattr(fr, "_AUDIT_LOG_PATH", tmp_path / "finance-audit.jsonl")
    monkeypatch.setattr(fr, "_WATERMARKS_PATH", tmp_path / "watermarks.json")
    monkeypatch.setattr(fr, "_FILED_IDS_PATH", tmp_path / "filed-ids.json")
    # historical_access identity for target-person resolution
    monkeypatch.setattr(ha, "_ACCOUNTS_PATH", accounts)
    monkeypatch.setattr(ha, "_ALIASES_PATH", tmp_path / "no-aliases.yaml")
    monkeypatch.setattr(ha, "_ALLOWLIST_PATH", tmp_path / "no-allowlist.yaml")
    fr.invalidate_cache()
    ha.invalidate_cache()
    yield
    fr.invalidate_cache()
    ha.invalidate_cache()


# ── Classifier (precision-biased) ─────────────────────────────────────────────

@pytest.mark.parametrize("title,content,author,attachments", [
    ("Invoice 998 from Wildpack", "Amount due $1,250.00 by June 30.", "", ()),
    ("Your order confirmation", "Order #5512 total $89.99. Thank you for your order.", "", ()),
    ("Statement of account", "", "billing@bluechipbev.com", ("statement_may.pdf",)),
    ("Re: WO#260478", "From: x\nAttachments: Invoice#20025550.pdf\n\nTotal due $4,400.00", "", ()),
    ("May payables", "remit to: ...", "QuickBooks <noreply@notification.intuit.com>", ()),
])
def test_classifier_positive(title, content, author, attachments):
    assert is_financial_document(title, content, author, attachments) is True


@pytest.mark.parametrize("title,content,author,attachments", [
    # Lone subject keyword — a casual mention is not a financial document.
    ("Re: that receipt you asked about", "did you ever find it?", "", ()),
    # Lone dollar amount in a personal email.
    ("Dinner Friday?", "It was like $40 for the two of us.", "", ()),
    # Plain business email, no signals.
    ("Q3 retailer pitch", "Following up on the pitch deck and timing.", "", ()),
    # Personal email with an unrelated PDF.
    ("Family photos", "See attached.", "", ("beach-trip.pdf",)),
])
def test_classifier_negative(title, content, author, attachments):
    assert is_financial_document(title, content, author, attachments) is False


def test_classifier_drive_filename_with_amounts():
    assert is_financial_document(
        "2026-06-08_lex_vendor-invoice-work-order-260478.pdf",
        "Work order 260478. Total $2,150.00 due on receipt.",
    ) is True


# ── Request gate: allowlist + channel lock + content-type ─────────────────────

def test_outside_finance_channel_is_pass_even_for_allowlisted():
    d = fr.check_request("UJUSTIN", "C_OTHER", "pull receipts from Hannah for May")
    assert d.action == "pass"  # normal Tier-2 rules apply instead


def test_allowlisted_financial_request_grants_any_mailbox():
    d = fr.check_request("UJUSTIN", _FIN_CHANNEL, "pull all invoices over $500 since April")
    assert d.action == "grant"
    assert d.mode == "finance"
    assert d.owner_emails is None  # ANY mailbox


def test_allowlisted_person_scoped_request():
    d = fr.check_request("UERIC", _FIN_CHANNEL, "pull receipts from Hannah for May")
    assert d.action == "grant"
    assert d.owner_emails == frozenset({"hannah@hjrglobal.com"})
    assert d.target_label == "Hannah Grant"


def test_non_allowlisted_user_refused_in_finance_channel():
    d = fr.check_request("UHANNAH", _FIN_CHANNEL, "pull receipts from Tommy for May")
    assert d.action == "respond"
    assert "finance team" in d.message


def test_allowlisted_non_financial_request_refused():
    d = fr.check_request("UJUSTIN", _FIN_CHANNEL, "pull up Hannah's emails about the lease")
    assert d.action == "respond"
    assert "financial documents" in d.message


def test_general_question_in_finance_channel_passes():
    d = fr.check_request("UJUSTIN", _FIN_CHANNEL, "what was OSN's April net income?")
    assert d.action == "pass"


def test_jerry_is_allowlisted_by_slack_id():
    d = fr.check_request("UJERRY", _FIN_CHANNEL, "find the invoices from Wildpack")
    assert d.action == "grant"


def test_missing_allowlist_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(fr, "_ALLOWLIST_PATH", tmp_path / "missing.yaml")
    fr.invalidate_cache()
    d = fr.check_request("UJUSTIN", _FIN_CHANNEL, "pull all invoices since April")
    assert d.action == "pass"  # no channel pin -> the finance path doesn't exist


def test_finance_retrieval_is_channel_scoped_both_directions():
    """2026-06-12 one-ship pin (task item a): the finance retrieval power is
    REFUSED for an allowlisted user OUTSIDE #hjr-finance (no grant anywhere
    else -- it falls back to normal Tier-2 'pass'), AND refused for a
    non-allowlisted user even INSIDE the channel."""
    # Allowlisted (Eric, Jerry) outside the pinned channel -> never a finance grant.
    for uid in ("UERIC", "UJERRY"):
        d = fr.check_request(uid, "C_SOME_OTHER_CHANNEL", "pull all invoices since April")
        assert d.action == "pass" and d.mode != "finance"
    # Non-allowlisted inside the channel -> explicit refusal.
    d = fr.check_request("UHANNAH", _FIN_CHANNEL, "pull all invoices since April")
    assert d.action == "respond" and "finance team" in d.message


# ── PHI exclusion ─────────────────────────────────────────────────────────────

def test_finance_drop_phi():
    clean = SearchResult(
        chunk_id="1", source="gmail", source_id="gmail:a@b.com:1", entity="LEX",
        title="Invoice 12 - office supplies", content="Total $99.00",
        deep_link="", date_modified=None, distance=0.4,
        metadata={"user_email": "a@b.com", "financial_document": True},
    )
    phi = SearchResult(
        chunk_id="2", source="gmail", source_id="gmail:a@b.com:2", entity="LEX",
        title="Invoice for client treatment plan",
        content="AHCCCS member services invoice for DDD client Jalen, $500.00",
        deep_link="", date_modified=None, distance=0.4,
        metadata={"user_email": "a@b.com", "financial_document": True},
    )
    assert fr.drop_phi([clean, phi]) == [clean]


# ── Audit logging ─────────────────────────────────────────────────────────────

def test_audit_logs_every_cross_mailbox_pull(tmp_path, monkeypatch):
    path = tmp_path / "audit2.jsonl"
    monkeypatch.setattr(fr, "_AUDIT_LOG_PATH", path)
    item = SearchResult(
        chunk_id="1", source="gmail", source_id="gmail:hannah@hjrglobal.com:m1",
        entity="FNDR", title="Invoice 1", content="$5.00", deep_link="",
        date_modified=None, distance=0.3,
        metadata={"user_email": "hannah@hjrglobal.com"},
    )
    fr.audit("UJUSTIN", "pull receipts from Hannah", None, [item], channel="hjr-finance")
    entry = json.loads(path.read_text().strip())
    assert entry["requester"] == "UJUSTIN"
    assert entry["scope"] == "ANY"
    assert entry["source_mailboxes"] == ["hannah@hjrglobal.com"]
    assert entry["items"] == ["gmail:hannah@hjrglobal.com:m1"]
    assert entry["mode"] == "on_demand"


# ── Auto-file ─────────────────────────────────────────────────────────────────

def _fin_result(owner: str, msg_id: str) -> SearchResult:
    return SearchResult(
        chunk_id=msg_id, source="gmail", source_id=f"gmail:{owner}:{msg_id}",
        entity="FNDR", title=f"Invoice {msg_id}", content="$10.00", deep_link="",
        date_modified=None, distance=0.3,
        metadata={"user_email": owner, "message_id": msg_id, "financial_document": True},
    )


def test_auto_file_results_caps_and_links(monkeypatch):
    calls = []

    def fake_file(mailbox, message_id, dry_run=False):
        calls.append((mailbox, message_id))
        return [{"filename": f"{message_id}.pdf", "link": f"https://drive/{message_id}"}]

    monkeypatch.setattr(fr, "file_message_attachments", fake_file)
    results = [_fin_result("hannah@hjrglobal.com", f"m{i}") for i in range(8)]
    links = fr.auto_file_results(results, cap=3)
    assert len(calls) == 3
    assert links["gmail:hannah@hjrglobal.com:m0"] == "https://drive/m0"


def test_auto_file_skips_already_filed(monkeypatch, tmp_path):
    ledger = tmp_path / "filed.json"
    ledger.write_text(json.dumps({"hannah@hjrglobal.com:m0": 123}), encoding="utf-8")
    monkeypatch.setattr(fr, "_FILED_IDS_PATH", ledger)
    monkeypatch.setattr(
        fr, "file_message_attachments",
        lambda *a, **k: [{"filename": "x.pdf", "link": "https://drive/x"}],
    )
    links = fr.auto_file_results([_fin_result("hannah@hjrglobal.com", "m0")])
    assert links == {}


def test_canonical_filename_shape():
    name = fr._canonical_filename(1780000000, "hannah@hjrglobal.com", "Invoice #998.pdf")
    assert name.endswith("Invoice -998.pdf")
    assert "_hannah_" in name
    assert name[:4].isdigit()


# ── Weekly digest: dedup + watermark + PHI + classifier gate ──────────────────

def _wire_gmail_mocks(monkeypatch, messages: dict[str, dict]):
    """messages: message_id -> parsed-metadata dict."""
    import cora.connectors.gmail_reader as gr

    monkeypatch.setattr(
        gr, "list_messages_with_attachments",
        lambda mailbox, since, max_results=200: list(messages.keys()),
    )
    monkeypatch.setattr(gr, "get_message", lambda mailbox, mid: {"id": mid})
    monkeypatch.setattr(gr, "parse_message_metadata", lambda msg: messages[msg["id"]])


def test_digest_files_once_and_advances_watermark(monkeypatch, tmp_path):
    messages = {
        "fin1": {
            "subject": "Invoice 998 from Wildpack",
            "from": "billing@wildpack.com",
            "snippet": "Amount due $1,250.00",
            "date_ts": 1780000000,
            "attachments": [{"filename": "invoice-998.pdf"}],
        },
        "personal1": {
            "subject": "Lunch thursday",
            "from": "friend@example.com",
            "snippet": "see you there",
            "date_ts": 1780000000,
            "attachments": [{"filename": "menu.pdf"}],
        },
        "phi1": {
            "subject": "Invoice - client treatment plan DDD member",
            "from": "billing@lexvendor.com",
            "snippet": "AHCCCS client invoice $500.00",
            "date_ts": 1780000000,
            "attachments": [{"filename": "invoice.pdf"}],
        },
    }
    _wire_gmail_mocks(monkeypatch, messages)
    filed_calls = []

    def fake_file(mailbox, message_id, dry_run=False):
        filed_calls.append((mailbox, message_id))
        # Mirror the real ledger write so the dedup test is faithful.
        ids = fr._load_filed_ids()
        ids[f"{mailbox}:{message_id}"] = 1
        fr._save_filed_ids(ids)
        return [{"filename": "f.pdf", "link": "https://drive/f"}]

    monkeypatch.setattr(fr, "file_message_attachments", fake_file)

    result = fr.run_digest(lookback_days=7)
    rows = result["rows"]
    # Only the clean financial message surfaces: personal fails the
    # classifier, the PHI invoice is excluded.
    assert [r["subject"] for r in rows] == ["Invoice 998 from Wildpack"] * 2
    assert rows[0]["amount"] == "$1,250.00"
    assert rows[0]["link"] == "https://drive/f"
    # 2 enabled+dwd accounts scanned (disabled account skipped).
    assert result["accounts_scanned"] == 2
    # Watermarks advanced for both accounts.
    marks = json.loads(fr._WATERMARKS_PATH.read_text())
    assert set(marks) == {"hannah@hjrglobal.com", "tommy@f3energy.com"}

    # Second run: dedup ledger prevents re-surfacing.
    result2 = fr.run_digest(lookback_days=7)
    assert result2["rows"] == []


def test_digest_dry_run_files_nothing(monkeypatch):
    _wire_gmail_mocks(monkeypatch, {
        "fin1": {
            "subject": "Invoice 12", "from": "billing@x.com",
            "snippet": "Total $5.00 amount due", "date_ts": 1780000000,
            "attachments": [{"filename": "invoice.pdf"}],
        },
    })
    seen_dry = []
    monkeypatch.setattr(
        fr, "file_message_attachments",
        lambda mailbox, mid, dry_run=False: (
            seen_dry.append(dry_run) or [{"filename": "f.pdf", "link": "(dry-run)"}]
        ),
    )
    result = fr.run_digest(dry_run=True)
    assert all(seen_dry)
    assert not fr._WATERMARKS_PATH.exists()


def test_digest_accounts_enabled_dwd_only():
    assert fr._digest_accounts() == ["hannah@hjrglobal.com", "tommy@f3energy.com"]


def test_format_digest_rows_and_empty():
    text = fr.format_digest([], 5)
    assert "no new receipts" in text
    text = fr.format_digest([{
        "vendor": "billing@wildpack.com", "subject": "Invoice 998",
        "amount": "$1,250.00", "date": "2026-06-01",
        "mailbox": "hannah@hjrglobal.com", "link": "https://drive/f",
    }], 5)
    assert "Invoice 998" in text and "$1,250.00" in text and "filed copy" in text


def test_parse_amount():
    assert fr.parse_amount("Invoice total $1,250.00 due") == "$1,250.00"
    assert fr.parse_amount("no money here", "but $44 here") == "$44"
    assert fr.parse_amount("nothing") == ""


# ── Format finance chunks ─────────────────────────────────────────────────────

def test_format_finance_chunks_includes_filed_links():
    r = _fin_result("hannah@hjrglobal.com", "m1")
    text = fr.format_finance_chunks(
        [r], "all monitored mailboxes", {"gmail:hannah@hjrglobal.com:m1": "https://drive/m1"},
    )
    assert "Filed: https://drive/m1" in text
    assert "hannah@hjrglobal.com" in text


def test_format_finance_chunks_empty():
    text = fr.format_finance_chunks([], "all monitored mailboxes", {})
    assert "No matching financial documents" in text


# ── W4-02: delivery-failure fail-loud alert ────────────────────────────────────

def _capture_slack(monkeypatch):
    sent = {}

    class _Client:
        def __init__(self, token=None):
            pass

        def chat_postMessage(self, channel, text, **kw):
            sent["channel"] = channel
            sent["text"] = text
            return {"ok": True}

    import slack_sdk
    monkeypatch.setattr(slack_sdk, "WebClient", _Client)
    return sent


def test_alert_delivery_failure_posts_metadata_only(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.delenv("FINANCE_DIGEST_FALLBACK_CHANNEL", raising=False)
    monkeypatch.delenv("HEALTH_REPORT_CHANNEL", raising=False)
    sent = _capture_slack(monkeypatch)

    ok = fr.alert_delivery_failure(164, 28)
    assert ok is True
    assert sent["channel"] == "hjrg-leadership"        # default ops fallback
    txt = sent["text"]
    # Metadata only — count + reason + fix, NO financial CONTENT leaked.
    assert "164 financial document" in txt
    assert "could not be delivered" in txt
    assert "$" not in txt              # no amounts
    assert "filed copy" not in txt     # no per-doc vendor lines
    assert "un-archive" in txt and "finance-receipt-allowlist.yaml" in txt


def test_alert_delivery_failure_honors_fallback_env(monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("FINANCE_DIGEST_FALLBACK_CHANNEL", "cora-health")
    sent = _capture_slack(monkeypatch)
    fr.alert_delivery_failure(1, 1)
    assert sent["channel"] == "cora-health"


def test_alert_delivery_failure_no_token_returns_false(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    # Should not raise, should not post, returns False.
    assert fr.alert_delivery_failure(5, 3) is False


def test_fallback_channel_precedence(monkeypatch):
    monkeypatch.delenv("FINANCE_DIGEST_FALLBACK_CHANNEL", raising=False)
    monkeypatch.delenv("HEALTH_REPORT_CHANNEL", raising=False)
    assert fr._fallback_alert_channel() == "hjrg-leadership"
    monkeypatch.setenv("HEALTH_REPORT_CHANNEL", "cora-health")
    assert fr._fallback_alert_channel() == "cora-health"
    monkeypatch.setenv("FINANCE_DIGEST_FALLBACK_CHANNEL", "explicit-chan")
    assert fr._fallback_alert_channel() == "explicit-chan"
