# Changelog

All notable changes to the Netily ISP Management System will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.0.0] - 2026-02-03

### Added

#### Backend - HotspotPlan Model Enhancement
- Added `validity_type` field (DAYS, HOURS, MINUTES, UNLIMITED)
- Added `validity_hours` and `validity_minutes` fields
- Added separate `download_speed` and `upload_speed` fields (replacing single speed_limit_mbps)
- Added `speed_unit` field (KBPS, MBPS, GBPS)
- Added `limitation_type` field (TIME_BASED, DATA_BASED, BOTH)
- Added `data_limit` and `data_unit` fields for data-based limits
- Added `simultaneous_devices` field
- Added `valid_days` JSONField (Mon-Sun checkboxes)
- Added `routers` ManyToMany field for router-specific plans
- Added `is_active` and `is_public` flags
- Migration: `bandwidth/0006_*.py`

#### Backend - Plan Model (Already Enhanced)
- `validity_type`, `validity_hours`, `validity_minutes` fields
- Burst settings: `burst_download`, `burst_upload`, `burst_threshold`, `burst_time`
- FUP settings: `fup_limit`, `fup_speed`
- Session control: `max_sessions`, `session_timeout`

#### Backend - RADIUS Integration
- Celery task for disconnecting expired hotspot users
- Automatic RADIUS disconnect via CoA packets
- Hotspot user expiry tracking

#### Frontend - Enhanced Plan Creation Dialogs
- Quick Create presets (30min, 1hr, 3hr, etc.) for Hotspot plans
- Sectioned dialog layout (Validity, Speed, Data Limit, Session & Burst, FUP)
- Validity type selector with dynamic input fields
- Separate download/upload speed inputs with unit selector
- Unlimited data toggle
- Valid days checkboxes (Mon-Sun)
- Router multi-select for hotspot plans
- FUP (Fair Usage Policy) settings section
- Burst settings configuration

#### Documentation
- Added comprehensive `DEVELOPER_SETUP.md`
- Added `start_backend.ps1` quick start script
- Added `start_frontend.ps1` quick start script
- Updated `README.md` with project overview

### Fixed
- Fixed hotspot wizard Step 2 field references (duration_minutes → validity_value)
- Fixed network_router table not found error (migration timing issue)

### Technical Notes

#### Migrations to Apply
```powershell
# For new installations or updates
python manage.py migrate_schemas --shared
python manage.py migrate_schemas --tenant
```

#### Key Migration Files
| App | Migration | Description |
|-----|-----------|-------------|
| bandwidth | 0006_* | HotspotPlan 17 new fields |
| billing | 0003_* | Plan burst/FUP settings |
| network | 0004_* | HotspotUser improvements |

---

## [1.0.0] - 2026-01-15

### Added
- Initial release
- Multi-tenant architecture with django-tenants
- Customer management
- Billing with M-Pesa integration
- Network management (Routers, IP Pools, VLANs)
- PPPoE user management
- Basic hotspot support
- Support ticketing system
- Staff management
- Analytics and reporting
