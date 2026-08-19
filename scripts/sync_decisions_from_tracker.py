#!/usr/bin/env python3
r"""PROPOSE-ONLY transcription: Org Remodel Tracker decisions -> decisions-pending.

cq-232fe6a541ff. The 2026-08-18 audit found the intake gap: five decisions (OSN
data source, Jerry DW access, BDM department lock, Eric LEX Learning Center, LEX
Phase 2) existed ONLY in the Airtable Org Remodel Tracker, table
`Pending / Build Needs`, each Type=Decision / Status=Open / Owner=Harrison with a
2026-08-13 gate. Every Cora surface that raises a pending decision reads ONE
source -- memory/decisions-pending.md -- so no lane had anything to deliver. The
bridge between the two registries was a human retyping them, and nobody did.

WHAT THIS IS, EXACTLY: it reads the tracker, diffs against decisions-pending.md,
and WRITES A PROPOSAL FILE INTO THE REPO. It never edits decisions-pending.md and
never touches Airtable. That file is canon-adjacent and D-011 governs it: Harrison
applies the entries. This script exists so the retyping is a review instead of an
act of memory.

  READ-ONLY on both sides. The tracker reader (connectors/airtable_org_tracker.py)
  is GET-only by construction with a pinned base+table -- deliberately NOT the
  dashboard client, whose allowlist is the two dashboard bases and whose
  credential cannot reach this base at all. This script writes exactly one file,
  under logs/, in the repo.

  THE LIVE READ IS UNVERIFIED as shipped: this session had no connector budget, so
  the mapping was exercised against a JSON fixture built from the five real tracker
  rows (--from-json), end to end into the gate alarm. The first live dry-run is
  Harrison's; if the credential lacks read scope on this base it reports "Airtable
  unavailable" and proposes nothing, which is the fail-soft direction.

The GATE DATE is the field that matters most. The gate-date escalation in
decision_lane can only fire on an entry that HAS one, and no entry in
decisions-pending.md carries one today -- so every proposal here emits
`**Gate**: YYYY-MM-DD` from the tracker's own deadline field.

OFFLINE VERIFIABLE: --from-json <file> feeds records from a JSON fixture instead
of Airtable, so the mapping can be exercised (and was, on the five real rows)
without a connector call.

Usage:
    .venv\Scripts\python.exe scripts\sync_decisions_from_tracker.py
    .venv\Scripts\python.exe scripts\sync_decisions_from_tracker.py --write-proposal
    .venv\Scripts\python.exe scripts\sync_decisions_from_tracker.py --from-json fixture.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env", override=True)

from cora import decision_lane  # noqa: E402

from cora.connectors import airtable_org_tracker as _tracker  # noqa: E402

TRACKER_BASE = _tracker.BASE_ID           # appAUZSQOCTnCO8yi, Org Remodel Tracker
TRACKER_TABLE = _tracker.TABLE_ID         # tbldM2EqIcho589Ql, "Pending / Build Needs"

#: Only these columns are requested (data minimization -- the table holds build
#: items and notes this job has no business reading).
FIELDS = ["Item", "Name", "Type", "Status", "Owner", "Severity", "Entity",
          "Gate date", "Deadline", "Notes", "Question"]

#: Row shape we act on. Anything else is skipped and counted, never guessed at.
DECISION_TYPE = "decision"
OPEN_STATUS = "open"


def _first(fields: dict[str, Any], *names: str) -> str:
    """First non-empty value among `names`. The tracker's column naming has
    drifted (Item/Name, Gate date/Deadline), so read a small set of aliases
    rather than one guess -- and never invent a value."""
    for name in names:
        value = fields.get(name)
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value if v)
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _iso_date(raw: str) -> str:
    """A date the file's own field rule accepts (absolute YYYY-MM-DD) or ""."""
    m = re.search(r"\d{4}-\d{2}-\d{2}", raw or "")
    return m.group(0) if m else ""


def to_entry(fields: dict[str, Any], *, today: date) -> dict[str, Any] | None:
    """One tracker row -> a proposal entry, or None when the row is out of scope."""
    if _first(fields, "Type").strip().lower() != DECISION_TYPE:
        return None
    if _first(fields, "Status").strip().lower() != OPEN_STATUS:
        return None
    topic = _first(fields, "Item", "Name")
    if not topic:
        return None
    gate = _iso_date(_first(fields, "Gate date", "Deadline"))
    return {
        "topic": topic[:140],
        "entity": _first(fields, "Entity") or "FNDR",
        "question": _first(fields, "Question", "Notes")[:300],
        "owner": _first(fields, "Owner") or "Harrison",
        # Severity is carried ACROSS, never upgraded. All five lost decisions are
        # P2, and the gate-date control is deliberately severity-blind -- quietly
        # promoting them to P1 to get them surfaced would hide the real defect.
        "severity": (_first(fields, "Severity") or "P2")[:4],
        "gate": gate,
        "surfaced": gate or today.isoformat(),
    }


