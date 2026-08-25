"""C13 (cq-015b3bc779e9): which recurring vendor invoices are in hand at close.

WHAT THE SEED ASKED FOR AND WHAT VERIFY-FIRST FOUND. The ask was a "Google Ads
invoice retrieval lane into the finance receipts flow", on the premise that
accounting lacks those invoices at close. The premise is correct. The implied
cause -- that Cora has no lane to retrieve them -- is not.

  * THE LANE ALREADY WORKS. The attachment filer has been filing Google
    WORKSPACE invoices monthly, on its own, into `01-HJR-Global/invoices/` --
    verified in the live filer ledger for June, July and August 2026.
  * GOOGLE ADS INVOICES HAVE NEVER ARRIVED. Zero Ads billing documents in the
    filer ledger ever, and zero Ads billing emails in 120 days of the founder
    mailbox (only `ads-noreply@` performance nags). The Ads account is held by
    invitation from an address outside the org, so its billing documents go
    somewhere no monitored mailbox can see.

So the missing piece was never retrieval code. It is (a) that Google billing mail
did not score as a financial document once filed -- fixed in the classifier, same
slice -- and (b) that NOBODY IS TOLD when an expected invoice simply does not
turn up. A retrieval lane cannot fix a document that was never delivered; a
report can say so out loud, every month, until the delivery is fixed.

THIS MODULE IS THE (b) HALF. It reads a human-maintained expectation list, checks
the filer's own content ledger for a matching filing in the period, and returns
PRESENT / MISSING per vendor. Read-only: it reads two files and writes nothing.

WHY THE FILER LEDGER IS THE RIGHT SOURCE. It is the record of what was actually
FILED to Drive, which is what accounting needs to have in hand -- as opposed to
what arrived in a mailbox (a mailbox hit that failed to file is a miss for this
purpose) or what the KB tagged (a different lane, for retrieval rather than
custody).

MISSING IS NEVER SILENT AND NEVER INFERRED AS FINE. An unreadable expectation
list or an unreadable ledger produces an explicit UNKNOWN, not an empty
all-clear -- the blank-radar failure mode `finance-renewal-radar.yaml` warns
about in its own header, and the same rule the Standing-ACTUALS label doctrine
locked.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTATIONS_PATH = _REPO_ROOT / "data" / "maps" / "finance-expected-invoices.yaml"
LEDGER_PATH = _REPO_ROOT / "data" / "state" / "filer-content-ledger.jsonl"

#: What makes a filed document an INVOICE rather than merely a vendor document.
#: Deliberately narrow, and deliberately a regex over the already-lowercased path
#: rather than a substring list, so "invoices/" as a FOLDER also qualifies.
_DOC_KIND_RE = re.compile(r"invoice|receipt|statement|remittance|bill(?:ing)?")

STATUS_PRESENT = "PRESENT"
STATUS_MISSING = "MISSING"
STATUS_UNKNOWN = "UNKNOWN"


def load_expectations(path: Path | None = None) -> list[dict[str, Any]] | None:
    """The expectation list, or None when it cannot be read.

    None, not [] -- "we could not look" and "nothing is expected" are different
    facts and the report renders them differently.
    """
    target = path or EXPECTATIONS_PATH
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        log.warning("expected_invoices: list unreadable (%s)", exc)
        return None
    items = raw.get("expected") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        return None
    return [i for i in items if isinstance(i, dict)]


def _iter_ledger(path: Path | None = None):
    target = path or LEDGER_PATH
    try:
        with target.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:  # noqa: BLE001 -- one bad line is not a failure
                    continue
                if isinstance(row, dict) and "_schema" not in row:
                    yield row
    except FileNotFoundError:
        return
    except Exception as exc:  # noqa: BLE001
        log.warning("expected_invoices: ledger unreadable (%s)", exc)
        return


#: Arizona. Fixed -7 all year (no DST), which is why a plain offset is correct
#: here rather than a zoneinfo lookup -- the same constant meeting_actions uses.
_AZ = timezone(timedelta(hours=-7))


def period_bounds(period: str) -> tuple[int, int]:
    """(start_ts, end_ts_exclusive) for a YYYY-MM ACCOUNTING period.

    BOUNDED IN ARIZONA TIME, NOT UTC. The filer writes `filed_at` as a UTC epoch,
    which is fine -- an instant is an instant. What is NOT fine is deciding which
    MONTH that instant belongs to using UTC calendar boundaries, because the
    business closes its books in Arizona: a document filed 2026-07-31 at 21:00 AZ
    is 2026-08-01 04:00 UTC, so a UTC boundary attributed July's last-day invoice
    to August. That is a wrong answer in a close report, once a month, forever --
    and it fails in the dangerous direction, reporting July MISSING while the
    document is sitting in Drive.

    Raises ValueError on an unparseable period rather than guessing (the caller
    turns that into an honest UNAVAILABLE).
    """
    parts = str(period or "").split("-")
    if len(parts) < 2:
        raise ValueError(f"period must be YYYY-MM, got {period!r}")
    year, month = int(parts[0]), int(parts[1])
    if not 1 <= month <= 12:
        raise ValueError(f"month out of range in {period!r}")
    start = datetime(year, month, 1, tzinfo=_AZ)
    end = (datetime(year + 1, 1, 1, tzinfo=_AZ) if month == 12
           else datetime(year, month + 1, 1, tzinfo=_AZ))
    return int(start.timestamp()), int(end.timestamp())


def previous_period(today: date | None = None) -> str:
    """The last CLOSED month as YYYY-MM -- the period accounting is reconciling.
    The current month is still open, so an absent invoice in it is not yet news.

    READS THE ARIZONA DATE. Arizona is UTC-7, so for the last seven hours of every
    day the UTC date is already tomorrow. On 31 July at 18:00 AZ a UTC reading
    makes it 1 August, whose previous month is July -- the month that has NOT
    closed yet in Arizona. The report would then ask "is July's invoice filed?"
    before July is over, and answer MISSING.
    """
    day = today or datetime.now(_AZ).date()
    year, month = (day.year - 1, 12) if day.month == 1 else (day.year, day.month - 1)
    return f"{year:04d}-{month:02d}"


def _matches(row: dict, patterns: list[str]) -> bool:
    """Does this filing look like the expected vendor's invoice?

    TWO constraints, both required. The vendor patterns are matched against
    `drive_path` (the filed NAME the filer builds from the email) as plain
    case-insensitive SUBSTRINGS, never regexes -- they come from a human-maintained
    YAML file, so a stray `(` must not raise and must not hand an editor a ReDoS
    foot-gun on a path (five such regressions here already). AND the path must name
    a financial DOCUMENT, or any vendor-named file counts: without it
    "google-ads-strategy-deck.pdf" reported the Ads INVOICE as PRESENT, which is
    the dangerous direction -- a false PRESENT tells accounting a document is in
    hand and, unlike a false MISSING, nobody goes looking.
    """
    haystack = f"{row.get('drive_path') or ''} {row.get('canonical') or ''}".lower()
    if not any(p and p.lower() in haystack for p in patterns):
        return False
    # AND IT HAS TO BE AN INVOICE. Without this, any vendor-named document counts:
    # "google-ads-strategy-deck.pdf" would report the Google Ads INVOICE as
    # PRESENT. That is the dangerous direction -- a false PRESENT tells accounting
    # a document is in hand when it is not, and unlike a false MISSING nobody goes
    # looking. The filer names financial documents with one of these words (the
    # live ledger's Google filings are all "...-monthly-invoice.pdf").
    return bool(_DOC_KIND_RE.search(haystack))


def assess(period: str | None = None, *,
           expectations_path: Path | None = None,
           ledger_path: Path | None = None,
           today: date | None = None) -> dict[str, Any]:
    """Per-vendor PRESENT / MISSING for one period.

    Returns {"period", "available", "reason", "results": [...]}.
    `available=False` means the check could not run -- never confuse that with a
    clean result.
    """
    per = period or previous_period(today)
    out: dict[str, Any] = {"period": per, "available": True, "reason": "",
                           "results": []}

    items = load_expectations(expectations_path)
    if items is None:
        out.update(available=False,
                   reason="expectation list missing or unreadable "
                          "(data/maps/finance-expected-invoices.yaml)")
        return out
    if not items:
        out.update(available=False, reason="expectation list has no entries")
        return out

    try:
        start, end = period_bounds(per)
    except (ValueError, TypeError) as exc:
        # A typo'd --period is an honest UNAVAILABLE, not a traceback.
        out.update(available=False, reason=f"unusable period {per!r}: {exc}")
        return out
    rows = list(_iter_ledger(ledger_path))
    if not rows:
        # An empty ledger is not evidence that invoices are missing -- the filer
        # may never have run. Say UNKNOWN for every vendor rather than crying
        # MISSING across the board.
        for item in items:
            out["results"].append({
                "name": str(item.get("name") or "unnamed"),
                "entity": str(item.get("entity") or ""),
                "status": STATUS_UNKNOWN,
                "detail": "filer ledger is empty or unreadable",
                "known_undelivered": bool(item.get("known_undelivered")),
                "note": str(item.get("note") or ""),
            })
        return out

    for item in items:
        patterns = [str(p) for p in (item.get("match") or []) if str(p).strip()]
        name = str(item.get("name") or "unnamed")
        if not patterns:
            out["results"].append({
                "name": name, "entity": str(item.get("entity") or ""),
                "status": STATUS_UNKNOWN,
                "detail": "entry has no `match` patterns, so it cannot be checked",
                "known_undelivered": bool(item.get("known_undelivered")),
                "note": str(item.get("note") or ""),
            })
            continue
        hits = [
            r for r in rows
            if _matches(r, patterns)
            and isinstance(r.get("filed_at"), (int, float))
            and start <= int(r["filed_at"]) < end
        ]
        paths = sorted({str(h.get("drive_path") or "") for h in hits})
        out["results"].append({
            "name": name,
            "entity": str(item.get("entity") or ""),
            "status": STATUS_PRESENT if hits else STATUS_MISSING,
            "detail": (paths[0] if paths else ""),
            "filed_count": len(hits),
            "known_undelivered": bool(item.get("known_undelivered")),
            "note": str(item.get("note") or ""),
        })
    return out


#: How much of a known-undelivered note the Slack line carries. The rest stays in
#: the YAML, which is where a reader who wants the account id and the fix goes.
_NOTE_CHARS = 180


def _first_sentence(note: Any) -> str:
    """The note's first sentence, hard-bounded.

    Cut on a WORD boundary, never mid-word: this repo has shipped mid-word
    truncation on three separate surfaces (friction cards, long DMs, meeting
    previews) and each time it read as corruption rather than as brevity.
    """
    text = re.sub(r"\s+", " ", str(note or "")).strip()
    if not text:
        return "delivery not configured"
    match = re.search(r"(?<=[.!?])\s", text)
    if match and match.start() <= _NOTE_CHARS:
        # `match.start()` is the index OF the whitespace, so +1 kept it -- every
        # known_undelivered Slack line ended with a dangling space. Slice to the
        # boundary itself, which is already past the punctuation.
        return text[:match.start()]
    if len(text) <= _NOTE_CHARS:
        return text
    head = text[:_NOTE_CHARS]
    cut = head.rfind(" ")
    return (head[:cut] if cut > 40 else head).rstrip(" ,;:-") + " ..."


def format_report(result: dict[str, Any]) -> str:
    """The Slack line(s). Leads with what is missing, because that is the only
    part that needs a human."""
    per = result.get("period") or "?"
    if not result.get("available"):
        return (f":page_facing_up: *Expected invoices — {per}*\n"
                f"• _Check unavailable: {result.get('reason') or 'unknown reason'}._")

    rows = result.get("results") or []
    missing = [r for r in rows if r.get("status") == STATUS_MISSING]
    unknown = [r for r in rows if r.get("status") == STATUS_UNKNOWN]
    present = [r for r in rows if r.get("status") == STATUS_PRESENT]

    lines = [f":page_facing_up: *Expected invoices — {per}*"]
    for r in missing:
        tag = f" [{r['entity']}]" if r.get("entity") else ""
        # A vendor we ALREADY KNOW does not deliver to a monitored mailbox is a
        # standing configuration gap, not a new surprise. Saying so keeps the
        # monthly line honest instead of crying wolf twelve times a year.
        if r.get("known_undelivered"):
            # The note in the YAML is deliberately long (it carries the account
            # id and the exact fix). A Slack line is not the place for all of it:
            # a monthly report that renders as a paragraph stops being read, which
            # defeats the whole point of saying it out loud. First sentence here,
            # full detail in the file.
            lines.append(f"• :grey_exclamation: *{r['name']}*{tag} — not filed, and "
                         f"not expected to be: {_first_sentence(r.get('note'))}")
        else:
            lines.append(f"• :rotating_light: *{r['name']}*{tag} — NOT filed for {per}")
    for r in unknown:
        tag = f" [{r['entity']}]" if r.get("entity") else ""
        lines.append(f"• :warning: *{r['name']}*{tag} — can't tell: {r.get('detail') or 'unknown'}")
    if present:
        lines.append(f"• :white_check_mark: {len(present)} filed: "
                     + ", ".join(r["name"] for r in present))
    if not missing and not unknown:
        lines.append("• Everything expected for this period is filed.")
    lines.append(f"_Checked {len(rows)} expected invoice(s) against the filer "
                 f"ledger. This reports CUSTODY (what was filed to Drive), not "
                 f"what arrived in a mailbox._")
    return "\n".join(lines)


def flag_count(result: dict[str, Any]) -> int:
    """How many rows need a human. A known-undelivered vendor does not count --
    it is already a tracked configuration gap."""
    if not result.get("available"):
        return 1
    return len([r for r in (result.get("results") or [])
                if (r.get("status") == STATUS_MISSING and not r.get("known_undelivered"))
                or r.get("status") == STATUS_UNKNOWN])
