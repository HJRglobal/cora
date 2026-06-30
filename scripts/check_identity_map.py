"""Roster-drift guard for the per-person dossier layer (spec section 3).

Asserts that the YAML-derived identities (person_identity.resolve) agree with the
human-readable `_brain/reference/team-identity-map.md` table -- so the consolidation
doc can't silently drift from the maps that actually drive the pulls (same spirit as
the org-roles drift tests). The YAMLs are the source of truth; this catches a GID /
owner-id / Slack-id that was changed in one place but not the other.

Tolerant of the map's hand-annotations ("pending", "dormant UFL ...", "owns 0 deals",
"DEACTIVATED, unmapped", "—", "TBD") -- those cells assert nothing. STRONG keys
(slack_id, asana_gid, hubspot_owner_id) are compared exactly when the cell carries a
clean value; the work-email cell is a containment check against the resolved emails.

Exit 0 = in sync; exit 1 = drift (prints each mismatch). Used by tests/test_person_identity.py.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.cora import person_identity  # noqa: E402

# Cells whose content asserts nothing about identity (annotations / placeholders).
_SKIP_TOKENS = ("—", "-", "n/a", "tbd", "pending", "none", "revoked", "deactivated",
                "unmapped", "(none")

_U_RE = re.compile(r"\bU[A-Z0-9]{6,}\b")
_DIGITS_RE = re.compile(r"\b\d{6,}\b")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")


def _map_path() -> Path:
    return Path(
        os.environ.get("BRAIN_REFERENCE_DIR")
        or r"G:\My Drive\HJR-Founder-OS\_brain\reference"
    ) / "team-identity-map.md"


def _cell_skips(cell: str) -> bool:
    c = (cell or "").strip().lower()
    return (not c) or any(tok in c for tok in _SKIP_TOKENS)


def parse_identity_map(text: str) -> list[dict]:
    """Parse the markdown identity table into rows with the cells we check.

    Columns: Person | Role/Entity | Slack ID | Work email | Email aliases | Asana GID | HubSpot owner ID
    """
    rows: list[dict] = []
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 7:
            continue
        person = cells[0]
        if person.lower() in ("person", "") or set(person) <= {"-", " ", ":"}:
            continue  # header / separator row
        rows.append({
            "person": person,
            "slack": cells[2],
            "email": cells[3],
            "asana": cells[5],
            "hubspot": cells[6],
        })
    return rows


def check(map_text: str) -> tuple[list[str], list[str]]:
    """Compare the map to the YAML-derived identities.

    Returns (errors, warnings). ERRORS are strong-key drift that would break a pull
    (Slack ID / Asana GID / HubSpot owner). WARNINGS are soft (the hand-written work-
    email cell, which legitimately carries variants like a Google identity vs an Asana
    login); they print but never fail the guard.
    """
    errors: list[str] = []
    warnings: list[str] = []
    for row in parse_identity_map(map_text):
        person = row["person"]
        # Resolve by the Slack ID in the map (the primary key), else by name.
        sid_m = _U_RE.search(row["slack"] or "")
        ident = None
        if sid_m:
            ident = person_identity.resolve(sid_m.group(0))
        if ident is None:
            ident = person_identity.resolve_by_name(person)
        if ident is None:
            # Map names someone the YAMLs can't resolve -- drift only if the map row
            # carries a real Slack ID (a rostered person should resolve).
            if sid_m:
                errors.append(f"{person}: map has Slack {sid_m.group(0)} but YAMLs don't resolve them")
            continue

        if sid_m and ident.slack_id and sid_m.group(0) != ident.slack_id:
            errors.append(f"{person}: Slack {sid_m.group(0)} (map) != {ident.slack_id} (YAML)")

        if not _cell_skips(row["asana"]):
            gid_m = _DIGITS_RE.search(row["asana"])
            if gid_m and ident.asana_gid and gid_m.group(0) != ident.asana_gid:
                errors.append(f"{person}: Asana GID {gid_m.group(0)} (map) != {ident.asana_gid} (YAML)")
            elif gid_m and not ident.asana_gid:
                errors.append(f"{person}: map has Asana GID {gid_m.group(0)} but YAML has none")

        if not _cell_skips(row["hubspot"]):
            hs_m = _DIGITS_RE.search(row["hubspot"])
            if hs_m and ident.hubspot_owner_id and hs_m.group(0) != ident.hubspot_owner_id:
                errors.append(f"{person}: HubSpot {hs_m.group(0)} (map) != {ident.hubspot_owner_id} (YAML)")
            elif hs_m and not ident.hubspot_owner_id:
                errors.append(f"{person}: map has HubSpot owner {hs_m.group(0)} but YAML has none")

        # Email: soft check (skip registry-only people with no resolvable emails).
        if ident.all_emails and not _cell_skips(row["email"]):
            em_m = _EMAIL_RE.search(row["email"])
            if em_m and em_m.group(0).lower() not in ident.all_emails:
                warnings.append(
                    f"{person}: map work email {em_m.group(0)} not in resolved emails {ident.all_emails}"
                )
    return errors, warnings


def main() -> int:
    path = _map_path()
    if not path.exists():
        print(f"identity map not found at {path} -- nothing to check")
        return 0
    errors, warnings = check(path.read_text(encoding="utf-8"))
    for w in warnings:
        print("  warning:", w)
    if errors:
        print("IDENTITY MAP DRIFT (strong keys):")
        for d in errors:
            print("  -", d)
        return 1
    print("identity map in sync with the YAMLs (strong keys)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
