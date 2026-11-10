"""
Loyalty Program Views.

Endpoints:
  GET/PUT  /loyalty/settings/           — Program settings
  GET/POST /loyalty/tiers/              — List/create tiers   (+ /tiers/{id}/ PATCH/DELETE)
  GET      /loyalty/members/            — Members list (search, filter, sort)
  GET      /loyalty/members/{id}/       — Member detail
  POST     /loyalty/members/award/      — Award points to one member
  POST     /loyalty/members/bulk-award/ — Award points to many members
  GET/POST /loyalty/rewards/            — Reward catalog       (+ /rewards/{id}/ PATCH/DELETE)
  POST     /loyalty/rewards/redeem/     — Redeem a reward
  POST     /loyalty/rewards/award-voucher/ — Award voucher + SMS
  GET      /loyalty/transactions/       — Transaction history
  GET/POST /loyalty/rules/              — Points rules         (+ /rules/{id}/ PATCH/DELETE)
  GET      /loyalty/stats/              — Dashboard stats
  GET      /loyalty/leaderboard/        — Top members by points/spend/payments
"""
import logging
from datetime import timedelta

from rest_framework import viewsets, generics, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from django.db import IntegrityError, connection, transaction
from django.db.models import Sum, Count, Q, F
from django.utils import timezone

from apps.core.permissions import IsAdminOrStaff

from .models import (
    LoyaltySettings,
    LoyaltyTier,
    LoyaltyMember,
    LoyaltyReward,
    PointsTransaction,
    PointsRule,
    LoyaltyDiscount,
)
from .serializers import (
    LoyaltySettingsSerializer,
    LoyaltyTierSerializer,
    LoyaltyMemberSerializer,
    LoyaltyRewardSerializer,
    LoyaltyRewardWriteSerializer,
    PointsTransactionSerializer,
    PointsRuleSerializer,
    AwardPointsSerializer,
    BulkAwardPointsSerializer,
    RedeemRewardSerializer,
    AwardVoucherSerializer,
)
from .services import settle_reservations

logger = logging.getLogger(__name__)

ADMIN_PERMS = [IsAuthenticated, IsAdminOrStaff]
LOYALTY_VOUCHER_VALID_DAYS = 30   # validity of vouchers awarded from the loyalty page


# ─── Helpers ─────────────────────────────────────────────────────
def _is_numeric(q):
    return q.replace('+', '').replace(' ', '').replace('-', '').isdigit()


def _local_digits(q):
    d = ''.join(c for c in q if c.isdigit())
    if d.startswith('254'):
        d = d[3:]
    return d.lstrip('0')


def _search_pppoe(q, limit=10):
    from apps.customers.models import Customer
    qs = Customer.objects.select_related('user', 'radius_credentials')
    if _is_numeric(q):
        local = _local_digits(q)
        if len(local) < 3:
            return []
        qs = qs.filter(Q(user__phone_number__contains=local) |
                       Q(radius_credentials__username__startswith=local))
    else:
        name_cond = Q()
        for tok in q.split()[:3]:
            name_cond &= (Q(user__first_name__icontains=tok) | Q(user__last_name__icontains=tok))
        qs = qs.filter(Q(customer_code__startswith=q.upper()) |
                       Q(radius_credentials__username__istartswith=q) | name_cond)
    customers = list(qs.order_by('-id')[:limit])
    pts = dict(LoyaltyMember.objects.filter(customer_id__in=[c.id for c in customers])
               .values_list('customer_id', 'current_points'))
    out = []
    for c in customers:
        creds = getattr(c, 'radius_credentials', None)
        out.append({
            'id': c.id, 'category': 'pppoe', 'name': c.user.get_full_name(),
            'phone': c.user.phone_number or '', 'code': c.customer_code,
            'username': creds.username if creds else '', 'points': pts.get(c.id, 0),
        })
    return out


