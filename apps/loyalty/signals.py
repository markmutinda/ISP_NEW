"""
Loyalty Signals — auto-award points on payment, auto-enroll customers, and hotspot sessions.
"""
import logging
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.db import transaction
from django.db.models import F

logger = logging.getLogger(__name__)


@receiver(post_save, sender='billing.Payment')
def award_loyalty_points_on_payment(sender, instance, created, **kwargs):
    """
    When a payment is marked COMPLETED, award loyalty points.
    Uses on_commit so the award runs after the Payment row is committed.
    """
    if instance.status != 'COMPLETED':
        return
    customer = instance.customer
    if not customer:
        return

    payment_id = instance.pk

    def _do_award():
        try:
            from apps.billing.models.payment_models import Payment as P
            from .models import LoyaltySettings, LoyaltyMember

            try:
                inst = P.objects.get(pk=payment_id)
            except P.DoesNotExist:
                return

            settings_obj = LoyaltySettings.load()
            if not settings_obj.program_active:
                return

            member, _ = LoyaltyMember.objects.get_or_create(customer=inst.customer)
            amount = float(inst.amount)
            member.total_spent += inst.amount
            member.total_payments += 1
            member.save(update_fields=['total_spent', 'total_payments', 'updated_at'])

            if settings_obj.currency_unit > 0:
                base_points = int(amount / settings_obj.currency_unit) * settings_obj.points_per_currency
            else:
                base_points = 0

            if base_points > 0:
                member.award_points(
                    points=base_points,
                    description=f'Payment of KES {amount:,.2f} (ref: {inst.reference or ""})',
                    transaction_type='earned',
                )

                if settings_obj.notify_points_earned:
                    try:
                        from apps.messaging.tasks import send_loyalty_notification_sms
                        send_loyalty_notification_sms.delay(
                            customer_id=inst.customer.id,
                            message_type='points_earned',
                            points=base_points,
                            reason=f'Payment KES {amount:,.2f}',
                        )
                    except Exception:
                        pass

        except Exception as e:
            logger.error(f'Loyalty points on payment (deferred) failed: {e}')

    transaction.on_commit(_do_award)


@receiver(post_save, sender='customers.Customer')
def auto_enroll_customer_in_loyalty(sender, instance, created, **kwargs):
    """
    Auto-enroll new customers and award signup bonus.
    Uses on_commit so the award runs after the Customer row is committed —
    this prevents the bonus from being rolled back if the outer transaction
    encounters an error.
    """
    if not created:
        return

    customer_id = instance.pk

    def _do_enroll():
        try:
            from apps.customers.models import Customer
            from .models import LoyaltySettings, LoyaltyMember, LoyaltyTier

            try:
                cust = Customer.objects.get(pk=customer_id)
            except Customer.DoesNotExist:
                return

            settings_obj = LoyaltySettings.load()
            if not settings_obj.program_active or not settings_obj.auto_enroll_new_customers:
                return

            # Ensure standard tiers exist for this tenant
            tier_defaults = [
                ('bronze',   'Bronze',   0,    49,   '1.00', 'bg-amber-500'),
                ('silver',   'Silver',   50,   199,  '1.25', 'bg-slate-400'),
                ('gold',     'Gold',     200,  499,  '1.50', 'bg-yellow-500'),
                ('platinum', 'Platinum', 500,  999,  '2.00', 'bg-slate-600'),
                ('diamond',  'Diamond',  1000, None, '3.00', 'bg-cyan-500'),
            ]
            for level, name, min_p, max_p, mult, color in tier_defaults:
                LoyaltyTier.objects.get_or_create(level=level, defaults={
                    'name': name, 'min_points': min_p, 'max_points': max_p,
                    'points_multiplier': mult, 'color': color,
                })

            bronze = LoyaltyTier.objects.filter(level='bronze').first()
            member, was_created = LoyaltyMember.objects.get_or_create(
                customer=cust,
                defaults={'tier': bronze}
            )
            if was_created and settings_obj.signup_bonus > 0:
                member.award_points(
                    points=settings_obj.signup_bonus,
                    description='Welcome bonus',
                    transaction_type='bonus',
                )
                logger.info(f'Loyalty: enrolled {cust} with {settings_obj.signup_bonus} pts')

        except Exception as e:
            logger.error(f'Loyalty auto-enroll failed for customer_id={customer_id}: {e}', exc_info=True)

    transaction.on_commit(_do_enroll)


@receiver(pre_save, sender='billing.HotspotSession')
def _track_hotspot_session_prev_status(sender, instance, **kwargs):
    """Cache previous status so post_save knows if this is a new activation."""
    if instance.pk:
        try:
            instance._prev_status = sender.objects.get(pk=instance.pk).status
        except sender.DoesNotExist:
            instance._prev_status = None
    else:
        instance._prev_status = None


@receiver(post_save, sender='billing.HotspotSession')
def award_hotspot_loyalty_points(sender, instance, created, **kwargs):
    """Award loyalty points when a hotspot session becomes active (first time only)."""
    if instance.status != 'active':
        return
    prev_status = getattr(instance, '_prev_status', None)
    if prev_status == 'active':
        return  # Already was active — skip double-award
    if not instance.hotspot_client:
        return

    session_id = str(instance.session_id)

    def _do_award():
        try:
            from apps.billing.models.hotspot_models import HotspotSession
            from .models import LoyaltySettings, LoyaltyMember, PointsTransaction
            from decimal import Decimal

            session = HotspotSession.objects.select_related('hotspot_client', 'plan').get(
                session_id=session_id
            )
            if not session.hotspot_client:
                return

            settings_obj = LoyaltySettings.load()
            if not settings_obj.program_active:
                return

            # Idempotency: check if points already awarded for this session
            dedup_desc = f'Hotspot session {session_id}'
            member, _ = LoyaltyMember.get_or_create_for_hotspot(session.hotspot_client)
            if not member:
                return

            if PointsTransaction.objects.filter(
                member=member, description=dedup_desc
            ).exists():
                return  # Already awarded

            amount = float(session.amount)
            if settings_obj.currency_unit <= 0:
                return
            points = int(amount / settings_obj.currency_unit) * settings_obj.points_per_currency

            if points > 0:
                member.award_points(
                    points=points,
                    description=dedup_desc,
                    transaction_type='earned',
                )
                # Update spend analytics using F expressions
                LoyaltyMember.objects.filter(pk=member.pk).update(
                    total_spent=F('total_spent') + Decimal(str(amount)),
                    total_payments=F('total_payments') + 1,
                )
                logger.info(
                    f'Loyalty: awarded {points} pts to hotspot client '
                    f'{session.hotspot_client.canonical_username} for KES {amount:.0f}'
                )

        except Exception as e:
            logger.error(f'Hotspot loyalty award error: {e}', exc_info=True)

    transaction.on_commit(_do_award)