#!/usr/bin/env python3
"""One-shot, HARRISON-GATED bulk-expiry of the STALE operational proposed-updates cohort.

Harrison DECIDED 2026-07-10: accelerate the drain of the dead-end operational
backlog instead of waiting ~54 days for it to route out at 10/run. As of writing
the ledger (data/cora-proposed-memory-updates.jsonl) carried ~541 PENDING
operational rows (hubspot_note / asana_task / task_close / decision_capture) that
never make Cora smarter even if approved. This is the 2026-06-21 triage playbook,
re-scoped to only the STALE cohort (older than a cutoff) so fresh operational
items keep routing.

RELATION to the existing auto-expiry: run_knowledge_review._auto_expire_unrouted_
operational already flips UNROUTED operational rows to DISMISSED/expired_unrouted
after 14 days. This script does the same TERMINAL disposition on demand and with a
DISTINCT reason so it can be told apart in the audit and does NOT inflate the
flywheel's expired_unrouted_7d gauge.

TERMINAL STATE (verified against the live readers 2026-07-10): a bulk-expired row
is set to state="DISMISSED" + resolved_reason="expired_bulk". DISMISSED is the
codebase's terminal disposition (mirrors triage_proposed_updates.py's
"bulk_triage_ws17b" and run_knowledge_review's "expired_unrouted") -- it is inert
to every reader (get_pending_updates / correlate_reactions / resolve_update all key
on state=="PENDING") AND rotates to the archive naturally (rotate_resolved archives
APPROVED/DISMISSED). The distinct resolved_reason keeps flywheel_metrics'
expired_unrouted_7d / routed_to_owner_7d gauges clean, and graduated_trust_shadow
is unaffected (its FP metric reads the shadow logs / real reply-log reactions, not
the ledger state). A novel state value would NOT rotate and is avoided.

SAFETY (this is a reversible state change -- treated like one):
  * DRY-RUN BY DEFAULT. Nothing is written without --apply.
  * Writes a full MANIFEST (every expired update_id + per-type counts + samples).
  * --apply makes a timestamped .bak copy of the ledger BEFORE rewriting; to
    revert, restore the .bak.
  * Re-checks the ledger's (mtime, size) fingerprint just before the rewrite and
    ABORTS if it changed since load (the live bot appends to this file with no
    cross-process lock) -- so a fresh contribution landing mid-apply is never
    clobbered.
  * State change only -- NEVER a row deletion. Malformed lines are preserved
    verbatim. The archive file is never touched.
  * ALLOWLIST, not denylist: only the four operational types are touched, and only
    PENDING rows with NO dm_message_ts (rows already in front of Harrison, or the
    knowledge stream -- known_answer / generic / efficiency / founder -- are never
    touched by construction). Passing a protected type via --types is refused.

Usage:
  # 1. Dry-run (writes a manifest + a cutoff-sensitivity table; no changes):
  python scripts/expire_stale_operational_updates.py
  python scripts/expire_stale_operational_updates.py --cutoff-days 14

  # 2. After reviewing the manifest, Harrison applies (makes a .bak first):
  python scripts/expire_stale_operational_updates.py --cutoff-days 14 --apply
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LEDGER_PATH = _REPO_ROOT / "data" / "cora-proposed-memory-updates.jsonl"
_MANIFEST_DIR = _REPO_ROOT / "logs"

# The operational "dead-end" types Harrison scoped for bulk expiry (2026-07-10).
_DEFAULT_EXPIRE_TYPES = ("hubspot_note", "asana_task", "task_close", "decision_capture")

# Never expired in bulk -- the knowledge / human-contribution stream and the
# founder layer. (Belt-and-braces: these are already excluded by the allowlist;
# this set makes passing one via --types a hard error.)
_PROTECTED_TYPES = frozenset({"known_answer", "generic", "efficiency", "founder"})

_EXPIRE_REASON = "expired_bulk"
_DEFAULT_CUTOFF_DAYS = 14
# Cutoffs shown in the dry-run sensitivity table so Harrison can pick one.
_SENSITIVITY_CUTOFFS = (7, 10, 14, 21, 30)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_ts(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _is_info_for_cora_generic(rec: dict) -> bool:
    """A generic item contributed via #info-for-cora is a human knowledge note,
    not operational noise -- always keep it (mirrors triage_proposed_updates.py)."""
    if rec.get("update_type") != "generic":
        return False
    return (rec.get("payload") or {}).get("source") == "info-for-cora"


def _should_expire(rec: dict, expire_types: frozenset[str], cutoff_dt: datetime) -> bool:
    """True if this row is a STALE operational dead-end safe to bulk-expire."""
    if rec.get("state") != "PENDING":
        return False
    utype = rec.get("update_type", "")
    if utype in _PROTECTED_TYPES:
        return False
    if _is_info_for_cora_generic(rec):
        return False
    if utype not in expire_types:
        return False
    # Never touch a row already surfaced to Harrison (DM'd / routed to an owner).
    if str(rec.get("dm_message_ts") or "").strip():
        return False
    proposed = _parse_ts(rec.get("proposed_at"))
    if proposed is None:
        return False  # unparseable timestamp -> keep (fail-safe)
    return proposed < cutoff_dt


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


def _sensitivity_table(records: list[dict], expire_types: frozenset[str], now: datetime) -> dict[int, int]:
    """For each candidate cutoff, how many rows WOULD expire. Helps pick a cutoff."""
    out: dict[int, int] = {}
    for days in _SENSITIVITY_CUTOFFS:
        cutoff = now - timedelta(days=days)
        out[days] = sum(1 for r in records
                        if r.get("__raw__") is None and _should_expire(r, expire_types, cutoff))
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--types", default=",".join(_DEFAULT_EXPIRE_TYPES),
        help=f"Comma-separated update_types to expire (default: {','.join(_DEFAULT_EXPIRE_TYPES)}). "
             "known_answer/generic/efficiency/founder are refused.")
    parser.add_argument(
        "--cutoff-days", type=int, default=_DEFAULT_CUTOFF_DAYS,
        help=f"Only expire PENDING rows proposed more than N days ago (default: {_DEFAULT_CUTOFF_DAYS}).")
    parser.add_argument("--ledger", type=Path, default=_LEDGER_PATH,
                        help="Path to the proposed-updates ledger.")
    parser.add_argument("--manifest-dir", type=Path, default=_MANIFEST_DIR,
                        help="Directory to write the audit manifest into.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually expire (default is dry-run). Makes a .bak first.")
    args = parser.parse_args(argv)

    if args.cutoff_days < 0:
        print("ERROR: --cutoff-days must be >= 0.")
        return 1

    expire_types = frozenset(t.strip() for t in args.types.split(",") if t.strip())
    bad = expire_types & _PROTECTED_TYPES
    if bad:
        print(f"ERROR: refusing to expire protected types {sorted(bad)} "
              "(known_answer/generic/efficiency/founder are the knowledge + founder stream).")
        return 1
    if not expire_types:
        print("ERROR: no expire types given.")
        return 1

    ledger: Path = args.ledger
    if not ledger.exists():
        print(f"ERROR: ledger not found: {ledger}")
        return 1

    now = _now()
    cutoff_dt = now - timedelta(days=args.cutoff_days)

    # Fingerprint BEFORE reading (D-051): if captured AFTER the read, an append
    # that lands DURING the read is baked into the baseline, so the pre-rewrite
    # re-check passes and that fresh row is silently dropped from the live ledger
    # (it survives only in the .bak). Capturing before means any change during the
    # read makes now_fp differ -> abort (fail-safe, no silent loss).
    try:
        load_fp = (ledger.stat().st_mtime, ledger.stat().st_size)
    except OSError:
        load_fp = None
    records = _load_records(ledger)
    total = len(records)

    # Census + the expiry set.
    pending_by_type: Counter = Counter()
    to_expire: list[dict] = []
    expire_by_type: Counter = Counter()
    samples_by_type: dict[str, list[str]] = defaultdict(list)
    skipped_dmd = 0  # PENDING target rows skipped because already surfaced to Harrison

    for rec in records:
        if rec.get("__raw__") is not None:
            continue
        if rec.get("state") == "PENDING":
            pending_by_type[rec.get("update_type", "?")] += 1
            if (rec.get("update_type") in expire_types
                    and rec.get("update_type") not in _PROTECTED_TYPES
                    and not _is_info_for_cora_generic(rec)
                    and str(rec.get("dm_message_ts") or "").strip()):
                skipped_dmd += 1
        if _should_expire(rec, expire_types, cutoff_dt):
            to_expire.append(rec)
            ut = rec.get("update_type", "?")
            expire_by_type[ut] += 1
            if len(samples_by_type[ut]) < 5:
                samples_by_type[ut].append(_sample(rec.get("description", "")))

    pending_total = sum(pending_by_type.values())
    expire_total = len(to_expire)
    sensitivity = _sensitivity_table(records, expire_types, now)

    # ── Manifest (always, even on dry-run) ───────────────────────────────────
    args.manifest_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    manifest_path = args.manifest_dir / f"expire-stale-operational-manifest-{stamp}.json"
    manifest = {
        "generated_at": _now_iso(),
        "ledger": str(ledger),
        "mode": "apply" if args.apply else "dry-run",
        "cutoff_days": args.cutoff_days,
        "cutoff_before": cutoff_dt.isoformat(),
        "expire_types": sorted(expire_types),
        "protected_types": sorted(_PROTECTED_TYPES),
        "terminal_state": "DISMISSED",
        "resolved_reason": _EXPIRE_REASON,
        "ledger_total_rows": total,
        "pending_total": pending_total,
        "pending_by_type": dict(pending_by_type),
        "expire_total": expire_total,
        "expire_by_type": dict(expire_by_type),
        "skipped_already_surfaced": skipped_dmd,
        "cutoff_sensitivity": sensitivity,
        "samples_by_type": dict(samples_by_type),
        "expired_update_ids": [r.get("update_id", "?") for r in to_expire],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── Human-readable summary ────────────────────────────────────────────────
    print("=" * 72)
    print(f"Stale operational bulk-expiry  ({'APPLY' if args.apply else 'DRY-RUN'})")
    print("=" * 72)
    print(f"Ledger: {ledger}")
    print(f"Total rows: {total}   PENDING: {pending_total}")
    print(f"PENDING by type: {dict(pending_by_type)}")
    print(f"Expire types: {sorted(expire_types)}")
    print(f"Cutoff: proposed before {cutoff_dt.date()} ({args.cutoff_days}d ago)")
    print("-" * 72)
    print("Cutoff sensitivity (rows that WOULD expire at each cutoff):")
    for days in _SENSITIVITY_CUTOFFS:
        marker = "  <- selected" if days == args.cutoff_days else ""
        print(f"  >{days:>2}d:  {sensitivity[days]:>5}{marker}")
    if args.cutoff_days not in _SENSITIVITY_CUTOFFS:
        print(f"  >{args.cutoff_days:>2}d:  {expire_total:>5}  <- selected")
    print("-" * 72)
    print(f"WOULD EXPIRE: {expire_total} PENDING row(s) -> DISMISSED / {_EXPIRE_REASON}")
    for ut, n in sorted(expire_by_type.items(), key=lambda kv: -kv[1]):
        print(f"  {ut:18s} {n:6d}")
        for s in samples_by_type[ut][:3]:
            print(f"       e.g. {s}")
    print(f"Skipped (already surfaced to Harrison -- dm_message_ts set): {skipped_dmd}")
    print(f"\nManifest written: {manifest_path}")

    if not args.apply:
        print("\nDRY-RUN -- no changes written. Re-run with --apply to expire.")
        return 0

    if expire_total == 0:
        print("\nNothing to expire -- ledger unchanged.")
        return 0

    # ── Apply: re-check the fingerprint, back up, rewrite with state flipped ───
    try:
        now_fp = (ledger.stat().st_mtime, ledger.stat().st_size)
    except OSError:
        now_fp = None
    if load_fp is None or now_fp != load_fp:
        print("\nABORT: the ledger changed since it was loaded (a live process may have "
              "appended). Nothing was written. Re-run --apply when the bot and scheduled "
              "producers are idle.")
        return 1

    bak_path = ledger.with_name(ledger.name + f".bak-{stamp}")
    shutil.copy2(ledger, bak_path)
    print(f"\nBackup written: {bak_path}")

    expire_ids = {r.get("update_id") for r in to_expire}
    now_iso = _now_iso()
    flipped = 0
    tmp = ledger.with_suffix(ledger.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for rec in records:
            if rec.get("__raw__") is not None:
                fh.write(rec["__raw__"] + "\n")
                continue
            if (rec.get("state") == "PENDING"
                    and rec.get("update_id") in expire_ids
                    and _should_expire(rec, expire_types, cutoff_dt)):
                rec["state"] = "DISMISSED"
                rec["resolved_at"] = now_iso
                rec["resolved_reason"] = _EXPIRE_REASON
                flipped += 1
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(ledger)

    print(f"APPLIED: expired {flipped} PENDING row(s) -> DISMISSED / {_EXPIRE_REASON}. "
          f"Backup at {bak_path.name}.")
    print(f"To revert: restore {bak_path.name} over {ledger.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
