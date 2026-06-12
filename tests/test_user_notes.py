"""Tests for the personal-notes layer (Org Synthesis Phase 5, deliverable 1).

The load-bearing invariant under test: a personal note is retrievable ONLY by
its owner, enforced at the SQL layer (store.search excludes source='user_note'
entirely; store.search_user_notes filters on metadata.owner_slack). Adversarial
framing throughout — another user issuing the IDENTICAL query (identical
embedding vector) must never see the note.

Embeddings are mocked content-aware: texts containing "alpha" embed on axis 1,
everything else on axis 0. Same-axis L2 distance = 0; cross-axis = sqrt(2)
(~1.414) — above both the 1.30 retrieval threshold and the 1.05 conflict
threshold, so similarity is fully controlled by the test text.
"""

import threading

import pytest

from cora import user_notes
from cora.knowledge_base import embeddings
from cora.knowledge_base.store import (
    Document,
    KnowledgeBase,
    KnowledgeBaseError,
    USER_NOTE_SOURCE,
    _BIN_READY_KEY,
)

_DIM = 1536

OWNER = "U_OWNER_1"
OTHER = "U_OTHER_2"


def _unit_vec(axis: int = 0) -> list[float]:
    vec = [0.0] * _DIM
    vec[axis % _DIM] = 1.0
    return vec


def _vec_for(text: str) -> list[float]:
    return _unit_vec(1 if "alpha" in text.lower() else 0)


@pytest.fixture(autouse=True)
def patch_embeddings(monkeypatch):
    monkeypatch.setattr(embeddings, "embed_texts", lambda texts: [_vec_for(t) for t in texts])
    monkeypatch.setattr(embeddings, "embed_query", _vec_for)


@pytest.fixture
def kb(tmp_path):
    db = KnowledgeBase(tmp_path / "notes_kb.db", check_same_thread=False)
    yield db
    db.close()


def _save(kb, text="the wifi password is alpha123", owner=OWNER, entity="F3E", **kw):
    return user_notes.save_note(
        kb,
        note_text=text,
        owner_slack=owner,
        owner_email=f"{owner.lower()}@hjrglobal.com",
        entity=entity,
        **kw,
    )


def _seed_canonical(kb, content="alpha canonical fact about the wifi", entity="F3E"):
    kb.upsert_documents([
        Document(
            source="static_md", source_id="canon-1", entity=entity,
            content=content, title="Canon Doc",
        )
    ])


# ──────────────────────────────────────────────────────────────────────────────
# SQL-layer owner exclusion — the load-bearing invariant
# ──────────────────────────────────────────────────────────────────────────────

class TestOwnerExclusionSQL:
    def test_general_search_never_returns_notes_float_path(self, kb):
        _save(kb)
        results = kb.search("the wifi password is alpha123", entity="F3E", k=10)
        assert all(r.source != USER_NOTE_SOURCE for r in results)
        assert results == []

    def test_general_search_never_returns_notes_binary_path(self, kb):
        _save(kb)
        kb.set_checkpoint(_BIN_READY_KEY, {"ready": True})
        assert kb._is_bin_index_ready() is True
        results = kb.search("the wifi password is alpha123", entity="F3E", k=10)
        assert all(r.source != USER_NOTE_SOURCE for r in results)
        assert results == []

    def test_other_user_identical_query_gets_nothing(self, kb):
        # Adversarial: identical query text -> identical embedding vector.
        _save(kb)
        assert kb.search_user_notes(
            "the wifi password is alpha123", owner_slack=OTHER
        ) == []

    def test_owner_retrieves_own_note(self, kb):
        _save(kb)
        results = kb.search_user_notes("the wifi password is alpha123", owner_slack=OWNER)
        assert len(results) == 1
        assert results[0].source == USER_NOTE_SOURCE
        assert results[0].metadata["owner_slack"] == OWNER

    def test_empty_owner_fails_closed(self, kb):
        _save(kb)
        assert kb.search_user_notes("alpha", owner_slack="") == []

    def test_search_owned_refuses_user_note_source(self, kb):
        with pytest.raises(KnowledgeBaseError):
            kb.search_owned("alpha", owner_emails=frozenset({"a@b.com"}),
                            sources=(USER_NOTE_SOURCE,))

    def test_canonical_chunks_unaffected_by_exclusion(self, kb):
        _seed_canonical(kb)
        results = kb.search("alpha wifi", entity="F3E", k=10)
        assert len(results) == 1
        assert results[0].source == "static_md"


