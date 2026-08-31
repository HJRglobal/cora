#!/usr/bin/env python
"""Purge the 8/24-8/25 test-fixture rows that reached live canon (cq-eba0861fc043).

DRY-RUN BY DEFAULT. Pass --apply to write. Harrison runs the --apply.

WHAT HAPPENED
    tests/conftest.py redirects a list of module-constant ledger paths to tmp, but
    knowledge_review._PROPOSED_UPDATES_PATH was never on that list (its sibling
    _AUTOWRITE_AUDIT_PATH is). So a test-suite run appended two synthetic
    "gapfill-*" proposals to the LIVE review ledger; one was later one-tapped and
    gap_autofill.apply_known_answer wrote it into live canon.

    The pollution is NOT confined to the 8/24 window: the same unredirected
    constant took a lexicon-teach fixture on 2026-08-02 (see --include-archive).

WHY THIS IS CONTENT-KEYED, NOT LINE-KEYED
    Line numbers drift -- the live bot appends to two of these files daily. Every
    target below is located by an exact synthetic key:
        gap_ts == "g1"  |  answered_by == "U1"  |  eval id == "auto-ka-1"
    NEVER by the answer text. "Net 30" appears 98 times in the archive as REAL
    invoice terms (Unis LLC, Dave Bang Associates, OSN security monitoring). A
    grep-purge on the answer string would destroy real business records.

ORDER IS LOAD-BEARING
    scripts/run_kb_evals.py satisfies the auto-ka-1 case from static context OR the
    KB. Removing the fndr.md entry while leaving the eval case turns Monday's KB
    eval RED on a case that is now correctly unanswerable. Both are in this one
    script so they land together.

WHAT IS DELIBERATELY NOT TOUCHED
  * The two APPROVED ledger rows are DELETED outright, never reverted to PENDING.
    knowledge_review.process_one_tap_action refuses anything not PENDING, so as
    APPROVED they are inert; flipping them back to PENDING while removing the
    "g1" resolved-gap row would re-arm the F3E twin's short-circuit and let a
    re-tap write the fixture into f3e.md -- a second-entity leak that never
    actually happened.
  * logs/graduated-trust-shadow-*.jsonl -- the audit record of the classifier
    seeing the fixture is historical truth.
  * The derived KB chunks -- the next static-md sync and Drive sweep replace them
    on conflict. Let them self-heal rather than hand-deleting vec0 rows.
  * data/evals/golden-set-auto.yaml and design/known-answers/.resolved-gaps.jsonl
    both carry UNCOMMITTED REAL bot rows in the working tree. This script edits in
    place, line-targeted. Never restore either from a git blob.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]

FIXTURE_HEADER = "**[2026-08-25] terms** _(gap autofill -- teammate DM)_"
FIXTURE_Q = "Q: What are the payment terms?"
FIXTURE_A = "A: Payment terms are Net 30."


def _known_answers_dir() -> Path:
    """Resolve the LIVE store the same way gap_autofill does.

    .env sets KNOWN_ANSWERS_DIR (Drive) but NOT RESOLVED_GAPS_PATH, so the two
    halves of apply_known_answer write to DIFFERENT TREES: the answer lands on G:
    and the resolved-gap ledger lands in the repo. A purge pointed only at
    design/known-answers/ would report success and change nothing -- the repo's
    fndr.md is clean (mtime Jul 9).
    """
    env = os.environ.get("KNOWN_ANSWERS_DIR")
    if env:
        return Path(env)
    try:
        from dotenv import dotenv_values

        val = (dotenv_values(_REPO / ".env") or {}).get("KNOWN_ANSWERS_DIR")
        if val:
            return Path(val)
    except Exception:
        pass
    return _REPO / "design" / "known-answers"


def purge_known_answer(apply: bool) -> tuple[int, list[str]]:
    """Remove the fixture block from the LIVE fndr.md, bounded by entry markers."""
    path = _known_answers_dir() / "fndr.md"
    notes = ["target: %s" % path]
    if not path.exists():
        return 0, notes + ["  SKIP: file not found"]
    lines = io.open(path, encoding="utf-8").read().splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == FIXTURE_HEADER)
    except StopIteration:
        return 0, notes + ["  clean: fixture header not present"]
    end = start + 1
    while end < len(lines):
        s = lines[end].strip()
        if s.startswith("**[") or s.startswith("## ") or s.startswith("### "):
            break
        end += 1
    block = lines[start:end]
    body = "\n".join(block)
    if FIXTURE_Q not in body or FIXTURE_A not in body:
        return 0, notes + ["  REFUSED: block does not contain the expected Q and A"]
    if len(block) > 6:
        return 0, notes + ["  REFUSED: block unexpectedly long (%d lines)" % len(block)]
    for ln in block:
        notes.append("  - " + ln)
    cut_from = start - 1 if start and not lines[start - 1].strip() else start
    if apply:
        rest = lines[:cut_from] + lines[end:]
        io.open(path, "w", encoding="utf-8", newline="\n").write("\n".join(rest) + "\n")
    return 1, notes


def purge_eval_case(apply: bool) -> tuple[int, list[str]]:
    """Drop the auto-ka-1 case. Every real id is auto-ka-<20 digits> or
    auto-note-<12 hex>; auto-ka-1 can only come from a gap_ts of "1"."""
    path = _REPO / "data" / "evals" / "golden-set-auto.yaml"
    notes = ["target: %s" % path]
    if not path.exists():
        return 0, notes + ["  SKIP: file not found"]
    lines = io.open(path, encoding="utf-8").read().splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == "- id: auto-ka-1")
    except StopIteration:
        return 0, notes + ["  clean: auto-ka-1 not present"]
    end = start + 1
    while end < len(lines) and not lines[end].startswith("- "):
        end += 1
    for ln in lines[start:end]:
        notes.append("  - " + ln)
    if apply:
        io.open(path, "w", encoding="utf-8", newline="\n").write(
            "\n".join(lines[:start] + lines[end:]) + "\n"
        )
    return 1, notes


def _purge_jsonl(path: Path, predicate, label: str, apply: bool) -> tuple[int, list[str]]:
    notes = ["target: %s" % path]
    if not path.exists():
        return 0, notes + ["  SKIP: file not found"]
    kept: list[str] = []
    dropped: list[str] = []
    for raw in io.open(path, encoding="utf-8"):
        if not raw.strip():
            kept.append(raw)
            continue
        try:
            row = json.loads(raw)
        except Exception:
            kept.append(raw)
            continue
        (dropped if predicate(row) else kept).append(raw)
    for raw in dropped:
        notes.append("  - " + raw.strip()[:200])
    if apply and dropped:
        io.open(path, "w", encoding="utf-8", newline="\n").write("".join(kept))
    if not dropped:
        notes.append("  clean: no %s rows" % label)
    return len(dropped), notes


def _is_fixture_proposal(row: dict) -> bool:
    p = row.get("payload") or {}
    return p.get("gap_ts") == "g1" and p.get("answered_by") == "U1"


def _is_fixture_resolved_gap(row: dict) -> bool:
    return row.get("id") == "g1"


def _is_fixture_lexicon(row: dict) -> bool:
    p = row.get("payload") or {}
    return (
        str(row.get("update_id", "")).startswith("lexicon-taught-")
        and p.get("term") == "x"
        and p.get("canonical_name") == "X meaning"
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument(
        "--include-archive",
        action="store_true",
        help="also drop the inert 2026-08-02 lexicon-teach fixture from the archive "
        "(cosmetic -- its canon target data/maps/lexicon/*.yaml is clean; its value "
        "is as EVIDENCE the hole predates 8/24 by three weeks)",
    )
    args = ap.parse_args()

    print("=== fixture-pollution purge (%s) ===\n" % ("APPLY" if args.apply else "DRY RUN"))

    jobs = [
        ("live known-answer (canon)", lambda: purge_known_answer(args.apply)),
        ("KB eval case", lambda: purge_eval_case(args.apply)),
        (
            "review ledger proposals",
            lambda: _purge_jsonl(
                _REPO / "data" / "cora-proposed-memory-updates.jsonl",
                _is_fixture_proposal,
                "gap_ts=g1/answered_by=U1",
                args.apply,
            ),
        ),
        (
            "resolved-gap row",
            lambda: _purge_jsonl(
                _REPO / "design" / "known-answers" / ".resolved-gaps.jsonl",
                _is_fixture_resolved_gap,
                'id="g1"',
                args.apply,
            ),
        ),
    ]
    if args.include_archive:
        jobs.append(
            (
                "archive lexicon fixture (8/02)",
                lambda: _purge_jsonl(
                    _REPO / "data" / "cora-proposed-memory-updates.archive.jsonl",
                    _is_fixture_lexicon,
                    "lexicon-taught x",
                    args.apply,
                ),
            )
        )

    total = 0
    for label, fn in jobs:
        count, notes = fn()
        total += count
        verb = "removed" if args.apply else "would remove"
        print("[%s] %s %d" % (label, verb, count))
        for n in notes:
            print("   " + n)
        print()

    print("total rows/blocks: %d" % total)
    if not args.apply:
        print("\nDRY RUN -- nothing written. Re-run with --apply to execute.")
        print("Spot-check the listed rows by hand before applying (smoke #2).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
