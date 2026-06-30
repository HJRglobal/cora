"""Weekly per-person involvement-dossier refresh (North Star pillar 4).

Iterates the roster and runs the SAME pull -> scrub -> synthesize -> write-back
as the on-demand `cora_person_dossier` tool, writing each person's
`_brain/people/{slug}.md` "Recent involvements" section.

Scheduled task: `cowork-cora-person-dossier-refresh`, Sunday 16:30 AZ (outside the
03:00-09:00 stagger window; ahead of Friction Mining 17:30). SCRIPT-SIDE -- it
spawns a fresh process importing on-disk source, so editing it needs NO bot restart.

Self-bounded: the script's own time budget is the real control (the briefing-task
lesson -- a task ExecutionTimeLimit SIGKILLs the wrapper, not the python child). On
budget exhaustion it stops and LOGS who was skipped (no silent truncation).

Usage:
  python scripts/run_person_dossier_refresh.py [--dry-run] [--only SLUG] [--days N]
                                               [--budget-seconds N]
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# Standalone scripts MUST load .env themselves (D-058) -- the bot's load happens in
# app.py, which this script does not import.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except Exception:  # noqa: BLE001 -- dotenv optional; env may already be set
    pass

# Make `src` importable when run as a bare script.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.cora import person_identity  # noqa: E402
from src.cora.tools import person_dossier  # noqa: E402

log = logging.getLogger("person_dossier_refresh")

# Founder has no self-dossier (he profiles others).
_FOUNDER_SLUG = "harrison-rogers"
_DEFAULT_BUDGET_SEC = 2700.0   # ~45 min, well under any task limit; self-bound is the control
_PER_PERSON_FLOOR_SEC = 40.0   # stop before starting a person if less than this remains


def _has_pullable_identity(p: "person_identity.PersonIdentity") -> bool:
    """Skip people with NO reachable source key -- the build would just return
    'no signals' and waste an LLM cycle (e.g. Tessa, an unmapped registry entry)."""
    return bool(p.mailboxes or p.asana_gid or p.hubspot_owner_id or p.all_emails)


def main() -> int:
    ap = argparse.ArgumentParser(description="Weekly per-person involvement-dossier refresh.")
    ap.add_argument("--dry-run", action="store_true", help="Synthesize but write nothing.")
    ap.add_argument("--only", default="", help="Refresh just this slug (e.g. tommy-anderson).")
    ap.add_argument("--days", type=int, default=14, help="Lookback window (default 14, max 30).")
    ap.add_argument("--budget-seconds", type=float, default=_DEFAULT_BUDGET_SEC,
                    help="Overall self-bound; stop + log skips when exhausted.")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    roster = person_identity.all_people()
    targets = [
        p for p in roster
        if p.slug != _FOUNDER_SLUG and _has_pullable_identity(p)
        and (not args.only or p.slug == args.only.strip().lower())
    ]
    if args.only and not targets:
        log.warning("No roster match for --only %r", args.only)
        return 1

    log.info(
        "person_dossier_refresh START dry_run=%s days=%d targets=%d budget=%.0fs",
        args.dry_run, args.days, len(targets), args.budget_seconds,
    )

    deadline = time.monotonic() + max(60.0, args.budget_seconds)
    written = skipped_budget = no_signal = phi_dropped = errored = 0
    not_reached: list[str] = []

    for p in targets:
        if time.monotonic() + _PER_PERSON_FLOOR_SEC > deadline:
            not_reached.append(p.slug)
            continue
        try:
            result = person_dossier.build_dossier(
                p, days=args.days, write_back_enabled=True, dry_run=args.dry_run,
            )
        except Exception as exc:  # noqa: BLE001 -- one person must never abort the run
            log.warning("person_dossier_refresh: build crashed for %s: %s", p.slug, exc)
            errored += 1
            continue
        if result.phi_dropped:
            phi_dropped += 1
        elif result.written:
            written += 1
        elif result.body is None:
            no_signal += 1
        log.info(
            "  %s -> written=%s body=%s coverage=%s",
            p.slug, result.written, bool(result.body),
            {k: v for k, v in result.coverage.items() if v not in ("skipped", "pending")},
        )

    skipped_budget = len(not_reached)
    log.info(
        "person_dossier_refresh DONE written=%d no_signal=%d phi_dropped=%d errored=%d "
        "skipped_over_budget=%d%s",
        written, no_signal, phi_dropped, errored, skipped_budget,
        (" [" + ", ".join(not_reached) + "]") if not_reached else "",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
