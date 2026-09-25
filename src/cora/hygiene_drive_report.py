r"""Pure builders for the weekly Founder-OS Drive hygiene report (Code #15 R8).

WHY THIS EXISTS (cq-592baba613f1)
---------------------------------
The Cowork ``hygiene-drive`` task inventoried the tree by sampling (30 Drive
searches over ``_inbox`` + the last 7 days), so it could never count a Drive
suffix twin, an empty folder or a loose root file it did not happen to search,
and its moves file landed at the tree root as well as in
``_shared\hygiene-pending-moves`` (the 2026-09-12 misfile). This module turns a
full NO-HASH inventory (the reviewed folder-audit PS1, ``-MaxHashMB 0``) into a
deterministic, week-over-week report instead.

NOT BOT-LOADED. Nothing in ``app.py`` / ``cora.main`` imports this module; its
only callers are ``scripts/run_hygiene_drive_weekly.py`` (the Saturday task) and
``scripts/propose_rider_b_holds_manifest.py`` (RIDER B items 6+7). A change here
activates at the next task fire, with no restart.

THE DISCLOSURE RULES (why the report is counts-only in places)
--------------------------------------------------------------
The report ``.md`` is written into ``_shared\hygiene-pending-moves``, and
``scripts/incremental_sync_static.py`` INGESTS that folder -- every name the
report lists becomes a KB chunk. So the report is a second ingest door for
whatever it names, and gets its own belt:

  * LEX -- ``08-Lexington-Services``, plus any path outside it with a segment
    naming LEX, a LEX sub-entity / program / lead, or COPA (``is_lex_relpath``:
    the ONE LEX title detector ``fireflies_connector.classify_lex_meeting`` per
    segment, cross_entity_guard's LEX keyword row, the named LEX people from
    the detector's lists and the org_roles roster, plus a non-letter bounded
    sub-entity belt) -- counts only, never a name or a path.
  * every KB-pinned container (the ``kb_exclusions`` pins inside the tree, plus
    the two RIDER B pins ``_archive`` and ``00-Founder\personal-finances``) --
    counts only: a pinned folder listed by name here would leak back into the
    KB through this door.
  * every name that is listed is screened with ``phi_guard.is_any_phi``, and
    any path with one of the static walk's PHI folder segments
    (``incremental_sync_static.PHI_BLACKLIST_SEGMENTS``, imported so the two
    cannot drift) is withheld: a hit is replaced by a count.
  * ``_archive`` is excluded from every offender list (its "X 2" mirrors are the
    archive working as designed) and counted separately.

Everything here is a pure function of its inputs: no filesystem writes, no
environment reads, no network. (The screens above are imported lazily, once,
from their owning modules -- importing ``incremental_sync_static`` runs its own
non-override ``load_dotenv()``, which never changes a variable already set; the
org_roles roster is a read of the repo's ``data/maps/org-roles.yaml``. Any import
or load failing makes its screen withhold every path: fail closed, never list.)
"""

from __future__ import annotations

import csv
import functools
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

from cora import kb_exclusions, phi_guard

# The first line of every report this module renders. The runner refuses to
# overwrite a same-named file that does not carry it (a hand-written note that
# happens to share the dated name is never clobbered).
GENERATOR_MARKER = "<!-- generated-by: cora.hygiene_drive_report (Code #15 R8) -->"

LEX_TOP = "08-Lexington-Services"

# Founder-OS relpath prefixes (backslash, case-insensitive) of every KB-pinned
# container inside the tree, with a short label. The first six are the
# KB_EXCLUDED_FOLDER_IDS pins that live inside the Founder OS (their labels in
# kb_exclusions.KB_EXCLUDED_FOLDER_LABELS name the same paths --
# tests/test_rider_b_holds_manifest.py pins the correspondence so a future pin
# cannot be missed here); the last two are the RIDER B pins (ruled 2026-09-24).
PINNED_CONTAINERS: tuple[tuple[str, str], ...] = (
    ("00-Founder\\insurance\\oneamerica", "oneamerica"),
    ("02-F3-Energy\\projects\\capital-raise", "capital-raise"),
    ("00-Founder\\travel-points", "travel-points"),
    ("08-Lexington-Services\\projects\\copa-bhrf", "copa-bhrf"),
    ("01-HJR-Global\\accounting\\cashflow-ledger", "cashflow-ledger"),
    ("_shared\\projects\\cora", "cora-workspace"),
    ("_archive", "_archive"),
    ("00-Founder\\personal-finances", "personal-finances"),
)

# The canonical depth-1 folder set (R1).
CANONICAL_TOP: frozenset[str] = frozenset({
    "00-Founder", "01-HJR-Global", "02-F3-Energy", "03-F3-Community", "04-UFL",
    "05-HJR-Productions", "06-HJR-Properties", "07-Big-D-Media",
    "08-Lexington-Services", "09-One-Stop-Nutrition",
    "_shared", "memory", "_brain", "_archive", "_inbox", "inventory",
})