class TestHarrisonOverride:
    def test_unrestricted_retrieves_another_users_note(self, kb):
        _save(kb, owner=OTHER)
        results = kb.search_user_notes(
            "the wifi password is alpha123", owner_slack="U_HARRISON",
            unrestricted=True,
        )
        assert len(results) == 1
        assert results[0].metadata["owner_slack"] == OTHER

    def test_override_notes_labeled_as_not_the_askers(self, kb):
        _save(kb, owner=OTHER)
        results = kb.search_user_notes(
            "alpha", owner_slack="U_HARRISON", unrestricted=True,
        )
        block = user_notes.format_notes_overlay(results, "U_HARRISON")
        assert "founder override" in block
        assert f"<@{OTHER}>" in block


# ──────────────────────────────────────────────────────────────────────────────
# Entity scoping (channel scope / LEX containment / DM)
# ──────────────────────────────────────────────────────────────────────────────

class TestEntityScope:
    def test_lex_note_excluded_from_f3e_channel_scope(self, kb):
        _save(kb, entity="LEX-LLC")
        assert kb.search_user_notes(
            "alpha", owner_slack=OWNER, entity_scope=("F3E", "FNDR"),
        ) == []

    def test_lex_note_visible_in_its_own_scope(self, kb):
        _save(kb, entity="LEX-LLC")
        results = kb.search_user_notes(
            "alpha", owner_slack=OWNER, entity_scope=("LEX-LLC",),
        )
        assert len(results) == 1

    def test_dm_scope_none_sees_all_owned_notes(self, kb):
        _save(kb, entity="LEX-LLC")
        _save(kb, text="alpha second note", entity="F3E")
        results = kb.search_user_notes("alpha", owner_slack=OWNER, entity_scope=None)
        assert len(results) == 2

    def test_empty_scope_tuple_fails_closed(self, kb):
        _save(kb)
        assert kb.search_user_notes("alpha", owner_slack=OWNER, entity_scope=()) == []


# ──────────────────────────────────────────────────────────────────────────────
# List / delete — owner-only management
# ──────────────────────────────────────────────────────────────────────────────

class TestListDelete:
    def test_list_returns_only_own_notes(self, kb):
        _save(kb, owner=OWNER)
        _save(kb, owner=OTHER, text="other user's alpha secret")
        notes = kb.list_user_notes(OWNER)
        assert len(notes) == 1
        assert notes[0]["metadata"]["owner_slack"] == OWNER

    def test_delete_other_users_note_is_noop(self, kb):
        note_id = _save(kb, owner=OWNER)
        assert kb.delete_user_note(note_id, owner_slack=OTHER) == 0
        assert len(kb.list_user_notes(OWNER)) == 1

    def test_delete_own_note_removes_chunks_and_vectors(self, kb):
        note_id = _save(kb, owner=OWNER)
        before = kb._conn.execute("SELECT COUNT(*) FROM knowledge_vec_f32").fetchone()[0]
        deleted = kb.delete_user_note(note_id, owner_slack=OWNER)
        assert deleted >= 1
        assert kb.list_user_notes(OWNER) == []
        after = kb._conn.execute("SELECT COUNT(*) FROM knowledge_vec_f32").fetchone()[0]
        assert after == before - deleted
        assert kb.search_user_notes("alpha", owner_slack=OWNER) == []


# ──────────────────────────────────────────────────────────────────────────────
# PHI save-decision matrix
# ──────────────────────────────────────────────────────────────────────────────

class TestSaveScopePHI:
    @pytest.fixture
    def phi_text(self, monkeypatch):
        monkeypatch.setattr(user_notes, "is_phi_risk", lambda t: "PHI" in t)

    def test_non_phi_saves_under_channel_entity(self, phi_text):
        d = user_notes.resolve_save_scope("plain fact", "F3E", OWNER, is_dm=False)
        assert d.allowed and d.entity == "F3E" and d.sub_entity is None

    def test_lex_sub_channel_keeps_sub_entity(self, phi_text):
        d = user_notes.resolve_save_scope("plain fact", "LEX-LLC", OWNER, is_dm=False)
        assert d.allowed and d.entity == "LEX-LLC" and d.sub_entity == "LEX-LLC"

    def test_phi_non_custodian_refused_everywhere(self, phi_text, monkeypatch):
        monkeypatch.setattr(user_notes.lex_phi_access, "phi_allowed",
                            lambda *a, **k: False)
        for entity, is_dm in (("F3E", False), ("LEX-LLC", False), ("FNDR", True)):
            d = user_notes.resolve_save_scope("PHI client detail", entity, OWNER, is_dm)
            assert not d.allowed
            assert d.reason == user_notes.PHI_REFUSAL

    def test_phi_custodian_in_lex_scope_allowed(self, phi_text, monkeypatch):
        monkeypatch.setattr(user_notes.lex_phi_access, "phi_allowed",
                            lambda *a, **k: True)
        d = user_notes.resolve_save_scope("PHI client detail", "LEX-LLC", OWNER, is_dm=False)
        assert d.allowed and d.entity == "LEX-LLC" and d.sub_entity == "LEX-LLC"

    def test_phi_custodian_dm_forced_into_lex_scope(self, phi_text, monkeypatch):
        monkeypatch.setattr(user_notes.lex_phi_access, "phi_allowed",
                            lambda *a, **k: True)
        d = user_notes.resolve_save_scope("PHI client detail", "FNDR", OWNER, is_dm=True)
        assert d.allowed and d.entity == "LEX" and d.sub_entity is None

    def test_default_entity_when_channel_unknown(self, phi_text):
        d = user_notes.resolve_save_scope("plain fact", "", OWNER, is_dm=True)
        assert d.allowed and d.entity == "FNDR"


