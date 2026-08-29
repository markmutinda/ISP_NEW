"""
Django settings for ISP Management System - Base Configuration
"""

import os
from corsheaders.defaults import default_headers
from pathlib import Path
from datetime import timedelta
import sys

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent
RUNNING_TESTS = 'test' in sys.argv

# Add apps directory to Python path
sys.path.insert(0, os.path.join(BASE_DIR, 'apps'))

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = os.environ.get('SECRET_KEY', 'django-insecure-development-key-change-in-production')

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = True

# For ngrok testing
NGROK_URL = "https://camden-convocative-oversorrowfully.ngrok-free.dev"
BASE_URL = NGROK_URL  # Use ngrok as base URL

ALLOWED_HOSTS = ['localhost', '127.0.0.1', '.localhost','camden-convocative-oversorrowfully.ngrok-free.dev','*.ngrok-free.dev', '*.ngrok.io',]

# For ngrok, we need to handle HTTPS/SSL
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
USE_X_FORWARDED_HOST = True
USE_X_FORWARDED_PORT = True

# ────────────────────────────────────────────────────────────────
#  BASE + THIRD-PARTY + DJANGO CONTRIB APPS (these must always exist)
# ────────────────────────────────────────────────────────────────
BASE_APPS = [                     
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'django_extensions',
    'rest_framework',
    'rest_framework_simplejwt',
    'corsheaders',
    'django_filters',
    'drf_yasg',
]

# ────────────────────────────────────────────────────────────────
#  🚨 FIXED: Celery Apps moved to SHARED_APPS (not BASE_APPS)
#  This ensures they exist in the public schema and can access
#  the database schedule table without schema confusion
# ────────────────────────────────────────────────────────────────
try:
    import celery
    # Celery apps are added to SHARED_APPS so they live in the public schema
    # This prevents the "relation django_celery_beat_periodictask does not exist" error
    CELERY_APPS = (
        'django_celery_beat',      # Celery Beat scheduler (periodic tasks)
        'django_celery_results',   # Celery task results backend
    )
except ImportError:
    CELERY_APPS = ()
    pass  # Celery not installed (local development)

# ────────────────────────────────────────────────────────────────
#  🚨🚨🚨 FIXED: SHARED vs TENANT APPS (CRITICAL FIX)
# ────────────────────────────────────────────────────────────────
SHARED_APPS = (
    'django_tenants',                  # Must be first
    'django.contrib.contenttypes',     # MUST be in BOTH shared and tenant
    'django.contrib.auth',             # SHOULD be in both
    'django.contrib.sessions',         # SHOULD be in both
    'django.contrib.messages',         # SHOULD be in both
    'django.contrib.admin',           # SHOULD be in both
    'apps.core',                       # Tenant & Domain models go here - MUST be in BOTH
    'apps.subscriptions',              # Netily platform subscriptions (public schema only)
    'apps.superadmin',
    'apps.affiliate',                  # Platform affiliate accounts and attribution (public schema)
    # NOTE: token_blacklist is intentionally NOT in SHARED_APPS.
    # It must live only in TENANT_APPS so each tenant schema has its own
    # token_blacklist_outstandingtoken table with an FK that correctly
    # references that tenant's core_user table (not the public schema's).
    # Having it in SHARED_APPS causes FK violations on login because
    # tenant user IDs don't exist in the public core_user table.
) + CELERY_APPS  # Add Celery apps to SHARED_APPS

TENANT_APPS = (
    'django.contrib.contenttypes',     # 🚨 MUST BE HERE TOO
    'django.contrib.auth',             # 🚨 MUST BE HERE TOO
    'django.contrib.sessions',         # 🚨 MUST BE HERE TOO
    'django.contrib.messages',         # 🚨 MUST BE HERE TOO
    'django.contrib.admin',           # 🚨 MUST BE HERE TOO
    'apps.core',                       # 🚨 MUST BE HERE TOO - THIS IS CRITICAL!
    'apps.customers',
    'apps.messaging',
    'apps.network',
    'apps.fiber_map',
    'apps.billing',
    'apps.support',
    'apps.analytics',
    'apps.staff',
    'apps.self_service',
    'apps.inventory',
    'apps.notifications',
    'apps.bandwidth',
    'apps.vpn',                        # VPN/WireGuard Management
    'apps.radius',                     # RADIUS/FreeRADIUS Integration
    'apps.fup',
    'apps.loyalty',                    # Loyalty/Rewards Program
    'rest_framework_simplejwt.token_blacklist', # 🚨 MUST BE HERE TOO so tenants have their own token tables
)

