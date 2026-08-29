# apps/network/views/access_point_views.py
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from apps.network.models.access_point_models import AccessPoint
from apps.network.serializers.access_point_serializers import AccessPointSerializer
from apps.network.services.ap_monitor import get_or_refresh_router_ap_status, check_single_access_point
from apps.core.permissions import HasCompanyAccess, HasRoleAccessPolicy


class AccessPointViewSet(viewsets.ModelViewSet):
    """
    GET  /network/access-points/?router_id=5      -> full topology for a router
    GET  /network/access-points/status-map/?router_id=5 -> lightweight poll payload
    POST /network/access-points/bulk-position/     -> save canvas layout in one call
    POST /network/access-points/{id}/check-now/    -> on-demand recheck
    """
    serializer_class = AccessPointSerializer
    permission_classes = [IsAuthenticated, HasCompanyAccess, HasRoleAccessPolicy]
    required_rbac_path = "/admin/routers"
    queryset = AccessPoint.objects.filter(is_active=True).select_related('router', 'parent')

    def get_queryset(self):
        qs = super().get_queryset()
        router_id = self.request.query_params.get('router_id')
        if router_id:
            qs = qs.filter(router_id=router_id)
        return qs

    def perform_destroy(self, instance):
        instance.is_active = False
        instance.save(update_fields=['is_active'])

    @action(detail=False, methods=['post'], url_path='bulk-position')
    def bulk_position(self, request):
        """Body: [{id, pos_x, pos_y, parent}, ...] — single round-trip layout save."""
        updates = request.data if isinstance(request.data, list) else []
        aps = {str(a.id): a for a in AccessPoint.objects.filter(id__in=[u.get('id') for u in updates])}

        to_update = []
        for u in updates:
            ap = aps.get(str(u.get('id')))
            if not ap:
                continue
            ap.pos_x = u.get('pos_x', ap.pos_x)
            ap.pos_y = u.get('pos_y', ap.pos_y)
            if 'parent' in u:
                ap.parent_id = u['parent']
            to_update.append(ap)

        AccessPoint.objects.bulk_update(to_update, ['pos_x', 'pos_y', 'parent'])
        return Response({'updated': len(to_update)})

    @action(detail=False, methods=['get'], url_path='status-map')
    def status_map(self, request):
        """
        Called every ~7s by the frontend WHILE the topology page is open.
        Triggers a real MikroTik check (rate-limited per router, see
        get_or_refresh_router_ap_status), then returns current DB state.
        No background polling exists — if nobody has the page open for a
        router, that router is never touched.
        """
        router_id = request.query_params.get('router_id')
        if router_id:
            from apps.network.models.router_models import Router
            router = Router.objects.filter(id=router_id, is_active=True).first()
            if router:
                get_or_refresh_router_ap_status(router)

        qs = self.get_queryset().only('id', 'status', 'last_seen')
        return Response({
            str(ap.id): {
                'status': ap.status,
                'last_seen': ap.last_seen.isoformat() if ap.last_seen else None,
            }
            for ap in qs
        })

    @action(detail=True, methods=['post'], url_path='check-now')
    def check_now(self, request, pk=None):
        """On-demand refresh for a single AP (UI 'refresh' button)."""
        ap = check_single_access_point(self.get_object())
        return Response(AccessPointSerializer(ap).data)