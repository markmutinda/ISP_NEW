# ============================================================================
# Netily ISP - Full Docker Stack Script
# ============================================================================
# This script manages the complete Docker stack for Netily ISP.
#
# Usage:
#   .\scripts\docker_stack.ps1 start     - Start all containers
#   .\scripts\docker_stack.ps1 stop      - Stop all containers
#   .\scripts\docker_stack.ps1 restart   - Restart all containers
#   .\scripts\docker_stack.ps1 status    - Show container status
#   .\scripts\docker_stack.ps1 logs      - Show logs
#   .\scripts\docker_stack.ps1 rebuild   - Rebuild and restart
# ============================================================================

param(
    [Parameter(Position=0)]
    [ValidateSet("start", "stop", "restart", "status", "logs", "rebuild", "clean")]
    [string]$Action = "status"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ComposeFile = "$ProjectRoot\docker\docker-compose.yml"

Set-Location $ProjectRoot

function Show-Status {
    Write-Host ""
    Write-Host "Container Status:" -ForegroundColor Cyan
    Write-Host "-----------------" -ForegroundColor Gray
    
    $containers = @(
        @{Name="netily_db"; Service="PostgreSQL Database"; Port="5433"},
        @{Name="netily_redis"; Service="Redis Cache"; Port="6379"},
        @{Name="netily_radius"; Service="FreeRADIUS Server"; Port="1812/1813"},
        @{Name="netily_backend"; Service="Django Backend"; Port="8000"},
        @{Name="netily_worker"; Service="Celery Worker"; Port="-"},
        @{Name="netily_beat"; Service="Celery Beat"; Port="-"}
    )
    
    foreach ($c in $containers) {
        $status = docker inspect -f '{{.State.Status}}' $c.Name 2>$null
        $color = switch ($status) {
            "running" { "Green" }
            "exited" { "Red" }
            "restarting" { "Yellow" }
            default { "Gray" }
        }
        $statusText = if ($status) { $status.ToUpper() } else { "NOT FOUND" }
        Write-Host ("  {0,-20} {1,-25} {2,-10} {3}" -f $c.Name, $c.Service, $c.Port, $statusText) -ForegroundColor $color
    }
    Write-Host ""
}

function Start-Stack {
    Write-Host "Starting Docker stack..." -ForegroundColor Yellow
    docker compose -f $ComposeFile up -d
    Write-Host "Stack started." -ForegroundColor Green
    Show-Status
}

function Stop-Stack {
    Write-Host "Stopping Docker stack..." -ForegroundColor Yellow
    docker compose -f $ComposeFile stop
    Write-Host "Stack stopped." -ForegroundColor Green
}

function Restart-Stack {
    Write-Host "Restarting Docker stack..." -ForegroundColor Yellow
    docker compose -f $ComposeFile restart
    Write-Host "Stack restarted." -ForegroundColor Green
    Show-Status
}

function Show-Logs {
    Write-Host "Showing logs (Ctrl+C to exit)..." -ForegroundColor Yellow
    docker compose -f $ComposeFile logs -f --tail 50
}

function Rebuild-Stack {
    Write-Host "Rebuilding Docker stack..." -ForegroundColor Yellow
    docker compose -f $ComposeFile down
    docker compose -f $ComposeFile build --no-cache
    docker compose -f $ComposeFile up -d
    Write-Host "Stack rebuilt and started." -ForegroundColor Green
    Show-Status
}

function Clean-Stack {
    Write-Host "WARNING: This will remove all containers, images, and volumes!" -ForegroundColor Red
    $confirm = Read-Host "Are you sure? (yes/no)"
    if ($confirm -eq "yes") {
        docker compose -f $ComposeFile down -v --rmi all
        Write-Host "Stack cleaned." -ForegroundColor Green
    } else {
        Write-Host "Cancelled." -ForegroundColor Yellow
    }
}

# Execute action
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Netily Docker Stack Manager" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan

switch ($Action) {
    "start"   { Start-Stack }
    "stop"    { Stop-Stack }
    "restart" { Restart-Stack }
    "status"  { Show-Status }
    "logs"    { Show-Logs }
    "rebuild" { Rebuild-Stack }
    "clean"   { Clean-Stack }
}
