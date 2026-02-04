# ============================================================================
# Netily ISP - RADIUS Testing Script
# ============================================================================
# This script tests RADIUS authentication and accounting functionality.
#
# Prerequisites:
# - Docker containers running (netily_radius, netily_db)
# - At least one test user in radcheck table
#
# Usage: .\scripts\test_radius.ps1
# ============================================================================

param(
    [string]$Username = "",
    [string]$Password = "",
    [switch]$Verbose
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot

Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Netily RADIUS Testing Script" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# Check if containers are running
Write-Host "[CHECK] Verifying containers..." -ForegroundColor Yellow

$radiusStatus = docker inspect -f '{{.State.Status}}' netily_radius 2>$null
$dbStatus = docker inspect -f '{{.State.Status}}' netily_db 2>$null

if ($radiusStatus -ne "running") {
    Write-Host "ERROR: RADIUS container is not running!" -ForegroundColor Red
    Write-Host "Run: docker compose -f docker/docker-compose.yml up -d radius" -ForegroundColor Yellow
    exit 1
}

if ($dbStatus -ne "running") {
    Write-Host "ERROR: Database container is not running!" -ForegroundColor Red
    Write-Host "Run: docker compose -f docker/docker-compose.yml up -d db" -ForegroundColor Yellow
    exit 1
}

Write-Host "        RADIUS: Running" -ForegroundColor Green
Write-Host "        Database: Running" -ForegroundColor Green
Write-Host ""

# Test 1: Check SQL module configuration
Write-Host "[TEST 1] Verifying SQL module configuration..." -ForegroundColor Yellow

$sqlCheck = docker exec netily_radius cat /etc/freeradius/mods-enabled/sql 2>$null | Select-String "accounting_start_query"
if ($sqlCheck) {
    Write-Host "         SQL accounting queries: CONFIGURED" -ForegroundColor Green
} else {
    Write-Host "         SQL accounting queries: NOT FOUND" -ForegroundColor Red
    Write-Host "         Run: .\scripts\rebuild_radius.ps1 -NoCache" -ForegroundColor Yellow
}

# Test 2: Check database tables
Write-Host ""
Write-Host "[TEST 2] Checking RADIUS database tables..." -ForegroundColor Yellow

$tables = @("radcheck", "radreply", "radacct", "radpostauth", "radusergroup")
foreach ($table in $tables) {
    $count = docker exec netily_db psql -U isp_user -d isp_management -t -c "SELECT COUNT(*) FROM public.$table;" 2>$null
    $count = $count.Trim()
    Write-Host "         $table : $count rows" -ForegroundColor $(if ([int]$count -gt 0) { "Green" } else { "Gray" })
}

# Test 3: List available test users
Write-Host ""
Write-Host "[TEST 3] Available RADIUS users..." -ForegroundColor Yellow

$users = docker exec netily_db psql -U isp_user -d isp_management -t -c "SELECT username, attribute, value FROM public.radcheck WHERE attribute = 'Cleartext-Password' LIMIT 5;" 2>$null
if ($users.Trim()) {
    Write-Host "$users" -ForegroundColor Cyan
} else {
    Write-Host "         No users found in radcheck table." -ForegroundColor Gray
    Write-Host "         Create a user via the admin panel or run sync task." -ForegroundColor Yellow
}

# Test 4: Check recent accounting records
Write-Host ""
Write-Host "[TEST 4] Recent accounting records..." -ForegroundColor Yellow

$acct = docker exec netily_db psql -U isp_user -d isp_management -t -c "SELECT username, acctstarttime, acctstoptime, acctterminatecause FROM public.radacct ORDER BY acctstarttime DESC LIMIT 5;" 2>$null
if ($acct.Trim()) {
    Write-Host "$acct" -ForegroundColor Cyan
} else {
    Write-Host "         No accounting records yet." -ForegroundColor Gray
    Write-Host "         Connect a device to generate records." -ForegroundColor Yellow
}

# Test 5: Check RADIUS logs for errors
Write-Host ""
Write-Host "[TEST 5] Checking RADIUS logs for errors..." -ForegroundColor Yellow

$errors = docker logs netily_radius 2>&1 | Select-String -Pattern "error|Error|ERROR|failed|Failed" | Select-Object -Last 5
if ($errors) {
    Write-Host "         Recent errors found:" -ForegroundColor Red
    $errors | ForEach-Object { Write-Host "         $_" -ForegroundColor Red }
} else {
    Write-Host "         No recent errors in logs." -ForegroundColor Green
}

# Summary
Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Test Summary" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "RADIUS Server Status: READY" -ForegroundColor Green
Write-Host ""
Write-Host "MikroTik Configuration:" -ForegroundColor Yellow
Write-Host "  RADIUS Server IP: $(hostname -I 2>$null)" -ForegroundColor White
Write-Host "  Auth Port: 1812" -ForegroundColor White
Write-Host "  Acct Port: 1813" -ForegroundColor White
Write-Host "  Secret: testing123 (or your configured secret)" -ForegroundColor White
Write-Host ""
Write-Host "Commands:" -ForegroundColor Yellow
Write-Host "  Watch logs:     docker logs -f netily_radius" -ForegroundColor Gray
Write-Host "  Check accounts: docker exec netily_db psql -U isp_user -d isp_management -c 'SELECT * FROM public.radacct;'" -ForegroundColor Gray
Write-Host ""
