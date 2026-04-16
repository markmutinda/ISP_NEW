from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.utils import timezone

from apps.customers.models import ServiceConnection
from apps.fup.models import FUPPolicy, FUPPolicyPlan, FUPUsageWindow, FUPPolicyHotspotPlan


class FUPUsageService:

    def get_active_policy_for_service(self, service_connection):
        plan_id = service_connection.plan_id
        if not plan_id:
            return None

        link = (
            FUPPolicyPlan.objects
            .select_related('policy')
            .filter(
                plan_id=plan_id,
                is_active=True,
                policy__is_active=True,
                policy__status='ACTIVE',
            )
            .first()
        )
        return link.policy if link else None

    def get_active_policy_for_hotspot_session(self, hotspot_session):
        """Return the FUP policy linked to this session's HotspotPlan, if any."""
        plan_id = hotspot_session.plan_id
        if not plan_id:
            return None

        link = (
            FUPPolicyHotspotPlan.objects
            .select_related('policy')
            .filter(
                hotspot_plan_id=plan_id,
                is_active=True,
                policy__is_active=True,
                policy__status='ACTIVE',
            )
            .first()
        )
        return link.policy if link else None

    def resolve_window(self, policy, service_connection=None, activation_date=None, now=None):
        """
        Compute the current usage window period.
        Accepts either a ServiceConnection or a raw activation_date.
        """
        now = now or timezone.localtime()

        if policy.reset_period == 'DAILY':
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)

        elif policy.reset_period == 'WEEKLY':
            start = (now - timedelta(days=now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            end = start + timedelta(days=7)

        elif policy.reset_period == 'MONTHLY':
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if start.month == 12:
                end = start.replace(year=start.year + 1, month=1)
            else:
                end = start.replace(month=start.month + 1)

        elif policy.reset_period == 'SUBSCRIPTION':
            if service_connection:
                activation = (
                    service_connection.activation_date
                    or service_connection.created_at
                )
                plan_duration = getattr(service_connection.plan, 'validity_days', 30) or 30
            elif activation_date:
                activation = activation_date
                plan_duration = 30
            else:
                activation = now
                plan_duration = 30

            if now >= activation:
                days_since = (now - activation).days
                periods_passed = days_since // plan_duration
                start = activation + timedelta(days=periods_passed * plan_duration)
                end = start + timedelta(days=plan_duration)
            else:
                start = activation
                end = start + timedelta(days=plan_duration)
        else:
            raise ValueError(f'Unsupported reset period: {policy.reset_period}')

        return start, end

    def get_radacct_usage_bytes(self, username, period_start, period_end):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    COALESCE(SUM(acctinputoctets), 0)  AS upload_bytes,
                    COALESCE(SUM(acctoutputoctets), 0) AS download_bytes
                FROM radacct
                WHERE username = %s
                  AND acctstarttime < %s
                  AND COALESCE(acctstoptime, NOW()) >= %s
                """,
                [username, period_end, period_start],
            )
            row = cursor.fetchone() or (0, 0)

        upload_bytes = int(row[0] or 0)
        download_bytes = int(row[1] or 0)
        return upload_bytes, download_bytes, upload_bytes + download_bytes

    def _get_username_for_service(self, service_connection):
        customer = service_connection.customer
        if hasattr(customer, 'radius_credentials'):
            return customer.radius_credentials.username
        return None

    def sync_usage_for_service(self, service_connection, now=None):
        policy = self.get_active_policy_for_service(service_connection)
        if not policy:
            return None

        customer = service_connection.customer
        plan = service_connection.plan
        period_start, period_end = self.resolve_window(
            policy, service_connection=service_connection, now=now
        )

        username = self._get_username_for_service(service_connection)
        if not username:
            return None

        upload_bytes, download_bytes, total_bytes = self.get_radacct_usage_bytes(
            username=username,
            period_start=period_start,
            period_end=period_end,
        )

        limit_bytes = policy.limit_bytes
        usage_percent = Decimal('0.00')
        if limit_bytes > 0:
            usage_percent = round(
                Decimal(total_bytes) / Decimal(limit_bytes) * Decimal('100'), 2
            )

        usage_window, _ = FUPUsageWindow.objects.update_or_create(
            policy=policy,
            service_connection=service_connection,
            period_start=period_start,
            period_end=period_end,
            defaults={
                'plan': plan,
                'customer': customer,
                'download_bytes': download_bytes,
                'upload_bytes': upload_bytes,
                'total_bytes': total_bytes,
                'limit_bytes': limit_bytes,
                'usage_percent': usage_percent,
                'last_accounting_update_at': timezone.now(),
            },
        )

        if total_bytes > limit_bytes and not usage_window.first_exceeded_at:
            usage_window.first_exceeded_at = timezone.now()
            usage_window.status = 'VIOLATED'
            usage_window.save(update_fields=['first_exceeded_at', 'status', 'updated_at'])

        return usage_window

    # ─── NEW: Hotspot session FUP tracking ────────────────────────────────

    def sync_usage_for_hotspot_session(self, hotspot_session, now=None):
        """
        Track FUP usage for a single active HotspotSession.
        Uses the session's access_code as the RADIUS username.
        """
        policy = self.get_active_policy_for_hotspot_session(hotspot_session)
        if not policy:
            return None

        username = hotspot_session.access_code
        if not username:
            return None

        now = now or timezone.now()
        # Hotspot sessions always use SUBSCRIPTION period anchored to activated_at
        activation = hotspot_session.activated_at or hotspot_session.created_at
        period_start, period_end = self.resolve_window(
            policy, activation_date=activation, now=now
        )

        upload_bytes, download_bytes, total_bytes = self.get_radacct_usage_bytes(
            username=username,
            period_start=period_start,
            period_end=period_end,
        )

        limit_bytes = policy.limit_bytes
        usage_percent = Decimal('0.00')
        if limit_bytes > 0:
            usage_percent = round(
                Decimal(total_bytes) / Decimal(limit_bytes) * Decimal('100'), 2
            )

        # FUPUsageWindow requires a ServiceConnection FK — hotspot sessions don't
        # have one.  We use a sentinel service_connection tied to the customer if
        # available; otherwise we skip writing a window row and only return stats.
        # The enforcement path handles throttle via RADIUS username directly.
        # If you later add a nullable service_connection field to FUPUsageWindow,
        # this can be revisited.  For now return a lightweight dict.
        return {
            'username': username,
            'policy': policy,
            'total_bytes': total_bytes,
            'limit_bytes': limit_bytes,
            'usage_percent': float(usage_percent),
            'exceeded': total_bytes > limit_bytes,
            'period_start': period_start,
            'period_end': period_end,
        }

    def sync_usage_for_all_active_services(self):
        synced = 0
        services = ServiceConnection.objects.select_related(
            'customer', 'plan'
        ).filter(status='ACTIVE', plan__isnull=False)

        for service in services:
            window = self.sync_usage_for_service(service)
            if window:
                synced += 1
        return synced

    def sync_usage_for_all_active_hotspot_sessions(self):
        """Sync FUP usage for every currently-active hotspot session."""
        from apps.billing.models.hotspot_models import HotspotSession
        now = timezone.now()
        synced = 0

        sessions = HotspotSession.objects.filter(
            status='active', expires_at__gt=now
        ).select_related('plan')

        for session in sessions:
            result = self.sync_usage_for_hotspot_session(session, now=now)
            if result:
                synced += 1
        return synced