"""
Billing Celery Tasks — Cloud Controller Hotspot Tasks

Periodic tasks for:
- Cleaning up expired hotspot sessions + RADIUS entries
- Expiring stale pending payments
"""

import logging
from celery import shared_task

from django.utils import timezone

logger = logging.getLogger(__name__)


@shared_task(name='apps.billing.tasks.cleanup_expired_hotspot_sessions')
def cleanup_expired_hotspot_sessions():
    """
    Periodic task: Clean up expired hotspot sessions.
    
    1. Finds active sessions past their expiry time
    2. Revokes RADIUS credentials (dual-write: tenant + public schema)
    3. Marks sessions as expired in the database
    
    Runs every 5 minutes via Celery Beat.
    """
    try:
        from apps.billing.services.hotspot_radius_service import HotspotRadiusService
        
        service = HotspotRadiusService()
        count = service.cleanup_expired_sessions()
        
        if count:
            logger.info(f"Cleaned up {count} expired hotspot sessions")
        
        return {'cleaned': count}
    except Exception as e:
        logger.error(f"Hotspot cleanup task failed: {e}", exc_info=True)
        return {'error': str(e)}


@shared_task(name='apps.billing.tasks.expire_stale_pending_payments')
def expire_stale_pending_payments():
    """
    Periodic task: Expire hotspot sessions stuck in 'pending' status.
    
    If a PayHero STK push was sent but never confirmed (user didn't enter PIN),
    the session stays pending forever. This task cleans them up after 10 minutes.
    
    Runs every 10 minutes via Celery Beat.
    """
    try:
        from apps.billing.models.hotspot_models import HotspotSession
        
        cutoff = timezone.now() - timezone.timedelta(minutes=10)
        
        stale_sessions = HotspotSession.objects.filter(
            status='pending',
            created_at__lt=cutoff
        )
        
        count = stale_sessions.count()
        
        for session in stale_sessions:
            session.mark_failed('Payment timeout — STK push not confirmed')
        
        if count:
            logger.info(f"Expired {count} stale pending hotspot payments")
        
        return {'expired': count}
    except Exception as e:
        logger.error(f"Stale payment cleanup task failed: {e}", exc_info=True)
        return {'error': str(e)}