# ────────────────────────────────────────────────────────────────
#  FINAL INSTALLED_APPS — merge everything without duplicates
# ────────────────────────────────────────────────────────────────
INSTALLED_APPS = list(SHARED_APPS) + [app for app in BASE_APPS if app not in SHARED_APPS] + \
                 [app for app in TENANT_APPS if app not in SHARED_APPS and app not in BASE_APPS]

# Remove duplicates while preserving order
INSTALLED_APPS = list(dict.fromkeys(INSTALLED_APPS))

# Debug toolbar only in DEBUG mode
if DEBUG and not RUNNING_TESTS:
    INSTALLED_APPS.append('debug_toolbar')

# ────────────────────────────────────────────────────────────────
#  MIDDLEWARE — CorsMiddleware MUST be before anything that might return a response
# ────────────────────────────────────────────────────────────────
MIDDLEWARE = [
    'corsheaders.middleware.CorsMiddleware',
    'apps.core.middleware.TenantMainMiddleware',
    'apps.core.middleware.DemoModeReadOnlyMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'apps.core.middleware.AuditLogMiddleware',
    'apps.core.middleware.CompanyContextMiddleware',
    'apps.core.middleware.SubscriptionEnforcementMiddleware',
    # REMOVED: 'apps.core.middleware.TenantMiddleware',
]
if DEBUG and not RUNNING_TESTS:
    MIDDLEWARE.insert(3, 'debug_toolbar.middleware.DebugToolbarMiddleware')  # After CORS middlewares
    INTERNAL_IPS = ['127.0.0.1', 'localhost', 'camden-convocative-oversorrowfully.ngrok-free.dev']
    DEBUG_TOOLBAR_CONFIG = {'SHOW_TOOLBAR_CALLBACK': lambda request: DEBUG}

# ────────────────────────────────────────────────────────────────
#  TENANT SETTINGS
# ────────────────────────────────────────────────────────────────
TENANT_MODEL = "core.Tenant"
PUBLIC_SCHEMA_NAME = 'public'
TENANT_DOMAIN_MODEL = "core.Domain"  # 🚨 ADD THIS - YOU WERE MISSING IT!

DEMO_MODE_HOSTS = ["demo.netily.co.ke"]
DEMO_MODE_TENANTS = ["demo"]

DATABASE_ROUTERS = [
    'django_tenants.routers.TenantSyncRouter',
]

# Recommended tenant admin settings
TENANT_COLOR_ADMIN_APPS = False
TENANT_LIMIT_ADMIN_ACCESS = True
SHOW_PUBLIC_IF_NO_TENANT_FOUND = True



# ────────────────────────────────────────────────────────────────
#  DATABASE
# ────────────────────────────────────────────────────────────────
DATABASES = {
    'default': {
        'ENGINE': 'django_tenants.postgresql_backend',
        'NAME': os.environ.get('DB_NAME', 'isp_management'),
        'USER': os.environ.get('DB_USER', 'isp_user'),
        'PASSWORD': os.environ.get('DB_PASSWORD', ''),
        'HOST': os.environ.get('DB_HOST', 'localhost'),
        'PORT': os.environ.get('DB_PORT', '5432'),
    }
}

# ────────────────────────────────────────────────────────────────
#  REDIS & CELERY CONFIGURATION
# ────────────────────────────────────────────────────────────────
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')

# Celery Configuration
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = 'django-db'  # Store results in Django DB
CELERY_CACHE_BACKEND = 'default'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_TIMEZONE = 'Africa/Nairobi'
CELERY_ENABLE_UTC = True

# ────────────────────────────────────────────────────────────────
#  🚨 FIXED: Comment out DatabaseScheduler to use code-defined schedule
#  The Senior Developer requested we use the crontab schedule defined in celery.py
#  instead of relying on the database schedule table.
#  This prevents the "relation does not exist" error on first run.
# ────────────────────────────────────────────────────────────────
# CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'
# Using the default scheduler which reads from celery.py beat_schedule

