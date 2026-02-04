"""
Celery Configuration for Netily ISP Management System

This module configures Celery for background task processing including:
- Sending notifications (SMS, Email, Push)
- RADIUS user session management
- Disconnecting expired users
- Billing and settlement tasks
"""

import os
from celery import Celery
from celery.schedules import crontab

# Set default Django settings module
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.local')

# Create Celery app
app = Celery('netily')

# Using a string here means the worker doesn't have to serialize
# the configuration object to child processes.
app.config_from_object('django.conf:settings', namespace='CELERY')

# Auto-discover tasks in all installed apps
app.autodiscover_tasks()

# ════════════════════════════════════════════════════════════════════════════
# CELERY BEAT SCHEDULE - Periodic Tasks
# ════════════════════════════════════════════════════════════════════════════

app.conf.beat_schedule = {
    # ────────────────────────────────────────────────────────────────
    # RADIUS Session Management - Runs every 5 minutes
    # ────────────────────────────────────────────────────────────────
    'disconnect-expired-users-every-5-min': {
        'task': 'apps.radius.tasks.disconnect_expired_users',
        'schedule': crontab(minute='*/5'),  # Every 5 minutes
        'options': {'queue': 'radius'}
    },
    
    # ────────────────────────────────────────────────────────────────
    # Cleanup stale RADIUS sessions - Daily at 3 AM
    # ────────────────────────────────────────────────────────────────
    'cleanup-stale-sessions-daily': {
        'task': 'apps.radius.tasks.cleanup_stale_sessions',
        'schedule': crontab(hour=3, minute=0),  # Daily at 3 AM
        'options': {'queue': 'radius'}
    },
    
    # ────────────────────────────────────────────────────────────────
    # Process Alert Rules - Every 15 minutes
    # ────────────────────────────────────────────────────────────────
    'process-alert-rules-every-15-min': {
        'task': 'apps.notifications.tasks.process_alert_rules_task',
        'schedule': crontab(minute='*/15'),
        'options': {'queue': 'notifications'}
    },
    
    # ────────────────────────────────────────────────────────────────
    # Sync RADIUS with MikroTik - Every hour
    # ────────────────────────────────────────────────────────────────
    'sync-radius-users-hourly': {
        'task': 'apps.radius.tasks.sync_all_radius_users',
        'schedule': crontab(minute=0),  # Every hour at :00
        'options': {'queue': 'radius'}
    },
}

# ════════════════════════════════════════════════════════════════════════════
# QUEUE ROUTING
# ════════════════════════════════════════════════════════════════════════════

app.conf.task_routes = {
    'apps.radius.tasks.*': {'queue': 'radius'},
    'apps.notifications.tasks.*': {'queue': 'notifications'},
    'apps.billing.tasks.*': {'queue': 'billing'},
}

# ════════════════════════════════════════════════════════════════════════════
# TASK CONFIGURATION
# ════════════════════════════════════════════════════════════════════════════

app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='Africa/Nairobi',
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
)


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    """Debug task for testing Celery setup."""
    print(f'Request: {self.request!r}')
