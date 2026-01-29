# VPN & RADIUS Architecture Guide

> **Netily ISP Management System**  
> **Document Version:** 1.0  
> **Last Updated:** January 2026

---

## Table of Contents

1. [Overview](#overview)
2. [Why VPN + RADIUS?](#why-vpn--radius)
3. [Architecture Diagram](#architecture-diagram)
4. [VPN System (OpenVPN)](#vpn-system-openvpn)
5. [RADIUS System (FreeRADIUS)](#radius-system-freeradius)
6. [System Coordination](#system-coordination)
7. [Data Flow Examples](#data-flow-examples)
8. [API Endpoints Reference](#api-endpoints-reference)
9. [Deployment Guide](#deployment-guide)
10. [Troubleshooting](#troubleshooting)

---

## Overview

The Netily ISP Management System uses **VPN** and **RADIUS** to solve two critical ISP challenges:

| Component | Purpose |
|-----------|---------|
| **VPN (OpenVPN)** | Secure remote management of customer routers (MikroTik) |
| **RADIUS (FreeRADIUS)** | Centralized authentication & bandwidth control for PPPoE/Hotspot users |

Together, they enable:
- ✅ Remote router configuration without public IPs
- ✅ Centralized user authentication across all routers
- ✅ Automatic bandwidth enforcement based on customer plans
- ✅ Real-time session monitoring and accounting
- ✅ Automatic suspension/activation based on billing status

---

## Why VPN + RADIUS?

### The Problem Without VPN

```
┌─────────────────┐                    ┌─────────────────┐
│  Netily Server  │       ❌           │  Customer       │
│  (Cloud/Office) │  ─── No Route ───► │  Router         │
│                 │                    │  (Private IP)   │
└─────────────────┘                    └─────────────────┘

Problem: Customer routers are behind NAT with private IPs.
         No way to push configurations or monitor remotely.
```

### The Solution With VPN

```
┌─────────────────┐                    ┌─────────────────┐
│  Netily Server  │                    │  Customer       │
│  + OpenVPN      │  ◄── VPN Tunnel ──►│  Router         │
│  (10.8.0.1)     │     (Encrypted)    │  (10.8.0.x)     │
└─────────────────┘                    └─────────────────┘

Solution: VPN creates a private network between server and all routers.
          Each router gets a predictable VPN IP for management.
```

### The Problem Without RADIUS

```
┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐
│  Router 1       │     │  Router 2       │     │  Router 3       │
│  - User A: 5Mb  │     │  - User A: 10Mb │     │  - User A: 5Mb  │
│  - User B: 10Mb │     │  - User C: 5Mb  │     │  - User D: 20Mb │
└─────────────────┘     └─────────────────┘     └─────────────────┘

Problem: Each router has its own user database.
         Changing a user's plan requires logging into each router.
         No centralized billing integration.
```

### The Solution With RADIUS

```
                      ┌─────────────────┐
                      │  FreeRADIUS     │
                      │  (Central DB)   │
                      │  - All Users    │
                      │  - All Plans    │
                      └────────┬────────┘
                               │
           ┌───────────────────┼───────────────────┐
           │                   │                   │
           ▼                   ▼                   ▼
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
│  Router 1       │  │  Router 2       │  │  Router 3       │
│  (Asks RADIUS)  │  │  (Asks RADIUS)  │  │  (Asks RADIUS)  │
└─────────────────┘  └─────────────────┘  └─────────────────┘

Solution: Single source of truth for all users.
          Change plan once → applied everywhere instantly.
          Billing system controls access directly.
```

---

## Architecture Diagram

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                           NETILY CLOUD INFRASTRUCTURE                         │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐    ┌─────────────┐   │
│  │   Next.js   │    │   Django    │    │  PostgreSQL │    │    Redis    │   │
│  │  Frontend   │◄──►│   Backend   │◄──►│   Database  │    │    Cache    │   │
│  │  (Port 3000)│    │  (Port 8000)│    │  (Port 5432)│    │ (Port 6379) │   │
│  └─────────────┘    └──────┬──────┘    └─────────────┘    └─────────────┘   │
│                            │                                                 │
│         ┌──────────────────┼──────────────────┐                             │
│         │                  │                  │                             │
│         ▼                  ▼                  ▼                             │
│  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐                      │
│  │   OpenVPN   │    │ FreeRADIUS  │    │   Celery    │                      │
│  │   Server    │    │   Server    │    │   Workers   │                      │
│  │ (Port 1194) │    │(Port 1812/13│    │             │                      │
│  └──────┬──────┘    └──────┬──────┘    └─────────────┘                      │
│         │                  │                                                 │
└─────────┼──────────────────┼────────────────────────────────────────────────┘
          │                  │
          │   VPN Tunnel     │   RADIUS Protocol
          │   (Encrypted)    │   (Auth/Acct)
          │                  │
┌─────────┼──────────────────┼────────────────────────────────────────────────┐
│         │                  │              CUSTOMER SITES                     │
├─────────┼──────────────────┼────────────────────────────────────────────────┤
│         ▼                  ▼                                                 │
│  ┌─────────────────────────────────┐                                        │
│  │         MikroTik Router         │                                        │
│  │  ┌───────────┐  ┌────────────┐  │     ┌─────────────┐                   │
│  │  │ VPN Client│  │PPPoE/Hotspot│◄─────►│  End Users  │                   │
│  │  │ (10.8.0.x)│  │   Server   │  │     │ (Customers) │                   │
│  │  └───────────┘  └────────────┘  │     └─────────────┘                   │
│  └─────────────────────────────────┘                                        │
│                                                                              │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## VPN System (OpenVPN)

### Purpose

The VPN system provides **secure remote access** to customer MikroTik routers that are behind NAT or dynamic IPs.

### Components

| Component | Location | Purpose |
|-----------|----------|---------|
| **OpenVPN Server** | Docker container | Central VPN hub |
| **VPN Client** | Each MikroTik router | Connects to server |
| **Certificate Authority** | Django app | Manages PKI |
| **VPN Certificates** | Django + Router | Authentication |

### Django Models

```python
# apps/vpn/models.py

CertificateAuthority
├── ca_certificate      # Root CA certificate (PEM)
├── ca_private_key      # CA private key (encrypted)
├── dh_parameters       # Diffie-Hellman params
├── tls_auth_key        # TLS-Auth key
└── crl                 # Certificate Revocation List

VPNCertificate
├── name                # Certificate identifier
├── certificate_type    # 'client' or 'server'
├── certificate         # X.509 certificate (PEM)
├── private_key         # Private key (encrypted)
├── serial_number       # Unique serial
├── status              # active/revoked/expired
├── valid_from/until    # Validity period
└── router              # FK to Router (for client certs)

VPNServer
├── name                # Server identifier
├── protocol            # UDP or TCP
├── port                # Default: 1194
├── network             # VPN subnet (10.8.0.0/24)
├── dns_servers         # DNS for clients
└── certificate         # FK to server certificate

VPNConnection
├── router              # FK to Router
├── certificate         # FK to VPNCertificate
├── vpn_ip              # Assigned VPN IP (10.8.0.x)
├── status              # connected/disconnected
├── connected_at        # Last connection time
├── bytes_in/out        # Traffic counters
└── last_seen           # Heartbeat timestamp
```

### How It Works

```
1. CERTIFICATE GENERATION
   ┌─────────────────┐
   │  Admin creates  │
   │  router in      │──► Django generates ──► Certificate stored
   │  Netily         │    client certificate   in database
   └─────────────────┘

2. ROUTER CONFIGURATION
   ┌─────────────────┐
   │  Admin clicks   │
   │  "Generate VPN  │──► MikroTik script ──► Router configures
   │  Config"        │    generated           OpenVPN client
   └─────────────────┘

3. VPN CONNECTION
   ┌─────────────────┐         ┌─────────────────┐
   │  Router boots   │         │  OpenVPN Server │
   │  and connects   │◄───────►│  authenticates  │
   │  via VPN        │         │  via certificate│
   └─────────────────┘         └─────────────────┘
                                       │
                                       ▼
                               Router gets VPN IP
                               (e.g., 10.8.0.5)

4. REMOTE MANAGEMENT
   ┌─────────────────┐         ┌─────────────────┐
   │  Django sends   │         │  Router receives│
   │  API command to │────────►│  command via    │
   │  10.8.0.5:8728  │         │  RouterOS API   │
   └─────────────────┘         └─────────────────┘
```

### VPN IP Assignment Strategy

```python
# Each router gets a predictable VPN IP based on its ID
def get_router_vpn_ip(router_id):
    # Router 1 → 10.8.0.2
    # Router 2 → 10.8.0.3
    # Router N → 10.8.0.(N+1)
    return f"10.8.0.{router_id + 1}"

# Server is always 10.8.0.1
```

---

## RADIUS System (FreeRADIUS)

### Purpose

The RADIUS system provides **centralized authentication, authorization, and accounting (AAA)** for all PPPoE/Hotspot users across all customer routers.

### Components

| Component | Location | Purpose |
|-----------|----------|---------|
| **FreeRADIUS Server** | Docker container | AAA server |
| **RADIUS Client** | Each MikroTik router | Sends auth requests |
| **RADIUS Database** | PostgreSQL (shared) | User credentials & policies |
| **Sync Service** | Django/Celery | Keeps RADIUS in sync with billing |

### Django Models (FreeRADIUS Schema)

```python
# apps/radius/models.py

# Authentication - What credentials are valid?
RadCheck
├── username            # PPPoE/Hotspot username
├── attribute           # e.g., "Cleartext-Password"
├── op                  # Operator: ":=", "==", etc.
└── value               # The password or value

# Authorization - What settings to apply?
RadReply
├── username            # User to apply to
├── attribute           # e.g., "Mikrotik-Rate-Limit"
├── op                  # Operator
└── value               # e.g., "5M/10M" (upload/download)

# Group-based policies
RadUserGroup
├── username            # User
├── groupname           # Group (e.g., "plan-10mbps")
└── priority            # Group priority

RadGroupReply
├── groupname           # Group name
├── attribute           # Applied to all group members
└── value               # Attribute value

# Session Accounting - Who is connected?
RadAcct
├── username            # Connected user
├── nasipaddress        # Router IP
├── acctstarttime       # Session start
├── acctstoptime        # Session end (null if active)
├── acctinputoctets     # Bytes downloaded
├── acctoutputoctets    # Bytes uploaded
├── framedipaddress     # Assigned IP
└── acctterminatecause  # Why session ended

# NAS Registration - Which routers can authenticate?
Nas
├── nasname             # Router IP or hostname
├── shortname           # Friendly name
├── secret              # RADIUS shared secret
└── type                # "mikrotik"

# Authentication Logs
RadPostAuth
├── username            # Who tried to authenticate
├── reply               # "Access-Accept" or "Access-Reject"
├── authdate            # When
└── nasipaddress        # Which router

# Netily Extension - Bandwidth profiles
RadiusBandwidthProfile
├── name                # Profile name (e.g., "10Mbps Plan")
├── download_speed      # Speed in Kbps
├── upload_speed        # Speed in Kbps
├── burst_download      # Burst speed
├── burst_upload        # Burst speed
└── billing_plan        # FK to BillingPlan
```

### How It Works

```
1. USER CREATION (via Netily)
   ┌─────────────────┐
   │  New customer   │
   │  subscribes to  │──► Django creates ──► RadCheck: username/password
   │  10Mbps plan    │    RADIUS entries    RadReply: rate-limit
   └─────────────────┘

2. USER CONNECTS (PPPoE/Hotspot)
   ┌─────────────────┐         ┌─────────────────┐
   │  Customer opens │         │  MikroTik       │
   │  laptop and     │────────►│  sends auth     │
   │  connects       │         │  to RADIUS      │
   └─────────────────┘         └────────┬────────┘
                                        │
                                        ▼
                               ┌─────────────────┐
                               │  FreeRADIUS     │
                               │  checks DB      │
                               │  - Password? ✓  │
                               │  - Active? ✓    │
                               │  - Paid? ✓      │
                               └────────┬────────┘
                                        │
                                        ▼
                               Access-Accept + Attributes:
                               - Mikrotik-Rate-Limit=5M/10M
                               - Framed-IP=192.168.1.100
                               - Session-Timeout=3600

3. SESSION ACCOUNTING
   ┌─────────────────┐         ┌─────────────────┐
   │  MikroTik sends │         │  FreeRADIUS     │
   │  Accounting     │────────►│  logs to        │
   │  Start/Update   │         │  RadAcct table  │
   └─────────────────┘         └─────────────────┘

4. AUTOMATIC SUSPENSION (Billing Integration)
   ┌─────────────────┐
   │  Invoice unpaid │
   │  for 7 days     │──► Celery task ──► RadCheck.value = "SUSPENDED"
   │                 │    disables user   User can't connect
   └─────────────────┘
```

### RADIUS Attributes for MikroTik

```python
# Common MikroTik RADIUS attributes

# Bandwidth Control
"Mikrotik-Rate-Limit" := "5M/10M"           # upload/download
"Mikrotik-Rate-Limit" := "5M/10M 10M/20M"   # with burst

# Address Assignment
"Framed-IP-Address" := "192.168.1.100"      # Static IP
"Framed-Pool" := "hotspot-pool"             # Dynamic from pool

# Session Control
"Session-Timeout" := "3600"                  # Max 1 hour
"Idle-Timeout" := "600"                      # Disconnect if idle 10min
"Acct-Interim-Interval" := "300"            # Update accounting every 5min

# Access Control
"Mikrotik-Wireless-Forward" := "no"         # Client isolation
"Mikrotik-Group" := "full-access"           # Firewall group
```

---

## System Coordination

### How Django Orchestrates Everything

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              DJANGO BACKEND                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐           │
│  │  apps/customers │   │  apps/billing   │   │  apps/network   │           │
│  │  - Customer     │   │  - Invoice      │   │  - Router       │           │
│  │  - Subscription │   │  - Payment      │   │  - RouterConfig │           │
│  └────────┬────────┘   └────────┬────────┘   └────────┬────────┘           │
│           │                     │                     │                     │
│           │         Django Signals & Celery Tasks     │                     │
│           │                     │                     │                     │
│           ▼                     ▼                     ▼                     │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                        COORDINATION LAYER                            │   │
│  │                                                                      │   │
│  │  on_customer_created ──────► Create RADIUS user                     │   │
│  │  on_plan_changed ──────────► Update RadReply rate-limit             │   │
│  │  on_invoice_overdue ───────► Disable RADIUS user                    │   │
│  │  on_payment_received ──────► Enable RADIUS user                     │   │
│  │  on_router_added ──────────► Generate VPN cert + Register NAS       │   │
│  │  on_router_removed ────────► Revoke VPN cert + Remove NAS           │   │
│  │                                                                      │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│           │                     │                     │                     │
│           ▼                     ▼                     ▼                     │
│  ┌─────────────────┐   ┌─────────────────┐   ┌─────────────────┐           │
│  │    apps/vpn     │   │   apps/radius   │   │  apps/network   │           │
│  │  - Certificates │   │  - RadCheck     │   │  - RouterOS API │           │
│  │  - Connections  │   │  - RadReply     │   │  - Config Push  │           │
│  └─────────────────┘   └─────────────────┘   └─────────────────┘           │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Signal-Based Coordination Example

```python
# apps/customers/signals.py

from django.db.models.signals import post_save
from django.dispatch import receiver
from apps.customers.models import Customer
from apps.radius.services import RadiusSyncService

@receiver(post_save, sender=Customer)
def sync_customer_to_radius(sender, instance, created, **kwargs):
    """When a customer is created or updated, sync to RADIUS."""
    if created:
        # Create RADIUS user for new customer
        RadiusSyncService.create_radius_user(
            username=instance.pppoe_username,
            password=instance.pppoe_password,
            customer=instance,
        )
    else:
        # Update existing RADIUS user
        RadiusSyncService.update_radius_user(
            username=instance.pppoe_username,
            customer=instance,
        )
```

```python
# apps/billing/signals.py

from django.db.models.signals import post_save
from django.dispatch import receiver
from apps.billing.models import Invoice
from apps.radius.services import RadiusSyncService

@receiver(post_save, sender=Invoice)
def handle_invoice_status_change(sender, instance, **kwargs):
    """When invoice goes overdue, disable RADIUS user."""
    if instance.status == 'overdue' and instance.days_overdue >= 7:
        RadiusSyncService.disable_radius_user(
            username=instance.customer.pppoe_username,
        )
    elif instance.status == 'paid':
        RadiusSyncService.enable_radius_user(
            username=instance.customer.pppoe_username,
        )
```

### Celery Tasks for Background Operations

```python
# apps/radius/tasks.py

from celery import shared_task
from apps.radius.services import RadiusSyncService

@shared_task
def sync_all_customers_to_radius():
    """Periodic task to ensure RADIUS is in sync."""
    RadiusSyncService.sync_all_customers()

@shared_task
def disconnect_overdue_users():
    """Disconnect users with overdue invoices."""
    from apps.billing.models import Invoice
    overdue = Invoice.objects.filter(
        status='overdue',
        days_overdue__gte=7,
    )
    for invoice in overdue:
        RadiusSyncService.disable_radius_user(
            username=invoice.customer.pppoe_username,
        )
```

---

## Data Flow Examples

### Example 1: New Customer Signup

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ 1. Customer signs up via Next.js frontend                                   │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 2. POST /api/v1/customers/                                                  │
│    Django creates Customer record                                           │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 3. Django Signal: post_save(Customer)                                       │
│    → RadiusSyncService.create_radius_user()                                │
│    → Creates RadCheck (password)                                           │
│    → Creates RadReply (rate-limit from plan)                               │
│    → Creates RadUserGroup (assigns to plan group)                          │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 4. Customer connects to Hotspot/PPPoE                                       │
│    → MikroTik sends Access-Request to FreeRADIUS                           │
│    → FreeRADIUS checks RadCheck → Password valid                           │
│    → FreeRADIUS returns RadReply attributes                                │
│    → Customer gets internet with correct speed                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Example 2: Plan Upgrade

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ 1. Admin upgrades customer from 10Mbps to 50Mbps via dashboard             │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 2. PUT /api/v1/customers/{id}/subscription/                                 │
│    Django updates Subscription.plan                                         │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 3. Django Signal: post_save(Subscription)                                   │
│    → RadiusSyncService.set_user_bandwidth(                                 │
│        username="customer1",                                                │
│        download_kbps=50000,                                                 │
│        upload_kbps=25000                                                    │
│      )                                                                      │
│    → Updates RadReply: Mikrotik-Rate-Limit = "25M/50M"                     │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 4. Next customer session gets new speed                                     │
│    (Or use CoA to update immediately - see Advanced section)               │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Example 3: Invoice Overdue → Suspension

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ 1. Celery Beat runs daily: check_overdue_invoices task                      │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 2. Task finds Invoice with days_overdue >= 7                                │
│    → RadiusSyncService.disable_radius_user(username)                       │
│    → Updates RadCheck: Auth-Type := Reject                                  │
│    → Optionally: Send CoA Disconnect to terminate active session           │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 3. Customer tries to connect                                                │
│    → FreeRADIUS returns Access-Reject                                       │
│    → Customer sees "Account suspended" message                              │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 4. Customer pays via M-Pesa                                                 │
│    → PayHero webhook → Django processes payment                            │
│    → Invoice.status = 'paid'                                                │
│    → Signal: RadiusSyncService.enable_radius_user(username)                │
│    → Customer can connect again                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Example 4: Remote Router Configuration

```
┌─────────────────────────────────────────────────────────────────────────────┐
│ 1. Admin adds new router via Next.js dashboard                              │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 2. POST /api/v1/network/routers/                                            │
│    Django creates Router record                                             │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 3. Django Signal: post_save(Router)                                         │
│    → CertificateService.generate_client_certificate(router)                │
│    → RadiusSyncService.register_nas(router)                                │
│    → VPNConnection.objects.create(router=router, vpn_ip="10.8.0.5")       │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 4. Admin downloads VPN config and uploads to router                         │
│    → Router connects to OpenVPN → Gets 10.8.0.5                            │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 5. Django can now push configurations:                                      │
│    → POST /api/v1/network/routers/{id}/apply-config/                       │
│    → Django connects to 10.8.0.5:8728 (RouterOS API)                       │
│    → Pushes hotspot config, RADIUS settings, firewall rules                │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## API Endpoints Reference

### VPN Endpoints (`/api/v1/vpn/`)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/dashboard/` | GET | VPN statistics overview |
| `/connections/active/` | GET | List active VPN connections |
| `/cas/` | GET, POST | Certificate Authority management |
| `/cas/{id}/` | GET, PUT, DELETE | CA CRUD |
| `/certificates/` | GET, POST | VPN certificate management |
| `/certificates/{id}/` | GET, PUT, DELETE | Certificate CRUD |
| `/certificates/{id}/revoke/` | POST | Revoke a certificate |
| `/certificates/{id}/download_config/` | GET | Download .ovpn file |
| `/servers/` | GET, POST | VPN server management |
| `/servers/{id}/` | GET, PUT, DELETE | Server CRUD |
| `/connections/` | GET | All VPN connections |
| `/routers/{id}/status/` | GET | Router VPN status |
| `/routers/{id}/generate-certificate/` | POST | Generate router certificate |

### RADIUS Endpoints (`/api/v1/radius/`)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/dashboard/` | GET | RADIUS statistics overview |
| `/sessions/active/` | GET | List active sessions |
| `/users/` | GET, POST | List/create RADIUS users |
| `/users/{username}/` | GET, PUT, DELETE | User CRUD |
| `/users/{username}/enable/` | POST | Enable user |
| `/users/{username}/disable/` | POST | Disable user |
| `/accounting/` | GET | Session accounting records |
| `/nas/` | GET, POST | NAS/router management |
| `/nas/{id}/` | GET, PUT, DELETE | NAS CRUD |
| `/profiles/` | GET, POST | Bandwidth profiles |
| `/profiles/{id}/` | GET, PUT, DELETE | Profile CRUD |
| `/auth-logs/` | GET | Authentication logs |
| `/sync/customers/` | POST | Sync all customers to RADIUS |
| `/sync/routers/` | POST | Sync all routers as NAS |
| `/sync/profiles/` | POST | Sync billing plans to profiles |

---

## Deployment Guide

### Docker Compose Services

```yaml
# docker/docker-compose.yml (relevant sections)

services:
  # OpenVPN Server
  openvpn:
    image: kylemanna/openvpn:latest
    container_name: netily-openvpn
    ports:
      - "1194:1194/udp"    # VPN connections
      - "1194:1194/tcp"    # Fallback TCP
      - "7505:7505"        # Management interface
    volumes:
      - openvpn_data:/etc/openvpn
      - openvpn_logs:/var/log/openvpn
    cap_add:
      - NET_ADMIN
    networks:
      - default
      - vpn_network

  # FreeRADIUS Server
  freeradius:
    image: freeradius/freeradius-server:latest
    container_name: netily-freeradius
    ports:
      - "1812:1812/udp"    # Authentication
      - "1813:1813/udp"    # Accounting
    volumes:
      - ./radius/raddb:/etc/raddb
      - ./radius/logs:/var/log/freeradius
    environment:
      - POSTGRES_HOST=db
      - POSTGRES_DB=netily_radius
      - POSTGRES_USER=netily
      - POSTGRES_PASSWORD=${DB_PASSWORD}
    depends_on:
      - db

networks:
  vpn_network:
    driver: bridge
    ipam:
      config:
        - subnet: 10.8.0.0/24
```

### FreeRADIUS SQL Configuration

```sql
-- FreeRADIUS SQL module configuration
-- /etc/raddb/mods-available/sql

sql {
    driver = "rlm_sql_postgresql"
    dialect = "postgresql"
    
    server = "db"
    port = 5432
    login = "netily"
    password = "${DB_PASSWORD}"
    
    radius_db = "netily"
    
    # Use Django-managed tables
    read_clients = yes
    client_table = "radius_nas"
    
    # Query configuration
    authcheck_table = "radius_radcheck"
    authreply_table = "radius_radreply"
    groupcheck_table = "radius_radgroupcheck"
    groupreply_table = "radius_radgroupreply"
    usergroup_table = "radius_radusergroup"
    
    acct_table1 = "radius_radacct"
    acct_table2 = "radius_radacct"
    
    postauth_table = "radius_radpostauth"
}
```

### Environment Variables

```bash
# .env

# VPN Configuration
VPN_NETWORK=10.8.0.0/24
VPN_SERVER_IP=10.8.0.1
OPENVPN_PROTOCOL=udp
OPENVPN_PORT=1194

# RADIUS Configuration
RADIUS_SECRET=your-secure-radius-secret
RADIUS_AUTH_PORT=1812
RADIUS_ACCT_PORT=1813

# Database (shared)
DB_HOST=db
DB_PORT=5432
DB_NAME=netily
DB_USER=netily
DB_PASSWORD=your-secure-db-password
```

---

## Troubleshooting

### VPN Issues

#### Router Can't Connect to VPN

```bash
# Check OpenVPN server logs
docker logs netily-openvpn

# Common issues:
# 1. Certificate expired → Regenerate certificate
# 2. Wrong server IP → Update client config
# 3. Firewall blocking → Check ports 1194 UDP/TCP
```

#### Router Connected but Django Can't Reach It

```bash
# Check VPN connection status
curl http://localhost:8000/api/v1/vpn/routers/{id}/status/

# Common issues:
# 1. Router not connected → Check VPN client on router
# 2. Wrong VPN IP → Verify VPN IP assignment
# 3. RouterOS API disabled → Enable API on router
```

### RADIUS Issues

#### User Can't Authenticate

```bash
# Check RADIUS logs
docker logs netily-freeradius

# Test authentication manually
radtest username password localhost 0 testing123

# Common issues:
# 1. User not in RadCheck → Sync customer to RADIUS
# 2. Wrong password → Check RadCheck.value
# 3. User disabled → Check for Auth-Type := Reject
```

#### User Authenticates but Gets Wrong Speed

```bash
# Check RadReply attributes
SELECT * FROM radius_radreply WHERE username = 'customer1';

# Common issues:
# 1. Missing Mikrotik-Rate-Limit → Run sync task
# 2. Wrong format → Should be "uploadK/downloadK"
# 3. Group override → Check RadGroupReply
```

#### Router Not Sending RADIUS Requests

```bash
# Check NAS registration
SELECT * FROM radius_nas WHERE nasname = 'router-ip';

# Common issues:
# 1. NAS not registered → Run router sync
# 2. Wrong secret → Update secret in router and NAS table
# 3. MikroTik not configured → Push RADIUS config to router
```

### Sync Issues

```python
# Manual sync via Django shell
python manage.py shell

from apps.radius.services import RadiusSyncService

# Sync single customer
RadiusSyncService.sync_customer(customer_id)

# Sync all customers
RadiusSyncService.sync_all_customers()

# Sync all routers as NAS
RadiusSyncService.sync_all_routers()
```

---

## Next Steps

### Week 3: Integration & Testing
- [ ] Add Django signals for automatic RADIUS sync
- [ ] Implement CoA (Change of Authorization) for instant updates
- [ ] Add VPN connection monitoring with Celery Beat
- [ ] Create integration tests for full flow

### Week 4: Advanced Features
- [ ] Implement RADIUS accounting reports
- [ ] Add bandwidth usage graphs from RadAcct
- [ ] Implement captive portal integration
- [ ] Add multi-router load balancing for VPN

---

## Questions?

Contact the backend team for:
- API endpoint clarifications
- Database schema questions
- Integration troubleshooting
- Performance optimization
