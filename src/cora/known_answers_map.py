"""Single source of truth: entity code -> known-answers filename.

WS17-B item 6/7. Historically THREE copies of this map drifted:
  - gap_autofill._ENTITY_FILES        (the WRITE side — where a gap answer lands)
  - context_loader._KNOWN_ANSWERS_PATHS (the READ side — what Cora loads per entity)
  - scripts/ingest_digest_answers.ENTITY_FILES (the legacy manual-digest write side)

When the read map was missing HJRP/UFL/F3C/HJRPROD, gap answers for those entities
were written to files Cora never read back — silent non-learning. This module is
the one map all three import, so they can't diverge again. A test asserts the read
and write sides stay reconcilable.

LEX sub-entities (LEX-LLC/LLA/LBHS/LTS) all share lex.md on the WRITE side. The
READ side deliberately surfaces lex.md only at the LEX (GM) level, NOT inside each
sub-entity channel, to avoid one sub-entity's answer surfacing in a sibling's
channel. context_loader builds its read map from ENTITY_FILES excluding the
``LEX-`` keys for exactly this reason.
"""

from __future__ import annotations

# entity code -> filename under design/known-answers/
ENTITY_FILES: dict[str, str] = {
    "F3E":      "f3e.md",
    "OSN":      "osn.md",
    "BDM":      "bdm.md",
    "HJRP":     "hjrp.md",
    "UFL":      "ufl.md",
    "F3C":      "f3c.md",
    "HJRPROD":  "hjrprod.md",
    "HJRG":     "fndr.md",
    "FNDR":     "fndr.md",
    "LEX":      "lex.md",
    "LEX-LLC":  "lex.md",
    "LEX-LLA":  "lex.md",
    "LEX-LBHS": "lex.md",
    "LEX-LTS":  "lex.md",
}

DEFAULT_FILE = "fndr.md"


def file_for(entity: str) -> str:
    """Known-answers filename for an entity (DEFAULT_FILE for unknown)."""
    return ENTITY_FILES.get((entity or "").strip().upper(), DEFAULT_FILE)