# ──────────────────────────────────────────────────────────────────────────────
# Save-time conflict check
# ──────────────────────────────────────────────────────────────────────────────

class TestConflictCheck:
    def test_high_similarity_canonical_chunk_flagged(self, kb):
        _seed_canonical(kb, content="alpha canonical fact about the wifi")
        excerpt = user_notes.conflict_excerpt(kb, "alpha note that disagrees", "F3E")
        assert excerpt is not None
        assert "alpha canonical fact" in excerpt

    def test_unrelated_canon_not_flagged(self, kb):
        _seed_canonical(kb, content="alpha canonical fact")
        # Note embeds on axis 0 (no "alpha") -> distance sqrt(2) > 1.05.
        assert user_notes.conflict_excerpt(kb, "unrelated topic", "F3E") is None

    def test_conflict_probe_failure_never_blocks(self):
        class Boom:
            def search(self, *a, **k):
                raise RuntimeError("kb down")
        assert user_notes.conflict_excerpt(Boom(), "alpha", "F3E") is None

    def test_conflict_probe_never_matches_other_notes(self, kb):
        # Another user's note on the same topic must not surface in the
        # conflict excerpt (search() excludes user_note chunks).
        _save(kb, owner=OTHER, text="alpha other user's private note")
        assert user_notes.conflict_excerpt(kb, "alpha new note", "F3E") is None


# ──────────────────────────────────────────────────────────────────────────────
# Overlay formatting + context_loader retrieval + cache-skip
# ──────────────────────────────────────────────────────────────────────────────

class TestOverlayFormatting:
    def test_owner_note_labeled_personal_not_canon(self, kb):
        _save(kb)
        results = kb.search_user_notes("alpha", owner_slack=OWNER)
        block = user_notes.format_notes_overlay(results, OWNER)
        assert "ASKER'S PERSONAL NOTE from" in block
        assert "not org-canon" in block
        assert "the wifi password is alpha123" in block

    def test_empty_results_render_nothing(self):
        assert user_notes.format_notes_overlay([], OWNER) == ""


class TestContextLoaderOverlay:
    @pytest.fixture
    def ctx(self, kb, monkeypatch, tmp_path):
        from cora import context_loader as ctx_mod
        monkeypatch.setattr(ctx_mod, "get_shared_kb", lambda: kb)
        monkeypatch.setattr(ctx_mod, "_KB_DB_PATH", kb.db_path)
        return ctx_mod

    def test_overlay_fires_even_without_canonical_hits(self, ctx, kb):
        _save(kb)  # no canonical chunks at all
        kb_meta: dict = {}
        block = ctx._try_kb_retrieve(
            "F3E", "the wifi password is alpha123",
            asker_slack_id=OWNER, kb_meta=kb_meta,
        )
        assert block is not None
        assert "ASKER'S PERSONAL NOTE" in block

    def test_note_response_sets_cache_skip_flag(self, ctx, kb):
        _save(kb)
        kb_meta: dict = {}
        ctx._try_kb_retrieve(
            "F3E", "alpha wifi password",
            asker_slack_id=OWNER, kb_meta=kb_meta,
        )
        assert kb_meta.get("unstripped_personal") is True

    def test_other_asker_same_query_no_note_no_flag(self, ctx, kb):
        _save(kb)
        kb_meta: dict = {}
        block = ctx._try_kb_retrieve(
            "F3E", "the wifi password is alpha123",
            asker_slack_id=OTHER, kb_meta=kb_meta,
        )
        assert block is None
        assert "unstripped_personal" not in kb_meta

    def test_lex_note_never_surfaces_in_f3e_channel(self, ctx, kb):
        _save(kb, entity="LEX-LLC")
        block = ctx._try_kb_retrieve(
            "F3E", "alpha wifi", asker_slack_id=OWNER, kb_meta={},
        )
        assert block is None

    def test_dm_sees_lex_note(self, ctx, kb):
        _save(kb, entity="LEX-LLC")
        block = ctx._try_kb_retrieve(
            "FNDR", "alpha wifi", asker_slack_id=OWNER, asker_is_dm=True, kb_meta={},
        )
        assert block is not None and "ASKER'S PERSONAL NOTE" in block

    def test_no_asker_id_no_overlay(self, ctx, kb):
        _save(kb)
        block = ctx._try_kb_retrieve("F3E", "alpha wifi", kb_meta={})
        assert block is None

    def test_canonical_and_note_compose(self, ctx, kb):
        _seed_canonical(kb)
        _save(kb)
        block = ctx._try_kb_retrieve(
            "F3E", "alpha wifi", asker_slack_id=OWNER, kb_meta={},
        )
        assert "Retrieved knowledge" in block
        assert "ASKER'S PERSONAL NOTE" in block


