#Requires -Version 5.1
<#
    HJR Founder OS - folder audit inventory (READ-ONLY)
    =====================================================

    Project: 00-Founder/projects/audit-clean-and-restructure-hjr-folders
    File:    G:\My Drive\HJR-Founder-OS\00-Founder\projects\audit-clean-and-restructure-hjr-folders\2026-09-21_fndr_folder-audit-inventory.ps1

    Generated 2026-09-21 by a Sonnet drafting subagent from the Fable design spec; reviewed before staging.

    SAFETY GUARANTEE (one sentence): this script only ever READS under -Root and only ever WRITES
    under -OutDir - it contains no Move-Item / Remove-Item / Rename-Item / Copy-Item / New-Item /
    Set-Content / Out-File / Export-Csv call that targets any path under -Root, it refuses to run
    at all (exit 2) if -OutDir resolves to a path inside -Root, and it refuses to run (exit 3) if
    -Root does not exist.

    EXAMPLE INVOCATIONS
    --------------------
    Plain run (defaults: Root = G:\My Drive\HJR-Founder-OS, OutDir = C:\Users\Harri\Downloads\hjr-folder-audit):

        powershell -ExecutionPolicy Bypass -File "G:\My Drive\HJR-Founder-OS\00-Founder\projects\audit-clean-and-restructure-hjr-folders\2026-09-21_fndr_folder-audit-inventory.ps1"

    Also hash Google Drive placeholder ("cloud-only") files (forces downloads - see note below):

        powershell -ExecutionPolicy Bypass -File "G:\My Drive\HJR-Founder-OS\00-Founder\projects\audit-clean-and-restructure-hjr-folders\2026-09-21_fndr_folder-audit-inventory.ps1" -IncludeOffline

    Hash every file regardless of size-collision grouping or size cap, up to 2000 MB per file:

        powershell -ExecutionPolicy Bypass -File "G:\My Drive\HJR-Founder-OS\00-Founder\projects\audit-clean-and-restructure-hjr-folders\2026-09-21_fndr_folder-audit-inventory.ps1" -HashAll -MaxHashMB 2000

    NOTE ON GOOGLE DRIVE PLACEHOLDERS
    ----------------------------------
    This tree is mounted via Google Drive for Desktop in "stream" mode. A file that is not pinned
    locally exists on disk only as a placeholder (Offline / RecallOnDataAccess / RecallOnOpen
    attributes); reading its bytes (which hashing requires) forces Drive to download the real file
    on demand. By default this script SKIPS hashing placeholder files (HashStatus = SKIPPED_OFFLINE)
    and reports how many were skipped, so a normal run never triggers surprise downloads or bandwidth
    use. Pass -IncludeOffline to hash them anyway; expect that to be slow and to pull data over the
    network.

    This script is READ-ONLY against -Root by construction. It is meant to be run by hand by Harrison;
    it does not move, rename, delete, or otherwise modify anything inside the audited tree. Its output
    (three CSV/TXT files) is a later analysis step's input for a duplicate/cleanup manifest - nothing
    here decides or performs any cleanup itself.
#>

