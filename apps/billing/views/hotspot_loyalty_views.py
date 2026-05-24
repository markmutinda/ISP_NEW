"""
Public Hotspot Loyalty Views — no auth required (captive portal access).
"""
import logging
import secrets
import string
from datetime import timedelta

from django.db.models import F, Q
from django.utils import timezone
from django_tenants.utils import schema_context, get_public_schema_name
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)


def _resolve_tenant_from_subdomain(subdomain):
    from apps.core.models import Tenant
    with schema_context(get_public_schema_name()):
        return Tenant.objects.filter(
            Q(subdomain=subdomain) | Q(schema_name=subdomain),
            is_active=True,
        ).first()


def _normalize_mac(mac):
    return (mac or '').upper().replace('-', ':').strip()


class HotspotLoyaltyInfoView(APIView):
    """
    GET /api/v1/hotspot/loyalty-info/
    ?mac=AA:BB:CC:DD:EE:FF&tenant=myisp&canonical_username=ABCD-1234

    Returns the loyalty status and available rewards for a hotspot user.
    Identified by MAC address or canonical_username.
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        mac_address = _normalize_mac(request.query_params.get('mac', ''))
        canonical_username = request.query_params.get('canonical_username', '').strip()
        tenant_subdomain = request.query_params.get('tenant', '').strip()

        if not tenant_subdomain:
            return Response({'program_active': False, 'has_loyalty': False})

        tenant = _resolve_tenant_from_subdomain(tenant_subdomain)
        if not tenant:
            return Response({'program_active': False, 'has_loyalty': False})

        with schema_context(tenant.schema_name):
            try:
                from apps.loyalty.models import LoyaltySettings, LoyaltyMember, LoyaltyReward

                settings_obj = LoyaltySettings.load()
                if not settings_obj.program_active:
                    return Response({'program_active': False, 'has_loyalty': False})

                # Resolve member by canonical_username or MAC
                member = _resolve_hotspot_member(canonical_username, mac_address)

                if not member:
                    return Response({
                        'program_active': True,
                        'has_loyalty': False,
                        'current_points': 0,
                        'all_hotspot_rewards': _get_all_hotspot_rewards(),
                    })

                affordable = LoyaltyReward.objects.filter(
                    status='active',
                    hotspot_reward_minutes__isnull=False,
                    points_cost__lte=member.current_points,
                ).order_by('points_cost')

                all_rewards = LoyaltyReward.objects.filter(
                    status='active',
                    hotspot_reward_minutes__isnull=False,
                ).order_by('points_cost')

                return Response({
                    'program_active': True,
                    'has_loyalty': True,
                    'member_id': member.id,
                    'current_points': member.current_points,
                    'lifetime_points': member.lifetime_points,
                    'tier_name': member.tier.name if member.tier else 'Bronze',
                    'tier_level': member.tier.level if member.tier else 'bronze',
                    'available_rewards': _serialize_rewards(affordable),
                    'all_hotspot_rewards': _serialize_rewards(all_rewards),
                })

            except Exception as e:
                logger.error(f'HotspotLoyaltyInfoView error: {e}', exc_info=True)
                return Response({'program_active': False, 'has_loyalty': False})


class HotspotLoyaltyRedeemView(APIView):
    """
    POST /api/v1/hotspot/loyalty-redeem/
    Body: { canonical_username, reward_id, router_id, mac_address, tenant }

    Deducts points and creates RADIUS credentials for free internet access.
    """
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        canonical_username = request.data.get('canonical_username', '').strip()
        reward_id = request.data.get('reward_id')
        router_id = request.data.get('router_id')
        mac_address = _normalize_mac(request.data.get('mac_address', ''))
        tenant_subdomain = request.data.get('tenant', '').strip()

        if not all([reward_id, router_id, tenant_subdomain]):
            return Response({'error': 'Missing required fields'}, status=400)

        tenant = _resolve_tenant_from_subdomain(tenant_subdomain)
        if not tenant:
            return Response({'error': 'Invalid tenant'}, status=400)

        with schema_context(tenant.schema_name):
            try:
                from apps.loyalty.models import LoyaltySettings, LoyaltyMember, LoyaltyReward, PointsTransaction
                from apps.network.models.router_models import Router

                settings_obj = LoyaltySettings.load()
                if not settings_obj.program_active:
                    return Response({'error': 'Loyalty program is not active'}, status=400)

                member = _resolve_hotspot_member(canonical_username, mac_address)
                if not member:
                    return Response({'error': 'No loyalty account found for this device'}, status=404)

                try:
                    reward = LoyaltyReward.objects.get(id=reward_id, status='active')
                except LoyaltyReward.DoesNotExist:
                    return Response({'error': 'Reward not found'}, status=404)

                if not reward.hotspot_reward_minutes:
                    return Response({'error': 'This reward cannot be used for hotspot access'}, status=400)

                if member.current_points < reward.points_cost:
                    return Response({
                        'error': f'Insufficient points. You have {member.current_points} pts but need {reward.points_cost} pts.'
                    }, status=400)

                try:
                    router = Router.objects.get(id=router_id, is_active=True)
                except Router.DoesNotExist:
                    return Response({'error': 'Router not found'}, status=404)

                # Deduct points first
                txn = member.deduct_points(
                    points=reward.points_cost,
                    description=f'Hotspot reward: {reward.name}',
                )
                txn.reward = reward
                txn.save(update_fields=['reward'])

                # FIX 1: Use the client's permanent canonical_username as access code
                # This ensures the same user appears with consistent identity
                if member.hotspot_client and member.hotspot_client.canonical_username:
                    access_code = member.hotspot_client.canonical_username
                else:
                    access_code = _generate_unique_access_code()

                expires_at = timezone.now() + timedelta(minutes=reward.hotspot_reward_minutes)

                # Create RADIUS credentials
                class _RewardPlan:
                    name = reward.name
                    speed_limit_mbps = reward.hotspot_reward_speed_mbps or '5'
                    duration_minutes = reward.hotspot_reward_minutes
                    data_limit_mb = None
                    session_timeout = None

                from apps.billing.services.hotspot_radius_service import HotspotRadiusService
                ok = HotspotRadiusService().create_hotspot_credentials(
                    username=access_code,
                    password=access_code,
                    router=router,
                    plan=_RewardPlan(),
                    expires_at=expires_at,
                    mac_address=mac_address or '',
                )

                if not ok:
                    # Refund points
                    member.award_points(
                        points=reward.points_cost,
                        description=f'Refund (RADIUS error): {reward.name}',
                        transaction_type='adjusted',
                    )
                    return Response({'error': 'Failed to activate access. Points refunded.'}, status=500)

                # FIX 2: CREATE HotspotSession so user appears in hotspot tab and online tab
                try:
                    from apps.billing.models.hotspot_models import HotspotSession, HotspotPlan
                    from apps.network.models.router_models import Router as RouterModel

                    router_obj = RouterModel.objects.filter(id=router_id, is_active=True).first()

                    # Build a minimal plan-like object for session (no real HotspotPlan needed)
                    # Try to find any active plan on this router for display purposes
                    display_plan = HotspotPlan.objects.filter(
                        router_id=router_id, is_active=True
                    ).first() if router_obj else None

                    if router_obj and display_plan:
                        session_id = HotspotSession.generate_session_id()
                        session = HotspotSession.objects.create(
                            session_id=session_id,
                            router=router_obj,
                            plan=display_plan,
                            phone_number=member.hotspot_client.canonical_phone or 'LOYALTY',
                            mac_address=mac_address or '',
                            amount=0,  # Free reward
                            status='active',
                            access_code=access_code,
                            radius_username=access_code,
                            activated_at=timezone.now(),
                            expires_at=expires_at,
                            hotspot_client=member.hotspot_client,
                        )
                        logger.info(f'Created loyalty HotspotSession {session_id} for {access_code}')
                except Exception as e:
                    logger.warning(f'Could not create HotspotSession for loyalty reward: {e}')

                # Update reward redemption counter atomically
                LoyaltyReward.objects.filter(pk=reward.pk).update(
                    redemption_count=F('redemption_count') + 1
                )

                logger.info(
                    f'Loyalty redemption: {canonical_username} used {reward.points_cost} pts '
                    f'for {reward.name} ({reward.hotspot_reward_minutes} min) access_code={access_code}'
                )

                return Response({
                    'status': 'success',
                    'access_code': access_code,
                    'expires_at': expires_at.isoformat(),
                    'reward_minutes': reward.hotspot_reward_minutes,
                    'points_used': reward.points_cost,
                    'points_remaining': member.current_points,
                    'message': f'{reward.name} activated! {reward.hotspot_reward_minutes} minutes of free internet.',
                })

            except Exception as e:
                logger.error(f'HotspotLoyaltyRedeemView error: {e}', exc_info=True)
                return Response({'error': 'Redemption failed. Please try again.'}, status=500)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _resolve_hotspot_member(canonical_username, mac_address):
    """Find LoyaltyMember by canonical_username or MAC address."""
    from apps.loyalty.models import LoyaltyMember
    from apps.billing.models.hotspot_models import HotspotClient, HotspotClientDevice

    if canonical_username:
        client = HotspotClient.objects.filter(canonical_username=canonical_username).first()
        if client:
            try:
                return client.loyalty_member
            except LoyaltyMember.DoesNotExist:
                pass

    if mac_address and mac_address != '00:00:00:00:00:00':
        device = (
            HotspotClientDevice.objects
            .filter(mac_address=mac_address)
            .select_related('client')
            .first()
        )
        if device and device.client:
            try:
                return device.client.loyalty_member
            except LoyaltyMember.DoesNotExist:
                pass

    return None


def _serialize_rewards(queryset):
    return [
        {
            'id': r.id,
            'name': r.name,
            'description': r.description,
            'points_cost': r.points_cost,
            'reward_minutes': r.hotspot_reward_minutes,
            'reward_speed_mbps': r.hotspot_reward_speed_mbps or '5',
        }
        for r in queryset
    ]


def _get_all_hotspot_rewards():
    from apps.loyalty.models import LoyaltyReward
    return _serialize_rewards(
        LoyaltyReward.objects.filter(
            status='active', hotspot_reward_minutes__isnull=False
        ).order_by('points_cost')
    )


def _generate_unique_access_code():
    from apps.billing.models.hotspot_models import HotspotSession
    chars = string.ascii_uppercase + string.digits
    for _ in range(30):
        code = (
            ''.join(secrets.choice(chars) for _ in range(4))
            + '-'
            + ''.join(secrets.choice(chars) for _ in range(4))
        )
        if not HotspotSession.objects.filter(access_code=code).exists():
            return code
    raise RuntimeError('Could not generate unique access code')