"""
RADIUS Celery Tasks - Background Jobs for Session Management

These tasks handle:
1. Disconnecting expired users (wall-clock expiration enforcement)
2. Cleaning up stale sessions in radacct
3. Syncing RADIUS users across all tenants
4. Session monitoring and alerting
"""

import logging
from datetime import timedelta
from celery import shared_task
from django.utils import timezone
from django.db import connection
from django_tenants.utils import schema_context, get_tenant_model

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def disconnect_expired_users(self):
    """
    Find users with expired subscriptions who still have active RADIUS sessions,
    then kick them off MikroTik routers.
    
    This task runs every 5 minutes via Celery Beat to enforce wall-clock expiration.
    
    Flow:
    1. Query radacct for sessions where acctstoptime IS NULL (still active)
    2. Cross-reference with subscription end_date
    3. For expired users, call MikroTik API to disconnect
    4. Mark session as terminated in radacct
    
    Returns:
        Dict with statistics on users processed
    """
    from apps.network.models import Router
    from apps.network.integrations.mikrotik_api import MikrotikAPI
    
    stats = {
        'checked': 0,
        'expired_found': 0,
        'disconnected': 0,
        'errors': 0,
        'tenants_processed': 0
    }
    
    now = timezone.now()
    
    try:
        # Get all active RADIUS sessions from public schema
        with connection.cursor() as cursor:
            # Find active sessions with expired subscriptions
            # We join radacct with radcheck to get the Expiration attribute
            cursor.execute("""
                SELECT DISTINCT 
                    ra.username,
                    ra.nasipaddress,
                    ra.acctsessionid,
                    ra.tenant_schema,
                    rc.value as expiration_date
                FROM public.radacct ra
                INNER JOIN public.radcheck rc 
                    ON ra.username = rc.username 
                    AND rc.attribute = 'Expiration'
                WHERE ra.acctstoptime IS NULL
                    AND rc.value IS NOT NULL
            """)
            
            active_sessions = cursor.fetchall()
        
        logger.info(f"[DISCONNECT TASK] Found {len(active_sessions)} active sessions to check")
        
        # Group sessions by router IP for efficient processing
        router_sessions = {}
        expired_users = []
        
        for row in active_sessions:
            username, nas_ip, session_id, tenant_schema, expiration_str = row
            stats['checked'] += 1
            
            # Parse expiration date (format: "Feb 02 2026 14:00:00")
            try:
                from datetime import datetime
                expiration = datetime.strptime(expiration_str, "%b %d %Y %H:%M:%S")
                expiration = timezone.make_aware(expiration)
                
                if expiration <= now:
                    # User is expired!
                    stats['expired_found'] += 1
                    expired_users.append({
                        'username': username,
                        'nas_ip': nas_ip,
                        'session_id': session_id,
                        'tenant_schema': tenant_schema,
                        'expiration': expiration_str
                    })
                    
                    # Group by router for batch processing
                    if nas_ip not in router_sessions:
                        router_sessions[nas_ip] = []
                    router_sessions[nas_ip].append(username)
                    
            except (ValueError, TypeError) as e:
                logger.warning(f"[DISCONNECT TASK] Could not parse expiration '{expiration_str}' for {username}: {e}")
                continue
        
        logger.info(f"[DISCONNECT TASK] Found {stats['expired_found']} expired users to disconnect")
        
        # Process each router
        for nas_ip, usernames in router_sessions.items():
            try:
                # Find the router by IP
                router = Router.objects.filter(ip_address=nas_ip, is_active=True).first()
                
                if not router:
                    logger.warning(f"[DISCONNECT TASK] Router not found for NAS IP: {nas_ip}")
                    continue
                
                # Connect to router and disconnect users
                api = MikrotikAPI(router)
                
                for username in usernames:
                    try:
                        # Try both PPPoE and Hotspot disconnect
                        result = api.disconnect_user(username, connection_type='both')
                        
                        if result.get('pppoe') or result.get('hotspot'):
                            stats['disconnected'] += 1
                            logger.info(f"[DISCONNECT TASK] Disconnected {username} from {router.name}")
                        
                    except Exception as e:
                        stats['errors'] += 1
                        logger.error(f"[DISCONNECT TASK] Error disconnecting {username}: {e}")
                
            except Exception as e:
                stats['errors'] += 1
                logger.error(f"[DISCONNECT TASK] Error processing router {nas_ip}: {e}")
        
        # Update radacct to mark disconnected sessions
        if expired_users:
            _mark_sessions_terminated(expired_users)
        
        logger.info(f"[DISCONNECT TASK] Complete: {stats}")
        return stats
        
    except Exception as e:
        logger.error(f"[DISCONNECT TASK] Task failed: {e}")
        self.retry(exc=e)


def _mark_sessions_terminated(expired_users: list):
    """
    Mark RADIUS accounting sessions as terminated in radacct.
    
    Args:
        expired_users: List of dicts with username, session_id, nas_ip
    """
    try:
        with connection.cursor() as cursor:
            for user in expired_users:
                cursor.execute("""
                    UPDATE public.radacct 
                    SET acctstoptime = NOW(),
                        acctterminatecause = 'Session-Timeout'
                    WHERE acctsessionid = %s 
                        AND username = %s 
                        AND nasipaddress = %s
                        AND acctstoptime IS NULL
                """, [user['session_id'], user['username'], user['nas_ip']])
        
        logger.info(f"[DISCONNECT TASK] Marked {len(expired_users)} sessions as terminated")
        
    except Exception as e:
        logger.error(f"[DISCONNECT TASK] Error marking sessions terminated: {e}")


