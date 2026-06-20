# OTTR redeploy: rebuild images (frontend re-bakes the API key, gateway picks up
# new deps) and recreate containers. Safe: your data lives in ./discord-bridge/data
# (host bind-mount) and is NOT touched.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "==> Stopping existing containers (data is preserved)..." -ForegroundColor Cyan
docker compose down

Write-Host "==> Building images..." -ForegroundColor Cyan
docker compose build

Write-Host "==> Starting stack..." -ForegroundColor Cyan
docker compose up -d

Write-Host ""
docker compose ps
Write-Host ""
Write-Host "==> Tailing discord-bridge logs (Ctrl+C to stop)." -ForegroundColor Green
Write-Host "    Look for: no 'CONFIG:' errors + a successful Discord login." -ForegroundColor Green
docker compose logs -f discord-bridge
