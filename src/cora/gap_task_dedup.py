"""Gap-executor task dedup + create planning (Code #15 S2, cq-22b84598aee8).

THE INCIDENT. The knowledge-review gap executor turned every approved
"[ENT] Drive doc suggests missing task: ..." card into an Asana task with NO
project and NO assignee: `create_task(name, notes)` only. As of 2026-09-24 it had
made 48 such creates, 32 still open, every one `projects=[]`, `assignee=null` --
invisible to project views, to `get_user_tasks`, and therefore to pass-5's own
LLM-side dedup (which is shown only ASSIGNED tasks). The same gaps kept coming
back as LLM paraphrases of one daily digest, and each re-proposal was a new
update_id, so the exact-id dedup in `propose_update` never fired.

WHAT THIS MODULE OWNS
  * `normalize` / `extract_ids` -- the measured normalizer (below).
  * two matchers: `tier_a` (auto-suppress at proposal, refuse at execution) and
    `tier_b` (listed on the card / in the post, NEVER suppresses).
  * the propose-once / created ledger at env GAP_TASK_FP_PATH, resolved per call.
  * `plan_create` -- the pure project + assignee planner (refuse, never orphan).

TWO TIERS, MEASURED -- NOT ONE. Replayed over the live 430-row pass-5 corpus
(recon S2, 2026-09-24): a safe normalizer collapses the figure/date drift ("...
($33,487 as of 2026-08-27)" == "..."), which is tier A. The LLM paraphrases
(Buzzelli, FeedSpot, "whey ... wholesale price increase") are only merged by
matchers loose enough to ALSO merge distinct gaps in the same corpus (January vs
February sponsorship posts, two REP Fitness shoot sub-tasks, awning vs window work
at one address). So those are tier B: shown to a human, never auto-suppressed. A
suppressed gap is never seen again, which is the failure that matters.

THE SOURCE DOC IS NOT IN THE KEY. Most pass-5 gaps cite a DAILY digest whose name
carries that day's date, so every drift variant of one family comes from a
different source file. ANDing the source into the key would match nothing. It is
display-only (the row's own payload.source_filename).

FAIL DIRECTIONS, NAMED. A ledger READ failure fails OPEN (a duplicate is
recoverable, a suppressed real gap is not -- the fact_fingerprint rule). A ledger
WRITE failure is fail-SOFT (never blocks the proposal / create it records).

LEAF MODULE. No cora import at module level (reconciliation_engine imports this at
its top; the planner and the bootstrap import their collaborators lazily). Every
regex is bounded -- linear on degenerate input (D-171) -- and input is capped at
_MAX_INPUT characters before any of them runs.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_LEDGER_DEFAULT = _REPO_ROOT / "data" / "state" / "gap-task-fingerprints.jsonl"

# How far back the ledger is consulted (the fact_fingerprint window).
DEFAULT_WINDOW_DAYS = 120
# Only pass-5 Drive gaps are seeded into / recorded by the propose-once side of
# the ledger: that is the corpus the matchers were measured on. `created` rows
# are written for EVERY asana_task create (a task that exists is a duplicate of
# anything that repeats it, whichever lane proposed it).
PASS5_PREFIX = "pass5:drive:"

_MAX_INPUT = 300
_MAX_SUBJECT_STORED = 240

# ── normalizer ───────────────────────────────────────────────────────────────

# "[OSN] Drive doc suggests missing task: " (the legacy card/task-name prefix) or a
# bare "[OSN] " task-name prefix. Bounded; anchored.
_PREFIX_RE = re.compile(
    r"^\s{0,4}\[([A-Za-z0-9-]{2,12})\]\s{0,4}"
    r"(?:drive\s{1,3}doc\s{1,3}suggests\s{1,3}missing\s{1,3}task\s{0,2}:\s{0,4})?",
    re.IGNORECASE,
)
_MONEY_RE = re.compile(
    r"~?\$\s?\d[\d,]{0,15}(?:\.\d{1,4})?\s?(?:[kKmM]\b)?(?:\s?/\s?[A-Za-z]{1,10})?"
)
_PCT_RE = re.compile(r"\b\d{1,6}(?:\.\d{1,4})?\s?%")
_ISO_DATE_RE = re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b")
_MD_DATE_RE = re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b")
_MONTH_DATE_RE = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]{0,6}\.?\s{1,3}"
    r"(\d{1,2})(?:st|nd|rd|th)?(?:\s{0,2},?\s{0,3}\d{4})?\b",
    re.IGNORECASE,
)
_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}
_PAREN_RE = re.compile(r"\([^()]{0,120}\)")
_CLAUSE_RE = re.compile(r"\b(?:as of|currently|pending since|overdue since)\b.*$",
                        re.IGNORECASE)
# The clause keywords themselves (every occurrence), stripped before the dropped
# clause's content words are collected (drift_identity, D-051 r2 s2#r2-2).
_CLAUSE_KW_RE = re.compile(r"\b(?:as of|currently|pending since|overdue since)\b",
                           re.IGNORECASE)
# Split ONCE on a SPACED hyphen / en dash / em dash ("day-over-day" is not split).
_DASH_SPLIT_RE = re.compile(r"\s[-–—]\s")
_NUMRUN_RE = re.compile(r"\d[\d,]{0,24}")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

# Popped from the END of the normalized string only.
_TRAILING_FUNCTION = frozenset({
    "effective", "due", "as", "of", "on", "by", "since", "currently", "from", "to",
    "at", "and", "the", "a", "for", "with", "vs", "per", "unit",
})
# Never content. Deliberately small: an over-eager stop list makes two different
# tasks look identical (the fact_fingerprint lesson).
_STOP = frozenset({
    "a", "an", "and", "as", "at", "be", "by", "for", "from", "in", "into", "is",
    "it", "its", "of", "on", "or", "per", "that", "the", "this", "to", "via", "vs",
    "with", "due", "since", "currently", "effective", "unit",
})
# Tokens that carry no discriminating signal for the tier-B "shared token" /
# "shared bigram" rules: task verbs, admin nouns, and street-address words (the
# awning-vs-window pair at one address shares ONLY "gilbert rd" -- measured).
_GENERIC = frozenset({
    "follow", "up", "complete", "completion", "process", "processing", "review",
    "confirm", "confirmation", "execute", "execution", "implement", "implementation",
    "monitor", "track", "tracking", "update", "schedule", "scheduling", "coordinate",
    "coordination", "finalize", "resolve", "resolution", "obtain", "create",
    "establish", "manage", "management", "verify", "verification", "send", "submit",
    "file", "collect", "set", "setup", "build", "approve", "approval", "prepare",
    "address", "decide", "decision", "assign", "add", "get", "make", "check",
    "ensure", "handle", "request", "pay", "payment", "sign", "signature",
    "document", "documentation", "organize", "plan", "planning", "launch",
    "deliver", "delivery", "audit", "reconcile", "reconciliation", "investigate",
    "clarify", "identify", "evaluate", "assess", "determine", "provide", "contact",
    "call", "email", "meeting", "task", "item", "status", "issue", "date",
    "change", "project", "deliverable", "invoice", "pending", "overdue", "new",
    "next", "weekly", "monthly", "daily", "quarterly", "annual", "recurring",
    "detail", "information", "strategy", "timeline", "team", "role", "account",
    "content", "incomplete", "action", "required", "final", "draft",
    "rd", "st", "ave", "blvd", "dr", "suite", "location", "store", "facility",
    # entity / founder tokens: inside ONE entity's scope they name everything
    # ("F3 Pure Variety ..." vs "... F3 Pure product tag" is not a near-dup).
    "f3", "f3e", "hjr", "osn", "bdm", "ufl", "harrison", "rogers",
})
_SUFFIXES = ("ation", "ment", "ing", "s")


def strip_task_prefix(text: str) -> str:
    """Drop a leading "[ENT] " and the legacy "Drive doc suggests missing task: "."""
    s = str(text or "")[:_MAX_INPUT]
    return _PREFIX_RE.sub("", s, count=1)


def _remove_figures(s: str) -> str:
    s = _MONEY_RE.sub(" ", s)
    s = _PCT_RE.sub(" ", s)
    s = _ISO_DATE_RE.sub(" ", s)
    s = _MD_DATE_RE.sub(" ", s)
    s = _MONTH_DATE_RE.sub(" ", s)
    return s


def _raw_tokens(s: str) -> list[str]:
    return _NON_ALNUM_RE.sub(" ", str(s or "").lower()).split()


def _content(tokens: Iterable[str]) -> list[str]:
    return [t for t in tokens if len(t) >= 2 and t not in _STOP]


def _split_figure_tail(s: str) -> tuple[str, str]:
    """(kept, dropped_tail). Split once on a spaced dash; drop the tail only when
    it carries a figure or date AND at most 2 content tokens remain once the
    figures are gone. `dropped_tail` is '' when nothing is dropped.

    The 2-token floor is what removed the one false merge the recon found: "REP
    Fitness ... shoot -- finalize filming location and timing for week of 7/30"
    keeps its (figure-bearing but contentful) tail, so it no longer collapses onto
    its sibling sub-task."""
    m = _DASH_SPLIT_RE.search(s)
    if not m:
        return s, ""
    tail = s[m.end():]
    if not re.search(r"\d", tail):
        return s, ""
    rest = _NUMRUN_RE.sub(" ", _remove_figures(tail))
    if len(_content(_raw_tokens(rest))) <= 2:
        return s[:m.start()], tail
    return s, ""


def _drop_figure_tail(s: str) -> str:
    return _split_figure_tail(s)[0]


def normalize(text: str) -> str:
    """The tier-A dedup key: prefix-stripped, figure/date drift removed, lowercased.
    The words it drops are NOT all drift -- `drift_identity` guards all three
    word-drop sites: parentheticals, a figure dash tail (s2#2) and the
    "as of / currently / pending since / overdue since" clause (r2 s2#r2-2)."""
    s = strip_task_prefix(text)
    s = _drop_figure_tail(s)
    s = _PAREN_RE.sub(" ", s)
    s = _remove_figures(s)
    s = _CLAUSE_RE.sub(" ", s)
    toks = _raw_tokens(s)
    while toks and toks[-1] in _TRAILING_FUNCTION:
        toks.pop()
    return " ".join(toks)


def extract_ids(text: str) -> frozenset[str]:
    """Numbers of 3+ digits in the RAW text (parentheticals included), after money,
    percentages and dates are removed. Two non-empty, unequal id sets mean two
    DISTINCT tasks (SAS Electric Q2 vs Q3 invoices; DLC #2077 vs #2101)."""
    s = _remove_figures(strip_task_prefix(text))
    out = set()
    for m in _NUMRUN_RE.finditer(s):
        d = m.group(0).replace(",", "")
        if len(d) >= 3:
            out.add(d)
    return frozenset(out)


def body_dates(text: str) -> frozenset[str]:
    """Month/day of every date that SURVIVES the drift removal (parentheticals,
    a figure-only dash tail, an "as of ..." clause). A date there is identity,
    not drift: "Process payroll for 9/2/26" and "... for 9/16/26" are two payroll
    runs, and two non-empty, unequal body-date sets mean two DISTINCT tasks --
    the same guard as the id sets. (Code #15 S2 D-051 self-review: without it
    every date-stamped recurring task would suppress next week's for 120 days.)
    Measured: no tier-A pair in the 430-row corpus changes."""
    s = strip_task_prefix(text)
    s = _drop_figure_tail(s)
    s = _PAREN_RE.sub(" ", s)
    s = _CLAUSE_RE.sub(" ", s)
    out = set()
    for m in _ISO_DATE_RE.finditer(s):
        parts = m.group(0).split("-")
        out.add(f"{int(parts[1])}/{int(parts[2])}")
    for m in _MD_DATE_RE.finditer(s):
        parts = m.group(0).split("/")
        out.add(f"{int(parts[0])}/{int(parts[1])}")
    for m in _MONTH_DATE_RE.finditer(s):
        mo = _MONTHS.get(m.group(1)[:3].lower())
        if mo:
            out.add(f"{mo}/{int(m.group(2))}")
    return frozenset(out)


def drift_identity(text: str) -> tuple[frozenset, frozenset, frozenset]:
    """(parenthetical words, dropped-tail words, dropped-clause words): the content
    words the tier-A key throws away that can still NAME the task. Stemmed; never
    used for matching, only as a guard.

    D-051 r1 s2#2. `normalize` deletes every parenthetical and every short
    figure-bearing dash tail -- right for the drift it was measured on ("($33,487
    as of 2026-08-27)", "-- cash dropped $23,186 to $35,337"), wrong when those
    words are the only thing telling two tasks apart: "(Mango flavor)" vs "(Berry
    flavor)", "(morning shift)" vs "(closing shift)", "- $4,800 setup fee" vs "-
    $2,100 monthly storage" all came out as tier A (suppressed for 120 days). Only
    a DIGIT-FREE parenthetical counts (a figure-bearing one is the measured drift;
    its ids are already guarded by `extract_ids`), and only a tail `normalize`
    really DROPS. Two non-empty, unequal sets = no tier A -- the pair falls to
    tier B and is listed, never suppressed (the id / body-date rule).

    D-051 r2 s2#r2-2: the THIRD drop site. `_CLAUSE_RE` deletes everything from
    the first "as of|currently|pending since|overdue since" to the end, so "...
    bug currently affecting Apple Pay" vs "... discount codes" -- and, with a
    mid-string keyword, "Update the currently active price list for Target" vs
    "... wholesale agreement with Costco" (key: "update") -- were tier A. The
    clause is read exactly where `normalize` drops it (after the tail, the
    parentheticals and the figures), minus the keywords and digit runs, so a
    figure-only clause ("as of 2026-08-27", "currently $35,337") stays empty and
    the measured drift still collapses."""
    s = strip_task_prefix(text)
    kept, tail = _split_figure_tail(s)
    paren: set[str] = set()
    for m in _PAREN_RE.finditer(kept):
        inner = m.group(0)
        if re.search(r"\d", inner):
            continue
        paren.update(stem(t) for t in _content(_raw_tokens(inner)))
    tail_words: set[str] = set()
    if tail:
        rest = _NUMRUN_RE.sub(" ", _remove_figures(tail))
        tail_words.update(stem(t) for t in _content(_raw_tokens(rest)))
    clause_words: set[str] = set()
    cm = _CLAUSE_RE.search(_remove_figures(_PAREN_RE.sub(" ", kept)))
    if cm:
        rest = _NUMRUN_RE.sub(" ", _CLAUSE_KW_RE.sub(" ", cm.group(0)))
        clause_words.update(stem(t) for t in _content(_raw_tokens(rest)))
    return frozenset(paren), frozenset(tail_words), frozenset(clause_words)


def stem(tok: str) -> str:
    """Iteratively strip -ation/-ment/-ing/-s (implement == implementation).
    Idempotent: the loop only stops once no suffix applies."""
    t = str(tok or "")
    changed = True
    while changed:
        changed = False
        for suf in _SUFFIXES:
            if t.endswith(suf) and len(t) - len(suf) >= 3:
                t = t[: -len(suf)]
                changed = True
                break
    return t


_GENERIC_STEMS = frozenset(stem(g) for g in _GENERIC)


def _is_generic(stemmed: str) -> bool:
    return stemmed in _GENERIC_STEMS or stemmed.isdigit()


@dataclass(frozen=True)
class Sig:
    norm: str
    ids: frozenset
    dates: frozenset         # body dates (identity, not drift) -- see body_dates
    tokens: tuple            # normalized tokens, order kept (prefix rule)
    content_n: int           # content-token count incl. digits (prefix floor)
    stems: frozenset         # stemmed content tokens incl. digits (token-set rule)
    words: frozenset         # stemmed NON-digit content tokens (tier-B shared rule)
    bigrams: frozenset       # stemmed non-generic adjacent pairs (tier-B bigram rule)
    paren: frozenset = frozenset()   # digit-free parenthetical words (tier-A guard only)
    tail: frozenset = frozenset()    # dropped dash-tail words (tier-A guard only)
    clause: frozenset = frozenset()  # dropped "as of / currently ..." clause words (ditto)


@lru_cache(maxsize=8192)
def signature(text: str) -> Sig:
    norm = normalize(text)
    toks = tuple(norm.split())
    content = _content(toks)
    stems = [stem(t) for t in content]
    words = [s for s in stems if not s.isdigit()]
    bigrams = frozenset(
        (a, b) for a, b in zip(words, words[1:])
        if not _is_generic(a) and not _is_generic(b)
    )
    paren, tail, clause = drift_identity(text)
    return Sig(norm=norm, ids=extract_ids(text), dates=body_dates(text), tokens=toks,
               content_n=len(content), stems=frozenset(stems), words=frozenset(words),
               bigrams=bigrams, paren=paren, tail=tail, clause=clause)


def _id_conflict(a: Sig, b: Sig) -> bool:
    if a.ids and b.ids and a.ids != b.ids:
        return True
    return bool(a.dates) and bool(b.dates) and a.dates != b.dates


def _drift_conflict(a: Sig, b: Sig) -> bool:
    """The words the tier-A key dropped disagree (see drift_identity). Gates tier
    A ONLY: such a pair is still listed as tier B, never suppressed."""
    if a.paren and b.paren and a.paren != b.paren:
        return True
    if a.clause and b.clause and a.clause != b.clause:
        return True
    return bool(a.tail) and bool(b.tail) and a.tail != b.tail


def _is_prefix(short: tuple, long_: tuple) -> bool:
    return len(short) < len(long_) and long_[: len(short)] == short


def tier_a(a_text: str, b_text: str) -> bool:
    """Same task, auto-suppressible. Caller guarantees the same entity."""
    a, b = signature(str(a_text or "")), signature(str(b_text or ""))
    if not a.norm or not b.norm or _id_conflict(a, b) or _drift_conflict(a, b):
        return False
    if a.norm == b.norm:
        return True
    if len(a.stems) >= 3 and a.stems == b.stems:
        return True
    short, long_ = (a, b) if len(a.tokens) <= len(b.tokens) else (b, a)
    return short.content_n >= 4 and _is_prefix(short.tokens, long_.tokens)


def tier_b(a_text: str, b_text: str) -> bool:
    """Possibly the same task -- SHOWN, never suppressed. Includes tier A."""
    a, b = signature(str(a_text or "")), signature(str(b_text or ""))
    if not a.norm or not b.norm or _id_conflict(a, b):
        return False
    if tier_a(a_text, b_text):
        return True
    shared = a.words & b.words
    specific = [t for t in shared if not _is_generic(t)]
    # >=3 shared, >=2 of them specific, one specific of 4+ chars. Without the
    # "2 specific" floor, "Follow up on IRS hearing ... post-decision" listed
    # against "Follow up on ... sponsorship posts" on {follow, up, post} (measured).
    if len(shared) >= 3 and len(specific) >= 2 and any(len(t) >= 4 for t in specific):
        return True
    if a.bigrams & b.bigrams:
        return True
    try:
        from . import fact_fingerprint  # leaf module, no cycle
        return bool(fact_fingerprint.same_fact(a.norm, b.norm))
    except Exception:  # noqa: BLE001 -- a listing aid is never worth a crash
        return SequenceMatcher(None, a.norm, b.norm).ratio() >= 0.85


def match_tier(a_text: str, b_text: str) -> str:
    """'A', 'B' or ''."""
    if tier_a(a_text, b_text):
        return "A"
    return "B" if tier_b(a_text, b_text) else ""


# ── row helpers ──────────────────────────────────────────────────────────────

def entity_of_description(desc: str) -> str:
    m = _PREFIX_RE.match(str(desc or ""))
    return m.group(1).upper() if m else ""


def subject_of(update: dict | None) -> str:
    """The task subject of a proposed-updates row: payload.subject (pass-5 rows
    since S2, drive_extractor rows) -> payload.suggested_task_name (pass-1) -> the
    description with the "[ENT] Drive doc suggests missing task: " prefix stripped
    (the legacy pass-5 rows, whose payload is empty)."""
    u = update or {}
    payload = u.get("payload") if isinstance(u.get("payload"), dict) else {}
    for key in ("subject", "suggested_task_name"):
        v = str(payload.get(key) or "").strip()
        if v:
            return strip_task_prefix(v).strip()[:_MAX_INPUT]
    return strip_task_prefix(str(u.get("description") or "")).strip()[:_MAX_INPUT]


def _parse_ts(v: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(v or "").replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def ref_of(row: dict) -> str:
    """The id a near-dup is known by: an Asana gid when one exists, else the
    proposal's update_id."""
    return str(row.get("gid") or row.get("ref") or "")


# ── ledger ───────────────────────────────────────────────────────────────────

def ledger_path() -> Path:
    """Resolved PER CALL (the cq-06f4797db4f1 trap: an import-time snapshot of the
    env var would make the conftest redirect a no-op)."""
    return Path(os.environ.get("GAP_TASK_FP_PATH") or _LEDGER_DEFAULT)


# In-memory bootstrap, per ledger path, for runs that must not write (a dry run
# builds it here and nowhere else). Cleared by any persist.
_BOOT_CACHE: dict[str, list[dict]] = {}


def _proposed_updates_files() -> list[Path]:
    from . import knowledge_review as kr  # lazy: bot-loaded module, big import
    # Module attributes read at CALL time, so the conftest redirect applies.
    return [Path(kr._PROPOSED_UPDATES_PATH), Path(kr._ARCHIVE_PATH)]


def bootstrap_rows() -> list[dict]:
    """The one-time seed: every pass-5 missing-task row in BOTH proposed-updates
    files (live wins over archive on a repeated update_id), as `proposed` rows at
    their own proposed_at. Any state -- D-030 propose-once. LEX never enters
    (pass-5 skips LEX; this is the belt). Read once, never per candidate (the
    18 MB archive, the fact_fingerprint warning)."""
    seen: set[str] = set()
    out: list[dict] = []
    for path in _proposed_updates_files():
        try:
            if not path.exists():
                continue
            fh = path.open(encoding="utf-8")
        except OSError:
            log.warning("gap_task_dedup: bootstrap source unreadable (%s)", path.name)
            continue
        with fh:
            for line in fh:
                if PASS5_PREFIX not in line:
                    continue  # cheap pre-filter before json over an 18 MB file
                try:
                    row = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(row, dict):
                    continue
                uid = str(row.get("update_id") or "")
                if not uid.startswith(PASS5_PREFIX) or uid in seen:
                    continue
                if row.get("update_type") != "asana_task":
                    continue
                ent = entity_of_description(str(row.get("description") or ""))
                if not ent or ent.startswith("LEX"):
                    continue
                subj = subject_of(row)
                if not subj:
                    continue
                seen.add(uid)
                out.append({
                    "ts": str(row.get("proposed_at") or ""),
                    "kind": "proposed",
                    "ref": uid,
                    "entity": ent,
                    "state": str(row.get("state") or ""),
                    "subject": subj[:_MAX_SUBJECT_STORED],
                    "key": normalize(subj),
                    "via": "bootstrap",
                })
    return out


def _write_bootstrap(path: Path, rows: list[dict]) -> bool:
    marker = {"ts": datetime.now(timezone.utc).isoformat(), "kind": "bootstrap",
              "rows": len(rows), "sources": 2}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(json.dumps(marker) + "\n")
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
        if path.exists():  # a concurrent writer won; never clobber its rows
            tmp.unlink(missing_ok=True)
            return False
        tmp.replace(path)
        log.info("gap_task_dedup: ledger bootstrapped with %d row(s)", len(rows))
        return True
    except Exception:  # noqa: BLE001 -- fail-soft
        log.warning("gap_task_dedup: ledger bootstrap write failed", exc_info=True)
        return False


def ensure_bootstrapped() -> int:
    """Seed the ledger on disk if it is absent. Returns rows written (0 when the
    ledger already existed or the seed could not be read or written). Call only
    on a path that is allowed to write -- never from a dry run.

    FAIL-SOFT, and it never loses the seed (D-051 r1 s2#0): a raising
    `bootstrap_rows` is caught here (it used to escape through `_append` into
    `record_created` AFTER the Asana create), and the in-memory seed is dropped
    only once a write has succeeded -- a failed write keeps it for the next try."""
    path = ledger_path()
    if path.exists():
        return 0
    key = str(path)
    rows = _BOOT_CACHE.get(key)
    if rows is None:
        try:
            rows = bootstrap_rows()
        except Exception:  # noqa: BLE001 -- fail-soft: the ledger stays absent
            log.warning("gap_task_dedup: bootstrap read failed -- ledger NOT seeded "
                        "(fail-soft; retried on the next write)", exc_info=True)
            return 0
    if _write_bootstrap(path, rows):
        _BOOT_CACHE.pop(key, None)
        return len(rows)
    _BOOT_CACHE[key] = rows
    return 0


def _read_rows(path: Path, window_days: int) -> list[dict]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
    out: list[dict] = []
    try:
        raw = path.read_text(encoding="utf-8").splitlines()
    except Exception:  # noqa: BLE001 -- fail OPEN
        log.warning("gap_task_dedup: ledger read failed (allowing)", exc_info=True)
        return []
    for line in raw:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(rec, dict) or rec.get("kind") not in ("proposed", "created"):
            continue
        ts = _parse_ts(rec.get("ts"))
        if ts is None or ts < cutoff:
            continue
        out.append(rec)
    return out


def ledger_rows(*, persist: bool, window_days: int = DEFAULT_WINDOW_DAYS) -> list[dict]:
    """The in-window `proposed` + `created` rows. Absent ledger -> the one-time
    bootstrap: written to disk when `persist`, otherwise kept in memory only (a dry
    run writes nothing). Fail-OPEN on any read error ([] = nothing suppressed)."""
    path = ledger_path()
    try:
        if not path.exists():
            if persist:
                ensure_bootstrapped()
            if not path.exists():
                key = str(path)
                if key not in _BOOT_CACHE:
                    _BOOT_CACHE[key] = bootstrap_rows()
                cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
                out = []
                for r in _BOOT_CACHE[key]:
                    ts = _parse_ts(r.get("ts"))
                    if ts is not None and ts >= cutoff:
                        out.append(r)
                return out
        return _read_rows(path, window_days)
    except Exception:  # noqa: BLE001 -- fail OPEN
        log.warning("gap_task_dedup: ledger unavailable (allowing)", exc_info=True)
        return []


def _append(row: dict) -> bool:
    """Append one row -- ONLY to a ledger that already exists (seeded).

    The ledger's existence is what stops every later seed attempt, so an append
    must never be the thing that creates it (D-051 r1 s2#0: a failed seed write
    followed by an `open("a")` created a ledger holding only the new row, and the
    430-row propose-once seed was lost for good). When the seed cannot be written
    this row is dropped, fail-soft: the next write retries the seed, which is
    rebuilt from the proposed-updates files (so a proposal is not lost), and a
    created task is still caught by the executor's in-run set and project scan."""
    try:
        path = ledger_path()
        if not path.exists():
            ensure_bootstrapped()  # never let a first append pre-empt the seed
        if not path.exists():
            log.warning("gap_task_dedup: ledger not seeded -- %s row NOT written "
                        "(fail-soft)", row.get("kind") or "?")
            return False
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except Exception:  # noqa: BLE001 -- fail-soft
        log.warning("gap_task_dedup: ledger write failed", exc_info=True)
        return False


def record_proposal(*, gap_id: str, entity: str, subject: str) -> bool:
    """Append one `proposed` row -- called by the RUNNER after propose_update
    succeeded (never at gap-build time: a dry run builds gaps and proposes
    nothing). Skips a ref already present, so a re-run adds no rows."""
    ent = str(entity or "").strip().upper()
    subj = str(subject or "").strip()
    if not gap_id or not ent or ent.startswith("LEX") or not subj:
        return False
    try:
        if any(r.get("ref") == gap_id and r.get("kind") == "proposed"
               for r in ledger_rows(persist=True)):
            return False
    except Exception:  # noqa: BLE001
        pass
    return _append({
        "ts": datetime.now(timezone.utc).isoformat(), "kind": "proposed",
        "ref": gap_id, "entity": ent, "state": "PENDING",
        "subject": subj[:_MAX_SUBJECT_STORED], "key": normalize(subj),
    })


def record_created(*, update_id: str, entity: str, subject: str, gid: str,
                   url: str = "", project_gid: str = "", assignee_gid: str = "") -> bool:
    """Append one `created` row -- the executor, immediately after Asana answers.
    Never a LEX row (the executor refuses LEX before any create; this is the belt)."""
    subj = str(subject or "").strip()
    if str(entity or "").strip().upper().startswith("LEX"):
        return False
    return _append({
        "ts": datetime.now(timezone.utc).isoformat(), "kind": "created",
        "ref": str(update_id or ""), "gid": str(gid or ""), "url": str(url or ""),
        "entity": str(entity or "").strip().upper(),
        "project_gid": str(project_gid or ""), "assignee_gid": str(assignee_gid or ""),
        "subject": subj[:_MAX_SUBJECT_STORED], "key": normalize(subj),
    })


# ── queries ──────────────────────────────────────────────────────────────────

def created_by(update_id: str, rows: Iterable[dict]) -> dict | None:
    """The `created` row THIS update already produced (crash-recovery: Asana
    answered, the resolve never happened). Never a duplicate of itself."""
    for r in rows or ():
        if r.get("kind") == "created" and r.get("ref") == update_id and r.get("gid"):
            return r
    return None


def find_tier_a(entity: str, subject: str, rows: Iterable[dict], *,
                exclude_ref: str = "", before_ts: str | None = None) -> dict | None:
    """The first ledger row that is a tier-A repeat of (entity, subject).

    A `created` row always counts (the task exists, whichever proposal made it).
    A `proposed` row counts in ANY state (D-030 propose-once) -- but, when
    `before_ts` is given (execution time), only if it was proposed BEFORE this
    row: two legacy PENDING duplicates must not refuse each other forever."""
    ent = str(entity or "").strip().upper()
    if not ent or ent.startswith("LEX"):
        return None  # LEX never enters this lane; an unknown entity matches nothing
    cutoff = _parse_ts(before_ts) if before_ts is not None else None
    for r in rows or ():
        if str(r.get("entity") or "").upper() != ent:
            continue
        if exclude_ref and r.get("ref") == exclude_ref:
            continue
        if r.get("kind") == "proposed" and before_ts is not None:
            ts = _parse_ts(r.get("ts"))
            if cutoff is None or ts is None or ts >= cutoff:
                continue
        if tier_a(subject, str(r.get("subject") or r.get("key") or "")):
            return r
    return None


def near_duplicates(entity: str, subject: str, candidates: Iterable[dict], *,
                    exclude_ref: str = "", limit: int = 3) -> list[dict]:
    """Up to `limit` tier-B (incl. tier-A) matches, tier A first then newest,
    one entry per ref (a `created` row beats the `proposed` row it came from).
    Never lists anything for, or from, a LEX entity (PHI: a LEX title must not
    reach a card or #hjrg-leadership through this surface)."""
    ent = str(entity or "").strip().upper()
    if not ent or ent.startswith("LEX"):
        return []
    found: dict[str, tuple[int, str, dict]] = {}
    cands = list(candidates or ())
    created_refs = {str(r.get("ref") or "") for r in cands if r.get("kind") == "created"}
    for r in cands:
        if str(r.get("entity") or "").upper() != ent:
            continue
        ref = str(r.get("ref") or "")
        if exclude_ref and ref == exclude_ref:
            continue
        if r.get("kind") == "proposed" and ref in created_refs:
            continue  # the created row stands for it
        tier = match_tier(subject, str(r.get("subject") or r.get("key") or ""))
        if not tier:
            continue
        key = ref_of(r) or ref
        rank = 0 if tier == "A" else 1
        prior = found.get(key)
        if prior is None or rank < prior[0]:
            found[key] = (rank, str(r.get("ts") or ""), dict(r, tier=tier))
    ordered = sorted(found.values(), key=lambda x: (x[0], _neg_ts(x[1])))
    return [x[2] for x in ordered[: max(0, int(limit))]]


def _neg_ts(ts: str) -> float:
    dt = _parse_ts(ts)
    return -dt.timestamp() if dt else 0.0


# ── the create planner ───────────────────────────────────────────────────────

@dataclass
class Plan:
    entity: str = ""
    subject: str = ""
    task_name: str = ""
    project_gid: str = ""
    project_route: str = ""      # 'catch_all' | 'routed'
    assignee_slack: str = ""
    assignee_gid: str = ""
    assignee_source: str = ""    # 'owner' | 'default'
    refusal: str = ""            # '' or entity_unresolved | lex_out_of_scope |
                                 # no_project:<ENT> | no_assignee:<ENT> | empty_task_name

    @property
    def ok(self) -> bool:
        return not self.refusal


def _owner_source(entity: str) -> str:
    """'owner' when gap-domain-owners.yaml names this entity, else 'default'
    (resolve_owner's `default:` fallback). Read-only."""
    try:
        import yaml
        from . import gap_autofill
        data = yaml.safe_load(gap_autofill._owners_map_path().read_text(encoding="utf-8")) or {}
        owners = data.get("owners") or {}
        return "owner" if owners.get(entity) else "default"
    except Exception:  # noqa: BLE001
        return "default"


def plan_create(update: dict | None) -> Plan:
    """Project + assignee for an approved asana_task row, or a named refusal.

    Never orphans (the incident) and never hard-codes a project: the project comes
    from project_resolver over asana-project-map.yaml (the meeting-capture pull's
    resolver), the assignee from the entity's domain owner (gap-domain-owners.yaml
    -> slack-to-asana.yaml). An entity with no configured project (BDM, D-052) is
    REFUSED visibly rather than filed somewhere arbitrary. LEX is refused outright:
    a LEX create needs the LEX project resolver and a PHI scrub this lane lacks.
    Pure apart from reading those maps; no network."""
    u = update or {}
    plan = Plan()
    try:
        from . import review_lanes
        plan.entity = str(review_lanes.resolve_entity(u) or "").strip().upper()
    except Exception:  # noqa: BLE001
        log.warning("gap_task_dedup: entity resolver failed", exc_info=True)
        plan.entity = ""
    plan.subject = subject_of(u)
    payload = u.get("payload") if isinstance(u.get("payload"), dict) else {}
    suggested = str(payload.get("suggested_task_name") or "").strip()
    if not plan.entity:
        plan.refusal = "entity_unresolved"
        return plan
    if plan.entity.startswith("LEX"):
        plan.refusal = "lex_out_of_scope"
        return plan
    if suggested:
        plan.task_name = suggested[:150].strip()
    elif plan.subject:
        plan.task_name = f"[{plan.entity}] {plan.subject}"[:150].strip()
    if not plan.task_name:
        plan.refusal = "empty_task_name"
        return plan

    try:
        from .tools import project_resolver as pr
        gid = None
        try:
            gid = pr.resolve_project(plan.entity, task_text=plan.subject)
        except Exception:  # noqa: BLE001
            log.warning("gap_task_dedup: project_resolver failed", exc_info=True)
            gid = None
        if gid and pr.is_blocked_project(str(gid)):
            gid = None
        catch_all = pr.entity_catch_all(plan.entity)
        if not gid and catch_all and not pr.is_blocked_project(catch_all):
            gid = catch_all
    except Exception:  # noqa: BLE001
        log.warning("gap_task_dedup: project map unavailable", exc_info=True)
        gid, catch_all = None, None
    if not gid:
        plan.refusal = f"no_project:{plan.entity}"
        return plan
    plan.project_gid = str(gid)
    plan.project_route = "catch_all" if catch_all and str(gid) == str(catch_all) else "routed"

    try:
        from . import gap_autofill
        from .tools import user_identity
        slack = gap_autofill.resolve_owner(plan.entity) or ""
        agid = user_identity.asana_gid(slack) if slack else None
    except Exception:  # noqa: BLE001
        log.warning("gap_task_dedup: owner resolution failed", exc_info=True)
        slack, agid = "", None
    if not agid:
        plan.refusal = f"no_assignee:{plan.entity}"
        return plan
    plan.assignee_slack = str(slack)
    plan.assignee_gid = str(agid)
    plan.assignee_source = _owner_source(plan.entity)
    return plan


def refusal_text(code: str) -> str:
    """Human sentence for a refusal code (card + #hjrg-leadership post)."""
    c = str(code or "")
    if c.startswith("no_project:"):
        return f"no {c.split(':', 1)[1]} Asana project is configured"
    if c.startswith("no_assignee:"):
        return f"no Asana assignee is mapped for {c.split(':', 1)[1]}'s domain owner"
    if c == "entity_unresolved":
        return "I can't tell which entity this task belongs to"
    if c == "lex_out_of_scope":
        return "Lexington tasks are out of scope for this lane"
    if c == "empty_task_name":
        return "the task has no name"
    if c.startswith("duplicate_of:"):
        return "it repeats a task or proposal that already exists"
    return c or "unknown reason"
