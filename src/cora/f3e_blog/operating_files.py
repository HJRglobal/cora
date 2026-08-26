"""Read/write the F3E blog lane's operating files on the Drive mount.

Four files, all authored and edited by humans, all read at RUNTIME so Harrison can
change the plan without a code change or a restart:

    backlog    the 12-week Learn editorial table (which post is next)
    checklist  the 14 claims rails (the human source; preflight.py is its mirror)
    templates  the post skeletons the drafting prompt is built from
    log        the append-only pipeline log, newest entry on top

Every touch goes through `drive_io`, never bare `pathlib`: a raw read against a
blipped G: mount can BLOCK the calling thread, and this module is imported by the
always-on bot process for the card path. `drive_io` bounds every call and raises
`DriveUnavailable` instead of hanging.

Row edits are SURGICAL -- only the status cell of the one matching row is
replaced, and the rest of the file is preserved byte for byte. Regenerating the
table from parsed records would silently reformat Harrison's own notes column and
lose anything the parser did not model.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import drive_io

log = logging.getLogger(__name__)

# Arizona: fixed UTC-7, no DST. Every date this lane stamps is an AZ calendar
# date, because "which Monday did this run" is a local-calendar question.
_AZ = timezone(timedelta(hours=-7))

_DEFAULT_DRIVE_ROOT = r"G:\My Drive\HJR-Founder-OS"
_PROJECT_REL = Path("02-F3-Energy") / "projects" / "build-f3e-news-and-blog-strategy"

BACKLOG_NAME = "2026-08-26_f3e_learn-editorial-backlog-v1.md"
CHECKLIST_NAME = "2026-08-26_f3e_content-claims-preflight-checklist.md"
TEMPLATES_NAME = "2026-08-26_f3e_post-templates-news-and-learn.md"
LOG_REL = Path("_notes") / "pipeline-log.md"

FAQ_URL = "https://f3energy.com/pages/faq"
LINEUP_REL = Path("02-F3-Energy") / "brand-assets" / "brand" / "f3-product-lineup-canonical.md"

# Blog targets (verified live 2026-08-26).
BLOG_LEARN = "gid://shopify/Blog/122516767040"
BLOG_NEWS = "gid://shopify/Blog/97115373888"

# Statuses. QUEUED is the only one the drafting job will pick up; PROPOSED exists
# so a refill can suggest topics without authorising them (Harrison flips it).
STATUS_QUEUED = "QUEUED"
STATUS_PROPOSED = "PROPOSED"
STATUS_DRAFTED = "DRAFTED"
STATUS_PUBLISHED = "PUBLISHED"
STATUS_DISMISSED = "DISMISSED"


def az_today() -> str:
    return datetime.now(_AZ).strftime("%Y-%m-%d")


def drive_root() -> Path:
    """Overridable for tests and for a host whose mount letter differs."""
    return Path(os.environ.get("CORA_DRIVE_ROOT", _DEFAULT_DRIVE_ROOT))


def project_dir() -> Path:
    return drive_root() / _PROJECT_REL


def backlog_path() -> Path:
    return project_dir() / BACKLOG_NAME


def checklist_path() -> Path:
    return project_dir() / CHECKLIST_NAME


def templates_path() -> Path:
    return project_dir() / TEMPLATES_NAME


def pipeline_log_path() -> Path:
    return project_dir() / LOG_REL


def lineup_path() -> Path:
    return drive_root() / LINEUP_REL


# ---------------------------------------------------------------------------
# Backlog table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BacklogRow:
    number: str
    status_cell: str          # the raw cell, e.g. "PUBLISHED 2026-08-26 (gid 618...)"
    title: str
    lane_pillar: str
    target_prompt: str
    notes: str
    line_index: int           # 0-based index into the file's line list

    @property
    def status(self) -> str:
        """The leading status token, uppercased. '' for an empty cell."""
        tok = (self.status_cell or "").strip().split()
        return tok[0].upper() if tok else ""

    @property
    def article_gid(self) -> str:
        """The numeric article id recorded in the status cell, if any."""
        # No minimum digit count: the pattern is already anchored on the literal
        # "gid", so any run of digits after it IS the id. An earlier {4,} bound
        # was arbitrary and silently returned "" for a short id.
        m = re.search(r"gid\s*([0-9]+)", self.status_cell or "")
        return m.group(1) if m else ""

    @property
    def is_pure_only(self) -> bool:
        return "pure" in (self.lane_pillar or "").lower()


_ROW_RE = re.compile(r"^\s*\|(?P<cells>.*)\|\s*$")


def _split_cells_raw(raw: str) -> list[str]:
    """Split a table row on UNESCAPED pipes, leaving `\\|` intact in the segments.

    Escape-awareness is not cosmetic. `_cell` escapes pipes on write, so a naive
    `raw.split("|")` would both truncate the value on read AND shift every later
    cell one column left -- silently moving a topic's notes into its target-prompt
    field. Keeping the segments RAW here is what lets `set_row_status` rejoin them
    byte for byte without having to re-escape anything.
    """
    cells: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "\\" and i + 1 < n and raw[i + 1] == "|":
            buf.append("\\|")
            i += 2
            continue
        if ch == "|":
            cells.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    cells.append("".join(buf))
    return cells


def _unescape(cell: str) -> str:
    return (cell or "").replace("\\|", "|")


def parse_backlog(text: str) -> list[BacklogRow]:
    """Parse the markdown table. Non-table lines and the header/separator are
    skipped, so prose above and the 'Standing rules' section below are ignored.
    """
    rows: list[BacklogRow] = []
    for idx, line in enumerate((text or "").replace("\r\n", "\n").split("\n")):
        m = _ROW_RE.match(line)
        if not m:
            continue
        cells = [_unescape(c).strip() for c in _split_cells_raw(m.group("cells"))]
        if len(cells) < 6:
            continue
        num = cells[0]
        if not num.isdigit():
            continue  # header ("#") and separator ("---") rows
        rows.append(BacklogRow(
            number=num, status_cell=cells[1], title=cells[2],
            lane_pillar=cells[3], target_prompt=cells[4], notes=cells[5],
            line_index=idx,
        ))
    return rows


def next_queued(rows: list[BacklogRow]) -> BacklogRow | None:
    """The TOP QUEUED row, in file order.

    A row already DRAFTED is skipped by construction (its status is not QUEUED),
    which is what keeps this job from double-staging a row the interim Cowork
    task drafted an hour earlier while both are running.
    """
    for r in rows:
        if r.status == STATUS_QUEUED:
            return r
    return None


def count_queued(rows: list[BacklogRow]) -> int:
    return sum(1 for r in rows if r.status == STATUS_QUEUED)


def set_row_status(text: str, row: BacklogRow, new_status_cell: str) -> str:
    """Replace ONE row's status cell, preserving every other byte of the file.

    Raises ValueError if the target line no longer looks like the row we parsed
    -- i.e. if the file changed under us between read and write. Better to fail
    the run than to write a status onto the wrong topic.
    """
    lines = (text or "").replace("\r\n", "\n").split("\n")
    if not (0 <= row.line_index < len(lines)):
        raise ValueError("backlog row %s: line index out of range" % row.number)
    line = lines[row.line_index]
    m = _ROW_RE.match(line)
    if not m:
        raise ValueError("backlog row %s: line is no longer a table row" % row.number)
    # RAW segments: rejoining them below must be byte-preserving for every cell
    # this function is not deliberately changing.
    cells = _split_cells_raw(m.group("cells"))
    if len(cells) < 6 or _unescape(cells[0]).strip() != row.number:
        raise ValueError(
            "backlog row %s: file changed under us (found row %r) -- not writing"
            % (row.number, _unescape(cells[0]).strip() if cells else "?")
        )
    if _unescape(cells[2]).strip() != row.title:
        raise ValueError(
            "backlog row %s: title changed under us (%r != %r) -- not writing"
            % (row.number, _unescape(cells[2]).strip(), row.title)
        )
    # Preserve the cell's original padding style (" X " rather than "X").
    cells[1] = " %s " % new_status_cell.strip()
    lines[row.line_index] = "|" + "|".join(cells) + "|"
    return "\n".join(lines)


def drafted_status_cell(article_gid: str, when: str | None = None) -> str:
    """The status cell written when a draft is staged: matches the existing
    human convention in the file, e.g. 'DRAFTED 2026-08-26 (gid 618441441600)'."""
    numeric = str(article_gid or "").rsplit("/", 1)[-1]
    return "%s %s (gid %s)" % (STATUS_DRAFTED, when or az_today(), numeric)


def published_status_cell(article_gid: str, when: str | None = None) -> str:
    numeric = str(article_gid or "").rsplit("/", 1)[-1]
    return "%s %s (gid %s)" % (STATUS_PUBLISHED, when or az_today(), numeric)


def dismissed_status_cell(article_gid: str, when: str | None = None) -> str:
    numeric = str(article_gid or "").rsplit("/", 1)[-1]
    return "%s %s (gid %s)" % (STATUS_DISMISSED, when or az_today(), numeric)


def append_proposed_rows(text: str, proposals: list[dict]) -> str:
    """Append PROPOSED rows to the end of the table.

    PROPOSED is deliberately not draftable: `next_queued` only matches QUEUED, so
    a refill suggests topics without authorising any of them.
    """
    if not proposals:
        return text
    lines = (text or "").replace("\r\n", "\n").split("\n")
    rows = parse_backlog(text)
    if not rows:
        raise ValueError("backlog has no parseable table rows -- refusing to append")
    last = rows[-1]
    next_num = max(int(r.number) for r in rows) + 1
    new_lines = []
    for p in proposals:
        cells = [
            str(next_num),
            "%s %s" % (STATUS_PROPOSED, az_today()),
            _cell(p.get("title", "")),
            _cell(p.get("lane_pillar", "")),
            _cell(p.get("target_prompt", "")),
            _cell(p.get("notes", "")),
        ]
        new_lines.append("| " + " | ".join(cells) + " |")
        next_num += 1
    at = last.line_index + 1
    return "\n".join(lines[:at] + new_lines + lines[at:])


_PIPE_RE = re.compile(r"\|")


def _cell(value: str) -> str:
    """Make a value safe inside a markdown table cell.

    A pipe would split the row and shift every later cell by one, so a topic
    string carrying one must be escaped -- topic text comes from an LLM refill
    proposal and from the frozen prompt basket, neither of which is
    pipe-free by contract. Newlines collapse for the same reason.
    """
    txt = " ".join(str(value or "").split())
    return _PIPE_RE.sub("\\|", txt)


# ---------------------------------------------------------------------------
# Drive reads / writes
# ---------------------------------------------------------------------------


def read_backlog() -> tuple[str, list[BacklogRow]]:
    text = drive_io.read_text(backlog_path())
    return text, parse_backlog(text)


def write_backlog(text: str) -> None:
    drive_io.write_text_atomic(backlog_path(), text)


def read_checklist() -> str:
    return drive_io.read_text(checklist_path())


def read_templates() -> str:
    return drive_io.read_text(templates_path())


def read_lineup() -> str:
    return drive_io.read_text(lineup_path())


_LOG_SECTION_RE = re.compile(r"^##\s", re.MULTILINE)


def prepend_log_entry(text: str, entry: str) -> str:
    """Insert a '## ...' section above the newest existing one.

    The log's own header says 'Newest entry on top', so an append would put every
    Cora entry at the bottom under the oldest human ones and read as stale.
    """
    body = (text or "").replace("\r\n", "\n")
    entry = entry.rstrip() + "\n"
    m = _LOG_SECTION_RE.search(body)
    if not m:
        return body.rstrip() + "\n\n" + entry
    at = m.start()
    return body[:at] + entry + "\n" + body[at:]


def append_pipeline_log(entry: str) -> None:
    """Read-modify-write the pipeline log with the new entry on top.

    Fail-soft: the log is a record, not a control. Losing a log line must never
    turn a successful publish into a reported failure, so this logs and returns
    rather than raising. The ledger on C: is the authoritative audit trail.
    """
    path = pipeline_log_path()
    try:
        try:
            current = drive_io.read_text(path)
        except FileNotFoundError:
            current = "# F3E News + Learn pipeline log\n"
        drive_io.write_text_atomic(path, prepend_log_entry(current, entry))
    except Exception as exc:  # noqa: BLE001 -- a record must not break the outcome
        log.error("f3e_blog: pipeline log write FAILED (%s): %s", path, exc)