def render_block(entry: dict[str, Any], *, today: date) -> str:
    """The canonical decisions-pending entry format, gate date included."""
    return "\n".join([
        f"### {entry['topic']}",
        f"- **Entity**: {entry['entity']}",
        f"- **Question**: {entry['question'] or 'see tracker row'}",
        f"- **Decision-maker**: {entry['owner']}",
        "- **Blockers**: (from the Org Remodel Tracker -- confirm before filing)",
        f"- **Severity**: {entry['severity']}",
        f"- **Surfaced**: {entry['surfaced']}",
        f"- **Last touched**: {entry['gate'] or today.isoformat()}",
        f"- **Gate**: {entry['gate'] or '(none recorded in the tracker)'}",
        f"- **Owner of next nudge**: {entry['owner']}",
        "- **Source**: Airtable Org Remodel Tracker, Pending / Build Needs",
        "",
    ])


def load_records(from_json: str | None) -> tuple[list[dict[str, Any]], str]:
    """(records, source-label). Records are each row's FIELDS dict."""
    if from_json:
        data = json.loads(Path(from_json).read_text(encoding="utf-8"))
        rows = data.get("records", data) if isinstance(data, dict) else data
        out = []
        for row in rows or []:
            if isinstance(row, dict):
                out.append(row.get("fields", row))
        return out, f"fixture {from_json}"

    # NOT the dashboard client: its base allowlist covers the two dashboard bases
    # and its credential cannot reach this one (see airtable_org_tracker's header).
    from cora.connectors import airtable_org_tracker
    result = airtable_org_tracker.list_pending_rows(fields=FIELDS)
    if not result.available:
        print(f"Airtable unavailable: {result.error}")
        return [], "airtable (unavailable)"
    return result.records, f"airtable {TRACKER_BASE}/{TRACKER_TABLE}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from-json", help="read records from a JSON fixture instead "
                                        "of Airtable (offline verification)")
    ap.add_argument("--write-proposal", action="store_true",
                    help="write the proposal file under logs/ (still NEVER edits "
                         "decisions-pending.md)")
    args = ap.parse_args()

    today = datetime.now(timezone.utc).date()
    records, source = load_records(args.from_json)
    print(f"source: {source}")
    print(f"rows read: {len(records)}")

    entries = [e for e in (to_entry(r, today=today) for r in records) if e]
    print(f"open decisions in the tracker: {len(entries)}")

    existing = decision_lane.load_entries(today=today)
    have = {decision_lane._topic_key(e["topic"]) for e in existing}
    print(f"entries already in decisions-pending.md: {len(existing)}")

    missing = [e for e in entries if decision_lane._topic_key(e["topic"]) not in have]
    gate_less = [e for e in entries if not e["gate"]]

    print(f"\nMISSING from decisions-pending.md: {len(missing)}")
    for entry in missing:
        overdue = ""
        if entry["gate"]:
            days = (today - datetime.strptime(entry["gate"], "%Y-%m-%d").date()).days
            overdue = f" -- gate {entry['gate']}, {days}d overdue" if days > 0 else \
                      f" -- gate {entry['gate']}"
        print(f"  - [{entry['severity']}] {entry['topic']}{overdue}")
    if gate_less:
        print(f"\n{len(gate_less)} tracker decision(s) carry NO gate date -- the "
              f"gate-date escalation cannot see those until one is set:")
        for entry in gate_less:
            print(f"  - {entry['topic']}")

    if not missing:
        print("\nNothing to propose.")
        return 0

    body = "\n".join([
        f"# Decision transcription proposal -- {today.isoformat()}",
        "",
        "PROPOSE-ONLY (D-011). Source: Airtable Org Remodel Tracker, "
        "`Pending / Build Needs`. Nothing below has been written to "
        "`memory/decisions-pending.md` -- Harrison files them.",
        "",
        f"{len(missing)} decision(s) open in the tracker and absent from the file:",
        "",
        *[render_block(e, today=today) for e in missing],
    ])

    if not args.write_proposal:
        print("\n--- proposal preview (not written) ---")
        print(body)
        print("\nDRY RUN -- re-run with --write-proposal to save it.")
        return 0

    out = _REPO_ROOT / "logs" / f"decision-sync-proposal-{today.isoformat()}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(body, encoding="utf-8")
    print(f"\nProposal written: {out}")
    print("decisions-pending.md was NOT modified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