[CmdletBinding()]
param(
    [string]$Root = 'G:\My Drive\HJR-Founder-OS',
    [string]$OutDir = 'C:\Users\Harri\Downloads\hjr-folder-audit',
    [switch]$IncludeOffline,
    [int]$MaxHashMB = 200,
    [switch]$HashAll,
    [switch]$Quiet
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Continue'

# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------

function Get-RelPath {
    <# Returns Path relative to RootPath, backslash-separated, no leading backslash.
       Returns '' when FullPath IS RootPath itself. #>
    param(
        [Parameter(Mandatory = $true)][string]$FullPath,
        [Parameter(Mandatory = $true)][string]$RootPath
    )
    $rootTrimmed = $RootPath.TrimEnd('\')
    $rootWithSep = $rootTrimmed + '\'
    $fullTrimmed = $FullPath.TrimEnd('\')

    if ($fullTrimmed.Length -gt $rootWithSep.Length -and
        $FullPath.Substring(0, $rootWithSep.Length).Equals($rootWithSep, [System.StringComparison]::OrdinalIgnoreCase)) {
        return $FullPath.Substring($rootWithSep.Length)
    }
    elseif ($fullTrimmed.Equals($rootTrimmed, [System.StringComparison]::OrdinalIgnoreCase)) {
        return ''
    }
    else {
        # Should not normally happen (everything walked comes from under Root); fall back safely.
        return $FullPath
    }
}

function Test-IsPlaceholder {
    <# True when a Google Drive "cloud-only" placeholder attribute bit is set. #>
    param($Attributes)
    $attrsInt = [int]$Attributes
    $offlineFlag = [int][System.IO.FileAttributes]::Offline
    $recallOnDataAccess = 0x400000
    $recallOnOpen = 0x40000

    if (($attrsInt -band $offlineFlag) -ne 0) { return $true }
    if (($attrsInt -band $recallOnDataAccess) -ne 0) { return $true }
    if (($attrsInt -band $recallOnOpen) -ne 0) { return $true }
    return $false
}

function Get-DriveSuffix {
    <# Classifies a file NAME (not path) as one Drive-sync-collision suffix pattern, or ''.
       Checked in this precedence order: paren-n, copy-of, conflicted, underscore-n. #>
    param([string]$Name)

    if ($Name -match ' \(\d+\)(\.[^.]+)?$') { return 'paren-n' }
    if ($Name -match '^Copy of ') { return 'copy-of' }
    if ($Name -match 'conflicted copy') { return 'conflicted' }
    if ($Name -match '_\d\.[^.]+$') { return 'underscore-n' }
    return ''
}

function Get-BaseName {
    <# Strips the detected DriveSuffix pattern from Name. Empty string in, empty string out. #>
    param(
        [string]$Name,
        [string]$Suffix
    )
    if ($Suffix -eq '') { return '' }

    switch ($Suffix) {
        'paren-n' {
            return [regex]::Replace($Name, ' \(\d+\)(?=(\.[^.]+)?$)', '')
        }
        'copy-of' {
            return [regex]::Replace($Name, '^Copy of ', '')
        }
        'conflicted' {
            return [regex]::Replace($Name, '\s*\([^)]*conflicted copy[^)]*\)', '', [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
        }
        'underscore-n' {
            return [regex]::Replace($Name, '_(\d)(?=\.[^.]+$)', '')
        }
        default {
            return ''
        }
    }
}

function Get-AncestorRelPaths {
    <# Returns RelDir plus every ancestor relative directory path up to and including '' (the root). #>
    param([string]$RelDir)
    $result = [System.Collections.Generic.List[string]]::new()
    $current = $RelDir
    while ($true) {
        $result.Add($current)
        if ($current -eq '') { break }
        $parent = Split-Path -Path $current -Parent
        if ($null -eq $parent) { $parent = '' }
        $current = $parent
    }
    return $result
}

# ---------------------------------------------------------------------------
# Pre-flight safety checks (hard exits; nothing written yet, nothing to flush)
# ---------------------------------------------------------------------------

$rootFull = [System.IO.Path]::GetFullPath($Root)
$outDirFull = [System.IO.Path]::GetFullPath($OutDir)

$rootNorm = $rootFull.TrimEnd('\')
$outDirNorm = $outDirFull.TrimEnd('\')
$rootWithSepForCheck = $rootNorm + '\'

$outDirIsInsideRoot = $outDirNorm.Equals($rootNorm, [System.StringComparison]::OrdinalIgnoreCase) -or
    $outDirNorm.StartsWith($rootWithSepForCheck, [System.StringComparison]::OrdinalIgnoreCase)

if ($outDirIsInsideRoot) {
    Write-Error "REFUSING TO RUN: -OutDir '$outDirNorm' resolves to a path inside -Root '$rootNorm'. Outputs must never be written inside the audited tree. Choose a different -OutDir."
    exit 2
}

if (-not (Test-Path -LiteralPath $rootNorm -PathType Container)) {
    Write-Error "REFUSING TO RUN: -Root '$rootNorm' does not exist or is not a directory."
    exit 3
}

# The ONLY New-Item call in this script, and it targets OutDir, never Root.
if (-not (Test-Path -LiteralPath $outDirNorm -PathType Container)) {
    New-Item -Path $outDirNorm -ItemType Directory | Out-Null
}

# ---------------------------------------------------------------------------
# Script-level state
# ---------------------------------------------------------------------------

$stamp = Get-Date -Format 'yyyyMMdd-HHmm'
$startTime = Get-Date
$script:RunStatus = 'ABORTED'

$fileRows = [System.Collections.Generic.List[object]]::new()
$dirRows = [System.Collections.Generic.List[object]]::new()
$walkErrorLines = [System.Collections.Generic.List[string]]::new()

$totalFiles = 0
$totalDirs = 0
[int64]$totalBytes = 0
$topLevelCounts = @{}
$desktopIniCount = 0
$placeholderCount = 0
$suffixCounts = @{ 'paren-n' = 0; 'copy-of' = 0; 'conflicted' = 0; 'underscore-n' = 0 }
$sizeGroupCount = 0
$sizeGroupFilesCount = 0
$hashedCount = 0
$notNeededCount = 0
$skippedOfflineCount = 0
$skippedSizeCount = 0
$skippedZeroCount = 0
$hashErrorCount = 0
$dupHashGroupCount = 0
$dupHashFilesCount = 0
$emptyDirCount = 0
$dirSuffixCount = 0

$filesCsvPath = Join-Path -Path $outDirNorm -ChildPath ("inventory-files-$stamp.csv")
$dirsCsvPath = Join-Path -Path $outDirNorm -ChildPath ("inventory-dirs-$stamp.csv")
$summaryPath = Join-Path -Path $outDirNorm -ChildPath ("inventory-summary-$stamp.txt")
$runLogPath = Join-Path -Path $outDirNorm -ChildPath ("_run-log-$stamp.txt")

# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

try {
    # --- WALK: files ---
    $fileWalkErrs = $null
    $files = Get-ChildItem -LiteralPath $rootNorm -Recurse -Force -File -ErrorAction SilentlyContinue -ErrorVariable fileWalkErrs
    if ($null -eq $files) { $files = @() }
    foreach ($e in $fileWalkErrs) {
        $target = ''
        if ($e.CategoryInfo -and $e.CategoryInfo.TargetName) { $target = $e.CategoryInfo.TargetName }
        $walkErrorLines.Add("WALK-FILE-ERROR: $target :: $($e.Exception.Message)")
    }

    # --- WALK: directories ---
    $dirWalkErrs = $null
    $dirs = Get-ChildItem -LiteralPath $rootNorm -Recurse -Force -Directory -ErrorAction SilentlyContinue -ErrorVariable dirWalkErrs
    if ($null -eq $dirs) { $dirs = @() }
    foreach ($e in $dirWalkErrs) {
        $target = ''
        if ($e.CategoryInfo -and $e.CategoryInfo.TargetName) { $target = $e.CategoryInfo.TargetName }
        $walkErrorLines.Add("WALK-DIR-ERROR: $target :: $($e.Exception.Message)")
    }

    $totalFiles = @($files).Count
    $totalDirs = @($dirs).Count

    # --- Size-group lookup: Bytes -> count of files sharing that exact size ---
    $sizeLookup = @{}
    $i = 0
    foreach ($f in $files) {
        $i++
        if (-not $Quiet -and ($i % 500 -eq 0)) {
            Write-Progress -Activity 'Pass 1/3: sizing files' -Status "$i of $totalFiles" -PercentComplete ([int](($i / [math]::Max($totalFiles, 1)) * 100))
        }
        $b = $f.Length
        if ($sizeLookup.ContainsKey($b)) {
            $sizeLookup[$b] = $sizeLookup[$b] + 1
        }
        else {
            $sizeLookup[$b] = 1
        }
    }
    if (-not $Quiet) { Write-Progress -Activity 'Pass 1/3: sizing files' -Completed }

    foreach ($k in $sizeLookup.Keys) {
        if ($sizeLookup[$k] -ge 2) {
            $sizeGroupCount++
            $sizeGroupFilesCount += $sizeLookup[$k]
        }
    }

    # --- Per-file rows (no hashing yet) ---
    $i = 0
    foreach ($f in $files) {
        $i++
        if (-not $Quiet -and ($i % 500 -eq 0)) {
            Write-Progress -Activity 'Pass 2/3: building file inventory' -Status "$i of $totalFiles" -PercentComplete ([int](($i / [math]::Max($totalFiles, 1)) * 100))
        }

        $relPath = Get-RelPath -FullPath $f.FullName -RootPath $rootNorm
        $relDir = Split-Path -Path $relPath -Parent
        if ($null -eq $relDir) { $relDir = '' }

        $ext = ''
        if ($f.Extension) { $ext = $f.Extension.TrimStart('.').ToLowerInvariant() }

        $segments = @()
        if ($relPath -ne '') { $segments = $relPath -split '\\' }

        $topLevel = '<root>'
        $depth = 0
        if ($segments.Count -gt 1) {
            $topLevel = $segments[0]
            $depth = $segments.Count - 1
        }

        $isPlaceholder = Test-IsPlaceholder -Attributes $f.Attributes
        $isDesktopIni = [bool]($f.Name -ieq 'desktop.ini')

        $suffix = Get-DriveSuffix -Name $f.Name
        $baseName = ''
        if ($suffix -ne '') { $baseName = Get-BaseName -Name $f.Name -Suffix $suffix }

        $bytes = [int64]$f.Length
        $sizeGroup = $sizeLookup[$bytes]

        $row = [PSCustomObject]@{
            RelPath       = $relPath
            Dir           = $relDir
            Name          = $f.Name
            Ext           = $ext
            Bytes         = $bytes
            CreatedUtc    = $f.CreationTimeUtc.ToString('yyyy-MM-ddTHH:mm:ssZ')
            ModifiedUtc   = $f.LastWriteTimeUtc.ToString('yyyy-MM-ddTHH:mm:ssZ')
            TopLevel      = $topLevel
            Depth         = $depth
            Attributes    = $f.Attributes.ToString()
            IsPlaceholder = $isPlaceholder
            IsDesktopIni  = $isDesktopIni
            DriveSuffix   = $suffix
            BaseName      = $baseName
            SizeGroup     = $sizeGroup
            Sha256        = ''
            HashStatus    = ''
            _FullPath     = $f.FullName
        }

        $totalBytes += $bytes
        if ($topLevelCounts.ContainsKey($topLevel)) { $topLevelCounts[$topLevel] = $topLevelCounts[$topLevel] + 1 } else { $topLevelCounts[$topLevel] = 1 }
        if ($isDesktopIni) { $desktopIniCount++ }
        if ($isPlaceholder) { $placeholderCount++ }
        if ($suffix -ne '' -and $suffixCounts.ContainsKey($suffix)) { $suffixCounts[$suffix] = $suffixCounts[$suffix] + 1 }

        $fileRows.Add($row)
    }
    if (-not $Quiet) { Write-Progress -Activity 'Pass 2/3: building file inventory' -Completed }

    # --- Hashing pass ---
    [int64]$maxBytes = [int64]$MaxHashMB * 1MB
    $i = 0
    foreach ($row in $fileRows) {
        $i++
        if (-not $Quiet -and ($i % 500 -eq 0)) {
            Write-Progress -Activity 'Pass 3/3: hashing files' -Status "$i of $totalFiles" -PercentComplete ([int](($i / [math]::Max($totalFiles, 1)) * 100))
        }

        $bytes = $row.Bytes

        if ($bytes -eq 0) {
            $row.HashStatus = 'SKIPPED_ZERO'
            $skippedZeroCount++
            continue
        }

        if ($row.IsPlaceholder -and (-not $IncludeOffline)) {
            $row.HashStatus = 'SKIPPED_OFFLINE'
            $skippedOfflineCount++
            continue
        }

        $sizeGroupQualifies = ($row.SizeGroup -ge 2) -or $HashAll
        if (-not $sizeGroupQualifies) {
            $row.HashStatus = 'NOT_NEEDED'
            $notNeededCount++
            continue
        }

        $sizeCapQualifies = ($bytes -le $maxBytes) -or $HashAll
        if (-not $sizeCapQualifies) {
            $row.HashStatus = 'SKIPPED_SIZE'
            $skippedSizeCount++
            continue
        }

        try {
            $h = Get-FileHash -LiteralPath $row._FullPath -Algorithm SHA256 -ErrorAction Stop
            $row.Sha256 = $h.Hash
            $row.HashStatus = 'HASHED'
            $hashedCount++
        }
        catch {
            $row.HashStatus = 'ERROR'
            $hashErrorCount++
            $walkErrorLines.Add("HASH-ERROR: $($row._FullPath) :: $($_.Exception.Message)")
        }
    }
    if (-not $Quiet) { Write-Progress -Activity 'Pass 3/3: hashing files' -Completed }

    # Confirmed byte-identical groups (same SHA-256, occurring 2+ times)
    $hashLookup = @{}
    foreach ($row in $fileRows) {
        if ($row.HashStatus -eq 'HASHED' -and $row.Sha256 -ne '') {
            if ($hashLookup.ContainsKey($row.Sha256)) {
                $hashLookup[$row.Sha256] = $hashLookup[$row.Sha256] + 1
            }
            else {
                $hashLookup[$row.Sha256] = 1
            }
        }
    }
    foreach ($k in $hashLookup.Keys) {
        if ($hashLookup[$k] -ge 2) {
            $dupHashGroupCount++
            $dupHashFilesCount += $hashLookup[$k]
        }
    }

    # --- Directory stats ---
    # Direct file counts per directory (keyed by relative Dir path, '' = root)
    $dirDirectFileCount = @{}
    $dirDirectFileCountExclIni = @{}
    foreach ($row in $fileRows) {
        $d = $row.Dir
        if ($dirDirectFileCount.ContainsKey($d)) { $dirDirectFileCount[$d] = $dirDirectFileCount[$d] + 1 } else { $dirDirectFileCount[$d] = 1 }
        if (-not $row.IsDesktopIni) {
            if ($dirDirectFileCountExclIni.ContainsKey($d)) { $dirDirectFileCountExclIni[$d] = $dirDirectFileCountExclIni[$d] + 1 } else { $dirDirectFileCountExclIni[$d] = 1 }
        }
    }

    $dirRelPaths = [System.Collections.Generic.List[string]]::new()
    foreach ($d in $dirs) {
        $dirRelPaths.Add((Get-RelPath -FullPath $d.FullName -RootPath $rootNorm))
    }

    # Direct subdirectory counts per parent path ('' = root)
    $directSubdirCount = @{}
    $directSubdirCount[''] = 0
    foreach ($rp in $dirRelPaths) { $directSubdirCount[$rp] = 0 }
    foreach ($rp in $dirRelPaths) {
        $parent = Split-Path -Path $rp -Parent
        if ($null -eq $parent) { $parent = '' }
        if (-not $directSubdirCount.ContainsKey($parent)) { $directSubdirCount[$parent] = 0 }
        $directSubdirCount[$parent] = $directSubdirCount[$parent] + 1
    }

    # Recursive file counts per directory, built by propagating each file's count up its ancestor chain
    $recursiveFileCount = @{}
    $recursiveFileCountExclIni = @{}
    $recursiveFileCount[''] = 0
    $recursiveFileCountExclIni[''] = 0
    foreach ($rp in $dirRelPaths) {
        $recursiveFileCount[$rp] = 0
        $recursiveFileCountExclIni[$rp] = 0
    }

    $j = 0
    foreach ($row in $fileRows) {
        $j++
        if (-not $Quiet -and ($j % 2000 -eq 0)) {
            Write-Progress -Activity 'Pass 4/4: rolling up directory stats' -Status "$j of $totalFiles"
        }
        $ancestors = Get-AncestorRelPaths -RelDir $row.Dir
        foreach ($a in $ancestors) {
            if (-not $recursiveFileCount.ContainsKey($a)) {
                $recursiveFileCount[$a] = 0
                $recursiveFileCountExclIni[$a] = 0
            }
            $recursiveFileCount[$a] = $recursiveFileCount[$a] + 1
            if (-not $row.IsDesktopIni) {
                $recursiveFileCountExclIni[$a] = $recursiveFileCountExclIni[$a] + 1
            }
        }
    }
    if (-not $Quiet) { Write-Progress -Activity 'Pass 4/4: rolling up directory stats' -Completed }

    # --- Directory rows ---
    # NOTE: Depth here counts the directory's OWN path segments below Root (a top-level dir = 1),
    # which mirrors the file convention where Depth counts directory segments BEFORE the filename
    # (a root-level file = 0). A directory has no trailing filename component to exclude.
    $k = 0
    foreach ($d in $dirs) {
        $k++
        if (-not $Quiet -and ($k % 500 -eq 0)) {
            Write-Progress -Activity 'Pass 5/5: building directory inventory' -Status "$k of $totalDirs" -PercentComplete ([int](($k / [math]::Max($totalDirs, 1)) * 100))
        }

        $relPath = Get-RelPath -FullPath $d.FullName -RootPath $rootNorm
        $segments = @()
        if ($relPath -ne '') { $segments = $relPath -split '\\' }
        $depth = $segments.Count

        $fileCount = 0
        if ($dirDirectFileCount.ContainsKey($relPath)) { $fileCount = $dirDirectFileCount[$relPath] }
        $fileCountExclIni = 0
        if ($dirDirectFileCountExclIni.ContainsKey($relPath)) { $fileCountExclIni = $dirDirectFileCountExclIni[$relPath] }
        $subdirCount = 0
        if ($directSubdirCount.ContainsKey($relPath)) { $subdirCount = $directSubdirCount[$relPath] }
        $recFileCount = 0
        if ($recursiveFileCount.ContainsKey($relPath)) { $recFileCount = $recursiveFileCount[$relPath] }
        $recFileCountExclIni = 0
        if ($recursiveFileCountExclIni.ContainsKey($relPath)) { $recFileCountExclIni = $recursiveFileCountExclIni[$relPath] }

        # Empty (excl desktop.ini) means the recursive file count that ISN'T desktop.ini is zero.
        $isEmptyExclIni = [bool]($recFileCountExclIni -eq 0)

        $dirSuffix = ''
        if ($d.Name -match ' \(\d+\)$') { $dirSuffix = 'paren-n' }
        elseif ($d.Name -match ' 2$') { $dirSuffix = 'space-2' }

        if ($dirSuffix -ne '') { $dirSuffixCount++ }
        if ($isEmptyExclIni) { $emptyDirCount++ }

        $dirRow = [PSCustomObject]@{
            RelPath                 = $relPath
            Depth                   = $depth
            FileCount               = $fileCount
            FileCountExclDesktopIni = $fileCountExclIni
            SubdirCount             = $subdirCount
            RecursiveFileCount      = $recFileCount
            IsEmptyExclDesktopIni   = $isEmptyExclIni
            HasDriveSuffix          = $dirSuffix
        }
        $dirRows.Add($dirRow)
    }
    if (-not $Quiet) { Write-Progress -Activity 'Pass 5/5: building directory inventory' -Completed }

    $script:RunStatus = 'COMPLETE'
}
catch {
    $walkErrorLines.Add("FATAL-ERROR: $($_.Exception.Message)")
    $walkErrorLines.Add("FATAL-ERROR-STACK: $($_.ScriptStackTrace)")
}
finally {
    $endTime = Get-Date
    $elapsed = $endTime - $startTime

    # --- Flush file inventory CSV (only the 17 spec'd columns, in order; drops the internal _FullPath) ---
    try {
        if ($fileRows.Count -gt 0) {
            $fileRows |
                Select-Object RelPath, Dir, Name, Ext, Bytes, CreatedUtc, ModifiedUtc, TopLevel, Depth, Attributes, IsPlaceholder, IsDesktopIni, DriveSuffix, BaseName, SizeGroup, Sha256, HashStatus |
                Export-Csv -Path $filesCsvPath -NoTypeInformation -Encoding UTF8
        }
        else {
            'RelPath,Dir,Name,Ext,Bytes,CreatedUtc,ModifiedUtc,TopLevel,Depth,Attributes,IsPlaceholder,IsDesktopIni,DriveSuffix,BaseName,SizeGroup,Sha256,HashStatus' |
                Set-Content -Path $filesCsvPath -Encoding UTF8
        }
    }
    catch {
        $walkErrorLines.Add("OUTPUT-WRITE-ERROR (files CSV): $($_.Exception.Message)")
    }

    # --- Flush directory inventory CSV ---
    try {
        if ($dirRows.Count -gt 0) {
            $dirRows |
                Select-Object RelPath, Depth, FileCount, FileCountExclDesktopIni, SubdirCount, RecursiveFileCount, IsEmptyExclDesktopIni, HasDriveSuffix |
                Export-Csv -Path $dirsCsvPath -NoTypeInformation -Encoding UTF8
        }
        else {
            'RelPath,Depth,FileCount,FileCountExclDesktopIni,SubdirCount,RecursiveFileCount,IsEmptyExclDesktopIni,HasDriveSuffix' |
                Set-Content -Path $dirsCsvPath -Encoding UTF8
        }
    }
    catch {
        $walkErrorLines.Add("OUTPUT-WRITE-ERROR (dirs CSV): $($_.Exception.Message)")
    }

    # --- Build summary text (no Format-* cmdlets; explicit string building; counts only, no paths) ---
    $summaryLines = [System.Collections.Generic.List[string]]::new()
    $summaryLines.Add('HJR Founder OS - Folder Audit Inventory Summary')
    $summaryLines.Add('================================================')
    $summaryLines.Add('')
    $summaryLines.Add('Run status:    ' + $script:RunStatus)
    $summaryLines.Add('Start (local): ' + $startTime.ToString('yyyy-MM-dd HH:mm:ss'))
    $summaryLines.Add('End (local):   ' + $endTime.ToString('yyyy-MM-dd HH:mm:ss'))
    $summaryLines.Add('Elapsed:       ' + $elapsed.ToString())
    $summaryLines.Add('')
    $summaryLines.Add('Parameters')
    $summaryLines.Add('----------')
    $summaryLines.Add('  Root           = ' + $rootNorm)
    $summaryLines.Add('  OutDir         = ' + $outDirNorm)
    $summaryLines.Add('  IncludeOffline = ' + $IncludeOffline.IsPresent)
    $summaryLines.Add('  MaxHashMB      = ' + $MaxHashMB)
    $summaryLines.Add('  HashAll        = ' + $HashAll.IsPresent)
    $summaryLines.Add('  Quiet          = ' + $Quiet.IsPresent)
    $summaryLines.Add('')
    $summaryLines.Add('Totals')
    $summaryLines.Add('------')
    $summaryLines.Add('  Total files scanned: ' + $totalFiles)
    $summaryLines.Add('  Total dirs scanned:  ' + $totalDirs)
    $summaryLines.Add('  Total bytes:         ' + $totalBytes)
    $summaryLines.Add('')
    $summaryLines.Add('Files per TopLevel segment')
    $summaryLines.Add('--------------------------')
    foreach ($tlk in ($topLevelCounts.Keys | Sort-Object)) {
        $summaryLines.Add('  ' + $tlk + ': ' + $topLevelCounts[$tlk])
    }
    $summaryLines.Add('')
    $summaryLines.Add('desktop.ini count:           ' + $desktopIniCount)
    $summaryLines.Add('Placeholder (offline) files: ' + $placeholderCount)
    $summaryLines.Add('')
    $summaryLines.Add('Drive-suffix counts (file names)')
    $summaryLines.Add('---------------------------------')
    foreach ($sk in ($suffixCounts.Keys | Sort-Object)) {
        $summaryLines.Add('  ' + $sk + ': ' + $suffixCounts[$sk])
    }
    $summaryLines.Add('')
    $summaryLines.Add('Size-collision groups (SizeGroup >= 2):   ' + $sizeGroupCount)
    $summaryLines.Add('Files inside those size-collision groups: ' + $sizeGroupFilesCount)
    $summaryLines.Add('')
    $summaryLines.Add('Hashing results')
    $summaryLines.Add('---------------')
    $summaryLines.Add('  HASHED:          ' + $hashedCount)
    $summaryLines.Add('  NOT_NEEDED:      ' + $notNeededCount)
    $summaryLines.Add('  SKIPPED_OFFLINE: ' + $skippedOfflineCount)
    $summaryLines.Add('  SKIPPED_SIZE:    ' + $skippedSizeCount)
    $summaryLines.Add('  SKIPPED_ZERO:    ' + $skippedZeroCount)
    $summaryLines.Add('  ERROR:           ' + $hashErrorCount)
    $summaryLines.Add('')
    $summaryLines.Add('Confirmed byte-identical groups (SHA-256 occurring >= 2 times): ' + $dupHashGroupCount)
    $summaryLines.Add('Files inside those confirmed-duplicate groups:                  ' + $dupHashFilesCount)
    $summaryLines.Add('')
    $summaryLines.Add('Empty directories (excl desktop.ini): ' + $emptyDirCount)
    $summaryLines.Add('Directories with a Drive suffix:      ' + $dirSuffixCount)
    $summaryLines.Add('')
    $summaryLines.Add('Walk/hash errors logged: ' + $walkErrorLines.Count)
    $summaryLines.Add('(See _run-log-' + $stamp + '.txt for individual walk/hash error messages.)')

    $summaryText = [string]::Join([Environment]::NewLine, $summaryLines)

    try {
        $summaryText | Set-Content -Path $summaryPath -Encoding UTF8
    }
    catch {
        $walkErrorLines.Add("OUTPUT-WRITE-ERROR (summary): $($_.Exception.Message)")
    }

    # --- Flush run-log (walk + hash + any output-write errors) ---
    try {
        if ($walkErrorLines.Count -gt 0) {
            $walkErrorLines | Set-Content -Path $runLogPath -Encoding UTF8
        }
        else {
            'No walk or hash errors recorded.' | Set-Content -Path $runLogPath -Encoding UTF8
        }
    }
    catch {
        Write-Host "WARNING: could not write run-log to $runLogPath"
    }

    Write-Host ''
    Write-Host $summaryText
    Write-Host ''

    if ($script:RunStatus -eq 'COMPLETE') {
        exit 0
    }
    else {
        exit 1
    }
}
