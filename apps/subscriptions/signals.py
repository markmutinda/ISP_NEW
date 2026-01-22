"""
Subscription Signals

Handles automatic creation of trial subscriptions for new companies.
"""

import logging
from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)


@receiver(post_save, sender='core.Company')
def create_trial_subscription_for_new_company(sender, instance, created, **kwargs):
    """
    Automatically create a 14-day free trial subscription when a new Company is created.
    
    This gives ISPs immediate access to try the platform before paying.
    """
    if not created:
        return
    
    # Avoid circular imports
    from .models import CompanySubscription, NetilyPlan
    
    # Check if subscription already exists (shouldn't, but be safe)
    if CompanySubscription.objects.filter(company=instance).exists():
        logger.info(f"Company {instance.name} already has a subscription")
        return
    
    try:
        subscription = CompanySubscription.create_trial_subscription(company=instance)
        logger.info(
            f"Created trial subscription for company {instance.name}. "
            f"Trial ends: {subscription.trial_ends_at}"
        )
    except Exception as e:
        logger.error(
            f"Failed to create trial subscription for company {instance.name}: {e}"
        )