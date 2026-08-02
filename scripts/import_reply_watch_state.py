"""One-time + nightly importer: B2's reply-watch-state.json -> the revops ledger.

Dry-run DEFAULT (prints the plan, writes nothing). --apply writes.

Rules (design section 4, locked 2026-08-01):
- Idempotent upsert on thread_key (mailbox + gmail_thread_id).
- The importer only adds threads or advances states with a fresher timestamp;
  it can NEVER regress a state written by a send event (enforced inside
  ledger.upsert_thread / ledger.transition, source='import').
- LEX threads never enter the DB (ledger raises LexThreadRejected; counted).
- Workstream labels normalized (Finance/Legal -> Finance-Legal, etc.).
- Deterministic escalation keyword screen on counterparty + note (fail-closed
  to escalated/Harrison). 'no nudge warranted' notes land in hold.

Usage:
  .venv\\Scripts\\python.exe scripts\\import_reply_watch_state.py            # dry run
  .venv\\Scripts\\python.exe scripts\\import_reply_watch_state.py --apply
  ... [--state-file PATH] (defaults to the Founder-OS live state file)
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(_REPO_ROOT / ".env", override=True)

from cora.revops import ledger, send_trust  # noqa: E402

_DEFAULT_STATE_FILE = Path(
    r"G:\My Drive\HJR-Founder-OS\00-Founder\_state\reply-watch-state.json"
)
_B2_MAILBOX = "harrison@hjrglobal.com"  # B2 scans Harrison's sent mail
_HOLD_MARKERS = ("no nudge warranted",)


def _read_state(path: Path) -> dict:
    try:
        from cora.drive_io import read_text as _drive_read  # type: ignore

        return json.loads(_drive_read(str(path)))
    except Exception:  # noqa: BLE001 - fall back to a plain bounded read
        return json.loads(path.read_text(encoding="utf-8"))


def _date_to_epoch(iso_date: str | None) -> float | None:
    if not iso_date:
        return None
    try:
        return _dt.datetime.fromisoformat(iso_date).timestamp()
    except ValueError:
        return None


def plan_thread(entry: dict) -> dict:
    ws = ledger.normalize_workstream(entry.get("workstream") or "Other")
    note = entry.get("note") or ""
    counterparty = entry.get("counterparty") or ""
    state = "awaiting_reply"
    hold_reason = None
    escalation_kw = ledger.escalation_screen(f"{counterparty}\n{note}")
    if any(m in note.lower() for m in _HOLD_MARKERS):
        state = "hold"
        hold_reason = note[:200]
    elif escalation_kw:
        state = "escalated"
    notes = note or None
    draft_id = entry.get("nudge_draft_id")
    if draft_id:
        notes = ((notes + " | ") if notes else "") + f"B2 nudge draft staged: {draft_id}"
    owner = (
        send_trust._DEFAULT_OWNER
        if state == "escalated"
        else send_trust.owner_for_workstream(ws)
    )
    return {
        "gmail_thread_id": entry.get("thread_id"),
        "counterparty_name": counterparty or None,
        "workstream": ws,
        "entity": entry.get("entity"),
        "state": state,
        "hold_reason": hold_reason,
        "escalation_keyword": escalation_kw,
        "owner": owner,
        "notes": notes,
        "last_outbound_ts": _date_to_epoch(entry.get("last_outbound_date")),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write to the ledger")
    parser.add_argument("--state-file", type=Path, default=_DEFAULT_STATE_FILE)
    args = parser.parse_args()

    data = _read_state(args.state_file)
    threads = data.get("threads") or []
    as_of = data.get("as_of")
    print(f"reply-watch-state as_of={as_of}: {len(threads)} threads")

    obs_ts = _date_to_epoch(as_of) or None
    counts = {"new": 0, "updated": 0, "lex_rejected": 0, "hold": 0, "escalated": 0, "errors": 0}
    conn = ledger.connect() if args.apply else None
    try:
        for entry in threads:
            plan = plan_thread(entry)
            if not plan["gmail_thread_id"]:
                counts["errors"] += 1
                continue
            tag = plan["state"]
            if tag in ("hold", "escalated"):
                counts[tag] += 1
            line = (
                f"  [{plan['state']:>14}] {plan['workstream']:<16} "
                f"{(plan['entity'] or '?'):<8} {plan['counterparty_name'] or plan['gmail_thread_id']}"
            )
            if plan["escalation_keyword"]:
                line += f"  (escalation: {plan['escalation_keyword']})"
            print(line)
            if not args.apply:
                continue
            try:
                existed = ledger.get_thread(
                    conn, ledger.make_thread_key(_B2_MAILBOX, plan["gmail_thread_id"])
                )
                ledger.upsert_thread(
                    conn,
                    mailbox=_B2_MAILBOX,
                    gmail_thread_id=plan["gmail_thread_id"],
                    counterparty_name=plan["counterparty_name"],
                    workstream=plan["workstream"],
                    entity=plan["entity"],
                    owner=plan["owner"],
                    playbook_id="silence_nudge",
                    state=plan["state"],
                    last_outbound_ts=plan["last_outbound_ts"],
                    hold_reason=plan["hold_reason"],
                    notes=plan["notes"],
                    source="import",
                    actor="import_reply_watch_state",
                    observation_ts=obs_ts,
                )
                counts["updated" if existed else "new"] += 1
            except ledger.LexThreadRejected:
                counts["lex_rejected"] += 1
            except Exception as exc:  # noqa: BLE001
                counts["errors"] += 1
                print(f"    ERROR: {exc}")
    finally:
        if conn is not None:
            conn.close()

    mode = "APPLIED" if args.apply else "DRY RUN (nothing written; use --apply)"
    print(f"\n{mode}: {counts}")
    return 0 if counts["errors"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
