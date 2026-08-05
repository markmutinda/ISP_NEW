import logging
from datetime import timedelta

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
        ip_address = data.get('ip_address', '')
        name = data.get('name') or mac

        if not plan:
            return Response({'error': 'A plan is required to determine speed and duration'}, status=400)

        expires_at = timezone.now() + timedelta(minutes=plan.total_validity_minutes)

        api = mikrotik_api_module.MikrotikAPI(router)
        binding_id = api.add_ip_binding(mac_address=mac, ip_address=ip_address, comment=f"Netily:{name}")
        if not binding_id:
            return Response({'error': 'Failed to create binding on router. Check router connectivity.'}, status=502)

        queue_name = f"netily-ipb-{mac.replace(':', '')}"
        speed_val = getattr(plan, 'download_speed', None) or 5
        max_limit = f"{speed_val}M/{speed_val}M"
        queue_ok = api.add_simple_queue(queue_name, f"{ip_address}/32", max_limit) if ip_address else False

        binding = IPBinding.objects.create(
            router=router, plan=plan, name=name, mac_address=mac,
            ip_address=ip_address or None, status='active',
            activated_at=timezone.now(), expires_at=expires_at,
            mikrotik_binding_id=binding_id or '',
            queue_name=queue_name if queue_ok else '',
            notes=data.get('notes', ''), created_by=request.user,
        )
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