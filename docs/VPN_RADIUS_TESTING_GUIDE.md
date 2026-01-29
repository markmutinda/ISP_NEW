# VPN & RADIUS Testing Guide

> **Netily ISP Management System**  
> **Document Type:** Hands-On Testing Guide  
> **Last Updated:** January 2026

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Test Environment Setup](#test-environment-setup)
3. [Phase 1: Database & API Testing](#phase-1-database--api-testing)
4. [Phase 2: FreeRADIUS Testing](#phase-2-freeradius-testing)
5. [Phase 3: MikroTik RADIUS Integration](#phase-3-mikrotik-radius-integration)
6. [Phase 4: VPN Testing](#phase-4-vpn-testing)
7. [Phase 5: End-to-End Testing](#phase-5-end-to-end-testing)
8. [Automated Test Scripts](#automated-test-scripts)
9. [Troubleshooting](#troubleshooting)

---

## Prerequisites

### Hardware/Software Required

| Component | Requirement | Notes |
|-----------|-------------|-------|
| **MikroTik Router** | Any RouterOS 6.x or 7.x | CHR (Cloud Hosted Router) works for testing |
| **Netily Server** | Running Django + Docker | Can be local or cloud |
| **Test Client** | Laptop/PC for PPPoE/Hotspot | Or use MikroTik's built-in tools |
| **Network Access** | Router can reach Netily server | For RADIUS communication |

### MikroTik Router Options for Testing

```
Option 1: Physical Router (Recommended)
- Any MikroTik device (hAP, RB750, CCR, etc.)
- Connected to your network

Option 2: Cloud Hosted Router (CHR)
- Download from https://mikrotik.com/download
- Run in VirtualBox, VMware, or Hyper-V
- Free license allows 1Mbps (enough for testing)

Option 3: RouterOS x86
- Install on any x86 machine
- Good for lab environments
```

### Network Diagram for Testing

```
┌─────────────────────────────────────────────────────────────────────┐
│                        YOUR TEST NETWORK                            │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌─────────────────┐         ┌─────────────────┐                   │
│  │  Netily Server  │         │  MikroTik       │                   │
│  │  192.168.1.100  │◄───────►│  Router         │                   │
│  │                 │   LAN   │  192.168.1.1    │                   │
│  │  Django :8000   │         │                 │                   │
│  │  RADIUS :1812   │         │  Hotspot/PPPoE  │                   │
│  │  OpenVPN :1194  │         │  Server         │                   │
│  └─────────────────┘         └────────┬────────┘                   │
│                                       │                             │
│                                       │ WiFi/Ethernet               │
│                                       ▼                             │
│                              ┌─────────────────┐                   │
│                              │  Test Client    │                   │
│                              │  (Your Laptop)  │                   │
│                              └─────────────────┘                   │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Test Environment Setup

### Step 1: Start Netily Services

```powershell
# Navigate to project directory
cd C:\Users\peter.junior\OneDrive - Ramco Group LTD\Documents\GitHub\netily-fullstack\ISP_NEW

# Start all Docker services
docker-compose -f docker/docker-compose.yml up -d

# Verify services are running
docker-compose -f docker/docker-compose.yml ps

# Expected output:
# netily-db         running  5432/tcp
# netily-redis      running  6379/tcp
# netily-web        running  8000/tcp
# netily-freeradius running  1812/udp, 1813/udp
# netily-openvpn    running  1194/udp, 1194/tcp
```

### Step 2: Run Migrations (if not done)

```powershell
# Apply all migrations
python manage.py migrate

# Create superuser if needed
python manage.py createsuperuser
```

### Step 3: Note Your Server IP

```powershell
# Find your server's IP address
ipconfig

# Look for your LAN adapter's IPv4 Address
# Example: 192.168.1.100
```

**Important:** Replace `YOUR_SERVER_IP` in all examples below with your actual IP.

---

## Phase 1: Database & API Testing

### Test 1.1: Create Test Data via API

```powershell
# First, get an authentication token
$token = (Invoke-RestMethod -Uri "http://localhost:8000/api/v1/auth/token/" -Method POST -Body @{
    username = "admin"
    password = "your-password"
} -ContentType "application/x-www-form-urlencoded").access

# Set headers for subsequent requests
$headers = @{
    "Authorization" = "Bearer $token"
    "Content-Type" = "application/json"
}
```

### Test 1.2: Create a RADIUS User Manually

```powershell
# Create a test RADIUS user
$radiusUser = @{
    username = "testuser1"
    password = "Test@123"
    download_speed = 10000  # 10 Mbps
    upload_speed = 5000     # 5 Mbps
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://localhost:8000/api/v1/radius/users/" `
    -Method POST -Headers $headers -Body $radiusUser

# Expected Response:
# {
#     "username": "testuser1",
#     "status": "enabled",
#     "message": "RADIUS user created successfully"
# }
```

### Test 1.3: Verify Database Entries

```powershell
# Open Django shell
python manage.py shell
```

```python
# In Django shell
from apps.radius.models import RadCheck, RadReply

# Check if user was created
user = RadCheck.objects.filter(username='testuser1')
print(f"RadCheck entries: {user.count()}")
for entry in user:
    print(f"  {entry.attribute} {entry.op} {entry.value}")

# Check reply attributes (bandwidth)
replies = RadReply.objects.filter(username='testuser1')
print(f"\nRadReply entries: {replies.count()}")
for reply in replies:
    print(f"  {reply.attribute} {reply.op} {reply.value}")

# Expected output:
# RadCheck entries: 1
#   Cleartext-Password := Test@123
#
# RadReply entries: 1
#   Mikrotik-Rate-Limit := 5000k/10000k
```

### Test 1.4: Test RADIUS Dashboard API

```powershell
# Get RADIUS dashboard stats
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/radius/dashboard/" `
    -Method GET -Headers $headers

# Expected Response:
# {
#     "total_users": 1,
#     "active_users": 1,
#     "disabled_users": 0,
#     "active_sessions": 0,
#     ...
# }
```

---

## Phase 2: FreeRADIUS Testing

### Test 2.1: Test RADIUS Authentication Locally

```powershell
# SSH into the FreeRADIUS container
docker exec -it netily-freeradius /bin/bash
```

```bash
# Inside the container, test authentication
radtest testuser1 Test@123 localhost 0 testing123

# Expected output (if successful):
# Sent Access-Request Id 123 from 0.0.0.0:12345 to 127.0.0.1:1812 length 75
#   User-Name = "testuser1"
#   User-Password = "Test@123"
#   NAS-IP-Address = 127.0.0.1
#   NAS-Port = 0
# Received Access-Accept Id 123 from 127.0.0.1:1812 to 0.0.0.0:12345 length 40
#   Mikrotik-Rate-Limit = "5000k/10000k"
```

### Test 2.2: Test with Wrong Password

```bash
# Test with incorrect password (should fail)
radtest testuser1 wrongpassword localhost 0 testing123

# Expected output:
# Received Access-Reject Id 124 from 127.0.0.1:1812
```

### Test 2.3: Check FreeRADIUS Logs

```powershell
# View FreeRADIUS logs
docker logs netily-freeradius -f

# You should see authentication attempts logged
```

### Test 2.4: Test from External (Your Network)

```powershell
# From your Windows machine (outside container)
# First, install radclient or use online tools

# Or test via Python script
python -c "
import socket

# Simple RADIUS test
# Note: For proper testing, use a RADIUS client library
print('Testing RADIUS connectivity...')
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.settimeout(5)
try:
    sock.connect(('localhost', 1812))
    print('RADIUS port 1812 is accessible')
except Exception as e:
    print(f'Error: {e}')
finally:
    sock.close()
"
```

---

## Phase 3: MikroTik RADIUS Integration

### Test 3.1: Register Router as NAS

First, register your MikroTik router in Netily:

```powershell
# Create NAS entry for your router
$nasData = @{
    nasname = "192.168.1.1"           # Your MikroTik's IP
    shortname = "test-router"
    secret = "radiussecret123"         # Choose a strong secret
    type = "mikrotik"
    description = "Test MikroTik Router"
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://localhost:8000/api/v1/radius/nas/" `
    -Method POST -Headers $headers -Body $nasData

# Verify NAS was created
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/radius/nas/" `
    -Method GET -Headers $headers
```

### Test 3.2: Configure MikroTik for RADIUS

Connect to your MikroTik via Winbox or SSH and run these commands:

```routeros
# ============================================
# RADIUS SERVER CONFIGURATION
# ============================================

# Add RADIUS server
/radius add \
    service=hotspot,ppp,login \
    address=YOUR_SERVER_IP \
    secret=radiussecret123 \
    authentication-port=1812 \
    accounting-port=1813 \
    timeout=3000ms \
    comment="Netily RADIUS Server"

# Enable RADIUS for incoming connections
/radius incoming set accept=yes port=3799

# ============================================
# HOTSPOT CONFIGURATION (Option A)
# ============================================

# Create IP pool for hotspot users
/ip pool add name=hotspot-pool ranges=192.168.88.10-192.168.88.254

# Create hotspot profile with RADIUS
/ip hotspot profile add \
    name=hsprof-radius \
    hotspot-address=192.168.88.1 \
    dns-name=hotspot.local \
    use-radius=yes \
    radius-accounting=yes \
    radius-interim-update=5m

# Create hotspot server
/ip hotspot add \
    name=hotspot1 \
    interface=bridge \
    address-pool=hotspot-pool \
    profile=hsprof-radius \
    disabled=no

# ============================================
# PPPoE CONFIGURATION (Option B)
# ============================================

# Create IP pool for PPPoE users
/ip pool add name=pppoe-pool ranges=10.10.10.2-10.10.10.254

# Create PPPoE profile with RADIUS
/ppp profile add \
    name=pppoe-radius \
    local-address=10.10.10.1 \
    remote-address=pppoe-pool \
    use-radius=yes \
    only-one=yes

# Enable RADIUS authentication for PPP
/ppp aaa set use-radius=yes accounting=yes interim-update=5m

# Create PPPoE server
/interface pppoe-server server add \
    service-name=internet \
    interface=ether2 \
    default-profile=pppoe-radius \
    authentication=pap,chap,mschap1,mschap2 \
    disabled=no
```

### Test 3.3: Verify RADIUS Configuration on MikroTik

```routeros
# Check RADIUS server status
/radius print

# Expected output:
# Flags: X - disabled
#  #   SERVICE    CALLED-ID  DOMAIN  ADDRESS          SECRET
#  0   hotspot              YOUR_SERVER_IP  radiussecret123

# Check RADIUS connectivity
/radius monitor 0

# Expected output:
# status: connected
# requests: 0
# accepts: 0
# rejects: 0
# ...
```

### Test 3.4: Test Authentication from MikroTik

```routeros
# Test RADIUS authentication manually
/tool user-manager user add name=testuser1 password=Test@123

# Or use the test command (RouterOS 7.x)
/radius test-user username=testuser1 password=Test@123 server=0

# For Hotspot, try connecting with a device:
# 1. Connect to the hotspot WiFi
# 2. Open browser → redirected to login page
# 3. Enter: testuser1 / Test@123
# 4. Should authenticate and get 10Mbps/5Mbps speed
```

### Test 3.5: Monitor Active Sessions

After a user connects:

```routeros
# Check active hotspot users
/ip hotspot active print

# Expected output:
# # USER        ADDRESS       MAC-ADDRESS       UPTIME
# 0 testuser1   192.168.88.10 AA:BB:CC:DD:EE:FF 5m30s

# Check active PPPoE users
/ppp active print

# Expected output:
# # NAME        SERVICE  CALLER-ID  ADDRESS     UPTIME
# 0 testuser1   pppoe    AA:BB:CC  10.10.10.5  5m30s

# Check assigned rate-limit
/queue simple print

# Expected output:
# # NAME              TARGET            MAX-LIMIT
# 0 <hotspot-testuser1> 192.168.88.10/32  5M/10M
```

### Test 3.6: Verify Session in Netily

```powershell
# Check active sessions in Netily
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/radius/sessions/active/" `
    -Method GET -Headers $headers

# Expected Response:
# {
#     "count": 1,
#     "results": [
#         {
#             "username": "testuser1",
#             "nasipaddress": "192.168.1.1",
#             "framedipaddress": "192.168.88.10",
#             "acctstarttime": "2026-01-29T10:30:00Z",
#             "acctinputoctets": 1234567,
#             "acctoutputoctets": 9876543
#         }
#     ]
# }
```

---

## Phase 4: VPN Testing

### Test 4.1: Initialize OpenVPN Server

```powershell
# Initialize OpenVPN PKI (first time only)
docker exec -it netily-openvpn ovpn_genconfig -u udp://YOUR_SERVER_IP
docker exec -it netily-openvpn ovpn_initpki

# Follow prompts:
# - Enter PEM pass phrase (remember this!)
# - Common Name: Netily VPN CA
```

### Test 4.2: Generate Server Certificate via API

```powershell
# Create Certificate Authority
$caData = @{
    name = "Netily Root CA"
    organization = "Netily ISP"
    country = "KE"
    state = "Nairobi"
    valid_days = 3650
} | ConvertTo-Json

$ca = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/vpn/cas/" `
    -Method POST -Headers $headers -Body $caData

Write-Host "CA created with ID: $($ca.id)"
```

### Test 4.3: Generate Router VPN Certificate

```powershell
# First, ensure you have a router in the system
$routers = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/network/routers/" `
    -Method GET -Headers $headers

$routerId = $routers.results[0].id

# Generate VPN certificate for router
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/vpn/routers/$routerId/generate-certificate/" `
    -Method POST -Headers $headers

# Download the .ovpn config
Invoke-WebRequest -Uri "http://localhost:8000/api/v1/vpn/certificates/$routerId/download_config/" `
    -Headers $headers -OutFile "router-vpn.ovpn"

Write-Host "VPN config saved to router-vpn.ovpn"
```

### Test 4.4: Configure MikroTik OpenVPN Client

First, upload the certificates to MikroTik:

```routeros
# Upload via Winbox: Files → Upload
# - ca.crt
# - client.crt
# - client.key

# Import certificates
/certificate import file-name=ca.crt passphrase=""
/certificate import file-name=client.crt passphrase=""
/certificate import file-name=client.key passphrase=""

# Create OpenVPN client interface
/interface ovpn-client add \
    name=ovpn-netily \
    connect-to=YOUR_SERVER_IP \
    port=1194 \
    mode=ip \
    protocol=udp \
    user=router-name \
    certificate=client.crt_0 \
    verify-server-certificate=yes \
    auth=sha1 \
    cipher=aes256 \
    disabled=no

# Check connection status
/interface ovpn-client print

# Expected output:
# Flags: X - disabled, R - running
#  0  R name="ovpn-netily" ... status="connected"
```

### Test 4.5: Verify VPN Connectivity

```routeros
# Check assigned VPN IP
/ip address print where interface=ovpn-netily

# Expected output:
# # ADDRESS         NETWORK    INTERFACE
# 0 10.8.0.5/24     10.8.0.0   ovpn-netily

# Ping the VPN server
/ping 10.8.0.1 count=3

# Expected output:
# HOST                    SIZE  TTL TIME
# 10.8.0.1                56    64  5ms
# 10.8.0.1                56    64  4ms
# 10.8.0.1                56    64  5ms
```

### Test 4.6: Verify VPN Connection in Netily

```powershell
# Check VPN connection status
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/vpn/connections/active/" `
    -Method GET -Headers $headers

# Check specific router VPN status
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/vpn/routers/$routerId/status/" `
    -Method GET -Headers $headers

# Expected Response:
# {
#     "router_id": "uuid...",
#     "vpn_status": "connected",
#     "vpn_ip": "10.8.0.5",
#     "connected_since": "2026-01-29T10:00:00Z",
#     "bytes_in": 123456,
#     "bytes_out": 654321
# }
```

### Test 4.7: Test Remote API Access via VPN

```powershell
# From Netily server, connect to router via VPN IP
# Using RouterOS API (port 8728)

python -c "
import socket

# Test connection to router via VPN
vpn_ip = '10.8.0.5'
api_port = 8728

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.settimeout(5)
try:
    sock.connect((vpn_ip, api_port))
    print(f'Successfully connected to router at {vpn_ip}:{api_port}')
    print('RouterOS API is accessible via VPN!')
except Exception as e:
    print(f'Connection failed: {e}')
finally:
    sock.close()
"
```

---

## Phase 5: End-to-End Testing

### Test 5.1: Complete Customer Flow

This test simulates the full customer lifecycle:

```powershell
# ============================================
# STEP 1: Create Customer via API
# ============================================

$customerData = @{
    first_name = "John"
    last_name = "Doe"
    email = "john.doe@example.com"
    phone = "+254712345678"
    pppoe_username = "john.doe"
    pppoe_password = "SecurePass123"
    plan = "10mbps-plan-id"  # Replace with actual plan ID
} | ConvertTo-Json

$customer = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/customers/" `
    -Method POST -Headers $headers -Body $customerData

Write-Host "Customer created: $($customer.id)"
```

```powershell
# ============================================
# STEP 2: Verify RADIUS User Created
# ============================================

$radiusUser = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/radius/users/john.doe/" `
    -Method GET -Headers $headers

Write-Host "RADIUS user status: $($radiusUser.status)"
Write-Host "Bandwidth: $($radiusUser.download_speed)/$($radiusUser.upload_speed)"
```

```powershell
# ============================================
# STEP 3: Customer Connects (Manual Step)
# ============================================

Write-Host @"
Manual Step Required:
1. Connect a test device to the MikroTik hotspot/PPPoE
2. Enter credentials:
   Username: john.doe
   Password: SecurePass123
3. Verify internet access
4. Run a speed test to confirm 10Mbps limit
"@
```

```powershell
# ============================================
# STEP 4: Verify Session Created
# ============================================

Start-Sleep -Seconds 10  # Wait for session to register

$sessions = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/radius/sessions/active/" `
    -Method GET -Headers $headers

$userSession = $sessions.results | Where-Object { $_.username -eq "john.doe" }

if ($userSession) {
    Write-Host "Session active!"
    Write-Host "IP: $($userSession.framedipaddress)"
    Write-Host "Connected at: $($userSession.acctstarttime)"
} else {
    Write-Host "No active session found"
}
```

```powershell
# ============================================
# STEP 5: Simulate Invoice Overdue → Suspension
# ============================================

# Disable the user
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/radius/users/john.doe/disable/" `
    -Method POST -Headers $headers

Write-Host "User disabled - simulating overdue invoice"
Write-Host "User should be disconnected and unable to reconnect"

# Try to verify (user should be rejected now)
Start-Sleep -Seconds 5

# Re-enable after "payment"
Invoke-RestMethod -Uri "http://localhost:8000/api/v1/radius/users/john.doe/enable/" `
    -Method POST -Headers $headers

Write-Host "User re-enabled - simulating payment received"
```

```powershell
# ============================================
# STEP 6: Plan Upgrade
# ============================================

# Update user bandwidth
$updateData = @{
    download_speed = 50000  # Upgrade to 50 Mbps
    upload_speed = 25000
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://localhost:8000/api/v1/radius/users/john.doe/" `
    -Method PUT -Headers $headers -Body $updateData

Write-Host "User upgraded to 50Mbps"
Write-Host "Reconnect to apply new speed"
```

### Test 5.2: Router Remote Configuration Test

```powershell
# ============================================
# STEP 1: Verify Router VPN Connected
# ============================================

$routerStatus = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/vpn/routers/$routerId/status/" `
    -Method GET -Headers $headers

if ($routerStatus.vpn_status -eq "connected") {
    Write-Host "Router VPN connected at $($routerStatus.vpn_ip)"
} else {
    Write-Host "Router VPN not connected!"
    exit
}
```

```powershell
# ============================================
# STEP 2: Push Configuration to Router
# ============================================

# Apply a configuration template
$configData = @{
    template = "hotspot-radius"
    settings = @{
        radius_server = "10.8.0.1"
        radius_secret = "radiussecret123"
    }
} | ConvertTo-Json

Invoke-RestMethod -Uri "http://localhost:8000/api/v1/network/routers/$routerId/apply-config/" `
    -Method POST -Headers $headers -Body $configData

Write-Host "Configuration pushed to router via VPN"
```

### Test 5.3: Monitoring & Reporting Test

```powershell
# ============================================
# Get RADIUS Dashboard Statistics
# ============================================

$dashboard = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/radius/dashboard/" `
    -Method GET -Headers $headers

Write-Host "=== RADIUS Dashboard ==="
Write-Host "Total Users: $($dashboard.total_users)"
Write-Host "Active Sessions: $($dashboard.active_sessions)"
Write-Host "Auth Success (24h): $($dashboard.auth_success_24h)"
Write-Host "Auth Failed (24h): $($dashboard.auth_failed_24h)"
Write-Host "Total Traffic In: $($dashboard.total_traffic_in_mb) MB"
Write-Host "Total Traffic Out: $($dashboard.total_traffic_out_mb) MB"
```

```powershell
# ============================================
# Get VPN Dashboard Statistics
# ============================================

$vpnDashboard = Invoke-RestMethod -Uri "http://localhost:8000/api/v1/vpn/dashboard/" `
    -Method GET -Headers $headers

Write-Host "=== VPN Dashboard ==="
Write-Host "Total Certificates: $($vpnDashboard.total_certificates)"
Write-Host "Active Connections: $($vpnDashboard.active_connections)"
Write-Host "Total Traffic: $($vpnDashboard.total_traffic_in_mb + $vpnDashboard.total_traffic_out_mb) MB"
```

---

## Automated Test Scripts

### Complete Test Suite Script

Save this as `test_vpn_radius.ps1`:

```powershell
# ============================================
# Netily VPN & RADIUS Test Suite
# ============================================

param(
    [string]$ServerUrl = "http://localhost:8000",
    [string]$Username = "admin",
    [string]$Password = "admin",
    [string]$RouterIP = "192.168.1.1",
    [string]$RadiusSecret = "radiussecret123"
)

$ErrorActionPreference = "Stop"

# Colors for output
function Write-Success { param($msg) Write-Host "✅ $msg" -ForegroundColor Green }
function Write-Fail { param($msg) Write-Host "❌ $msg" -ForegroundColor Red }
function Write-Info { param($msg) Write-Host "ℹ️  $msg" -ForegroundColor Cyan }
function Write-Test { param($msg) Write-Host "`n🧪 $msg" -ForegroundColor Yellow }

# ============================================
# Get Auth Token
# ============================================
Write-Test "Authenticating..."

try {
    $token = (Invoke-RestMethod -Uri "$ServerUrl/api/v1/auth/token/" -Method POST -Body @{
        username = $Username
        password = $Password
    } -ContentType "application/x-www-form-urlencoded").access
    
    $headers = @{
        "Authorization" = "Bearer $token"
        "Content-Type" = "application/json"
    }
    Write-Success "Authentication successful"
} catch {
    Write-Fail "Authentication failed: $_"
    exit 1
}

# ============================================
# Test 1: RADIUS API
# ============================================
Write-Test "Testing RADIUS API..."

try {
    $dashboard = Invoke-RestMethod -Uri "$ServerUrl/api/v1/radius/dashboard/" `
        -Method GET -Headers $headers
    Write-Success "RADIUS dashboard accessible"
    Write-Info "Total users: $($dashboard.total_users)"
} catch {
    Write-Fail "RADIUS dashboard failed: $_"
}

# ============================================
# Test 2: Create Test User
# ============================================
Write-Test "Creating test RADIUS user..."

$testUsername = "testuser_$(Get-Random -Maximum 9999)"
$testPassword = "Test@$(Get-Random -Maximum 9999)"

try {
    $radiusUser = @{
        username = $testUsername
        password = $testPassword
        download_speed = 10000
        upload_speed = 5000
    } | ConvertTo-Json
    
    $result = Invoke-RestMethod -Uri "$ServerUrl/api/v1/radius/users/" `
        -Method POST -Headers $headers -Body $radiusUser
    
    Write-Success "User created: $testUsername"
    Write-Info "Password: $testPassword"
    Write-Info "Speed: 10Mbps/5Mbps"
} catch {
    Write-Fail "Failed to create user: $_"
}

# ============================================
# Test 3: Verify User
# ============================================
Write-Test "Verifying user in database..."

try {
    $user = Invoke-RestMethod -Uri "$ServerUrl/api/v1/radius/users/$testUsername/" `
        -Method GET -Headers $headers
    
    if ($user.username -eq $testUsername) {
        Write-Success "User verified in database"
    } else {
        Write-Fail "User not found"
    }
} catch {
    Write-Fail "Failed to verify user: $_"
}

# ============================================
# Test 4: Disable/Enable User
# ============================================
Write-Test "Testing user disable/enable..."

try {
    Invoke-RestMethod -Uri "$ServerUrl/api/v1/radius/users/$testUsername/disable/" `
        -Method POST -Headers $headers | Out-Null
    Write-Success "User disabled"
    
    Start-Sleep -Seconds 1
    
    Invoke-RestMethod -Uri "$ServerUrl/api/v1/radius/users/$testUsername/enable/" `
        -Method POST -Headers $headers | Out-Null
    Write-Success "User re-enabled"
} catch {
    Write-Fail "Disable/enable failed: $_"
}

# ============================================
# Test 5: VPN API
# ============================================
Write-Test "Testing VPN API..."

try {
    $vpnDashboard = Invoke-RestMethod -Uri "$ServerUrl/api/v1/vpn/dashboard/" `
        -Method GET -Headers $headers
    Write-Success "VPN dashboard accessible"
    Write-Info "Total certificates: $($vpnDashboard.total_certificates)"
    Write-Info "Active connections: $($vpnDashboard.active_connections)"
} catch {
    Write-Fail "VPN dashboard failed: $_"
}

# ============================================
# Test 6: NAS Registration
# ============================================
Write-Test "Testing NAS registration..."

try {
    $nasData = @{
        nasname = $RouterIP
        shortname = "test-router-$(Get-Random -Maximum 9999)"
        secret = $RadiusSecret
        type = "mikrotik"
    } | ConvertTo-Json
    
    $nas = Invoke-RestMethod -Uri "$ServerUrl/api/v1/radius/nas/" `
        -Method POST -Headers $headers -Body $nasData
    
    Write-Success "NAS registered: $($nas.shortname)"
} catch {
    if ($_.Exception.Response.StatusCode -eq 400) {
        Write-Info "NAS may already exist (this is OK)"
    } else {
        Write-Fail "NAS registration failed: $_"
    }
}

# ============================================
# Cleanup
# ============================================
Write-Test "Cleaning up test data..."

try {
    Invoke-RestMethod -Uri "$ServerUrl/api/v1/radius/users/$testUsername/" `
        -Method DELETE -Headers $headers | Out-Null
    Write-Success "Test user deleted"
} catch {
    Write-Info "Cleanup skipped or user already deleted"
}

# ============================================
# Summary
# ============================================
Write-Host "`n========================================" -ForegroundColor Cyan
Write-Host "       TEST SUITE COMPLETE" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host @"

Next Steps for Manual Testing:
1. Connect to MikroTik via Winbox
2. Configure RADIUS (see commands above)
3. Connect a test client to hotspot/PPPoE
4. Verify authentication and bandwidth
"@
```

### Running the Test Suite

```powershell
# Run with defaults
.\test_vpn_radius.ps1

# Run with custom parameters
.\test_vpn_radius.ps1 -ServerUrl "http://192.168.1.100:8000" `
    -Username "admin" `
    -Password "yourpassword" `
    -RouterIP "192.168.1.1" `
    -RadiusSecret "mysecret"
```

---

## Troubleshooting

### Issue: RADIUS Authentication Fails

```powershell
# Check 1: FreeRADIUS container is running
docker ps | Select-String "freeradius"

# Check 2: View FreeRADIUS logs
docker logs netily-freeradius --tail 50

# Check 3: Test locally in container
docker exec -it netily-freeradius radtest testuser1 Test@123 localhost 0 testing123

# Check 4: Verify database connection
docker exec -it netily-freeradius cat /etc/raddb/mods-enabled/sql
```

### Issue: MikroTik Can't Reach RADIUS

```routeros
# On MikroTik, test connectivity
/ping YOUR_SERVER_IP count=3

# Check if RADIUS port is open
/tool fetch url="http://YOUR_SERVER_IP:8000" mode=http

# Check firewall isn't blocking
/ip firewall filter print where chain=output
```

### Issue: VPN Connection Fails

```powershell
# Check OpenVPN container
docker logs netily-openvpn --tail 50

# Verify certificate files
docker exec -it netily-openvpn ls -la /etc/openvpn/pki/

# Check if port is listening
netstat -an | Select-String "1194"
```

### Issue: Speed Limiting Not Working

```routeros
# On MikroTik, check queue
/queue simple print

# If no queue, check RADIUS attributes received
/log print where topics~"radius"

# Verify attribute format
# Should be: Mikrotik-Rate-Limit = "5000k/10000k"
# Not: Mikrotik-Rate-Limit = "5M/10M"
```

### Debug Mode

```powershell
# Enable debug logging in Django
# In settings/local.py:
LOGGING['loggers']['apps.radius']['level'] = 'DEBUG'
LOGGING['loggers']['apps.vpn']['level'] = 'DEBUG'

# Run FreeRADIUS in debug mode
docker stop netily-freeradius
docker run -it --rm \
    --name freeradius-debug \
    -p 1812:1812/udp \
    -p 1813:1813/udp \
    freeradius/freeradius-server -X
```

---

## Test Checklist

Use this checklist to track your testing progress:

```markdown
## RADIUS Testing
- [ ] FreeRADIUS container running
- [ ] Can create RADIUS user via API
- [ ] Can authenticate locally (radtest)
- [ ] NAS registered in database
- [ ] MikroTik configured with RADIUS
- [ ] User can connect via Hotspot
- [ ] User can connect via PPPoE
- [ ] Bandwidth limiting works
- [ ] Session accounting recorded
- [ ] User disable blocks access
- [ ] User enable restores access

## VPN Testing
- [ ] OpenVPN container running
- [ ] CA certificate created
- [ ] Router certificate generated
- [ ] MikroTik OpenVPN client configured
- [ ] VPN tunnel established
- [ ] Router gets VPN IP (10.8.0.x)
- [ ] Can ping router via VPN
- [ ] Can access RouterOS API via VPN

## Integration Testing
- [ ] Customer creation syncs to RADIUS
- [ ] Plan change updates bandwidth
- [ ] Invoice overdue disables user
- [ ] Payment re-enables user
- [ ] Router config push via VPN works
```

---

## Next Steps

After successful testing:

1. **Document your configuration** - Save MikroTik commands used
2. **Create backup** - Export MikroTik and database
3. **Set up monitoring** - Add alerts for RADIUS/VPN issues
4. **Scale testing** - Test with multiple users and routers
5. **Production deployment** - Use production.py settings