# The 9-column dedup-manifest schema, byte-for-byte as manifest-dedup-2026-09.csv
# writes it: UTF-8 BOM, unquoted header, LF line ends. apply.ps1 reads it with
# Import-Csv.
MANIFEST_COLUMNS: tuple[str, ...] = (
    "action", "source_relpath", "dest_relpath", "reason_code", "sha256", "bytes",
    "keep_relpath", "is_md", "top_level",
)
MANIFEST_HEADER_BYTES: bytes = b"\xef\xbb\xbf" + ",".join(MANIFEST_COLUMNS).encode("ascii") + b"\n"

_PROJECT_SLUG_RE = re.compile(r"^(00-Founder|0[1-9]-[^\\]+|_shared)\\projects\\([^\\]+)$", re.IGNORECASE)
# The PS1's underscore-n regex is single-digit only ('_\d\.[^.]+$', l.110) and so
# misses _10.png / _26.jpeg. This is the recomputed superset -- it ALSO matches
# ordinary names such as report_2026.pdf, so it is reported as a labelled count,
# never as an offender list.
_UNDERSCORE_ANY_RE = re.compile(r"_\d+\.[^.]+$")
SLUG_LEN = 40
DEFAULT_LIST_CAP = 50


# ── path predicates ──────────────────────────────────────────────────────────

def _segs(relpath: str) -> list[str]:
    return [s for s in re.split(r"[\\/]+", relpath or "") if s]


# A LEX-named segment OUTSIDE the partition: "Lexington Entities" folders in the
# HJRG accounting tree, "Lexington - Progress (16).gdoc" meeting summaries in
# _shared\meetings (measured live 2026-09-24). "Non-Lexington" / "02 Non-Lexington"
# (the HJRG non-LEX entity folders) are NOT LEX and stay listable.
_LEX_NAME_RE = re.compile(r"(?<![a-z])(?<!non-)(?<!non )(?<!non_)lexington", re.IGNORECASE)

# The sub-entity / program belt (D-051 R1 rb2-r8#0 + lens-d082#0): the LEX
# codes the rest of Cora files as LEX (cross_entity_guard's LEX list,
# drive_entity_detect's 'lbhs') that the title detector below does not key on
# alone. "Word-bounded" = any NON-LETTER on the left, looser than a regex \b on
# purpose ('_' and digits are word characters to \b): it fires on "LBHS_report",
# "LTS-weekly", "Copy of Lex Services", "LexLLC", "DDD contract", "COPA_LOI",
# "COPA2" -- and never inside "Alex", "flex", "complex", "results", "villa",
# "Maricopa", "copay", "Lexus" or "Lexington" (that word has the rule above, with
# its Non-Lexington exception). D-051 R2 rb2#r2-1 widened it three ways:
#   * lex glued or joined to a code / 'services' -- LexServices, LexLTS, Lex_LBHS;
#   * lbhs / lts / bhrf / hcbs with a glued TAIL -- LTSpayroll, BHRFs, HCBSrates
#     (no English word starts with those letters, so the left bound suffices);
#   * lex / lla / ddd / copa with an optional plural (LLAs, DDDs) -- a digit tail
#     (LEX2026, COPA2) already passes the non-letter right bound. COPA joins the
#     belt because kb_exclusions' whole-word ``\bcopa\b`` misses COPA_LOI / COPA2.
_LEX_BELT_RE = re.compile(
    r"(?<![a-z])(?:"
    r"lex[\s_.-]*(?:ll[ac]|lts|lbhs|lla|services?|svcs?)"
    r"|lbhs|lts|bhrf|hcbs"
    r"|(?:lex|lla|ddd|copa)s?(?![a-z])"
    r")", re.IGNORECASE)

# Separators a hand- or Drive-named file joins words with. The detector and the
# cross-entity keyword list below match literal spaced phrases ('jared harker',
# 'tucson dta'), so each also sees the segment with these turned into spaces.
_SEP_RE = re.compile(r"[_.\-]+")
# The HJRG 'Non-Lexington' binder folder, once separators are spaces: masked
# before cross_entity_guard's keywords run (its 'lexington' keyword has no such
# exception) -- the same sanctioned exception _LEX_NAME_RE carries.
_NON_LEXINGTON_RE = re.compile(r"(?<![a-z])non\s+lexington", re.IGNORECASE)

# The ONE LEX title detector, imported once (a list so a failed import is cached
# too): [classify_lex_meeting], or [None] when the import failed.
_LEX_DETECTOR: list = []


def _lex_detector():
    if not _LEX_DETECTOR:
        try:
            from cora.connectors.fireflies_connector import classify_lex_meeting
        except Exception:  # noqa: BLE001 -- an unavailable detector withholds, never lists
            classify_lex_meeting = None
        _LEX_DETECTOR.append(classify_lex_meeting)
    return _LEX_DETECTOR[0]


# cross_entity_guard's entity keyword table (its LEX row carries 'tucson dta',
# 'revalidation', ... that neither the belt nor the detector knows), imported
# once through its public read-only view so the two lists cannot drift:
# [detect_entities], or [None] when the import failed.
_LEX_ENTITIES: list = []


def _lex_entities():
    if not _LEX_ENTITIES:
        try:
            from cora.cross_entity_guard import detect_entities
        except Exception:  # noqa: BLE001 -- an unavailable keyword list withholds, never lists
            detect_entities = None
        _LEX_ENTITIES.append(detect_entities)
    return _LEX_ENTITIES[0]


