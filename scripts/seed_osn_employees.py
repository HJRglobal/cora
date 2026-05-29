"""Seed OSN employee profiles into the shift scheduler database.

Usage:
    uv run python scripts/seed_osn_employees.py

Edit the EMPLOYEES list below with real Slack user IDs and details before running.
Re-running is safe — upsert logic means existing records are updated, not duplicated.

To find a Slack user ID:
  - In Slack, click the employee's profile → ⋮ (More) → Copy member ID
  - Or ask Cora: "@Cora what is @Username's Slack user ID" (if user_identity tool is configured)

Tier guide:
  "high"  — top performers; can be paired with anyone
  "mid"   — solid employees; can be paired with anyone
  "low"   — newer/developing employees; must be paired with a high or mid employee
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cora.tools.osn_shift_db import Employee, upsert_employee, get_all_active_employees, STORE_NAMES

# ── Edit this list with real data ─────────────────────────────────────────────
# Format: (slack_user_id, full_name, tier, [location_codes])
# Location codes: "GW" (Gilbert & Warner), "GM" (Gilbert & McKellips),
#                 "GF" (Greenfield & 60), "VVP" (Val Vista & Pecos)

EMPLOYEES = [
    # ("UXXXXXXXXX", "Jane Doe",    "high", ["GW", "GM", "GF", "VVP"]),
    # ("UYYYYYYYYY", "John Smith",  "mid",  ["GW", "GF"]),
    # ("UZZZZZZZZZ", "Alex Jones",  "low",  ["GM"]),
    #
    # Replace the examples above with real employees.
    # Paste one tuple per employee.
]

# ─────────────────────────────────────────────────────────────────────────────


def main():
    if not EMPLOYEES:
        print("⚠  EMPLOYEES list is empty — edit this script and add your staff before running.")
        return

    print(f"Seeding {len(EMPLOYEES)} employee(s)...\n")
    for uid, name, tier, locations in EMPLOYEES:
        emp = Employee(
            slack_user_id=uid,
            name=name,
            tier=tier,
            preferred_locations=locations,
            is_active=True,
        )
        upsert_employee(emp)
        loc_str = ", ".join(f"{c} ({STORE_NAMES[c]})" for c in locations)
        print(f"  ✅  {name} | {tier} | {loc_str}")

    print(f"\nDone. Current active employee count:")
    for e in get_all_active_employees():
        loc_str = ", ".join(e.preferred_locations)
        print(f"  {e.name} ({e.tier}) — {loc_str} — {e.slack_user_id}")


if __name__ == "__main__":
    main()