@shared_task
def cleanup_stale_sessions():
    """
    Clean up stale RADIUS sessions that were never properly closed.
    
    Runs daily to find sessions without stop time that are older than 24 hours
    and marks them as terminated due to NAS-Error.
    
    Returns:
        Number of stale sessions cleaned
    """
    try:
        cutoff_time = timezone.now() - timedelta(hours=24)
        
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE public.radacct 
                SET acctstoptime = NOW(),
                    acctterminatecause = 'NAS-Error'
                WHERE acctstoptime IS NULL 
                    AND acctstarttime < %s
            """, [cutoff_time])
            
            cleaned = cursor.rowcount
        
        logger.info(f"[CLEANUP TASK] Cleaned {cleaned} stale sessions")
        return cleaned
        
    except Exception as e:
        logger.error(f"[CLEANUP TASK] Error cleaning stale sessions: {e}")
        return 0


@shared_task
def sync_all_radius_users():
    """
    Sync all RADIUS users from tenant schemas to public schema.
    
    This ensures FreeRADIUS has the latest user data for all tenants.
    Runs hourly to catch any missed sync operations.
    
    Returns:
        Dict with sync statistics per tenant
    """
    from apps.radius.services import RadiusSyncService
    
    TenantModel = get_tenant_model()
    stats = {
        'tenants_processed': 0,
        'users_synced': 0,
        'errors': 0
    }
    
    try:
        # Get all tenant schemas (exclude public)
        tenants = TenantModel.objects.exclude(schema_name='public')
        
        for tenant in tenants:
            try:
                with schema_context(tenant.schema_name):
                    service = RadiusSyncService()
                    result = service.sync_all_customers()
                    
                    stats['tenants_processed'] += 1
                    stats['users_synced'] += result.get('total', 0)
                    
                    logger.info(f"[SYNC TASK] Synced {result.get('total', 0)} users for tenant {tenant.schema_name}")
                    
            except Exception as e:
                stats['errors'] += 1
                logger.error(f"[SYNC TASK] Error syncing tenant {tenant.schema_name}: {e}")
        
        logger.info(f"[SYNC TASK] Complete: {stats}")
        return stats
        
    except Exception as e:
        logger.error(f"[SYNC TASK] Task failed: {e}")
        return stats


@shared_task
def disconnect_user_immediately(username: str, router_ip: str = None, connection_type: str = 'both'):
    """
    Immediately disconnect a specific user from the network.
    
    Called when:
    - Admin manually disconnects a user
    - Payment fails and user should be kicked
    - Subscription is cancelled
    
    Args:
        username: RADIUS username to disconnect
        router_ip: Specific router IP (optional, searches all if not provided)
        connection_type: 'hotspot', 'pppoe', or 'both'
        
    Returns:
        Dict with disconnect result
    """
    from apps.network.models import Router
    from apps.network.integrations.mikrotik_api import MikrotikAPI
    
    result = {
        'username': username,
        'disconnected': False,
        'routers_checked': 0,
        'error': None
    }
    
    try:
        if router_ip:
            # Specific router
            routers = Router.objects.filter(ip_address=router_ip, is_active=True)
        else:
            # Search all active routers
            routers = Router.objects.filter(is_active=True)
        
        for router in routers:
            result['routers_checked'] += 1
            
            try:
                api = MikrotikAPI(router)
                disconnect_result = api.disconnect_user(username, connection_type)
                
                if disconnect_result.get('pppoe') or disconnect_result.get('hotspot'):
                    result['disconnected'] = True
                    logger.info(f"[IMMEDIATE DISCONNECT] User {username} disconnected from {router.name}")
                    
            except Exception as e:
                logger.warning(f"[IMMEDIATE DISCONNECT] Error on router {router.name}: {e}")
        
        # Also update radacct
        if result['disconnected']:
            with connection.cursor() as cursor:
                cursor.execute("""
                    UPDATE public.radacct 
                    SET acctstoptime = NOW(),
                        acctterminatecause = 'Admin-Reset'
                    WHERE username = %s 
                        AND acctstoptime IS NULL
                """, [username])
        
        return result
        
    except Exception as e:
        result['error'] = str(e)
        logger.error(f"[IMMEDIATE DISCONNECT] Task failed for {username}: {e}")
        return result


@shared_task
def update_user_expiration(username: str, new_expiration: str):
    """
    Update the expiration date for a RADIUS user in public schema.
    
    Called when:
    - Subscription is renewed
    - Admin extends validity
    
    Args:
        username: RADIUS username
        new_expiration: New expiration in format "Feb 02 2026 14:00:00"
        
    Returns:
        True if updated successfully
    """
    try:
        with connection.cursor() as cursor:
            # Update or insert the Expiration attribute
            cursor.execute("""
                INSERT INTO public.radcheck (username, attribute, op, value, created_at, updated_at)
                VALUES (%s, 'Expiration', ':=', %s, NOW(), NOW())
                ON CONFLICT (username, attribute) 
                DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """, [username, new_expiration])
        
        logger.info(f"[EXPIRATION UPDATE] Updated {username} expiration to {new_expiration}")
        return True
        
    except Exception as e:
        logger.error(f"[EXPIRATION UPDATE] Failed for {username}: {e}")
        return False
