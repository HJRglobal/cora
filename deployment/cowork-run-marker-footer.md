# Cowork run-marker footer contract (v1) -- Code #13 slice 9c, cq-06045f418bd2

STATUS: STAGED. Nothing here is live until Harrison runs the pin script with
`-InjectRunMarkerFooter -Apply` (section 4) and restarts the Claude app so the
scheduler re-reads the task files. Coverage before -Apply: 0 of 109 tasks write
this contract (4 self-log to ad-hoc jsonl at three different Drive paths).
Coverage target after -Apply: 109 of 109.

## 1. Why

"A task that fires and writes nothing is indistinguishable from one that never
fired." The Cowork scheduled-task estate (109 task folders under
`%USERPROFILE%\OneDrive\Documents\Claude\Scheduled`, one SKILL.md each) leaves no
run record anywhere Cora can read: the cron lives in the app DB, the app's own logs
are off disk, and only four tasks self-log -- each to a different file. Cora's
in-repo scheduled tasks already carry a run-marker contract (src/cora/run_marker.py,
ONE append-only ledger). A Cowork task cannot import cora and usually has no G:
drive in bash, so it gets a FILE-DROP contract instead: one small JSON per task per
day, written with the file tools.

The mirror (scripts/mirror_claude_workspace.py, 03:45 + 12:15 AZ) reads those
files into PARITY-REPORT.md ("## Run markers") and mirror-status.json
("run_markers"); the 08:45 nightly health check and the Monday digest WARN on the
alarms. The reader keys on the task's FOLDER id and diffs against the DECLARED
cadence in data/maps/cowork-run-cadence.yaml (the SKILL.md carries no cron).

## 2. The contract

Path -- the FOLDER id is the key (the folder name under `...\Claude\Scheduled\`,
not the frontmatter `name:`; today they are identical for all 109 tasks):

    G:\My Drive\HJR-Founder-OS\_shared\claude-workspace-mirror\_runs\<folder-id>\<YYYY-MM-DD>.json

Body -- exactly these four fields:

    {"ok": true, "outputs": ["<path or permalink>", "..."], "duration_s": 42, "notes": "one line"}

| field      | type    | meaning |
|------------|---------|---------|
| ok         | boolean | false when any required step failed |
| outputs    | LIST    | every file path / Slack permalink / Notion or Asana URL this run actually wrote or sent. Never a count. `[]` is truthful and alarmed: a run that writes nothing is a run that did not happen |
| duration_s | number  | seconds from the first step to the marker write (estimate if needed) |
| notes      | string  | one short line; never client names, health information or anything protected -- the file lands in a KB-adjacent Drive zone |

Reader semantics (window = 2x the declared cadence):

| observation                                                          | status                   | alarmed |
|----------------------------------------------------------------------|--------------------------|---------|
| marker inside the window, outputs non-empty (or expects_output false) | ok                       | no      |
| no marker inside the window, or none ever                            | did not run              | yes     |
| marker inside the window with outputs == [] on expects_output: true  | fired but wrote nothing  | yes     |
| file present but not a JSON object carrying ok + a LIST outputs      | unreadable marker        | yes     |
| marker inside the window with ok: false                              | reported error           | yes     |
| task with no row in cowork-run-cadence.yaml                          | unknown cadence          | no (not computable; never reads ok either) |

Invariants: the mirror never writes, removes or stubs anything under `_runs/`
(its removal detection covers only its own manifest dests). `.json` is never
KB-ingested; a stray `.md` under `_runs/` is excluded by
`incremental_sync_static.is_static_excluded`.

## 3. The footer (exact text the injector appends to every SKILL.md body)

The injector keys idempotence on the FIRST line, the marker comment
`<!-- run-marker-footer v1 -->`. `<FOLDER-ID>` is replaced with the task's folder
id at injection time, so the task never has to derive it.

```text
<!-- run-marker-footer v1 -->

## RUN MARKER -- mandatory last step, every run, even a run that did nothing

Before you finish, write ONE JSON file with the FILE TOOLS (not bash -- the G: drive is usually not mounted there):

    G:\My Drive\HJR-Founder-OS\_shared\claude-workspace-mirror\_runs\<FOLDER-ID>\<YYYY-MM-DD>.json

The path segment <FOLDER-ID> is this task's folder name under ...\Claude\Scheduled\ (the FOLDER id is the key the reader uses -- not the name: line of this file, even though they match today). <YYYY-MM-DD> is today's date in America/Phoenix. Create the folders if missing; overwrite the file if it exists (one marker per task per day, last run wins).

Write exactly these four fields:

    {"ok": true, "outputs": ["<file path or message permalink>"], "duration_s": 0, "notes": "one line"}

- ok: false if any required step failed.
- outputs: a LIST of every thing this run actually wrote or sent -- file paths, Slack message permalinks, Notion or Asana URLs. Never a count, never a placeholder. An empty list [] is the truthful answer when nothing was written: a run that writes nothing is a run that did not happen, and the reader alarms on it.
- duration_s: seconds from your first step to this one (estimate if needed).
- notes: one short line. No client names, no health information, nothing protected.

Never skip this step -- write it even when every other step failed (ok: false). Never list an output you did not produce.
```

## 4. Pin-script change: -InjectRunMarkerFooter

Paste block against `C:\Users\Harri\code\pin-scheduled-task-models.ps1` (the
script lives OUTSIDE the repo; this document is the change of record). Apply the
FIND / REPLACE pairs below in order; each FIND occurs exactly once in the live
script (tests/test_cowork_run_markers.py pins the anchors and runs the assembled
script against fixture SKILL.md files). Body-only edit: the frontmatter block is
never touched; the `$Root` default line is untouched (tests/scripts/
test_mirror_claude_workspace.py pins it against the mirror yaml). Dry-run stays the
default; `-Apply` writes; idempotent on the marker comment. ASCII-only (D-016).

Then:

    .\pin-scheduled-task-models.ps1 -InjectRunMarkerFooter              # dry run: table shows FOOTER per task
    .\pin-scheduled-task-models.ps1 -InjectRunMarkerFooter -Apply       # writes, .bak beside each file
    # restart the Claude app so the scheduler re-reads the task files
    .\pin-scheduled-task-models.ps1 -InjectRunMarkerFooter              # re-run: every task OK, 0 footers remaining

### EDIT 1 -- parameter doc

FIND:
```powershell
.PARAMETER NoBackup
  Skip the timestamped .bak copy that -Apply writes beside each changed file.
```
REPLACE:
```powershell
.PARAMETER NoBackup
  Skip the timestamped .bak copy that -Apply writes beside each changed file.

.PARAMETER InjectRunMarkerFooter
  Also append the run-marker footer (cora repo: deployment/cowork-run-marker-footer.md,
  contract v1) to the BODY of every SKILL.md that does not already carry the marker
  comment "<!-- run-marker-footer v1 -->". The frontmatter is never touched; the
  footer is appended after the existing body; idempotent (a second run reports OK).
  Dry run unless -Apply. The reader (scripts/mirror_claude_workspace.py) keys on the
  task FOLDER id, which is substituted into the footer at injection time.
```

### EDIT 2 -- param block (the $Root line is NOT touched)

FIND:
```powershell
    [switch]$Apply,
    [switch]$NoBackup
)
```
REPLACE:
```powershell
    [switch]$Apply,
    [switch]$NoBackup,
    [switch]$InjectRunMarkerFooter
)
```

### EDIT 3 -- the footer template (single-quoted here-string; the closing '@ must stay at column 0)

FIND:
```powershell
$results   = @()
```
REPLACE:
```powershell
$results   = @()

