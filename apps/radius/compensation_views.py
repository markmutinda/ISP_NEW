import logging
import time

from django.core.cache import cache
from django.db import connection
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.models import AuditLog
from apps.core.permissions import HasCompanyAccess, HasRoleAccessPolicy, IsAdminOrStaff
from apps.radius.services import compensation_service as svc

logger = logging.getLogger(__name__)

PERMS = [IsAuthenticated, IsAdminOrStaff, HasCompanyAccess, HasRoleAccessPolicy]
KINDS = ('all', 'pppoe', 'hotspot')


def _router_id(raw):
    try:
        return int(raw) if raw not in (None, '', 'all') else None
    except (TypeError, ValueError):
        return None


class CompensationPreviewView(APIView):
    """GET /radius/compensation/preview/?type=all|pppoe|hotspot&router_id=&search="""
    permission_classes = PERMS
    required_rbac_path = "/admin/users"

    def get(self, request):
        kind = request.query_params.get('type', 'all')
        if kind not in KINDS:
            kind = 'all'
        search = (request.query_params.get('search') or '').strip()[:64]
        return Response(svc.build_preview(kind, _router_id(request.query_params.get('router_id')), search))


class CompensationExecuteView(APIView):
    """POST /radius/compensation/execute/"""
    permission_classes = PERMS
    required_rbac_path = "/admin/users"

    def post(self, request):
        d = request.data
        schema = getattr(connection, 'schema_name', None)
        if not schema or schema == 'public':
            return Response({'detail': 'Cannot run from the public portal.'}, status=400)

        kind = d.get('type', 'all')
        if kind not in KINDS:
            return Response({'detail': 'Invalid type.'}, status=400)
        try:
            delta = svc.parse_delta(d.get('duration_amount'), d.get('duration_unit'))
        except ValueError as e:
            return Response({'detail': str(e)}, status=400)

        send_sms = bool(d.get('send_sms'))
        message = (d.get('message') or '').strip()
        if send_sms:
            from apps.messaging.event_views import _unknown_vars, COMPOSE_ALLOWED_VARS
            from apps.messaging.services.compose_service import MAX_MESSAGE_CHARS
            from apps.messaging.services.gateway_dispatcher import GatewayDispatcher
            if not message:
                return Response({'detail': 'Message is required when SMS is enabled.'}, status=400)
            if len(message) > MAX_MESSAGE_CHARS:
                return Response({'detail': f'Message too long (max {MAX_MESSAGE_CHARS}).'}, status=400)
            unknown = _unknown_vars(message, COMPOSE_ALLOWED_VARS)
            if unknown:
                return Response({'detail': 'Unknown variable(s): ' + ', '.join('{%s}' % u for u in unknown)}, status=400)
            try:
                GatewayDispatcher(schema_name=schema)      # fail fast: no gateway configured
            except ValueError as e:
                return Response({'detail': str(e)}, status=400)

        router_id = _router_id(d.get('router_id'))
        search = (d.get('search') or '').strip()[:64]
        exclude = [str(x)[:64] for x in (d.get('exclude') or [])][:1000]

        if svc.build_preview(kind, router_id, search, limit=0)['counts']['total'] == 0:
            return Response({'detail': 'No eligible users match these filters.'}, status=400)

        # One job per tenant at a time (guards double-clicks / concurrent admins)
        job_id = svc.new_job_id()
        lk = svc.lock_key(schema)
        if not cache.add(lk, job_id, svc.LOCK_TTL):
            cur = cache.get(lk)
            j = cache.get(svc.job_key(schema, cur)) if cur else None
            if j and j.get('state') in ('queued', 'running') and time.time() - j.get('updated', 0) < svc.STALE_AFTER:
                return Response({'detail': 'A compensation is already running.', 'job_id': cur}, status=status.HTTP_409_CONFLICT)
            cache.set(lk, job_id, svc.LOCK_TTL)           # previous job is dead -> take over

        company = getattr(request, 'company', None)
        params = {
            'type': kind, 'router_id': router_id, 'search': search, 'exclude': exclude,
            'amount': int(d.get('duration_amount')), 'unit': str(d.get('duration_unit')).upper(),
            'send_sms': send_sms, 'message': message,
            'company_name': getattr(company, 'name', '') or '',
        }
        cache.set(svc.job_key(schema, job_id), {
            'job_id': job_id, 'state': 'queued', 'cursor': 0, 'failed_chunks': [], 'skip': [],
            'processed': 0, 'compensated': 0, 'skipped': 0, 'failed': 0, 'total': 0,
            'sms_recipients': 0, 'sms_queued': 0, 'sms_dispatched': False, 'updated': time.time(),
        }, svc.JOB_TTL)

        try:
            from apps.radius.tasks import compensate_users
            compensate_users.delay(schema, job_id, params)
        except Exception:
            logger.exception("Could not queue compensation job")
            cache.delete(lk)
            return Response({'detail': 'Could not queue the job. Please try again.'}, status=503)

        AuditLog.log_action(
            user=request.user, action="update", model_name="Compensation", object_id=job_id,
            object_repr=f"+{params['amount']} {params['unit'].lower()}",
            changes={k: v for k, v in params.items() if k not in ('exclude', 'message')} | {'excluded': len(exclude)},
            ip_address=request.META.get("REMOTE_ADDR"),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
            tenant=getattr(request, "tenant", None),
        )
        return Response({'job_id': job_id}, status=status.HTTP_202_ACCEPTED)


class CompensationJobView(APIView):
    """GET /radius/compensation/jobs/<job_id>/"""
    permission_classes = PERMS
    required_rbac_path = "/admin/users"

    def get(self, request, job_id):
        job = cache.get(svc.job_key(connection.schema_name, job_id))
        if not job:
            return Response({'detail': 'Job not found or expired.'}, status=404)
        keys = ('job_id', 'state', 'total', 'processed', 'compensated', 'skipped', 'failed',
                'sms_recipients', 'sms_queued', 'error')
        return Response({k: job.get(k) for k in keys})