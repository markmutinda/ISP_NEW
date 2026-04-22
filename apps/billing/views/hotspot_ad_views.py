# apps/billing/views/hotspot_ad_views.py
"""
Hotspot Ad Views

PUBLIC endpoints (captive portal):
  GET  /api/v1/hotspot/ads/serve/         — return best ad for this router
  POST /api/v1/hotspot/ads/grant-access/  — grant RADIUS access after ad completion

ADMIN endpoints (authenticated):
  GET/POST   /api/v1/hotspot/admin/ads/
  GET/PATCH/DELETE /api/v1/hotspot/admin/ads/<pk>/
  GET        /api/v1/hotspot/admin/ads/storage/
"""
import secrets
import string
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.db.models import F, Q
from django.utils import timezone

from rest_framework import status, viewsets, filters
from rest_framework.decorators import action
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from django_tenants.utils import schema_context, get_public_schema_name

from apps.billing.models.ad_models import HotspotAd, HotspotAdGrant
from apps.core.permissions import IsAdminOrStaff
from apps.network.models.router_models import Router


# ─── Helpers ────────────────────────────────────────────────────────────────

def _resolve_tenant(subdomain: str):
    if not subdomain:
        return None
    from apps.core.models import Tenant
    with schema_context(get_public_schema_name()):
        return Tenant.objects.filter(
            Q(subdomain=subdomain) | Q(schema_name=subdomain),
            is_active=True,
        ).first()


def _normalize_mac(mac: str) -> str:
    return (mac or '').upper().replace('-', ':').strip()


def _generate_ad_access_code() -> str:
    safe = ''.join(c for c in (string.ascii_uppercase + string.digits) if c not in 'O0I1S5')
    part1 = ''.join(secrets.choice(safe) for _ in range(4))
    part2 = ''.join(secrets.choice(safe) for _ in range(4))
    return f"{part1}-{part2}"


class _AdPlan:
    """Minimal plan-like object for ad-sponsored RADIUS credentials."""
    speed_limit_mbps = '5'
    duration_minutes = 30
    data_limit_mb = None
    name = 'Ad-Sponsored'
    session_timeout = None

    def __init__(self, reward_minutes: int):
        self.duration_minutes = reward_minutes


# ─── Public: Serve Ad ───────────────────────────────────────────────────────

class HotspotAdServeView(APIView):
    """Return the best active ad for the given router. Public — no auth."""
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        router_id = request.query_params.get('router_id')
        tenant_subdomain = request.query_params.get('tenant')

        tenant = _resolve_tenant(tenant_subdomain)
        if not tenant:
            return Response({'ad': None})

        with schema_context(tenant.schema_name):
            now = timezone.now()
            ad = (
                HotspotAd.objects.filter(
                    schema_name=tenant.schema_name,
                    is_active=True,
                ).filter(
                    Q(start_date__isnull=True) | Q(start_date__lte=now),
                    Q(end_date__isnull=True) | Q(end_date__gte=now),
                ).filter(
                    Q(router__isnull=True) | Q(router_id=router_id)
                ).order_by('priority', '-created_at')
                .first()
            )

            if not ad:
                return Response({'ad': None})

            # Atomic impression count — never use ad.impressions += 1
            HotspotAd.objects.filter(pk=ad.pk).update(impressions=F('impressions') + 1)

            return Response({
                'ad': {
                    'id': ad.id,
                    'name': ad.name,
                    'media_url': ad.get_media_url(request),
                    'media_type': ad.media_type,
                    'target_url': ad.target_url,
                    'reward_enabled': ad.reward_enabled,
                    'reward_minutes': ad.reward_minutes,
                }
            })


# ─── Public: Grant Access After Ad Completion ────────────────────────────────

