"""
RADIUS Celery Tasks - Background Jobs for Session Management

These tasks handle:
1. Disconnecting expired users (wall-clock expiration enforcement)
2. Cleaning up stale sessions in radacct across all tenants
3. Syncing RADIUS users across all tenants
4. Session monitoring and alerting
"""

import logging
import time
from datetime import timedelta
from celery import shared_task
from django.utils import timezone
from django.db import connection
from django.conf import settings
from django_tenants.utils import schema_context, get_tenant_model

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def disconnect_expired_users(self):
    """
    Find users with expired subscriptions who still have active RADIUS sessions,
    then kick them off MikroTik routers across all tenants.
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
    
    tenants = TenantModel.objects.exclude(schema_name='public')
    logger.info(f"[DISCONNECT TASK] Starting sweep across {tenants.count()} tenants")
    
    for tenant in tenants:
        tenant_start = time.perf_counter()
        try:
            stats['tenants_processed'] += 1
            with schema_context(tenant.schema_name):
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
                
                router_sessions = {}
                expired_users = []
                
                for row in active_sessions:
                    username, nas_ip, session_id, expiration_str = row
                    stats['checked'] += 1
                    try:
                        from datetime import datetime
                        expiration = datetime.strptime(expiration_str, "%b %d %Y %H:%M:%S")
                        expiration = timezone.make_aware(expiration)
                        
                        if expiration <= now:
                            stats['expired_found'] += 1
                            expired_users.append({
                                'username': username,
                                'nas_ip': nas_ip,
                                'session_id': session_id,
                                'tenant_schema': tenant.schema_name,
                                'expiration': expiration_str
                            })
                            if nas_ip not in router_sessions:
                                router_sessions[nas_ip] = []
                            router_sessions[nas_ip].append(username)
                    except (ValueError, TypeError):
                        continue

                for nas_ip, usernames in router_sessions.items():
                    try:
                        router = Router.objects.filter(ip_address=nas_ip, is_active=True).first()
                        if not router: continue
                        api = MikrotikAPI(router)
                        for username in usernames:
                            try:
                                result = api.disconnect_user(username, connection_type='both')
                                if result.get('pppoe') or result.get('hotspot'):
                                    stats['disconnected'] += 1
                            except Exception as e:
                                stats['errors'] += 1
                    except Exception:
                        stats['errors'] += 1

                _mark_sessions_terminated_for_tenant(expired_users, tenant.schema_name)
        except Exception as e:
            stats['errors'] += 1
            logger.error(f"[DISCONNECT TASK] Error processing tenant {tenant.schema_name}: {e}")
        finally:
            logger.info(
                "[RADIUS TASK TIMING] task=disconnect_expired_users tenant=%s duration_ms=%d",
                tenant.schema_name,
                int((time.perf_counter() - tenant_start) * 1000)
            )
    
    logger.info(f"[DISCONNECT TASK] Complete: {stats}")
    return stats


def _mark_sessions_terminated_for_tenant(expired_users: list, tenant_schema: str):
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
    except Exception as e:
        logger.error(f"Error marking sessions terminated for tenant {tenant_schema}: {e}")


@shared_task(bind=True, max_retries=2)
def cleanup_stale_sessions(self):
    """
    Closes ghost RADIUS sessions - sessions where:
    1. acctstoptime IS NULL (still "open")
    2. acctupdatetime hasn't been updated in > configured minutes (NAS stopped sending)
    
    This is the ONLY guaranteed way to catch ghost sessions without
    relying on Accounting-Stop packets from the router.
    
    FIX 1: Raised ghost threshold from 15 to 30 minutes to accommodate
    MikroTik routers with longer interim update intervals (5-15 minutes typical).
    A 30-minute threshold gives a bigger buffer before assuming a session is a ghost.
    
    FIX 3: Exclude usernames that have an active HotspotSession from being ghost-closed.
    Even if interim updates are delayed, if a HotspotSession with status='active' 
    and future expires_at exists, the radacct row stays open so users remain visible 
    in the Online tab.
    """
    from django_tenants.utils import get_tenant_model, schema_context
    
    TenantModel = get_tenant_model()
    now = timezone.now()
    
    # FIX 1: Increased ghost threshold from 15 to 30 minutes
    # This prevents premature termination of valid sessions on routers with
    # longer interim-update intervals (e.g., 5-15 minutes configured)
    ghost_minutes = int(getattr(settings, "RADIUS_GHOST_MINUTES", 30))
    stale_hours = int(getattr(settings, "RADIUS_STALE_HOURS", 4))
    
    ghost_threshold = now - timedelta(minutes=ghost_minutes)
    stale_threshold = now - timedelta(hours=stale_hours)
    
    total_ghost = 0
    total_stale = 0

    tenants = TenantModel.objects.exclude(schema_name='public').values_list('schema_name', flat=True)

    for schema_name in tenants:
        try:
            with schema_context(schema_name):
                with connection.cursor() as cursor:
                    # GHOST sessions: had interim updates but NAS went silent
                    # CRITICAL FIX: Protect ANY session where the HotspotSession
                    # subscription is still valid (active OR paid, not expired).
                    # This prevents VPN tunnel hiccups from kicking valid users.
                    cursor.execute("""
                        UPDATE radacct
                        SET
                            acctstoptime     = acctupdatetime,
                            acctterminatecause = 'NAS-Reboot',
                            acctsessiontime  = EXTRACT(EPOCH FROM (acctupdatetime - acctstarttime))::bigint
                        WHERE
                            acctstoptime IS NULL
                            AND acctupdatetime IS NOT NULL
                            AND acctupdatetime < %s
                            AND username NOT IN (
                                SELECT access_code
                                FROM billing_hotspotsession
                                WHERE status IN ('active', 'paid')
                                  AND expires_at > NOW()
                                  AND access_code IS NOT NULL
                            )
                        RETURNING radacctid
                    """, [ghost_threshold])
                    ghost_count = cursor.rowcount
                    total_ghost += ghost_count

                    # STALE sessions: never got any interim update
                    # Also protect hotspot sessions here
                    cursor.execute("""
                        UPDATE radacct
                        SET
                            acctstoptime     = NOW(),
                            acctterminatecause = 'Stale-Session-Cleanup',
                            acctsessiontime  = EXTRACT(EPOCH FROM (NOW() - acctstarttime))::bigint
                        WHERE
                            acctstoptime IS NULL
                            AND acctupdatetime IS NULL
                            AND acctstarttime < %s
                            AND username NOT IN (
                                SELECT access_code
                                FROM billing_hotspotsession
                                WHERE status IN ('active', 'paid')
                                  AND expires_at > NOW()
                                  AND access_code IS NOT NULL
                            )
                    """, [stale_threshold])
                    stale_count = cursor.rowcount
                    total_stale += stale_count

                    if ghost_count or stale_count:
                        logger.info(
                            f"[CLEANUP] {schema_name}: closed {ghost_count} ghost + "
                            f"{stale_count} stale sessions"
                        )
        except Exception as e:
            logger.error(f"[CLEANUP] Error in schema {schema_name}: {e}")

    # ── Public schema sweep ──────────────────────────────────────────────────
    # NOTE: Accounting is now tenant-correct. This public schema sweep is disabled
    # by default and only runs if ENABLE_PUBLIC_RADACCT_SWEEP=True in settings.
    if getattr(settings, "ENABLE_PUBLIC_RADACCT_SWEEP", False):
        try:
            with connection.cursor() as cursor:
                # Ghost rows: had interim updates but the NAS went silent
                cursor.execute("""
                    UPDATE public.radacct
                    SET
                        acctstoptime       = acctupdatetime,
                        acctterminatecause = 'NAS-Reboot',
                        acctsessiontime    = EXTRACT(
                            EPOCH FROM (acctupdatetime - acctstarttime)
                        )::bigint
                    WHERE
                        acctstoptime    IS NULL
                        AND acctupdatetime IS NOT NULL
                        AND acctupdatetime < %s
                        AND username NOT IN (
                            SELECT access_code
                            FROM public.radcheck
                            WHERE attribute = 'Expiration'
                            AND TO_TIMESTAMP(value, 'Mon DD YYYY HH24:MI:SS') > NOW()
                        )
                """, [ghost_threshold])
                public_ghost = cursor.rowcount
                total_ghost += public_ghost

                # Stale rows: never got any interim update at all
                cursor.execute("""
                    UPDATE public.radacct
                    SET
                        acctstoptime       = NOW(),
                        acctterminatecause = 'Stale-Session-Cleanup',
                        acctsessiontime    = EXTRACT(
                            EPOCH FROM (NOW() - acctstarttime)
                        )::bigint
                    WHERE
                        acctstoptime    IS NULL
                        AND acctupdatetime IS NULL
                        AND acctstarttime  < %s
                """, [stale_threshold])
                public_stale = cursor.rowcount
                total_stale += public_stale

                if public_ghost or public_stale:
                    logger.info(
                        "[CLEANUP] public schema: closed %d ghost + %d stale sessions",
                        public_ghost, public_stale,
                    )
        except Exception as e:
            logger.error("[CLEANUP] Error in public schema: %s", e)
    else:
        logger.debug("[CLEANUP] Public schema sweep disabled (ENABLE_PUBLIC_RADACCT_SWEEP=False)")

    logger.info(f"[CLEANUP] Done: {total_ghost} ghost + {total_stale} stale sessions closed")
    return {'ghost': total_ghost, 'stale': total_stale}


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def force_close_expired_sessions(self):
    """
    For ACTIVE sessions where the user's RADIUS Expiration has passed:
    1. Sends CoA Disconnect to the router (kicks them off immediately)
    2. Marks the session closed in radacct
    
    Runs every 5 minutes via Celery Beat.
    """
    from django_tenants.utils import get_tenant_model, schema_context
    from apps.radius.services.coa_service import CoAService

    TenantModel = get_tenant_model()
    now = timezone.now()
    
    # --- ADD THIS LOG ---
    logger.info(f"[FORCE CLOSE] Checking for expired sessions at {now}")
    
    kicked = 0
    closed = 0

    tenants = TenantModel.objects.exclude(
        schema_name='public'
    ).values_list('schema_name', flat=True)

    for schema_name in tenants:
        try:
            with schema_context(schema_name):
                with connection.cursor() as cursor:
                    # Find expired but still-open sessions
                    cursor.execute("""
                        SELECT
                            ra.username,
                            ra.nasipaddress,
                            ra.acctsessionid,
                            ra.radacctid
                        FROM radacct ra
                        INNER JOIN radcheck rc
                            ON ra.username = rc.username
                           AND rc.attribute = 'Expiration'
                        WHERE
                            ra.acctstoptime IS NULL
                            AND TO_TIMESTAMP(rc.value, 'Mon DD YYYY HH24:MI:SS') < NOW()
                        LIMIT 200
                    """)
                    rows = cursor.fetchall()

                for username, nas_ip, session_id, radacct_id in rows:
                    # 1. CoA kick (best effort — don't block on failure)
                    try:
                        coa = CoAService(nas_ip=nas_ip)
                        coa.disconnect_user_via_coa(username, nas_ip, session_id)
                        kicked += 1
                        logger.info(f"[FORCE CLOSE] CoA disconnect sent to {username}@{nas_ip}")
                    except Exception as e:
                        logger.warning(f"[FORCE CLOSE] CoA failed for {username}: {e}")

                    # 2. Close the DB record regardless of CoA result
                    with schema_context(schema_name):
                        with connection.cursor() as cursor:
                            cursor.execute("""
                                UPDATE radacct
                                SET
                                    acctstoptime = NOW(),
                                    acctterminatecause = 'Session-Timeout',
                                    acctsessiontime = EXTRACT(
                                        EPOCH FROM (NOW() - acctstarttime)
                                    )::bigint
                                WHERE radacctid = %s
                                  AND acctstoptime IS NULL
                            """, [radacct_id])
                            closed += cursor.rowcount
                            if cursor.rowcount > 0:
                                logger.info(f"[FORCE CLOSE] Closed DB record for {username}")

        except Exception as e:
            logger.error(f"[FORCE CLOSE] Error in {schema_name}: {e}")

    logger.info(f"[FORCE CLOSE] Kicked {kicked} sessions, closed {closed} records")
    return {'kicked': kicked, 'closed': closed}


@shared_task
def sync_all_radius_users():
    """
    Sync all RADIUS users from tenant schemas to public schema.
    """
    from apps.radius.services import RadiusSyncService
    TenantModel = get_tenant_model()
    stats = {'tenants_processed': 0, 'users_synced': 0, 'errors': 0}
    
    tenants = TenantModel.objects.exclude(schema_name='public')
    for tenant in tenants:
        tenant_start = time.perf_counter()
        try:
            with schema_context(tenant.schema_name):
                service = RadiusSyncService()
                result = service.sync_all_customers()
                stats['tenants_processed'] += 1
                stats['users_synced'] += result.get('total', 0)
                logger.info(f"[SYNC TASK] Synced {result.get('total', 0)} users for tenant {tenant.schema_name}")
        except Exception as e:
            stats['errors'] += 1
            logger.error(f"Error syncing tenant {tenant.schema_name}: {e}")
        finally:
            logger.info(
                "[RADIUS TASK TIMING] task=sync_all_radius_users tenant=%s duration_ms=%d",
                tenant.schema_name,
                int((time.perf_counter() - tenant_start) * 1000)
            )
    
    logger.info(f"[SYNC TASK] Complete: {stats}")
    return stats


@shared_task
def disconnect_user_immediately(username: str, router_ip: str = None, connection_type: str = 'both'):
    """
    Immediately disconnect a specific user from the network.
    """
    from apps.network.models import Router
    from apps.network.integrations.mikrotik_api import MikrotikAPI
    
    result = {'username': username, 'disconnected': False, 'routers_checked': 0, 'tenants_searched': 0, 'error': None}
    
    try:
        routers = Router.objects.filter(ip_address=router_ip, is_active=True) if router_ip else Router.objects.filter(is_active=True)
        TenantModel = get_tenant_model()
        tenants = TenantModel.objects.exclude(schema_name='public')

        for tenant in tenants:
            tenant_start = time.perf_counter()
            try:
                result['tenants_searched'] += 1  # Counter added back
                with schema_context(tenant.schema_name):
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT acctsessionid FROM radacct WHERE username = %s AND acctstoptime IS NULL", [username])
                        active_sessions = cursor.fetchall()
                    if active_sessions:
                        for session in active_sessions:
                            session_id = session[0]
                            with connection.cursor() as update_cursor:
                                update_cursor.execute("UPDATE radacct SET acctstoptime = NOW(), acctterminatecause = 'Admin-Reset' WHERE acctsessionid = %s AND acctstoptime IS NULL", [session_id])
                        result['disconnected'] = True
            except Exception as e:
                logger.warning(f"Error checking tenant {tenant.schema_name}: {e}")
            finally:
                logger.debug(
                    "[RADIUS TASK TIMING] task=disconnect_user_immediately tenant=%s duration_ms=%d",
                    tenant.schema_name,
                    int((time.perf_counter() - tenant_start) * 1000)
                )

        for router in routers:
            router_start = time.perf_counter()
            result['routers_checked'] += 1
            try:
                api = MikrotikAPI(router)
                disconnect_result = api.disconnect_user(username, connection_type)
                if disconnect_result.get('pppoe') or disconnect_result.get('hotspot'):
                    result['disconnected'] = True
            except Exception as e:
                logger.warning(f"Error on router {router.name}: {e}")
            finally:
                logger.debug(
                    "[RADIUS TASK TIMING] task=disconnect_user_immediately router=%s duration_ms=%d",
                    router.name,
                    int((time.perf_counter() - router_start) * 1000)
                )
        return result
    except Exception as e:
        result['error'] = str(e)
        return result


@shared_task
def update_user_expiration(username: str, new_expiration: str):
    """
    Update the expiration date for a RADIUS user across all tenant schemas.
    """
    TenantModel = get_tenant_model()
    results = {'username': username, 'updated_tenants': [], 'errors': []}
    tenants = TenantModel.objects.exclude(schema_name='public')
    
    for tenant in tenants:
        tenant_start = time.perf_counter()
        try:
            with schema_context(tenant.schema_name):
                with connection.cursor() as cursor:
                    cursor.execute("""
                        INSERT INTO radcheck (username, attribute, op, value, created_at, updated_at)
                        VALUES (%s, 'Expiration', ':=', %s, NOW(), NOW())
                        ON CONFLICT (username, attribute) 
                        DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
                    """, [username, new_expiration])
                    results['updated_tenants'].append(tenant.schema_name)
        except Exception as e:
            results['errors'].append({'tenant': tenant.schema_name, 'error': str(e)})
        finally:
            logger.debug(
                "[RADIUS TASK TIMING] task=update_user_expiration tenant=%s duration_ms=%d",
                tenant.schema_name,
                int((time.perf_counter() - tenant_start) * 1000)
            )
    
    return results


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def extend_user_validity(self, credentials_id: int, extend_by_plan: bool = True):
    """
    Extend a user's RADIUS validity period.
    """
    from apps.radius.models import CustomerRadiusCredentials
    result = {'success': False, 'credentials_id': credentials_id, 'username': None, 'new_expiration': None, 'error': None}
    try:
        credentials = CustomerRadiusCredentials.objects.select_related('customer__services__plan').get(id=credentials_id)
        result['username'] = credentials.username
        if extend_by_plan:
            service = credentials.customer.services.filter(status='ACTIVE', plan__isnull=False).first()
            if service and service.plan:
                new_expiration = service.plan.calculate_expiration()
                credentials.expiration_date = new_expiration
                if new_expiration:
                    result['new_expiration'] = new_expiration.strftime('%b %d %Y %H:%M:%S')
        credentials.is_enabled = True
        credentials.disabled_reason = ''
        credentials.save()
        result['success'] = True
        return result
    except Exception as e:
        result['error'] = str(e)
        self.retry(exc=e)


@shared_task
def process_expired_subscriptions():
    """
    FIXED: Now loops through all tenants to find and disable expired users.
    """
    from apps.radius.models import CustomerRadiusCredentials
    TenantModel = get_tenant_model()
    now = timezone.now()
    stats = {'checked': 0, 'expired': 0, 'disabled': 0, 'errors': 0}
    
    tenants = TenantModel.objects.exclude(schema_name='public')
    
    for tenant in tenants:
        tenant_start = time.perf_counter()
        try:
            with schema_context(tenant.schema_name):
                expired_credentials = CustomerRadiusCredentials.objects.filter(
                    is_enabled=True, 
                    expiration_date__isnull=False, 
                    expiration_date__lt=now
                )
                
                stats['checked'] += expired_credentials.count()
                
                for credentials in expired_credentials:
                    stats['expired'] += 1
                    credentials.is_enabled = False
                    credentials.disabled_reason = 'Subscription expired'
                    credentials.save()
                    stats['disabled'] += 1
                    
                    # Kick them off the router immediately
                    disconnect_user_immediately.delay(username=credentials.username, connection_type='both')
                    logger.info(f"[EXPIRED CHECK] Disabled expired user: {credentials.username} in tenant {tenant.schema_name}")
                    
        except Exception as e:
            logger.error(f"Error in process_expired_subscriptions for {tenant.schema_name}: {e}")
            stats['errors'] += 1
        finally:
            logger.info(
                "[RADIUS TASK TIMING] task=process_expired_subscriptions tenant=%s duration_ms=%d",
                tenant.schema_name,
                int((time.perf_counter() - tenant_start) * 1000)
            )
    
    logger.info(f"[EXPIRED CHECK] Complete: {stats}")
    return stats


@shared_task
def notify_expiring_soon(hours_before: int = 24):
    """
    FIXED: Now loops through all tenants to send expiration warnings.
    """
    from apps.radius.models import CustomerRadiusCredentials
    from apps.notifications.services import notification_service
    TenantModel = get_tenant_model()
    now = timezone.now()
    expiry_window = now + timedelta(hours=hours_before)
    stats = {'checked': 0, 'notified': 0, 'errors': 0}
    
    tenants = TenantModel.objects.exclude(schema_name='public')
    
    for tenant in tenants:
        tenant_start = time.perf_counter()
        try:
            with schema_context(tenant.schema_name):
                expiring_soon = CustomerRadiusCredentials.objects.filter(
                    is_enabled=True, 
                    expiration_date__isnull=False, 
                    expiration_date__gt=now, 
                    expiration_date__lte=expiry_window
                ).select_related('customer__user')
                
                stats['checked'] += expiring_soon.count()
                
                for credentials in expiring_soon:
                    try:
                        customer = credentials.customer
                        time_remaining = credentials.expiration_date - now
                        hours_left = int(time_remaining.total_seconds() // 3600)
                        notification_service.send_expiry_warning(
                            customer=customer, 
                            hours_remaining=hours_left, 
                            username=credentials.username
                        )
                        stats['notified'] += 1
                        logger.info(f"[EXPIRY NOTICE] Sent notification to {customer.customer_code}: {hours_left} hours remaining")
                    except Exception as e:
                        logger.error(f"Error notifying {credentials.username} in tenant {tenant.schema_name}: {e}")
                        stats['errors'] += 1
                        
        except Exception as e:
            logger.error(f"Error in notify_expiring_soon for {tenant.schema_name}: {e}")
            stats['errors'] += 1
        finally:
            logger.info(
                "[RADIUS TASK TIMING] task=notify_expiring_soon tenant=%s duration_ms=%d",
                tenant.schema_name,
                int((time.perf_counter() - tenant_start) * 1000)
            )
    
    logger.info(f"[EXPIRY NOTICE] Complete: {stats}")
    return stats