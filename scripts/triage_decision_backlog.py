#!/usr/bin/env python3
r"""One-shot, HARRISON-GATED triage of the decision_capture backlog (Fork 4).

The decisions lane (2026-08-01) made decision_capture a never-expiring one-tap
card lane. This script handles the PRE-EXISTING pool the old operational lane
left behind (the kickoff's "existing 63"):

  * REPORT (dry-run, DEFAULT -- writes nothing): the live PENDING decision pool
    by age/entity/confidence, rows recently AUTO-expired (recoverable), and the
    archive's already-dismissed majority (count only, read-only).
  * --apply: dismiss the STALE subset -- live PENDING decision rows older than
    --stale-days (default 14) and never DM'd -- with
    resolved_reason="fork4_backfill_stale". The recent remainder stays PENDING
    and rides the new card lane at 5/run.
  * --apply --rearm: additionally flip BACK to PENDING any live decision row
    AUTO-dismissed (expired_unrouted / auto_expired_dmd_unreacted) within
    --rearm-days (default 7) -- recovers rows the old TTL killed between the
    Fork-4 kickoff and this merge. Harrison-resolved rows (one-tap, emoji,
    routed_to_owner, bulk-triage) are NEVER re-armed, and a row that fails the
    LEX/PHI screen is never re-armed (it could only be excluded at the drain).

Safety rails (mirrors expire_stale_operational_updates.py): dry-run default;
fingerprint-abort if the ledger changes between load and rewrite; timestamped
.bak before any write; manifest to logs/ on every run; malformed ledger lines
preserved verbatim; the archive is READ-ONLY.

Usage:
    .venv\Scripts\python.exe scripts\triage_decision_backlog.py
    .venv\Scripts\python.exe scripts\triage_decision_backlog.py --apply
    .venv\Scripts\python.exe scripts\triage_decision_backlog.py --apply --rearm
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

_LEDGER_PATH = _REPO_ROOT / "data" / "cora-proposed-memory-updates.jsonl"
_ARCHIVE_PATH = _REPO_ROOT / "data" / "cora-proposed-memory-updates.archive.jsonl"
_MANIFEST_DIR = _REPO_ROOT / "logs"

_TYPE = "decision_capture"
_STALE_REASON = "fork4_backfill_stale"
_REARM_MARK = "fork4_backfill_rearm"
# Only AUTO-dismissals are recoverable; every Harrison-touched resolution stays.
_REARMABLE_REASONS = frozenset({"expired_unrouted", "auto_expired_dmd_unreacted"})

_DEFAULT_STALE_DAYS = 14
_MIN_SAFE_STALE_DAYS = 7  # below this you'd dismiss rows the new lane would card
_DEFAULT_REARM_DAYS = 7
_AGE_BUCKETS = (7, 14, 30, 60)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:  # noqa: BLE001
        return None


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
                records.append({"__raw__": line})  # preserved verbatim on rewrite
    return records


def _is_stale_pending(rec: dict, cutoff_dt: datetime) -> bool:
    """A live PENDING decision row old enough to dismiss as backfill-stale.
    Never touches a row already surfaced to Harrison (dm_message_ts set)."""
    if rec.get("update_type") != _TYPE or rec.get("state") != "PENDING":
        return False
    if str(rec.get("dm_message_ts") or "").strip():
        return False
    proposed = _parse_ts(rec.get("proposed_at"))
    if proposed is None:
        return False  # unparseable -> keep (fail-safe)
    return proposed < cutoff_dt


def _is_rearmable(rec: dict, rearm_cutoff: datetime) -> tuple[bool, str]:
    """(rearmable, skip_reason). Only recently AUTO-dismissed decision rows that
    pass the LEX/PHI screen come back."""
    if rec.get("update_type") != _TYPE or rec.get("state") != "DISMISSED":
        return False, ""
    if rec.get("resolved_reason") not in _REARMABLE_REASONS:
        return False, ""
    resolved = _parse_ts(rec.get("resolved_at"))
    if resolved is None or resolved < rearm_cutoff:
        return False, ""
    try:
        from cora.decision_inbox import screen_decision
        excluded, why = screen_decision(rec)
    except Exception:  # noqa: BLE001 -- fail closed
        return False, "screen_error"
    if excluded:
        return False, why
    return True, ""


def _age_census(rows: list[dict], now: datetime) -> dict[str, int]:
    out: dict[str, int] = {}
    for days in _AGE_BUCKETS:
        cutoff = now - timedelta(days=days)
        out[f">{days}d"] = sum(
            1 for r in rows
            if (_parse_ts(r.get("proposed_at")) or now) < cutoff)
    return out


def _entity_of(rec: dict) -> str:
    try:
        from cora.decision_inbox import entity_of
        return entity_of(rec) or "(none)"
    except Exception:  # noqa: BLE001
        return "(none)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--stale-days", type=int, default=_DEFAULT_STALE_DAYS,
                        help=f"Dismiss PENDING decision rows proposed more than N days "
                             f"ago (default: {_DEFAULT_STALE_DAYS}).")
    parser.add_argument("--rearm", action="store_true",
                        help="Also re-arm recently AUTO-expired decision rows back to "
                             "PENDING (with --apply).")
    parser.add_argument("--rearm-days", type=int, default=_DEFAULT_REARM_DAYS,
                        help=f"Re-arm window: auto-dismissed within the last N days "
                             f"(default: {_DEFAULT_REARM_DAYS}).")
    parser.add_argument("--ledger", type=Path, default=_LEDGER_PATH,
                        help="Path to the proposed-updates ledger.")
    parser.add_argument("--archive", type=Path, default=_ARCHIVE_PATH,
                        help="Path to the archive ledger (READ-ONLY census).")
    parser.add_argument("--manifest-dir", type=Path, default=_MANIFEST_DIR,
                        help="Directory to write the audit manifest into.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write (default is dry-run). Makes a .bak first.")
    parser.add_argument("--force", action="store_true",
                        help=f"Override the {_MIN_SAFE_STALE_DAYS}-day minimum-safe "
                             "stale floor.")
    args = parser.parse_args(argv)

    if args.stale_days < _MIN_SAFE_STALE_DAYS and not args.force:
        print(f"ERROR: --stale-days {args.stale_days} is below the "
              f"{_MIN_SAFE_STALE_DAYS}-day minimum-safe floor -- it would dismiss "
              "recent decisions the new card lane is about to surface. Pass --force "
              "if you really mean it.")
        return 1

    ledger: Path = args.ledger
    if not ledger.exists():
        print(f"ERROR: ledger not found: {ledger}")
        return 1

    now = _now()
    stale_cutoff = now - timedelta(days=args.stale_days)
    rearm_cutoff = now - timedelta(days=args.rearm_days)

    # Fingerprint BEFORE reading (D-051 pattern from the sibling script).
    try:
        load_fp = (ledger.stat().st_mtime, ledger.stat().st_size)
    except OSError:
        load_fp = None
    records = _load_records(ledger)

    decisions = [r for r in records
                 if r.get("__raw__") is None and r.get("update_type") == _TYPE]
    pending = [r for r in decisions if r.get("state") == "PENDING"]
    to_dismiss = [r for r in decisions if _is_stale_pending(r, stale_cutoff)]
    keep_pending = [r for r in pending if r not in to_dismiss]

    rearm_rows: list[dict] = []
    rearm_skipped: Counter = Counter()
    for r in decisions:
        ok, skip_why = _is_rearmable(r, rearm_cutoff)
        if ok:
            rearm_rows.append(r)
        elif skip_why:
            rearm_skipped[skip_why] += 1

    # Archive census (READ-ONLY -- the stale majority the kickoff lets go).
    archive_counts: Counter = Counter()
    if args.archive.exists():
        try:
            with args.archive.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("update_type") == _TYPE:
                        archive_counts[rec.get("state", "?")] += 1
        except OSError:
            archive_counts["(unreadable)"] += 1

    # ── Manifest (always, even on dry-run) ───────────────────────────────────
    args.manifest_dir.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    manifest_path = args.manifest_dir / f"triage-decision-backlog-manifest-{stamp}.json"
    manifest = {
        "generated_at": now.isoformat(),
        "ledger": str(ledger),
        "mode": "apply" if args.apply else "dry-run",
        "stale_days": args.stale_days,
        "rearm": bool(args.rearm),
        "rearm_days": args.rearm_days,
        "live_decision_rows": len(decisions),
        "live_pending": len(pending),
        "pending_age_census": _age_census(pending, now),
        "pending_by_entity": dict(Counter(_entity_of(r) for r in pending)),
        "pending_by_confidence": dict(Counter(r.get("confidence", "?") for r in pending)),
        "dismiss_total": len(to_dismiss),
        "dismissed_update_ids": [r.get("update_id", "?") for r in to_dismiss],
        "keep_pending_total": len(keep_pending),
        "rearm_total": len(rearm_rows),
        "rearm_update_ids": [r.get("update_id", "?") for r in rearm_rows],
        "rearm_skipped": dict(rearm_skipped),
        "archive_decision_counts": dict(archive_counts),
        "resolved_reason": _STALE_REASON,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")

    # ── Human-readable summary ────────────────────────────────────────────────
    print("=" * 72)
    print(f"Fork 4 decision-backlog triage  ({'APPLY' if args.apply else 'DRY-RUN'})")
    print("=" * 72)
    print(f"Ledger: {ledger}")
    print(f"Live decision rows: {len(decisions)}   PENDING: {len(pending)}")
    print(f"PENDING age census: {manifest['pending_age_census']}")
    print(f"PENDING by entity: {manifest['pending_by_entity']}")
    print(f"PENDING by confidence: {manifest['pending_by_confidence']}")
    print("-" * 72)
    print(f"WOULD DISMISS (stale, >{args.stale_days}d, never DM'd): "
          f"{len(to_dismiss)} -> DISMISSED / {_STALE_REASON}")
    for r in to_dismiss[:8]:
        print(f"   e.g. {str(r.get('description', ''))[:110]}")
    print(f"WOULD KEEP PENDING (ride the new card lane): {len(keep_pending)}")
    if args.rearm:
        print(f"WOULD RE-ARM (auto-expired within {args.rearm_days}d, screen-clean): "
              f"{len(rearm_rows)}")
        if rearm_skipped:
            print(f"   re-arm skipped (LEX/PHI screen / errors): {dict(rearm_skipped)}")
    print(f"Archive (READ-ONLY, the stale majority): {dict(archive_counts)}")
    print(f"\nManifest written: {manifest_path}")

    if not args.apply:
        print("\nDRY-RUN -- no changes written. Re-run with --apply"
              + (" --rearm" if args.rearm else "") + " to execute.")
        return 0

    if not to_dismiss and not (args.rearm and rearm_rows):
        print("\nNothing to change -- ledger unchanged.")
        return 0

    # ── Apply: re-check the fingerprint, back up, rewrite ────────────────────
    try:
        now_fp = (ledger.stat().st_mtime, ledger.stat().st_size)
    except OSError:
        now_fp = None
    if load_fp is None or now_fp != load_fp:
        print("\nABORT: the ledger changed since it was loaded (a live process may "
              "have appended). Nothing was written. Re-run when producers are idle.")
        return 1

    bak_path = ledger.with_name(ledger.name + f".bak-{stamp}")
    shutil.copy2(ledger, bak_path)
    print(f"\nBackup written: {bak_path}")

    dismiss_ids = {r.get("update_id") for r in to_dismiss}
    rearm_ids = {r.get("update_id") for r in rearm_rows} if args.rearm else set()
    now_iso = now.isoformat()
    n_dismissed = n_rearmed = 0
    tmp = ledger.with_suffix(ledger.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for rec in records:
            if rec.get("__raw__") is not None:
                fh.write(rec["__raw__"] + "\n")
                continue
            uid = rec.get("update_id")
            if uid in dismiss_ids and _is_stale_pending(rec, stale_cutoff):
                rec["state"] = "DISMISSED"
                rec["resolved_at"] = now_iso
                rec["resolved_reason"] = _STALE_REASON
                n_dismissed += 1
            elif uid in rearm_ids and _is_rearmable(rec, rearm_cutoff)[0]:
                rec["state"] = "PENDING"
                rec["resolved_at"] = None
                rec.pop("resolved_reason", None)
                rec["dm_message_ts"] = ""
                rec["dm_channel_id"] = ""
                rec["rearmed_at"] = now_iso
                rec["rearm_reason"] = _REARM_MARK
                n_rearmed += 1
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(ledger)

    print(f"APPLIED: dismissed {n_dismissed} stale row(s); re-armed {n_rearmed} "
          f"row(s). Backup at {bak_path.name}.")
    print(f"To revert: restore {bak_path.name} over {ledger.name}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