# ──────────────────────────────────────────────────────────────────────────────
# Tool layer — staged gates, PHI refusal, conflict append, owner-only mgmt
# ──────────────────────────────────────────────────────────────────────────────

class TestTools:
    @pytest.fixture
    def td(self, kb, monkeypatch):
        from cora.tools import tool_dispatch as td_mod
        monkeypatch.setattr(td_mod, "_notes_kb", lambda: (kb, threading.Lock()))
        return td_mod

    def test_remember_refuses_without_confirmed(self, td, kb):
        out = td._tool_cora_remember(OWNER, "F3E", {"note_text": "alpha fact"})
        assert "refused" in out and "preview" in out
        assert kb.list_user_notes(OWNER) == []

    def test_remember_saves_and_owner_only(self, td, kb):
        out = td._tool_cora_remember(
            OWNER, "F3E",
            {"note_text": "the wifi password is alpha123", "confirmed": True,
             "_channel_id": "C123"},
        )
        assert "WRITE_CONFIRMED" in out
        assert "only you can retrieve" in out.lower()
        assert len(kb.search_user_notes("alpha wifi", owner_slack=OWNER)) == 1
        assert kb.search_user_notes("alpha wifi", owner_slack=OTHER) == []
        assert kb.search("the wifi password is alpha123", entity="F3E") == []

    def test_remember_share_requested_mentions_harrison_review(self, td, kb):
        out = td._tool_cora_remember(
            OWNER, "F3E",
            {"note_text": "alpha team process", "confirmed": True,
             "share_requested": True, "_channel_id": "C123"},
        )
        assert "Harrison" in out and "review" in out
        notes = kb.list_user_notes(OWNER)
        assert notes[0]["metadata"]["share_requested"] is True

    def test_remember_appends_conflict_heads_up(self, td, kb):
        _seed_canonical(kb, content="alpha canonical wifi fact")
        out = td._tool_cora_remember(
            OWNER, "F3E",
            {"note_text": "alpha wifi note that contradicts canon", "confirmed": True,
             "_channel_id": "C123"},
        )
        assert "may conflict" in out
        assert "alpha canonical wifi fact" in out
        # The save still happened (conflict never blocks).
        assert len(kb.list_user_notes(OWNER)) == 1

    def test_remember_phi_refusal(self, td, kb, monkeypatch):
        monkeypatch.setattr(user_notes, "is_phi_risk", lambda t: True)
        monkeypatch.setattr(user_notes.lex_phi_access, "phi_allowed",
                            lambda *a, **k: False)
        out = td._tool_cora_remember(
            OTHER, "F3E",
            {"note_text": "client diagnosis detail", "confirmed": True,
             "_channel_id": "C123"},
        )
        assert out == user_notes.PHI_REFUSAL
        assert kb.list_user_notes(OTHER) == []

    def test_remember_empty_text_rejected(self, td, kb):
        out = td._tool_cora_remember(OWNER, "F3E", {"note_text": "  ", "confirmed": True})
        assert "required" in out
        assert kb.list_user_notes(OWNER) == []

    def test_my_notes_lists_only_own(self, td, kb):
        _save(kb, owner=OWNER)
        _save(kb, owner=OTHER, text="other alpha secret")
        out = td._tool_cora_my_notes(OWNER, "F3E", {})
        assert "1." in out
        assert "other alpha secret" not in out

    def test_my_notes_empty_state(self, td, kb):
        out = td._tool_cora_my_notes(OWNER, "F3E", {})
        assert "no saved personal notes" in out.lower()

    def test_forget_refuses_without_confirmed(self, td, kb):
        note_id = _save(kb)
        out = td._tool_cora_forget_note(OWNER, "F3E", {"note_id": note_id})
        assert "refused" in out
        assert len(kb.list_user_notes(OWNER)) == 1

    def test_forget_short_id_owner_only(self, td, kb):
        note_id = _save(kb)
        short = note_id.rsplit(":", 1)[-1]
        # Another user cannot delete it via the short id (resolved against
        # THEIR notes) or the full id (SQL owner check).
        out_other = td._tool_cora_forget_note(OTHER, "F3E",
                                              {"note_id": short, "confirmed": True})
        assert "nothing was deleted" in out_other
        out_other_full = td._tool_cora_forget_note(OTHER, "F3E",
                                                   {"note_id": note_id, "confirmed": True})
        assert "nothing was deleted" in out_other_full
        assert len(kb.list_user_notes(OWNER)) == 1
        # Owner can.
        out_owner = td._tool_cora_forget_note(OWNER, "F3E",
                                              {"note_id": short, "confirmed": True})
        assert "WRITE_CONFIRMED" in out_owner
        assert kb.list_user_notes(OWNER) == []


