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


def test_non_lex_retrieval_is_never_scrubbed(monkeypatch):
    """A non-LEX entity retrieval is left untouched even for a non-custodian."""
    _patch_staff(monkeypatch)
    # Same text in an F3E chunk (contrived) must pass through unscrubbed.
    _wire_kb(monkeypatch, [_result(_PHI_TEXT, entity="F3E")])
    text = cl._try_kb_retrieve("F3E", "anything", phi_custodian=False)
    assert text is not None
    assert "Bob Smith" in text  # scrub does not run outside LEX scope


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