class HotspotAdGrantView(APIView):
    """Grant RADIUS internet access after ad is fully watched. Public — no auth."""
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        ad_id = request.data.get('ad_id')
        mac_address = _normalize_mac(request.data.get('mac_address', ''))
        router_id = request.data.get('router_id')
        tenant_subdomain = request.data.get('tenant')

        if not all([ad_id, mac_address, router_id, tenant_subdomain]):
            return Response({'error': 'Missing required fields'}, status=status.HTTP_400_BAD_REQUEST)

        tenant = _resolve_tenant(tenant_subdomain)
        if not tenant:
            return Response({'error': 'Invalid tenant'}, status=status.HTTP_400_BAD_REQUEST)

        with schema_context(tenant.schema_name):
            # Validate ad
            try:
                ad = HotspotAd.objects.get(id=ad_id, is_active=True, schema_name=tenant.schema_name)
            except HotspotAd.DoesNotExist:
                return Response({'error': 'Ad not found'}, status=status.HTTP_404_NOT_FOUND)

            if not ad.reward_enabled or ad.reward_minutes == 0:
                return Response({'error': 'This ad has no reward'}, status=status.HTTP_400_BAD_REQUEST)

            # Validate router
            try:
                router = Router.objects.get(id=router_id, is_active=True)
            except Router.DoesNotExist:
                return Response({'error': 'Router not found'}, status=status.HTTP_404_NOT_FOUND)

            now = timezone.now()

            # Check cooldown: don't grant if MAC already has active grant on this router
            existing = HotspotAdGrant.objects.filter(
                mac_address=mac_address,
                router=router,
                expires_at__gt=now,
                schema_name=tenant.schema_name,
            ).first()
            if existing:
                remaining_mins = max(0, int((existing.expires_at - now).total_seconds() / 60))
                return Response({
                    'error': f'Already has active ad access. {remaining_mins} minute(s) remaining.',
                    'access_code': existing.access_code,
                    'expires_at': existing.expires_at.isoformat(),
                    'already_active': True,
                }, status=status.HTTP_409_CONFLICT)

            # Generate unique access code
            from apps.billing.models.hotspot_models import HotspotSession
            access_code = _generate_ad_access_code()
            for _ in range(20):
                if not HotspotSession.objects.filter(access_code=access_code).exists() \
                   and not HotspotAdGrant.objects.filter(access_code=access_code).exists():
                    break
                access_code = _generate_ad_access_code()

            expires_at = now + timedelta(minutes=ad.reward_minutes)

            # Create RADIUS credentials
            from apps.billing.services.hotspot_radius_service import HotspotRadiusService
            plan = _AdPlan(ad.reward_minutes)
            ok = HotspotRadiusService().create_hotspot_credentials(
                username=access_code,
                password=access_code,
                router=router,
                plan=plan,
                expires_at=expires_at,
                mac_address=mac_address,
            )
            if not ok:
                return Response({'error': 'Failed to create RADIUS credentials'}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

            # Persist grant record
            HotspotAdGrant.objects.create(
                ad=ad,
                mac_address=mac_address,
                router=router,
                access_code=access_code,
                expires_at=expires_at,
                schema_name=tenant.schema_name,
            )

            # Atomic completion counter
            HotspotAd.objects.filter(pk=ad.pk).update(completions=F('completions') + 1)

            return Response({
                'status': 'success',
                'access_code': access_code,
                'expires_at': expires_at.isoformat(),
                'reward_minutes': ad.reward_minutes,
            })


# ─── Admin: Ad Management (Authenticated) ────────────────────────────────────

class HotspotAdAdminViewSet(viewsets.ModelViewSet):
    """CRUD for hotspot ads. Staff only."""
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    parser_classes = [MultiPartParser, FormParser, JSONParser]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = ['name']
    ordering_fields = ['priority', 'created_at', 'impressions', 'completions']
    ordering = ['priority', '-created_at']

    def get_queryset(self):
        return HotspotAd.objects.filter(schema_name=connection.schema_name)

    def _serialize_ad(self, ad, request=None):
        return {
            'id': ad.id,
            'name': ad.name,
            'media_url': ad.get_media_url(request),
            'media_type': ad.media_type,
            'file_size_mb': float(ad.file_size_mb),
            'target_url': ad.target_url,
            'reward_enabled': ad.reward_enabled,
            'reward_minutes': ad.reward_minutes,
            'router': ad.router_id,
            'is_active': ad.is_active,
            'start_date': ad.start_date,
            'end_date': ad.end_date,
            'priority': ad.priority,
            'impressions': ad.impressions,
            'completions': ad.completions,
            'ctr': round(ad.completions / ad.impressions * 100, 2) if ad.impressions else 0,
            'created_at': ad.created_at.isoformat(),
        }

    def list(self, request, *args, **kwargs):
        ads = self.filter_queryset(self.get_queryset())
        return Response([self._serialize_ad(a, request) for a in ads])

    def retrieve(self, request, *args, **kwargs):
        return Response(self._serialize_ad(self.get_object(), request))

    def create(self, request, *args, **kwargs):
        schema = connection.schema_name
        media_file = request.FILES.get('media_file')

        # Storage check
        if media_file:
            file_size_mb = Decimal(str(round(media_file.size / (1024 * 1024), 2)))
            used = HotspotAd.get_used_storage_mb(schema)
            available = HotspotAd.STORAGE_LIMIT_MB - used
            if file_size_mb > available:
                return Response(
                    {'error': f'Not enough storage. You need {file_size_mb:.1f}MB but only {available:.1f}MB available. Delete inactive ads to free space.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        else:
            file_size_mb = Decimal('0')

        reward_minutes = int(request.data.get('reward_minutes', 30))
        reward_minutes = max(0, min(60, reward_minutes))  # Clamp 0–60

        ad = HotspotAd.objects.create(
            schema_name=schema,
            name=request.data.get('name', 'Untitled Ad'),
            media_file=media_file,
            media_url=request.data.get('media_url', ''),
            media_type=request.data.get('media_type', 'VIDEO'),
            file_size_mb=file_size_mb,
            target_url=request.data.get('target_url', ''),
            reward_enabled=request.data.get('reward_enabled', 'true') in (True, 'true', '1'),
            reward_minutes=reward_minutes,
            router_id=request.data.get('router') or None,
            is_active=request.data.get('is_active', 'true') in (True, 'true', '1'),
            priority=int(request.data.get('priority', 1)),
            created_by=request.user,
        )
        return Response(self._serialize_ad(ad, request), status=status.HTTP_201_CREATED)

    def partial_update(self, request, *args, **kwargs):
        ad = self.get_object()
        data = request.data

        if 'name' in data:
            ad.name = data['name']
        if 'target_url' in data:
            ad.target_url = data['target_url']
        if 'reward_enabled' in data:
            ad.reward_enabled = data['reward_enabled'] in (True, 'true', '1')
        if 'reward_minutes' in data:
            ad.reward_minutes = max(0, min(60, int(data['reward_minutes'])))
        if 'is_active' in data:
            ad.is_active = data['is_active'] in (True, 'true', '1')
        if 'priority' in data:
            ad.priority = int(data['priority'])
        if 'router' in data:
            ad.router_id = data['router'] or None

        ad.save()
        return Response(self._serialize_ad(ad, request))

    def destroy(self, request, *args, **kwargs):
        ad = self.get_object()
        if ad.media_file:
            ad.media_file.delete(save=False)
        ad.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    @action(detail=False, methods=['get'])
    def storage(self, request):
        schema = connection.schema_name
        used = HotspotAd.get_used_storage_mb(schema)
        total = HotspotAd.STORAGE_LIMIT_MB
        return Response({
            'used_mb': float(used),
            'total_mb': float(total),
            'available_mb': float(total - used),
            'percentage': round(float(used / total * 100), 1),
        })

    @action(detail=True, methods=['post'])
    def toggle_active(self, request, pk=None):
        ad = self.get_object()
        ad.is_active = not ad.is_active
        ad.save(update_fields=['is_active', 'updated_at'])
        return Response({'id': ad.id, 'is_active': ad.is_active})