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
#
# Slack IDs marked SLACK_TBD_* are placeholders — OSN staff are not yet in the
# HJR Slack workspace. Replace each placeholder with the real Slack user ID once
# the employee accepts their workspace invite.
# Format: In Slack → click profile → ⋮ More → Copy member ID (starts with U)

EMPLOYEES = [
    # ── HIGH tier — key holders who can open AND close alone ──────────────────
    ("SLACK_TBD_CP", "Corey Patten",       "high", ["GF", "GM", "GW", "VVP"]),
    ("SLACK_TBD_CH", "Cody Hale",          "high", ["GF", "GW", "VVP"]),
    ("SLACK_TBD_ED", "Easton Doherty",     "high", ["GF", "GM", "GW", "VVP"]),
    ("SLACK_TBD_TH", "Truston Hosch",      "high", ["GF", "GM"]),
    ("SLACK_TBD_MP", "Micahlind Pelletier","high", ["GF", "GW", "VVP"]),
    ("SLACK_TBD_VT", "Victor Trujillo",    "high", ["GF", "GM"]),
    ("SLACK_TBD_MH", "Mason Hayes",        "high", ["GW", "VVP"]),
    ("SLACK_TBD_JM1","Jack Murphy",        "high", ["GF", "GM"]),
    ("SLACK_TBD_JA", "Jesse Archibald",    "high", ["GF", "GM"]),
    ("SLACK_TBD_AH", "Adam Hamzeh",        "high", ["GF", "GW", "VVP"]),

    # ── MID tier — key holders; open OR close limited ─────────────────────────
    ("SLACK_TBD_KK", "Kristy Kelly",       "mid",  ["GM", "GW"]),
    ("SLACK_TBD_AT", "Anthony Tindall",    "mid",  ["GF", "GW", "VVP"]),
    ("SLACK_TBD_JM2","Jackson McCutcheon", "mid",  ["GW", "VVP"]),
    ("SLACK_TBD_KC", "Kylie Crisp",        "mid",  ["GF", "GW", "VVP"]),
    ("SLACK_TBD_JT", "Jaylie Taylor",      "mid",  ["GF", "GW", "VVP"]),
    ("SLACK_TBD_CM", "Carson Meyer",       "mid",  ["GF", "GW", "VVP"]),

    # ── LOW tier — developing employees; must pair with high or mid ───────────
    ("SLACK_TBD_MO", "McKinley Oswald",    "low",  ["GF", "GW", "VVP"]),
    ("SLACK_TBD_LJ", "Logan Joiner",       "low",  ["GF", "GW", "VVP"]),
    ("SLACK_TBD_BB", "Bryce Bigler",       "low",  ["GF", "GM"]),
    ("SLACK_TBD_JH", "Justin Hill",        "low",  ["GF", "GM", "GW"]),
    ("SLACK_TBD_OY", "Olivia Yellen",      "low",  ["GF", "GM", "GW", "VVP"]),
    ("SLACK_TBD_CS", "Caleb Santana",      "low",  ["GF", "GM"]),
    ("SLACK_TBD_AD", "Ave Dommer",         "low",  ["GF", "GW", "VVP"]),
    ("SLACK_TBD_AA", "Alizabeth Aguilar",  "low",  ["GF", "GW"]),
    ("SLACK_TBD_SB", "Spencer Becker",     "low",  ["GF", "GW", "VVP"]),
    ("SLACK_TBD_KU", "Katie Udall",        "low",  ["GF"]),
    ("SLACK_TBD_NC", "Nicole Carstens",    "low",  ["GF", "GM", "GW", "VVP"]),
    ("SLACK_TBD_EL", "Ethan Lemmons",      "low",  ["GF", "GM", "GW", "VVP"]),
    ("SLACK_TBD_JP", "Joselyn Ponce",      "low",  ["GF", "GM", "GW", "VVP"]),
    ("SLACK_TBD_JMU","Jackson Mullarkey",  "low",  ["GF", "GM", "GW", "VVP"]),
    ("SLACK_TBD_JC", "Joseph Caliendo",    "low",  ["GF", "GM"]),
    ("SLACK_TBD_RL", "Reese Lundell",      "low",  ["GF", "GM", "GW", "VVP"]),
    ("SLACK_TBD_DF", "David Franks",       "low",  ["GW", "VVP"]),
    ("SLACK_TBD_ES", "Eli Smith",          "low",  ["GF", "GM", "GW", "VVP"]),
    # Jenna Degnan excluded — noted as departing in HR data ("15th last day at EOS")
    ("SLACK_TBD_LH", "Logan Harris",       "low",  ["GF", "GM", "GW", "VVP"]),
    ("SLACK_TBD_JMC","Jayce McGuirk",      "low",  ["GF", "GW", "VVP"]),
    ("SLACK_TBD_JD", "Julia Degnan",       "low",  ["GF", "GM", "GW", "VVP"]),
    ("SLACK_TBD_JG", "Jack Goar",          "low",  ["GF", "GM", "GW", "VVP"]),
    ("SLACK_TBD_AP", "Arseny Putikov",     "low",  ["GF", "GM", "GW", "VVP"]),
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
