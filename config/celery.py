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
    # Disconnects users whose Expiration attribute has passed
    # ────────────────────────────────────────────────────────────────
    'disconnect-expired-users-every-5-min': {
        'task': 'apps.radius.tasks.disconnect_expired_users',
        'schedule': crontab(minute='*/5'),  # Every 5 minutes
        'options': {'queue': 'radius'}
    },
    
    # ────────────────────────────────────────────────────────────────
    # Process Expired Subscriptions - Every 15 minutes
    # Backup check: Disables RADIUS credentials based on expiration_date
    # ────────────────────────────────────────────────────────────────
    'process-expired-subscriptions-every-15-min': {
        'task': 'apps.radius.tasks.process_expired_subscriptions',
        'schedule': crontab(minute='*/15'),  # Every 15 minutes
        'options': {'queue': 'radius'}
    },
    
    # ────────────────────────────────────────────────────────────────
    # Expiry Warning Notifications - Every hour
    # Notifies customers 24 hours before expiration
    # ────────────────────────────────────────────────────────────────
    'notify-expiring-soon-hourly': {
        'task': 'apps.radius.tasks.notify_expiring_soon',
        'schedule': crontab(minute=30),  # Every hour at :30
        'args': (24,),  # 24 hours before expiry
        'options': {'queue': 'notifications'}
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

    # ════════════════════════════════════════════════════════════════
    # CLOUD CONTROLLER — Hotspot RADIUS Cleanup
    # ════════════════════════════════════════════════════════════════
    'cleanup-expired-hotspot-sessions-every-5-min': {
        'task': 'apps.billing.tasks.cleanup_expired_hotspot_sessions',
        'schedule': crontab(minute='*/5'),
        'options': {'queue': 'billing'}
    },
    'expire-stale-pending-payments-every-10-min': {
        'task': 'apps.billing.tasks.expire_stale_pending_payments',
        'schedule': crontab(minute='*/10'),
        'options': {'queue': 'billing'}
    },

    # ════════════════════════════════════════════════════════════════
    # CLOUD CONTROLLER — VPN Tunnel Monitoring
    # ════════════════════════════════════════════════════════════════
    'monitor-vpn-tunnels-every-2-min': {
        'task': 'apps.vpn.tasks.monitor_vpn_tunnels',
        'schedule': crontab(minute='*/2'),
        'options': {'queue': 'default'}
    },
    'check-vpn-health-every-minute': {
        'task': 'apps.vpn.tasks.check_vpn_health',
        'schedule': crontab(minute='*/1'),
        'options': {'queue': 'default'}
    },
    'cleanup-orphaned-ccd-daily': {
        'task': 'apps.vpn.tasks.cleanup_orphaned_ccd',
        'schedule': crontab(hour=4, minute=0),  # Daily at 4 AM
        'options': {'queue': 'default'}
    },
}

# ════════════════════════════════════════════════════════════════════════════
# QUEUE ROUTING
# ════════════════════════════════════════════════════════════════════════════

app.conf.task_routes = {
    'apps.radius.tasks.*': {'queue': 'radius'},
    'apps.notifications.tasks.*': {'queue': 'notifications'},
    'apps.billing.tasks.*': {'queue': 'billing'},
    'apps.vpn.tasks.*': {'queue': 'default'},
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
