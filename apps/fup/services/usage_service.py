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

        # Check standard Billing Plans first
        link = (
            FUPPolicyPlan.objects
            .select_related('policy', 'plan')
            .filter(
                plan_id=plan_id,
                is_active=True,
                policy__is_active=True,
                policy__status='ACTIVE',
            )
            .first()
        )
        
        if link:
            return link.policy

        # Check Hotspot Plans (if the service connection is linked to a HotspotPlan)
        # The plan_id might be a HotspotPlan ID if this is a hotspot service
        hotspot_link = (
            FUPPolicyHotspotPlan.objects
            .select_related('policy', 'hotspot_plan')
            .filter(
                hotspot_plan_id=plan_id,
                is_active=True,
                policy__is_active=True,
                policy__status='ACTIVE',
            )
            .first()
        )

        return hotspot_link.policy if hotspot_link else None

    def resolve_window(self, policy, service_connection, now=None):
        now = now or timezone.localtime()

        if policy.reset_period == 'DAILY':
            start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)

        elif policy.reset_period == 'WEEKLY':
            start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=7)

        elif policy.reset_period == 'MONTHLY':
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            if start.month == 12:
                end = start.replace(year=start.year + 1, month=1)
            else:
                end = start.replace(month=start.month + 1)

        elif policy.reset_period == 'SUBSCRIPTION':
            # Use the actual service expiration date as the window boundary
            # This respects the actual subscription period (7-day, 30-day, etc.)
            start = service_connection.activation_date or service_connection.created_at
            
            # Get the subscription duration in days
            if service_connection.expiration_date and service_connection.activation_date:
                subscription_duration = (service_connection.expiration_date - service_connection.activation_date).days
            else:
                # Fallback to 30 days if dates aren't set properly
                subscription_duration = 30
            
            # Calculate the current window based on activation date
            # Find the window that contains 'now'
            if now >= start:
                # Calculate how many subscription periods have passed
                days_since_activation = (now - start).days
                periods_passed = days_since_activation // subscription_duration
                
                start = start + timedelta(days=periods_passed * subscription_duration)
                end = start + timedelta(days=subscription_duration)
            else:
                # Shouldn't happen in normal operation
                end = start + timedelta(days=subscription_duration)
                
        else:
            raise ValueError(f'Unsupported reset period: {policy.reset_period}')

        return start, end

    def get_radacct_usage_bytes(self, username, period_start, period_end):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    COALESCE(SUM(acctinputoctets), 0) AS upload_bytes,
                    COALESCE(SUM(acctoutputoctets), 0) AS download_bytes
                FROM radacct
                WHERE username = %s
                  AND acctstarttime < %s
                  AND COALESCE(acctstoptime, NOW()) >= %s
                """,
                [username, period_end, period_start]
            )
            row = cursor.fetchone() or (0, 0)

        upload_bytes = int(row[0] or 0)
        download_bytes = int(row[1] or 0)
        return upload_bytes, download_bytes, upload_bytes + download_bytes

    def sync_usage_for_service(self, service_connection, now=None):
        policy = self.get_active_policy_for_service(service_connection)
        if not policy:
            return None

        customer = service_connection.customer
        plan = service_connection.plan
        period_start, period_end = self.resolve_window(policy, service_connection, now=now)

        username = None
        if hasattr(customer, 'radius_credentials'):
            username = customer.radius_credentials.username

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
            usage_percent = round(Decimal(total_bytes) / Decimal(limit_bytes) * Decimal('100'), 2)

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
            }
        )

        if total_bytes > limit_bytes and not usage_window.first_exceeded_at:
            usage_window.first_exceeded_at = timezone.now()
            usage_window.status = 'VIOLATED'
            usage_window.save(update_fields=['first_exceeded_at', 'status', 'updated_at'])

        return usage_window

    def sync_usage_for_all_active_services(self):
        services = ServiceConnection.objects.select_related('customer', 'plan').filter(
            status='ACTIVE',
            plan__isnull=False,
        )

        synced = 0
        for service in services:
            window = self.sync_usage_for_service(service)
            if window:
                synced += 1
        return synced