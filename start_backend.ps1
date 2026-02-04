# Netily Quick Start Script
# Run this in PowerShell to set up and start the development environment

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Netily ISP - Quick Start Script" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Check if virtual environment exists
$venvPath = ".\.venv\Scripts\Activate.ps1"
if (-not (Test-Path $venvPath)) {
    Write-Host "[!] Virtual environment not found. Creating..." -ForegroundColor Yellow
    python -m venv .venv
    & $venvPath
    Write-Host "[*] Installing dependencies..." -ForegroundColor Yellow
    pip install -r requirements/local.txt
} else {
    Write-Host "[+] Activating virtual environment..." -ForegroundColor Green
    & $venvPath
}

# Check database connection
Write-Host ""
Write-Host "[*] Checking database connection..." -ForegroundColor Yellow
python test_db.py
if ($LASTEXITCODE -ne 0) {
    Write-Host "[!] Database connection failed. Please check your .env file and ensure PostgreSQL is running." -ForegroundColor Red
    exit 1
}

# Run migrations
Write-Host ""
Write-Host "[*] Running database migrations..." -ForegroundColor Yellow
python manage.py migrate_schemas --shared
python manage.py migrate_schemas --tenant

# Start the server
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Starting Django Development Server" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Backend will be available at: http://localhost:8000" -ForegroundColor Green
Write-Host "Admin panel: http://localhost:8000/admin/" -ForegroundColor Green
Write-Host "API docs: http://localhost:8000/api/v1/docs/" -ForegroundColor Green
Write-Host ""
Write-Host "Press Ctrl+C to stop the server" -ForegroundColor Yellow
Write-Host ""

python manage.py runserver
