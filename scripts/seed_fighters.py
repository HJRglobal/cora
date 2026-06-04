#!/usr/bin/env python3
"""One-time seed: register all F3 sponsored fighters in the influencer tracker.

Source: Google Sheet 1oFmiSVbPMLOMdpjsUBOG_SGp00a9xzTrUCVNuyb0_kA
        (extracted 2026-06-03, deduplicated across Jan-Jun 2026 tabs)

Skipped:
  - Fighters with handle = "NO" (no IG account on file)
  - Jovan Ravago (TikTok only, no IG)
  - Louie Lopez / Taquel Young (column had dates, not handles)
  - Malik Besseck (handle = "NO")
  - Gym accounts (Betweenrounds, MMAelite, MMALAB) -- not individual fighters

Run:
    python scripts/seed_fighters.py [--dry-run]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from dotenv import load_dotenv
load_dotenv(dotenv_path=_REPO_ROOT / ".env", override=True)

# ── Deduplicated fighter roster (name, instagram_handle) ────────────────────
# Where a fighter had different handles across months, used most recent.
# Handles stored without @ prefix.
FIGHTERS = [
    ("Clay Carpenter",       "concreteclayton"),
    ("Mario Bautista",       "mario_bautistamma"),
    ("Kyler Phillips",       "kymatrix"),
    ("Marcus McGhee",        "maniac_mcgheemma"),
    ("Bryce Meredith",       "bmeredith001"),
    ("Jose Delgado",         "jdelgado_145"),
    ("Marquel Mederos",      "marquelmederos"),
    ("Tresean Gore",         "treseangore"),
    ("Eric McConico",        "ericmcconicojr"),
    ("Leslie Hernandez",     "dorathedesstoryerrr"),
    ("Rosalani Ikei",        "rosalaniikeimma"),
    ("Jenna Williams",       "jennawilliam_cpt"),
    ("Kevin Rosas",          "kevinrosas222"),
    ("An Ho",                "antuanhomma"),
    ("Livio Riberio",        "livioriberiomma"),
    ("Chance Ikei",          "ik3i_chance"),
    ("Jared Braun",          "jaredbraun25"),
    ("Christian Natividad",  "christiannatividad"),
    ("Jack Eglin",           "jackeglinmma"),
    ("Amari Sengsavanh",     "theinsurancesalesman"),
    ("Ezra Elliot",          "Ezra_elliott"),
    ("Miguel Francisco",     "deku.mma"),
    ("Moses Diaz",           "mosesdiaz"),
    ("Greg Foster",          "gregmma10"),
    ("Gavin Leath",          "gavin_leath"),
    ("Paul Marghitas",       "paulmarghitas"),
    ("Sheymon Moraes",       "sheymonmoraes"),
    ("Alex Caceres",         "i_am_here_now_alex"),
    ("Jacobi Jones",         "Bigtoemma"),
    ("Cedric Katambwa",      "cedrickatambwa"),
    ("Alexis Martinez",      "amar155_"),
    ("Loai Abushaar",        "loaiabull"),
    ("Shane Christie",       "shanechristie_"),
    ("Bruce Xavier",         "bxmma"),
    ("Alik Lorenz",          "aliklorenz"),
    ("Marcus Nash",          "daamnkobe"),
    ("Olivia Hendrickson",   "oli_marie_"),
    ("Anthony Chung",        "mochungmoproblems"),
    ("Josh Cruz",            "josh_cruz13"),
    ("Riley Helt",           "rileyhelt"),
    ("Sear Sanjar",          "sanjar.sear"),
    ("Zeke Breuninger",      "el_zb"),
    ("Besnik Ghashi",        "besninxha"),
    ("Mark Lozano",          "theemarklozano"),
    ("Eric Fimbres",         "eric_fimbres"),
    ("Abdul Kamara",         "reborn2fight33"),
    ("Sebastian Mordecai",   "mordecai_mma"),
    ("Anthony Cruz",         "Koba_kruz"),
    ("Jackson McVey",        "jacksonmcveymma"),
    ("Oscar Garcia",         "ogarcia.125"),
    ("Zane Darlington",      "sane_d35"),
    ("Jared Cannonier",      "thakillagorllia"),
    ("Roderick Ageyman",     "Karateroddy"),
    ("Ashtynn Edgin",        "ashtynedgin_"),
    ("Dwight Grant",         "dwightgrantmma"),
    ("David Almayo",         "David_almayo_mma"),
    ("Adam Stewart",         "Adamstewartboxer"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would happen without writing to DB")
    args = parser.parse_args()

    from cora.tools.influencer_client import (
        InfluencerClientError,
        _get_conn,
        register_handle,
    )

    if args.dry_run:
        print(f"[DRY RUN] Would register {len(FIGHTERS)} fighters:")
        for name, handle in FIGHTERS:
            print(f"  {name:30s} @{handle}")
        return 0

    registered = 0
    skipped = 0
    errors = 0

    for name, handle in FIGHTERS:
        try:
            row = register_handle(
                athlete_name=name,
                platform="instagram",
                handle=handle,
                entity="F3E",
            )
            print(f"  OK  {name:30s} @{handle} (id={row.get('id', '?')})")
            registered += 1
        except InfluencerClientError as exc:
            if "already registered" in str(exc).lower() or "UNIQUE" in str(exc):
                print(f"  --  {name:30s} @{handle}  (already exists)")
                skipped += 1
            else:
                print(f"  ERR {name:30s} @{handle}  ERROR: {exc}")
                errors += 1

    print()
    print(f"Done: {registered} registered, {skipped} already existed, {errors} errors")
    print(f"Total fighters in tracker: {registered + skipped}")
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
