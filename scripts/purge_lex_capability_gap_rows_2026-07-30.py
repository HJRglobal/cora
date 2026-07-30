#!/usr/bin/env python3
"""Fork 3c (Wave-1 flywheel-conversion calibration, 2026-07-30) -- one-time purge
of the raw LEX capability-ask text already persisted in logs/knowledge-gaps.jsonl
by the pre-parity-raise `code_queue._route_to_flywheel -> log_gap` path.

Three specific rows (entity=LEX, detector=code_queue_route, the 7/30 "how to
guide" asks) hold raw LEX capability-ask text -- non-PHI in this specific
instance (a staff how-to guide), but the class can embed PHI and the LEX wall
is fail-closed, so these rows are redacted regardless of content. Selection is
by EXACT ts + entity + detector match (not line number, which could shift) --
precise, conservative, and idempotent.

Two effects, both --apply-gated:
  1. Redact the `question` field of the 3 target rows in knowledge-gaps.jsonl to
     a fixed placeholder (all other fields -- ts/entity/channel/user/detector/
     gap -- are left as-is; `gap` already holds only the generic description
     "capability/knowledge ask routed from code-queue classifier").
  2. Mark the 3 gaps resolved in design/known-answers/.resolved-gaps.jsonl
     (action="capability_routed") so gap_autofill.load_open_gaps() stops
     surfacing them -- post-fix, an identical ask would never even reach the
     gap log (Fork 3a intercepts it at capture time into the code-queue
     instead), so retroactively closing these out of the open pool matches the
     new steady state.

Dry-run by default (prints what would change, touches nothing). Pass --apply
to write. Idempotent: a second --apply run is a no-op (redaction check is by
exact-value comparison; resolved-ledger append checks the existing ledger
first).

ORDERING (D-051 adversarial review finding): run
`scripts/triage_flywheel_pool_2026-07-30.py --write` (STEP 0's T0 baseline)
BEFORE running this script with --apply. This script's `mark_resolved` effect
removes the 3 target gaps from gap_autofill.load_open_gaps(), so if it runs
FIRST, the T0 baseline would snapshot 10 open gaps instead of the 13 the
handoff doc verified live -- understating the pool the 2-week review compares
against. Both scripts are safe to run in either order operationally (neither
corrupts live state), but this ordering preserves an accurate historical T0
snapshot.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora import gap_autofill  # noqa: E402

_TARGET_TS = frozenset({
    "2026-07-30T04:58:49.176210+00:00",
    "2026-07-30T05:01:15.827097+00:00",
    "2026-07-30T05:24:01.404247+00:00",
})
_REDACTED_QUESTION = "[LEX capability ask -- redacted at rest, routed to code-queue (Wave-1 Fork 3c)]"


def _gaps_log_path() -> Path:
    import os
    return Path(os.environ.get("KNOWLEDGE_GAPS_LOG_PATH")
                or _REPO_ROOT / "logs" / "knowledge-gaps.jsonl")


def _is_target(rec: dict) -> bool:
    return (
        rec.get("ts") in _TARGET_TS
        and str(rec.get("entity", "")).strip().upper() == "LEX"
        and rec.get("detector") == "code_queue_route"
    )


def plan() -> dict:
    path = _gaps_log_path()
    if not path.exists():
        return {"lines": [], "targets": [], "already_redacted": []}
    lines = path.read_text(encoding="utf-8").splitlines()
    targets, already = [], []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not _is_target(rec):
            continue
        if rec.get("question") == _REDACTED_QUESTION:
            already.append(rec["ts"])
        else:
            targets.append(rec)
    return {"lines": lines, "targets": targets, "already_redacted": already}


def apply_redaction(p: dict) -> int:
    """Redact the target rows in place. D-051 finding (adversarial review): the
    live bot process appends to knowledge-gaps.jsonl concurrently via
    knowledge_gaps.log_gap, and that write is guarded by an IN-PROCESS
    threading.Lock -- which provides zero protection against this SEPARATE
    process. Using a stale `plan()` snapshot (read at an arbitrary earlier
    time) to build the full rewritten file would silently drop any row the bot
    appended in between. Fixed by re-reading the file FRESH here, immediately
    before the write, so the only remaining race window is the few
    milliseconds between this read and the atomic tmp.replace() below --
    equivalent to any other file-rewrite race in this codebase. `target_ts`
    (from `p["targets"]`) is safe to reuse across the gap: those are historical
    rows that cannot change identity, only whether they still need redacting."""
    path = _gaps_log_path()
    target_ts = {t["ts"] for t in p["targets"]}
    fresh_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    out_lines = []
    n = 0
    for line in fresh_lines:
        stripped = line.strip()
        if not stripped:
            out_lines.append(line)
            continue
        try:
            rec = json.loads(stripped)
        except json.JSONDecodeError:
            out_lines.append(line)
            continue
        if rec.get("ts") in target_ts and _is_target(rec):
            rec["question"] = _REDACTED_QUESTION
            n += 1
        out_lines.append(json.dumps(rec, ensure_ascii=False))
    # Sanity check: a rewrite must never SHRINK the file (every fresh line is
    # preserved, only `question` values change) -- abort rather than risk
    # silently dropping rows if something unexpected happened mid-read.
    if len(out_lines) < len(fresh_lines):
        raise RuntimeError(
            f"apply_redaction: rewritten line count ({len(out_lines)}) is less "
            f"than the fresh read ({len(fresh_lines)}) -- aborting without writing"
        )
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    tmp.replace(path)
    return n


def mark_resolved(p: dict) -> int:
    resolved_path = gap_autofill._resolved_path()
    already_resolved = gap_autofill._load_resolved_ids()
    n = 0
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    with resolved_path.open("a", encoding="utf-8") as fh:
        for rec in p["targets"]:
            ts = rec.get("ts")
            if ts in already_resolved:
                continue
            fh.write(json.dumps({
                "id": ts,
                "action": "capability_routed",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "target_entity": rec.get("entity", "LEX"),
                "captured_entity": rec.get("entity", "LEX"),
                "source": "wave1_fork3c_purge",
            }, ensure_ascii=False) + "\n")
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Purge raw LEX capability-ask text from knowledge-gaps.jsonl (Fork 3c).")
    ap.add_argument("--apply", action="store_true",
                    help="Actually redact + mark resolved (default: dry-run print only).")
    args = ap.parse_args()

    p = plan()
    if p["already_redacted"]:
        print(f"Already redacted ({len(p['already_redacted'])}): {sorted(p['already_redacted'])}")
    if not p["targets"]:
        print("No un-redacted target rows found. Nothing to do.")
        return 0

    print(f"Target rows to redact ({len(p['targets'])}):")
    for rec in p["targets"]:
        print(f"  ts={rec['ts']} entity={rec.get('entity')} "
              f"question={rec.get('question', '')[:80]!r}")

    if not args.apply:
        print("\nDry-run: re-run with --apply to redact + mark resolved.")
        return 0

    n_redacted = apply_redaction(p)
    n_resolved = mark_resolved(p)
    print(f"\nRedacted {n_redacted} row(s) in {_gaps_log_path()}")
    print(f"Marked {n_resolved} gap(s) resolved in {gap_autofill._resolved_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
