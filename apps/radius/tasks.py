"""
RADIUS Celery Tasks - Background Jobs for Session Management

These tasks handle:
1. Disconnecting expired users (wall-clock expiration enforcement)
2. Cleaning up stale sessions in radacct across all tenants
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
    then kick them off MikroTik routers across all tenants.
    
    This task runs every 5 minutes via Celery Beat to enforce wall-clock expiration.
    
    Flow:
    1. Iterate through all tenant schemas
    2. Query radacct for sessions where acctstoptime IS NULL (still active)
    3. Cross-reference with subscription expiration in radcheck
    4. For expired users, call MikroTik API to disconnect
    5. Mark session as terminated in radacct
    
    Returns:
        Dict with statistics on users processed
    """
    from apps.network.models import Router
    from apps.network.integrations.mikrotik_api import MikrotikAPI
    
    TenantModel = get_tenant_model()
    now = timezone.now()
    
    stats = {
        'checked': 0,
        'expired_found': 0,
        'disconnected': 0,
        'errors': 0,
        'tenants_processed': 0
    }
    
    # Get all tenant schemas (exclude public)
    tenants = TenantModel.objects.exclude(schema_name='public')
    
    logger.info(f"[DISCONNECT TASK] Starting sweep across {tenants.count()} tenants")
    
    for tenant in tenants:
        try:
            stats['tenants_processed'] += 1
            
            # Enter tenant schema context
            with schema_context(tenant.schema_name):
                # Query active RADIUS sessions for this tenant
                with connection.cursor() as cursor:
                    cursor.execute("""
                        SELECT DISTINCT 
                            ra.username,
                            ra.nasipaddress,
                            ra.acctsessionid,
                            rc.value as expiration_date
                        FROM radacct ra
                        INNER JOIN radcheck rc 
                            ON ra.username = rc.username 
                            AND rc.attribute = 'Expiration'
                        WHERE ra.acctstoptime IS NULL
                            AND rc.value IS NOT NULL
                    """)
                    
                    active_sessions = cursor.fetchall()
                
                if not active_sessions:
                    continue
                
                logger.info(f"[DISCONNECT TASK] Tenant {tenant.schema_name}: Found {len(active_sessions)} active sessions to check")
                
                # Group sessions by router IP for efficient processing
                router_sessions = {}
                expired_users = []
                
                for row in active_sessions:
                    username, nas_ip, session_id, expiration_str = row
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
                                'tenant_schema': tenant.schema_name,
                                'expiration': expiration_str
                            })
                            
                            # Group by router for batch processing
                            if nas_ip not in router_sessions:
                                router_sessions[nas_ip] = []
                            router_sessions[nas_ip].append(username)
                            
                    except (ValueError, TypeError) as e:
                        logger.warning(f"[DISCONNECT TASK] Tenant {tenant.schema_name}: Could not parse expiration '{expiration_str}' for {username}: {e}")
                        continue
                
                if not expired_users:
                    continue
                
                logger.info(f"[DISCONNECT TASK] Tenant {tenant.schema_name}: Found {len(expired_users)} expired users to disconnect")
                
                # Process each router
                for nas_ip, usernames in router_sessions.items():
                    try:
                        # Find the router by IP (from public schema - routers are shared)
                        router = Router.objects.filter(ip_address=nas_ip, is_active=True).first()
                        
                        if not router:
                            logger.warning(f"[DISCONNECT TASK] Router not found for NAS IP: {nas_ip} (tenant: {tenant.schema_name})")
                            continue
                        
                        # Connect to router and disconnect users
                        api = MikrotikAPI(router)
                        
                        for username in usernames:
                            try:
                                # Try both PPPoE and Hotspot disconnect
                                result = api.disconnect_user(username, connection_type='both')
                                
                                if result.get('pppoe') or result.get('hotspot'):
                                    stats['disconnected'] += 1
                                    logger.info(f"[DISCONNECT TASK] Disconnected {username} from {router.name} (tenant: {tenant.schema_name})")
                                
                            except Exception as e:
                                stats['errors'] += 1
                                logger.error(f"[DISCONNECT TASK] Error disconnecting {username} from {router.name}: {e}")
                    
                    except Exception as e:
                        stats['errors'] += 1
                        logger.error(f"[DISCONNECT TASK] Error processing router {nas_ip} for tenant {tenant.schema_name}: {e}")
                
                # Mark sessions as terminated in this tenant's radacct
                _mark_sessions_terminated_for_tenant(expired_users, tenant.schema_name)
                
        except Exception as e:
            stats['errors'] += 1
            logger.error(f"[DISCONNECT TASK] Error processing tenant {tenant.schema_name}: {e}")
    
    logger.info(f"[DISCONNECT TASK] Complete: {stats}")
    return stats


