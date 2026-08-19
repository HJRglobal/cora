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
    # `osn-core4` is the parent-company slug used by the accounting archive
    # (CORE 4 OSN LLC). It was absent here, so 2026-05_osn-core4_pl.xlsx ingested
    # as entity=LEX -- Haiku guessing, because justin@lexingtonservices.com sweeps
    # that folder with entity_default LEX. The QBO monthly-report populator emits
    # this slug every month, so without this row it would industrialize that
    # misclassification (D-051).
    "osn-core4": "OSN",
    # ---- accounting-archive slugs (2026-08-19, D-194) ----------------------
    # 01-HJR-Global/accounting/monthly-reports/ is swept into the KB by
    # justin@lexingtonservices.com (entity_default LEX), so ANY archive slug
    # missing from this map is decided by a Haiku guess anchored to LEX. The
    # archive's full slug universe is pinned in data/maps/qbo-monthly-report-
    # slugs.yaml; these are the remaining ones that have a deterministic home.
    "f3comm": "F3C",        # F3 Community
    "hjrpod": "HJRPROD",    # HJR Podcast -- rolls up like pod/ff/chk/chb
    "mv": "LEX-LLA",        # LLA Maryvale
    # `lexcorp` ("LexCorp, LLC") maps to the LEX PARENT, not a sub-entity: it is
    # a Lexington-family book, but no `LEX-CORP` KB sub-entity exists and
    # asserting one of LLC/LLA/LBHS/LTS would be a guess.
    #
    # Parent-level is only ACTUALLY parent-level because drive_sweep marks these
    # files `metadata.lex_gm_level`. Without that, store.upsert_documents Step 0
    # re-derives sub_entity from title + content for any LEX doc arriving with
    # sub_entity=None, so this row would have fixed the parent while leaving the
    # scatter it was added to stop. A map entry and an ingest flag are load-
    # bearing together here; changing one without the other re-opens it.
    # NOTE for whoever settles this: drive_financial_reader.ENTITY_TO_REPORT_CODE
    # maps LEX -> "lexcorp", which CONTRADICTS the twice-confirmed
    # qbo-monthly-report-slugs.yaml (realm LEX -> slug "llc", company "Lexington
    # LLC", with "lexcorp" listed as a separate unmapped company). That conflict
    # is money-adjacent and is flagged for Harrison rather than resolved here.
    "lexcorp": "LEX",
}

# Slugs whose files must never enter the KB at all. This is an EXCLUSION, not an
# entity: there is no KB entity that means "personal", and mapping one to FNDR
# would be strictly worse because FNDR chunks co-scan into every non-LEX
# retrieval. Enforced at the sweep chokepoint (drive_sweep), so it covers every
# swept mailbox rather than one roster entry (D-194, Harrison-ruled 2026-08-18).
#
#   hjrllc -- "Harrison Rogers, LLC", Harrison's PERSONAL books. Already in
#             finance_close.PACK_EXCLUDED_ENTITIES and shipped `enabled: false`
#             in the monthly-report populator for the same reason. The rows the
#             pre-exclusion sweeps already ingested are purged separately by
#             scripts/purge_kb_personal_books_2026-08-19.py (Harrison-gated).
_EXCLUDED_CODES: frozenset[str] = frozenset({"hjrllc"})

# Slugs that mean what they mean ONLY inside the dated accounting-archive
# convention (`YYYY-MM_<slug>_<doctype>`), and are ordinary words or initials
# everywhere else. They match ONLY when a leading date token is present.
#
# D-051: without this the detector runs corpus-wide over every swept mailbox and
# overrides Haiku on a bare token match, so `MV.xlsx` and `LexCorp_Balance+Sheet.xlsx`
# -- real Founder-OS files currently tagged HJRG/FNDR, verified live against
# 53,350 distinct Drive titles -- would move into LEX, the FIREWALLED entity, on
# their next sweep: visible to #llc-*/#lex-* and invisible to HJRG. `mv` is two
# characters; it was never safe as a global token. The purge pass already
# required the dated convention for its re-tag (archive_slug); the sweep-side
# detector has to agree, or the two disagree about what an archive file is.
_ARCHIVE_ONLY_CODES: frozenset[str] = frozenset({
    "mv", "lexcorp", "f3comm", "hjrpod", "osn-core4",
})