# Run-marker footer (cora repo: deployment/cowork-run-marker-footer.md, contract v1).
# Idempotence keys on $footerMarker; <FOLDER-ID> is substituted per task.
$footerMarker   = "<!-- run-marker-footer v1 -->"
$footerTemplate = @'
<!-- run-marker-footer v1 -->

## RUN MARKER -- mandatory last step, every run, even a run that did nothing

Before you finish, write ONE JSON file with the FILE TOOLS (not bash -- the G: drive is usually not mounted there):

    G:\My Drive\HJR-Founder-OS\_shared\claude-workspace-mirror\_runs\<FOLDER-ID>\<YYYY-MM-DD>.json

The path segment <FOLDER-ID> is this task's folder name under ...\Claude\Scheduled\ (the FOLDER id is the key the reader uses -- not the name: line of this file, even though they match today). <YYYY-MM-DD> is today's date in America/Phoenix. Create the folders if missing; overwrite the file if it exists (one marker per task per day, last run wins).

Write exactly these four fields:

    {"ok": true, "outputs": ["<file path or message permalink>"], "duration_s": 0, "notes": "one line"}

- ok: false if any required step failed.
- outputs: a LIST of every thing this run actually wrote or sent -- file paths, Slack message permalinks, Notion or Asana URLs. Never a count, never a placeholder. An empty list [] is the truthful answer when nothing was written: a run that writes nothing is a run that did not happen, and the reader alarms on it.
- duration_s: seconds from your first step to this one (estimate if needed).
- notes: one short line. No client names, no health information, nothing protected.

Never skip this step -- write it even when every other step failed (ok: false). Never list an output you did not produce.
'@
```

### EDIT 4 -- decide per file whether the footer is missing (body only)

FIND:
```powershell
    $fmInner = $fm.Groups['inner'].Value
    $body    = $original.Substring($fm.Length)
```
REPLACE:
```powershell
    $fmInner = $fm.Groups['inner'].Value
    $body    = $original.Substring($fm.Length)

    # Run-marker footer: needed only when asked for AND the body lacks the marker.
    $needsFooter = $false
    if ($InjectRunMarkerFooter) { $needsFooter = -not $body.Contains($footerMarker) }