def _search_hotspot(q, limit=10):
    from apps.billing.models.hotspot_models import HotspotClient
    qs = HotspotClient.objects.filter(schema_name=connection.schema_name)
    if _is_numeric(q):
        local = _local_digits(q)
        if len(local) < 3:
            return []
        qs = qs.filter(canonical_phone__contains=local).exclude(canonical_phone__startswith='MAC-')
    else:
        qs = qs.filter(canonical_username__startswith=q.upper())
    clients = list(qs.order_by('-last_seen_at')[:limit])
    pts = dict(LoyaltyMember.objects.filter(hotspot_client_id__in=[c.id for c in clients])
               .values_list('hotspot_client_id', 'current_points'))
    out = []
    for c in clients:
        phone = c.canonical_phone or ''
        out.append({
            'id': c.id, 'category': 'hotspot', 'name': c.canonical_username or 'Hotspot User',
            'phone': '' if phone.startswith('MAC-') else phone, 'code': c.canonical_username or '',
            'username': c.canonical_username or '', 'points': pts.get(c.id, 0),
        })
    return out


def _resolve_member(member_id=None, category=None, target_id=None):
    if member_id:
        return LoyaltyMember.objects.filter(pk=member_id).first()
    bronze = LoyaltyTier.objects.filter(level='bronze').first()
    if category == 'pppoe':
        from apps.customers.models import Customer
        cust = Customer.objects.filter(pk=target_id).first()
        if cust:
            return LoyaltyMember.objects.get_or_create(customer=cust, defaults={'tier': bronze})[0]
    elif category == 'hotspot':
        from apps.billing.models.hotspot_models import HotspotClient
        client = HotspotClient.objects.filter(pk=target_id).first()
        if client:
            return LoyaltyMember.objects.get_or_create(hotspot_client=client, defaults={'tier': bronze})[0]
    return None


def _member_phone(member):
    if member.customer_id:
        return getattr(member.customer.user, 'phone_number', '') or ''
    hc = member.hotspot_client
    p = (hc.canonical_phone or '') if hc else ''
    return '' if p.startswith('MAC-') else p


def _queue_sms(phone, message):
    if not phone:
        return False
    try:
        from .tasks import send_loyalty_sms
        send_loyalty_sms.delay(connection.schema_name, phone, message)
        return True
    except Exception as e:
        logger.warning(f'Could not queue loyalty SMS: {e}')
        return False


def _issue_loyalty_voucher(plan, user):
    """One single-use voucher tied to a hotspot plan (shows up on the Vouchers page)."""
    from apps.billing.models.voucher_models import VoucherBatch
    now = timezone.now()
    for attempt in range(3):
        try:
            with transaction.atomic():
                batch = VoucherBatch.objects.create(
                    name=f'Loyalty - {plan.name}'[:200],
                    description='Awarded from loyalty program',
                    voucher_type='LOYALTY', face_value=plan.price, sale_price=plan.price,
                    valid_from=now, valid_to=now + timedelta(days=LOYALTY_VOUCHER_VALID_DAYS),
                    is_reusable=False, max_uses=1, quantity=1,
                    status='ACTIVE', is_active=True,
                    prefix='LYL', length=9, charset='0123456789',
                    plan_restriction=True, hotspot_plan=plan, created_by=user,
                )
                vouchers = batch.generate_vouchers(1)
                return vouchers[0]
        except IntegrityError:
            if attempt == 2:
                raise


# ─── Settings ────────────────────────────────────────────────────
class LoyaltySettingsView(generics.RetrieveUpdateAPIView):
    permission_classes = ADMIN_PERMS
    serializer_class = LoyaltySettingsSerializer

    def get_object(self):
        return LoyaltySettings.load()


# ─── Tiers ───────────────────────────────────────────────────────
class LoyaltyTierViewSet(viewsets.ModelViewSet):
    permission_classes = ADMIN_PERMS
    serializer_class = LoyaltyTierSerializer
    queryset = LoyaltyTier.objects.all()


