"""Finance-SOP adherence checks (A1-A3) -- deterministic, read-only facts.

WHAT THIS IS
------------
The graduation slice: three SOP-adherence checks that used to be done by hand (or
by a Cowork task doing its own file reads) become a deterministic Cora script-lane
job. It emits a FACTS BLOCK -- one line per check -- consumed by:

  * ``scripts/run_finance_close_pack.py`` via ``finance_close`` (close-prep notes),
  * the Cowork weekly finance review task, whose Section 1 stops doing its own file
    reads and cites this block instead (its prompt already requires that every claim
    cite a read -- the facts block IS the read).

D-095 CONTRACT: this module computes; a model never does. There is NO model call
anywhere in this job, so no ``llm_usage`` ``caller=`` tag applies here -- that is a
deliberate property of a model-free job, not the un-tagged-spend omission the
2026-08-02 audit found in the delegated-work runner. The one model call in this
bundle (the close pack's optional narration) carries ``caller="finance_close_pack"``.

CHECKS (SOP rev 4, ratified 2026-08-04)
---------------------------------------
A1  Cash-sheet freshness -- the REAL live sheet, modified within 7 days.
    Deliberately does NOT encode the SOP's ``_LIVE``-named set: that naming was
    never migrated, so checking for it would report a permanent false absence.
A2  Clover export presence -- RETIRED. SOP rev 4 struck the daily Clover export
    per the 2026-06-06 Clover retirement (D-027: QBO is OSN's sole financial
    source). Emits ONE static ``lane_retired`` fact -- never a ``lane_absent``
    alarm and never a per-day miss count, which would nag weekly about a lane
    that was deliberately shut down. Drop the check entirely once Justin's
    no-downstream-consumer confirmation closes (SOP rev-4 open item 1).
A3  Monthly filing presence + bank-statement freshness, against the REAL folder
    structure verified on the mount 2026-08-04.

HONEST-FACT CONTRACT (load-bearing)
-----------------------------------
Every check yields an explicit fact with a status. A renamed or moved file/folder
produces ``missing``; it NEVER produces a blank, an empty list, or a silent pass
(the 2026-06-04 Standing-ACTUALS label-fragility doctrine, applied to paths).

Crucially, a vanished ``G:`` mount yields ``unknown``, never ``missing``: every read
goes through :mod:`cora.drive_io`, which distinguishes "mount gone" (raises
``DriveUnavailable``) from "file genuinely absent while the mount is up" (ordinary
``False``/``FileNotFoundError``). Reporting a mount blip as a missing month-end
filing would be a false alarm about someone else's work.
"""

from __future__ import annotations

import datetime
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import drive_io

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# ── paths (verified against the live mount 2026-08-04) ───────────────────────

ACCOUNTING_ROOT = Path(r"G:\My Drive\HJR-Founder-OS\01-HJR-Global\accounting")

# A1 -- the REAL live cash sheet. The SOP's `_LIVE`-named set was never migrated,
# so this is the ratified reality, not the document.
CASH_SHEET_PATH = (
    ACCOUNTING_ROOT
    / "live-sheets"
    / "HJR-Lexco_ENTITIES_Weekly Cash Flow Requirements_Standing ACTUALS.gsheet"
)
CASH_SHEET_MAX_AGE_DAYS = 7

# A3a -- monthly close packs. NOTE the folder semantics, verified on the mount:
# `monthly-reports/YYYY-MM/` holds the PRIOR period's reports (2026-06/ contains
# `2026-05_*` files). So the folder for a given month is expected to fill during
# that month, and the check targets the most recent month whose 15th has passed.
MONTHLY_REPORTS_ROOT = ACCOUNTING_ROOT / "monthly-reports"
MONTHLY_FILING_DAY = 15

# A3b -- per-entity bank statement folders. This is the REAL structure (13 folders,
# enumerated on the mount 2026-08-04), NOT a Cora entity-code list: the folder names
# are the source of truth and a rename must read as `missing`, not be silently
# glob-absorbed.
BANK_STATEMENTS_ROOT = ACCOUNTING_ROOT / "bank-statements"
BANK_ENTITY_FOLDERS: tuple[str, ...] = (
    "Big D Media",
    "HJR GS",
    "HJR Properties",
    "LBHS",
    "LLC",
    "LTS",
    "Lex Corp",
    "Maryvale ASA",
    "OSN Core 4",
    "OSN GF",
    "OSN GMK",
    "OSN GW",
    "OSN VVP",
)
# Statements arrive monthly, so a 45-day window tolerates a late bank cycle without
# tolerating a skipped month.
BANK_STATEMENT_MAX_AGE_DAYS = 45

