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
from datetime import timedelta, datetime, timezone as dt_timezone
from celery import shared_task
from django.utils import timezone
from django.db import connection, IntegrityError, transaction as db_transaction
from django.db.models import Q
from django.conf import settings
from django_tenants.utils import schema_context, get_tenant_model

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def disconnect_expired_users(self):
    """
    Find users with expired subscriptions who still have active RADIUS sessions,
    then kick them off MikroTik routers across all tenants.
    
    FIX: The expiration string is stored as UTC. Using timezone.make_aware()
    would interpret the UTC string as Africa/Nairobi time, making every session
    appear expired 3 hours early. Now we treat it as UTC directly.
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
                        # ── FIX: The expiration string is stored as UTC.
                        # DO NOT use timezone.make_aware() here — it would
                        # interpret the UTC string as Africa/Nairobi time,
                        # making every session appear expired 3 hours early.
                        expiration = datetime.strptime(expiration_str, "%b %d %Y %H:%M:%S")
                        expiration = expiration.replace(tzinfo=dt_timezone.utc)  # ← CHANGED LINE
                        
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
    Closes ghost RADIUS sessions ONLY when the user has no active subscription.
    
    Rules:
    - PPPoE users: protected if they have a valid Expiration attribute in the future
    - Hotspot users: protected if they have an active HotspotSession (status=active/paid, expires_at > now)
    - If user has active subscription → NEVER close their session, even if NAS goes silent
    - Only close if subscription is expired OR user has no subscription record at all
    """
    from django_tenants.utils import get_tenant_model, schema_context
    
    TenantModel = get_tenant_model()
    now = timezone.now()
    
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

                    # ── GHOST sessions ──────────────────────────────────────────
                    # Had interim updates but NAS went silent.
                    # ONLY close if the user has NO active subscription.
                    # A user with active sub whose router rebooted should NOT be evicted.
                    cursor.execute("""
                        UPDATE radacct
                        SET
                            acctstoptime       = acctupdatetime,
                            acctterminatecause = 'NAS-Reboot',
                            acctsessiontime    = EXTRACT(
                                EPOCH FROM (acctupdatetime - acctstarttime)
                            )::bigint
                        WHERE
                            acctstoptime IS NULL
                            AND acctupdatetime IS NOT NULL
                            AND acctupdatetime < %s
                            -- Protect PPPoE users with future Expiration attribute
                            AND username NOT IN (
                                SELECT DISTINCT rc.username
                                FROM radcheck rc
                                WHERE rc.attribute = 'Expiration'
                                  AND TO_TIMESTAMP(rc.value, 'Mon DD YYYY HH24:MI:SS') > NOW()
                            )
                            -- Protect hotspot users with active/paid sessions
                            AND username NOT IN (
                                SELECT access_code
                                FROM billing_hotspotsession
                                WHERE status IN ('active', 'paid')
                                  AND expires_at > NOW()
                                  AND access_code IS NOT NULL
                            )
                    """, [ghost_threshold])
                    ghost_count = cursor.rowcount
                    total_ghost += ghost_count

                    # ── STALE sessions ─────────────────────────────────────────
                    # Never received any interim update.
                    # Same protection rules apply.
                    cursor.execute("""
                        UPDATE radacct
                        SET
                            acctstoptime       = NOW(),
                            acctterminatecause = 'Stale-Session-Cleanup',
                            acctsessiontime    = EXTRACT(
                                EPOCH FROM (NOW() - acctstarttime)
                            )::bigint
                        WHERE
                            acctstoptime IS NULL
                            AND acctupdatetime IS NULL
                            AND acctstarttime < %s
                            -- Protect PPPoE users with future Expiration attribute
                            AND username NOT IN (
                                SELECT DISTINCT rc.username
                                FROM radcheck rc
                                WHERE rc.attribute = 'Expiration'
                                  AND TO_TIMESTAMP(rc.value, 'Mon DD YYYY HH24:MI:SS') > NOW()
                            )
                            -- Protect hotspot users with active/paid sessions
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
                            f"{stale_count} stale sessions (subscription-expired only)"
                        )
        except Exception as e:
            logger.error(f"[CLEANUP] Error in schema {schema_name}: {e}")

    # ── Public schema sweep ──────────────────────────────────────────────────
    # This sweep protects PPPoE users via Expiration attribute check.
    # Hotspot protection is not applicable in public schema.
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
                            SELECT DISTINCT rc.username
                            FROM public.radcheck rc
                            WHERE rc.attribute = 'Expiration'
                              AND TO_TIMESTAMP(rc.value, 'Mon DD YYYY HH24:MI:SS') > NOW()
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
                        AND username NOT IN (
                            SELECT DISTINCT rc.username
                            FROM public.radcheck rc
                            WHERE rc.attribute = 'Expiration'
                              AND TO_TIMESTAMP(rc.value, 'Mon DD YYYY HH24:MI:SS') > NOW()
                        )
                """, [stale_threshold])
                public_stale = cursor.rowcount
                total_stale += public_stale

                if public_ghost or public_stale:
                    logger.info(
                        "[CLEANUP] public schema: closed %d ghost + %d stale sessions (subscription-expired only)",
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

    Routers are tenant-scoped tables in this project.  Keep every Router
    lookup inside the same tenant schema that owns the user's radacct rows;
    otherwise the lazy QuerySet is evaluated later in the public schema and
    PostgreSQL raises: relation "network_router" does not exist.
    """
    from apps.network.models import Router
    from apps.network.integrations.mikrotik_api import MikrotikAPI

    result = {
        'username': username,
        'disconnected': False,
        'routers_checked': 0,
        'tenants_searched': 0,
        'error': None,
    }

    TenantModel = get_tenant_model()
    tenants = TenantModel.objects.exclude(schema_name='public')

    for tenant in tenants:
        tenant_start = time.perf_counter()
        try:
            result['tenants_searched'] += 1

            with schema_context(tenant.schema_name):
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT DISTINCT acctsessionid, nasipaddress
                        FROM radacct
                        WHERE username = %s
                          AND acctstoptime IS NULL
                        """,
                        [username],
                    )
                    active_sessions = cursor.fetchall()

                if not active_sessions:
                    continue

                with connection.cursor() as update_cursor:
                    for session_id, _nas_ip in active_sessions:
                        update_cursor.execute(
                            """
                            UPDATE radacct
                            SET acctstoptime = NOW(),
                                acctterminatecause = 'Admin-Reset'
                            WHERE acctsessionid = %s
                              AND username = %s
                              AND acctstoptime IS NULL
                            """,
                            [session_id, username],
                        )

                result['disconnected'] = True

                nas_ips = {
                    nas_ip
                    for _session_id, nas_ip in active_sessions
                    if nas_ip
                }

                routers = Router.objects.filter(is_active=True)

                if router_ip:
                    routers = routers.filter(
                        Q(ip_address=router_ip) | Q(vpn_ip_address=router_ip)
                    )
                elif nas_ips:
                    routers = routers.filter(
                        Q(ip_address__in=nas_ips) | Q(vpn_ip_address__in=nas_ips)
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
                        logger.warning(
                            "Error disconnecting user %s on router %s in tenant %s: %s",
                            username,
                            router.name,
                            tenant.schema_name,
                            e,
                        )

                    finally:
                        logger.debug(
                            "[RADIUS TASK TIMING] task=disconnect_user_immediately "
                            "tenant=%s router=%s duration_ms=%d",
                            tenant.schema_name,
                            router.name,
                            int((time.perf_counter() - router_start) * 1000),
                        )

        except Exception as e:
            result['error'] = str(e)

            logger.warning(
                "Error disconnecting user %s in tenant %s: %s",
                username,
                tenant.schema_name,
                e,
            )

        finally:
            logger.debug(
                "[RADIUS TASK TIMING] task=disconnect_user_immediately tenant=%s duration_ms=%d",
                tenant.schema_name,
                int((time.perf_counter() - tenant_start) * 1000),
            )

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
    Disables expired RADIUS credentials and sends a ONE-TIME
    'subscription expired' notice (pppoe_expiry_notification toggle).
    
    Each credential only crosses the expiry threshold once per renewal cycle
    since expiration_date is moved forward on renewal.
    """
    from apps.radius.models import CustomerRadiusCredentials
    from apps.messaging.services.notification_sender import SMSNotifier
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
                ).select_related('customer__user', 'bandwidth_profile')
                
                stats['checked'] += expired_credentials.count()
                
                for credentials in expired_credentials:
                    stats['expired'] += 1
                    credentials.is_enabled = False
                    credentials.disabled_reason = 'Subscription expired'
                    credentials.save()
                    stats['disabled'] += 1
                    
                    # Kick them off the router immediately
                    disconnect_user_immediately.delay(username=credentials.username, connection_type='both')
                    
                    # ============================================================
                    # ONE-TIME "subscription expired" SMS
                    # Fires exactly once per expiry cycle because the credential
                    # is disabled after this and won't be processed again until
                    # the next renewal moves expiration_date forward.
                    # ============================================================
                    try:
                        plan_name = (
                            credentials.bandwidth_profile.name
                            if credentials.bandwidth_profile else ""
                        )
                        SMSNotifier.pppoe_expired_notice(
                            customer=credentials.customer,
                            plan_name=plan_name,
                        )
                        logger.info(
                            f"[EXPIRED CHECK] Sent expiry notice SMS to "
                            f"{credentials.customer.customer_code} for plan {plan_name}"
                        )
                    except Exception as sms_err:
                        logger.warning(
                            f"[EXPIRED CHECK] Expiry notice SMS failed for "
                            f"{credentials.username}: {sms_err}"
                        )

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
    Loops through all tenants to send automated expiration warnings.
    Swapped from NotificationManager to your production SMSNotifier library.
    """
    from apps.radius.models import CustomerRadiusCredentials
    from apps.messaging.services.notification_sender import SMSNotifier
    
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
                ).select_related('customer__user', 'bandwidth_profile')
                
                stats['checked'] += expiring_soon.count()
                
                for credentials in expiring_soon:
                    try:
                        customer = credentials.customer
                        time_remaining = credentials.expiration_date - now
                        
                        # Convert to days remaining for the standard template alignment
                        days_left = max(1, int(time_remaining.total_seconds() / 86400))
                        plan_name = credentials.bandwidth_profile.name if credentials.bandwidth_profile else "Internet Plan"
                        
                        # Trigger the working template framework
                        SMSNotifier.pppoe_expiry_reminder(
                            customer=customer,
                            days_left=days_left,
                            plan_name=plan_name,
                        )
                        stats['notified'] += 1
                        logger.info(f"[EXPIRY NOTICE] Sent SMS notification to {customer.customer_code}")
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


