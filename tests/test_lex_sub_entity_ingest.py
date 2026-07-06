"""Tests for ingest-time LEX sub-entity tagging (Part 2 of the 5/23 siloing fix).

The shared detection module (cora.knowledge_base.lex_sub_entity) is wired into
KnowledgeBase.upsert_documents so every connector's LEX docs get tagged at the
choke point. Invariants:

  - LEX doc with UNAMBIGUOUS sub-entity signal + sub_entity=None -> tagged at insert
  - LEX doc with an EXPLICIT sub_entity -> never overridden by detection
  - LEX doc with general content -> stays NULL (GM-level by design)
  - LEX doc matching 2+ sub-entities -> ambiguous, stays NULL
  - Non-LEX docs -> never touched, even if text contains LEX keywords

Companion files: tests/test_backfill_lex_sub_entity.py (detection cases),
tests/test_kb_store.py (store behavior), tests/test_lex_sub_entity_tagging.py
(retrieval-side strict filter).
"""

import pytest

from cora.knowledge_base import embeddings
from cora.knowledge_base.lex_sub_entity import detect_sub_entity
from cora.knowledge_base.store import Document, KnowledgeBase

_DIM = 1536


def _unit_vec() -> list:
    vec = [0.0] * _DIM
    vec[0] = 1.0
    return vec


def _embed_texts_mock(texts):
    return [_unit_vec() for _ in texts]


def _embed_query_mock(query):
    return _unit_vec()


@pytest.fixture(autouse=True)
def patch_embeddings(monkeypatch):
    monkeypatch.setattr(embeddings, "embed_texts", _embed_texts_mock)
    monkeypatch.setattr(embeddings, "embed_query", _embed_query_mock)


@pytest.fixture
def kb(tmp_path):
    db = KnowledgeBase(tmp_path / "test_kb.db")
    yield db
    db.close()


def _doc(**overrides) -> Document:
    defaults = dict(
        source="gmail",
        source_id="msg-001",
        entity="LEX",
        content="General Lexington payroll summary for May 2026.",
        title="Payroll summary",
    )
    defaults.update(overrides)
    return Document(**defaults)


def _stored_sub_entities(kb: KnowledgeBase, source_id: str) -> set:
    cur = kb._conn.cursor()
    cur.execute(
        "SELECT DISTINCT sub_entity FROM knowledge_chunks WHERE source_id = ?",
        (source_id,),
    )
    return {row[0] for row in cur.fetchall()}


class TestIngestTimeTagging:
    def test_unambiguous_llc_doc_tagged(self, kb):
        kb.upsert_documents([_doc(
            source_id="msg-llc",
            title="HCBS billing report Q1",
            content="Supported Living placements and HCBS claims for the quarter.",
        )])
        assert _stored_sub_entities(kb, "msg-llc") == {"LEX-LLC"}

    def test_unambiguous_lts_doc_tagged(self, kb):
        # source=asana isolates the TAGGING behavior — a gmail/drive_sweep LTS doc is
        # dropped at ingest by the W6-01 deny-list (see TestW601RestrictedIngestDrop).
        kb.upsert_documents([_doc(
            source="asana",
            source_id="msg-lts",
            title="Provider Type 15 deadline",
            content="DDD Therapy Revalidation paperwork is due June 30.",
        )])
        assert _stored_sub_entities(kb, "msg-lts") == {"LEX-LTS"}

    def test_unambiguous_lbhs_doc_tagged(self, kb):
        # source=asana: gmail/drive_sweep LBHS docs are dropped by W6-01 (tested below);
        # this asserts the tagging chokepoint independent of the ingest deny-list.
        kb.upsert_documents([_doc(
            source="asana",
            source_id="msg-lbhs",
            title="LBHS Q2 census numbers",
            content="Census report attached.",
        )])
        assert _stored_sub_entities(kb, "msg-lbhs") == {"LEX-LBHS"}

    def test_unambiguous_lla_doc_tagged(self, kb):
        kb.upsert_documents([_doc(
            source_id="msg-lla",
            title="Lex Life Academy enrollment",
            content="Maryvale site enrollment numbers.",
        )])
        assert _stored_sub_entities(kb, "msg-lla") == {"LEX-LLA"}

    def test_general_lex_doc_stays_null(self, kb):
        kb.upsert_documents([_doc(
            source_id="msg-general",
            title="Staff training slides Q2",
            content="Training material for all Lexington staff.",
        )])
        assert _stored_sub_entities(kb, "msg-general") == {None}

    def test_ambiguous_doc_stays_null(self, kb):
        kb.upsert_documents([_doc(
            source_id="msg-ambig",
            title="[LEX-LLC] LBHS billing overlap",
            content="Crossover items between the two entities.",
        )])
        assert _stored_sub_entities(kb, "msg-ambig") == {None}

    def test_explicit_sub_entity_never_overridden(self, kb):
        # Content looks like LLC, but the connector explicitly tagged LBHS.
        # source=asana so the W6-01 gmail/drive drop doesn't remove it.
        kb.upsert_documents([_doc(
            source="asana",
            source_id="msg-explicit",
            sub_entity="LEX-LBHS",
            title="HCBS billing report",
            content="Supported Living claims.",
        )])
        assert _stored_sub_entities(kb, "msg-explicit") == {"LEX-LBHS"}

    def test_non_lex_entity_untouched(self, kb):
        kb.upsert_documents([_doc(
            source_id="msg-osn",
            entity="OSN",
            title="HCBS mention in a non-LEX doc",
            content="Sandy Patel stopped by the Gilbert store.",
        )])
        assert _stored_sub_entities(kb, "msg-osn") == {None}

    def test_fndr_entity_untouched(self, kb):
        kb.upsert_documents([_doc(
            source_id="msg-fndr",
            entity="FNDR",
            title="Portfolio note mentioning LBHS",
            content="LBHS COPA diligence continues.",
        )])
        assert _stored_sub_entities(kb, "msg-fndr") == {None}

    def test_tagged_chunk_retrievable_in_sub_entity_scope(self, kb):
        """End-to-end: an auto-tagged chunk passes the strict sub-entity filter."""
        kb.upsert_documents([_doc(
            source_id="msg-search",
            title="HCBS billing report Q1",
            content="Supported Living placements for the quarter.",
        )])
        results = kb.search("billing report", entity="LEX", sub_entity="LEX-LLC")
        assert any(r.source_id == "msg-search" for r in results)
        # And it must NOT surface in a sibling sub-entity scope.
        sibling = kb.search("billing report", entity="LEX", sub_entity="LEX-LBHS")
        assert not any(r.source_id == "msg-search" for r in sibling)