def _mark_sessions_terminated_for_tenant(expired_users: list, tenant_schema: str):
    """
    Mark RADIUS accounting sessions as terminated in a specific tenant's radacct.
    
    Args:
        expired_users: List of dicts with username, session_id, nas_ip
        tenant_schema: The tenant schema name
    """
    try:
        with schema_context(tenant_schema):
            with connection.cursor() as cursor:
                for user in expired_users:
                    cursor.execute("""
                        UPDATE radacct 
                        SET acctstoptime = NOW(),
                            acctterminatecause = 'Session-Timeout'
                        WHERE acctsessionid = %s 
                            AND username = %s 
                            AND nasipaddress = %s
                            AND acctstoptime IS NULL
                    """, [user['session_id'], user['username'], user['nas_ip']])
        
        logger.info(f"[DISCONNECT TASK] Marked {len(expired_users)} sessions as terminated for tenant {tenant_schema}")
        
    except Exception as e:
        logger.error(f"[DISCONNECT TASK] Error marking sessions terminated for tenant {tenant_schema}: {e}")


@shared_task
def cleanup_stale_sessions():
    """
    Clean up stale RADIUS sessions that were never properly closed across all tenants.
    
    Finds sessions without stop time that are older than 24 hours
    and marks them as terminated due to NAS-Error.
    
    Returns:
        Total number of stale sessions cleaned across all tenants
    """
    TenantModel = get_tenant_model()
    cutoff_time = timezone.now() - timedelta(hours=24)
    total_cleaned = 0
    
    # 1. Get all tenant schemas (excluding public)
    tenants = TenantModel.objects.exclude(schema_name='public')
    
    for tenant in tenants:
        try:
            # 2. Enter each tenant's schema context
            with schema_context(tenant.schema_name):
                with connection.cursor() as cursor:
                    # 3. Target the tenant-specific radacct table
                    cursor.execute("""
                        UPDATE radacct 
                        SET acctstoptime = NOW(),
                            acctterminatecause = 'NAS-Error'
                        WHERE acctstoptime IS NULL 
                            AND acctstarttime < %s
                    """, [cutoff_time])
                    
                    cleaned = cursor.rowcount
                    total_cleaned += cleaned
                    if cleaned > 0:
                        logger.info(f"[CLEANUP TASK] Cleaned {cleaned} stale sessions for tenant {tenant.schema_name}")
                        
        except Exception as e:
            logger.error(f"[CLEANUP TASK] Error cleaning tenant {tenant.schema_name}: {e}")

    # 4. Optional: Still sweep public.radacct for legacy/shared visibility
    try:
        with connection.cursor() as cursor:
            cursor.execute("""
                UPDATE public.radacct 
                SET acctstoptime = NOW(),
                    acctterminatecause = 'NAS-Error'
                WHERE acctstoptime IS NULL 
                    AND acctstarttime < %s
            """, [cutoff_time])
            total_cleaned += cursor.rowcount
            if cursor.rowcount > 0:
                logger.info(f"[CLEANUP TASK] Cleaned {cursor.rowcount} stale sessions from public.radacct")
    except Exception as e:
        logger.error(f"[CLEANUP TASK] Error cleaning public.radacct: {e}")

    logger.info(f"[CLEANUP TASK] Total stale sessions cleaned: {total_cleaned}")
    return total_cleaned


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
    
    Note: This task searches across all tenant schemas to find the user's active session.
    
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
        'tenants_searched': 0,
        'error': None
    }
    
    try:
        if router_ip:
            # Specific router
            routers = Router.objects.filter(ip_address=router_ip, is_active=True)
        else:
            # Search all active routers
            routers = Router.objects.filter(is_active=True)
        
        # Search across all tenants to find and terminate the session
        TenantModel = get_tenant_model()
        tenants = TenantModel.objects.exclude(schema_name='public')
        
        for tenant in tenants:
            result['tenants_searched'] += 1
            
            try:
                with schema_context(tenant.schema_name):
                    # Check if user has an active session in this tenant
                    with connection.cursor() as cursor:
                        cursor.execute("""
                            SELECT acctsessionid, nasipaddress
                            FROM radacct
                            WHERE username = %s AND acctstoptime IS NULL
                        """, [username])
                        active_sessions = cursor.fetchall()
                    
                    if active_sessions:
                        logger.info(f"[IMMEDIATE DISCONNECT] Found {len(active_sessions)} active sessions for {username} in tenant {tenant.schema_name}")
                        
                        # Mark sessions as terminated in this tenant's radacct
                        for session in active_sessions:
                            session_id, nas_ip = session
                            with connection.cursor() as update_cursor:
                                update_cursor.execute("""
                                    UPDATE radacct 
                                    SET acctstoptime = NOW(),
                                        acctterminatecause = 'Admin-Reset'
                                    WHERE acctsessionid = %s AND acctstoptime IS NULL
                                """, [session_id])
                        
                        result['disconnected'] = True
                        
            except Exception as e:
                logger.warning(f"[IMMEDIATE DISCONNECT] Error checking tenant {tenant.schema_name}: {e}")
        
        # Disconnect from routers
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
        
        return result
        
    except Exception as e:
        result['error'] = str(e)
        logger.error(f"[IMMEDIATE DISCONNECT] Task failed for {username}: {e}")
        return result