# CamelCase and a glued all-caps code read as word breaks, for the belt and the
# LEX-name rule (D-051 R3 rb2#r3-2): "LexOps" / "LexHR" -> "Lex Ops" / "Lex HR",
# "LEXreport" / "LLAreport" / "COPAdiligence" -> "LEX report" / ..., "LEXReport"
# -> "LEX Report", "TheLexington" / "HJRLexington" -> "The Lexington" / "HJR
# Lexington". "Lexicon", "Lexus", "LEXUS", "Alex", "FlexSeal" and "NonLexington"
# ("Non Lexington": the rule's own exception) split into nothing the rules match.
_CAMEL_RE = re.compile(
    r"(?<![A-Z])(LEX|LLA|DDD|COPA)(?=[a-z]|[A-Z][a-z])"      # a glued all-caps code (D-051 r4:
                                                             # after a lowercase too: TheLEXreport)
    r"|(?<=[a-z])(?=[A-Z])"                                  # camelCase
    r"|(?<=[A-Z])(?=[A-Z][a-z])")                            # ACRONYMWord


def _split_camel(seg: str) -> str:
    # a glued code gets a break on BOTH sides ("TheLEXreport" -> "The LEX report")
    return _CAMEL_RE.sub(
        lambda m: ((" " if m.start() else "") + m.group(1) + " ") if m.group(1) else " ", seg)


# The named LEX people, as ONE compiled regex: [pattern], or [None] when a source
# failed to load. Sources (each read from its owner, never copied here): the LEX
# detector's own lead identifiers (the per-sub-entity participant lists, the
# Shaun list, the person names among its LEX title keywords) and every
# org_roles registry person whose PRIMARY entity is a LEX entity (the one
# roster). Each name matches first-last OR last-first, joined by any run of
# spaces / '_' / '.' / ',' / '-' / '+' or by NOTHING ("Justin_Gilmore", "Gilmore,
# Justin", "jeff.montgomery", "JustinGilmore", "gilmorejustin"), and first
# INITIAL + last ("JGilmore", "J.Gilmore", "J_Gilmore", "J. Gilmore") -- and on
# its own, so a segment naming two leads of different sub-entities (which the
# detector deliberately leaves untagged) is still LEX-named. Not covered: the
# last name alone, and dotted / spaced initials of both names. Primary entity
# only: a founder or finance lead with LEX among several entities is not a LEX
# person, and a file named after them is not LEX.
_LEX_LEADS: list = []
_LEAD_SEP = r"[\s_.,+\-]+"
_LEAD_JOIN = r"[\s_.,+\-]*"          # any run of separators, or none (glued)
_LEAD_INITIAL_JOIN = r"[\s_.]{0,2}"   # "J.Gilmore", "J_Gilmore", "J. Gilmore", "JGilmore"


def _lex_person_names() -> set[str]:
    from cora import org_roles
    from cora.connectors import fireflies_connector as ff

    names: set[str] = set()
    for identifiers, _code in ff._FIREFLIES_PARTICIPANT_SUB_ENTITY:
        names.update(identifiers)
    names.update(ff._SHAUN_IDENTIFIERS)
    for kw in ff._LEX_TITLE_KEYWORDS:
        # only the PERSON names among the title keywords ('shaun hawkins'); the
        # entity phrases ('lex services', 'lexington services') are the belt's
        if not (_LEX_BELT_RE.search(kw) or _LEX_NAME_RE.search(kw)):
            names.add(kw)
    for rec in org_roles.all_roles():
        if str(rec.entity or "").strip().upper().startswith("LEX"):
            names.add(rec.name)
    return names


def _lex_lead_re():
    if not _LEX_LEADS:
        try:
            alts: set[str] = set()
            for name in _lex_person_names():
                toks = [t for t in re.split(_LEAD_SEP, str(name).strip().lower()) if t]
                if len(toks) < 2 or not all(t.replace("'", "").isalpha() for t in toks):
                    continue   # a person name is two or more words
                first, last = re.escape(toks[0]), re.escape(toks[-1])
                alts.add(f"{first}{_LEAD_JOIN}{last}")
                alts.add(f"{last}{_LEAD_JOIN}{first}")
                alts.add(f"{re.escape(toks[0][0])}{_LEAD_INITIAL_JOIN}{last}")
            if not alts:
                raise ValueError("no LEX person names loaded")
            rx = re.compile(r"(?<![a-z])(?:" + "|".join(sorted(alts)) + r")(?![a-z])", re.IGNORECASE)
        except Exception:  # noqa: BLE001 -- an unavailable name list withholds, never lists
            rx = None
        _LEX_LEADS.append(rx)
    return _LEX_LEADS[0]