# Windows drops these into every synced Drive folder. A folder containing only
# these is EMPTY for adherence purposes -- monthly-reports/2026-07 was exactly
# this on 2026-08-04 and would otherwise have read as filed.
IGNORED_NAMES = frozenset({"desktop.ini", ".ds_store", "thumbs.db", "icon\r"})

# Bounded per-folder stat budget. Real folders hold ~12-30 files; the cap stops a
# pathological folder from turning one weekly check into thousands of bounded reads.
MAX_FILES_STATTED = 80

# ── outputs ──────────────────────────────────────────────────────────────────

# Machine-readable block for finance_close's close-prep section.
FACTS_JSON_PATH = _REPO_ROOT / "data" / "state" / "finance-adherence-facts.json"

# Human/KB-readable block for the Cowork weekly review task. Written IN PLACE
# (D-087 supersede-in-place): a recurring state file, not a dated capture, so the
# nightly static sync's replace-on-conflict keeps its chunks clean instead of
# accumulating 52 near-duplicate files a year.
FACTS_MD_PATH = ACCOUNTING_ROOT / "finance-adherence-facts.md"


# ─────────────────────────────────────────────────────────────────────────────
# Fact model
# ─────────────────────────────────────────────────────────────────────────────

STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_STALE = "stale"
STATUS_RETIRED = "retired"
STATUS_UNKNOWN = "unknown"

# Statuses that mean "a human should look". `unknown` is NOT one of them -- it
# means Cora could not see, which is an infrastructure note, not a finance finding.
PROBLEM_STATUSES = frozenset({STATUS_MISSING, STATUS_STALE})


@dataclass
class Fact:
    key: str
    status: str
    text: str
    # Structured values behind the prose, so a consumer can roll facts up without
    # re-parsing the sentence. ``group`` names the roll-up family (e.g.
    # "bank_statements"); ``label`` is the member's own name; ``age_days`` its age.
    group: Optional[str] = None
    label: Optional[str] = None
    age_days: Optional[int] = None

    @property
    def is_problem(self) -> bool:
        return self.status in PROBLEM_STATUSES

    def line(self) -> str:
        return f"{self.key}: {self.text}"


# A roll-up fires only once a group is big enough that per-member lines become a
# wall. Below this the individual lines are more useful than a summary.
ROLLUP_MIN_MEMBERS = 3


def _rollup_line(group: str, status: str, members: list[Fact]) -> str:
    """One line standing in for ``members``, preserving the status token.

    The status word is kept verbatim (MISSING / STALE / ...) because downstream
    consumers surface it to a human.

    The ``detail`` clause MUST branch on status. An UNKNOWN group has no ages (the
    folders were never opened), and defaulting to "no files found" would state a
    factual absence about folders nobody could look inside -- a false alarm about
    someone else's work, which is exactly what this module promises never to emit.
    """
    names = ", ".join(m.label or m.key for m in members)
    ages = sorted(a for a in (m.age_days for m in members) if a is not None)
    if ages:
        span = f"{ages[0]}d" if ages[0] == ages[-1] else f"{ages[0]}-{ages[-1]}d"
        detail = f"newest file {span} old"
    elif status == STATUS_UNKNOWN:
        detail = "could not be read"
    else:
        detail = "no files found"
    return (
        f"{group} ({len(members)} folders): {status.upper()} — {detail} "
        f"across {len(members)} folder(s): {names}"
    )


