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
    # Router Status Management - Runs every 5 minutes
    # Prevents frontend locking by caching API responses
    # ────────────────────────────────────────────────────────────────
    'refresh-router-statuses-every-5-min': {
        'task': 'apps.network.tasks.refresh_router_statuses',
        'schedule': crontab(minute='*/5'),
        'options': {'queue': 'default'}
    },
    
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
    # Cleanup stale RADIUS sessions - Runs every 5 minutes
    # Sweeps all tenant radacct tables for ghost sessions
    # ────────────────────────────────────────────────────────────────
    'cleanup-stale-sessions-every-5-min': {
        'task': 'apps.radius.tasks.cleanup_stale_sessions',
        'schedule': crontab(minute='*/5'),  # Every 5 minutes
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
    'retry-failed-notifications-every-30-min': {
        'task': 'apps.notifications.tasks.retry_failed_notifications_task',
        'schedule': crontab(minute='*/30'),
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
    # HOTSPOT SMS NOTIFICATIONS (Expiry warnings & expired alerts)
    # ════════════════════════════════════════════════════════════════
    'hotspot-expiry-warnings-every-5-min': {
        'task': 'apps.billing.tasks.send_hotspot_expiry_warnings',
        'schedule': crontab(minute='*/5'),
        'options': {'queue': 'billing'},
    },
    'notify-expired-hotspot-every-5-min': {
        'task': 'apps.billing.tasks.notify_expired_hotspot_sessions',
        'schedule': crontab(minute='*/5'),
        'options': {'queue': 'billing'},
    },

    # ════════════════════════════════════════════════════════════════
    # PPPOE SMS NOTIFICATIONS (Expiry reminders - daily at 9am)
    # ════════════════════════════════════════════════════════════════
    'pppoe-expiry-reminders-daily': {
        'task': 'apps.radius.tasks.send_pppoe_expiry_reminders',
        'schedule': crontab(hour=9, minute=0),
        'options': {'queue': 'radius'},
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

    # ════════════════════════════════════════════════════════════════
    # PLATFORM BILLING — The Engine (Phase 5 Autopilot)
    # ════════════════════════════════════════════════════════════════
    'generate-metered-invoices-daily': {
        'task': 'apps.subscriptions.tasks.generate_metered_invoices',
        'schedule': crontab(hour=0, minute=5),  # Daily at 12:05 AM
        'options': {'queue': 'billing'}
    },
    'sweep-pppoe-ghost-records-daily': {
        'task': 'apps.subscriptions.tasks.sweep_pppoe_ghost_records',
        'schedule': crontab(hour=0, minute=10),  # Daily at 12:10 AM
        'options': {'queue': 'billing'}
    },
    'check-trial-lifecycle-daily': {
        'task': 'apps.subscriptions.tasks.check_trial_lifecycle',
        'schedule': crontab(hour=8, minute=0),  # Daily at 8:00 AM
        'options': {'queue': 'billing'}
    },
    'enforce-billing-grace-period-daily': {
        'task': 'apps.subscriptions.tasks.enforce_billing_grace_period',
        'schedule': crontab(hour=0, minute=15),  # Daily at 12:15 AM
        'options': {'queue': 'billing'}
    },
    'reconcile-hotspot-accumulators': {
        'task': 'apps.subscriptions.tasks.reconcile_hotspot_accumulators',
        'schedule': crontab(hour=6, minute=0),  # Daily at 6:00 AM
        'options': {'queue': 'billing'}
    },

    # ────────────────────────────────────────────────────────────────
    # Metered Billing Estimate Cache — 3× / day
    # Pre-computes and caches billing estimates for metered plan tenants
    # so the admin Usage tab loads instantly from Redis.
    # ────────────────────────────────────────────────────────────────
    'refresh-metered-billing-estimates-3x-daily': {
        'task': 'apps.subscriptions.tasks.refresh_metered_billing_estimates',
        'schedule': crontab(hour='8,14,20', minute=0),  # 8 AM, 2 PM, 8 PM
        'options': {'queue': 'billing'}
    },

    # ────────────────────────────────────────────────────────────────
    # FUP Automation - Every 10 minutes
    # ────────────────────────────────────────────────────────────────
    'sync-fup-usage-every-10-min': {
        'task': 'apps.fup.tasks.sync_fup_usage',
        'schedule': crontab(minute='*/10'),
        'options': {'queue': 'radius'}
    },
    'enforce-fup-policies-every-10-min': {
        'task': 'apps.fup.tasks.enforce_fup_policies',
        'schedule': crontab(minute='*/10'),
        'options': {'queue': 'radius'}
    },
    'reconcile-fup-states-hourly': {
        'task': 'apps.fup.tasks.reconcile_fup_states',
        'schedule': crontab(minute=15),  # Every hour at :15
        'options': {'queue': 'radius'}
    },

    # ════════════════════════════════════════════════════════════════
    # LOYALTY — Monthly tenure bonus & auto-enrollment catch-all
    # ════════════════════════════════════════════════════════════════
    'loyalty-monthly-tenure-bonus-daily': {
        'task': 'apps.loyalty.tasks.award_monthly_tenure_bonus',
        'schedule': crontab(hour=9, minute=0),  # Daily at 9:00 AM
        'options': {'queue': 'default'}
    },
    'loyalty-enroll-missing-customers-hourly': {
        'task': 'apps.loyalty.tasks.enroll_missing_customers',
        'schedule': crontab(minute=45),  # Every hour at :45
        'options': {'queue': 'default'}
    },

    # ════════════════════════════════════════════════════════════════
    # BILLING EMAIL REMINDERS — Daily at 10 AM
    # ════════════════════════════════════════════════════════════════
    'send-billing-reminder-emails-daily': {
        'task': 'apps.billing.tasks.send_billing_reminder_emails',
        'schedule': crontab(hour=10, minute=0),  # Daily at 10:00 AM
        'options': {'queue': 'billing'}
    },
}

# ════════════════════════════════════════════════════════════════════════════
# QUEUE ROUTING
# ════════════════════════════════════════════════════════════════════════════
# FIX: Route messaging tasks to 'default' queue instead of non-existent 'messaging'
# This ensures SMS campaign tasks are picked up by the celery-worker container
# which listens to 'default' queue by default.
app.conf.task_routes = {
    'apps.radius.tasks.*': {'queue': 'radius'},
    'apps.notifications.tasks.*': {'queue': 'notifications'},
    'apps.billing.tasks.*': {'queue': 'billing'},
    'apps.subscriptions.tasks.*': {'queue': 'billing'},  # Added subscription routing
    'apps.messaging.tasks.*': {'queue': 'default'},      # ← Changed from 'messaging' to 'default'
    'apps.vpn.tasks.*': {'queue': 'default'},
    'apps.fup.tasks.*': {'queue': 'radius'},  # FUP tasks use radius queue
    'apps.network.tasks.*': {'queue': 'default'},  # Network tasks (router status)
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