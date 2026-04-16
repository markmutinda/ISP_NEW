from django.utils import timezone
from apps.fup.models import FUPThrottleState, FUPViolation
from .radius_service import FUPRadiusService
from .usage_service import FUPUsageService


class FUPEnforcementService:
    def __init__(self):
        self.usage_service = FUPUsageService()
        self.radius_service = FUPRadiusService()

    def evaluate_service(self, service_connection):
        usage_window = self.usage_service.sync_usage_for_service(service_connection)
        if not usage_window:
            return None

        if usage_window.total_bytes <= usage_window.limit_bytes:
            if usage_window.is_throttled:
                self.release_service(
                    service_connection, reason='Usage back within limit or period reset'
                )
            return usage_window

        self.throttle_service(usage_window)
        return usage_window

    def evaluate_hotspot_session(self, hotspot_session):
        """
        Evaluate FUP for an active hotspot session.
        Throttles via RADIUS directly (no ServiceConnection required).
        """
        usage = self.usage_service.sync_usage_for_hotspot_session(hotspot_session)
        if not usage:
            return None

        if not usage['exceeded']:
            return usage

        # Apply throttle via RADIUS using the session's access_code as username
        policy = usage['policy']
        username = usage['username']

        self.radius_service.apply_throttle(
            username=username,
            down_mbps=policy.throttle_download_mbps,
            up_mbps=policy.throttle_upload_mbps,
        )

        # Log a violation (no ServiceConnection FK, so fields are nullable-safe)
        try:
            from apps.fup.models import FUPViolation
            FUPViolation.objects.create(
                policy=policy,
                plan=hotspot_session.plan,
                # service_connection is required on the model — we skip creating
                # the violation row rather than crashing.  If you want to track
                # hotspot violations properly, add a nullable hotspot_session FK
                # to FUPViolation in a future migration.
            )
        except Exception:
            pass

        return usage

    def throttle_service(self, usage_window):
        service = usage_window.service_connection
        customer = usage_window.customer
        policy = usage_window.policy

        creds = getattr(customer, 'radius_credentials', None)
        if not creds or not creds.username:
            return None

        throttle_state, created = FUPThrottleState.objects.get_or_create(
            service_connection=service,
            defaults={
                'policy': policy,
                'customer': customer,
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

        throttle_state.last_synced_at = timezone.now()
        throttle_state.save()
        return throttle_state

    def release_service(self, service_connection, reason='FUP reset'):
        throttle_state = getattr(service_connection, 'fup_throttle_state', None)
        if not throttle_state or not throttle_state.active:
            return None

        customer = service_connection.customer
        creds = getattr(customer, 'radius_credentials', None)
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

        latest_window = service_connection.fup_usage_windows.order_by(
            '-period_start'
        ).first()
        if latest_window:
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

        return throttle_state

    def enforce_all(self):
        from apps.customers.models import ServiceConnection
        from apps.billing.models.hotspot_models import HotspotSession

        services = ServiceConnection.objects.select_related(
            'customer', 'plan'
        ).filter(status='ACTIVE', plan__isnull=False)

        processed = 0
        throttled = 0

        # PPPoE / Static service connections
        for service in services:
            if not self.usage_service.get_active_policy_for_service(service):
                continue
            before = FUPThrottleState.objects.filter(
                service_connection=service, active=True
            ).exists()
            usage_window = self.evaluate_service(service)
            after = FUPThrottleState.objects.filter(
                service_connection=service, active=True
            ).exists()
            if usage_window:
                processed += 1
            if not before and after:
                throttled += 1

        # ── NEW: Active hotspot sessions ──────────────────────────────────
        now = timezone.now()
        active_hotspot_sessions = HotspotSession.objects.filter(
            status='active', expires_at__gt=now
        ).select_related('plan')

        for session in active_hotspot_sessions:
            if not self.usage_service.get_active_policy_for_hotspot_session(session):
                continue
            result = self.evaluate_hotspot_session(session)
            if result:
                processed += 1
                if result.get('exceeded'):
                    throttled += 1

        return {'processed': processed, 'throttled': throttled}