@shared_task
def update_user_expiration(username: str, new_expiration: str):
    """
    Update the expiration date for a RADIUS user across all tenant schemas.
    
    Called when:
    - Subscription is renewed
    - Admin extends validity
    
    Args:
        username: RADIUS username
        new_expiration: New expiration in format "Feb 02 2026 14:00:00"
        
    Returns:
        Dict with update results per tenant
    """
    TenantModel = get_tenant_model()
    results = {
        'username': username,
        'updated_tenants': [],
        'errors': []
    }
    
    try:
        tenants = TenantModel.objects.exclude(schema_name='public')
        
        for tenant in tenants:
            try:
                with schema_context(tenant.schema_name):
                    with connection.cursor() as cursor:
                        # Update or insert the Expiration attribute in tenant's radcheck
                        cursor.execute("""
                            INSERT INTO radcheck (username, attribute, op, value, created_at, updated_at)
                            VALUES (%s, 'Expiration', ':=', %s, NOW(), NOW())
                            ON CONFLICT (username, attribute) 
                            DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                        """, [username, new_expiration])
                        
                        results['updated_tenants'].append(tenant.schema_name)
                        logger.info(f"[EXPIRATION UPDATE] Updated {username} expiration to {new_expiration} in tenant {tenant.schema_name}")
                        
            except Exception as e:
                results['errors'].append({'tenant': tenant.schema_name, 'error': str(e)})
                logger.error(f"[EXPIRATION UPDATE] Failed for {username} in tenant {tenant.schema_name}: {e}")
        
        # Also update public schema for backward compatibility
        try:
            with connection.cursor() as cursor:
                cursor.execute("""
                    INSERT INTO public.radcheck (username, attribute, op, value, created_at, updated_at)
                    VALUES (%s, 'Expiration', ':=', %s, NOW(), NOW())
                    ON CONFLICT (username, attribute) 
                    DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                """, [username, new_expiration])
            results['updated_tenants'].append('public')
        except Exception as e:
            results['errors'].append({'tenant': 'public', 'error': str(e)})
        
        return results
        
    except Exception as e:
        logger.error(f"[EXPIRATION UPDATE] Task failed for {username}: {e}")
        return results


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def extend_user_validity(self, credentials_id: int, extend_by_plan: bool = True):
    """
    Extend a user's RADIUS validity period.
    
    Called when:
    - Payment is received for subscription renewal
    - Admin manually extends validity
    - Automatic renewal is triggered
    
    Args:
        credentials_id: CustomerRadiusCredentials ID
        extend_by_plan: If True, extend based on the current plan's validity
                       If False, just re-enable without changing expiration
        
    Returns:
        Dict with result details
    """
    from apps.radius.models import CustomerRadiusCredentials
    
    result = {
        'success': False,
        'credentials_id': credentials_id,
        'username': None,
        'new_expiration': None,
        'error': None
    }
    
    try:
        credentials = CustomerRadiusCredentials.objects.select_related(
            'customer__services__plan'
        ).get(id=credentials_id)
        
        result['username'] = credentials.username
        
        if extend_by_plan:
            # Get the active service connection with a plan
            service = credentials.customer.services.filter(
                status='ACTIVE',
                plan__isnull=False
            ).first()
            
            if service and service.plan:
                # Calculate new expiration from plan
                new_expiration = service.plan.calculate_expiration()
                credentials.expiration_date = new_expiration
                
                if new_expiration:
                    result['new_expiration'] = new_expiration.strftime('%b %d %Y %H:%M:%S')
                    logger.info(
                        f"[EXTEND VALIDITY] Extended {credentials.username} to "
                        f"{result['new_expiration']} based on plan {service.plan.name}"
                    )
                else:
                    logger.info(
                        f"[EXTEND VALIDITY] Set {credentials.username} to unlimited validity"
                    )
            else:
                logger.warning(
                    f"[EXTEND VALIDITY] No active service with plan found for "
                    f"{credentials.username}, just enabling account"
                )
        
        # Enable the account
        credentials.is_enabled = True
        credentials.disabled_reason = ''
        credentials.save()
        
        result['success'] = True
        return result
        
    except CustomerRadiusCredentials.DoesNotExist:
        result['error'] = f"Credentials not found: {credentials_id}"
        logger.error(f"[EXTEND VALIDITY] {result['error']}")
        return result
        
    except Exception as e:
        result['error'] = str(e)
        logger.error(f"[EXTEND VALIDITY] Task failed: {e}")
        self.retry(exc=e)


