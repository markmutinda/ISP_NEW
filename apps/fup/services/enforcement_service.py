import logging
from django.utils import timezone
from apps.fup.models import FUPThrottleState, FUPViolation
from .radius_service import FUPRadiusService
from .usage_service import FUPUsageService

logger = logging.getLogger(__name__)


class FUPEnforcementService:
    def __init__(self):
        self.usage_service = FUPUsageService()
        self.radius_service = FUPRadiusService()

    # ─── PPPoE / Static ───────────────────────────────────────────────────────

    def evaluate_service(self, service_connection):
        usage_window = self.usage_service.sync_usage_for_service(service_connection)
        if not usage_window:
            return None

        if usage_window.total_bytes <= usage_window.limit_bytes:
            if usage_window.is_throttled:
                self.release_service(service_connection, reason='Usage back within limit or period reset')
            return usage_window

        self.throttle_service(usage_window)
        return usage_window

    def throttle_service(self, usage_window):
        service = usage_window.service_connection
        customer = usage_window.customer
        policy = usage_window.policy

        creds = getattr(customer, 'radius_credentials', None) if customer else None
        if not creds or not creds.username:
            return None

        throttle_state, created = FUPThrottleState.objects.get_or_create(
            service_connection=service,
            defaults={
                'policy': policy,
                'customer': customer,
                'hotspot_session': None,
                'original_download_mbps': service.download_speed,
                'original_upload_mbps': service.upload_speed,
                'throttled_download_mbps': policy.throttle_download_mbps,
                'throttled_upload_mbps': policy.throttle_upload_mbps,
                'active': True,
                'reason': 'FUP threshold exceeded',
            },
        )

        if not created and throttle_state.active:
            return throttle_state

        if not created:
            throttle_state.policy = policy
            throttle_state.customer = customer
            throttle_state.original_download_mbps = service.download_speed
            throttle_state.original_upload_mbps = service.upload_speed
            throttle_state.throttled_download_mbps = policy.throttle_download_mbps
            throttle_state.throttled_upload_mbps = policy.throttle_upload_mbps
            throttle_state.active = True
            throttle_state.reason = 'FUP threshold exceeded'
            throttle_state.released_at = None
            throttle_state.save()

        self.radius_service.apply_throttle(
            username=creds.username,
            down_mbps=policy.throttle_download_mbps,
            up_mbps=policy.throttle_upload_mbps,
        )

        usage_window.is_throttled = True
        usage_window.throttled_at = timezone.now()
        usage_window.status = 'THROTTLED'
        usage_window.save(update_fields=['is_throttled', 'throttled_at', 'status', 'updated_at'])

        try:
            FUPViolation.objects.create(
                policy=policy,
                plan=usage_window.plan,
                service_connection=service,
                customer=customer,
                usage_window=usage_window,
                total_usage_bytes=usage_window.total_bytes,
                limit_bytes=usage_window.limit_bytes,
                exceeded_by_bytes=max(0, usage_window.total_bytes - usage_window.limit_bytes),
                action_taken='THROTTLED',
                status='OPEN',
            )
        except Exception as e:
            logger.warning(f"FUP: Failed to log PPPoE violation: {e}")

        throttle_state.last_synced_at = timezone.now()
        throttle_state.save(update_fields=['last_synced_at'])
        return throttle_state

    def release_service(self, service_connection, reason='FUP reset'):
        throttle_state = getattr(service_connection, 'fup_throttle_state', None)
        if not throttle_state or not throttle_state.active:
            return None

        customer = service_connection.customer
        creds = getattr(customer, 'radius_credentials', None) if customer else None
        if creds and creds.username:
            self.radius_service.release_throttle(
                username=creds.username,
                original_down_mbps=throttle_state.original_download_mbps,
                original_up_mbps=throttle_state.original_upload_mbps,
            )

        throttle_state.active = False
        throttle_state.reason = reason
        throttle_state.released_at = timezone.now()
        throttle_state.last_synced_at = timezone.now()
        throttle_state.save()

        latest_window = service_connection.fup_usage_windows.order_by('-period_start').first()
        if latest_window:
            try:
                FUPViolation.objects.create(
                    policy=throttle_state.policy,
                    plan=service_connection.plan,
                    service_connection=service_connection,
                    customer=service_connection.customer,
                    usage_window=latest_window,
                    total_usage_bytes=0,
                    limit_bytes=0,
                    exceeded_by_bytes=0,
                    action_taken='RELEASED',
                    status='RESOLVED',
                    notes=reason,
                )
            except Exception as e:
                logger.warning(f"FUP: Failed to log release violation: {e}")

        return throttle_state

    # ─── Hotspot ──────────────────────────────────────────────────────────────

    def evaluate_hotspot_session(self, hotspot_session):
        """
        FIX: Properly creates FUPThrottleState and FUPViolation for hotspot sessions.
        Returns the FUPUsageWindow (or None if outside peak hours / no policy).
        """
        usage_window = self.usage_service.sync_usage_for_hotspot_session(hotspot_session)
        if not usage_window:
            return None

        # Check if need to release (came back under limit)
        if usage_window.total_bytes <= usage_window.limit_bytes:
            try:
                existing = FUPThrottleState.objects.filter(
                    hotspot_session=hotspot_session, active=True
                ).first()
                if existing:
                    self._release_hotspot_throttle(hotspot_session, existing, 'Usage within limit')
            except Exception as e:
                logger.warning(f"FUP: Failed to release hotspot throttle: {e}")
            return usage_window

        # Exceeded → throttle
        self._throttle_hotspot_session(usage_window, hotspot_session)
        return usage_window

    def _throttle_hotspot_session(self, usage_window, hotspot_session):
        """Apply throttle to a hotspot session that exceeded FUP limits."""
        policy = usage_window.policy
        username = hotspot_session.access_code
        plan = hotspot_session.plan

        orig_down = plan.download_speed if plan else 10
        orig_up = plan.upload_speed if plan else 5

        throttle_state, created = FUPThrottleState.objects.get_or_create(
            hotspot_session=hotspot_session,
            defaults={
                'policy': policy,
                'service_connection': None,
                'customer': None,
                'original_download_mbps': orig_down,
                'original_upload_mbps': orig_up,
                'throttled_download_mbps': policy.throttle_download_mbps,
                'throttled_upload_mbps': policy.throttle_upload_mbps,
                'active': True,
                'reason': 'FUP hotspot limit exceeded',
            },
        )

        if not created and throttle_state.active:
            return throttle_state

        if not created:
            throttle_state.active = True
            throttle_state.reason = 'FUP hotspot limit exceeded'
            throttle_state.released_at = None
            throttle_state.save()

        self.radius_service.apply_throttle(
            username=username,
            down_mbps=policy.throttle_download_mbps,
            up_mbps=policy.throttle_upload_mbps,
        )

        usage_window.is_throttled = True
        usage_window.throttled_at = timezone.now()
        usage_window.status = 'THROTTLED'
        usage_window.save(update_fields=['is_throttled', 'throttled_at', 'status', 'updated_at'])

        try:
            FUPViolation.objects.create(
                policy=policy,
                hotspot_session=hotspot_session,
                hotspot_plan=plan,
                plan=None,
                service_connection=None,
                customer=None,
                usage_window=usage_window,
                total_usage_bytes=usage_window.total_bytes,
                limit_bytes=usage_window.limit_bytes,
                exceeded_by_bytes=max(0, usage_window.total_bytes - usage_window.limit_bytes),
                action_taken='THROTTLED',
                status='OPEN',
            )
        except Exception as e:
            logger.warning(f"FUP: Failed to log hotspot violation: {e}")

        throttle_state.last_synced_at = timezone.now()
        throttle_state.save(update_fields=['last_synced_at'])
        return throttle_state

    def _release_hotspot_throttle(self, hotspot_session, throttle_state, reason=''):
        plan = hotspot_session.plan
        if plan:
            self.radius_service.release_throttle(
                username=hotspot_session.access_code,
                original_down_mbps=plan.download_speed,
                original_up_mbps=plan.upload_speed,
            )
        throttle_state.active = False
        throttle_state.reason = reason
        throttle_state.released_at = timezone.now()
        throttle_state.last_synced_at = timezone.now()
        throttle_state.save()

    # ─── Bulk Enforce ─────────────────────────────────────────────────────────

    def enforce_all(self):
        from apps.customers.models import ServiceConnection
        from apps.billing.models.hotspot_models import HotspotSession

        now = timezone.now()
        processed = 0
        throttled = 0

        # PPPoE / Static
        for service in ServiceConnection.objects.select_related('customer', 'plan').filter(status='ACTIVE', plan__isnull=False):
            if not self.usage_service.get_active_policy_for_service(service):
                continue
            before = FUPThrottleState.objects.filter(service_connection=service, active=True).exists()
            self.evaluate_service(service)
            after = FUPThrottleState.objects.filter(service_connection=service, active=True).exists()
            processed += 1
            if not before and after:
                throttled += 1

        # Hotspot
        for session in HotspotSession.objects.filter(status='active', expires_at__gt=now).select_related('plan'):
            if not self.usage_service.get_active_policy_for_hotspot_session(session):
                continue
            before = FUPThrottleState.objects.filter(hotspot_session=session, active=True).exists()
            result = self.evaluate_hotspot_session(session)
            after = FUPThrottleState.objects.filter(hotspot_session=session, active=True).exists()
            if result:
                processed += 1
            if not before and after:
                throttled += 1

        return {'processed': processed, 'throttled': throttled}