# ────────────────────────────────────────────────────────────────
#  ROOT URLCONF, TEMPLATES, WSGI
# ────────────────────────────────────────────────────────────────
ROOT_URLCONF = 'config.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(BASE_DIR, 'templates')],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

# ────────────────────────────────────────────────────────────────
#  PASSWORD VALIDATION
# ────────────────────────────────────────────────────────────────
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ────────────────────────────────────────────────────────────────
#  INTERNATIONALIZATION & TIME
# ────────────────────────────────────────────────────────────────
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'Africa/Nairobi'
USE_I18N = True
USE_TZ = True

# ────────────────────────────────────────────────────────────────
#  STATIC & MEDIA
# ────────────────────────────────────────────────────────────────
STATIC_URL = 'static/'
STATICFILES_DIRS = [os.path.join(BASE_DIR, 'static')]
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')

MEDIA_URL = '/media/'
MEDIA_ROOT = os.path.join(BASE_DIR, 'media')

# ────────────────────────────────────────────────────────────────
#  DEFAULT PRIMARY KEY & CUSTOM USER
# ────────────────────────────────────────────────────────────────
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
AUTH_USER_MODEL = 'core.User'

# ────────────────────────────────────────────────────────────────
#  REST FRAMEWORK & JWT
# ────────────────────────────────────────────────────────────────
REST_FRAMEWORK = {
    'DEFAULT_AUTHENTICATION_CLASSES': (
        'apps.core.authentication.SessionBoundJWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.AllowAny',
    ),
    'DEFAULT_THROTTLE_CLASSES': (
        'rest_framework.throttling.ScopedRateThrottle',
    ),
    'DEFAULT_FILTER_BACKENDS': (
        'django_filters.rest_framework.DjangoFilterBackend',
    ),
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
    
    # Rate limiting for hotspot endpoints
    # This supports the throttles used in cloud_portal_views.py
    'DEFAULT_THROTTLE_RATES': {
        'hotspot_auto_login': '20/min',           # Auto-login attempts
        'hotspot_tv_code_generate': '30/min',     # TV code generation (prevents abuse)
        'hotspot_tv_code_verify': '60/min',       # TV code verification (user-friendly)
        'affiliate_click': '120/min',
        'affiliate_register': '10/hour',
        'affiliate_login': '20/min',
        'affiliate_verify': '10/hour',
        'lead_submit': '10/hour',
    },
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(days=1),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': SECRET_KEY,
    'AUTH_HEADER_TYPES': ('Bearer',),
    'TOKEN_TYPE_CLAIM': 'token_type',
}

# ────────────────────────────────────────────────────────────────
#  CORS
# ────────────────────────────────────────────────────────────────
# CORS Configuration - Multi-Tenant Subdomains
if DEBUG:
    # Development: allow all subdomains of localhost + 127.0.0.1
    CORS_ALLOWED_ORIGINS = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://*.localhost:3000",    # ← Add this for danstedd.localhost:3000
        "http://*.127.0.0.1:3000",    # ← Add this for backup
    ]
else:
    # Production: allow your main domain + all subdomains
    CORS_ALLOWED_ORIGINS = [
        "https://yourisp.com",
        "https://*.yourisp.com",      # All subdomains like danstedd.yourisp.com
    ]

# Always allow credentials (cookies, auth headers)
CORS_ALLOW_CREDENTIALS = True

# Standard CORS headers plus the tenant and OTP browser-session headers.
CORS_ALLOW_HEADERS = (
    *default_headers,
    "x-session-id",
    "x-tenant",
)

# Allow all methods (GET, POST, PUT, DELETE)
CORS_ALLOW_METHODS = [
    "DELETE",
    "GET",
    "OPTIONS",
    "PATCH",
    "POST",
    "PUT",
]

# Allow all origins in development (ultra-safe for local testing)
if DEBUG:
    CORS_ALLOW_ALL_ORIGINS = True  # ← Add this line for dev - REM


