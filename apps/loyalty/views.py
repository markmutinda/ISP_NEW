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
from rest_framework import viewsets, generics, status
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Sum, Count, Q, F
from django.utils import timezone

from .models import (
    LoyaltySettings,
    LoyaltyTier,
    LoyaltyMember,
    LoyaltyReward,
    PointsTransaction,
    PointsRule,
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

logger = logging.getLogger(__name__)


# ─── Settings ────────────────────────────────────────────────────
class LoyaltySettingsView(generics.RetrieveUpdateAPIView):
    permission_classes = [IsAuthenticated]
    serializer_class = LoyaltySettingsSerializer

    def get_object(self):
        return LoyaltySettings.load()


# ─── Tiers ───────────────────────────────────────────────────────
class LoyaltyTierViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = LoyaltyTierSerializer
    queryset = LoyaltyTier.objects.all()


# ─── Members ─────────────────────────────────────────────────────
class LoyaltyMemberViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = LoyaltyMemberSerializer

    def get_queryset(self):
        # 1. ADD 'hotspot_client' to select_related
        qs = LoyaltyMember.objects.select_related(
            'customer__user', 'tier', 'hotspot_client'
        ).all()
        
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
            'lifetime_points', '-lifetime_points',
            'current_points', '-current_points',
            'total_spent', '-total_spent',
            'total_payments', '-total_payments',
            'joined_date', '-joined_date',
            'last_activity', '-last_activity',
        ]
        if sort in allowed_sorts:
            qs = qs.order_by(sort)
            
        return qs

    @action(detail=False, methods=['post'], url_path='award')
    def award_points(self, request):
        ser = AwardPointsSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        member = LoyaltyMember.objects.get(id=ser.validated_data['member_id'])
        txn = member.award_points(
            points=ser.validated_data['points'],
            description=ser.validated_data.get('reason', 'Manual award'),
            transaction_type='bonus',
            created_by=request.user,
        )
        # SMS notification
        self._notify_points_earned(member, ser.validated_data['points'], ser.validated_data.get('reason', ''))
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

    def _notify_points_earned(self, member, points, reason):
        try:
            settings_obj = LoyaltySettings.load()
            if not settings_obj.notify_points_earned:
                return
            from apps.messaging.tasks import send_loyalty_notification_sms
            send_loyalty_notification_sms.delay(
                customer_id=member.customer_id,
                message_type='points_earned',
                points=points,
                reason=reason,
            )
        except Exception as e:
            logger.warning(f'Loyalty SMS notification failed: {e}')


# ─── Rewards ─────────────────────────────────────────────────────
class LoyaltyRewardViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
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

        # SMS notification
        self._notify_redemption(member, reward, voucher_code)

        return Response({
            'transaction': PointsTransactionSerializer(txn).data,
            'voucher_code': voucher_code,
        }, status=status.HTTP_201_CREATED)

    @action(detail=False, methods=['post'], url_path='award-voucher')
    def award_voucher(self, request):
        """Directly award a voucher to a member (free, not point-redeemed)."""
        ser = AwardVoucherSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        try:
            member = LoyaltyMember.objects.select_related('customer__user').get(
                id=ser.validated_data['member_id']
            )
        except LoyaltyMember.DoesNotExist:
            return Response({'error': 'Member not found'}, status=status.HTTP_404_NOT_FOUND)

        batch_id = ser.validated_data.get('voucher_batch_id')
        voucher = self._pick_voucher(batch_id)
        if not voucher:
            return Response({'error': 'No available vouchers'}, status=status.HTTP_400_BAD_REQUEST)

        # Mark voucher as sold
        voucher.sold_to = member.customer.user.get_full_name()
        voucher.sold_at = timezone.now()
        voucher.status = 'SOLD'
        voucher.save(update_fields=['sold_to', 'sold_at', 'status'])

        # SMS the voucher code
        if ser.validated_data.get('send_sms', True):
            self._sms_voucher(member, voucher)

        return Response({
            'voucher_code': voucher.code,
            'voucher_pin': getattr(voucher, 'pin', ''),
            'member': member.customer.full_name,
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

    def _notify_redemption(self, member, reward, voucher_code=None):
        try:
            settings_obj = LoyaltySettings.load()
            if not settings_obj.notify_redemption:
                return
            from apps.messaging.tasks import send_loyalty_notification_sms
            send_loyalty_notification_sms.delay(
                customer_id=member.customer_id,
                message_type='redemption',
                reward_name=reward.name,
                voucher_code=voucher_code,
            )
        except Exception as e:
            logger.warning(f'Redemption SMS failed: {e}')


# ─── Transactions ────────────────────────────────────────────────
class PointsTransactionViewSet(viewsets.ReadOnlyModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = PointsTransactionSerializer
    queryset = PointsTransaction.objects.select_related('member__customer__user', 'reward').all()

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params
        txn_type = params.get('type')
        if txn_type:
            qs = qs.filter(transaction_type=txn_type)
        member_id = params.get('member_id')
        if member_id:
            qs = qs.filter(member_id=member_id)
        return qs


# ─── Points Rules ────────────────────────────────────────────────
class PointsRuleViewSet(viewsets.ModelViewSet):
    permission_classes = [IsAuthenticated]
    serializer_class = PointsRuleSerializer
    queryset = PointsRule.objects.all()


# ─── Stats ───────────────────────────────────────────────────────
@api_view(['GET'])
@permission_classes([IsAuthenticated])
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
@permission_classes([IsAuthenticated])
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
    top = LoyaltyMember.objects.select_related('customer__user', 'tier').order_by(order)[:limit]
    return Response(LoyaltyMemberSerializer(top, many=True).data)