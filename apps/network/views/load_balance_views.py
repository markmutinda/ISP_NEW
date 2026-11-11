# apps/network/views/load_balance_views.py
import logging

from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from apps.core.permissions import HasCompanyAccess
from apps.network.models.router_models import RouterEvent
from apps.network.models.load_balance_models import RouterLoadBalance
from apps.network.services.mikrotik_script_generator import MikrotikScriptGenerator
from apps.network.services import load_balancer as lb
from apps.network.views.router_views import RouterRBACMixin, get_router_for_tenant

logger = logging.getLogger(__name__)


def _state(router, cfg, request):
    if not cfg:
        return {'configured': False, 'is_enabled': False, 'uplinks': [], 'in_sync': False}
    base = MikrotikScriptGenerator(router, request=request).provision_base
    script, h = lb.script_for(router, cfg, base, mask=True)
    return {
        'configured': True,
        'is_enabled': cfg.is_enabled,
        'uplinks': lb.public_uplinks(cfg.uplinks),
        'script_hash': h,
        'applied_hash': cfg.applied_hash,
        'in_sync': cfg.applied_hash == h,
        'last_applied_at': cfg.last_applied_at,
        'magic_link': lb.magic_link(router, base),
        'script_preview': script,
    }


class RouterLoadBalanceView(RouterRBACMixin, APIView):
    """GET state · PUT save+enable · DELETE disable (serves removal script on next fetch)."""
    permission_classes = [IsAuthenticated, HasCompanyAccess]

    def _ctx(self, request, pk):
        router, _ = get_router_for_tenant(request, pk)
        cfg = RouterLoadBalance.objects.filter(router=router).first() if router else None
        return router, cfg

    def get(self, request, pk):
        router, cfg = self._ctx(request, pk)
        if not router:
            return Response({'error': 'Router not found'}, status=404)
        return Response(_state(router, cfg, request))

    def put(self, request, pk):
        router, cfg = self._ctx(request, pk)
        if not router:
            return Response({'error': 'Router not found'}, status=404)
        if router.router_type != 'mikrotik':
            return Response({'error': 'Load balancing is only available for MikroTik routers'}, status=400)

        clean, errors = lb.validate_uplinks(request.data.get('uplinks'), router, cfg.uplinks if cfg else None)
        if errors:
            return Response({'error': 'Invalid configuration', 'details': errors}, status=400)

        cfg, _ = RouterLoadBalance.objects.update_or_create(
            router=router, defaults={'uplinks': clean, 'is_enabled': True, 'applied_hash': ''})
        RouterEvent.objects.create(
            router=router, event_type='config_change',
            message=f'Load balancing configured ({len(clean)} uplinks)',
            details={'uplinks': [{'name': u['name'], 'port': u['port'], 'mode': u['mode'], 'weight': u['weight']}
                                 for u in clean],
                     'by': getattr(request.user, 'email', str(request.user))})
        return Response(_state(router, cfg, request))

    def delete(self, request, pk):
        router, cfg = self._ctx(request, pk)
        if not router or not cfg:
            return Response({'error': 'Load balancing is not configured'}, status=404)
        cfg.is_enabled = False
        cfg.applied_hash = ''
        cfg.save(update_fields=['is_enabled', 'applied_hash', 'updated_at'])
        RouterEvent.objects.create(router=router, event_type='config_change',
                                   message='Load balancing disabled (removal script issued)')
        return Response(_state(router, cfg, request))