@functools.lru_cache(maxsize=65536)
def _segment_names_lex(seg: str) -> bool:
    """True when ONE path segment names the LEX world. Fails CLOSED: any
    screen error (or an unavailable detector / keyword list / name list) counts
    the path as LEX."""
    if _LEX_NAME_RE.search(seg) or _LEX_BELT_RE.search(seg):
        return True
    try:
        camel = _split_camel(seg)
        if camel != seg and (_LEX_NAME_RE.search(camel) or _LEX_BELT_RE.search(camel)):
            return True
        if kb_exclusions.is_copa_meeting_title(seg):
            return True
        leads = _lex_lead_re()
        # D-051 r4: a lead glued to a neighbouring word ("JaneDoeNotes") is found on
        # the CamelCase-split form too.
        if leads is None or leads.search(seg) or (camel != seg and leads.search(camel)):
            return True
        spaced = _SEP_RE.sub(" ", seg)
        entities = _lex_entities()
        if entities is None or "LEX" in entities(_NON_LEXINGTON_RE.sub(" ", spaced)):
            return True
        detect = _lex_detector()
        if detect is None:
            return True
        # The segment is offered as the title AND as an attendee name: the
        # detector's named-lead signal (the LEX sub-entity leads) keys on
        # attendee names, and a file named after a LEX lead is LEX-named. It
        # sees the raw segment and the separator-spaced one ("jeff.montgomery").
        for text in dict.fromkeys((seg, spaced)):
            verdict = detect({"title": text, "meeting_attendees": [{"displayName": text, "email": ""}]})
            if verdict.is_lex:
                return True
        return False
    except Exception:  # noqa: BLE001 -- a screen error withholds, never lists
        return True


# The static walk's PHI folder segments, imported once from the ingest door this
# report feeds (so the two lists cannot drift): [frozenset], or [None] when the
# import failed.
_PHI_SEGMENTS: list = []


def _static_phi_segments() -> frozenset[str] | None:
    if not _PHI_SEGMENTS:
        try:
            import incremental_sync_static as _static  # scripts/ -- on sys.path for both callers
            segs: frozenset[str] | None = frozenset(str(s).lower() for s in _static.PHI_BLACKLIST_SEGMENTS)
        except Exception:  # noqa: BLE001 -- an unavailable screen withholds, never lists
            segs = None
        _PHI_SEGMENTS.append(segs)
    return _PHI_SEGMENTS[0]


def is_phi_segment_path(relpath: str) -> bool:
    """True for a path with any segment the static walk refuses as PHI
    (``clients``, ``consumers``, ``phi``, ``clinical``, ``ehr``). Fails closed:
    an unavailable segment list withholds every path."""
    segs = _static_phi_segments()
    if segs is None:
        return True
    return any(s.lower() in segs for s in _segs(relpath))


def is_lex_partition(relpath: str) -> bool:
    """True for a path IN the LEX partition: a top segment starting ``08-``, or ANY
    segment that is ``08-Lexington-Services`` / ``copa-bhrf`` (an archived LEX copy
    under ``_archive`` still counts). This is apply.ps1's gate-2 world (plus the
    archived copies), used where a manifest row is being decided: the RIDER B
    proposer and the desktop.ini DELETE rows."""
    segs = [s.lower() for s in _segs(relpath)]
    if not segs:
        return False
    if segs[0].startswith("08-"):
        return True
    return any(s == LEX_TOP.lower() or s == "copa-bhrf" for s in segs)


def is_lex_relpath(relpath: str) -> bool:
    """The LISTING gate: True for any path in, or NAMING, the LEX world -- counted
    by the report, never named:

      * the partition (``is_lex_partition``);
      * any segment carrying a LEX name outside the partition ("Lexington
        Entities", "Lexington - Progress (16).gdoc", "TheLexington"; not
        "Non-Lexington" / "NonLexington");
      * any segment the ONE LEX title detector (``fireflies_connector.
        classify_lex_meeting``) calls LEX -- 'LBHS', 'Lex-LLC', 'Lex Services',
        the named LEX leads, DDD / care / clinical titles -- as named, and with
        '_' / '.' / '-' read as spaces;
      * any segment carrying a sub-entity / program code (``_LEX_BELT_RE``:
        lex, lbhs, lla, lts, lex-llc/lla, ddd, hcbs, bhrf, copa, left
        non-letter bounded, plurals and glued forms included), also read with
        CamelCase and a glued all-caps code as word breaks (``_split_camel``:
        LexOps, LexHR, LEXreport, LEXReport, LLAreport, COPAdiligence);
      * any segment cross_entity_guard's LEX keyword row matches ('tucson
        dta', 'revalidation', ...; the same table, never a copy);
      * any segment naming a LEX person (the detector's lead lists plus every
        org_roles person whose primary entity is LEX), first-last or
        last-first joined by any run of separators or by nothing (CamelCase,
        glued), or first initial + last (JLast, J.Last, J_Last, J. Last), one
        lead or several. NOT covered (listed): the last name alone, a middle
        name or initial between the two, both names as initials;
      * any segment naming the NDA'd COPA diligence (the belt's non-letter
        bounded ``copa`` -- COPA_LOI, COPA2 -- plus ``kb_exclusions.
        is_copa_meeting_title``; never "Maricopa" or "copay") -- its meeting
        exports live OUTSIDE the copa-bhrf folder.

    A detector error counts the path as LEX (fail closed). Over-matching is the
    safe direction here: the report is a KB ingest door, and a LEX path is only
    ever counted. Manifest decisions keep ``is_lex_partition`` (the proposer, the
    desktop.ini DELETE rows) -- widening those held ordinary rows."""
    if is_lex_partition(relpath):
        return True
    return any(_segment_names_lex(s) for s in _segs(relpath))


