"""Tests for per-user email/Drive historical access control (Tiers 1 + 2).

Matrix from the 2026-06-09 spec: owner-only retrieval, Harrison-any,
non-owner refusal, DM-only enforcement (channel redirect), Tier-1 header
strip on non-owner chunks, fail-closed on unmapped user, alias matching,
LEX PHI still excluded — plus the app.py / context_loader wiring asserts
(D-034: guard must run before any Claude call).
"""

import hashlib
from pathlib import Path

import pytest

from cora import historical_access as ha
from cora.knowledge_base import embeddings
from cora.knowledge_base.store import (
    Document, KnowledgeBase, KnowledgeBaseError, SearchResult,
)

_DIM = 1536


def _vec_for(text: str) -> list[float]:
    h = hashlib.sha256(text.encode()).digest()
    vec = [0.0] * _DIM
    for i in range(12):
        vec[(h[i] * 7 + i * 131) % _DIM] = (h[i] / 255.0) + 0.25
    return vec


@pytest.fixture(autouse=True)
def patch_embeddings(monkeypatch):
    monkeypatch.setattr(embeddings, "embed_texts",
                        lambda texts: [_vec_for(t) for t in texts])
    monkeypatch.setattr(embeddings, "embed_query", _vec_for)


_ACCOUNTS_YAML = """
accounts:
  - email: harrison@hjrglobal.com
    name: Harrison Rogers
    enabled: true
    slack_user_id: U0B2RM2JYJ1
    known_aliases:
      - harrison@f3energy.com
  - email: tommy@f3energy.com
    name: Tommy Anderson
    enabled: true
    slack_user_id: UTOMMY
    known_aliases:
      - tommy@hjrglobal.com
  - email: hannah@hjrglobal.com
    name: Hannah Grant
    enabled: true
    slack_user_id: UHANNAH
  - email: shaun@lexingtonservices.com
    name: Shaun Hawkins
    enabled: true
    slack_user_id: USHAUN
  - email: payables@hjrglobal.com
    name: HJR Payables Inbox
    enabled: true
"""

_ALIASES_YAML = """
aliases:
  Shaun Hawkins:
    - Sean
  Hannah Grant:
    - Hannah
"""

_ALLOWLIST_YAML = """
unrestricted:
  - U0B2RM2JYJ1
"""


@pytest.fixture(autouse=True)
def identity_fixture(tmp_path, monkeypatch):
    accounts = tmp_path / "accounts.yaml"
    accounts.write_text(_ACCOUNTS_YAML, encoding="utf-8")
    aliases = tmp_path / "aliases.yaml"
    aliases.write_text(_ALIASES_YAML, encoding="utf-8")
    allowlist = tmp_path / "allowlist.yaml"
    allowlist.write_text(_ALLOWLIST_YAML, encoding="utf-8")
    monkeypatch.setattr(ha, "_ACCOUNTS_PATH", accounts)
    monkeypatch.setattr(ha, "_ALIASES_PATH", aliases)
    monkeypatch.setattr(ha, "_ALLOWLIST_PATH", allowlist)
    monkeypatch.setattr(ha, "_AUDIT_LOG_PATH", tmp_path / "audit.jsonl")
    ha.invalidate_cache()
    yield
    ha.invalidate_cache()


def _result(source="gmail", owner="hannah@hjrglobal.com", **kw) -> SearchResult:
    defaults = dict(
        chunk_id="c1",
        source=source,
        source_id=f"{source}:{owner}:msg123",
        entity="FNDR",
        title="Q3 retailer pitch follow-up",
        content=(
            "From: Hannah Grant <hannah@hjrglobal.com>\n"
            "To: buyer@sprouts.com\n"
            "Subject: Q3 retailer pitch follow-up\n"
            "Date: 2026-05-02\n"
            "Attachments: pitch.pdf\n\n"
            "Hi - following up on the Q3 retailer pitch and pricing."
        ),
        deep_link="https://mail.example/msg123",
        date_modified=1780000000,
        distance=0.5,
        author="Hannah Grant <hannah@hjrglobal.com>",
        metadata={"user_email": owner, "message_id": "msg123", "thread_id": "t1"},
    )
    defaults.update(kw)
    return SearchResult(**defaults)


# ── Identity + aliases ────────────────────────────────────────────────────────

def test_owned_emails_includes_aliases():
    assert ha.owned_emails("UTOMMY") == frozenset(
        {"tommy@f3energy.com", "tommy@hjrglobal.com"}
    )


def test_owned_emails_unmapped_user_is_empty():
    assert ha.owned_emails("UNOBODY") == frozenset()


def test_unrestricted_allowlist_default_harrison_only():
    assert ha.is_unrestricted("U0B2RM2JYJ1") is True
    assert ha.is_unrestricted("UTOMMY") is False
    assert ha.is_unrestricted("") is False


