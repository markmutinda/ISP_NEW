"""
Django settings for ISP Management System - Base Configuration
"""

import os
from pathlib import Path
from datetime import timedelta
import sys

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

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

# Add Celery apps only if celery is installed (production)
try:
    import celery
    BASE_APPS.extend([
        'django_celery_beat',      # Celery Beat scheduler (periodic tasks)
        'django_celery_results',   # Celery task results backend
    ])
except ImportError:
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
)

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
    'apps.billing',
    'apps.support',
    'apps.analytics',
    'apps.staff',
    'apps.self_service',
    'apps.inventory',
    'apps.notifications',
    'apps.bandwidth',
    'apps.vpn',                        # VPN/OpenVPN Management
    'apps.radius',                     # RADIUS/FreeRADIUS Integration
)

# ────────────────────────────────────────────────────────────────
#  FINAL INSTALLED_APPS — merge everything without duplicates
# ────────────────────────────────────────────────────────────────
INSTALLED_APPS = list(SHARED_APPS) + [app for app in BASE_APPS if app not in SHARED_APPS] + \
                 [app for app in TENANT_APPS if app not in SHARED_APPS and app not in BASE_APPS]

# Remove duplicates while preserving order
INSTALLED_APPS = list(dict.fromkeys(INSTALLED_APPS))

# Debug toolbar only in DEBUG mode
if DEBUG:
    INSTALLED_APPS.append('debug_toolbar')

# ────────────────────────────────────────────────────────────────
#  MIDDLEWARE — CorsMiddleware MUST be before anything that might return a response
# ────────────────────────────────────────────────────────────────
MIDDLEWARE = [
    'apps.core.middleware.CorsPreflightMiddleware',  # Handle CORS preflight FIRST
    'corsheaders.middleware.CorsMiddleware',
    'apps.core.middleware.TenantMainMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'apps.core.middleware.AuditLogMiddleware',
    'apps.core.middleware.CompanyContextMiddleware',
    # REMOVED: 'apps.core.middleware.TenantMiddleware',
]
if DEBUG:
    MIDDLEWARE.insert(3, 'debug_toolbar.middleware.DebugToolbarMiddleware')  # After CORS middlewares
    INTERNAL_IPS = ['127.0.0.1', 'localhost', 'camden-convocative-oversorrowfully.ngrok-free.dev']
    DEBUG_TOOLBAR_CONFIG = {'SHOW_TOOLBAR_CALLBACK': lambda request: DEBUG}

# ────────────────────────────────────────────────────────────────
#  TENANT SETTINGS
# ────────────────────────────────────────────────────────────────
TENANT_MODEL = "core.Tenant"
PUBLIC_SCHEMA_NAME = 'public'
TENANT_DOMAIN_MODEL = "core.Domain"  # 🚨 ADD THIS - YOU WERE MISSING IT!

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

# Celery Beat (scheduled tasks) - Use database scheduler
CELERY_BEAT_SCHEDULER = 'django_celery_beat.schedulers:DatabaseScheduler'

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
        'rest_framework_simplejwt.authentication.JWTAuthentication',
    ),
    'DEFAULT_PERMISSION_CLASSES': (
        'rest_framework.permissions.AllowAny',
    ),
    'DEFAULT_FILTER_BACKENDS': (
        'django_filters.rest_framework.DjangoFilterBackend',
    ),
    'DEFAULT_PAGINATION_CLASS': 'rest_framework.pagination.PageNumberPagination',
    'PAGE_SIZE': 20,
}

SIMPLE_JWT = {
    'ACCESS_TOKEN_LIFETIME': timedelta(days=1),
    'REFRESH_TOKEN_LIFETIME': timedelta(days=7),
    'ROTATE_REFRESH_TOKENS': True,
    'BLACKLIST_AFTER_ROTATION': True,
    'ALGORITHM': 'HS256',
    'SIGNING_KEY': SECRET_KEY,
    'AUTH_HEADER_TYPES': ('Bearer',),
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

# Allow all headers (JWT, Content-Type, etc.)
CORS_ALLOW_ALL_HEADERS = True

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
DEFAULT_FROM_EMAIL = os.getenv('DEFAULT_FROM_EMAIL', 'noreply@yourisp.com')

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


EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'  # For dev - prints emails in console
# In production: 'django.core.mail.backends.smtp.EmailBackend'
EMAIL_HOST = 'smtp.gmail.com'  # Example for Gmail
EMAIL_PORT = 587
EMAIL_USE_TLS = True
EMAIL_HOST_USER = 'your.email@gmail.com'
EMAIL_HOST_PASSWORD = 'your-app-password'
DEFAULT_FROM_EMAIL = 'noreply@yourisp.com'

AFRICASTALKING_USERNAME = os.getenv('AFRICASTALKING_USERNAME', 'sandbox')
AFRICASTALKING_API_KEY = os.getenv('AFRICASTALKING_API_KEY', '')
AFRICASTALKING_SENDER_ID = os.getenv('AFRICASTALKING_SENDER_ID', 'ISPMS')

TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID', '')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN', '')
TWILIO_PHONE_NUMBER = os.getenv('TWILIO_PHONE_NUMBER', '')

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
#  CLOUD CONTROLLER / VPN SETTINGS
# ────────────────────────────────────────────────────────────────
VPN_SERVER_IP = os.environ.get('VPN_SERVER_IP', '10.8.0.1')
VPN_NETWORK_CIDR = os.environ.get('VPN_NETWORK_CIDR', '10.8.0.0/24')
VPN_IP_RANGE_START = int(os.environ.get('VPN_IP_RANGE_START', '10'))   # 10.8.0.10
VPN_IP_RANGE_END = int(os.environ.get('VPN_IP_RANGE_END', '250'))      # 10.8.0.250

# OpenVPN Management Interface (for monitoring connected routers)
OPENVPN_MANAGEMENT_HOST = os.environ.get('OPENVPN_MANAGEMENT_HOST', '127.0.0.1')
OPENVPN_MANAGEMENT_PORT = int(os.environ.get('OPENVPN_MANAGEMENT_PORT', '7505'))

# CCD path inside the OpenVPN Docker container volume
OPENVPN_CCD_PATH = os.environ.get('OPENVPN_CCD_PATH', '/etc/openvpn/ccd')

# Captive Portal (Next.js frontend for WiFi users)
CAPTIVE_PORTAL_URL = os.environ.get('CAPTIVE_PORTAL_URL', 'https://portal.netily.co.ke')

# VPN API endpoint (how routers reach Django through the tunnel)
VPN_API_URL = f"http://{VPN_SERVER_IP}:8000"