def pinned_container(relpath: str) -> str | None:
    """The label of the KB-pinned container a relpath sits in, else None.

    Prefix match against PINNED_CONTAINERS first (gives the container identity
    the RIDER B proposer needs to tell pinned-internal from a straddle), then the
    ``kb_exclusions`` segment predicates as a belt: a dashboard-store /
    copa-bhrf / finance-worksheet segment ANYWHERE in the path, or the
    ``_shared/projects/cora`` subsequence / a Cora build-doc filename. The belt
    over-matches on purpose (a stray folder that happens to be called
    ``capital-raise`` is withheld too)."""
    low = (relpath or "").replace("/", "\\").lower()
    for prefix, label in PINNED_CONTAINERS:
        p = prefix.lower()
        if low == p or low.startswith(p + "\\"):
            return label
    raw = relpath or ""
    if kb_exclusions.is_dashboard_store_path(raw):
        return "segment:dashboard-store"
    if kb_exclusions.is_copa_bhrf_path(raw):
        return "segment:copa-bhrf"
    if kb_exclusions.is_finance_worksheet_path(raw):
        return "segment:finance-worksheet"
    if kb_exclusions.is_cora_internal_path(Path(raw)):
        return "segment:cora-internal"
    return None


def in_archive(relpath: str) -> bool:
    low = (relpath or "").lower()
    return low == "_archive" or low.startswith("_archive\\")


def phi_hit(text: str) -> bool:
    """Fail-closed PHI screen for a name the report would list."""
    try:
        return bool(phi_guard.is_any_phi(text or ""))
    except Exception:  # noqa: BLE001 -- a screen error withholds, never lists
        return True


# ── manifest CSV (shared with the RIDER B proposer) ─────────────────────────

def render_manifest_csv(rows: list[dict]) -> bytes:
    """The 9-column manifest bytes: BOM + header exactly MANIFEST_HEADER_BYTES,
    QUOTE_MINIMAL fields, LF line ends (manifest-dedup-2026-09.csv's format)."""
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    for r in rows:
        w.writerow([str(r.get(c, "") if r.get(c) is not None else "") for c in MANIFEST_COLUMNS])
    return MANIFEST_HEADER_BYTES + buf.getvalue().encode("utf-8")


def parse_manifest_csv(data: bytes) -> list[dict]:
    text = data.decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text, newline="")))


# ── inventory parsing ────────────────────────────────────────────────────────