# ──────────────────────────────────────────────────────────────────────────────
# The "remember Harrison approved my raise" pin (spec-mandated)
# ──────────────────────────────────────────────────────────────────────────────

class TestRaisePin:
    def test_raise_note_saves_but_never_surfaces_to_anyone_else(self, kb):
        text = "alpha: Harrison approved my raise"
        _save(kb, owner="U_TOMMY", text=text, entity="F3E")
        # Saves fine.
        assert len(kb.list_user_notes("U_TOMMY")) == 1
        # Never via general search (any entity, any asker).
        for entity in ("F3E", "FNDR", "HJRG"):
            results = kb.search(text, entity=entity, k=10, include_fndr=True)
            assert all(r.source != USER_NOTE_SOURCE for r in results)
        # Never via another user's identical owner-scoped query.
        assert kb.search_user_notes(text, owner_slack="U_MATT") == []
        # For the owner, it is labeled personal — never org-fact.
        results = kb.search_user_notes(text, owner_slack="U_TOMMY")
        block = user_notes.format_notes_overlay(results, "U_TOMMY")
        assert "not org-canon" in block


# ──────────────────────────────────────────────────────────────────────────────
# Wiring — exposure, functions, timeouts, prompt coverage
# ──────────────────────────────────────────────────────────────────────────────

class TestWiring:
    def test_tool_definitions_present(self):
        from cora.tools import tool_dispatch as td
        names = {t["name"] for t in td.TOOL_DEFINITIONS}
        assert {"cora_remember", "cora_my_notes", "cora_forget_note"} <= names

    def test_tool_functions_wired(self):
        from cora.tools import tool_dispatch as td
        for name in ("cora_remember", "cora_my_notes", "cora_forget_note"):
            assert name in td._TOOL_FUNCTIONS

    def test_global_core_exposure(self):
        from cora.tools import tool_dispatch as td
        for name in ("cora_remember", "cora_my_notes", "cora_forget_note"):
            assert name in td._GLOBAL_CORE_TOOLS
        # Every entity (incl. lean + sub-entity scopes) gets the note tools.
        for entity in ("F3E", "OSN", "LEX-LLC", "F3C", "HJRPROD", "FNDR"):
            offered = {t["name"] for t in td.tools_for_entity(entity)}
            assert "cora_remember" in offered

    def test_timeout_tiers(self):
        from cora.tools import tool_dispatch as td
        assert td._TOOL_TIMEOUTS["cora_remember"] == 15
        assert td._TOOL_TIMEOUTS["cora_my_notes"] == 8
        assert td._TOOL_TIMEOUTS["cora_forget_note"] == 8

    def test_remember_requires_confirmed_in_schema(self):
        from cora.tools import tool_dispatch as td
        defn = next(t for t in td.TOOL_DEFINITIONS if t["name"] == "cora_remember")
        assert "confirmed" in defn["input_schema"]["required"]
        assert "Saving to YOUR notes" in defn["description"]

    def test_all_17_prompts_carry_personal_notes_section(self):
        from pathlib import Path
        prompts_dir = Path(__file__).resolve().parents[1] / "design" / "system-prompts"
        files = sorted(prompts_dir.glob("*.md"))
        assert len(files) == 17
        for f in files:
            text = f.read_text(encoding="utf-8")
            assert "## Personal notes" in text, f"{f.name} missing personal-notes section"
            assert "org-wide sharing needs Harrison's review" in text, f.name
