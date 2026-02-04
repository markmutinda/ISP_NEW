"""
Netily ISP Management System Configuration

This package contains Django and Celery configuration.
"""

# Import Celery app when Django starts (if celery is installed)
try:
    from .celery import app as celery_app
    __all__ = ('celery_app',)
except ImportError:
    # Celery not installed (local development without celery)
    celery_app = None
    __all__ = ()