# ════════════════════════════════════════════════════════════════════════════
# PPPOE EXPIRY REMINDERS (SMS) — Multi‑interval support with DB-level dedup
# ════════════════════════════════════════════════════════════════════════════

def _normalise_pppoe_expiry_reminders(settings_obj):
    """Convert pppoe_expiry_intervals JSON to sorted list with hours."""
    raw = getattr(settings_obj, 'pppoe_expiry_intervals', None) or []

    if not raw:
        # fallback to old single value
        raw = [{'value': 4, 'unit': 'days'}]

    reminders = []
    for item in raw:
        try:
            value = int(item.get('value'))
            unit = str(item.get('unit', '')).lower().strip()
            if value <= 0:
                continue
            if unit in ('day', 'days'):
                hours = value * 24
                key = f'{value}d'
            elif unit in ('hour', 'hours'):
                hours = value
                key = f'{value}h'
            else:
                continue
            reminders.append({'key': key, 'hours': hours, 'value': value, 'unit': unit})
        except Exception:
            continue

    # deduplicate by key
    unique = {r['key']: r for r in reminders}
    # furthest first: 4d, 3d, 5h
    return sorted(unique.values(), key=lambda x: x['hours'], reverse=True)


def _get_due_reminder(reminders, remaining_hours):
    """
    Pick exactly ONE reminder bucket for current remaining time.
    reminders must be sorted furthest-first.
    """
    for index, reminder in enumerate(reminders):
        current_hours = reminder['hours']
        next_hours = reminders[index + 1]['hours'] if index + 1 < len(reminders) else 0
        if next_hours < remaining_hours <= current_hours:
            return reminder
    return None


