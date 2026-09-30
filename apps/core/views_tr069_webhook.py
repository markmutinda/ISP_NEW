cat > /root/netily_cloud/apps/core/views_tr069_webhook.py << 'PY_EOF'
"""
Public TR-069 webhooks. These run BEFORE any tenant is known:
1. credentials lookup  — called by the GenieACS extension on every CWMP auth check.
2. inform notify       — called by a GenieACS provision script on every Inform,
                          so we know a device just checked in and can queue a
                          full parameter sync inside the right tenant schema.
"""
import hmac
import json
import logging

from django.conf import settings
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST
from django_tenants.utils import schema_context

from .models import Tr069DeviceIndex

logger = logging.getLogger(__name__)


def _check_secret(request) -> bool:
    secret = getattr(settings, 'TR069_WEBHOOK_SECRET', '')
    if not secret:
        return True
    provided = request.headers.get('X-TR069-Webhook-Secret', '')
    return hmac.compare_digest(provided, secret)


@csrf_exempt
@require_GET
def tr069_credentials_lookup(request):
    """Called by the GenieACS ext script — returns the expected username/password for a serial."""
    if not _check_secret(request):
        return JsonResponse({'error': 'unauthorized'}, status=401)

    serial = (request.GET.get('serial') or '').strip()
    if not serial:
        return JsonResponse({'error': 'serial required'}, status=400)

    entry = Tr069DeviceIndex.objects.filter(serial_number=serial, is_active=True).first()
    if not entry:
        return JsonResponse({'error': 'not found'}, status=404)

    return JsonResponse({'username': entry.acs_username, 'password': entry.acs_password})


@csrf_exempt
@require_POST
def tr069_inform_webhook(request):
    """Called by GenieACS on every Inform. Resolves tenant, flags the device online,
    and queues a full NBI sync so signal/WiFi/host data gets pulled in shortly after."""
    if not _check_secret(request):
        return JsonResponse({'error': 'unauthorized'}, status=401)

    try:
        payload = json.loads(request.body or '{}')
    except ValueError:
        return JsonResponse({'error': 'invalid json'}, status=400)

    serial = (payload.get('serial_number') or '').strip()
    genieacs_device_id = payload.get('genieacs_device_id') or ''
    if not serial:
        return JsonResponse({'error': 'serial_number required'}, status=400)

    entry = Tr069DeviceIndex.objects.filter(serial_number=serial, is_active=True).first()
    if not entry:
        logger.info("TR-069 Inform for unenrolled serial %s — ignoring", serial)
        return JsonResponse({'status': 'unenrolled'})

    if genieacs_device_id and entry.genieacs_device_id != genieacs_device_id:
        entry.genieacs_device_id = genieacs_device_id
        entry.save(update_fields=['genieacs_device_id', 'updated_at'])

    with schema_context(entry.tenant_schema):
        from apps.tr069.models import CPEDevice
        from apps.tr069.tasks import sync_cpe_device_task

        device = CPEDevice.objects.filter(serial_number=serial).first()
        if not device:
            return JsonResponse({'status': 'device_missing_in_tenant'})

        updates = {'status': 'online', 'last_inform_at': timezone.now()}
        if genieacs_device_id and device.genieacs_device_id != genieacs_device_id:
            updates['genieacs_device_id'] = genieacs_device_id
        CPEDevice.objects.filter(pk=device.pk).update(**updates)

        # Full parameter pull runs shortly after, once GenieACS has stored the
        # session's reported values — queued so this webhook stays fast.
        sync_cpe_device_task.apply_async(args=[device.pk, entry.tenant_schema], countdown=8)

    return JsonResponse({'status': 'ok'})
PY_EOF