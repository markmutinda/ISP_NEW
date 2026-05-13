from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.utils import timezone

from apps.customers.models import ServiceConnection
from apps.fup.models import FUPPolicy, FUPPolicyPlan, FUPUsageWindow, FUPPolicyHotspotPlan

import pytz

NAIROBI_TZ = pytz.timezone('Africa/Nairobi')


class FUPUsageService:

    # ─── Policy Resolution ────────────────────────────────────────────────────

    def get_active_policy_for_service(self, service_connection):
        plan_id = service_connection.plan_id
        if not plan_id:
            return None
        link = (
            FUPPolicyPlan.objects
            .select_related('policy')
            .filter(plan_id=plan_id, is_active=True, policy__is_active=True, policy__status='ACTIVE')
            .first()
        )
        return link.policy if link else None

    def get_active_policy_for_hotspot_session(self, hotspot_session):
        plan_id = hotspot_session.plan_id
        if not plan_id:
            return None
        link = (
            FUPPolicyHotspotPlan.objects
            .select_related('policy')
            .filter(hotspot_plan_id=plan_id, is_active=True, policy__is_active=True, policy__status='ACTIVE')
            .first()
        )
        return link.policy if link else None

    # ─── Peak Hours Helper ────────────────────────────────────────────────────

    def _is_peak_hour(self, policy, now=None) -> bool:
        """Check if current Nairobi EAT time is within peak hours."""
        if not policy.peak_hour_start or not policy.peak_hour_end:
            return True  # No config → always active

        now = now or timezone.now()
        now_eat = now.astimezone(NAIROBI_TZ)
        current = now_eat.time()
        start = policy.peak_hour_start
        end = policy.peak_hour_end

        if start <= end:
            # Normal range e.g. 19:00–22:00
            return start <= current <= end
        else:
            # Overnight e.g. 22:00–02:00
            return current >= start or current <= end

    # ─── Window Resolution ────────────────────────────────────────────────────

    def resolve_window(self, policy, service_connection=None, activation_date=None, now=None):
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
            end = start.replace(month=start.month % 12 + 1) if start.month < 12 else start.replace(year=start.year + 1, month=1)

        elif policy.reset_period == 'PEAK_HOURS':
            # Daily window but only accumulate during peak hours
            now_eat = now.astimezone(NAIROBI_TZ)
            start = now_eat.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)

        elif policy.reset_period == 'SUBSCRIPTION':
            if service_connection:
                activation = service_connection.activation_date or service_connection.created_at
                plan_duration = getattr(service_connection.plan, 'validity_days', 30) or 30
            elif activation_date:
                activation = activation_date
                # For hotspot sessions, we need to use the plan's actual duration
                # This will be overridden by the caller for hotspot sessions
                plan_duration = 30  # Default fallback
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

    # ─── Hotspot Subscription Window Resolution ──────────────────────────────

    def _resolve_subscription_window_for_hotspot(self, activated_at, total_minutes, now):
        """
        Resolve subscription window for hotspot plans using minutes instead of days.
        
        This is critical for 1-hour, 6-hour, 12-hour, and multi-day hotspot plans
        that don't align with the 30-day default used for PPPoE subscriptions.
        
        Args:
            activated_at: When the session was activated
            total_minutes: Total validity period in minutes (e.g., 60 for 1 hour)
            now: Current datetime
        
        Returns:
            tuple: (period_start, period_end) for the current subscription period
        """
        delta = timedelta(minutes=total_minutes)
        elapsed = now - activated_at
        periods_passed = int(elapsed.total_seconds() // delta.total_seconds())
        period_start = activated_at + (delta * periods_passed)
        period_end = period_start + delta
        return period_start, period_end

    # ─── RADIUS Usage Query ───────────────────────────────────────────────────

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
        return int(row[0] or 0), int(row[1] or 0), int(row[0] or 0) + int(row[1] or 0)

    def _get_username_for_service(self, service_connection):
        customer = service_connection.customer
        if hasattr(customer, 'radius_credentials'):
            return customer.radius_credentials.username
        return None

    # ─── PPPoE / Static Sync ─────────────────────────────────────────────────

    def sync_usage_for_service(self, service_connection, now=None):
        policy = self.get_active_policy_for_service(service_connection)
        if not policy:
            return None

        customer = service_connection.customer
        plan = service_connection.plan
        period_start, period_end = self.resolve_window(policy, service_connection=service_connection, now=now)

        username = self._get_username_for_service(service_connection)
        if not username:
            return None

        upload_bytes, download_bytes, total_bytes = self.get_radacct_usage_bytes(
            username=username, period_start=period_start, period_end=period_end,
        )

        limit_bytes = policy.limit_bytes
        usage_percent = Decimal('0.00')
        if limit_bytes > 0:
            usage_percent = round(Decimal(total_bytes) / Decimal(limit_bytes) * Decimal('100'), 2)

        usage_window, _ = FUPUsageWindow.objects.update_or_create(
            policy=policy,
            service_connection=service_connection,
            period_start=period_start,
            period_end=period_end,
            defaults={
                'plan': plan,
                'customer': customer,
                'hotspot_session': None,
                'hotspot_plan': None,
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

    # ─── Hotspot Sync ─────────────────────────────────────────────────────────

    def sync_usage_for_hotspot_session(self, hotspot_session, now=None):
        """
        FIX: Now creates a proper FUPUsageWindow so the dashboard shows hotspot users.
        Also respects PEAK_HOURS — skips tracking outside peak window.
        FIX: Properly handles SUBSCRIPTION reset period using hotspot plan duration.
        """
        policy = self.get_active_policy_for_hotspot_session(hotspot_session)
        if not policy:
            return None

        username = hotspot_session.access_code
        if not username:
            return None

        now = now or timezone.now()

        # Peak hours gate: only track during configured peak window
        if policy.reset_period == 'PEAK_HOURS' and not self._is_peak_hour(policy, now):
            return None

        activation = hotspot_session.activated_at or hotspot_session.created_at
        
        # 🆕 FIX: For SUBSCRIPTION reset period, use hotspot plan's actual duration
        if policy.reset_period == 'SUBSCRIPTION' and hotspot_session.plan:
            plan = hotspot_session.plan
            # Get total validity in minutes from the hotspot plan
            total_minutes = plan.total_validity_minutes or 60  # Default 60 min (1 hour)
            period_start, period_end = self._resolve_subscription_window_for_hotspot(
                activation, total_minutes, now
            )
        else:
            # For other reset periods (DAILY, WEEKLY, MONTHLY, PEAK_HOURS)
            period_start, period_end = self.resolve_window(
                policy, activation_date=activation, now=now
            )

        upload_bytes, download_bytes, total_bytes = self.get_radacct_usage_bytes(
            username=username, period_start=period_start, period_end=period_end,
        )

        limit_bytes = policy.limit_bytes
        usage_percent = Decimal('0.00')
        if limit_bytes > 0:
            usage_percent = round(Decimal(total_bytes) / Decimal(limit_bytes) * Decimal('100'), 2)

        # Create actual FUPUsageWindow for dashboard visibility
        usage_window, _ = FUPUsageWindow.objects.update_or_create(
            policy=policy,
            hotspot_session=hotspot_session,
            period_start=period_start,
            period_end=period_end,
            defaults={
                'hotspot_plan': hotspot_session.plan,
                'plan': None,
                'service_connection': None,
                'customer': None,
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

    # ─── Bulk Sync ────────────────────────────────────────────────────────────

    def sync_usage_for_all_active_services(self):
        synced = 0
        for service in ServiceConnection.objects.select_related('customer', 'plan').filter(status='ACTIVE', plan__isnull=False):
            if self.sync_usage_for_service(service):
                synced += 1
        return synced

    def sync_usage_for_all_active_hotspot_sessions(self):
        from apps.billing.models.hotspot_models import HotspotSession
        now = timezone.now()
        synced = 0
        for session in HotspotSession.objects.filter(status='active', expires_at__gt=now).select_related('plan'):
            if self.sync_usage_for_hotspot_session(session, now=now):
                synced += 1
        return synced