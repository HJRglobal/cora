"""Phase 2.3 -- content-level PHI scrub on the general KB retrieval path (F-2, G-B).

A LEX-authorized NON-custodian whose question misses the `phi` keyword gate must
not surface raw PHI from a retrieved LEX chunk. context_loader scrubs LEX chunk
text for non-custodians (phi_custodian=False), preserves staff names, and leaves
custodians + non-LEX retrievals untouched. The custodian gate + entity-siloing
are unchanged.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test-token")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")

import cora.context_loader as cl  # noqa: E402
from cora.knowledge_base.store import SearchResult  # noqa: E402

_PHI_TEXT = (
    "Client Bob Smith's care plan needs review. Shaun Hawkins will follow up. "
    "DOB 03/15/1990."
)


def _result(content: str, source: str = "asana", entity: str = "LEX") -> SearchResult:
    return SearchResult(
        chunk_id="c1",
        source=source,
        source_id="s1",
        entity=entity,
        title="t",
        content=content,
        deep_link="",
        date_modified=None,
        distance=0.2,  # below the distance threshold
    )


def _patch_staff(monkeypatch, names=("Shaun Hawkins",)):
    monkeypatch.setattr(
        cl.org_roles, "all_roles",
        lambda: [SimpleNamespace(name=n) for n in names],
    )


# ── direct scrub helper ───────────────────────────────────────────────────────
def test_apply_lex_phi_scrub_redacts_phi_keeps_staff(monkeypatch):
    _patch_staff(monkeypatch)
    out = cl._apply_lex_phi_scrub([_result(_PHI_TEXT)])
    scrubbed = out[0].content
    assert "Bob Smith" not in scrubbed
    assert "1990" not in scrubbed  # DOB redacted
    assert "Shaun Hawkins" in scrubbed  # staff name preserved


def test_apply_lex_phi_scrub_fail_closed_on_error(monkeypatch):
    _patch_staff(monkeypatch)
    monkeypatch.setattr(
        cl.phi_guard, "scrub_lex_phi",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    r = _result(_PHI_TEXT)
    out = cl._apply_lex_phi_scrub([r])
    # Scrub raised -> content WITHHELD (fail-closed); raw PHI never surfaces.
    assert "Bob Smith" not in out[0].content
    assert "withheld" in out[0].content.lower()


def test_apply_lex_phi_scrub_neutralizes_title_and_deeplink(monkeypatch):
    _patch_staff(monkeypatch)
    r = _result("benign body")
    # A BARE client-name title + a pre-baked deep-link whose LABEL is the same bare
    # name (the dominant LEX form; scrub_lex_phi can't catch a cue-less name).
    r.title = "Jalen Alicea Progress Review"
    r.deep_link = "<https://app.fireflies.ai/view/abc|Jalen Alicea Progress Review>"
    out = cl._apply_lex_phi_scrub([r])
    assert "Alicea" not in out[0].title           # title neutralized
    assert out[0].deep_link == ""                  # link dropped -> label can't leak
    assert out[0].title == "LEX knowledge base entry"


def test_apply_lex_phi_scrub_redacts_cue_adjacent_bare_name(monkeypatch):
    # B5: a bare client name near a cue ("client, Madison" / "session") that
    # scrub_lex_phi's immediate-noun rule misses must be redacted on the
    # non-custodian retrieval path; staff + non-PHI prose survive.
    _patch_staff(monkeypatch)
    body = "Shaun Hawkins noted the client, Madison, attended the session on 6/30."
    out = cl._apply_lex_phi_scrub([_result(body)])
    scrubbed = out[0].content
    assert "Madison" not in scrubbed
    assert "Shaun Hawkins" in scrubbed   # staff preserved
    assert "6/30" in scrubbed            # operational date survives (numeric)


# ── retrieval path: LEX non-custodian / custodian / non-LEX ──────────────────
def _wire_kb(monkeypatch, results):
    monkeypatch.setattr(cl, "_KB_DB_PATH", Path(__file__).resolve().parent)  # a dir that .exists()
    fake_kb = SimpleNamespace(search=lambda *a, **k: list(results))
    monkeypatch.setattr(cl, "get_shared_kb", lambda: fake_kb)


def test_lex_non_custodian_retrieval_is_scrubbed(monkeypatch):
    _patch_staff(monkeypatch)
    _wire_kb(monkeypatch, [_result(_PHI_TEXT, entity="LEX")])
    text = cl._try_kb_retrieve("LEX", "how is the project going", phi_custodian=False)
    assert text is not None
    assert "Bob Smith" not in text
    assert "1990" not in text
    assert "Shaun Hawkins" in text


def test_lex_custodian_retrieval_is_not_scrubbed(monkeypatch):
    _patch_staff(monkeypatch)
    _wire_kb(monkeypatch, [_result(_PHI_TEXT, entity="LEX")])
    text = cl._try_kb_retrieve("LEX", "how is the project going", phi_custodian=True)
    assert text is not None
    assert "Bob Smith" in text  # custodian sees full PHI
    assert "1990" in text


def test_lex_sub_entity_non_custodian_is_scrubbed(monkeypatch):
    """A LEX sub-entity channel (LEX-LLC -> kb_entity LEX) is scrubbed too."""
    _patch_staff(monkeypatch)
    _wire_kb(monkeypatch, [_result(_PHI_TEXT, entity="LEX")])
    text = cl._try_kb_retrieve("LEX-LLC", "status update", phi_custodian=False)
    assert text is not None and "Bob Smith" not in text


# ── W2-01 (2026-07-05): non-LEX content PHI backstop ─────────────────────────
# POSTURE CHANGE (audit Slice D, W2-01) — HARRISON SIGN-OFF REQUIRED at merge:
# The former test_non_lex_retrieval_is_never_scrubbed ENSHRINED the passthrough of a
# mis-tagged LEX-PHI chunk under a non-LEX entity ("assert 'Bob Smith' in text"). That
# was the documented residual: only the prompt-only FNDR guardrail backstopped it,
# violating D-034 (deterministic code over prompt for a PHI invariant). W2-01 adds a
# content-level backstop mirroring drive_materializer._phi_wall's non-LEX branch, so a
# mis-tagged CLINICAL-PHI chunk is now WITHHELD — while ordinary non-PHI / wellness /
# commercial-billing non-LEX prose still passes untouched (the tests below pin both).
def test_non_lex_retrieval_clinical_phi_is_withheld(monkeypatch):
    """A LEX-PHI chunk MIS-TAGGED under a non-LEX entity (F3E) is now WITHHELD for a
    non-custodian (W2-01). _PHI_TEXT carries a DOB -> is_clinical_phi -> dropped."""
    _patch_staff(monkeypatch)
    _wire_kb(monkeypatch, [_result(_PHI_TEXT, entity="F3E")])
    text = cl._try_kb_retrieve("F3E", "anything", phi_custodian=False)
    assert "Bob Smith" not in (text or "")
    assert "1990" not in (text or "")


def test_non_lex_retrieval_ordinary_prose_passes(monkeypatch):
    """Ordinary non-PHI non-LEX content is NOT over-refused (the passthrough that
    matters — the backstop is NARROW)."""
    _patch_staff(monkeypatch)
    body = "The F3 Pure hero images are 2880px wide and Larry owns the ad platform."
    _wire_kb(monkeypatch, [_result(body, entity="F3E")])
    text = cl._try_kb_retrieve("F3E", "anything", phi_custodian=False)
    assert text is not None and "2880px" in text


def test_non_lex_retrieval_wellness_passes(monkeypatch):
    """F3 Mood wellness copy (anxiety/calm/focus) must NOT be over-refused (the
    wellness-overlap trap the slice explicitly warns against)."""
    _patch_staff(monkeypatch)
    body = "F3 Mood helps take the edge off everyday anxiety and supports calm focus."
    _wire_kb(monkeypatch, [_result(body, entity="F3E")])
    text = cl._try_kb_retrieve("F3E", "how does mood help", phi_custodian=False)
    assert text is not None and "anxiety" in text


def test_non_lex_retrieval_commercial_billing_passes(monkeypatch):
    """Ordinary commercial 'client billing' vocab with NO care-program cue passes —
    the billing/status leg requires a Lexington/Medicaid cue to fire."""
    _patch_staff(monkeypatch)
    body = "The client's invoice for the wholesale energy-drink order is past due."
    _wire_kb(monkeypatch, [_result(body, entity="F3E")])
    text = cl._try_kb_retrieve("F3E", "billing", phi_custodian=False)
    assert text is not None and "invoice" in text


def test_non_lex_custodian_bypasses_backstop(monkeypatch):
    """phi_custodian=True skips the non-LEX backstop (a custodian is authorized for PHI)."""
    _patch_staff(monkeypatch)
    _wire_kb(monkeypatch, [_result(_PHI_TEXT, entity="F3E")])
    text = cl._try_kb_retrieve("F3E", "anything", phi_custodian=True)
    assert text is not None and "Bob Smith" in text


def test_withhold_non_lex_phi_direct_and_fail_closed(monkeypatch):
    """_withhold_non_lex_phi drops PHI chunks, keeps benign ones, and fail-closes on a
    predicate error (withhold, never surface un-vetted content)."""
    _patch_staff(monkeypatch)
    benign = _result("F3 Pure hero images are 2880px", entity="F3E")
    phi = _result(_PHI_TEXT, entity="F3E")
    kept = cl._withhold_non_lex_phi([benign, phi])
    assert kept == [benign]
    # fail-closed: the LIVE predicate raises -> chunk withheld
    monkeypatch.setattr(
        cl.phi_guard, "non_lex_phi_backstop_trips_live",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert cl._withhold_non_lex_phi([benign]) == []


def test_non_lex_backstop_neutralizes_fireflies_client_title(monkeypatch):
    """D-051 finding 1: a mis-tagged non-LEX fireflies chunk with a bare client-name TITLE
    (benign body, so the body predicate keeps it) has its citation neutralized so the client
    name never reaches the LLM — on BOTH the main path and the cross-entity fallback."""
    _patch_staff(monkeypatch)
    r = _result("Reviewed the operational checklist; next steps assigned.",
                source="fireflies", entity="F3E")
    r.title = "Jalen Alicea Intake Assessment"
    r.deep_link = "<https://app.fireflies.ai/view/abc|Jalen Alicea Intake Assessment>"
    # main path
    _wire_kb(monkeypatch, [r])
    text = cl._try_kb_retrieve("F3E", "what were the next steps", phi_custodian=False)
    assert text is not None
    assert "Alicea" not in text and "fireflies.ai" not in text
    assert "next steps" in text  # the vetted body still answers


def test_non_lex_backstop_keeps_ordinary_asana_title(monkeypatch):
    """A benign non-fireflies title with no client-name signal is preserved (no over-strip
    of legitimate business citations)."""
    _patch_staff(monkeypatch)
    r = _result("The Sprouts wholesale order ships Friday.", source="asana", entity="F3E")
    r.title = "Sprouts Wholesale Order"
    out = cl._withhold_non_lex_phi([r])
    assert out and out[0].title == "Sprouts Wholesale Order"


def test_cross_entity_fallback_withholds_mistagged_phi(monkeypatch):
    """The cross-entity fallback (FNDR/HJRG channel, empty main search) also applies the
    backstop — a clinical-PHI chunk mis-tagged F3E is withheld for a non-custodian."""
    _patch_staff(monkeypatch)

    def _search(*a, **k):
        ent = k.get("entity")
        # Empty for the channel's own entity; a mis-tagged clinical chunk on a fallback entity.
        if ent in ("FNDR", "HJRG"):
            return []
        return [_result(_PHI_TEXT, entity="F3E")]

    monkeypatch.setattr(cl, "_KB_DB_PATH", Path(__file__).resolve().parent)
    monkeypatch.setattr(cl, "get_shared_kb", lambda: SimpleNamespace(search=_search))
    text = cl._try_kb_retrieve("HJRG", "who is the vendor", phi_custodian=False)
    assert "Bob Smith" not in (text or "")


# ── Cache PHI-leak guard (custodian answers never enter the shared cache) ────
_APP_SRC = (Path(__file__).resolve().parent.parent / "src" / "cora" / "app.py").read_text(
    encoding="utf-8"
)


def test_custodian_answer_excluded_from_semantic_cache():
    """A custodian's un-scrubbed LEX answer must not be cacheable -- the
    user-agnostic semantic cache would replay it to a non-custodian, bypassing the
    retrieval scrub. Pinned at the cache_storable expression."""
    assert "and not phi_custodian" in _APP_SRC
    # phi_custodian is defaulted before the retrieval branch so it's always in scope.
    assert "phi_custodian = False" in _APP_SRC
