# Security & Environment Configuration Guide

## 🚨 Problem: Credentials in Git

Sensitive credentials (database passwords, API keys, secrets) should **NEVER** be committed to git. This guide explains how to properly handle secrets.

---

## ✅ Solution: Environment Variables + .gitignore

### 1. Files That Should NEVER Be Committed

| File | Purpose | Status |
|------|---------|--------|
| `.env` | Local development credentials | ❌ gitignored |
| `docker/.env` | Docker deployment credentials | ❌ gitignored |
| `docker-compose.override.yml` | Local Docker overrides | ❌ gitignored |
| `config/settings/local.py` | Local Django settings | ❌ gitignored |

### 2. Template Files That SHOULD Be Committed

| File | Purpose | Status |
|------|---------|--------|
| `.env.example` | Template with placeholder values | ✅ tracked |
| `docker/.env.example` | Docker template | ✅ tracked |
| `docker-compose.yml` | Base Docker config (uses env vars) | ✅ tracked |

---

## 🔧 Setup Instructions for New Developers

### Backend Setup

```bash
# 1. Clone the repository
git clone https://github.com/your-org/netily-fullstack.git
cd netily-fullstack/ISP_NEW

# 2. Copy environment template
cp .env.example .env

# 3. Edit .env with your credentials
# Open .env and replace all CHANGE_ME values

# 4. Create virtual environment and install dependencies
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux/Mac
pip install -r requirements/local.txt

# 5. Run migrations
python manage.py migrate
```

### Docker Setup

```bash
# 1. Copy Docker environment template
cp docker/.env.example docker/.env

# 2. Edit docker/.env with your credentials
# Replace all CHANGE_ME values

# 3. Start services
cd docker
docker-compose up -d
```

---

## 📁 Updated .gitignore

The following patterns are now in `.gitignore`:

```gitignore
# Environment (NEVER commit these!)
.env
.env.local
.env.*.local
.env.production
.env.staging

# Docker override files with credentials
docker-compose.override.yml
docker-compose.local.yml
docker-compose.prod.yml
docker/.env
docker/*.env

# Local settings with credentials
config/settings/local.py
**/local_settings.py
```

---

## 🔐 Environment Variables Reference

### Required Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `SECRET_KEY` | Django secret key (50+ chars) | Generate with Django utility |
| `DB_PASSWORD` | PostgreSQL password | `str0ng_p@ssw0rd!` |
| `PAYHERO_API_USERNAME` | PayHero API username | From PayHero dashboard |
| `PAYHERO_API_PASSWORD` | PayHero API password | From PayHero dashboard |
| `PAYHERO_CHANNEL_ID` | PayHero M-Pesa channel | From PayHero dashboard |

### Optional Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `DEBUG` | Django debug mode | `False` |
| `DB_HOST` | Database host | `localhost` |
| `DB_PORT` | Database port | `5432` |
| `REDIS_URL` | Redis connection URL | `redis://localhost:6379/0` |

---

## 🛠️ Generating Secure Values

### Django Secret Key
```python
python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
```

### Random Password (PowerShell)
```powershell
-join ((65..90) + (97..122) + (48..57) | Get-Random -Count 24 | ForEach-Object {[char]$_})
```

### Random Password (Bash)
```bash
openssl rand -base64 24
```

---

## ⚠️ If Credentials Were Already Committed

If you've already committed credentials, you need to:

### 1. Change All Exposed Credentials Immediately
- Database passwords
- API keys  
- Secret keys

### 2. Remove from Git History (Optional but Recommended)

```bash
# Install BFG Repo-Cleaner
# https://rtyley.github.io/bfg-repo-cleaner/

# Remove .env files from history
bfg --delete-files .env

# Or use git filter-branch (slower)
git filter-branch --force --index-filter \
  "git rm --cached --ignore-unmatch .env" \
  --prune-empty --tag-name-filter cat -- --all

# Force push
git push origin --force --all
```

### 3. Rotate All Credentials
Even after removing from history, assume they're compromised:
- Generate new `SECRET_KEY`
- Change database password
- Regenerate PayHero API credentials
- Update any other API keys

---

## 🔄 How Docker Compose Uses Environment Variables

The `docker-compose.yml` uses `${VARIABLE}` syntax to read from `docker/.env`:

```yaml
services:
  db:
    environment:
      - POSTGRES_PASSWORD=${DB_PASSWORD}  # Reads from .env
  
  web:
    environment:
      - DATABASE_URL=postgres://netily:${DB_PASSWORD}@db:5432/netily_isp
```

This means:
1. Credentials are NOT hardcoded in docker-compose.yml
2. Each developer/server has their own `docker/.env` file
3. The `.env` file is gitignored, so credentials are never committed

---

## 📋 Pre-Commit Checklist

Before every commit, verify:

- [ ] No `.env` files in the commit (`git status` should not show any)
- [ ] No hardcoded passwords in Python code (search for `password =` or `PASSWORD =`)
- [ ] All credentials use `os.environ.get()` in Django settings
- [ ] `.env.example` has only placeholder values (CHANGE_ME)
- [ ] Docker compose uses `${VARIABLE}` syntax (not hardcoded values)
- [ ] `config/settings/local.py` is gitignored

---

## 🔍 Quick Audit Commands

### Check for Hardcoded Credentials in Staged Files
```bash
git diff --cached | grep -iE "(password|secret|key|token)\s*=\s*['\"][^'\"]+['\"]"
```

### Find All Environment Variable Usage
```bash
grep -r "os.environ" config/settings/
```

### Verify .env is Not Tracked
```bash
git ls-files | grep "\.env$"
# Should return ONLY .env.example files
```

---

## 📚 Files Changed in This Update

| File | Change |
|------|--------|
| [.env.example](.env.example) | Cleaned up - all real values replaced with CHANGE_ME |
| [.gitignore](.gitignore) | Added Docker and local settings patterns |
| [docker/.env.example](docker/.env.example) | **NEW** - Docker environment template |
| [docs/ENVIRONMENT_SECURITY.md](docs/ENVIRONMENT_SECURITY.md) | **NEW** - This document |

---

*Last Updated: February 1, 2026*
