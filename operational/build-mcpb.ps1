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
    # Baseline default tracks the most recent shipped .mcpb so the
    # pyproject.toml deps (which get mirrored into the new .mcpb's
    # server/requirements.txt) stay current. If you build off an older
    # baseline you will silently drop any dep added since that release —
    # the script can't see deps that aren't in the baseline's pyproject.
    [string] $BaseRelease = '2.4.1',
    [string] $Version     = '',   # if empty, derive from _SHIM_VERSION in shim_server.py
    # v3.0.6 — description + long_description are no longer left frozen at
    # whatever the baseline .mcpb shipped. Pass these per-release so the
    # text Claude Desktop's Connector directory shows matches the version.
    # If either is empty the field in the manifest is preserved as-is
    # (back-compat with the pre-v3.0.6 pattern of hand-editing).
    [string] $Description     = '',
    [string] $LongDescription = '',
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

Step "Refresh bundled tools.json (SAP offline fallback)"
# The bundled tools.json is the SAP backend's offline fallback - loaded ONLY
# when pa_v2 is unreachable at first launch (otherwise the shim fetches /tools
# live). It MUST track the live catalogue, NOT be frozen at whatever the
# baseline .mcpb shipped: the 2.4.1 baseline carried a stale 104-tool V1
# snapshot (get_vendor_aging, ...) which the v2 backend does not even serve.
# Copy the repo's canonical tools.json. Regenerate it before a release via:
#   cd C:\claude\punch-analytics
#   .\.venv\Scripts\python.exe C:\claude\production_mcp_shim\generate_bundled_tools.py
$repoTools = Join-Path $repoRoot 'tools.json'
if (Test-Path $repoTools) {
    Copy-Item -LiteralPath $repoTools -Destination (Join-Path $staging 'server\tools.json') -Force
    $tj = Get-Content -Raw $repoTools | ConvertFrom-Json
    Ok ("tools.json refreshed ({0} tools, server_version {1})" -f $tj.count, $tj.server_version)
} else {
    Note "no repo tools.json at $repoTools - bundled fallback left as baseline (stale)"
}

Step "Bundle uv.lock (v3.4.3 - fast, deterministic env build)"
# Ship a uv.lock at the project root (next to pyproject.toml) so `uv run` skips
# re-resolution on first env build. Combined with the persistent UV_* env vars
# (added to the manifest below), the per-update env rebuild becomes a reuse.
# Regenerate before a release via:  bin\uv.exe lock  (in a dir with pyproject.toml)
$repoLock = Join-Path $repoRoot 'uv.lock'
if (Test-Path $repoLock) {
    Copy-Item -LiteralPath $repoLock -Destination (Join-Path $staging 'uv.lock') -Force
    Ok "uv.lock bundled at project root"
} else {
    Note "no repo uv.lock at $repoLock - shipping without a lock (uv will resolve at launch)"
}

