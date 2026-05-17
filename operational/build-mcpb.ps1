# Build a new .mcpb release from the current shim_server.py source.
#
# Usage:
#     pwsh operational\build-mcpb.ps1                       # uses _SHIM_VERSION from shim_server.py
#     pwsh operational\build-mcpb.ps1 -BaseRelease 2.2.6    # which existing .mcpb to use as the layout baseline
#     pwsh operational\build-mcpb.ps1 -DryRun               # build to %TEMP%, don't drop into releases/
#
# What it does:
#   1. Read _SHIM_VERSION from shim_server.py (target version).
#   2. Take the structure of releases/punch-analytics-<BaseRelease>.mcpb (bundled uv.exe + dir layout).
#   3. Replace server/shim_server.py with the current canonical.
#   4. Read deps from pyproject.toml::[project].dependencies, mirror into server/requirements.txt.
#   5. Bump manifest.json::version. (Description / long_description are NOT auto-rewritten;
#      caller should hand-edit manifest.json BEFORE running for a meaningful release.)
#   6. Re-zip into releases/punch-analytics-<version>.mcpb.
#
# This script does NOT push to GitHub. After it runs, commit the new .mcpb and
# (optionally) run operational/publish-shim-to-github.ps1 to bump the
# auto-update manifest as well.

[CmdletBinding()]
param(
    [string] $BaseRelease = '2.2.6',
    [string] $Version     = '',   # if empty, derive from _SHIM_VERSION in shim_server.py
    [switch] $DryRun
)

$ErrorActionPreference = 'Stop'

function Note([string]$m){ Write-Host "      $m" -ForegroundColor Gray }
function Step([string]$m){ Write-Host ""; Write-Host ">>>   $m" -ForegroundColor Cyan }
function Ok  ([string]$m){ Write-Host "OK    $m" -ForegroundColor Green }
function Fail([string]$m){ Write-Host "FAIL  $m" -ForegroundColor Red }

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot  = Resolve-Path (Join-Path $scriptDir '..')
$canonical = Join-Path $repoRoot 'shim_server.py'
$baseMcpb  = Join-Path $repoRoot ('releases\punch-analytics-' + $BaseRelease + '.mcpb')

if (-not (Test-Path $canonical)) { Fail "shim_server.py not at $canonical"; exit 1 }
if (-not (Test-Path $baseMcpb))  { Fail "base mcpb not at $baseMcpb";       exit 1 }

# Derive version if not given
if (-not $Version) {
    $m = [regex]::Match((Get-Content -Raw $canonical), '(?m)^_SHIM_VERSION\s*=\s*"(\d+\.\d+\.\d+)"')
    if (-not $m.Success) { Fail "shim_server.py has no parseable _SHIM_VERSION"; exit 1 }
    $Version = $m.Groups[1].Value
}

Step "Building punch-analytics-$Version.mcpb (baseline: v$BaseRelease)"
Note "canonical: $canonical"
Note "base mcpb: $baseMcpb"

$outDir = if ($DryRun) { $env:TEMP } else { Join-Path $repoRoot 'releases' }
$outMcpb = Join-Path $outDir ("punch-analytics-$Version.mcpb")
$staging = Join-Path $env:TEMP ('mcpb-build-' + [Guid]::NewGuid().ToString('N').Substring(0,8))

Step "Staging"
New-Item -ItemType Directory -Force -Path $staging | Out-Null
Expand-Archive -LiteralPath $baseMcpb -DestinationPath $staging -Force

Step "Replace shim_server.py"
Copy-Item -LiteralPath $canonical -Destination (Join-Path $staging 'server\shim_server.py') -Force
Ok "shim_server.py updated"

Step "Sync deps from pyproject.toml into requirements.txt"
$pyproj = Get-Content -Raw (Join-Path $staging 'pyproject.toml')
$depMatch = [regex]::Match($pyproj, '(?ms)dependencies\s*=\s*\[(.*?)\]')
if ($depMatch.Success) {
    $deps = [regex]::Matches($depMatch.Groups[1].Value, '"([^"]+)"') | ForEach-Object { $_.Groups[1].Value }
    $reqLines = @(
        '# Laptop-side requirements -- auto-derived from pyproject.toml::[project].dependencies by build-mcpb.ps1.'
        ''
    ) + $deps
    [System.IO.File]::WriteAllText(
        (Join-Path $staging 'server\requirements.txt'),
        ($reqLines -join "`n") + "`n",
        [System.Text.UTF8Encoding]::new($false)
    )
    Ok ("requirements.txt synced ({0} deps: {1})" -f $deps.Count, ($deps -join ', '))
} else {
    Note "pyproject.toml has no parseable dependencies array; leaving requirements.txt as-is"
}

Step "Bump manifest.json version"
$manPath = Join-Path $staging 'manifest.json'
$man = Get-Content -Raw $manPath | ConvertFrom-Json
$man.version = $Version
$manJson = ($man | ConvertTo-Json -Depth 10) + "`n"
[System.IO.File]::WriteAllText($manPath, $manJson, [System.Text.UTF8Encoding]::new($false))
Ok "manifest.json -> $Version"
Note "(description / long_description NOT updated; edit by hand for a meaningful release)"

Step "Re-zip"
if (Test-Path $outMcpb) { Remove-Item -LiteralPath $outMcpb -Force }
$items = Get-ChildItem -LiteralPath $staging
Compress-Archive -Path ($items | ForEach-Object { $_.FullName }) -DestinationPath $outMcpb -CompressionLevel Optimal -Force
$size = (Get-Item $outMcpb).Length
Ok "Built $outMcpb ($size bytes)"

Remove-Item -Recurse -Force $staging
if ($DryRun) { Note "Dry-run: artifact lives in $env:TEMP, not committed to releases/" }
