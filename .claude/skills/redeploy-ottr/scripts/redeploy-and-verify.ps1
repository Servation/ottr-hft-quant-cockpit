<#
redeploy-and-verify.ps1 — OTTR stack redeploy + post-deploy health verification.

Why this exists: redeploy.ps1 ends in `docker compose logs -f`, which follows the
logs forever and blocks any unattended/agent run. This script does the same rebuild
+ recreate WITHOUT that trailing tail, then runs the 4-point health check and prints
a PASS/FAIL summary so a deploy can be driven and verified hands-off.

Safety (mirrors redeploy.ps1 + docs/RUNBOOK.md):
  - A rebuild is required every deploy: the frontend bakes VITE_OTTR_API_KEY at build
    time and the gateway installs deps. That's why this always `build`s.
  - Data lives in ./discord-bridge/data (host bind-mount) and is NOT touched. We use
    plain `down`, NEVER `down -v` (which would delete volumes).

Usage:
  pwsh redeploy-and-verify.ps1               # full redeploy, then verify
  pwsh redeploy-and-verify.ps1 -VerifyOnly   # skip the rebuild, just run the checks
  pwsh redeploy-and-verify.ps1 -TimeoutSec 180

Exit code: 0 if all hard checks pass, 1 otherwise.
#>

[CmdletBinding()]
param(
    [switch]$VerifyOnly,
    [int]$TimeoutSec = 120
)

$ErrorActionPreference = "Stop"

# Resolve the repo root by walking up from this script until docker-compose.yml is
# found, so the script works no matter the caller's working directory.
function Find-RepoRoot {
    $dir = $PSScriptRoot
    while ($dir) {
        if (Test-Path (Join-Path $dir "docker-compose.yml")) { return $dir }
        $parent = Split-Path $dir -Parent
        if ($parent -eq $dir) { break }
        $dir = $parent
    }
    throw "Could not locate docker-compose.yml above $PSScriptRoot"
}

$repo = Find-RepoRoot
Set-Location $repo
Write-Host "Repo root: $repo" -ForegroundColor Cyan

# ── Redeploy (down -> build -> up -d), minus the blocking logs tail ──────────
if (-not $VerifyOnly) {
    Write-Host "==> Stopping containers (data preserved; never -v)..." -ForegroundColor Cyan
    docker compose down
    Write-Host "==> Building images (frontend re-bakes API key, gateway installs deps)..." -ForegroundColor Cyan
    docker compose build
    Write-Host "==> Starting stack..." -ForegroundColor Cyan
    docker compose up -d
    Write-Host ""
    docker compose ps
    Write-Host ""
}

# ── Verification ────────────────────────────────────────────────────────────
function New-Deadline { param([int]$Seconds) (Get-Date).AddSeconds($Seconds) }

# 1. Gateway health: poll until {"status":"OK"} or timeout.
Write-Host "==> [1/4] Gateway health (http://localhost:8000/api/v1/health)..." -ForegroundColor Cyan
$gatewayOk = $false
$deadline = New-Deadline $TimeoutSec
do {
    try {
        $h = Invoke-RestMethod -Uri 'http://localhost:8000/api/v1/health' -TimeoutSec 5
        if ($h.status -eq 'OK') { $gatewayOk = $true; break }
    } catch { Start-Sleep -Seconds 3 }
} while ((Get-Date) -lt $deadline)

# 2. Bridge fully operational: poll the logs for the readiness line.
Write-Host "==> [2/4] Bridge 'fully operational' (discord-bridge logs)..." -ForegroundColor Cyan
$bridgeOk = $false
$deadline = New-Deadline $TimeoutSec
do {
    $logs = (docker compose logs --tail=200 discord-bridge 2>&1) | Out-String
    if ($logs -match 'fully operational') { $bridgeOk = $true; break }
    Start-Sleep -Seconds 3
} while ((Get-Date) -lt $deadline)

# 3 + 4. Embeddings work in-container, and report the index size. One exec: pipe a
# small program to the container's python via stdin (avoids -c quoting issues).
Write-Host "==> [3/4] Embeddings from inside the container..." -ForegroundColor Cyan
$embedOk = $false
$embedDim = 0
$indexCount = -1
$py = @'
import asyncio, os, json
from bot.embeddings import embed
v = asyncio.run(embed('redeploy verify smoke test'))
dim = len(v) if v else 0
try:
    n = len(json.load(open('data/embeddings_index.json')))
except Exception:
    n = -1
print(f'EMBED {dim} {n}')
'@
try {
    $out = ($py | docker compose exec -T discord-bridge python 2>&1) | Out-String
    $m = [regex]::Match($out, 'EMBED\s+(\d+)\s+(-?\d+)')
    if ($m.Success) {
        $embedDim = [int]$m.Groups[1].Value
        $indexCount = [int]$m.Groups[2].Value
        $embedOk = $embedDim -gt 0
    }
    if (-not $embedOk) { Write-Host $out.Trim() -ForegroundColor DarkYellow }
} catch { Write-Host "embeddings check error: $($_.Exception.Message)" -ForegroundColor DarkYellow }

Write-Host "==> [4/4] Embedding index size: $indexCount (informational; cap is 50)" -ForegroundColor Cyan

# ── Summary ─────────────────────────────────────────────────────────────────
function Row { param($name, [bool]$ok, $detail = "")
    $mark = if ($ok) { "PASS" } else { "FAIL" }
    $color = if ($ok) { "Green" } else { "Red" }
    Write-Host ("  {0,-22} {1}  {2}" -f $name, $mark, $detail) -ForegroundColor $color
}

Write-Host ""
Write-Host "===== POST-DEPLOY VERIFICATION =====" -ForegroundColor Cyan
Row "gateway_health"      $gatewayOk
Row "bridge_operational"  $bridgeOk
Row "embeddings"          $embedOk ("dim=$embedDim")
Write-Host ("  {0,-22} {1}  {2}" -f "embedding_index", "INFO", "count=$indexCount (cap 50)") -ForegroundColor Gray

$allPass = $gatewayOk -and $bridgeOk -and $embedOk
Write-Host ""
if ($allPass) {
    Write-Host "ALL CHECKS PASSED — deploy is healthy." -ForegroundColor Green
} else {
    Write-Host "ONE OR MORE CHECKS FAILED — see docs/RUNBOOK.md (Troubleshooting)." -ForegroundColor Red
    Write-Host "Tips: embeddings FAIL usually means LM Studio (host:1234) is down;" -ForegroundColor DarkYellow
    Write-Host "      bridge FAIL — inspect 'docker compose logs --tail=80 discord-bridge'." -ForegroundColor DarkYellow
}
exit ([int](-not $allPass))