# ────────────────────────────────────────────────────────────────
#  EMAIL (development)
# ────────────────────────────────────────────────────────────────
EMAIL_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = os.getenv('EMAIL_HOST', 'smtp.gmail.com')
EMAIL_PORT = int(os.getenv('EMAIL_PORT', 587))
EMAIL_USE_TLS = os.getenv('EMAIL_USE_TLS', 'True') == 'True'
EMAIL_HOST_USER = os.getenv('EMAIL_HOST_USER', '')
EMAIL_HOST_PASSWORD = os.getenv('EMAIL_HOST_PASSWORD', '')
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'billing@netily.co.ke')
EMAIL_TIMEOUT = int(os.getenv('EMAIL_TIMEOUT', 10))

# Resend — modern transactional email (primary provider)
# Sign up at https://resend.com, add your domain, get the API key
RESEND_API_KEY = os.getenv('RESEND_API_KEY', '')  # Set in .env for production
RESEND_FROM_EMAIL = os.getenv('RESEND_FROM_EMAIL', 'Netily <billing@netily.co.ke>')
OTP_EXEMPT_EMAILS = [e.strip().lower() for e in os.getenv('OTP_EXEMPT_EMAILS', 'admin@netily.co.ke').split(',') if e.strip()]
AFFILIATE_ATTRIBUTION_WINDOW_DAYS = int(os.getenv('AFFILIATE_ATTRIBUTION_WINDOW_DAYS', '30'))
AFFILIATE_CLICK_DEDUP_MINUTES = int(os.getenv('AFFILIATE_CLICK_DEDUP_MINUTES', '30'))

# ────────────────────────────────────────────────────────────────
#  PLATFORM DOMAIN SETTINGS
# ────────────────────────────────────────────────────────────────
# Base domain for tenant subdomains (e.g. "acme.netily.co.ke")
TENANT_BASE_DOMAIN = os.getenv('TENANT_BASE_DOMAIN', 'localhost')
# Frontend URL (Next.js app) — used for admin panel links et al
FRONTEND_URL = os.getenv('FRONTEND_URL', 'http://localhost:3000')

# ── WebAuthn / Passkey settings ──
WEBAUTHN_RP_ID = os.getenv('WEBAUTHN_RP_ID', 'localhost')       # e.g. 'netily.co.ke' in prod
WEBAUTHN_RP_NAME = os.getenv('WEBAUTHN_RP_NAME', 'Netily')

# Netily tenant support chatbot
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
NETILY_SUPPORT_CHAT_KNOWLEDGE_DIR = os.getenv('NETILY_SUPPORT_CHAT_KNOWLEDGE_DIR', 'rag/netily-support')
NETILY_SUPPORT_CHAT_USE_LLM = os.getenv('NETILY_SUPPORT_CHAT_USE_LLM', 'True').lower() in ('1', 'true', 'yes', 'on')
NETILY_SUPPORT_CHAT_MODEL = os.getenv('NETILY_SUPPORT_CHAT_MODEL', 'gpt-4o-mini')
NETILY_SUPPORT_CHAT_TEMPERATURE = float(os.getenv('NETILY_SUPPORT_CHAT_TEMPERATURE', '0.2'))
NETILY_SUPPORT_CHAT_TIMEOUT = int(os.getenv('NETILY_SUPPORT_CHAT_TIMEOUT', '20'))
NETILY_SUPPORT_CHAT_MAX_RETRIES = int(os.getenv('NETILY_SUPPORT_CHAT_MAX_RETRIES', '1'))
NETILY_SUPPORT_CHAT_CHUNK_SIZE = int(os.getenv('NETILY_SUPPORT_CHAT_CHUNK_SIZE', '900'))
NETILY_SUPPORT_CHAT_CHUNK_OVERLAP = int(os.getenv('NETILY_SUPPORT_CHAT_CHUNK_OVERLAP', '120'))
NETILY_SUPPORT_CHAT_MAX_CONTEXT_CHARS = int(os.getenv('NETILY_SUPPORT_CHAT_MAX_CONTEXT_CHARS', '5000'))

# ────────────────────────────────────────────────────────────────
#  SESSION SETTINGS
# ────────────────────────────────────────────────────────────────
SESSION_COOKIE_AGE = 86400  # 24 hours
SESSION_SAVE_EVERY_REQUEST = True

