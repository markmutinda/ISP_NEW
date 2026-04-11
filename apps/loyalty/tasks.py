"""
Loyalty Celery Tasks — periodic and event-driven loyalty automation.
"""
import logging
from celery import shared_task
from django_tenants.utils import get_tenant_model, tenant_context

logger = logging.getLogger(__name__)


@shared_task(name='apps.loyalty.tasks.award_monthly_tenure_bonus')
def award_monthly_tenure_bonus():
    """
    Award monthly tenure bonus to all active loyalty members.
    Run daily — only awards once per calendar month per member.
    """
    from django.utils import timezone

    TenantModel = get_tenant_model()
    now = timezone.now()

    for tenant in TenantModel.objects.exclude(schema_name='public'):
        with tenant_context(tenant):
            try:
                _award_tenure_for_tenant(now)
            except Exception as e:
                logger.error(f'[{tenant.schema_name}] tenure bonus error: {e}')


def _award_tenure_for_tenant(now):
    from .models import LoyaltySettings, LoyaltyMember, PointsTransaction

    settings_obj = LoyaltySettings.load()
    if not settings_obj.program_active or settings_obj.tenure_monthly_bonus <= 0:
        return

    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    for member in LoyaltyMember.objects.select_related('customer').all():
        # Skip if already awarded this month
        already = PointsTransaction.objects.filter(
            member=member,
            transaction_type='bonus',
            description__contains='Monthly tenure',
            created_at__gte=month_start,
        ).exists()
        if already:
            continue

        member.award_points(
            points=settings_obj.tenure_monthly_bonus,
            description=f'Monthly tenure bonus ({now.strftime("%B %Y")})',
            transaction_type='bonus',
        )


@shared_task(name='apps.loyalty.tasks.enroll_missing_customers')
def enroll_missing_customers():
    """
    Catch-all: enroll any customers not yet in the loyalty program.
    Handles edge cases where the signal might have been missed.
    """
    TenantModel = get_tenant_model()

    for tenant in TenantModel.objects.exclude(schema_name='public'):
        with tenant_context(tenant):
            try:
                _enroll_missing_for_tenant()
            except Exception as e:
                logger.error(f'[{tenant.schema_name}] enroll missing error: {e}')


def _enroll_missing_for_tenant():
    from .models import LoyaltySettings, LoyaltyTier, LoyaltyMember
    from apps.customers.models import Customer

    settings_obj = LoyaltySettings.load()
    if not settings_obj.program_active or not settings_obj.auto_enroll_new_customers:
        return

    bronze = LoyaltyTier.objects.filter(level='bronze').first()
    enrolled_ids = set(LoyaltyMember.objects.values_list('customer_id', flat=True))

    for customer in Customer.objects.exclude(id__in=enrolled_ids):
        member = LoyaltyMember.objects.create(
            customer=customer,
            tier=bronze,
            joined_date=customer.created_at,
        )
        if settings_obj.signup_bonus > 0:
            member.award_points(
                points=settings_obj.signup_bonus,
                description='Welcome bonus (auto-enrolled)',
                transaction_type='bonus',
            )
