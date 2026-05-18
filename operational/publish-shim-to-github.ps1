# Publish a new shim release: build the .mcpb, commit + push the
# updated shim_server.py + manifest.json + releases/<version>.mcpb,
# and tag the version.
#
# Usage:
#     pwsh operational\publish-shim-to-github.ps1                  # full publish
#     pwsh operational\publish-shim-to-github.ps1 -DryRun          # build + dry git status, no commit/push
#     pwsh operational\publish-shim-to-github.ps1 -SkipBuild       # skip build-mcpb, commit existing files only
#     pwsh operational\publish-shim-to-github.ps1 -NoTag           # commit + push but skip the git tag
#
# Steps:
#   1. Read _SHIM_VERSION from shim_server.py — this is the target version.
#   2. Run operational\build-mcpb.ps1 (unless -SkipBuild) — produces
#      releases/punch-analytics-<version>.mcpb and syncs the top-level
#      manifest.json with the new sha256/size/version/released_at.
#   3. Verify the working tree has the expected three changes:
#        - shim_server.py            (the user's _SHIM_VERSION bump)
#        - manifest.json             (updated by build-mcpb)
#        - releases/<version>.mcpb   (new artifact from build-mcpb)
#   4. Commit the three with a message that includes the version.
#   5. Tag v<version> on the new commit.
#   6. Push origin main + the new tag.
#
# KRB-2 — backlog: pre-v3.0.5 a publish flow only updated shim_server.py
# + manifest.json (the auto-update path), but never rebuilt the .mcpb.
# A fresh-install user (downloading the .mcpb from releases/) got whatever
# was last manually built, which could drift from what auto-update served.
# This script ties both together so they can never drift.
#
# Idempotent against an in-progress publish: if releases/<version>.mcpb
# already exists and matches the current shim_server.py, build-mcpb is a
# no-op rebuild (same content -> same size; manifest.json may show a new
# released_at timestamp, which is fine to overwrite).

[CmdletBinding()]
param(
    [switch] $DryRun,
    [switch] $SkipBuild,
    [switch] $NoTag,
    [switch] $NoGhRelease,            # skip the `gh release create` step
    [string] $BaseRelease = '2.4.1',  # forwarded to build-mcpb.ps1
    [string] $TagPrefix   = 'punch-analytics-v',
    [string] $ReleaseTitle = ''       # if empty, generated from version
)

$ErrorActionPreference = 'Stop'

function Note([string]$m){ Write-Host "      $m" -ForegroundColor Gray }
function Step([string]$m){ Write-Host ""; Write-Host ">>>   $m" -ForegroundColor Cyan }
function Ok  ([string]$m){ Write-Host "OK    $m" -ForegroundColor Green }
function Fail([string]$m){ Write-Host "FAIL  $m" -ForegroundColor Red }

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot  = Resolve-Path (Join-Path $scriptDir '..')
Set-Location $repoRoot

# 1. Read version
$shimSrc  = Join-Path $repoRoot 'shim_server.py'
$m = [regex]::Match((Get-Content -Raw $shimSrc), '(?m)^_SHIM_VERSION\s*=\s*"(\d+\.\d+\.\d+)"')
if (-not $m.Success) { Fail "shim_server.py has no parseable _SHIM_VERSION"; exit 1 }
$Version = $m.Groups[1].Value
$Mcpb = Join-Path $repoRoot ('releases\punch-analytics-' + $Version + '.mcpb')

Step "Publishing shim v$Version"
Note "repo:    $repoRoot"
Note "mcpb:    $Mcpb"
Note "dry-run: $DryRun"

# 2. Build (unless skipped)
if ($SkipBuild) {
    Note "-SkipBuild: not invoking build-mcpb.ps1"
    if (-not (Test-Path $Mcpb)) {
        Fail "releases/punch-analytics-$Version.mcpb does not exist and -SkipBuild was set. Either drop -SkipBuild or place the .mcpb manually."
        exit 1
    }
} else {
    Step "Running build-mcpb.ps1"
    $buildScript = Join-Path $scriptDir 'build-mcpb.ps1'
    & pwsh -NoProfile -File $buildScript -BaseRelease $BaseRelease
    if ($LASTEXITCODE -ne 0) { Fail "build-mcpb.ps1 exited $LASTEXITCODE"; exit $LASTEXITCODE }
    if (-not (Test-Path $Mcpb)) { Fail "build-mcpb didn't produce $Mcpb"; exit 1 }
    Ok "Built $Mcpb"
}