# ────────────────────────────────────────────────────────────────
#  SECURITY HEADERS (development)
# ────────────────────────────────────────────────────────────────
SECURE_BROWSER_XSS_FILTER = True
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'

# ────────────────────────────────────────────────────────────────
#  LOGGING (development)
# ────────────────────────────────────────────────────────────────
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {'format': '{levelname} {asctime} {module} {message}', 'style': '{'},
        'simple': {'format': '{levelname} {message}', 'style': '{'},
    },
    'handlers': {
        'console': {'class': 'logging.StreamHandler', 'formatter': 'simple'},
    },
    'loggers': {
        'django': {'handlers': ['console'], 'level': 'INFO', 'propagate': True},
        'apps.core': {'handlers': ['console'], 'level': 'INFO', 'propagate': True},
    },
}

# ────────────────────────────────────────────────────────────────
#  NOTIFICATION SETTINGS
# ────────────────────────────────────────────────────────────────
NOTIFICATION_SETTINGS = {
    'SMS_PROVIDER': 'africastalking',
    'SMS_CONFIG': {'sender_id': 'ISPMS', 'max_per_hour': 10},
    'EMAIL_CONFIG': {
        'backend': 'django',
        'default_from': 'noreply@yourisp.com',
        'bcc_enabled': True,
        'bcc_email': 'notifications@yourisp.com',
    },
    'PUSH_NOTIFICATION_CONFIG': {
        'provider': 'firebase',
        'firebase_credentials_path': 'path/to/firebase-credentials.json',
    },
}


# Email is configured above via .env — Gmail SMTP is the sole provider

AFRICASTALKING_USERNAME = os.getenv('AFRICASTALKING_USERNAME', 'sandbox')
AFRICASTALKING_API_KEY = os.getenv('AFRICASTALKING_API_KEY', '')
AFRICASTALKING_SENDER_ID = os.getenv('AFRICASTALKING_SENDER_ID', 'ISPMS')

TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID', '')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN', '')
TWILIO_PHONE_NUMBER = os.getenv('TWILIO_PHONE_NUMBER', '')

# ────────────────────────────────────────────────────────────────
#  INBUILT SYSTEM SMS GATEWAY (BYTEWAVE MASTER ACCOUNT)
#  Used as the system's own SMS gateway for internal notifications,
#  tenant SMS wallet top-ups, and fallback when no tenant gateway is configured.
# ────────────────────────────────────────────────────────────────
BYTEWAVE_API_TOKEN = os.getenv('BYTEWAVE_API_TOKEN', '')
BYTEWAVE_SENDER_ID = os.getenv('BYTEWAVE_SENDER_ID', 'BytewaveSMS')
BYTEWAVE_BASE_URL = os.getenv('BYTEWAVE_BASE_URL', 'https://portal.bytewavenetworks.com/api/v3')

# ────────────────────────────────────────────────────────────────
#  PAYHERO CONFIG (Netily Master Account)
#  All customer payments flow through this account.
#  ISPs receive settlements after 5% commission deduction.
# ────────────────────────────────────────────────────────────────
PAYHERO_API_USERNAME = os.getenv('PAYHERO_API_USERNAME', '')
PAYHERO_API_PASSWORD = os.getenv('PAYHERO_API_PASSWORD', '')
PAYHERO_ENVIRONMENT = os.getenv('PAYHERO_ENVIRONMENT', 'sandbox')  # 'sandbox' or 'production'
PAYHERO_CHANNEL_ID = int(os.getenv('PAYHERO_CHANNEL_ID', '1180'))  # Default STK channel
PAYHERO_WEBHOOK_SECRET = os.getenv('PAYHERO_WEBHOOK_SECRET', '')  # For verifying webhooks

# Callback URLs for different payment types
PAYHERO_CALLBACK_URL = os.getenv('PAYHERO_CALLBACK_URL', 'https://api.netily.io/api/v1/webhooks/payhero/')
PAYHERO_SUBSCRIPTION_CALLBACK = os.getenv('PAYHERO_SUBSCRIPTION_CALLBACK', 'https://api.netily.io/api/v1/webhooks/payhero/subscription/')
PAYHERO_HOTSPOT_CALLBACK = os.getenv('PAYHERO_HOTSPOT_CALLBACK', 'https://api.netily.io/api/v1/webhooks/payhero/hotspot/')
PAYHERO_BILLING_CALLBACK = os.getenv('PAYHERO_BILLING_CALLBACK', 'https://api.netily.io/api/v1/webhooks/payhero/billing/')

