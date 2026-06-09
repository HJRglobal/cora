"""Deterministic entity detection from HJR file names.

The personal-Drive sweep (drive_sweep.sweep_user) leans on Claude Haiku to guess
each file's owning entity from a content preview. Haiku misfires often enough to
pollute entity-scoped recall -- e.g. an OSN P&L (`2026-04_osn-gf_pl.xlsx`) tagged
LEX, or an HJRP invoice (`2026-06-01_hjrp_...pdf`) tagged LEX-LLC. Those files then
fail to surface in their real entity's channels and risk surfacing in the wrong one.

HJR's naming convention is reliable where it is followed:

    YYYY-MM-DD_entity-code_kebab-description.ext
    YYYY-MM_entity-code_kebab-description.ext
    entity-code_kebab-description.ext

So when the entity-code token is present and unambiguous, we trust it over Haiku.
This module returns the FINAL entity label string expected by
drive_sweep._ingest_file (which splits "LEX-LLC" -> entity=LEX, sub_entity=LEX-LLC
for the LEX / HJRP / HJRPROD prefixes only). For store-level / service-line codes
that are NOT KB sub-entities (e.g. osn-gf), we collapse to the parent entity.

Detection is conservative: it only fires on an exact token match in the first two
naming tokens, so ordinary description words never trigger a false override. When
nothing matches, it returns None and the caller keeps Haiku's guess.
"""

from __future__ import annotations

import re

# Leading date token: 2026, 2026-06, or 2026-06-01
_DATE_TOKEN = re.compile(r"^\d{4}(-\d{2}){0,2}$")

# Map an exact lowercase entity-code token -> the entity label _ingest_file expects.
# LEX / HJRP sub-entities keep the combined form so _ingest_file derives sub_entity.
# Store-level / service-line codes that are NOT KB sub-entities collapse to parent.
_CODE_TO_LABEL: dict[str, str] = {
    # Founder + holdco
    "fndr": "FNDR",
    "hjrg": "HJRG",
    # F3
    "f3e": "F3E",
    "f3c": "F3C",
    # UFL
    "ufl": "UFL",
    # Productions umbrella (POD/FF/HJR-PB/CHK/CHB roll up to HJRPROD at the KB level)
    "hjrprod": "HJRPROD",
    "pod": "HJRPROD",
    "ff": "HJRPROD",
    "hjr-pb": "HJRPROD",
    "chk": "HJRPROD",
    "chb": "HJRPROD",
    # Big D Media
    "bdm": "BDM",
    # Properties + sub-entities
    "hjrp": "HJRP",
    "hjrp-cl": "HJRP-CL",
    "hjrp-lci": "HJRP-LCI",
    "hjrp-rr": "HJRP-RR",
    # Lexington + sub-entities (bare + prefixed spellings both seen in the wild)
    "lex": "LEX",
    "lex-llc": "LEX-LLC",
    "llc": "LEX-LLC",
    "lex-lla": "LEX-LLA",
    "lla": "LEX-LLA",
    "lex-lbhs": "LEX-LBHS",
    "lbhs": "LEX-LBHS",
    "lex-lts": "LEX-LTS",
    "lts": "LEX-LTS",
    "lex-dds": "LEX",   # DDS is a service line, not a KB sub-entity -> parent LEX
    # One Stop Nutrition (store-level codes collapse to OSN)
    "osn": "OSN",
    "osn-gf": "OSN",
    "osn-gm": "OSN",
    "osn-gw": "OSN",
    "osn-vv": "OSN",
    "osn-vvp": "OSN",
}

# Tokens we never treat as an entity code even if they collide (kept explicit so
# the map above stays the single source of truth; reserved for future guards).
_AMBIGUOUS: frozenset[str] = frozenset({"hjr"})


def detect_entity_from_filename(filename: str) -> str | None:
    """Return the entity label encoded in a filename, or None if none is unambiguous.

    Looks only at the first two underscore-delimited naming tokens (after an
    optional leading date), so description words cannot trigger a false match.
    Codes may themselves contain hyphens (e.g. ``lex-llc``), so we split on
    underscores only.
    """
    if not filename:
        return None

    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    tokens = [t for t in stem.split("_") if t]
    if not tokens:
        return None

    # Drop a single leading date token if present.
    if _DATE_TOKEN.match(tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return None

    # Only consider the first two naming positions for a code match.
    for token in tokens[:2]:
        code = token.strip().lower()
        if code in _AMBIGUOUS:
            continue
        label = _CODE_TO_LABEL.get(code)
        if label:
            return label
    return None