@shared_task(name='apps.radius.tasks.send_pppoe_expiry_reminders')
def send_pppoe_expiry_reminders():
    """
    Send PPPoE expiry reminders with DB-level dedup.
    Runs hourly. Each customer gets exactly one SMS per interval per subscription period.
    
    FIX: Skip customers who renewed within the last hour to prevent
    sending expiry reminders immediately after payment/renewal.
    """
    from django.db import IntegrityError, transaction as db_transaction
    TenantModel = get_tenant_model()

    for tenant in TenantModel.objects.exclude(schema_name='public'):
        try:
            with schema_context(tenant.schema_name):
                from apps.messaging.models import SMSNotificationSettings
                from apps.messaging.services.notification_sender import SMSNotifier
                from apps.radius.models import CustomerRadiusCredentials, RadiusExpiryReminderLog

                s = SMSNotificationSettings.get_settings()
                if not s.pppoe_expiry_reminder:
                    continue

                # Clean up old logs (older than 60 days)
                cutoff = timezone.now() - timedelta(days=60)
                RadiusExpiryReminderLog.objects.filter(sent_at__lt=cutoff).delete()

                reminders = _normalise_pppoe_expiry_reminders(s)
                if not reminders:
                    continue

                now = timezone.now()
                max_hours = reminders[0]['hours']

                # Only fetch users expiring within the largest threshold
                expiring = CustomerRadiusCredentials.objects.filter(
                    is_enabled=True,
                    expiration_date__isnull=False,
                    expiration_date__gt=now,
                    expiration_date__lte=now + timedelta(hours=max_hours),
                ).select_related('customer__user', 'bandwidth_profile')

                for cred in expiring:
                    try:
                        # ============================================================
                        # FIX: Skip customers who renewed within the last hour
                        # This prevents sending expiry reminders right after payment
                        # ============================================================
                        if cred.subscription_activated_at:
                            minutes_since_renewal = (now - cred.subscription_activated_at).total_seconds() / 60
                            if minutes_since_renewal < 60:
                                logger.debug(
                                    f"[EXPIRY] Skipping {cred.username} - "
                                    f"renewed {minutes_since_renewal:.0f} minutes ago (grace period)"
                                )
                                continue
                        
                        remaining_hours = (cred.expiration_date - now).total_seconds() / 3600
                        due = _get_due_reminder(reminders, remaining_hours)

                        if not due:
                            continue

                        customer = cred.customer
                        reminder_key = due['key']

                        # Normalize to minute precision - microseconds cause false cache misses
                        expiry_normalized = cred.expiration_date.replace(second=0, microsecond=0)

                        # DB-level dedup — unique constraint blocks duplicates
                        try:
                            with db_transaction.atomic():
                                RadiusExpiryReminderLog.objects.create(
                                    customer_id=str(customer.id),
                                    interval_key=reminder_key,
                                    expiration_date=expiry_normalized,
                                    username=cred.username,
                                )
                        except IntegrityError:
                            # Already sent for this customer+interval+expiry
                            logger.debug(
                                f"[EXPIRY] Skipping {customer.id} "
                                f"interval={reminder_key} — already sent"
                            )
                            continue

                        # Send SMS
                        plan_name = (
                            cred.bandwidth_profile.name
                            if cred.bandwidth_profile else ""
                        )
                        days_left = due['value'] if due['unit'] in ('day', 'days') else 0

                        sent = SMSNotifier.pppoe_expiry_reminder(
                            customer=customer,
                            days_left=days_left,
                            plan_name=plan_name,
                        )

                        logger.info(
                            f"[EXPIRY] {reminder_key} reminder for "
                            f"{customer.customer_code} sent={sent}"
                        )

                    except Exception as e:
                        logger.warning(
                            f"[EXPIRY] Failed for "
                            f"{getattr(cred, 'username', '?')}: {e}"
                        )

        except Exception as e:
            logger.error(
                f"[EXPIRY] Error in {tenant.schema_name}: {e}"
            )

    return {'status': 'done'}