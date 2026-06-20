#!/usr/bin/env python3
"""Attach the Entity / Status / Priority custom fields to the meeting-capture
catch-all projects (Phase 1.10 enablement).

Why: captured-task custom-field tagging was inert because the FNDR-portfolio
Entity/Status/Priority fields were not attached to the capture-target projects
(design/asana-architecture.md Sec 5). This attaches the EXISTING fields (by their
exact GIDs from data/maps/meeting-capture-projects.yaml) so set_task_custom_fields
can stamp them at creation. Passing exact GIDs means it can never create a
duplicate field (the risk a UI/Chrome-Agent attach carries).

Idempotent: reads each project's current fields first and attaches only the
missing ones. Drives off meeting-capture-projects.yaml `projects:` (dedup by GID;
HJRG/FNDR and LEX/LEX-LLC share a catch-all) and `custom_fields:` field GIDs.

Usage:
  .venv\\Scripts\\python.exe scripts\\attach_capture_custom_fields.py --dry-run
  .venv\\Scripts\\python.exe scripts\\attach_capture_custom_fields.py
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

# UTF-8 stdout so any non-ASCII (project names) can't crash a cp1252 console.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

load_dotenv()
_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

from cora.tools import asana_client  # noqa: E402

_CFG_PATH = _REPO / "data" / "maps" / "meeting-capture-projects.yaml"

# custom_fields key -> human label, in attach order.
_FIELD_KEYS = (("entity_field_gid", "Entity"),
               ("status_field_gid", "Status"),
               ("priority_field_gid", "Priority"))


def load_cfg() -> dict:
    return yaml.safe_load(_CFG_PATH.read_text(encoding="utf-8")) or {}


def field_gids(cfg: dict) -> dict[str, str]:
    """Return {label: field_gid} for the configured Entity/Status/Priority fields."""
    cf = cfg.get("custom_fields") or {}
    out: dict[str, str] = {}
    for key, label in _FIELD_KEYS:
        gid = str(cf.get(key) or "").strip()
        if gid:
            out[label] = gid
    return out


def project_gids(cfg: dict) -> dict[str, str]:
    """Return {project_gid: label} for every project that should carry the capture
    custom fields, deduped (first label wins, empties skipped).

    Sources (WS10 — closes the coverage gap beyond the per-entity catch-alls):
      - `projects:`                 per-entity "Operations — General" catch-alls
      - `new_projects_2026_06_08:`  the function-specific Harrison-owned projects
                                    (sales/leasing/tech/etc.) that were created
                                    without the fields attached
      - `field_target_projects:`    ad-hoc extra GIDs (e.g. TikTok Shop, Wikipedia
                                    Presence, press-pipeline) — Harrison-editable

    BDM client projects are NEVER listed in this config, so they are excluded by
    construction (the BDM catch-all is also intentionally blank).
    """
    seen: dict[str, str] = {}

    def _add(gid, label):
        gid = str(gid or "").strip()
        if gid and gid not in seen:
            seen[gid] = str(label)

    for ent, gid in (cfg.get("projects") or {}).items():
        _add(gid, ent)
    for ent, items in (cfg.get("new_projects_2026_06_08") or {}).items():
        for it in (items or []):
            if isinstance(it, dict):
                _add(it.get("gid"), ent)
    for it in (cfg.get("field_target_projects") or []):
        if isinstance(it, dict):
            _add(it.get("gid"), it.get("label") or "extra")
        else:
            _add(it, "extra")
    return seen


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Attach capture custom fields to catch-all projects")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; attach nothing")
    args = ap.parse_args(argv)

    cfg = load_cfg()
    fields = field_gids(cfg)
    projects = project_gids(cfg)
    if not fields:
        print("No custom_fields GIDs configured -- nothing to do.")
        return 1
    print(f"Fields to attach: {fields}")
    print(f"{len(projects)} unique catch-all project(s){' (DRY RUN)' if args.dry_run else ''}")

    attached = skipped = errors = 0
    for gid, ent in projects.items():
        try:
            present = asana_client.list_project_custom_field_gids(gid)
        except asana_client.AsanaClientError as exc:
            print(f"  [{ent} {gid}] READ FAILED -> skipping: {exc}")
            errors += 1
            continue
        for label, fgid in fields.items():
            if fgid in present:
                print(f"  [{ent} {gid}] {label} already attached")
                skipped += 1
                continue
            if args.dry_run:
                print(f"  [{ent} {gid}] WOULD attach {label} ({fgid})")
                continue
            try:
                asana_client.add_project_custom_field_setting(gid, fgid)
                print(f"  [{ent} {gid}] attached {label}")
                attached += 1
            except asana_client.AsanaClientError as exc:
                print(f"  [{ent} {gid}] {label} ATTACH FAILED: {exc}")
                errors += 1

    print(f"Done. attached={attached} skipped={skipped} errors={errors}")
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