```

### EDIT 5 -- fold the footer into the early OK / continue decision

FIND:
```powershell
    if (-not $needsModel -and -not $hasDup -and -not $needsRepin) {
```
REPLACE:
```powershell
    if (-not $needsModel -and -not $hasDup -and -not $needsRepin -and -not $needsFooter) {
```

### EDIT 6 -- append the footer to $bodyFinal BEFORE the rebuild line

FIND:
```powershell
    $bodyFinal = $bodyFinal.TrimStart("`r", "`n")

    $newText = "---" + $nl + $fmFinal + $nl + "---" + $nl + $nl + $bodyFinal
```
REPLACE:
```powershell
    $bodyFinal = $bodyFinal.TrimStart("`r", "`n")

    if ($needsFooter) {
        $footerText = ($footerTemplate -replace "`r`n", "`n") -replace "`n", $nl
        $footerText = $footerText.Replace("<FOLDER-ID>", $taskId)
        $bodyFinal  = $bodyFinal.TrimEnd("`r", "`n") + $nl + $nl + $footerText + $nl
    }

    $newText = "---" + $nl + $fmFinal + $nl + "---" + $nl + $nl + $bodyFinal
```

### EDIT 7 -- name the action + keep the Model column truthful for a footer-only change

FIND:
```powershell
    $detail = $modelNote
    if ($needsRepin) { $detail = "override map: $existingModel -> $modelToUse" }
    elseif (-not $needsModel) { $detail = "pin already present; removed dead block" }
```
REPLACE:
```powershell
    $detail = $modelNote
    if ($needsRepin) { $detail = "override map: $existingModel -> $modelToUse" }
    elseif (-not $needsModel) { $detail = "pin already present; removed dead block" }

    $modelShown = $modelToUse
    if ($needsFooter) {
        if (-not $needsModel -and -not $needsRepin -and -not $hasDup) {
            $action     = "FOOTER"
            $detail     = "run-marker footer appended (body only)"
            $modelShown = $existingModel
        } else {
            $action = "$action + FOOTER"
            $detail = "$detail; run-marker footer appended"
        }
    }
```

### EDIT 8 -- the results row uses the truthful model column

FIND:
```powershell
        Task = $taskId; Action = $action; Model = $modelToUse; Detail = $detail
```
REPLACE:
```powershell
        Task = $taskId; Action = $action; Model = $modelShown; Detail = $detail
```

### EDIT 9 -- totals

FIND:
```powershell
$changeCount  = ($results | Where-Object { $_.Action -like "*PIN*" -or $_.Action -like "*DEBRIS*" }).Count
$debrisCount  = ($results | Where-Object { $_.Action -like "*DEBRIS*" }).Count
```
REPLACE:
```powershell
$changeCount  = ($results | Where-Object { $_.Action -like "*PIN*" -or $_.Action -like "*DEBRIS*" -or $_.Action -like "*FOOTER*" }).Count
$debrisCount  = ($results | Where-Object { $_.Action -like "*DEBRIS*" }).Count
$footerCount  = ($results | Where-Object { $_.Action -like "*FOOTER*" }).Count
```

### EDIT 10 -- totals line

FIND:
```powershell
Write-Host "  needing a change    : $changeCount  (of which duplicate-block debris: $debrisCount)"
```
REPLACE:
```powershell
Write-Host "  needing a change    : $changeCount  (of which duplicate-block debris: $debrisCount)"
if ($InjectRunMarkerFooter) {
    Write-Host "  run-marker footers  : $footerCount of $($skillFiles.Count) task file(s) need the footer (0 = full coverage)"
}
```

### EDIT 11 -- post-apply message

FIND:
```powershell
    Write-Host "Done. Restart the Claude app so the scheduler re-reads the task files." -ForegroundColor Green
```
REPLACE:
```powershell
    Write-Host "Done. Restart the Claude app so the scheduler re-reads the task files." -ForegroundColor Green
    if ($InjectRunMarkerFooter) {
        Write-Host "Run-marker footers appended: $footerCount. After the restart, the next mirror run (03:45 / 12:15 AZ) reports coverage in PARITY-REPORT.md; re-run this script with -InjectRunMarkerFooter (dry) to confirm 0 remaining." -ForegroundColor Green
    }
```

### EDIT 12 -- dry-run hint

FIND:
```powershell
    Write-Host "  .\pin-scheduled-task-models.ps1 -Apply" -ForegroundColor Cyan
```
REPLACE:
```powershell
    Write-Host "  .\pin-scheduled-task-models.ps1 -Apply" -ForegroundColor Cyan
    if ($InjectRunMarkerFooter) {
        Write-Host "  .\pin-scheduled-task-models.ps1 -InjectRunMarkerFooter -Apply" -ForegroundColor Cyan
    }
```

## 5. After -Apply

1. Restart the Claude app so the scheduler re-reads the task files.
2. The next mirror run writes "## Run markers" with coverage `N of 109`. Every task
   with a row in data/maps/cowork-run-cadence.yaml reads `did not run` until its
   first post-footer fire lands a marker; that is the honest state, not a defect.
3. Add cadence rows (Harrison-maintained) as tasks are confirmed writing markers;
   a task without a row stays `unknown cadence` -- visible, never alarmed, never ok.
