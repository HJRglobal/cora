#!/usr/bin/env python3
"""WS17-B Phase 0 -- one-shot, HARRISON-GATED bulk-triage of the proposed-updates ledger.

The knowledge-review ledger (data/cora-proposed-memory-updates.jsonl) accumulated
~17.6k PENDING items, ~80% of which are operational "nudge" dead-ends
(hubspot_note / decision_capture / drive-extractor generic) that never make Cora
smarter even if approved and were never surfaced to Harrison. This script bulk-
DISMISSES that operational backlog so the daily knowledge drain (known_answer /
efficiency) is no longer buried behind it.

SAFETY (this is a destructive state change -- treat it like one):
  * DRY-RUN BY DEFAULT. Nothing is written without --apply.
  * Writes a full MANIFEST (every dismissed update_id + per-type counts + samples)
    so the action is auditable and reversible.
  * --apply makes a timestamped .bak copy of the ledger BEFORE rewriting.
  * KEEPS, always: known_answer, efficiency, AND generic items contributed via
    #info-for-cora (payload.source == "info-for-cora") -- those are genuine
    knowledge / human contributions, not operational noise.
  * Only PENDING rows are touched. APPROVED / DISMISSED rows are never altered.

Usage:
  # 1. Review what WOULD be dismissed (default types) -- writes a manifest, no changes:
  python scripts/triage_proposed_updates.py

  # 2. Review including the asana_task / task_close operational backlog too:
  python scripts/triage_proposed_updates.py --types hubspot_note,decision_capture,generic,asana_task,task_close

  # 3. After reviewing the manifest, Harrison applies (makes a .bak first):
  python scripts/triage_proposed_updates.py --apply

The default dismiss set is the three "dead-end" types the WS17-B grounding brief
scoped as the ~14k operational backlog. asana_task / task_close are NOT in the
default set (they are detected action suggestions); the manifest reports their
counts so Harrison can opt to include them via --types.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LEDGER_PATH = _REPO_ROOT / "data" / "cora-proposed-memory-updates.jsonl"
_MANIFEST_DIR = _REPO_ROOT / "logs"

# Default operational "dead-end" types the brief scoped for bulk dismissal.
_DEFAULT_DISMISS_TYPES = ("hubspot_note", "decision_capture", "generic")

# Never dismissed in bulk -- the genuine learning / human-contribution stream.
_PROTECTED_TYPES = frozenset({"known_answer", "efficiency"})

_DISMISS_REASON = "bulk_triage_ws17b"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_info_for_cora_generic(rec: dict) -> bool:
    """A generic item contributed via #info-for-cora is a human knowledge note,
    NOT operational noise -- always keep it."""
    if rec.get("update_type") != "generic":
        return False
    return (rec.get("payload") or {}).get("source") == "info-for-cora"


def _should_dismiss(rec: dict, dismiss_types: frozenset[str]) -> bool:
    if rec.get("state") != "PENDING":
        return False
    utype = rec.get("update_type", "")
    if utype in _PROTECTED_TYPES:
        return False
    if _is_info_for_cora_generic(rec):
        return False
    return utype in dismiss_types


def _load_records(path: Path) -> list[dict]:
    records: list[dict] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                # Preserve malformed lines verbatim so --apply never drops data.
                records.append({"__raw__": line})
    return records


def _sample(desc: str, n: int = 120) -> str:
    return (desc or "").replace("\n", " ")[:n]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--types",
        default=",".join(_DEFAULT_DISMISS_TYPES),
        help=f"Comma-separated update_types to dismiss (default: {','.join(_DEFAULT_DISMISS_TYPES)}). "
             "known_answer/efficiency and #info-for-cora generics are always kept.",
    )
    parser.add_argument("--ledger", type=Path, default=_LEDGER_PATH,
                        help="Path to the proposed-updates ledger.")
    parser.add_argument("--manifest-dir", type=Path, default=_MANIFEST_DIR,
                        help="Directory to write the audit manifest into.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually dismiss (default is dry-run). Makes a .bak first.")
    args = parser.parse_args()

    dismiss_types = frozenset(t.strip() for t in args.types.split(",") if t.strip())
    bad = dismiss_types & _PROTECTED_TYPES
    if bad:
        print(f"ERROR: refusing to dismiss protected types {sorted(bad)} "
              "(known_answer/efficiency are the learning stream).")
        return 1

    ledger: Path = args.ledger
    if not ledger.exists():
        print(f"ERROR: ledger not found: {ledger}")
        return 1

    records = _load_records(ledger)
    total = len(records)
    # Capture the ledger fingerprint at load time. The live bot appends to this
    # file (#info-for-cora) with no cross-process lock, so on --apply we re-check
    # this just before the rewrite and abort if it changed — otherwise a fresh
    # human contribution landing mid-apply would be overwritten AND absent from
    # the .bak (irreversible loss of exactly the protected type).
    try:
        _load_fp = (ledger.stat().st_mtime, ledger.stat().st_size)
    except OSError:
        _load_fp = None

    # Census of current PENDING state, and the dismissal set.
    pending_by_type: Counter = Counter()
    pending_kept_info_generic = 0
    to_dismiss: list[dict] = []
    dismiss_by_type: Counter = Counter()
    samples_by_type: dict[str, list[str]] = defaultdict(list)

    for rec in records:
        if rec.get("__raw__") is not None:
            continue
        if rec.get("state") == "PENDING":
            pending_by_type[rec.get("update_type", "?")] += 1
            if _is_info_for_cora_generic(rec):
                pending_kept_info_generic += 1
        if _should_dismiss(rec, dismiss_types):
            to_dismiss.append(rec)
            ut = rec.get("update_type", "?")
            dismiss_by_type[ut] += 1
            if len(samples_by_type[ut]) < 5:
                samples_by_type[ut].append(_sample(rec.get("description", "")))

    pending_total = sum(pending_by_type.values())
    dismiss_total = len(to_dismiss)

    # ── Write the manifest (always, even on dry-run) ─────────────────────────
    args.manifest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest_path = args.manifest_dir / f"triage-manifest-{stamp}.json"
    manifest = {
        "generated_at": _now_iso(),
        "ledger": str(ledger),
        "mode": "apply" if args.apply else "dry-run",
        "dismiss_types": sorted(dismiss_types),
        "protected_types_kept": sorted(_PROTECTED_TYPES),
        "ledger_total_rows": total,
        "pending_total": pending_total,
        "pending_by_type": dict(pending_by_type),
        "info_for_cora_generics_kept": pending_kept_info_generic,
        "dismiss_total": dismiss_total,
        "dismiss_by_type": dict(dismiss_by_type),
        "samples_by_type": dict(samples_by_type),
        "dismissed_update_ids": [r.get("update_id", "?") for r in to_dismiss],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    # ── Human-readable summary ───────────────────────────────────────────────
    print("=" * 72)
    print(f"WS17-B proposed-updates triage  ({'APPLY' if args.apply else 'DRY-RUN'})")
    print("=" * 72)
    print(f"Ledger: {ledger}")
    print(f"Total rows: {total}   PENDING: {pending_total}")
    print(f"PENDING by type: {dict(pending_by_type)}")
    print(f"Dismiss types: {sorted(dismiss_types)}")
    print(f"#info-for-cora generics KEPT: {pending_kept_info_generic}")
    print("-" * 72)
    print(f"WOULD DISMISS: {dismiss_total} PENDING row(s)")
    for ut, n in sorted(dismiss_by_type.items(), key=lambda kv: -kv[1]):
        print(f"  {ut:18s} {n:6d}")
        for s in samples_by_type[ut][:3]:
            print(f"       e.g. {s}")
    kept = pending_total - dismiss_total
    print("-" * 72)
    print(f"Would KEEP {kept} PENDING row(s): "
          f"{ {k: v for k, v in pending_by_type.items() if dismiss_by_type.get(k, 0) < v} }")
    if not dismiss_by_type.get("asana_task") and pending_by_type.get("asana_task"):
        print(f"\nNOTE: {pending_by_type['asana_task']} asana_task + "
              f"{pending_by_type.get('task_close', 0)} task_close PENDING are NOT in the "
              "default dismiss set. Add them via --types if you want them cleared too.")
    print(f"\nManifest written: {manifest_path}")

    if not args.apply:
        print("\nDRY-RUN -- no changes written. Re-run with --apply to dismiss.")
        return 0

    if dismiss_total == 0:
        print("\nNothing to dismiss -- ledger unchanged.")
        return 0

    # ── Apply: re-check the ledger hasn't changed since load, then back up ────
    try:
        now_fp = (ledger.stat().st_mtime, ledger.stat().st_size)
    except OSError:
        now_fp = None
    if _load_fp is None or now_fp != _load_fp:
        print("\nABORT: the ledger changed since it was loaded (a live process may "
              "have appended). Nothing was written. Re-run --apply when the bot and "
              "scheduled producers are idle.")
        return 1

    # ── Backup, then rewrite with state flipped ──────────────────────────────
    bak_path = ledger.with_name(ledger.name + f".bak-{stamp}")
    shutil.copy2(ledger, bak_path)
    print(f"\nBackup written: {bak_path}")

    dismiss_ids = {r.get("update_id") for r in to_dismiss}
    now = _now_iso()
    flipped = 0
    tmp = ledger.with_suffix(ledger.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for rec in records:
            if rec.get("__raw__") is not None:
                fh.write(rec["__raw__"] + "\n")
                continue
            if (rec.get("state") == "PENDING"
                    and rec.get("update_id") in dismiss_ids
                    and _should_dismiss(rec, dismiss_types)):
                rec["state"] = "DISMISSED"
                rec["resolved_at"] = now
                rec["resolved_reason"] = _DISMISS_REASON
                flipped += 1
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(ledger)

    print(f"APPLIED: dismissed {flipped} PENDING row(s). Backup at {bak_path.name}.")
    print(f"To revert: restore {bak_path.name} over {ledger.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