@dataclass
class AdherenceReport:
    generated_date: str
    facts: list[Fact] = field(default_factory=list)

    @property
    def problems(self) -> list[Fact]:
        return [f for f in self.facts if f.is_problem]

    @property
    def unknowns(self) -> list[Fact]:
        return [f for f in self.facts if f.status == STATUS_UNKNOWN]

    def compact_pairs(self) -> list[tuple[str, str]]:
        """``(line, status)`` pairs with large same-status groups rolled up.

        This is what goes DOWNSTREAM (the close pack, and from there Slack). The
        first live run found all 13 bank-statement folders stale within a 3-day
        spread -- one cause, not 13 findings -- so 13 near-identical lines would
        bury the two facts that differ. The full per-folder list stays in the
        markdown block, which is the audit record.

        The STATUS travels with each line so a consumer never has to infer severity
        by matching words in the prose. A rolled-up line's key is synthetic
        ("bank_statements (13 folders)") and matches no per-folder status key, so
        key-lookup alone would silently under-flag the very group the roll-up exists
        to surface.
        """
        out: list[tuple[str, str]] = []
        groups: dict[tuple[str, str], list[Fact]] = {}
        order: list[Any] = []

        for fact in self.facts:
            if not fact.group:
                order.append(fact)
                continue
            key = (fact.group, fact.status)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(fact)

        for item in order:
            if isinstance(item, Fact):
                out.append((item.line(), item.status))
                continue
            members = groups[item]
            group, status = item
            if len(members) >= ROLLUP_MIN_MEMBERS:
                out.append((_rollup_line(group, status, members), status))
            else:
                out.extend((m.line(), m.status) for m in members)
        return out

    def compact_facts(self) -> list[str]:
        """Just the lines from :meth:`compact_pairs`."""
        return [line for line, _status in self.compact_pairs()]

    def to_json(self) -> dict[str, Any]:
        """Payload shape consumed by finance_close.build_close_prep_section."""
        pairs = self.compact_pairs()
        return {
            "generated_date": self.generated_date,
            "facts": [line for line, _ in pairs],
            # Parallel to `facts`, one status per line -- the authoritative severity
            # signal for the consumer.
            "facts_status": [status for _, status in pairs],
            "facts_full": [f.line() for f in self.facts],
            "statuses": {f.key: f.status for f in self.facts},
            "problem_count": len(self.problems),
            "unknown_count": len(self.unknowns),
        }

    def to_markdown(self) -> str:
        lines = [
            "# Finance SOP adherence facts",
            "",
            f"Generated {self.generated_date} by Cora "
            "(`scripts/run_finance_adherence_check.py`). Deterministic file reads only "
            "-- no figures, no model involvement. Every line below IS a read.",
            "",
            f"**{len(self.problems)} item(s) need attention"
            + (f"; {len(self.unknowns)} could not be read" if self.unknowns else "")
            + ".**",
            "",
        ]
        for fact in self.facts:
            marker = {
                STATUS_OK: "OK",
                STATUS_MISSING: "MISSING",
                STATUS_STALE: "STALE",
                STATUS_RETIRED: "RETIRED",
                STATUS_UNKNOWN: "UNREADABLE",
            }.get(fact.status, fact.status.upper())
            lines.append(f"- **[{marker}]** `{fact.key}` -- {fact.text}")
        lines.append("")
        return "\n".join(lines)

    def summary_line(self) -> str:
        """One finance-safe Slack line. Contains no dollar figure by construction.

        "all clear" is claimable ONLY when nothing was also unreadable. Zero problems
        across checks that could not run is not an all-clear -- it is an absence of
        information, and this line is the most-read artifact of the job.
        """
        if not self.facts:
            return "Finance SOP adherence: no checks ran."
        bits = [f"{len(self.facts)} check(s)"]
        if self.problems:
            bits.append(f":triangular_flag_on_post: {len(self.problems)} need attention")
        elif self.unknowns:
            bits.append("no problems in what could be read")
        else:
            bits.append("all clear")
        if self.unknowns:
            bits.append(f":warning: {len(self.unknowns)} could NOT be read")
        return "Finance SOP adherence — " + ", ".join(bits) + "."


# ─────────────────────────────────────────────────────────────────────────────
# Bounded Drive helpers
# ─────────────────────────────────────────────────────────────────────────────

def _age_days(mtime: float, today: datetime.date) -> int:
    """Age in whole days, floored at 0.

    A future mtime (clock skew, or a Drive sync stamping ahead) would otherwise
    render as a negative age -- "-3d old" -- inside a roll-up span.
    """
    modified = datetime.datetime.fromtimestamp(mtime).date()
    return max(0, (today - modified).days)