def test_allowlist_missing_file_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(ha, "_ALLOWLIST_PATH", tmp_path / "missing.yaml")
    ha.invalidate_cache()
    assert ha.is_unrestricted("U0B2RM2JYJ1") is False


def test_resolve_person_by_alias():
    label, emails = ha.resolve_person("Sean")
    assert label == "Shaun Hawkins"
    assert emails == frozenset({"shaun@lexingtonservices.com"})


# ── Retrieval-intent classifier ───────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "pull up my emails about Allen Flavors",
    "show me the email from Dana about the deposit",
    "can you find my drive files about onboarding",
    "what did Justin email me last week?",
    "search my inbox for the BCB thread",
    "forward me the email thread with Wildpack",
])
def test_retrieval_intent_positive(text):
    assert ha.detect_retrieval_intent(text) is True


@pytest.mark.parametrize("text", [
    "what's our refund policy?",
    "find the shipping invoice",          # stays on the KB DOCUMENT_QUERY path (D-013)
    "create an asana task for the launch",
    "what's the cash position this week?",
    "summarize the Q3 retailer strategy",
])
def test_retrieval_intent_negative(text):
    assert ha.detect_retrieval_intent(text) is False


# ── Target-person detection ───────────────────────────────────────────────────

def test_target_person_possessive():
    target = ha.detect_target_person("pull up Hannah's emails about UFL")
    assert target is not None
    assert target[0] == "Hannah Grant"
    assert target[1] == frozenset({"hannah@hjrglobal.com"})


def test_target_person_alias_possessive():
    target = ha.detect_target_person("show me Sean's inbox for DDD threads")
    assert target is not None and target[0] == "Shaun Hawkins"


def test_target_person_external_name_is_none():
    assert ha.detect_target_person("show me Dana's emails about flavors") is None


def test_target_from_pattern_only_with_include_from():
    text = "pull receipts from Hannah for May"
    assert ha.detect_target_person(text) is None
    target = ha.detect_target_person(text, include_from=True)
    assert target is not None and target[0] == "Hannah Grant"


def test_target_own_mailbox_referenced_by_name_is_not_a_target():
    own = ha.owned_emails("UHANNAH")
    assert ha.detect_target_person("pull up Hannah's emails", asker_emails=own) is None


# ── Tier 1 header strip ───────────────────────────────────────────────────────

def test_tier1_strips_non_owner_gmail_chunk():
    r = _result(owner="hannah@hjrglobal.com")
    out, unstripped = ha.apply_tier1([r], frozenset({"tommy@f3energy.com"}), False)
    assert unstripped is False
    s = out[0]
    assert "details withheld" in s.title
    assert s.author == "" and s.deep_link == "" and s.metadata is None
    assert s.date_modified is None
    assert "From:" not in s.content and "Subject:" not in s.content
    assert "Date:" not in s.content and "Attachments:" not in s.content
    # The factual body survives.
    assert "Q3 retailer pitch and pricing" in s.content


def test_tier1_owner_chunk_passes_unstripped_and_flags():
    r = _result(owner="hannah@hjrglobal.com")
    out, unstripped = ha.apply_tier1([r], frozenset({"hannah@hjrglobal.com"}), False)
    assert unstripped is True
    assert out[0].title == "Q3 retailer pitch follow-up"
    assert "From:" in out[0].content


def test_tier1_alias_owner_passes_unstripped():
    r = _result(owner="tommy@hjrglobal.com")
    out, unstripped = ha.apply_tier1([r], ha.owned_emails("UTOMMY"), False)
    assert unstripped is True
    assert out[0].deep_link != ""


def test_tier1_unrestricted_passes_unstripped():
    r = _result(owner="hannah@hjrglobal.com")
    out, unstripped = ha.apply_tier1([r], frozenset(), True)
    assert unstripped is True
    assert out[0].title == "Q3 retailer pitch follow-up"


def test_tier1_unknown_asker_fail_closed():
    r = _result(owner="hannah@hjrglobal.com")
    out, unstripped = ha.apply_tier1([r], frozenset(), False)
    assert unstripped is False
    assert "details withheld" in out[0].title


def test_tier1_unknown_owner_is_stripped():
    r = _result(owner="hannah@hjrglobal.com", source_id="gmail:bad", metadata=None)
    out, _ = ha.apply_tier1([r], frozenset({"hannah@hjrglobal.com"}), False)
    assert "details withheld" in out[0].title


def test_tier1_org_shared_founders_os_exempt():
    r = _result(source="drive_sweep", owner="founders_os@hjrglobal.com",
                metadata={"user_email": "founders_os@hjrglobal.com"})
    out, unstripped = ha.apply_tier1([r], frozenset(), False)
    assert out[0].title == "Q3 retailer pitch follow-up"
    assert unstripped is False  # org-shared does not poison the cache flag


