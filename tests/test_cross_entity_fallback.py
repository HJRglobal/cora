"""WS4: cross-entity vendor/contact fallback (the genuine Minute Press fix).

When an entity-scoped KB search is empty AND the asker has cross-entity authority
(founder, or a founder/holdco channel), Cora searches the wider portfolio and
returns a CONFIDENCE-LABELED result instead of a confident "no record" -- but
NEVER surfaces a LEX-store contact to a non-custodian.
"""

from unittest.mock import patch

from cora import context_loader as cl
from cora.knowledge_base.store import SearchResult


def _sr(entity, content, distance=0.5, chunk_id="c1", title="t"):
    return SearchResult(
        chunk_id=chunk_id, source="fireflies", source_id="s", entity=entity,
        title=title, content=content, deep_link="", date_modified=None,
        distance=distance, author="", metadata=None,
    )


class FakeKB:
    def __init__(self, by_entity):
        self.by_entity = by_entity
        self.searched: list[str] = []

    def search(self, query, entity, k=8, max_age_days=None, include_fndr=True,
               query_vec=None, sub_entity=None):
        self.searched.append(entity)
        return list(self.by_entity.get(entity, []))


_VEC = [0.0] * 4


def test_fallback_disabled_for_non_authorized_asker():
    fake = FakeKB({"OSN": [_sr("OSN", "Minute Press printer")]})
    with patch.object(cl, "get_shared_kb", return_value=fake):
        out = cl._try_cross_entity_fallback("printer vendor", _VEC, "F3E", frozenset(), False, False)
    assert out is None
    assert fake.searched == []  # never even searched


def test_fallback_founder_finds_cross_entity_vendor():
    fake = FakeKB({"OSN": [_sr("OSN", "Minute Press is our printer; contact Joe")]})
    with patch.object(cl, "get_shared_kb", return_value=fake):
        out = cl._try_cross_entity_fallback("who is the printer vendor", _VEC, "F3E", frozenset(), True, False)
    assert out is not None
    assert "Minute Press" in out
    assert "wider portfolio" in out.lower()
    assert "F3E" not in fake.searched   # channel entity excluded (already searched)
    assert "LEX" not in fake.searched   # LEX excluded for a non-custodian


def test_fallback_excludes_lex_for_non_custodian():
    fake = FakeKB({"LEX": [_sr("LEX", "client contact")]})
    with patch.object(cl, "get_shared_kb", return_value=fake):
        out = cl._try_cross_entity_fallback("contact", _VEC, "F3E", frozenset(), True, False)
    assert out is None
    assert "LEX" not in fake.searched


def test_fallback_includes_lex_for_custodian():
    fake = FakeKB({"LEX": [_sr("LEX", "Lexington vendor X")]})
    with patch.object(cl, "get_shared_kb", return_value=fake):
        out = cl._try_cross_entity_fallback("vendor", _VEC, "F3E", frozenset(), True, True)
    assert out is not None
    assert "Lexington vendor X" in out
    assert "LEX" in fake.searched


def test_fallback_none_when_nothing_found():
    fake = FakeKB({})
    with patch.object(cl, "get_shared_kb", return_value=fake):
        out = cl._try_cross_entity_fallback("nonexistent thing", _VEC, "F3E", frozenset(), True, False)
    assert out is None


def test_fallback_founder_channel_authority_without_unrestricted():
    # A FNDR-channel asker who is not "unrestricted" still gets the fallback
    # (founder/holdco channels are cross-cutting by design).
    fake = FakeKB({"OSN": [_sr("OSN", "Minute Press printer")]})
    with patch.object(cl, "get_shared_kb", return_value=fake):
        out = cl._try_cross_entity_fallback("printer", _VEC, "FNDR", frozenset(), False, False)
    assert out is not None
    assert "FNDR" not in fake.searched   # the channel's own entity is excluded


def test_fallback_respects_distance_threshold():
    far = _sr("OSN", "barely related", distance=cl._KB_MAX_DISTANCE + 0.5)
    fake = FakeKB({"OSN": [far]})
    with patch.object(cl, "get_shared_kb", return_value=fake):
        out = cl._try_cross_entity_fallback("printer", _VEC, "F3E", frozenset(), True, False)
    assert out is None


class _MisTagKB:
    """search() returns a chunk tagged entity='LEX' regardless of the entity asked
    -- simulates a mis-tagged chunk slipping past entity-column filtering, the exact
    input the WS4 belt-and-suspenders (layer-2) filter exists to stop."""

    def __init__(self):
        self.searched: list[str] = []

    def search(self, query, entity, k=8, max_age_days=None, include_fndr=True,
               query_vec=None, sub_entity=None):
        self.searched.append(entity)
        return [_sr("LEX", "client PHI contact", chunk_id="mis")]


def test_fallback_layer2_drops_mistagged_lex_for_non_custodian():
    # Review fix #8: even if a LEX-tagged chunk is returned under a non-LEX entity
    # search, the layer-2 filter must drop it for a non-custodian.
    fake = _MisTagKB()
    with patch.object(cl, "get_shared_kb", return_value=fake):
        out = cl._try_cross_entity_fallback("contact", _VEC, "F3E", frozenset(), True, False)
    assert out is None


def test_fallback_layer2_keeps_mistagged_lex_for_custodian():
    fake = _MisTagKB()
    with patch.object(cl, "get_shared_kb", return_value=fake):
        out = cl._try_cross_entity_fallback("contact", _VEC, "F3E", frozenset(), True, True)
    assert out is not None and "client PHI contact" in out