# 3. Verify git working tree
Step "Verifying git state"
$gitStatus = & git status --porcelain
$statusLines = $gitStatus -split "`n" | Where-Object { $_ }
Note ("Working tree has {0} changed/new path(s)" -f $statusLines.Count)
foreach ($line in $statusLines) { Note $line }

$expected = @('shim_server.py', 'manifest.json', "releases/punch-analytics-$Version.mcpb")
$found = @()
foreach ($exp in $expected) {
    if ($statusLines | Where-Object { $_ -match [regex]::Escape($exp) }) {
        $found += $exp
    }
}
if ($found.Count -eq 0) {
    Fail "None of the expected publish files are dirty. Did you forget to bump _SHIM_VERSION? (current: $Version)"
    exit 1
}

if ($DryRun) {
    Step "Dry-run: stopping before commit/tag/push"
    Note ("Would commit: {0}" -f ($found -join ', '))
    Note "Would tag:    $TagPrefix$Version"
    Note "Would push:   origin main + tag $TagPrefix$Version"
    if (-not $NoGhRelease) { Note "Would gh release create $TagPrefix$Version with releases/punch-analytics-$Version.mcpb attached" }
    exit 0
}

# 4. Commit
Step "Committing"
foreach ($f in $found) { & git add $f }
$commitMsg = "shim v$Version — published via publish-shim-to-github.ps1"
& git commit -m $commitMsg
if ($LASTEXITCODE -ne 0) { Fail "git commit exited $LASTEXITCODE"; exit $LASTEXITCODE }
Ok "Committed v$Version"

# 5. Tag (project convention: punch-analytics-vX.Y.Z)
$tagName = "$TagPrefix$Version"
if (-not $NoTag) {
    Step "Tagging $tagName"
    & git tag -a $tagName -m "shim $tagName"
    if ($LASTEXITCODE -ne 0) { Fail "git tag exited $LASTEXITCODE"; exit $LASTEXITCODE }
    Ok "Tagged $tagName"
}

# 6. Push
Step "Pushing to origin"
& git push origin main
if ($LASTEXITCODE -ne 0) { Fail "git push main exited $LASTEXITCODE"; exit $LASTEXITCODE }
if (-not $NoTag) {
    & git push origin $tagName
    if ($LASTEXITCODE -ne 0) { Fail "git push tag exited $LASTEXITCODE"; exit $LASTEXITCODE }
}
Ok "Pushed $tagName to origin"

# 7. Create GitHub Release (the user-facing "Releases" tab depends on this;
# a tag alone is invisible there).
if (-not $NoGhRelease -and -not $NoTag) {
    Step "Creating GitHub Release $tagName"
    if (-not $ReleaseTitle) {
        $ReleaseTitle = "Punch Analytics MCPB v$Version"
    }
    $mcpbName = "punch-analytics-$Version.mcpb"
    $mcpbPath = Join-Path $repoRoot ('releases/' + $mcpbName)
    # gh-release notes: pull the last commit body so the release page links
    # back to the change rationale without manual copy-paste.
    $lastCommitBody = & git log -1 --format='%B'
    $notesPath = [System.IO.Path]::GetTempFileName()
    [System.IO.File]::WriteAllText($notesPath, $lastCommitBody, [System.Text.UTF8Encoding]::new($false))
    try {
        & gh release create $tagName $mcpbPath --title $ReleaseTitle --notes-file $notesPath
        if ($LASTEXITCODE -ne 0) {
            Fail "gh release create exited $LASTEXITCODE (the tag is pushed; you can run gh release create by hand later)"
        } else {
            Ok "Release $tagName published"
        }
    } finally {
        Remove-Item $notesPath -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Write-Host "Done. Auto-update laptops will pick up v$Version on next Claude Desktop bounce." -ForegroundColor Green
