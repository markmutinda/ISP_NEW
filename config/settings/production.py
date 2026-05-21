"""
Production settings — DigitalOcean / Railway / any Docker host.
"""
import dj_database_url
from .base import *

# ────────────────────────────────────────────────────────────────
#  CORE
# ────────────────────────────────────────────────────────────────
DEBUG = False
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', os.environ.get('SECRET_KEY'))

def _csv_env(name):
    value = os.environ.get(name, '')
    return [item.strip() for item in value.split(',') if item.strip()]


_default_hosts = [
    'api.netily.co.ke',
    '.netily.co.ke',
    'localhost',
    '127.0.0.1',
    'web',
    'backend',
    'api',
]
_env_hosts = _csv_env('DJANGO_ALLOWED_HOSTS')

# Common deployment env names used across DO/nginx/compose setups.
_domain = os.environ.get('DOMAIN', '').strip()
_droplet_ip = os.environ.get('DROPLET_IP', '').strip()
_public_ip = os.environ.get('PUBLIC_IP', '').strip()
_server_ip = os.environ.get('SERVER_IP', '').strip()

ALLOWED_HOSTS = _env_hosts or _default_hosts
for host in [_domain, _droplet_ip, _public_ip, _server_ip]:
    if host and host not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(host)

# ────────────────────────────────────────────────────────────────
#  DATABASE
#  • Railway / Render → provides DATABASE_URL (auto-parsed)
#  • DigitalOcean Docker → uses DB_HOST / DB_NAME env vars
#    which are already read by base.py, so we only override
#    when DATABASE_URL is explicitly set.
# ────────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL:
    DATABASES = {
        'default': dj_database_url.config(
            default=DATABASE_URL,
            conn_max_age=600,
            conn_health_checks=True,
            engine='django_tenants.postgresql_backend',
        )
    }

# ────────────────────────────────────────────────────────────────
#  REDIS & CELERY — Railway provides REDIS_URL automatically
# ────────────────────────────────────────────────────────────────
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = 'django-db'

# ────────────────────────────────────────────────────────────────
#  CACHE — Redis (required for OTP, rate-limiting, etc.)
# ────────────────────────────────────────────────────────────────
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.redis.RedisCache',
        'LOCATION': REDIS_URL,
    }
}

# ────────────────────────────────────────────────────────────────
#  STATIC FILES — WhiteNoise (no nginx on Railway)
# ────────────────────────────────────────────────────────────────
STATICFILES_STORAGE = 'whitenoise.storage.CompressedManifestStaticFilesStorage'
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')

# Insert WhiteNoise right after SecurityMiddleware
_security_idx = next(
    (i for i, m in enumerate(MIDDLEWARE) if 'SecurityMiddleware' in m), None
)
if _security_idx is not None:
    MIDDLEWARE.insert(_security_idx + 1, 'whitenoise.middleware.WhiteNoiseMiddleware')
else:
    MIDDLEWARE.insert(0, 'whitenoise.middleware.WhiteNoiseMiddleware')

# ────────────────────────────────────────────────────────────────
#  CORS — driven by CORS_ALLOWED_ORIGINS env var + safe defaults
# ────────────────────────────────────────────────────────────────
_extra_origins = os.environ.get('CORS_ALLOWED_ORIGINS', '')
CORS_ALLOWED_ORIGINS = [
    'https://netily.vercel.app',
    'https://www.netily.vercel.app',
    'https://netily.co.ke',
    'https://www.netily.co.ke',
]
if _extra_origins:
    CORS_ALLOWED_ORIGINS += [o.strip() for o in _extra_origins.split(',') if o.strip()]

# Base domain used for new tenant subdomains (e.g. "acme.netily.co.ke")
TENANT_BASE_DOMAIN = os.environ.get('TENANT_BASE_DOMAIN', 'netily.co.ke')

# Also allow any *.vercel.app preview deployments and *.netily.co.ke tenant subdomains
CORS_ALLOWED_ORIGIN_REGEXES = [
    r'^https://.*\.vercel\.app$',
    r'^https://.*\.netily\.co\.ke$',
]

CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_ALL_ORIGINS = False

# CSRF trusted origins (for cookies / sessions across domains)
CSRF_TRUSTED_ORIGINS = [
    'https://netily.vercel.app',
    'https://*.vercel.app',
    'https://*.railway.app',
    'https://netily.co.ke',
    'https://*.netily.co.ke',
    'https://api.netily.co.ke',
]
# Add the DO domain if set
_domain = os.environ.get('DOMAIN', '')
if _domain:
    CSRF_TRUSTED_ORIGINS += [f'https://{_domain}', f'https://*.{_domain}', f'http://{_domain}']

# Also trust the raw droplet IP for initial setup before DNS
_droplet_ip = os.environ.get('DROPLET_IP', '')
if _droplet_ip:
    CSRF_TRUSTED_ORIGINS.append(f'http://{_droplet_ip}')

# ────────────────────────────────────────────────────────────────
#  SECURITY — relax SSL redirect when behind nginx on plain HTTP
# ────────────────────────────────────────────────────────────────
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = os.environ.get('SECURE_SSL_REDIRECT', 'False') == 'True'
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
SECURE_HSTS_SECONDS = 31536000       # 1 year
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True

# ────────────────────────────────────────────────────────────────
#  BASE URL for webhooks / callbacks
# ────────────────────────────────────────────────────────────────
RAILWAY_PUBLIC_DOMAIN = os.environ.get('RAILWAY_PUBLIC_DOMAIN', '')
if RAILWAY_PUBLIC_DOMAIN:
    BASE_URL = f"https://{RAILWAY_PUBLIC_DOMAIN}"
elif _domain:
    BASE_URL = f"https://{_domain}"
elif _droplet_ip:
    BASE_URL = f"http://{_droplet_ip}"

# Frontend URL (Next.js) — override for production
FRONTEND_URL = os.environ.get('FRONTEND_URL', 'https://netily.co.ke')

# Tuma payment gateway callback URL
TUMA_CALLBACK_URL = os.environ.get(
    'TUMA_CALLBACK_URL',
    'https://api.netily.co.ke/api/v1/webhooks/tuma/callback/',
)

# ────────────────────────────────────────────────────────────────
#  LOGGING — reduced verbosity for production VPS
# ────────────────────────────────────────────────────────────────
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'production': {
            'format': '[{levelname}] {asctime} {name} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'production',
        },
    },
    'root': {
        'handlers': ['console'],
        'level': 'WARNING',  # Changed from INFO to WARNING
    },
    'loggers': {
        'django': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        'django.request': {'handlers': ['console'], 'level': 'ERROR', 'propagate': False},
        'apps': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},  # Changed from INFO to WARNING
        'apps.core.views': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
    },
}

# ────────────────────────────────────────────────────────────────
#  REMOVE DEBUG TOOLBAR
# ────────────────────────────────────────────────────────────────
if 'debug_toolbar' in INSTALLED_APPS:
    INSTALLED_APPS.remove('debug_toolbar')

if 'debug_toolbar.middleware.DebugToolbarMiddleware' in MIDDLEWARE:
    MIDDLEWARE.remove('debug_toolbar.middleware.DebugToolbarMiddleware')
