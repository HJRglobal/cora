"""WS17-B item 6/7: the entity -> known-answers map is single-sourced, and every
entity gap_autofill can WRITE resolves to a file context_loader will READ (the
bug that wrote HJRP/UFL/F3C/HJRPROD answers to files Cora never read)."""

import cora.known_answers_map as m
import cora.context_loader as cl
import cora.gap_autofill as g


def test_write_side_uses_the_canonical_map():
    assert g._ENTITY_FILES is m.ENTITY_FILES


def test_every_nonlex_write_entity_is_readable_to_same_file():
    for entity, filename in m.ENTITY_FILES.items():
        if entity.startswith("LEX-"):
            continue
        assert entity in cl._KNOWN_ANSWERS_PATHS, f"{entity} is written but never read"
        assert cl._KNOWN_ANSWERS_PATHS[entity].name == filename


def test_named_bug_entities_now_readable():
    """HJRP/UFL/F3C/HJRPROD answers were silently written to unread files."""
    for e in ("HJRP", "UFL", "F3C", "HJRPROD"):
        assert e in cl._KNOWN_ANSWERS_PATHS


def test_lex_subentities_share_parent_file_and_are_not_in_read_map():
    # Sub-entity answers all land in lex.md (GM-readable at LEX) and are
    # deliberately NOT surfaced inside the sub-entity channel.
    for sub in ("LEX-LLC", "LEX-LLA", "LEX-LBHS", "LEX-LTS"):
        assert m.ENTITY_FILES[sub] == "lex.md"
        assert sub not in cl._KNOWN_ANSWERS_PATHS
    assert cl._KNOWN_ANSWERS_PATHS["LEX"].name == "lex.md"


def test_read_map_has_no_orphans():
    for entity in cl._KNOWN_ANSWERS_PATHS:
        assert entity in m.ENTITY_FILES, f"{entity} is read but no writer targets it"


def test_file_for_default():
    assert m.file_for("NOPE") == m.DEFAULT_FILE
    assert m.file_for("f3e") == "f3e.md"   # case-insensitive