def load_csv_rows(path: str | Path) -> list[dict]:
    """Read one PS1 inventory CSV (Export-Csv UTF8 = BOM; names may carry U+FFFD)."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


_SUMMARY_PATTERNS: dict[str, re.Pattern[str]] = {
    "run_status": re.compile(r"^Run status:\s+(\S+)", re.M),
    "root": re.compile(r"^\s+Root\s+=\s+(.+?)\s*$", re.M),
    "max_hash_mb": re.compile(r"^\s+MaxHashMB\s+=\s+(-?\d+)", re.M),
    "hash_all": re.compile(r"^\s+HashAll\s+=\s+(\S+)", re.M),
    "files_total": re.compile(r"^\s+Total files scanned:\s+(\d+)", re.M),
    "dirs_total": re.compile(r"^\s+Total dirs scanned:\s+(\d+)", re.M),
    "hashed": re.compile(r"^\s+HASHED:\s+(\d+)", re.M),
    "errors_logged": re.compile(r"^Walk/hash errors logged:\s+(\d+)", re.M),
}
_INT_KEYS = {"max_hash_mb", "files_total", "dirs_total", "hashed", "errors_logged"}


def parse_summary(text: str) -> dict:
    """Fields of an ``inventory-summary-<stamp>.txt`` (missing field -> None)."""
    text = (text or "").lstrip("﻿").replace("\r\n", "\n")
    out: dict = {}
    for key, rx in _SUMMARY_PATTERNS.items():
        m = rx.search(text)
        if not m:
            out[key] = None
        elif key in _INT_KEYS:
            out[key] = int(m.group(1))
        else:
            out[key] = m.group(1)
    return out


_RUNLOG_PREFIXES = ("WALK-FILE-ERROR", "WALK-DIR-ERROR", "HASH-ERROR", "FATAL-ERROR", "OUTPUT-WRITE-ERROR")
# The PS1's line shapes: "PREFIX: ..." and, for the output writes, "PREFIX (files
# CSV): ..." / "(dirs CSV)" / "(summary)" (folder-audit-inventory.ps1 l.543/559/
# 629). The prefix token is matched, then an optional "(qualifier)", then ':' --
# so "FATAL-ERROR-STACK: ..." (the stack line that follows a FATAL-ERROR) is not
# a second fatal. (D-051 R1 rb2-r8#2: the old exact-head match never counted
# the three OUTPUT-WRITE-ERROR shapes.)
_RUNLOG_LINE_RE = re.compile(
    r"^(" + "|".join(re.escape(p) for p in _RUNLOG_PREFIXES) + r")(?:\s*\([^)]*\))?\s*:")


def count_run_log(text: str) -> dict[str, int]:
    """Counts of each error prefix in a ``_run-log-<stamp>.txt``. Counts only --
    the lines themselves carry paths (possibly LEX) and are never echoed."""
    counts = {p: 0 for p in _RUNLOG_PREFIXES}
    for line in (text or "").lstrip("﻿").splitlines():
        m = _RUNLOG_LINE_RE.match(line.strip().lstrip("﻿"))
        if m:
            counts[m.group(1)] += 1
    return counts


def walk_error_count(counts: dict[str, int]) -> int:
    """Errors that make a walk untrustworthy (hash errors do not: a NO-HASH run
    has none, and the hashing baseline's 895 were unreadable-file hashes)."""
    return sum(counts.get(k, 0) for k in ("WALK-FILE-ERROR", "WALK-DIR-ERROR", "FATAL-ERROR", "OUTPUT-WRITE-ERROR"))


def norm_root(p: str | Path | None) -> str:
    s = str(p or "").strip().rstrip("\\/")
    return s.replace("/", "\\").lower()


# ── categories ───────────────────────────────────────────────────────────────

CATEGORY_LABELS: dict[str, str] = {
    "suffix-paren-n": "Drive-suffix files: ' (n)'",
    "suffix-underscore-n": "Drive-suffix files: '_n' (inventory regex, one digit)",
    "suffix-copy-of": "Drive-suffix files: 'Copy of'",
    "suffix-conflicted": "Drive-suffix files: conflicted copy",
    "loose-root": "Loose files at the tree root (excl. CLAUDE.md, desktop.ini)",
    "loose-founder-root": "Loose files at the 00-Founder root",
    "noncanonical-top": "Non-canonical top-level folders",
    "suffix-dirs": "'X 2' / ' (n)' folders",
    "slug-40": "Project slugs of exactly 40 characters",
    "empty-dirs": "Empty folders (excl. desktop.ini)",
}
# Count-only rows (no offender list).
TREND_LABELS: dict[str, str] = {
    "suffix-underscore-any": "Recomputed '_<digits>.' names (superset; includes ordinary names like _2026)",
    "desktop-ini": "desktop.ini (trend only -- Drive for Desktop recreates them)",
}


@dataclass
class Category:
    key: str
    count: int = 0          # offenders outside _archive (LEX + pinned included)
    lex: int = 0
    pinned: int = 0
    archive: int = 0        # offenders inside _archive (counted, never listed)
    items: set[str] = field(default_factory=set)

    def add(self, relpath: str) -> None:
        if in_archive(relpath):
            self.archive += 1
            return
        self.count += 1
        if is_lex_relpath(relpath):
            self.lex += 1
        elif pinned_container(relpath):
            self.pinned += 1
        self.items.add(relpath)


@dataclass
class CategorySet:
    cats: dict[str, Category]
    ext: dict[str, dict[str, int]]   # suffix type -> {extension: count}, non-LEX, outside _archive

    def __getitem__(self, key: str) -> Category:
        return self.cats[key]


def categories(files: list[dict], dirs: list[dict]) -> CategorySet:
    cats = {k: Category(k) for k in (*CATEGORY_LABELS, *TREND_LABELS)}
    ext_counts: dict[str, dict[str, int]] = {}
    for r in files:
        rel = r.get("RelPath", "")
        name = r.get("Name", "")
        if str(r.get("IsDesktopIni", "")).lower() == "true":
            cats["desktop-ini"].add(rel)
            continue
        sfx = r.get("DriveSuffix", "") or ""
        if sfx:
            key = f"suffix-{sfx}"
            if key in cats:
                cats[key].add(rel)
                if not in_archive(rel) and not is_lex_relpath(rel):
                    by_ext = ext_counts.setdefault(sfx, {})
                    ext = (r.get("Ext", "") or "(none)").lower()
                    by_ext[ext] = by_ext.get(ext, 0) + 1
        if _UNDERSCORE_ANY_RE.search(name):
            cats["suffix-underscore-any"].add(rel)
        d = r.get("Dir", "")
        if d == "" and name != "CLAUDE.md":
            cats["loose-root"].add(rel)
        elif d == "00-Founder":
            cats["loose-founder-root"].add(rel)
    for r in dirs:
        rel = r.get("RelPath", "")
        if str(r.get("Depth", "")) == "1" and rel not in CANONICAL_TOP:
            cats["noncanonical-top"].add(rel)
        if r.get("HasDriveSuffix"):
            cats["suffix-dirs"].add(rel)
        m = _PROJECT_SLUG_RE.match(rel)
        if m and len(m.group(2)) == SLUG_LEN:
            cats["slug-40"].add(rel)
        if str(r.get("IsEmptyExclDesktopIni", "")).lower() == "true":
            cats["empty-dirs"].add(rel)
    return CategorySet(cats, ext_counts)


@dataclass
class Diff:
    key: str
    now: int
    last: int | None
    new: set[str]
    resolved: int

    @property
    def delta(self) -> int | None:
        return None if self.last is None else self.now - self.last


def diff(now: CategorySet, prior: CategorySet | None) -> dict[str, Diff]:
    out: dict[str, Diff] = {}
    for key in (*CATEGORY_LABELS, *TREND_LABELS):
        n = now[key]
        if prior is None:
            out[key] = Diff(key, n.count, None, set(), 0)
            continue
        p = prior[key]
        out[key] = Diff(key, n.count, p.count, n.items - p.items, len(p.items - n.items))
    return out


# ── rendering ────────────────────────────────────────────────────────────────

@dataclass
class ScreenedList:
    listed: list[tuple[str, bool]]    # (relpath, is_new)
    lex: int = 0
    pinned: int = 0
    phi: int = 0


def screen(items: set[str], new: set[str]) -> ScreenedList:
    """NEW offenders first, then standing; LEX / pinned / PHI hits (the name
    screen or a static-walk PHI folder segment) withheld as counts. Every
    remaining name is safe to list."""
    out = ScreenedList([])
    ordered = [(r, True) for r in sorted(items & new, key=str.lower)]
    ordered += [(r, False) for r in sorted(items - new, key=str.lower)]
    for rel, is_new in ordered:
        if is_lex_relpath(rel):
            out.lex += 1
        elif pinned_container(rel):
            out.pinned += 1
        elif is_phi_segment_path(rel) or phi_hit(rel):
            out.phi += 1
        else:
            out.listed.append((rel, is_new))
    return out


def _md_path(rel: str) -> str:
    return "`" + rel.replace("`", "'") + "`"


def _fmt_delta(d: int | None) -> str:
    if d is None:
        return "n/a"
    return f"{d:+d}"


@dataclass
class RunFacts:
    date: str                 # YYYY-MM-DD (the run's local date)
    stamp: str                # the PS1 run stamp yyyyMMdd-HHmm
    files_total: int
    dirs_total: int
    walk_errors: int
    prior_stamp: str | None
    status: str               # "clean" | "unverified"
    sidecar_name: str | None


def render_md(facts: RunFacts, now: CategorySet, diffs: dict[str, Diff],
              *, cap: int = DEFAULT_LIST_CAP) -> str:
    lines: list[str] = [GENERATOR_MARKER, ""]
    lines.append(f"# [Hygiene] Drive -- {facts.date} (Cora weekly inventory)")
    lines.append("")
    status = ("CLEAN (walk passed the sanity floor: at least 80% of the largest of the last four "
              "comparable full runs, and of the newest re-baseline -- else the 2026-09-21 baseline)"
              if facts.status == "clean" else
              "UNVERIFIED (no prior full-root run to compare; the sanity floor was not applied)")
    lines.append(f"- Status: **{status}**")
    lines.append(f"- Run: stamp `{facts.stamp}` -- NO-HASH inventory (`-MaxHashMB 0`) of the full Founder-OS root")
    lines.append(f"- Compared with: {'stamp `' + facts.prior_stamp + '`' if facts.prior_stamp else 'nothing (first comparable run)'}")
    lines.append(f"- Walked: {facts.files_total} files, {facts.dirs_total} folders, {facts.walk_errors} walk errors")
    lines.append("- LEX (`08-Lexington-Services`, plus any path elsewhere naming LEX, a LEX sub-entity, "
                 "program or lead, or COPA) and the KB-pinned containers are reported as COUNTS ONLY; "
                 "their names are never listed. Listed names passed the PHI screen (the name screen and "
                 "the static walk's PHI folder segments). `_archive` is "
                 "excluded from every offender list and counted separately.")
    if facts.sidecar_name:
        lines.append(f"- Full lists (non-LEX): `{facts.sidecar_name}` next to the inventory in "
                     "`Downloads\\hjr-folder-audit`.")
    lines.append("")
    lines.append("## Week over week")
    lines.append("")
    lines.append("| Category | Now | Last | Delta | New | LEX | KB-pinned | In _archive |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for key, label in (*CATEGORY_LABELS.items(), *TREND_LABELS.items()):
        d, c = diffs[key], now[key]
        last = "n/a" if d.last is None else str(d.last)
        new = "n/a" if d.last is None else str(len(d.new))
        lines.append(f"| {label} | {d.now} | {last} | {_fmt_delta(d.delta)} | {new} | {c.lex} | {c.pinned} | {c.archive} |")
    lines.append("")
    ext = now.ext
    if ext:
        lines.append("## Drive-suffix files by extension (outside `_archive`, non-LEX)")
        lines.append("")
        for sfx in sorted(ext):
            top = sorted(ext[sfx].items(), key=lambda kv: (-kv[1], kv[0]))
            shown = ", ".join(f"{e} {n}" for e, n in top[:8])
            rest = sum(n for _, n in top[8:])
            if rest:
                shown += f", other {rest}"
            lines.append(f"- {sfx}: {shown}")
        lines.append("")
    for key, label in CATEGORY_LABELS.items():
        c, d = now[key], diffs[key]
        if not c.count:
            continue
        sl = screen(c.items, d.new)
        lines.append(f"## {label} ({c.count})")
        lines.append("")
        shown = sl.listed[:cap]
        n_new = sum(1 for _, is_new in sl.listed if is_new)
        if d.last is not None:
            lines.append(f"NEW this week: {len(d.new)} (listable {n_new}); resolved since last run: {d.resolved}.")
            lines.append("")
        for rel, is_new in shown:
            lines.append(f"- {'NEW ' if is_new and d.last is not None else ''}{_md_path(rel)}")
        notes = []
        if len(sl.listed) > cap:
            notes.append(f"{len(sl.listed) - cap} more in the full-list sidecar")
        if sl.lex:
            notes.append(f"{sl.lex} LEX (counts only)")
        if sl.pinned:
            notes.append(f"{sl.pinned} under KB-pinned containers (counts only)")
        if sl.phi:
            notes.append(f"{sl.phi} withheld by the PHI screen")
        if notes:
            lines.append("")
            lines.append("_" + "; ".join(notes) + "._")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_incomplete_md(*, date: str, stamp: str, reasons: list[str], files_total: int | None,
                         dirs_total: int | None, prior_stamp: str | None, prior_files: int | None,
                         walk_errors: int | None, rebaseline_stamp: str | None = None) -> str:
    """The report for a walk that failed the sanity floor. Deliberately carries NO
    offender lists and NO deltas: a partial walk reads as a false improvement
    (the "0 violations" failure mode), so nothing here may look like a result.
    ``rebaseline_stamp`` (set only when the walk passed every check of its own
    and a re-baseline onto it would be accepted) prints the operator's recovery
    commands instead of the mount-health next step."""
    lines = [GENERATOR_MARKER, "",
             f"# [Hygiene] Drive -- {date} (Cora weekly inventory): INCOMPLETE WALK", "",
             "This week's inventory did NOT pass the sanity floor, so this report carries no "
             "offender lists and no week-over-week deltas. Treat every category as UNKNOWN "
             "this week -- not as improved.", ""]
    lines.append(f"- Run stamp: `{stamp}`")
    for r in reasons:
        lines.append(f"- Reason: {r}")
    lines.append(f"- Walked: {files_total if files_total is not None else '?'} files, "
                 f"{dirs_total if dirs_total is not None else '?'} folders, "
                 f"{walk_errors if walk_errors is not None else '?'} walk errors")
    if prior_stamp:
        lines.append(f"- Prior full run: stamp `{prior_stamp}` ({prior_files} files)")
    if rebaseline_stamp:
        # D-051 R2 rb2#r2-0 / lens#r2-1: the walk itself is sound and only the
        # COMPARISON failed (the high-water floor, or an unreadable prior), which
        # re-running can never clear -- so name both causes and the recovery.
        lines.append("- Next step: this walk is internally consistent (COMPLETE, full root, 0 walk errors, "
                     "CSV row counts match the summary); only the comparison with earlier runs failed. "
                     "If the G: mount was degraded (a partial sync), re-run "
                     "`scripts\\run_hygiene_drive_weekly.py --apply` once it is healthy. If the change is "
                     "real and intended (a folder moved out of the tree, a purge), accept it: "
                     f"`scripts\\run_hygiene_drive_weekly.py --rebaseline {rebaseline_stamp} --apply`, then "
                     f"`scripts\\run_hygiene_drive_weekly.py --from-stamp {rebaseline_stamp} --apply` rebuilds "
                     f"this report. The re-baseline is recorded in the stamp ledger, and no run older than "
                     f"`{rebaseline_stamp}` (the 2026-09-21 baseline included) anchors the floor again.")
    else:
        lines.append("- Next step: re-run `scripts\\run_hygiene_drive_weekly.py --apply` once the G: mount is healthy.")
    return "\n".join(lines) + "\n"


def has_generator_marker(text: str) -> bool:
    head = (text or "").lstrip("﻿").splitlines()[:5]
    return any(line.strip() == GENERATOR_MARKER for line in head)


# ── sidecars ─────────────────────────────────────────────────────────────────

def render_full_list_csv(now: CategorySet, diffs: dict[str, Diff]) -> tuple[bytes, dict[str, int]]:
    """Every listable offender (non-LEX, PHI-screened) for the Downloads sidecar.
    Pinned-container rows ARE included (Downloads is not an ingest door) but
    flagged in ``zone``; LEX and PHI hits are counted, never written."""
    buf = io.StringIO(newline="")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["category", "status", "zone", "relpath"])
    withheld = {"lex": 0, "phi": 0}
    for key in CATEGORY_LABELS:
        c, d = now[key], diffs[key]
        for rel in sorted(c.items, key=str.lower):
            if is_lex_relpath(rel):
                withheld["lex"] += 1
                continue
            if is_phi_segment_path(rel) or phi_hit(rel):
                withheld["phi"] += 1
                continue
            zone = "kb-pinned" if pinned_container(rel) else ""
            status = "n/a" if d.last is None else ("NEW" if rel in d.new else "standing")
            w.writerow([key, status, zone, rel])
    return b"\xef\xbb\xbf" + buf.getvalue().encode("utf-8"), withheld


def desktop_ini_rows(files: list[dict]) -> tuple[list[dict], int]:
    """DELETE rows (reason D3-desktopini) for every NON-LEX desktop.ini -- apply.ps1
    gate 2 refuses a manifest with any 08- path, and gate 3 allows DELETE only for
    desktop.ini by name. sha256 is empty (a NO-HASH run). Returns (rows, lex_count)."""
    rows: list[dict] = []
    lex = 0
    for r in files:
        if str(r.get("IsDesktopIni", "")).lower() != "true":
            continue
        rel = r.get("RelPath", "")
        if is_lex_partition(rel):
            lex += 1
            continue
        if (r.get("Name", "") or "").lower() != "desktop.ini":
            continue
        rows.append({
            "action": "DELETE", "source_relpath": rel, "dest_relpath": "",
            "reason_code": "D3-desktopini", "sha256": "", "bytes": r.get("Bytes", ""),
            "keep_relpath": "", "is_md": "False", "top_level": r.get("TopLevel", ""),
        })
    return rows, lex
