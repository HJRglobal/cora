# ship-org-roles-roster-2026-06-10.ps1
# Roster update for Org Synthesis Phase 1 (post-Harrison review):
#   - Jerry Reick: title confirmed -> Staff Accountant (ARs, entity expense
#     allocation), reports to Justin
#   - Tessa Miller: ADDED as registry-only entry (no Slack access) -- loader
#     extended to carry slack-less people in all_roles()/roles_for_entity()
#   - +4 tests (32 total in tests/test_org_roles.py)
#
# Run from elevated PowerShell in C:\Users\Harri\code\cora
# NO restart needed tonight:
#   - org-roles.yaml reloads live via the 60s TTL (Jerry's update is already
#     being served by the running bot)
#   - the loader code change only adds registry-only roster support, which
#     nothing live consumes yet; it activates at the next routine restart
#     (Phase 2 ship at the latest)

$ErrorActionPreference = "Stop"
Set-Location "C:\Users\Harri\code\cora"

Write-Host "=== Step 1: import smoke test ==="
& .venv\Scripts\python.exe -c "from src.cora.app import app"
if ($LASTEXITCODE -ne 0) { Write-Error "Import smoke FAILED - aborting."; exit 1 }

Write-Host "=== Step 2: full pytest suite ==="
& .venv\Scripts\python.exe -m pytest -q
if ($LASTEXITCODE -ne 0) { Write-Error "Test suite FAILED - aborting. Nothing committed."; exit 1 }

Write-Host "=== Step 3: commit + push ==="
git add data/maps/org-roles.yaml src/cora/org_roles.py tests/test_org_roles.py deployment/ship-org-roles-roster-2026-06-10.ps1
git commit -m "feat(org): roster review updates - Jerry title confirmed, Tessa added as registry-only

Per Harrison's roster review 2026-06-10:
- Jerry Reick: Staff Accountant (runs ARs across entities, allocates entity
  expenses to categories), reports to Justin Moran
- Tessa Miller: added as the first registry-only entry (part-time remote EA,
  ~10 hrs/wk; no Slack/Asana access). org_roles loader extended: entries
  without slack_id ride all_roles()/roles_for_entity() for roster-level
  features (Phases 2-4) but can never trigger role-block injection.

+4 tests (32 total). No restart required: the YAML change is TTL-live; the
loader change has no live consumer until Phase 2."
if ($LASTEXITCODE -ne 0) { Write-Error "Commit FAILED."; exit 1 }
git push origin main
if ($LASTEXITCODE -ne 0) { Write-Error "Push FAILED - commit is local only."; exit 1 }

Write-Host "=== Done. No restart needed. ==="
