"""WS17-C Part 3 -- build_coras_read enrichment + its six guards.

The read is decision-SUPPORT: it never writes/approves; it is advisory text
appended to the knowledge proposal DM. Every guard is fail-soft (-> "").
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import cora.coras_read as cr


@pytest.fixture(autouse=True)
def _clear_cache():
    cr._CACHE.clear()
    yield
    cr._CACHE.clear()


def _hit(content, source="slack", distance=0.5):
    return SimpleNamespace(content=content, source=source, distance=distance,
                           title="some title", deep_link="https://drive.google.com/x",
                           entity="F3E")


def _fake_kb(hits):
    kb = MagicMock()
    kb.search.return_value = list(hits)
    return kb


def _update(text="The Anaheim warehouse moved to 500 Brand Blvd.", entity="F3E"):
    return {"update_type": "generic", "description": text,
            "payload": {"text": text, "entity": entity, "source": "info-for-cora"}}


# ── fail-soft ────────────────────────────────────────────────────────────────

def test_no_api_key_returns_empty(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # _classify -> None
    assert cr.build_coras_read(_update(), kb=_fake_kb([])) == ""


def test_empty_claim_returns_empty():
    assert cr.build_coras_read({"payload": {}, "description": ""}, kb=_fake_kb([])) == ""


def test_kb_search_raises_is_fail_soft(monkeypatch):
    # Retrieval failure must not crash; classify still runs against empty evidence.
    monkeypatch.setattr(cr, "_classify", lambda *a, **k: {"verdict": "NET-NEW", "note": "n"})
    kb = MagicMock()
    kb.search.side_effect = RuntimeError("kb dead")
    out = cr.build_coras_read(_update(), kb=kb)
    assert out.startswith("🧠")


def test_classify_raises_is_fail_soft(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("llm down")
    monkeypatch.setattr(cr, "_classify", boom)
    assert cr.build_coras_read(_update(), kb=_fake_kb([_hit("ctx")])) == ""


# ── entity-scope (cross-entity firewall) ──────────────────────────────────────

def test_lex_subentity_scopes_to_lex(monkeypatch):
    captured = {}

    def fake_search(query=None, entity=None, k=None, sub_entity=None):
        captured["entity"] = entity
        captured["sub_entity"] = sub_entity
        return []
    kb = MagicMock()
    kb.search.side_effect = fake_search
    monkeypatch.setattr(cr, "_classify", lambda *a, **k: {"verdict": "NET-NEW", "note": "x"})
    cr.build_coras_read(_update(entity="LEX-LLC"), kb=kb)
    assert captured["entity"] == "LEX"
    assert captured["sub_entity"] == "LEX-LLC"


def test_paired_entity_also_searched_but_not_others(monkeypatch):
    seen = []

    def fake_search(query=None, entity=None, k=None, sub_entity=None):
        seen.append(entity)
        return []
    kb = MagicMock()
    kb.search.side_effect = fake_search
    monkeypatch.setattr(cr, "_classify", lambda *a, **k: {"verdict": "NET-NEW", "note": "x"})
    cr.build_coras_read(_update(entity="F3E"), kb=kb)
    assert "F3E" in seen and "F3C" in seen       # paired pair retrieved
    assert "OSN" not in seen and "LEX" not in seen  # nothing else


# ── PHI (evidence drop + note drop + LEX scrub) ────────────────────────────────

def test_phi_evidence_excluded_from_prompt(monkeypatch):
    captured = {}

    def fake_classify(claim, prior, evidence):
        captured["evidence"] = evidence
        return {"verdict": "CORROBORATED", "note": "ok"}
    monkeypatch.setattr(cr, "_classify", fake_classify)
    hits = [_hit("The client was diagnosed with autism and started risperidone."),
            _hit("The Anaheim warehouse is at 500 Brand Blvd.")]
    cr.build_coras_read(_update(), kb=_fake_kb(hits))
    joined = " ".join(captured["evidence"])
    assert "risperidone" not in joined          # clinical PHI chunk dropped
    assert "Brand Blvd" in joined               # clean chunk kept


def test_note_with_phi_is_dropped_label_kept(monkeypatch):
    monkeypatch.setattr(cr, "_classify", lambda *a, **k:
                        {"verdict": "CONFLICTS", "note": "the patient was diagnosed with autism"})
    out = cr.build_coras_read(_update(), kb=_fake_kb([_hit("ctx")]))
    assert "autism" not in out                  # PHI note dropped
    assert "CONFLICTS" in out                    # safe verdict label still shown


def test_excluded_sources_never_evidence(monkeypatch):
    captured = {}

    def fake_classify(claim, prior, evidence):
        captured["ev"] = evidence
        return {"verdict": "NET-NEW", "note": "x"}
    monkeypatch.setattr(cr, "_classify", fake_classify)
    hits = [_hit("stale team note", source="team_note"),
            _hit("private user note", source="user_note"),
            _hit("a real slack fact", source="slack")]
    cr.build_coras_read(_update(), kb=_fake_kb(hits))
    joined = " ".join(captured["ev"])
    assert "team note" not in joined and "user note" not in joined
    assert "a real slack fact" in joined


def test_distance_ceiling_filters_far_chunks(monkeypatch):
    captured = {}

    def fake_classify(claim, prior, evidence):
        captured["ev"] = evidence
        return {"verdict": "NET-NEW", "note": "x"}
    monkeypatch.setattr(cr, "_classify", fake_classify)
    hits = [_hit("too far away", distance=2.0), _hit("close enough", distance=0.4)]
    cr.build_coras_read(_update(), kb=_fake_kb(hits))
    joined = " ".join(captured["ev"])
    assert "too far away" not in joined and "close enough" in joined


# ── source-opacity ─────────────────────────────────────────────────────────────

def test_note_links_and_ids_redacted(monkeypatch):
    monkeypatch.setattr(cr, "_classify", lambda *a, **k:
                        {"verdict": "CORROBORATED", "note": "see https://docs.google.com/abc123"})
    out = cr.build_coras_read(_update(), kb=_fake_kb([_hit("ctx")]))
    assert "docs.google.com" not in out


# ── verdict validation + format ──────────────────────────────────────────────

def test_unknown_verdict_returns_empty(monkeypatch):
    monkeypatch.setattr(cr, "_classify", lambda *a, **k: {"verdict": "MAYBE", "note": "x"})
    assert cr.build_coras_read(_update(), kb=_fake_kb([_hit("ctx")])) == ""


def test_corroborated_format(monkeypatch):
    monkeypatch.setattr(cr, "_classify", lambda *a, **k:
                        {"verdict": "CORROBORATED", "note": "matches an existing F3E fact"})
    out = cr.build_coras_read(_update(), kb=_fake_kb([_hit("ctx")]))
    assert out == "🧠 *Cora's read:* ✅ CORROBORATED: matches an existing F3E fact"


# ── format_single_item_dm append (knowledge_review) ───────────────────────────

def test_format_single_item_dm_appends_read():
    import cora.knowledge_review as kr
    update = {"update_type": "known_answer", "confidence": "HIGH", "description": "A fact",
              "payload": {}, "_coras_read": "🧠 *Cora's read:* ✅ CORROBORATED: ok"}
    out = kr.format_single_item_dm(update)
    assert "Cora's read" in out
    assert out.index("Cora's read") < out.index("👍 Approve")  # before the footer


def test_phi_claim_not_sent_to_llm(monkeypatch):
    # MF-3: a PHI claim must be refused BEFORE _classify (defense-in-depth, even if
    # an upstream intake gate regressed).
    classify = MagicMock(return_value={"verdict": "NET-NEW", "note": "x"})
    monkeypatch.setattr(cr, "_classify", classify)
    upd = _update(text="the participant was diagnosed with autism and prescribed risperidone")
    assert cr.build_coras_read(upd, kb=_fake_kb([_hit("ctx")])) == ""
    classify.assert_not_called()


def test_lex_billing_evidence_excluded(monkeypatch):
    # MF-5: a LEX administrative-billing-PHI chunk (named person + auth, no clinical
    # keyword) must be dropped from the evidence for a LEX-scoped retrieval.
    captured = {}

    def fake_classify(claim, prior, evidence):
        captured["ev"] = evidence
        return {"verdict": "NET-NEW", "note": "x"}
    monkeypatch.setattr(cr, "_classify", fake_classify)
    hits = [_hit("Bob Smith's billing authorization is pending for next month."),
            _hit("The LLC fleet uses the new kiosk for clock-in.")]
    cr.build_coras_read(_update(text="how does LLC clock-in work", entity="LEX-LLC"),
                        kb=_fake_kb(hits))
    joined = " ".join(captured["ev"])
    assert "billing authorization" not in joined  # LEX admin-PHI chunk dropped
    assert "kiosk" in joined


def test_classify_constructs_client_with_timeout(monkeypatch):
    # MF-6: the LLM client is bounded so a hung call never delays/blocks the 7am DM.
    import sys
    import types
    captured = {}

    class _FakeMsgs:
        def create(self, **kw):
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(text='{"verdict":"NET-NEW","note":"ok"}')])

    class _FakeAnthropic:
        def __init__(self, **kw):
            captured.update(kw)
            self.messages = _FakeMsgs()

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=_FakeAnthropic))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    out = cr._classify("a claim", "prior", ["evidence"])
    assert out == {"verdict": "NET-NEW", "note": "ok"}
    assert captured.get("timeout") == 15.0
    assert captured.get("max_retries") == 1


def test_format_single_item_dm_no_read_when_absent():
    import cora.knowledge_review as kr
    update = {"update_type": "known_answer", "confidence": "HIGH", "description": "A fact",
              "payload": {}}
    out = kr.format_single_item_dm(update)
    assert "Cora's read" not in out


def test_billing_phi_claim_not_sent_to_llm_even_when_non_lex(monkeypatch):
    # WS17-C pre-merge fix (PHI-egress asymmetry): a folded contribution carries the
    # AUTHOR's entity (e.g. a custodian like Harrison=FNDR), but a named-client LEX
    # billing/authorization claim (no clinical keyword) must NOT reach the LLM
    # regardless of the carried entity. is_lex_billing is now UNCONDITIONAL on the
    # claim egress screen (was entity.startswith("LEX")-gated -> a real leak).
    classify = MagicMock(return_value={"verdict": "NET-NEW", "note": "x"})
    monkeypatch.setattr(cr, "_classify", classify)
    upd = _update(text="Bob Smith's billing authorization is pending.", entity="FNDR")
    assert cr.build_coras_read(upd, kb=_fake_kb([_hit("ctx")])) == ""
    classify.assert_not_called()


def test_billing_phi_evidence_excluded_even_when_non_lex(monkeypatch):
    # The evidence billing filter is now entity-agnostic (truly symmetric with _scrub):
    # a billing-PHI chunk is dropped before the prompt even for a NON-LEX retrieval.
    captured = {}

    def fake_classify(claim, prior, evidence):
        captured["ev"] = evidence
        return {"verdict": "NET-NEW", "note": "x"}
    monkeypatch.setattr(cr, "_classify", fake_classify)
    hits = [_hit("Maria Lopez's billing authorization is pending for next month."),
            _hit("The Anaheim warehouse is at 500 Brand Blvd.")]
    cr.build_coras_read(_update(entity="F3E"), kb=_fake_kb(hits))
    joined = " ".join(captured["ev"])
    assert "billing authorization" not in joined   # admin-PHI dropped even for non-LEX
    assert "Brand Blvd" in joined
