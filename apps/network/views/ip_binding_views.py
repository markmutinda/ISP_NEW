import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.permissions import HasRoleAccessPolicy, IsAdminOrStaff
from apps.network.models.ip_binding_models import IPBinding
from apps.network.models.router_models import Router
from apps.network.serializers.ip_binding_serializers import IPBindingSerializer
import apps.network.integrations.mikrotik_api as mikrotik_api_module

logger = logging.getLogger(__name__)


def _plan_max_limit(plan) -> str:
    """
    Build a MikroTik max-limit string ('upload/download') from a HotspotPlan,
    honouring speed_unit (Mbps vs Kbps) and distinct up/down values instead of
    silently defaulting.
    """
    unit = (getattr(plan, 'speed_unit', 'MBPS') or 'MBPS').upper()
    suffix = 'k' if unit == 'KBPS' else 'M'

    download = getattr(plan, 'download_speed', None)
    upload = getattr(plan, 'upload_speed', None)

    # Only fall back to a default when the plan genuinely has no value set —
    # never silently override a real (even small) configured speed.
    if not download:
        download = 5
    if not upload:
        upload = download

    return f"{upload}{suffix}/{download}{suffix}"


def _resolve_ip_for_mac(api: "mikrotik_api_module.MikrotikAPI", mac: str) -> str:
    """Best-effort IP lookup via ARP/DHCP when the caller didn't supply one."""
    try:
        hosts = api.get_arp_and_dhcp_hosts()
        match = next((h for h in hosts if h.get('mac', '').upper() == mac.upper()), None)
        return match.get('ip', '') if match else ''
    except Exception as e:
        logger.warning(f"IP resolution for {mac} failed (non-fatal): {e}")
        return ''


class IPBindingViewSet(viewsets.ModelViewSet):
    serializer_class = IPBindingSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff, HasRoleAccessPolicy]
    required_rbac_path = "/admin/users"

    def get_queryset(self):
        qs = IPBinding.objects.select_related('router', 'plan').all()
        router_id = self.request.query_params.get('router_id')
        if router_id:
            qs = qs.filter(router_id=router_id)
        status_filter = self.request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)
        return qs

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        router = data['router']
        plan = data.get('plan')
        mac = data['mac_address']
        ip_address = data.get('ip_address', '') or ''
        name = data.get('name') or mac

        if not plan:
            return Response({'error': 'A plan is required to determine speed and duration'}, status=400)

        # ── Idempotency guard: prevent duplicate router objects from a
        # double-click. If an active binding already exists for this
        # router+mac, just return it instead of creating a duplicate.
        existing = IPBinding.objects.filter(
            router=router, mac_address=mac, status='active'
        ).first()
        if existing:
            return Response(self.get_serializer(existing).data, status=status.HTTP_200_OK)

        expires_at = timezone.now() + timedelta(minutes=plan.total_validity_minutes)
        max_limit = _plan_max_limit(plan)

        api = mikrotik_api_module.MikrotikAPI(router)
        binding_id = api.add_ip_binding(mac_address=mac, ip_address=ip_address, comment=f"Netily:{name}")
        if not binding_id:
            return Response({'error': 'Failed to create binding on router. Check router connectivity.'}, status=502)

        # If no IP was supplied up front, try to resolve it now so we can
        # still apply the plan's speed via a targeted simple queue.
        if not ip_address:
            ip_address = _resolve_ip_for_mac(api, mac)

        queue_name = f"netily-ipb-{mac.replace(':', '')}"
        queue_ok = False
        if ip_address:
            queue_ok = api.add_simple_queue(queue_name, f"{ip_address}/32", max_limit)
        else:
            logger.warning(
                f"IP binding {mac} on router {router.id} created without a known IP — "
                f"no speed-limiting queue applied yet. It will apply once the device "
                f"gets an IP (edit/recreate the binding once connected)."
            )

        try:
            with transaction.atomic():
                binding = IPBinding.objects.create(
                    router=router, plan=plan, name=name, mac_address=mac,
                    ip_address=ip_address or None, status='active',
                    activated_at=timezone.now(), expires_at=expires_at,
                    mikrotik_binding_id=binding_id or '',
                    queue_name=queue_name if queue_ok else '',
                    notes=data.get('notes', ''), created_by=request.user,
                )
        except IntegrityError:
            # Lost a race with a parallel duplicate request — the router-side
            # objects we just created are harmless duplicates of an existing
            # binding; clean them up and return the winner.
            self._teardown_router_objects(api, mac, binding_id, queue_name if queue_ok else '')
            winner = IPBinding.objects.filter(router=router, mac_address=mac, status='active').first()
            if winner:
                return Response(self.get_serializer(winner).data, status=status.HTTP_200_OK)
            raise

        return Response(self.get_serializer(binding).data, status=status.HTTP_201_CREATED)

    def destroy(self, request, *args, **kwargs):
        binding = self.get_object()
        self._teardown_on_router(binding)
        binding.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=True, methods=['post'])
    def extend(self, request, pk=None):
        binding = self.get_object()
        minutes = int(request.data.get('minutes', 0) or 0)
        if minutes <= 0:
            return Response({'error': 'minutes must be positive'}, status=400)
        base = binding.expires_at if (binding.expires_at and binding.expires_at > timezone.now()) else timezone.now()
        binding.expires_at = base + timedelta(minutes=minutes)
        binding.status = 'active'
        binding.save(update_fields=['expires_at', 'status', 'updated_at'])
        return Response(self.get_serializer(binding).data)

    @action(detail=True, methods=['post'])
    def disable(self, request, pk=None):
        binding = self.get_object()
        self._teardown_on_router(binding)
        binding.status = 'disabled'
        binding.save(update_fields=['status', 'updated_at'])
        return Response(self.get_serializer(binding).data)

    def _teardown_on_router(self, binding: IPBinding):
        api = mikrotik_api_module.MikrotikAPI(binding.router)
        try:
            api.remove_ip_binding(binding.mac_address, binding.mikrotik_binding_id)
            if binding.queue_name:
                api.remove_simple_queue_by_name(binding.queue_name)
        except Exception as e:
            logger.warning(f"IP binding teardown failed for {binding.mac_address}: {e}")

    def _teardown_router_objects(self, api, mac: str, binding_id: str, queue_name: str):
        try:
            api.remove_ip_binding(mac, binding_id)
            if queue_name:
                api.remove_simple_queue_by_name(queue_name)
        except Exception as e:
            logger.warning(f"Duplicate-binding cleanup failed for {mac}: {e}")


class RouterKnownHostsView(APIView):
    """GET /api/v1/network/routers/{router_id}/known-hosts/ — ARP+DHCP picker."""
    permission_classes = [IsAuthenticated, IsAdminOrStaff, HasRoleAccessPolicy]
    required_rbac_path = "/admin/users"

    def get(self, request, router_id):
        try:
            router = Router.objects.get(id=router_id, is_active=True)
        except Router.DoesNotExist:
            return Response({'error': 'Router not found'}, status=404)

        hosts = mikrotik_api_module.MikrotikAPI(router).get_arp_and_dhcp_hosts()
        bound_macs = set(
            IPBinding.objects.filter(router=router, status='active').values_list('mac_address', flat=True)
        )
        hosts = [h for h in hosts if h['mac'] not in bound_macs]
        return Response({'hosts': hosts})