# Tokens we never treat as an entity code even if they collide (kept explicit so
# the map above stays the single source of truth; reserved for future guards).
_AMBIGUOUS: frozenset[str] = frozenset({"hjr"})


def naming_tokens(filename: str) -> list[str]:
    """Return the naming tokens a code match may be drawn from, lowercased.

    Shared by :func:`detect_entity_from_filename` and :func:`excluded_slug_from_filename`
    so the exclusion and the entity map can never disagree about which tokens a
    filename offers: the same window decides both.

    Splits on underscores only (codes may contain hyphens, e.g. ``lex-llc``),
    drops one optional leading date token, and keeps only the first two naming
    positions so ordinary description words cannot trigger a match.
    """
    if not filename:
        return []

    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    tokens = [t for t in stem.split("_") if t]
    if not tokens:
        return []

    # Drop a single leading date token if present.
    if _DATE_TOKEN.match(tokens[0]):
        tokens = tokens[1:]

    return [t.strip().lower() for t in tokens[:2]]


def has_date_token(filename: str) -> bool:
    """True when the filename opens with the archive convention's date token.

    Shared by the detector and the D-194 re-tag pass so "is this an archive
    file?" has exactly one answer.
    """
    stem = (filename or "").rsplit(".", 1)[0] if "." in (filename or "") else (filename or "")
    head = stem.split("_", 1)[0].strip()
    return bool(head) and bool(_DATE_TOKEN.match(head))


def excluded_slug_from_filename(filename: str) -> str | None:
    """Return the excluded slug encoded in a filename, or None.

    A non-None result means the file must not be ingested into the KB at all --
    it is not a "which entity?" answer, it is a "no entity, ever" answer.
    Checked BEFORE :func:`detect_entity_from_filename` at the sweep chokepoint so
    a filename carrying both an excluded slug and a mappable one (e.g.
    ``2026-05_hjrllc_llc-summary.xlsx``) is excluded rather than filed.
    """
    for code in naming_tokens(filename):
        if code in _EXCLUDED_CODES:
            return code
    return None


# Parent codes whose "PARENT-CHILD" labels are real KB sub-entities. Everything
# else (e.g. OSN store codes) has already been collapsed to its parent above.
_SUBENTITY_PREFIXES: tuple[str, ...] = ("LEX", "HJRP", "HJRPROD")


def split_entity_label(label: str) -> tuple[str, str | None]:
    """Split an entity LABEL into the (entity, sub_entity) pair the KB stores.

    ``"LEX-LLA" -> ("LEX", "LEX-LLA")`` · ``"OSN" -> ("OSN", None)``.

    This is the same split :func:`cora.connectors.drive_sweep._ingest_file`
    applies, extracted so that anything reasoning about where a filename SHOULD
    have landed (the D-194 re-tag pass) computes it from the same function the
    sweep writes with, rather than from a re-derived copy that can drift.
    """
    entity = (label or "").strip()
    if "-" in entity:
        prefix = entity.split("-")[0]
        if prefix in _SUBENTITY_PREFIXES:
            return prefix, entity
    return entity, None


def detect_entity_from_filename(filename: str) -> str | None:
    """Return the entity label encoded in a filename, or None if none is unambiguous.

    Looks only at the first two underscore-delimited naming tokens (after an
    optional leading date), so description words cannot trigger a false match.
    Codes may themselves contain hyphens (e.g. ``lex-llc``), so we split on
    underscores only.

    Codes in :data:`_ARCHIVE_ONLY_CODES` additionally require the dated
    accounting-archive convention -- they are ordinary words or initials outside
    it, and this function runs over every swept mailbox.

    An excluded slug (see :func:`excluded_slug_from_filename`) never yields a
    label -- it is absent from ``_CODE_TO_LABEL`` by construction -- but callers
    must still check the exclusion explicitly, because a *second* token could
    otherwise place a file the first token forbids.
    """
    dated = has_date_token(filename)
    for code in naming_tokens(filename):
        if code in _AMBIGUOUS:
            continue
        if code in _ARCHIVE_ONLY_CODES and not dated:
            continue
        label = _CODE_TO_LABEL.get(code)
        if label:
            return label
    return None
