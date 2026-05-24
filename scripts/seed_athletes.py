"""Seed the influencer handle registry from data/seed/athletes.yaml.

Run this once after populating the seed file, then re-run whenever you add
or update athletes:

    uv run python scripts/seed_athletes.py

The script is IDEMPOTENT — it upserts by (athlete_name, platform), so
re-running it is safe and won't create duplicate rows.

Output:
    Prints a summary of how many handles were inserted vs. updated.
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

# Add src/ to path so cora package resolves without an editable install
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from cora.tools import influencer_client  # noqa: E402  (after sys.path patch)


def main() -> None:
    seed_path = _REPO_ROOT / "data" / "seed" / "athletes.yaml"
    if not seed_path.exists():
        print(f"ERROR: seed file not found at {seed_path}")
        sys.exit(1)

    with open(seed_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    athletes: list[dict] = data.get("athletes") or []
    if not athletes:
        print("No athletes found in seed file. Add entries under the `athletes:` key and re-run.")
        sys.exit(0)

    inserted = 0
    updated = 0
    skipped = 0

    for entry in athletes:
        if not isinstance(entry, dict):
            print(f"  SKIP — malformed entry (not a dict): {entry!r}")
            skipped += 1
            continue

        athlete_name = (entry.get("athlete_name") or "").strip()
        entity = (entry.get("entity") or "F3E").strip().upper()
        handles: dict = entry.get("handles") or {}

        if not athlete_name:
            print("  SKIP — missing athlete_name")
            skipped += 1
            continue
        if not handles:
            print(f"  SKIP — {athlete_name}: no handles defined")
            skipped += 1
            continue

        for platform, raw_handle in handles.items():
            if not raw_handle:
                continue
            platform = platform.strip().lower()
            handle = str(raw_handle).strip().lstrip("@")

            # Check if already exists so we can report insert vs update
            existing = influencer_client.get_athlete_by_handle(platform, handle)
            if existing and existing["athlete_name"].lower() == athlete_name.lower():
                # Already registered with same name — no-op but count as "updated"
                updated += 1
                tag = f"  OK (already registered) — {athlete_name} / {platform} @{handle} [{entity}]"
            else:
                try:
                    influencer_client.register_handle(
                        athlete_name=athlete_name,
                        platform=platform,
                        handle=handle,
                        entity=entity,
                        added_by="seed_athletes.py",
                    )
                    inserted += 1
                    tag = f"  REGISTERED — {athlete_name} / {platform} @{handle} [{entity}]"
                except influencer_client.InfluencerClientError as exc:
                    print(f"  ERROR — {athlete_name} / {platform} @{handle}: {exc}")
                    skipped += 1
                    continue
            print(tag)

    print(f"\nSeed complete: {inserted} inserted, {updated} already-current, {skipped} skipped.")
    if inserted or updated:
        print("Run `uv run python scripts/run_influencer_scan.py` to verify the scanner can reach the registered accounts.")


if __name__ == "__main__":
    main()
