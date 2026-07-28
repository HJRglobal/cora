"""Canonical entity-code normalization for KB ingest (Part 2 Slice 2-2, 2026-07-28).

Some connectors historically wrote KB chunks under a SUB-entity or franchise code as the
top-level ``entity`` (e.g. ``entity='LEX-LLC'``, ``'OSNGF'``, ``'HJRP-LCI'``, ``'F3'``).
Retrieval filters on the CANONICAL parent codes (F3E, OSN, HJRP, LEX, ...), so those
chunks were DARK -- never matched by any channel's entity filter. 211 such chunks existed
on 2026-07-28 (LEX-LLC 120, OSNG{F,M,W}/OSNVV 15 each, HJRP-LCI 14, LEX-LLA 9,
HJRP-1337 4, HJRP-1555 3, F3 1).

``normalize_entity`` maps a stray code back to its canonical parent:
  * a ``LEX-*`` code moves into ``sub_entity`` (PRESERVING the LEX sub-entity security
    scoping -- the canonical sub_entity form IS the full ``LEX-LLC``-style code), so a
    normalized LEX-LLC chunk is visible in #llc + GM-LEX channels and still excluded
    from #lts/#lbhs/#lla by the strict sub-entity filter;
  * the OSN franchise codes and the HJRP property codes collapse onto their parent
    (OSN / HJRP have no sub-entity security scoping);
  * ``F3`` -> ``F3E``.

FAIL-OPEN: an unrecognized code is passed through UNCHANGED with a WARN, never dropped --
mis-tagging a chunk is worse than leaving an unknown code visible under its own name.
Idempotent: a canonical code maps to itself.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# The canonical top-level entity codes. A doc already carrying one of these is left
# alone (case-normalized to upper). Everything else is a candidate for remapping.
CANONICAL: frozenset[str] = frozenset({
    "FNDR", "HJRG", "F3E", "F3C", "OSN", "UFL", "BDM", "HJRP", "HJRPROD", "LEX",
})

# OSN franchise / store codes seen as top-level entities. Also matched by the
# ``OSN``-prefix rule below (so a future franchise code is caught too).
_OSN_STORES: frozenset[str] = frozenset({"OSNGF", "OSNGM", "OSNGW", "OSNVV"})


def normalize_entity(entity: str | None,
                     sub_entity: str | None = None) -> tuple[str, str | None]:
    """Return (canonical_entity, sub_entity) for a possibly-stray entity code.

    * LEX-* -> ("LEX", existing sub_entity or the code itself, in canonical LEX-XXX form)
    * OSN franchise (OSNGF/GM/GW/VV or any OSN-prefixed non-"OSN") -> ("OSN", sub_entity)
    * HJRP-* -> ("HJRP", sub_entity)
    * F3 -> ("F3E", sub_entity)
    * canonical code -> (code, sub_entity)
    * anything else -> (code_as_given, sub_entity) + a WARN (fail-open, never dropped)
    """
    e = (entity or "").strip().upper()
    if not e:
        return e, sub_entity
    if e in CANONICAL:
        return e, sub_entity
    if e == "F3":
        return "F3E", sub_entity
    if e in _OSN_STORES or (e.startswith("OSN") and e != "OSN"):
        return "OSN", sub_entity
    if e.startswith("LEX-"):
        # The stray entity code is already in the canonical sub_entity form (LEX-LLC,
        # LEX-LLA, ...). Preserve an existing sub_entity if the connector already set one.
        return "LEX", (sub_entity if sub_entity else e)
    if e.startswith("HJRP-"):
        return "HJRP", sub_entity
    log.warning("entity_normalize: unrecognized entity code %r -- left as-is (fail-open)",
                entity)
    return e, sub_entity