def _content_entries(directory: Path) -> list[Path] | None:
    """Non-ignored entries in ``directory``, or None if the read could not be done.

    An empty LIST means the folder exists but holds nothing that counts. None means
    "could not look" -- and EVERY read failure returns None, not just a vanished
    mount. Collapsing a PermissionError or an unstattable Drive placeholder into
    "nothing there" would report a filed month as MISSING, which is the false-alarm
    class this module exists to avoid. ``Path.glob`` on an absent directory yields
    nothing WITHOUT raising, so a genuinely missing folder still gets its ``[]``
    from the success path.
    """
    try:
        entries = drive_io.glob(directory, "*")
    except drive_io.DriveUnavailable:
        return None
    except OSError as exc:
        log.warning("finance_adherence: could not list %s: %s", directory, exc)
        return None
    return [p for p in entries if p.name.strip().lower() not in IGNORED_NAMES]


def _newest_age_days(
    directory: Path, today: datetime.date,
) -> tuple[int | None, int, bool, bool]:
    """(age of newest file in days, files counted, read_ok, truncated).

    ``read_ok=False`` means the read could not be performed -- reported as
    ``unknown``, never as a missing filing.

    The stat budget is applied to entries sorted NEWEST-NAME-FIRST. ``Path.glob``
    returns directory (≈ name) order, so capping the raw list and then taking the
    max mtime would keep the 80 oldest-named files and report a perfectly current
    folder as STALE once it passed the cap -- a false alarm that gets louder the
    longer the folder is maintained. Statement filenames are date-bearing, so
    reverse-name order puts the newest first; ``truncated`` is surfaced in the fact
    text so a capped read can never pass silently either.
    """
    entries = _content_entries(directory)
    if entries is None:
        return None, 0, False, False
    entries = sorted(entries, key=lambda p: p.name, reverse=True)
    truncated = len(entries) > MAX_FILES_STATTED
    newest: float | None = None
    counted = 0
    for path in entries[:MAX_FILES_STATTED]:
        try:
            info = drive_io.stat_info(path, retry_seconds=0)
        except drive_io.DriveUnavailable:
            return None, counted, False, truncated
        except OSError:
            continue
        if info is None:
            continue
        counted += 1
        if newest is None or info[0] > newest:
            newest = info[0]
    if newest is None:
        return None, counted, True, truncated
    return _age_days(newest, today), counted, True, truncated


# ─────────────────────────────────────────────────────────────────────────────
# A1 -- cash-sheet freshness
# ─────────────────────────────────────────────────────────────────────────────

def check_cash_sheet(
    *,
    today: datetime.date | None = None,
    path: Path | None = None,
    max_age_days: int = CASH_SHEET_MAX_AGE_DAYS,
) -> Fact:
    """A1: the live Standing ACTUALS sheet must be modified within ``max_age_days``."""
    day = today or datetime.date.today()
    target = path or CASH_SHEET_PATH

    try:
        info = drive_io.stat_info(target, retry_seconds=0)
    except drive_io.DriveUnavailable:
        return Fact(
            key="cash_sheet",
            status=STATUS_UNKNOWN,
            text=(
                "could not read — the Drive mount was unreachable. Freshness unknown "
                "this run (this is an infrastructure note, not a finance finding)."
            ),
        )
    except OSError as exc:
        return Fact(
            key="cash_sheet", status=STATUS_UNKNOWN,
            text=f"could not read ({type(exc).__name__}); freshness unknown this run.",
        )

    if info is None:
        # Genuine absence with the mount UP -- a rename or a move.
        #
        # Deliberately does NOT echo the sheet's filename. This facts block is
        # written under 01-HJR-Global/accounting/ and IS KB-ingested, and
        # gsheets_financials locks a source-opaque contract ("never log or surface
        # file IDs, sheet names, or Drive links"). The exact path goes to the local
        # log instead, which is where whoever fixes it will be looking anyway.
        log.error(
            "finance_adherence: cash sheet absent at expected path %s", target,
        )
        return Fact(
            key="cash_sheet",
            status=STATUS_MISSING,
            text=(
                "MISSING — the live Standing-ACTUALS cash sheet is not at its expected "
                "location under the accounting live-sheets folder. It was renamed, "
                "moved, or deleted; the weekly cash cross-check reads that exact file. "
                "The full path is in this run's log."
            ),
        )

    age = _age_days(info[0], day)
    if age > max_age_days:
        return Fact(
            key="cash_sheet",
            status=STATUS_STALE,
            text=(
                f"STALE — last modified {age}d ago (threshold {max_age_days}d). "
                "The weekly cash figures downstream are as-of that edit, not today."
            ),
        )
    return Fact(
        key="cash_sheet", status=STATUS_OK,
        text=f"fresh — last modified {age}d ago (threshold {max_age_days}d).",
    )


