"""
Production settings — Railway + Vercel deployment.
"""
import dj_database_url
from .base import *

# ────────────────────────────────────────────────────────────────
#  CORE
# ────────────────────────────────────────────────────────────────
DEBUG = False
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', os.environ.get('SECRET_KEY'))

ALLOWED_HOSTS = os.environ.get(
    'DJANGO_ALLOWED_HOSTS',
    '.railway.app,localhost'
).split(',')

# ────────────────────────────────────────────────────────────────
#  DATABASE — Railway provides DATABASE_URL automatically
#  Uses django-tenants backend for multi-tenant support
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
#  CORS — Allow Vercel frontend
# ────────────────────────────────────────────────────────────────
CORS_ALLOWED_ORIGINS = [
    'https://netily.vercel.app',
    'https://www.netily.vercel.app',
]

# Also allow any *.vercel.app preview deployments
CORS_ALLOWED_ORIGIN_REGEXES = [
    r'^https://.*\.vercel\.app$',
]

CORS_ALLOW_CREDENTIALS = True
CORS_ALLOW_ALL_ORIGINS = False

# CSRF trusted origins (for cookies / sessions across domains)
CSRF_TRUSTED_ORIGINS = [
    'https://netily.vercel.app',
    'https://*.vercel.app',
    'https://*.railway.app',
]

# ────────────────────────────────────────────────────────────────
#  SECURITY
# ────────────────────────────────────────────────────────────────
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
SECURE_SSL_REDIRECT = os.environ.get('SECURE_SSL_REDIRECT', 'True') == 'True'
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

# ────────────────────────────────────────────────────────────────
#  LOGGING — structured for Railway log drain
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
        'level': 'INFO',
    },
    'loggers': {
        'django': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        'django.request': {'handlers': ['console'], 'level': 'ERROR', 'propagate': False},
        'apps': {'handlers': ['console'], 'level': 'INFO', 'propagate': False},
    },
}

# ────────────────────────────────────────────────────────────────
#  REMOVE DEBUG TOOLBAR
# ────────────────────────────────────────────────────────────────
if 'debug_toolbar' in INSTALLED_APPS:
    INSTALLED_APPS.remove('debug_toolbar')

if 'debug_toolbar.middleware.DebugToolbarMiddleware' in MIDDLEWARE:
    MIDDLEWARE.remove('debug_toolbar.middleware.DebugToolbarMiddleware')