class TestSharedModuleParity:
    """The script aliases must point at the shared module (no drift)."""

    def test_script_aliases_are_shared_module(self):
        import sys
        from pathlib import Path
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
        import backfill_lex_sub_entity as script
        assert script._detect_sub_entity is detect_sub_entity

    def test_detection_unchanged_from_locked_5_31_behavior(self):
        # Spot checks mirroring the locked test cases
        assert detect_sub_entity("[LEX-LLC] Grow to 750 Members", "") == "LEX-LLC"
        assert detect_sub_entity("Sandy Patel membership repurchase 2023", "") == "LEX-LLA"
        assert detect_sub_entity("Lexington payroll report May 2026", "") is None
        assert detect_sub_entity("[LEX-LLC] LBHS billing overlap", "") is None


class TestW601RestrictedIngestDrop:
    """W6-01 (2026-07-05): a gmail/drive_sweep doc resolving to a restricted LEX
    sub-entity (LBHS/LTS) is DROPPED at ingest — no LBHS (42 CFR Part 2) / LTS
    (Provider-Type-15) PHI enters the KB via the non-Slack sweeps. GM-level LEX,
    LLC/LLA, and all non-gmail/drive sources are untouched."""

    # ── DROP direction: LBHS/LTS gmail/drive docs whose CONTENT is PHI (Fix-A / D-073) ──
    def test_gmail_lbhs_clinical_dropped(self, kb):
        kb.upsert_documents([_doc(
            source="gmail", source_id="g-lbhs-clin", title="LBHS intake note",
            content="Client was diagnosed with autism and started on risperidone.",
        )])
        assert _stored_sub_entities(kb, "g-lbhs-clin") == set()  # PHI -> dropped

    def test_drive_lts_clinical_dropped(self, kb):
        kb.upsert_documents([_doc(
            source="drive_sweep", source_id="d-lts-clin",
            title="Provider Type 15 revalidation",
            content="Client Marcus was diagnosed with ADHD this month.",
        )])
        assert _stored_sub_entities(kb, "d-lts-clin") == set()

    def test_gmail_lbhs_named_billing_dropped(self, kb):
        # Named individual + program billing = PHI -> dropped.
        kb.upsert_documents([_doc(
            source="gmail", source_id="g-lbhs-bill", title="LBHS",
            content="The client John Smith's BHRF service authorization is still pending.",
        )])
        assert _stored_sub_entities(kb, "g-lbhs-bill") == set()

    # ── KEEP direction: LBHS/LTS BUSINESS gmail/drive docs are RETAINED + tagged ──
    def test_gmail_lbhs_business_kept(self, kb):
        kb.upsert_documents([_doc(
            source="gmail", source_id="g-lbhs-biz", title="LBHS payroll",
            content="Payroll summary and PTO balances for LBHS staff in June.",
        )])
        assert _stored_sub_entities(kb, "g-lbhs-biz") == {"LEX-LBHS"}  # business kept + tagged

    def test_drive_lts_business_kept(self, kb):
        kb.upsert_documents([_doc(
            source="drive_sweep", source_id="d-lts-biz",
            title="Provider Type 15 revalidation deadline",
            content="Revalidation paperwork is due June 30 for Lexington Therapeutic.",
        )])
        assert _stored_sub_entities(kb, "d-lts-biz") == {"LEX-LTS"}

    def test_gmail_lbhs_aggregate_billing_kept(self, kb):
        # Aggregate 'client billing' with NO named individual = business -> KEPT.
        kb.upsert_documents([_doc(
            source="gmail", source_id="g-lbhs-agg", title="LBHS",
            content="BHRF client billing volume rose 12% this quarter.",
        )])
        assert _stored_sub_entities(kb, "g-lbhs-agg") == {"LEX-LBHS"}

    def test_gmail_llc_doc_kept(self, kb):
        # LLC is NOT restricted — a gmail LLC doc still ingests + tags normally.
        kb.upsert_documents([_doc(
            source="gmail", source_id="g-llc",
            title="HCBS billing report Q1",
            content="Supported Living placements and HCBS claims.",
        )])
        assert _stored_sub_entities(kb, "g-llc") == {"LEX-LLC"}

    def test_gmail_general_lex_doc_kept(self, kb):
        # GM-level (NULL) LEX content is kept — the drop is LBHS/LTS-scoped only.
        kb.upsert_documents([_doc(
            source="gmail", source_id="g-general",
            title="Staff training slides Q2",
            content="Training material for all Lexington staff.",
        )])
        assert _stored_sub_entities(kb, "g-general") == {None}

    def test_slack_lbhs_clinical_kept(self, kb):
        # Even CLINICAL LBHS content via a NON-gmail/drive source (a GM #lex-leadership
        # slack thread) is out of the ingest-drop scope — slack is denied upstream, and a
        # content-tagged GM chunk stays (subject to the W2-01 retrieval scrub).
        kb.upsert_documents([_doc(
            source="slack", source_id="s-lbhs",
            title="LBHS", content="Client was diagnosed with autism.",
        )])
        assert _stored_sub_entities(kb, "s-lbhs") == {"LEX-LBHS"}

    def test_mixed_batch_drops_only_phi(self, kb):
        # gmail LBHS clinical (drop) + gmail LBHS business (keep) + gmail LLC (keep) +
        # gmail general (keep): only the PHI doc is dropped.
        kb.upsert_documents([
            _doc(source="gmail", source_id="mix-phi", title="LBHS",
                 content="Client diagnosed with autism, on risperidone."),
            _doc(source="gmail", source_id="mix-biz", title="LBHS payroll",
                 content="LBHS staff PTO balances and payroll for June."),
            _doc(source="gmail", source_id="mix-llc", title="HCBS billing",
                 content="Supported Living HCBS claims"),
            _doc(source="gmail", source_id="mix-gen", title="Lexington all-staff memo",
                 content="general note"),
        ])
        assert _stored_sub_entities(kb, "mix-phi") == set()
        assert _stored_sub_entities(kb, "mix-biz") == {"LEX-LBHS"}
        assert _stored_sub_entities(kb, "mix-llc") == {"LEX-LLC"}
        assert _stored_sub_entities(kb, "mix-gen") == {None}

    def test_business_with_incidental_dx_term_kept(self, kb):
        # D-073 re-gate finding 1: a BUSINESS gmail LBHS doc that merely MENTIONS a diagnosis
        # term (in a school name / job title / fee schedule) with NO named individual is KEPT
        # -- the program cue is present by construction (LBHS tag) so it must NOT trigger the
        # drop on its own.
        kb.upsert_documents([_doc(
            source="gmail", source_id="g-recruit", title="LBHS careers form submission",
            content="Candidate work history includes ACHIEVE School for Autism; "
                    "applying for the Autism Behavioral Support Representative role.",
        )])
        assert _stored_sub_entities(kb, "g-recruit") == {"LEX-LBHS"}  # business kept + tagged

    def test_empty_staff_roster_defers_drop(self, kb, monkeypatch):
        # D-051 re-gate finding 2: if the org-roles staff roster is unavailable/empty, the
        # PHI-content drop can't exclude staff possessives, so it is DEFERRED for the batch
        # (business kept; W2-01 guards at retrieval) rather than over-dropping.
        import cora.knowledge_base.store as store_mod
        monkeypatch.setattr(store_mod, "_lex_staff_names", lambda: set())
        kb.upsert_documents([_doc(
            source="gmail", source_id="g-noroster", title="LBHS intake",
            content="Client was diagnosed with autism.",  # clinical, but roster unavailable
        )])
        assert _stored_sub_entities(kb, "g-noroster") == {"LEX-LBHS"}  # drop deferred -> kept

    # ── D-051 finding 5: a now-PHI RE-INGEST must purge the doc's STALE chunks ──
    def test_reingest_now_phi_purges_stale_all_dropped(self, kb):
        # First ingest is BUSINESS -> stored + tagged LEX-LBHS. Later edited to add clinical
        # PHI; on re-ingest the doc is dropped AND its old business chunk must be purged
        # (else it survives forever). All-dropped path.
        kb.upsert_documents([_doc(source="drive_sweep", source_id="reY",
                                  title="LBHS payroll",
                                  content="LBHS staff payroll and PTO for June.")])
        assert _stored_sub_entities(kb, "reY") == {"LEX-LBHS"}
        kb.upsert_documents([_doc(source="drive_sweep", source_id="reY",
                                  title="LBHS intake",
                                  content="Client diagnosed with autism, on risperidone.")])
        assert _stored_sub_entities(kb, "reY") == set()  # stale business chunk purged

    def test_reingest_now_phi_purges_stale_mixed_batch(self, kb):
        # Same, but the re-ingest batch ALSO carries a kept doc -> NORMAL delete pass
        # (not the all-dropped early return); exercises the seen_keys seeding.
        kb.upsert_documents([_doc(source="drive_sweep", source_id="reZ",
                                  title="LBHS payroll",
                                  content="LBHS payroll batch for June.")])
        assert _stored_sub_entities(kb, "reZ") == {"LEX-LBHS"}
        kb.upsert_documents([
            _doc(source="drive_sweep", source_id="reZ", title="LBHS intake",
                 content="Client Marcus diagnosed with ADHD."),        # now PHI -> dropped
            _doc(source="drive_sweep", source_id="keptW", title="ops",
                 content="Lexington general ops note"),                 # kept
        ])
        assert _stored_sub_entities(kb, "reZ") == set()        # stale business chunk purged
        assert _stored_sub_entities(kb, "keptW") == {None}     # co-batch kept doc ingested


