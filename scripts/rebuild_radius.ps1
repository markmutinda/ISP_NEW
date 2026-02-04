# ============================================================================
# Netily ISP - RADIUS Container Rebuild Script
# ============================================================================
# This script rebuilds and restarts the FreeRADIUS container with the latest
# configuration changes.
#
# Usage: .\scripts\rebuild_radius.ps1
# ============================================================================

param(
    [switch]$NoCache,
    [switch]$Logs,
    [switch]$Test
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Netily RADIUS Container Rebuild Script" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# Navigate to project root
Set-Location $ProjectRoot

# Step 1: Stop existing container
Write-Host "[1/5] Stopping existing RADIUS container..." -ForegroundColor Yellow
docker compose -f docker/docker-compose.yml stop radius 2>$null
Write-Host "      Done." -ForegroundColor Green

# Step 2: Remove old container
Write-Host "[2/5] Removing old container..." -ForegroundColor Yellow
docker compose -f docker/docker-compose.yml rm -f radius 2>$null
Write-Host "      Done." -ForegroundColor Green

# Step 3: Build new image
Write-Host "[3/5] Building new RADIUS image..." -ForegroundColor Yellow
if ($NoCache) {
    Write-Host "      (Using --no-cache)" -ForegroundColor Gray
    docker compose -f docker/docker-compose.yml build radius --no-cache
} else {
    docker compose -f docker/docker-compose.yml build radius
}

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Build failed!" -ForegroundColor Red
    exit 1
}
Write-Host "      Done." -ForegroundColor Green

# Step 4: Start new container
Write-Host "[4/5] Starting new RADIUS container..." -ForegroundColor Yellow
docker compose -f docker/docker-compose.yml up -d radius

if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to start container!" -ForegroundColor Red
    exit 1
}
Write-Host "      Done." -ForegroundColor Green

# Step 5: Wait and verify
Write-Host "[5/5] Waiting for container to initialize..." -ForegroundColor Yellow
Start-Sleep -Seconds 5

$status = docker inspect -f '{{.State.Status}}' netily_radius 2>$null
if ($status -eq "running") {
    Write-Host ""
    Write-Host "============================================" -ForegroundColor Green
    Write-Host "  RADIUS Container Started Successfully!" -ForegroundColor Green
    Write-Host "============================================" -ForegroundColor Green
    Write-Host ""
    Write-Host "Ports:" -ForegroundColor Cyan
    Write-Host "  - Auth:  1812/udp" -ForegroundColor White
    Write-Host "  - Acct:  1813/udp" -ForegroundColor White
    Write-Host "  - Debug: 18120/tcp" -ForegroundColor White
    Write-Host ""
} else {
    Write-Host "WARNING: Container may not be running properly." -ForegroundColor Red
    Write-Host "Status: $status" -ForegroundColor Red
    Write-Host ""
    Write-Host "Check logs with: docker logs netily_radius" -ForegroundColor Yellow
    exit 1
}

# Optional: Show logs
if ($Logs) {
    Write-Host "Container Logs (last 30 lines):" -ForegroundColor Cyan
    Write-Host "--------------------------------" -ForegroundColor Gray
    docker logs --tail 30 netily_radius
}

# Optional: Run test
if ($Test) {
    Write-Host ""
    Write-Host "Running RADIUS test..." -ForegroundColor Cyan
    & "$ProjectRoot\scripts\test_radius.ps1"
}

Write-Host ""
Write-Host "Next Steps:" -ForegroundColor Yellow
Write-Host "  1. Configure MikroTik to use this RADIUS server" -ForegroundColor White
Write-Host "  2. Run: .\scripts\test_radius.ps1" -ForegroundColor White
Write-Host "  3. Check logs: docker logs -f netily_radius" -ForegroundColor White
Write-Host ""