def test_tier1_non_personal_sources_untouched():
    r = _result(source="fireflies", metadata=None)
    out, _ = ha.apply_tier1([r], frozenset(), False)
    assert out[0].title == "Q3 retailer pitch follow-up"
    assert out[0].deep_link != ""


def test_chunk_owner_falls_back_to_gmail_source_id():
    r = _result(metadata=None, source_id="gmail:hannah@hjrglobal.com:msg9")
    assert ha.chunk_owner_email(r) == "hannah@hjrglobal.com"


# ── Tier 2 decisions ──────────────────────────────────────────────────────────

def test_tier2_pass_for_general_question():
    d = ha.check_tier2("UHANNAH", True, "what's the latest on the Tucson opening?")
    assert d.action == "pass"


def test_tier2_channel_redirects_to_dm():
    d = ha.check_tier2("UHANNAH", False, "pull up my emails about UFL")
    assert d.action == "respond"
    assert "DM" in d.message


def test_tier2_dm_own_mailbox_grant():
    d = ha.check_tier2("UHANNAH", True, "pull up my emails about UFL")
    assert d.action == "grant"
    assert d.mode == "personal"
    assert d.owner_emails == frozenset({"hannah@hjrglobal.com"})


def test_tier2_non_owner_request_refused_without_existence_leak():
    d = ha.check_tier2("UTOMMY", True, "show me Hannah's emails about UFL")
    assert d.action == "respond"
    assert "your own" in d.message
    # No leak of whether such items exist.
    assert "found" not in d.message.lower() and "exist" not in d.message.lower()


def test_tier2_harrison_override_grants_target_mailbox():
    d = ha.check_tier2("U0B2RM2JYJ1", True, "pull up Hannah's emails about UFL")
    assert d.action == "grant"
    assert d.owner_emails == frozenset({"hannah@hjrglobal.com"})
    assert d.target_label == "Hannah Grant"


def test_tier2_unmapped_user_fails_closed():
    d = ha.check_tier2("UNOBODY", True, "pull up my emails about UFL")
    assert d.action == "respond"
    assert "can't link" in d.message


def test_tier2_no_user_id_fails_closed():
    d = ha.check_tier2("", True, "pull up my emails about UFL")
    assert d.action == "respond"


# ── PHI exclusion on grants ───────────────────────────────────────────────────

def test_drop_phi_removes_client_record_chunks():
    clean = _result()
    phi = _result(
        title="Client treatment plan - DDD member",
        content="Treatment plan for DDD client Jalen, AHCCCS ID 12345.",
    )
    out = ha.drop_phi([clean, phi])
    assert out == [clean]


# ── Owner-scoped KB search (store.search_owned) ───────────────────────────────

@pytest.fixture
def kb(tmp_path):
    db = KnowledgeBase(tmp_path / "owned_kb.db")
    yield db
    db.close()


def _gmail_doc(owner: str, source_id: str, content: str, financial=False, **kw):
    meta = {"user_email": owner, "message_id": source_id}
    if financial:
        meta["financial_document"] = True
    return Document(
        source="gmail", source_id=f"gmail:{owner}:{source_id}", entity="FNDR",
        content=content, title=kw.pop("title", content[:40]),
        author=owner, metadata=meta, **kw,
    )


def test_search_owned_owner_only(kb):
    kb.upsert_documents([
        _gmail_doc("hannah@hjrglobal.com", "h1", "pricing thread about widgets"),
        _gmail_doc("tommy@f3energy.com", "t1", "pricing thread about widgets too"),
    ])
    res = kb.search_owned(
        "pricing thread about widgets",
        owner_emails=frozenset({"hannah@hjrglobal.com"}),
    )
    assert res
    assert all(r.metadata["user_email"] == "hannah@hjrglobal.com" for r in res)


def test_search_owned_alias_set_matches_both_mailboxes(kb):
    kb.upsert_documents([
        _gmail_doc("tommy@f3energy.com", "t1", "sample kit follow up"),
        _gmail_doc("tommy@hjrglobal.com", "t2", "sample kit follow up again"),
    ])
    res = kb.search_owned(
        "sample kit follow up",
        owner_emails=frozenset({"tommy@f3energy.com", "tommy@hjrglobal.com"}),
    )
    owners = {r.metadata["user_email"] for r in res}
    assert owners == {"tommy@f3energy.com", "tommy@hjrglobal.com"}