# ─────────────────────────────────────────────────────────────────────────────
# A2 -- Clover export lane (RETIRED)
# ─────────────────────────────────────────────────────────────────────────────

def clover_fact() -> Fact:
    """A2: one static retired fact. Never an alarm, never a per-day miss count.

    SOP rev 4 struck the daily Clover export per the 2026-06-06 Clover retirement
    (D-027 -- QBO is OSN's sole financial source). Checking for the export would
    nag weekly about a deliberately shut-down lane. Drop this function entirely
    once Justin's no-downstream-consumer confirmation closes (SOP rev-4 open item 1).
    """
    return Fact(
        key="clover",
        status=STATUS_RETIRED,
        text="lane_retired (2026-06-06 decision; SOP rev 4) — no export expected.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# A3a -- monthly filing presence
# ─────────────────────────────────────────────────────────────────────────────

def target_filing_month(
    today: datetime.date | None = None, filing_day: int = MONTHLY_FILING_DAY,
) -> datetime.date:
    """First day of the most recent month whose ``filing_day`` has passed.

    On or before the 15th the current month is not yet due, so the check looks at
    the previous month. This is why an early-month run does not nag about a folder
    nobody was supposed to have filled yet.
    """
    day = today or datetime.date.today()
    if day.day > filing_day:
        return day.replace(day=1)
    first_this = day.replace(day=1)
    return (first_this - datetime.timedelta(days=1)).replace(day=1)


def check_monthly_filing(
    *,
    today: datetime.date | None = None,
    root: Path | None = None,
    filing_day: int = MONTHLY_FILING_DAY,
) -> Fact:
    """A3a: ``monthly-reports/YYYY-MM/`` must exist AND hold content once due."""
    day = today or datetime.date.today()
    base = root or MONTHLY_REPORTS_ROOT
    month = target_filing_month(day, filing_day)
    tag = month.strftime("%Y-%m")
    folder = base / tag
    key = f"monthly_filing {tag}"

    entries = _content_entries(folder)
    if entries is None:
        return Fact(
            key=key, status=STATUS_UNKNOWN,
            text=(
                "could not read — the Drive mount was unreachable. Filing presence "
                "unknown this run (infrastructure note, not a finance finding)."
            ),
        )

    try:
        folder_exists = drive_io.exists(folder, retry_seconds=0)
    except drive_io.DriveUnavailable:
        return Fact(
            key=key, status=STATUS_UNKNOWN,
            text="could not read — the Drive mount was unreachable.",
        )
    except OSError:
        folder_exists = False

    if not folder_exists:
        return Fact(
            key=key, status=STATUS_MISSING,
            text=(
                f"MISSING — no `{tag}/` folder under `monthly-reports/`. The close "
                f"pack for period {tag} has not been filed (due after the {filing_day}th)."
            ),
        )
    if not entries:
        # The exact live state of monthly-reports/2026-07 on 2026-08-04: present but
        # holding only desktop.ini. A folder-exists check alone would pass it.
        return Fact(
            key=key, status=STATUS_MISSING,
            text=(
                f"MISSING (no content) — `{tag}/` exists but holds no report files "
                "(only OS metadata). The folder being present is not the filing."
            ),
        )
    return Fact(
        key=key, status=STATUS_OK,
        text=f"filed — `{tag}/` holds {len(entries)} file(s).",
    )


# ─────────────────────────────────────────────────────────────────────────────
# A3b -- bank statement freshness, per entity folder
# ─────────────────────────────────────────────────────────────────────────────

def check_bank_statements(
    *,
    today: datetime.date | None = None,
    root: Path | None = None,
    folders: tuple[str, ...] | None = None,
    max_age_days: int = BANK_STATEMENT_MAX_AGE_DAYS,
) -> list[Fact]:
    """A3b: newest-file age per entity folder. One fact per folder, always."""
    day = today or datetime.date.today()
    base = root or BANK_STATEMENTS_ROOT
    names = folders if folders is not None else BANK_ENTITY_FOLDERS

    facts: list[Fact] = []
    for name in names:
        key = f"bank_statements {name}"
        folder = base / name
        age, counted, mount_ok, truncated = _newest_age_days(folder, day)
        cap_note = (
            f" (only the {MAX_FILES_STATTED} newest-named files were checked)"
            if truncated else ""
        )

        if not mount_ok:
            facts.append(Fact(
                key=key, status=STATUS_UNKNOWN,
                text="could not read — the Drive mount was unreachable.",
                group="bank_statements", label=name,
            ))
            continue

        if age is None:
            try:
                folder_exists = drive_io.exists(folder, retry_seconds=0)
            except drive_io.DriveUnavailable:
                facts.append(Fact(
                    key=key, status=STATUS_UNKNOWN,
                    text="could not read — the Drive mount was unreachable.",
                    group="bank_statements", label=name,
                ))
                continue
            except OSError:
                folder_exists = False
            if not folder_exists:
                facts.append(Fact(
                    key=key, status=STATUS_MISSING,
                    text=(
                        f"MISSING — no `{name}/` folder under `bank-statements/`. "
                        "It was renamed or moved; this check reads the folder name."
                    ),
                    group="bank_statements", label=name,
                ))
            else:
                facts.append(Fact(
                    key=key, status=STATUS_MISSING,
                    text=f"MISSING (no content) — `{name}/` holds no statement files.",
                    group="bank_statements", label=name,
                ))
            continue

        if age > max_age_days:
            facts.append(Fact(
                key=key, status=STATUS_STALE,
                text=(
                    f"STALE — newest statement is {age}d old (threshold {max_age_days}d), "
                    f"{counted} file(s) present.{cap_note}"
                ),
                group="bank_statements", label=name, age_days=age,
            ))
        else:
            facts.append(Fact(
                key=key, status=STATUS_OK,
                text=(
                    f"current — newest statement {age}d old, "
                    f"{counted} file(s) present.{cap_note}"
                ),
                group="bank_statements", label=name, age_days=age,
            ))
    return facts


# ─────────────────────────────────────────────────────────────────────────────
# Report assembly + persistence
# ─────────────────────────────────────────────────────────────────────────────

def build_report(
    *,
    today: datetime.date | None = None,
    accounting_root: Path | None = None,
) -> AdherenceReport:
    """Run A1-A3 and assemble the facts block.

    Each check is individually guarded: an unforeseen exception becomes an
    ``unknown`` fact for that check, so one bad path can never empty the block.
    """
    day = today or datetime.date.today()
    root = accounting_root

    cash_path = (
        root / "live-sheets" / CASH_SHEET_PATH.name if root else None
    )
    monthly_root = root / "monthly-reports" if root else None
    bank_root = root / "bank-statements" if root else None

    report = AdherenceReport(generated_date=day.isoformat())

    def guarded(key: str, fn) -> list[Fact]:
        try:
            got = fn()
        except Exception as exc:  # noqa: BLE001 -- one bad check must not empty the block
            log.error("finance_adherence: check %s raised: %s", key, exc)
            return [Fact(
                key=key, status=STATUS_UNKNOWN,
                text=f"check failed ({type(exc).__name__}); status unknown this run.",
            )]
        return got if isinstance(got, list) else [got]

    report.facts.extend(guarded(
        "cash_sheet", lambda: check_cash_sheet(today=day, path=cash_path),
    ))
    report.facts.append(clover_fact())
    report.facts.extend(guarded(
        "monthly_filing", lambda: check_monthly_filing(today=day, root=monthly_root),
    ))
    report.facts.extend(guarded(
        "bank_statements", lambda: check_bank_statements(today=day, root=bank_root),
    ))
    return report


def write_facts_json(report: AdherenceReport, *, path: Path | None = None) -> Path:
    """Persist the machine-readable block the close pack reads. Local disk (C:)."""
    target = path or FACTS_JSON_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(report.to_json(), indent=2), encoding="utf-8")
    tmp.replace(target)
    return target


def write_facts_markdown(report: AdherenceReport, *, path: Path | None = None) -> Optional[Path]:
    """Persist the Drive-side block for the Cowork weekly review task.

    Written IN PLACE via the bounded atomic writer. Returns None if the mount is
    unreachable -- the local JSON is still authoritative for the close pack, so a
    Drive outage degrades the job rather than failing it.
    """
    target = path or FACTS_MD_PATH
    try:
        drive_io.write_text_atomic(target, report.to_markdown())
    except drive_io.DriveUnavailable:
        log.warning("finance_adherence: Drive unreachable — facts markdown not written")
        return None
    except OSError as exc:
        log.warning("finance_adherence: facts markdown write failed: %s", exc)
        return None
    return target