Step "Sync deps from pyproject.toml into requirements.txt"
$pyprojPath = Join-Path $staging 'pyproject.toml'
$pyproj = Get-Content -Raw $pyprojPath
$depMatch = [regex]::Match($pyproj, '(?ms)dependencies\s*=\s*\[(.*?)\]')
if ($depMatch.Success) {
    $deps = [regex]::Matches($depMatch.Groups[1].Value, '"([^"]+)"') | ForEach-Object { $_.Groups[1].Value }

    # v3.2.1 — bake pywin32 into every Windows install. The Python mcp SDK's
    # client/stdio submodule imports pywintypes (from pywin32) on Windows;
    # without it the shim crashes on `from mcp.server.fastmcp import FastMCP`
    # before any tool is registered, taking down every backend the shim
    # federates. Surfaced 2026-05-23 when a laptop with a fresh venv hit
    # `ModuleNotFoundError: No module named 'pywintypes'` in Claude Desktop's
    # mcp-server log. Marker-controlled to remain idempotent if a baseline
    # already declares it.
    $pywin32Spec = 'pywin32 ; sys_platform == "win32"'
    if (-not ($deps | Where-Object { $_ -match '^pywin32(\s|;|$)' })) {
        $deps += $pywin32Spec
        Note "injected dep: $pywin32Spec"
        # Patch the staged pyproject.toml too, so `uv run` sees it at install time.
        # Insert before the closing ]; preserve existing whitespace style.
        # TOML quoting: an entry containing a double-quote must use literal
        # single-quoted strings so the inner "win32" survives.
        $newDepsBlock = ($deps | ForEach-Object {
            if ($_ -like '*"*') { "    '$_'" } else { "    `"$_`"" }
        }) -join ",`n"
        $pyproj = [regex]::Replace(
            $pyproj,
            '(?ms)(dependencies\s*=\s*\[)(.*?)(\])',
            "`$1`n$newDepsBlock,`n`$3"
        )
        [System.IO.File]::WriteAllText($pyprojPath, $pyproj, [System.Text.UTF8Encoding]::new($false))
        Ok "pyproject.toml deps array patched to include pywin32"
    } else {
        Note "pywin32 already in baseline pyproject.toml; no patch needed"
    }

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

Step "Update manifest.json (version + descriptions + bundled-runtime flag)"
$manPath = Join-Path $staging 'manifest.json'
$man = Get-Content -Raw $manPath | ConvertFrom-Json
$man.version = $Version

# v3.0.6 — overwrite user_config.punch_sap_url.default (was frozen at
# v1's mcp.punchpowertrain.com in the v2.4.1 baseline; v3.0.1 was
# supposed to fix this but only hand-edited a single .mcpb, never
# propagated back here, so every subsequent build silently re-introduced
# the bad default. A fresh-install user on Joris's laptop 2026-05-18
# saw the v1 URL prefilled and the shim then couldn't connect.)
if ($man.user_config -and $man.user_config.punch_sap_url) {
    $man.user_config.punch_sap_url.default = 'http://ai.punchpowertrain.com:3000'
    Note "user_config.punch_sap_url.default -> http://ai.punchpowertrain.com:3000"
}

# v3.0.6 — flip user_config.punch_sap_key.required from true to false.
# Pre-Kerberos the X-Punch-Auth header was the only auth path so this
# being required made sense. Post-Kerberos cutover (v0.0.115/116 +
# shim v3.0.0+) the default flow is Kerberos for humans + the install
# dialog shouldn't force the user to type something into a field they
# don't need — anything typed there ends up flipping the auto-seed's
# branch from Kerberos to x-punch-auth, with a placeholder key that
# 401s on every call.
if ($man.user_config -and $man.user_config.punch_sap_key) {
    $man.user_config.punch_sap_key.required = $false
    # Description rewritten so the install dialog matches the new shape.
    $man.user_config.punch_sap_key.description = 'OPTIONAL: only for service-account installs (headless callers that can''t carry a Windows ticket). Leave BLANK for human users — Kerberos/SPNEGO authenticates via your existing Windows logon. The shim''s auto-seed branches on this field: any value here triggers the X-Punch-Auth path; empty triggers Kerberos.'
    Note "user_config.punch_sap_key.required -> false (Kerberos is the default)"
}

# v3.0.6 / v3.4.2 - description + long_description. Precedence: caller arg >
# repo file (manifest-description.txt / manifest-long-description.txt) > the
# baseline value. The repo files are the canonical source so the directory text
# stops freezing at the baseline (it sat at the v2.4.1 blurb through v3.4.1).
$descFile = Join-Path $repoRoot 'manifest-description.txt'
$longFile = Join-Path $repoRoot 'manifest-long-description.txt'
if ($Description) {
    $man.description = $Description
    Note "description: caller arg"
} elseif (Test-Path $descFile) {
    $man.description = (Get-Content -Raw $descFile).Trim()
    Note "description: manifest-description.txt"
}
if ($LongDescription) {
    $man.long_description = $LongDescription
    Note "long_description: caller arg"
} elseif (Test-Path $longFile) {
    $man.long_description = (Get-Content -Raw $longFile).Trim()
    Note "long_description: manifest-long-description.txt"
}

# v3.4.2 - sync the declared `tools` array from the bundled tools.json so the
# connector directory shows the CURRENT catalogue, not the baseline's frozen V1
# set (the 2.4.1 baseline declared 92 get_* V1 tools the v2 backend never serves).
$repoToolsJson = Join-Path $repoRoot 'tools.json'
if (Test-Path $repoToolsJson) {
    $cat = Get-Content -Raw $repoToolsJson | ConvertFrom-Json
    $man.tools = @($cat.tools | ForEach-Object {
        [pscustomobject]@{ name = $_.name; description = $_.description }
    })
    Ok ("manifest.tools synced from tools.json ({0} tools)" -f $man.tools.Count)
}

# v3.0.6 — strip `compatibility.runtimes.python`. The bundled `bin/uv.exe`
# materializes its own Python on first launch, so declaring a system-level
# Python requirement is wrong: Claude Desktop's MCPB installer reads this
# field and warns/blocks if the user's machine doesn't have Python ≥3.10,
# even though our shim doesn't need it. Surfaced on a fresh-install attempt
# 2026-05-18 (kerberos-cutover follow-up).
if ($man.compatibility -and $man.compatibility.runtimes -and $man.compatibility.runtimes.python) {
    $man.compatibility.runtimes.PSObject.Properties.Remove('python')
    Note "compatibility.runtimes.python removed (bundled via uv.exe)"
}

# v3.4.3 - persistent uv paths so the env survives .mcpb updates. Each update
# extracts to a NEW dir; without these, `uv run` rebuilt the venv from scratch
# every time (download CPython + ~33 wheels) - minutes on a corporate network,
# because the MS Store Claude sandbox does not persist uv's default cache.
# HOME/.punch-shim/* is a stable, non-virtualised location, so the venv + cache
# + interpreter are REUSED on update (validated: 7s first build, 1s next update).
# Single-quoted so the literal ${HOME} is written for Claude Desktop to expand.
$uvEnv = @{
    'UV_PROJECT_ENVIRONMENT' = '${HOME}/.punch-shim/venv'
    'UV_CACHE_DIR'           = '${HOME}/.punch-shim/uv-cache'
    'UV_PYTHON_INSTALL_DIR'  = '${HOME}/.punch-shim/uv-python'
}
if ($man.server -and $man.server.mcp_config) {
    if (-not $man.server.mcp_config.env) {
        $man.server.mcp_config | Add-Member -NotePropertyName env -NotePropertyValue ([pscustomobject]@{}) -Force
    }
    foreach ($k in $uvEnv.Keys) {
        $man.server.mcp_config.env | Add-Member -NotePropertyName $k -NotePropertyValue $uvEnv[$k] -Force
    }
    Ok "manifest server.env: persistent UV_* paths set under HOME/.punch-shim"
}

$manJson = ($man | ConvertTo-Json -Depth 10) + "`n"
[System.IO.File]::WriteAllText($manPath, $manJson, [System.Text.UTF8Encoding]::new($false))
Ok "manifest.json -> $Version"

Step "Re-zip"
if (Test-Path $outMcpb) { Remove-Item -LiteralPath $outMcpb -Force }
$items = Get-ChildItem -LiteralPath $staging
Compress-Archive -Path ($items | ForEach-Object { $_.FullName }) -DestinationPath $outMcpb -CompressionLevel Optimal -Force
$size = (Get-Item $outMcpb).Length
Ok "Built $outMcpb ($size bytes)"

Remove-Item -Recurse -Force $staging

# Sync the top-level auto-update manifest.json to match the current
# shim_server.py — otherwise a laptop that fetches the manifest after
# a shim_server.py bump but before someone hand-edits the manifest
# would either refuse the new shim (sha mismatch) or believe it's still
# on the old version. Same file matt would otherwise hand-edit; this
# just spares him the bookkeeping.
if (-not $DryRun) {
    Step "Sync top-level manifest.json for auto-update"
    $topManPath = Join-Path $repoRoot 'manifest.json'
    if (Test-Path $topManPath) {
        $topMan = Get-Content -Raw $topManPath | ConvertFrom-Json
        $shimBytes = [System.IO.File]::ReadAllBytes($canonical)
        $shimSha   = (Get-FileHash -Algorithm SHA256 -Path $canonical).Hash.ToLower()
        $shimSize  = $shimBytes.Length
        $topMan.version     = $Version
        $topMan.sha256      = $shimSha
        $topMan.size_bytes  = $shimSize
        $topMan.released_at = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
        $topManJson = ($topMan | ConvertTo-Json -Depth 10) + "`n"
        [System.IO.File]::WriteAllText($topManPath, $topManJson, [System.Text.UTF8Encoding]::new($false))
        Ok "manifest.json (top-level / auto-update) synced: v$Version  sha=$($shimSha.Substring(0,12))...  size=$shimSize"
    } else {
        Note "(top-level manifest.json not found at $topManPath — skipped)"
    }
} else {
    Note "Dry-run: artifact lives in $env:TEMP, not committed to releases/; top-level manifest.json NOT touched"
}
