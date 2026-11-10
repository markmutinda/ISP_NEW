"""Loyalty coupon helpers (PPPoE plan discounts)."""
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from .models import LoyaltyMember, LoyaltyDiscount

RESERVATION_TTL = timedelta(minutes=30)


def discounted_amount(amount, percent):
    amt = (Decimal(str(amount)) * (Decimal('100') - Decimal(str(percent))) / Decimal('100'))
    return max(amt.quantize(Decimal('1'), rounding=ROUND_HALF_UP), Decimal('1'))


def settle_reservations(member):
    """Lazily resolve reserved coupons: payment COMPLETED -> used; FAILED/stale -> available again."""
    from apps.billing.models.payment_models import Payment

    now = timezone.now()
    with transaction.atomic():
        for d in LoyaltyDiscount.objects.select_for_update().filter(member=member, status='reserved'):
            pay_status = (
                Payment.objects.filter(pk=d.reserved_payment_id)
                .values_list('status', flat=True).first()
            )
            if pay_status == 'COMPLETED':
                d.status, d.used_at = 'used', now
            elif (pay_status in (None, 'FAILED', 'CANCELLED')
                  or (d.reserved_at and d.reserved_at < now - RESERVATION_TTL)):
                d.status, d.reserved_payment_id, d.reserved_at = 'available', None, None
            else:
                continue
            d.save(update_fields=['status', 'used_at', 'reserved_payment_id', 'reserved_at'])


def pick_discount(customer):
    """Oldest usable coupon for this customer, row-locked. Call inside an atomic block."""
    member = LoyaltyMember.objects.filter(customer=customer).first()
    if not member:
        return None
    with transaction.atomic():
        settle_reservations(member)
        return (
            LoyaltyDiscount.objects.select_for_update()
            .filter(member=member, status='available')
            .order_by('created_at').first()
        )


def reserve_discount(discount, payment_id):
    discount.status = 'reserved'
    discount.reserved_payment_id = payment_id
    discount.reserved_at = timezone.now()
    discount.save(update_fields=['status', 'reserved_payment_id', 'reserved_at'])