# Commission settings
NETILY_COMMISSION_RATE = float(os.getenv('NETILY_COMMISSION_RATE', '0.05'))  # 5% default

# ────────────────────────────────────────────────────────────────
#  NGROK / PUBLIC DOMAIN
# ────────────────────────────────────────────────────────────────
NGROK_URL = os.environ.get('NGROK_URL', '')
PUBLIC_DOMAIN = os.environ.get('PUBLIC_DOMAIN', '')

if NGROK_URL:
    BASE_URL = NGROK_URL
elif PUBLIC_DOMAIN:
    BASE_URL = f"https://{PUBLIC_DOMAIN}"
else:
    BASE_URL = ''

# ────────────────────────────────────────────────────────────────
#  🚨 UPDATED: CLOUD CONTROLLER / VPN SETTINGS (EXPANDED CAPACITY)
#  Now using /16 CIDR for up to 65,534 usable IP addresses
# ────────────────────────────────────────────────────────────────
VPN_SERVER_IP = os.environ.get('VPN_SERVER_IP', '10.8.0.1')
VPN_NETWORK_CIDR = os.environ.get('VPN_NETWORK_CIDR', '10.8.0.0/16')  # 65,534 usable IPs (upgraded from /24)
VPN_RESERVED_IPS = os.environ.get('VPN_RESERVED_IPS', '10.8.0.1,10.8.0.2')  # Reserved for server/infrastructure

# OpenVPN Management Interface (for monitoring connected routers)
OPENVPN_MANAGEMENT_HOST = os.environ.get('OPENVPN_MANAGEMENT_HOST', '127.0.0.1')
OPENVPN_MANAGEMENT_PORT = int(os.environ.get('OPENVPN_MANAGEMENT_PORT', '7505'))

# CCD path inside the OpenVPN Docker container volume
OPENVPN_CCD_PATH = os.environ.get('OPENVPN_CCD_PATH', '/etc/openvpn/ccd')

# Captive Portal (Next.js frontend for WiFi users)
CAPTIVE_PORTAL_URL = os.environ.get('CAPTIVE_PORTAL_URL', 'https://portal.netily.co.ke')

# VPN API endpoint (how routers reach Django through the tunnel)
VPN_API_URL = f"http://{VPN_SERVER_IP}:8000"

# config/settings/base.py

# TUMA PAYMENT GATEWAY CONFIG
TUMA_API_BASE_URL = os.getenv("TUMA_API_BASE_URL", "https://api.tuma.co.ke")
TUMA_MASTER_EMAIL = os.getenv("TUMA_MASTER_EMAIL", "")       # Your main Tuma account email
TUMA_MASTER_API_KEY = os.getenv("TUMA_MASTER_API_KEY", "")   # Your main Tuma API Key
TUMA_CALLBACK_URL = os.getenv("TUMA_CALLBACK_URL", "https://your-production-domain.com/api/v1/webhooks/tuma/callback/")
TUMA_SUBSCRIPTION_CALLBACK = os.getenv("TUMA_SUBSCRIPTION_CALLBACK", "https://api.netily.co.ke/api/v1/webhooks/tuma/subscription-callback/")
TUMA_DEFAULT_LOGO_URL = os.getenv("TUMA_DEFAULT_LOGO_URL", "https://your-saas.com/default-logo.png")