# ─── Members ─────────────────────────────────────────────────────
class LoyaltyMemberViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = ADMIN_PERMS
    serializer_class = LoyaltyMemberSerializer

    def get_queryset(self):
        # 1. ADD 'hotspot_client' to select_related
        qs = LoyaltyMember.objects.select_related(
            'customer__user', 'tier', 'hotspot_client'
        ).exclude(customer__isnull=True, hotspot_client__isnull=True)
        
        params = self.request.query_params
        search = params.get('search')
        
        if search:
            qs = qs.filter(
                Q(customer__user__first_name__icontains=search) |
                Q(customer__user__last_name__icontains=search) |
                Q(customer__user__email__icontains=search) |
                Q(customer__customer_code__icontains=search) |
                Q(hotspot_client__canonical_phone__icontains=search) |
                Q(hotspot_client__canonical_username__icontains=search)
            )
            
        # Filter by tier
        tier = params.get('tier')
        if tier and tier != 'all':
            qs = qs.filter(tier__level=tier)
            
        # Sort
        sort = params.get('sort', '-lifetime_points')
        allowed_sorts = [
            'lifetime_points', '-lifetime_points', 'current_points', '-current_points',
            'total_spent', '-total_spent', 'total_payments', '-total_payments',
            'joined_date', '-joined_date', 'last_activity', '-last_activity',
        ]
        if sort not in allowed_sorts:
            sort = '-lifetime_points'
        return qs.order_by(sort, '-id')

    @action(detail=False, methods=['get'], url_path='search')
    def search(self, request):
        category = request.query_params.get('category', 'pppoe')
        q = (request.query_params.get('q') or '').strip()[:50]
        if len(q) < 3 or category not in ('pppoe', 'hotspot'):
            return Response({'results': []})
        results = _search_pppoe(q) if category == 'pppoe' else _search_hotspot(q)
        return Response({'results': results})

    @action(detail=False, methods=['post'], url_path='award')
    def award_points(self, request):
        ser = AwardPointsSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data
        member = _resolve_member(d.get('member_id'), d.get('category'), d.get('target_id'))
        if not member:
            return Response({'error': 'Member not found'}, status=status.HTTP_404_NOT_FOUND)
        reason = d.get('reason') or 'Manual award'
        with transaction.atomic():
            member = LoyaltyMember.objects.select_for_update().get(pk=member.pk)
            txn = member.award_points(points=d['points'], description=reason,
                                      transaction_type='bonus', created_by=request.user)
        if d.get('send_sms', True):
            _queue_sms(
                _member_phone(member),
                f"You have been awarded {d['points']} loyalty points ({reason}). "
                f"Your balance is {member.current_points} pts.",
            )
        return Response(PointsTransactionSerializer(txn).data, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'], url_path='bulk-award')
    def bulk_award_points(self, request):
        ser = BulkAwardPointsSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        members = LoyaltyMember.objects.filter(id__in=ser.validated_data['member_ids'])
        txns = []
        for m in members:
            txn = m.award_points(
                points=ser.validated_data['points'],
                description=ser.validated_data.get('reason', 'Bulk award'),
                transaction_type='bonus',
                created_by=request.user,
            )
            txns.append(txn)
        return Response({
            'awarded': len(txns),
            'points_each': ser.validated_data['points'],
        }, status=status.HTTP_201_CREATED)


# ─── Rewards ─────────────────────────────────────────────────────
class LoyaltyRewardViewSet(viewsets.ModelViewSet):
    permission_classes = ADMIN_PERMS
    queryset = LoyaltyReward.objects.all()

    def get_serializer_class(self):
        if self.action in ('create', 'update', 'partial_update'):
            return LoyaltyRewardWriteSerializer
        return LoyaltyRewardSerializer

    @action(detail=False, methods=['post'], url_path='redeem')
    def redeem(self, request):
        ser = RedeemRewardSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        member = ser.validated_data['member']
        reward = ser.validated_data['reward']

        # PPPoE rewards are redeemed by customers at checkout, not from admin.
        if reward.category == 'pppoe':
            return Response({'error': 'PPPoE rewards are redeemed by customers at checkout.'}, status=400)

        # Deduct points
        txn = member.deduct_points(
            points=reward.points_cost,
            description=f'Redeemed: {reward.name}',
            created_by=request.user,
        )
        txn.reward = reward
        txn.save(update_fields=['reward'])

        # Update reward stats
        reward.redemption_count = F('redemption_count') + 1
        if reward.stock_quantity is not None:
            reward.stock_quantity = F('stock_quantity') - 1
        reward.save()
        reward.refresh_from_db()

        # Handle voucher rewards — auto-assign a voucher and SMS the code
        voucher_code = None
        if reward.category == 'voucher' and reward.voucher_batch_id:
            voucher_code = self._assign_voucher(member, reward)

        # Handle credit rewards
        if reward.category == 'credit' and reward.credit_amount:
            self._apply_credit(member, reward)

        return Response({
            'transaction': PointsTransactionSerializer(txn).data,
            'voucher_code': voucher_code,
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'], url_path='award-voucher')
    def award_voucher(self, request):
        """Hotspot only: generate a voucher for the chosen hotspot plan and SMS it."""
        from apps.billing.models.hotspot_models import HotspotClient, HotspotPlan
        ser = AwardVoucherSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        d = ser.validated_data

        client = None
        if d.get('target_id'):
            client = HotspotClient.objects.filter(pk=d['target_id']).first()
        else:
            m = LoyaltyMember.objects.select_related('hotspot_client').filter(pk=d['member_id']).first()
            client = m.hotspot_client if m else None
        if not client:
            return Response({'error': 'Hotspot user not found'}, status=status.HTTP_404_NOT_FOUND)

        plan = HotspotPlan.objects.filter(pk=d['plan_id'], is_active=True).first()
        if not plan:
            return Response({'error': 'Hotspot plan not found'}, status=status.HTTP_404_NOT_FOUND)

        voucher = _issue_loyalty_voucher(plan, request.user)

        phone = client.canonical_phone or ''
        phone = '' if phone.startswith('MAC-') else phone
        sms_sent = False
        if d.get('send_sms', True):
            sms_sent = _queue_sms(
                phone,
                f"You have been awarded a free WiFi voucher ({plan.name}). Code: {voucher.code}. "
                f"Enter it on the WiFi login page to connect.",
            )
        return Response({
            'voucher_code': voucher.code,
            'plan_name': plan.name,
            'member': client.canonical_username or phone,
            'sms_sent': sms_sent,
        })

    def _assign_voucher(self, member, reward):
        voucher = self._pick_voucher(reward.voucher_batch_id)
        if not voucher:
            return None
        voucher.sold_to = member.customer.user.get_full_name()
        voucher.sold_at = timezone.now()
        voucher.status = 'SOLD'
        voucher.save(update_fields=['sold_to', 'sold_at', 'status'])
        return voucher.code

    def _pick_voucher(self, batch_id=None):
        from apps.billing.models.voucher_models import Voucher
        qs = Voucher.objects.filter(status='UNUSED')
        if batch_id:
            qs = qs.filter(batch_id=batch_id)
        return qs.order_by('?').first()

    def _apply_credit(self, member, reward):
        try:
            customer = member.customer
            customer.outstanding_balance = max(
                0, customer.outstanding_balance - reward.credit_amount
            )
            customer.save(update_fields=['outstanding_balance', 'updated_at'])
        except Exception as e:
            logger.error(f'Failed to apply credit reward: {e}')

    def _sms_voucher(self, member, voucher):
        try:
            from apps.messaging.services.gateway_dispatcher import GatewayDispatcher
            phone = getattr(member.customer, 'alternative_phone', '') or ''
            if hasattr(member.customer, 'user') and hasattr(member.customer.user, 'phone_number'):
                phone = member.customer.user.phone_number or phone
            if not phone:
                return
            code = voucher.code
            pin = getattr(voucher, 'pin', '')
            msg = f'You have been awarded a free voucher! Code: {code}'
            if pin:
                msg += f', PIN: {pin}'
            msg += '. Enjoy your service!'
            dispatcher = GatewayDispatcher()
            dispatcher.send_sms(to=phone, message=msg)
        except Exception as e:
            logger.warning(f'Voucher SMS failed: {e}')


# ─── Transactions ────────────────────────────────────────────────
class PointsTransactionViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = ADMIN_PERMS
    serializer_class = PointsTransactionSerializer
    queryset = PointsTransaction.objects.select_related(
        'member__customer__user', 'member__hotspot_client', 'reward'
    ).exclude(member__customer__isnull=True, member__hotspot_client__isnull=True)

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        txn_type = params.get('type')
        if txn_type:
            qs = qs.filter(transaction_type=txn_type)
        member_id = params.get('member_id')
        if member_id:
            qs = qs.filter(member_id=member_id)
        return qs.order_by('-created_at', '-id')


# ─── Points Rules ────────────────────────────────────────────────
class PointsRuleViewSet(viewsets.ModelViewSet):
    permission_classes = ADMIN_PERMS
    serializer_class = PointsRuleSerializer
    queryset = PointsRule.objects.order_by('id')


# ─── Stats ───────────────────────────────────────────────────────
@api_view(['GET'])
@permission_classes(ADMIN_PERMS)
def loyalty_stats(request):
    members = LoyaltyMember.objects.all()
    agg = members.aggregate(
        total_members=Count('id'),
        total_points_issued=Sum('lifetime_points'),
        total_redemptions=Sum('redemptions_count'),
        total_spent=Sum('total_spent'),
    )
    active_rewards = LoyaltyReward.objects.filter(status='active').count()
    avg_points = 0
    if agg['total_members']:
        avg_points = int(members.aggregate(avg=Sum('current_points'))['avg'] / agg['total_members'])

    # Tier distribution
    tiers = LoyaltyTier.objects.all()
    tier_distribution = []
    for tier in tiers:
        tier_distribution.append({
            'id': tier.id,
            'name': tier.name,
            'level': tier.level,
            'count': tier.members.count(),
        })

    return Response({
        'total_members': agg['total_members'] or 0,
        'total_points_issued': agg['total_points_issued'] or 0,
        'total_redemptions': agg['total_redemptions'] or 0,
        'avg_points_per_member': avg_points,
        'active_rewards': active_rewards,
        'total_spent': float(agg['total_spent'] or 0),
        'tier_distribution': tier_distribution,
    })


# ─── Leaderboard ─────────────────────────────────────────────────
@api_view(['GET'])
@permission_classes(ADMIN_PERMS)
def loyalty_leaderboard(request):
    sort_by = request.query_params.get('sort', 'lifetime_points')
    allowed = {
        'lifetime_points': '-lifetime_points',
        'total_spent': '-total_spent',
        'total_payments': '-total_payments',
        'current_points': '-current_points',
    }
    order = allowed.get(sort_by, '-lifetime_points')
    limit = min(int(request.query_params.get('limit', 20)), 100)
    top = (
        LoyaltyMember.objects.select_related('customer__user', 'tier')
        .exclude(customer__isnull=True, hotspot_client__isnull=True)
        .order_by(order)[:limit]
    )
    return Response(LoyaltyMemberSerializer(top, many=True).data)


# ─── Customer endpoints ──────────────────────────────────────────
def _customer_of(request):
    return getattr(request.user, 'customer_profile', None)


class MyLoyaltyView(APIView):
    """GET /loyalty/me/ - points + PPPoE rewards redeemable at checkout."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        customer = _customer_of(request)
        if not customer:
            return Response({'detail': 'Customer account required'}, status=status.HTTP_403_FORBIDDEN)
        if not LoyaltySettings.load().program_active:
            return Response({'program_active': False})

        member = LoyaltyMember.objects.select_related('tier').filter(customer=customer).first()
        if member:
            settle_reservations(member)
            member.refresh_from_db()
        points = member.current_points if member else 0

        rewards = [
            {'id': r.id, 'name': r.name, 'description': r.description, 'points_cost': r.points_cost,
             'discount_percent': str(r.discount_percent), 'affordable': points >= r.points_cost}
            for r in LoyaltyReward.objects.filter(status='active', category='pppoe', discount_percent__isnull=False)
            .order_by('points_cost') if r.is_available
        ]
        return Response({
            'program_active': True,
            'current_points': points,
            'lifetime_points': member.lifetime_points if member else 0,
            'tier_name': member.tier.name if member and member.tier else 'Bronze',
            'rewards': rewards,
        })