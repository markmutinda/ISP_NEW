import logging
from datetime import timedelta

from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

RECOVERY_GRACE_SECONDS = 90   # give the live status poll first shot
RECOVERY_BATCH = 50


def settle_hotspot_session(session, receipt: str = '') -> bool:
    """
    Idempotent: payment confirmed -> paid -> active + RADIUS credentials.
    Safe to call from webhook, status poll, and recovery sweep concurrently
    (row lock + status guard). Returns True only if THIS call activated it.
    All-or-nothing: if RADIUS provisioning fails the session stays 'paid'
    so the sweep retries it.
    """
    from apps.billing.models.hotspot_models import HotspotSession
    from apps.billing.services.hotspot_radius_service import HotspotRadiusService
    from apps.billing.services.hotspot_credentials import schedule_tv_login_push
    from apps.messaging.tasks import send_hotspot_welcome_sms

    schema = connection.schema_name
    try:
        with transaction.atomic():
            s = (
                HotspotSession.objects
                .select_for_update(of=('self',))
                .select_related('plan', 'router')
                .get(pk=session.pk)
            )
            if s.status == 'pending':
                s.mark_paid(receipt or s.mpesa_receipt or '')
            if s.status != 'paid':
                return False  # already active/failed/expired -> nothing to do

            s.activate(s.access_code)

            ok = HotspotRadiusService().create_hotspot_credentials(
                username=s.access_code,
                password=s.access_code,
                router=s.router,
                plan=s.plan,
                expires_at=s.expires_at,
                mac_address=s.mac_address or '',
            )
            if not ok:
                raise RuntimeError("RADIUS provisioning returned False")

            schedule_tv_login_push(s, schema)
            sid = s.session_id
            transaction.on_commit(lambda: send_hotspot_welcome_sms.delay(sid, schema))

        session.refresh_from_db()
        logger.info("Hotspot session %s settled/activated", session.session_id)
        return True
    except Exception:
        logger.exception("settle_hotspot_session failed for %s (will be retried by sweep)",
                         getattr(session, 'session_id', session))
        return False


def recover_stuck_hotspot_sessions(limit: int = RECOVERY_BATCH) -> int:
    """
    Safety net (tenant-schema context): activates sessions whose payment was
    confirmed but never activated (browser closed mid-payment, lost callback...).
    """
    from apps.billing.models.hotspot_models import HotspotSession

    cutoff = timezone.now() - timedelta(seconds=RECOVERY_GRACE_SECONDS)
    stuck = (
        HotspotSession.objects
        .filter(updated_at__lt=cutoff)
        .filter(Q(status='paid') | Q(status='pending', payment__status='COMPLETED'))
        .select_related('payment')
        .order_by('updated_at')[:limit]
    )
    recovered = 0
    for s in stuck:
        receipt = s.mpesa_receipt or (s.payment.mpesa_receipt if s.payment_id else '')
        if settle_hotspot_session(s, receipt):
            recovered += 1
    return recovered