@shared_task
def process_expired_subscriptions():
    """
    Process all expired subscriptions and disable RADIUS access.
    
    This is a backup task that runs every 15 minutes to catch any
    users who should have been disconnected but weren't.
    
    It checks the CustomerRadiusCredentials.expiration_date against
    the current time and disables access for expired users.
    
    Returns:
        Dict with processing statistics
    """
    from apps.radius.models import CustomerRadiusCredentials
    
    now = timezone.now()
    stats = {
        'checked': 0,
        'expired': 0,
        'disabled': 0,
        'errors': 0
    }
    
    try:
        # Find all enabled credentials with past expiration dates
        expired_credentials = CustomerRadiusCredentials.objects.filter(
            is_enabled=True,
            expiration_date__isnull=False,
            expiration_date__lt=now
        )
        
        stats['checked'] = expired_credentials.count()
        
        for credentials in expired_credentials:
            try:
                stats['expired'] += 1
                
                # Disable the credentials
                credentials.is_enabled = False
                credentials.disabled_reason = 'Subscription expired'
                credentials.save()
                
                stats['disabled'] += 1
                logger.info(
                    f"[EXPIRED CHECK] Disabled expired user: {credentials.username}, "
                    f"expired at {credentials.expiration_date}"
                )
                
                # Optionally disconnect from router immediately
                disconnect_user_immediately.delay(
                    username=credentials.username,
                    connection_type='both'
                )
                
            except Exception as e:
                stats['errors'] += 1
                logger.error(f"[EXPIRED CHECK] Error processing {credentials.username}: {e}")
        
        logger.info(f"[EXPIRED CHECK] Complete: {stats}")
        return stats
        
    except Exception as e:
        logger.error(f"[EXPIRED CHECK] Task failed: {e}")
        return stats


@shared_task
def notify_expiring_soon(hours_before: int = 24):
    """
    Send notifications to customers whose subscriptions are expiring soon.
    
    Args:
        hours_before: Number of hours before expiration to send notification
        
    Returns:
        Dict with notification statistics
    """
    from apps.radius.models import CustomerRadiusCredentials
    from apps.notifications.services import notification_service
    
    now = timezone.now()
    expiry_window = now + timedelta(hours=hours_before)
    
    stats = {
        'checked': 0,
        'notified': 0,
        'errors': 0
    }
    
    try:
        # Find credentials expiring in the next X hours
        expiring_soon = CustomerRadiusCredentials.objects.filter(
            is_enabled=True,
            expiration_date__isnull=False,
            expiration_date__gt=now,
            expiration_date__lte=expiry_window
        ).select_related('customer__user')
        
        stats['checked'] = expiring_soon.count()
        
        for credentials in expiring_soon:
            try:
                customer = credentials.customer
                time_remaining = credentials.expiration_date - now
                hours_left = int(time_remaining.total_seconds() // 3600)
                
                # Send SMS/Email notification
                notification_service.send_expiry_warning(
                    customer=customer,
                    hours_remaining=hours_left,
                    username=credentials.username
                )
                
                stats['notified'] += 1
                logger.info(
                    f"[EXPIRY NOTICE] Sent notification to {customer.customer_code}: "
                    f"{hours_left} hours remaining"
                )
                
            except Exception as e:
                stats['errors'] += 1
                logger.error(f"[EXPIRY NOTICE] Error notifying {credentials.username}: {e}")
        
        return stats
        
    except Exception as e:
        logger.error(f"[EXPIRY NOTICE] Task failed: {e}")
        return stats