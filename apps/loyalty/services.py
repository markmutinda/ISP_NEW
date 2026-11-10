"""Loyalty checkout helpers: redeem a PPPoE reward at payment time, settle/refund by payment outcome."""
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .models import (
    LoyaltyDiscount, LoyaltyMember, LoyaltyReward, LoyaltySettings, PointsTransaction,
)

RESERVATION_TTL = timedelta(minutes=60)
FINAL_FAILED = (None, 'FAILED', 'CANCELLED')


class RewardError(Exception):
    """User-facing reward problem (maps to HTTP 400)."""


def discounted_amount(amount, percent):
    amt = Decimal(str(amount)) * (Decimal('100') - Decimal(str(percent))) / Decimal('100')
    return max(amt.quantize(Decimal('1'), rounding=ROUND_HALF_UP), Decimal('1'))


def preview_reward(customer, reward_id, amount):
    """Validate WITHOUT deducting. Returns (reward, discounted_amount)."""
    if not LoyaltySettings.load().program_active:
        raise RewardError('Loyalty program is not active.')
    try:
        reward_id = int(reward_id)
    except (TypeError, ValueError):
        raise RewardError('Invalid reward.')
    member = LoyaltyMember.objects.filter(customer=customer).first()
    reward = LoyaltyReward.objects.filter(
        pk=reward_id, category='pppoe', discount_percent__isnull=False
    ).first()
    if not member or not reward or not reward.is_available:
        raise RewardError('This reward is not available.')
    if member.current_points < reward.points_cost:
        raise RewardError(f'You need {reward.points_cost} points (you have {member.current_points}).')
    return reward, discounted_amount(amount, reward.discount_percent)


@transaction.atomic
def reserve_reward(customer, reward_id, payment_id):
    """Deduct points + record a reservation tied to the payment (re-validates under row locks)."""
    member = LoyaltyMember.objects.select_for_update().filter(customer=customer).first()
    reward = LoyaltyReward.objects.select_for_update().filter(pk=reward_id, category='pppoe').first()
    if not member or not reward or not reward.is_available or not reward.discount_percent:
        raise RewardError('This reward is no longer available.')
    if member.current_points < reward.points_cost:
        raise RewardError('Not enough points.')

    txn = member.deduct_points(points=reward.points_cost, description=f'Redeemed at checkout: {reward.name}')
    txn.reward = reward
    txn.save(update_fields=['reward'])

    reward.redemption_count += 1
    if reward.stock_quantity is not None:
        reward.stock_quantity -= 1
    reward.save(update_fields=['redemption_count', 'stock_quantity', 'updated_at'])

    return LoyaltyDiscount.objects.create(
        member=member, reward=reward, percent=reward.discount_percent,
        status='reserved', reserved_payment_id=payment_id, reserved_at=timezone.now(),
    )


def _refund(d):
    if not d.reward_id:
        return
    r = d.reward
    LoyaltyMember.objects.filter(pk=d.member_id).update(
        current_points=F('current_points') + r.points_cost,
        redemptions_count=F('redemptions_count') - 1,
    )
    PointsTransaction.objects.create(
        member_id=d.member_id, transaction_type='adjusted', points=r.points_cost,
        reward_id=r.pk, description=f'Refund: {r.name} (payment not completed)',
    )
    LoyaltyReward.objects.filter(pk=r.pk).update(redemption_count=F('redemption_count') - 1)
    LoyaltyReward.objects.filter(pk=r.pk, stock_quantity__isnull=False).update(
        stock_quantity=F('stock_quantity') + 1
    )


def _settle(d, pay_status, now):
    """Caller holds a row lock on d."""
    if pay_status == 'COMPLETED':
        d.status, d.used_at = 'used', now
        d.save(update_fields=['status', 'used_at'])
    elif pay_status in FINAL_FAILED or (d.reserved_at and d.reserved_at < now - RESERVATION_TTL):
        _refund(d)
        d.delete()


def settle_payment_reservation(payment):
    """Call as soon as a payment reaches a final state."""
    with transaction.atomic():
        # NOTE: no select_related('reward') here — LoyaltyDiscount.reward is
        # nullable (SET_NULL), so the JOIN is an OUTER JOIN and PostgreSQL
        # rejects FOR UPDATE on the nullable side. Load reward lazily per row.
        for d in LoyaltyDiscount.objects.select_for_update().filter(
            reserved_payment_id=payment.pk, status='reserved'
        ):
            _settle(d, payment.status, timezone.now())


def settle_reservations(member):
    """Lazy sweep for one member (completed -> used, failed/stale -> refund)."""
    from apps.billing.models.payment_models import Payment

    now = timezone.now()
    with transaction.atomic():
        # NOTE: see settle_payment_reservation — no select_related on the
        # nullable reward FK, or PostgreSQL rejects FOR UPDATE.
        for d in LoyaltyDiscount.objects.select_for_update().filter(
            member=member, status='reserved'
        ):
            pay_status = Payment.objects.filter(pk=d.reserved_payment_id).values_list('status', flat=True).first()
            _settle(d, pay_status, now)