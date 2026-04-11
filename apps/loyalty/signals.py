"""
Loyalty Signals — auto-award points on payment, auto-enroll customers.
"""
import logging
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(post_save, sender='billing.Payment')
def award_loyalty_points_on_payment(sender, instance, created, **kwargs):
    """
    When a payment is marked COMPLETED, award loyalty points based on amount.
    """
    if instance.status != 'COMPLETED':
        return
    customer = instance.customer
    if not customer:
        return

    try:
        from .models import LoyaltySettings, LoyaltyMember

        settings_obj = LoyaltySettings.load()
        if not settings_obj.program_active:
            return

        member, _ = LoyaltyMember.objects.get_or_create(customer=customer)
        # Update spent tracking
        amount = float(instance.amount)
        member.total_spent += instance.amount
        member.total_payments += 1
        member.save(update_fields=['total_spent', 'total_payments', 'updated_at'])

        # Calculate points: (amount / currency_unit) * points_per_currency
        if settings_obj.currency_unit > 0:
            base_points = int(amount / settings_obj.currency_unit) * settings_obj.points_per_currency
        else:
            base_points = 0

        if base_points > 0:
            member.award_points(
                points=base_points,
                description=f'Payment of KES {amount:,.2f} (ref: {instance.reference or ""})',
                transaction_type='earned',
            )

            # SMS notification
            if settings_obj.notify_points_earned:
                try:
                    from apps.messaging.tasks import send_loyalty_notification_sms
                    send_loyalty_notification_sms.delay(
                        customer_id=customer.id,
                        message_type='points_earned',
                        points=base_points,
                        reason=f'Payment KES {amount:,.2f}',
                    )
                except Exception:
                    pass

    except Exception as e:
        logger.error(f'Loyalty points on payment failed: {e}')


@receiver(post_save, sender='customers.Customer')
def auto_enroll_customer_in_loyalty(sender, instance, created, **kwargs):
    """
    Auto-enroll new customers into the loyalty program with signup bonus.
    """
    if not created:
        return
    try:
        from .models import LoyaltySettings, LoyaltyMember, LoyaltyTier

        settings_obj = LoyaltySettings.load()
        if not settings_obj.program_active or not settings_obj.auto_enroll_new_customers:
            return

        bronze = LoyaltyTier.objects.filter(level='bronze').first()
        member, was_created = LoyaltyMember.objects.get_or_create(
            customer=instance,
            defaults={'tier': bronze}
        )
        if was_created and settings_obj.signup_bonus > 0:
            member.award_points(
                points=settings_obj.signup_bonus,
                description='Welcome bonus',
                transaction_type='bonus',
            )
    except Exception as e:
        logger.error(f'Loyalty auto-enroll failed: {e}')