# ────────────────────────────────────────────────────────────────
#  NETILY OWN PAYBILL (replaces Tuma passthrough for STK routing)
# ────────────────────────────────────────────────────────────────
NETILY_PAYBILL_CONSUMER_KEY = os.getenv('NETILY_PAYBILL_CONSUMER_KEY', '')
NETILY_PAYBILL_CONSUMER_SECRET = os.getenv('NETILY_PAYBILL_CONSUMER_SECRET', '')
NETILY_PAYBILL_SHORTCODE = os.getenv('NETILY_PAYBILL_SHORTCODE', '')
NETILY_PAYBILL_PASSKEY = os.getenv('NETILY_PAYBILL_PASSKEY', '')
NETILY_PAYBILL_ENVIRONMENT = os.getenv('NETILY_PAYBILL_ENVIRONMENT', 'production')
NETILY_PAYBILL_CALLBACK_URL = os.getenv(
    'NETILY_PAYBILL_CALLBACK_URL',
    'https://api.netily.co.ke/api/v1/webhooks/netily-paybill/callback/',
)

# 🚨 NEW: Netily subscription paybill callback (replaces Tuma subscription callback)
NETILY_SUBSCRIPTION_PAYBILL_CALLBACK = os.getenv(
    'NETILY_SUBSCRIPTION_PAYBILL_CALLBACK',
    'https://api.netily.co.ke/api/v1/webhooks/netily-paybill/subscription-callback/',
)

# NETILY SYSTEM PAYMENT SIMULATOR
# Disabled by default and isolated from subscription/payment ledgers.
NETILY_SYSTEM_PAYMENT_SIMULATOR_ENABLED = os.getenv("NETILY_SYSTEM_PAYMENT_SIMULATOR_ENABLED", "False")
NETILY_SYSTEM_PAYMENT_SIMULATOR_TOKEN = os.getenv("NETILY_SYSTEM_PAYMENT_SIMULATOR_TOKEN", "")
NETILY_SYSTEM_PAYMENT_CALLBACK_URL = os.getenv("NETILY_SYSTEM_PAYMENT_CALLBACK_URL", "")
NETILY_SYSTEM_MPESA_ENVIRONMENT = os.getenv("NETILY_SYSTEM_MPESA_ENVIRONMENT", "production")
NETILY_SYSTEM_MPESA_SHORTCODE = os.getenv("NETILY_SYSTEM_MPESA_SHORTCODE", "")
NETILY_SYSTEM_MPESA_CONSUMER_KEY = os.getenv("NETILY_SYSTEM_MPESA_CONSUMER_KEY", "")
NETILY_SYSTEM_MPESA_CONSUMER_SECRET = os.getenv("NETILY_SYSTEM_MPESA_CONSUMER_SECRET", "")
NETILY_SYSTEM_MPESA_PASSKEY = os.getenv("NETILY_SYSTEM_MPESA_PASSKEY", "")

# ────────────────────────────────────────────────────────────────
#  WIREGUARD SETTINGS (replaces OpenVPN)
# ────────────────────────────────────────────────────────────────
WG_SERVER_HOST       = os.environ.get('WG_SERVER_HOST', 'vpn.netily.co.ke')
WG_SERVER_PORT       = int(os.environ.get('WG_SERVER_PORT', '51820'))
WG_SERVER_PUBLIC_KEY = os.environ.get('WG_SERVER_PUBLIC_KEY', '')
WG_INTERFACE         = os.environ.get('WG_INTERFACE', 'wg0')
WG_PEERS_DIR         = os.environ.get('WG_PEERS_DIR', '/etc/wireguard/peers')


# ────────────────────────────────────────────────────────────────
#  TELEGRAM NOTIFICATIONS
# ────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
_tg_admins = os.environ.get('TELEGRAM_ADMIN_CHAT_IDS', '')
TELEGRAM_ADMIN_CHAT_IDS = [x.strip() for x in _tg_admins.split(',') if x.strip()]

# ────────────────────────────────────────────────────────────────
#  TELEGRAM — PAYMENT NOTIFICATIONS (separate bot from lead alerts)
# ────────────────────────────────────────────────────────────────
TELEGRAM_PAYMENTS_BOT_TOKEN = os.environ.get('TELEGRAM_PAYMENTS_BOT_TOKEN', '')
# Reuses the same admin chat IDs as leads unless overridden
_tg_payment_admins = os.environ.get('TELEGRAM_PAYMENTS_ADMIN_CHAT_IDS', '')
TELEGRAM_PAYMENTS_ADMIN_CHAT_IDS = (
    [x.strip() for x in _tg_payment_admins.split(',') if x.strip()]
    or TELEGRAM_ADMIN_CHAT_IDS
)