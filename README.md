# Netily ISP Management System

A comprehensive multi-tenant ISP management system for the Kenyan market.

## Features
- 🏢 Multi-tenant architecture (schema-based isolation)
- 👥 Customer Management with self-service portal
- 💳 Billing & M-Pesa Integration
- 🌐 Network Management (OLT, TR-069, Mikrotik, RADIUS)
- 📡 PPPoE and Hotspot user management
- 📊 Bandwidth Monitoring & Analytics
- 🎫 Support Ticketing
- 📈 Analytics & Reporting

## Documentation

📖 **For detailed setup instructions, see [DEVELOPER_SETUP.md](DEVELOPER_SETUP.md)**

## Quick Start

### Backend (Django)
```powershell
# Option 1: Use the quick start script
.\start_backend.ps1

# Option 2: Manual setup
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements/local.txt
python manage.py migrate_schemas --shared
python manage.py migrate_schemas --tenant
python manage.py runserver
```

### Frontend (Next.js)
```powershell
cd ..\netily
.\start_frontend.ps1
# Or: npm install && npm run dev
```

## Tech Stack

| Layer | Technology |
|-------|------------|
| Backend | Django 5.x, Django REST Framework |
| Frontend | Next.js 14+, TypeScript, shadcn/ui |
| Database | PostgreSQL with django-tenants |
| Task Queue | Celery + Redis |
| Auth | JWT (djangorestframework-simplejwt) |
| RADIUS | FreeRADIUS (Docker containers) |

## Project Structure

```
ISP_NEW/           # Django Backend
├── apps/          # Django applications
├── config/        # Django settings
├── requirements/  # Python dependencies
└── manage.py

netily/            # Next.js Frontend
├── app/           # Next.js pages
├── components/    # React components
└── lib/           # Utilities & types
```

## Environment Variables

See `.env.example` or [DEVELOPER_SETUP.md](DEVELOPER_SETUP.md) for required environment variables.

## License

Proprietary - Ramco Group LTD