class TestW601Predicate:
    """The shared scope gate (is_restricted_lex_ingest) + the PHI-content drop decision
    (restricted_lex_phi_content_drop) -- single source of truth for the ingest drop + purge."""

    def test_scope_gate_matrix(self):
        from cora.knowledge_base.lex_sub_entity import is_restricted_lex_ingest
        assert is_restricted_lex_ingest("gmail", "LEX-LBHS") is True
        assert is_restricted_lex_ingest("drive_sweep", "LEX-LTS") is True
        assert is_restricted_lex_ingest("gmail", "LEX-LLC") is False   # LLC not restricted
        assert is_restricted_lex_ingest("gmail", None) is False        # GM-level
        assert is_restricted_lex_ingest("slack", "LEX-LBHS") is False  # slack denied upstream
        assert is_restricted_lex_ingest("asana", "LEX-LTS") is False
        assert is_restricted_lex_ingest("static_md", "LEX-LBHS") is False

    def test_phi_content_drop_matrix(self):
        from cora.knowledge_base.lex_sub_entity import restricted_lex_phi_content_drop as drop
        # DROP: in-scope AND PHI content
        assert drop("gmail", "LEX-LBHS", "LBHS", "Client diagnosed with autism.") is True
        assert drop("drive_sweep", "LEX-LTS", "PT15",
                    "the client John Smith's BHRF authorization is pending.") is True
        # bare bookkeeper/vendor possessive + billing + program = BUSINESS -> KEPT (re-gate F4)
        assert drop("gmail", "LEX-LBHS", "Rita Tracking",
                    "Rita Hill's Lexington Medicaid billing reconciliation sheet.") is False
        assert drop("drive_sweep", "LEX-LTS", "P&L",
                    "Lowe's invoice line items for the BHRF facility this quarter.") is False
        # KEEP: in-scope but BUSINESS content
        assert drop("gmail", "LEX-LBHS", "LBHS payroll",
                    "LBHS staff PTO and payroll for June.") is False
        assert drop("gmail", "LEX-LBHS", "LBHS",
                    "BHRF client billing volume rose this quarter.") is False  # aggregate, no individual
        # KEEP: out of scope even if clinical (scope gate)
        assert drop("gmail", "LEX-LLC", "x", "Client diagnosed with autism.") is False
        assert drop("slack", "LEX-LBHS", "x", "Client diagnosed with autism.") is False
        assert drop("gmail", None, "x", "Client diagnosed with autism.") is False