def test_search_owned_financial_only_filter(kb):
    kb.upsert_documents([
        _gmail_doc("hannah@hjrglobal.com", "f1", "invoice 123 amount due $500.00",
                   financial=True, title="Invoice 123 - $500.00"),
        _gmail_doc("hannah@hjrglobal.com", "n1", "lunch plans thursday"),
    ])
    res = kb.search_owned(
        "invoice amount due", owner_emails=None, financial_only=True,
    )
    assert res
    assert all((r.metadata or {}).get("financial_document") for r in res)


def test_search_owned_any_mailbox_requires_financial_only(kb):
    with pytest.raises(KnowledgeBaseError):
        kb.search_owned("anything", owner_emails=None, financial_only=False)


def test_search_owned_excludes_non_personal_sources(kb):
    kb.upsert_documents([
        Document(source="fireflies", source_id="m1", entity="FNDR",
                 content="pricing thread about widgets",
                 metadata={"user_email": "hannah@hjrglobal.com"}),
        _gmail_doc("hannah@hjrglobal.com", "h1", "pricing thread about widgets"),
    ])
    res = kb.search_owned(
        "pricing thread about widgets",
        owner_emails=frozenset({"hannah@hjrglobal.com"}),
    )
    assert {r.source for r in res} == {"gmail"}


def test_search_results_carry_author_and_metadata(kb):
    kb.upsert_documents([_gmail_doc("hannah@hjrglobal.com", "h1", "widget pricing")])
    res = kb.search("widget pricing", entity="FNDR", k=5, max_age_days=None)
    assert res
    assert res[0].author == "hannah@hjrglobal.com"
    assert res[0].metadata["user_email"] == "hannah@hjrglobal.com"


# ── Ingest-time financial tagging (store Step 0b) ─────────────────────────────

def test_upsert_tags_financial_documents(kb):
    kb.upsert_documents([
        _gmail_doc("hannah@hjrglobal.com", "f1",
                   "Amount due $1,250.00 by June 30. Invoice number 998.",
                   title="Invoice 998 from Wildpack"),
        _gmail_doc("hannah@hjrglobal.com", "n1", "see you at the meeting tomorrow",
                   title="meeting tomorrow"),
    ])
    res = kb.search_owned("invoice amount due", owner_emails=None, financial_only=True)
    ids = {r.source_id for r in res}
    assert any("f1" in i for i in ids)
    assert not any("n1" in i for i in ids)


# ── Audit log ─────────────────────────────────────────────────────────────────

def test_audit_writes_jsonl(tmp_path, monkeypatch):
    path = tmp_path / "audit.jsonl"
    monkeypatch.setattr(ha, "_AUDIT_LOG_PATH", path)
    ha.audit("UHANNAH", "pull up my emails", "personal",
             frozenset({"hannah@hjrglobal.com"}), ["gmail:x:1"], channel="dm")
    import json
    entry = json.loads(path.read_text().strip())
    assert entry["requester"] == "UHANNAH"
    assert entry["mode"] == "personal"
    assert entry["owner_emails"] == ["hannah@hjrglobal.com"]


# ── Formatting ────────────────────────────────────────────────────────────────

def test_format_owned_chunks_full_headers():
    r = _result(owner="hannah@hjrglobal.com")
    text = ha.format_owned_chunks([r], "your")
    assert "Q3 retailer pitch follow-up" in text
    assert "hannah@hjrglobal.com" in text
    assert "owner-authorized" in text


def test_format_owned_chunks_empty():
    text = ha.format_owned_chunks([], "your")
    assert "No matching items" in text


# ── Wiring asserts (D-034: guard before any Claude call) ─────────────────────

_APP_SRC = (Path(__file__).resolve().parents[1] / "src" / "cora" / "app.py").read_text(
    encoding="utf-8"
)
_CTX_SRC = (
    Path(__file__).resolve().parents[1] / "src" / "cora" / "context_loader.py"
).read_text(encoding="utf-8")


def test_app_wires_gate_before_intent_classification():
    gate_pos = _APP_SRC.index("finance_receipts.check_request")
    tier2_pos = _APP_SRC.index("historical_access.check_tier2")
    intent_pos = _APP_SRC.index("intent = ic.classify(")
    assert gate_pos < intent_pos and tier2_pos < intent_pos


def test_app_grant_skips_semantic_cache():
    assert "retrieval_grant is None" in _APP_SRC
    assert "cache_storable" in _APP_SRC
    # Both cache-store call sites are guarded.
    assert _APP_SRC.count("if cache_storable:") == 2


def test_app_has_dm_retrieval_branch():
    assert "historical_access.detect_retrieval_intent(text)" in _APP_SRC
    assert 'channel_name="dm"' in _APP_SRC


def test_app_injects_tier1_synthesis_rule():
    assert "TIER1_SYNTHESIS_RULE" in _APP_SRC


def test_context_loader_applies_tier1():
    assert "historical_access.apply_tier1" in _CTX_SRC
    assert "unstripped_personal" in _CTX_SRC
