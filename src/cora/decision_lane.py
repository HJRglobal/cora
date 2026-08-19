"""The decisions lane: one parser, gate-date escalation, and delivery evidence.

WHY (cq-232fe6a541ff; evidence: the 2026-08-18 decisions-lane delivery audit).
Five decisions -- OSN data source, Jerry DW access, BDM department lock, Eric LEX
Learning Center, LEX Phase 2 -- sat Open past their 2026-08-13 gate date and
NEVER reached Harrison on any surface. The audit found two independent causes,
either of which alone produces total silence:

  1. INTAKE. All five exist only in the Airtable Org Remodel Tracker. Every Cora
     surface that raises a pending decision reads ONE source,
     memory/decisions-pending.md. Nothing read the Airtable table, so no lane had
     anything to deliver.
  2. THE SEVERITY FILTER. All five are P2, and `gather_stalled_decisions`
     hard-filters to P0/P1. Even a correct transcription would have stayed dark.

Consequence for the control, and this is the load-bearing part: the seed asked for
"delivery verification for P-decisions older than N days", and implemented
literally -- staleness on P0/P1 -- that check would have stayed GREEN through all
five. Aging-P0/P1 verification is worth having; it is NOT the control that catches
this class. **The control that does is "an Open decision whose GATE DATE has
passed, at ANY severity, that no surface has delivered."** Both halves matter:
any-severity, and delivered-not-just-gathered.

EXPIRY SEMANTICS (aligned with the 2026-08-19 approval-recon adoption): an
expired-undecided item either never should have queued for a human, or it must
ESCALATE. It must never silently age out. So nothing here expires a decision --
a passed gate makes it LOUDER, never gone.

D-011 is untouched. This module READS decisions-pending.md and records delivery
evidence; it never writes canon. The Airtable->decisions-pending transcription is
a separate PROPOSE-ONLY script.
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: Where a surface records that it actually EMITTED a decision. Gathering is not
#: delivering -- conflating the two is what let the audit's silence look like
#: activity.
DELIVERY_LEDGER = _REPO_ROOT / "logs" / "decision-deliveries.jsonl"

#: A gate that passed this many days ago with no delivery is an alarm. Small: the
#: whole failure mode is silence, and a day of it is already a day too many.
GATE_GRACE_DAYS = 1

#: Delivery counts as recent within this window. The weekly memo is the slowest
#: surface, so anything longer than a week cannot distinguish "delivered on
#: cadence" from "stopped being delivered".
DELIVERY_WINDOW_DAYS = 8

_TEMPLATE_TOPIC = "[Topic]"

# Field patterns. All tolerate the file's real formatting (bolded label, em
# dashes, trailing annotations) and all are anchored + bounded -- this text comes
# from a hand-maintained file, and the repo has had six ReDoS defects.
_SEVERITY_RE = re.compile(r"\*\*Severity\*\*:\s*(P\d)\b(?!\s*/)")
_ENTITY_RE = re.compile(r"\*\*Entity\*\*:\s*([^\n]{0,120})")
_OWNER_RE = re.compile(r"\*\*Owner of next nudge\*\*:\s*([^\n]{0,120})")
_SURFACED_RE = re.compile(r"\*\*Surfaced\*\*:\s*[^\n]{0,40}?(\d{4}-\d{2}-\d{2})")
_TOUCHED_RE = re.compile(r"\*\*Last touched\*\*:\s*[^\n]{0,40}?(\d{4}-\d{2}-\d{2})")

#: The GATE date -- the day a decision was supposed to be made. NOT in the file's
#: template today, which is exactly why nothing could enforce it; the parser
#: accepts three spellings so the schema addition can land without a migration,
#: and an ABSENT gate is not an alarm (it is simply a decision with no deadline).
_GATE_RE = re.compile(
    r"\*\*(?:Gate|Gate date|Decide by|Decision due)\*\*:\s*[^\n]{0,40}?(\d{4}-\d{2}-\d{2})",
    re.I,
)

#: A closed entry. The live file marks these inline in the heading ("-- CLOSED
#: 2026-07-20"), so a parser that ignored it would alarm on settled decisions.
_CLOSED_RE = re.compile(r"\b(?:closed|resolved|decided|withdrawn)\b", re.I)


def decisions_pending_path() -> Path:
    """Same env override strategy_memo uses, so tests point both at one file."""
    return Path(os.environ.get("STRATEGY_DECISIONS_PATH")
                or r"G:\My Drive\HJR-Founder-OS\memory\decisions-pending.md")


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_entries(content: str, *, today: date | None = None,
                  skip_closed: bool = True) -> list[dict[str, Any]]:
    """Every real entry in decisions-pending.md, at EVERY severity.

    THE SHARED PARSER. `strategy_memo.gather_stalled_decisions` applies its
    P0/P1 filter on top of this rather than parsing again -- one grammar, so a
    field added here cannot be visible to one surface and invisible to another.
    That drift is the shape of the original bug: two registries of the same thing.

    Skips the "## Recently resolved" tail and the template skeleton always.
    `skip_closed` additionally drops an entry whose HEADING marks it closed (the
    live file annotates them inline: "-- CLOSED 2026-07-20"), which is what the
    gate control wants and what strategy_memo deliberately does NOT: it passes
    skip_closed=False so its output stays byte-identical to the behaviour its
    persisted weekly snapshots were built from. Never raises.
    """
    today = today or datetime.now(timezone.utc).date()
    resolved = re.search(r"^## Recently resolved\b", content or "", re.MULTILINE)
    parseable = (content or "")[:resolved.start()] if resolved else (content or "")

    entries: list[dict[str, Any]] = []
    for block in re.split(r"\n(?=### )", parseable):
        if not block.startswith("### "):
            continue
        heading = block.split("\n", 1)[0][4:].strip()
        if heading == _TEMPLATE_TOPIC:
            continue
        if skip_closed and _CLOSED_RE.search(heading):
            continue

        sev_m = _SEVERITY_RE.search(block)
        entity_m = _ENTITY_RE.search(block)
        owner_m = _OWNER_RE.search(block)
        surfaced = _parse_date(m.group(1) if (m := _SURFACED_RE.search(block)) else None)
        touched = _parse_date(m.group(1) if (m := _TOUCHED_RE.search(block)) else None)
        gate = _parse_date(m.group(1) if (m := _GATE_RE.search(block)) else None)

        entries.append({
            "topic": heading[:140],
            "entity": (entity_m.group(1).strip() if entity_m else "FNDR")[:60],
            # None, not a default: an entry with no parseable severity is a
            # FORMATTING defect, and inventing "P3" would hide it.
            "severity": sev_m.group(1) if sev_m else None,
            "owner": (owner_m.group(1).strip() if owner_m else "unassigned")[:60],
            "surfaced": surfaced.isoformat() if surfaced else None,
            "last_touched": touched.isoformat() if touched else None,
            "gate": gate.isoformat() if gate else None,
            "open_days": (today - surfaced).days if surfaced else None,
            "stale_days": (today - touched).days if touched else None,
            "gate_overdue_days": (today - gate).days if gate else None,
        })
    return entries


def load_entries(*, today: date | None = None) -> list[dict[str, Any]]:
    """parse_entries over the live file. [] when the file cannot be read."""
    from . import drive_io
    try:
        content = drive_io.read_text(decisions_pending_path(), encoding="utf-8")
    except Exception:  # noqa: BLE001 -- a bounded G: outage is not an alarm
        return []
    return parse_entries(content, today=today)


# ── delivery evidence ────────────────────────────────────────────────────────

def _topic_key(topic: str) -> str:
    """Normalized topic identity. The heading is the only stable handle these
    entries have (no ids in the file), so it is normalized rather than trusted
    verbatim: a punctuation or case edit must not read as a different decision
    and reset its delivery history."""
    return re.sub(r"[^a-z0-9]+", " ", str(topic or "").lower()).strip()


def record_delivery(topics: list[str] | str, surface: str,
                    *, ledger: Path | None = None) -> int:
    """Record that `surface` actually EMITTED these decisions. Never raises.

    Called by a surface AFTER a successful send, never at gather time. Returns
    the number of rows written (0 on any failure) so a caller can log it.
    """
    if isinstance(topics, str):
        topics = [topics]
    rows = [t for t in (topics or []) if str(t or "").strip()]
    if not rows:
        return 0
    path = ledger or DELIVERY_LEDGER
    stamp = datetime.now(timezone.utc).isoformat()
    written = 0
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for topic in rows:
                fh.write(json.dumps({
                    "ts": stamp,
                    "surface": str(surface or "")[:40],
                    "topic": str(topic)[:140],
                    "key": _topic_key(topic),
                }) + "\n")
                written += 1
    except Exception:  # noqa: BLE001 -- evidence-keeping never breaks a send
        return 0
    return written


def delivery_index(*, ledger: Path | None = None,
                   now: datetime | None = None) -> dict[str, dict[str, Any]]:
    """{topic_key: {"last": datetime, "surfaces": {...}}} from the ledger.

    Reads the whole file -- these are a handful of rows per week, and a windowed
    tail read would make "never delivered" indistinguishable from "delivered
    before the window", which is the distinction the alarm turns on.
    """
    now = now or datetime.now(timezone.utc)
    try:
        lines = (ledger or DELIVERY_LEDGER).read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    index: dict[str, dict[str, Any]] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or _topic_key(row.get("topic", "")))
        if not key:
            continue
        try:
            stamp = datetime.fromisoformat(str(row.get("ts", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        bucket = index.setdefault(key, {"last": stamp, "surfaces": {}})
        if stamp > bucket["last"]:
            bucket["last"] = stamp
        bucket["surfaces"][str(row.get("surface") or "?")] = stamp.isoformat()
    return index


# ── the control ──────────────────────────────────────────────────────────────

def undelivered_overdue(entries: list[dict[str, Any]] | None = None, *,
                        today: date | None = None,
                        now: datetime | None = None,
                        ledger: Path | None = None,
                        grace_days: int = GATE_GRACE_DAYS,
                        window_days: int = DELIVERY_WINDOW_DAYS) -> list[dict[str, Any]]:
    """Open decisions whose GATE has passed and that nothing has delivered.

    ANY severity, deliberately: the five lost decisions were all P2, and a
    P0/P1-filtered check would have stayed green through every one of them.

    A decision with no gate date is NOT reported -- it has no deadline to blow.
    That is the gap the schema addition closes, and the reason the transcription
    script carries the gate date across from Airtable.
    """
    today = today or datetime.now(timezone.utc).date()
    now = now or datetime.now(timezone.utc)
    rows = entries if entries is not None else load_entries(today=today)
    index = delivery_index(ledger=ledger, now=now)

    out: list[dict[str, Any]] = []
    for entry in rows:
        overdue = entry.get("gate_overdue_days")
        if overdue is None or overdue < grace_days:
            continue
        record = index.get(_topic_key(entry.get("topic", "")))
        last = record["last"] if record else None
        delivered_recently = bool(
            last and (now - last).days <= window_days)
        if delivered_recently:
            continue
        item = dict(entry)
        item["last_delivered"] = last.isoformat() if last else None
        item["never_delivered"] = last is None
        out.append(item)
    out.sort(key=lambda e: -(e.get("gate_overdue_days") or 0))
    return out


def format_alarm(rows: list[dict[str, Any]]) -> str:
    """One compact block for the health digest. "" when there is nothing."""
    if not rows:
        return ""
    lines = [
        f"{len(rows)} Open decision(s) past their gate date with no recorded "
        f"delivery -- at ANY severity, which is the point (the five lost in "
        f"August were all P2):"
    ]
    for row in rows[:10]:
        never = "never delivered" if row.get("never_delivered") else \
            f"last delivered {row.get('last_delivered')}"
        lines.append(
            f"  - [{row.get('severity') or 'P?'}] {row.get('topic')} "
            f"({row.get('entity')}) -- gate {row.get('gate')}, "
            f"{row.get('gate_overdue_days')}d overdue, {never}, "
            f"owner {row.get('owner')}"
        )
    if len(rows) > 10:
        lines.append(f"  - ...and {len(rows) - 10} more")
    return "\n".join(lines)
