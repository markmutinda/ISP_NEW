"""
Subscription Views for Netily Platform

These views handle ISP subscription management - where ISP companies
pay Netily for access to the platform.
"""

import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction, connection
from django.db.models import Sum, Count
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django_tenants.utils import schema_context

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.services.netily_paybill_service import stk_push_own_paybill, NetilyPaybillError
from requests.exceptions import RequestException
from apps.core.models import Company

from .models import (
    NetilyPlan,
    CompanySubscription,
    BillingCycle,
    SubscriptionPayment,
    ISPPayoutConfig,
    ISPSettlement,
    CommissionLedger,
)
from .serializers import (
    NetilyPlanSerializer,
    CompanySubscriptionSerializer,
    SubscriptionUsageSerializer,
    BillingCycleBreakdownSerializer,
    InitiateSubscriptionPaymentSerializer,
    SubscriptionPaymentSerializer,
    SubscriptionPaymentStatusSerializer,
    ISPPayoutConfigSerializer,
    ISPPayoutConfigUpdateSerializer,
    VerifyPayoutSerializer,
    ISPSettlementSerializer,
    SettlementSummarySerializer,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
#  NETILY SUBSCRIPTION PLANS
# ─────────────────────────────────────────────────────────────

class NetilyPlanViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for listing Netily subscription plans.
    
    GET /api/v1/subscriptions/plans/
    GET /api/v1/subscriptions/plans/{id}/
    """
    
    serializer_class = NetilyPlanSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        # Plans live in public schema (SHARED_APPS) — must query explicitly
        from django_tenants.utils import schema_context
        with schema_context('public'):
            return list(NetilyPlan.objects.filter(is_active=True).order_by('sort_order', 'price_monthly'))
    
    def list(self, request, *args, **kwargs):
        from django_tenants.utils import schema_context
        with schema_context('public'):
            return super().list(request, *args, **kwargs)


class CurrentSubscriptionView(APIView):
    """
    Get the current company's subscription details.
    Auto-creates a 14-day trial subscription for new companies.
    
    GET /api/v1/subscriptions/current/
    """
    
    permission_classes = [IsAuthenticated]
    
    def get_company(self, request):
        """Get the company from tenant or user context"""
        try:
            # First, try to get from tenant
            tenant = getattr(request, 'tenant', None)
            if tenant:
                company = getattr(tenant, 'company', None)
                if company:
                    return company
            
            # Fall back to user's company
            user = request.user
            if hasattr(user, 'company') and user.company:
                return user.company
            
            # For superusers without a company, return None (they can't have subscriptions)
            return None
        except Exception as e:
            logger.error(f"Error getting company: {e}")
            return None
    
    def get(self, request):
        try:
            company = self.get_company(request)
            
            if not company:
                # For superusers or users without company, return a meaningful response
                if request.user.is_superuser:
                    return Response({
                        'message': 'Superuser account - no subscription required',
                        'is_superuser': True,
                        'subscription': None
                    })
                return Response(
                    {'error': 'No company associated with your account'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Use public schema for subscriptions (they're in SHARED_APPS)
            with schema_context('public'):
                try:
                    subscription = CompanySubscription.objects.select_related('plan').get(
                        company=company
                    )
                except CompanySubscription.DoesNotExist:
                    # Auto-create trial subscription for new companies
                    starter_plan = NetilyPlan.objects.filter(code='starter', is_active=True).first()
                    if not starter_plan:
                        return Response(
                            {'error': 'No subscription plans available. Please contact support.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR
                        )
                    
                    # Create trial subscription using the class method
                    try:
                        subscription = CompanySubscription.create_trial_subscription(
                            company=company,
                            plan=starter_plan
                        )
                        logger.info(f"Auto-created trial subscription for company: {company.name}")
                    except Exception as e:
                        logger.error(f"Failed to create trial subscription: {e}")
                        return Response(
                            {'error': 'Failed to initialize subscription. Please contact support.'},
                            status=status.HTTP_500_INTERNAL_SERVER_ERROR
                        )
                
                # Build response with trial warnings
                data = CompanySubscriptionSerializer(subscription).data

                # Normalize status from the authoritative subscription state.
                # A paid/converted subscription may retain historical trial dates, so
                # never downgrade an active paid account because trial_ends_at is past.
                if (
                    subscription.status == 'active'
                    and subscription.current_period_end
                    and subscription.current_period_end > timezone.now()
                ):
                    data['status'] = 'active'
                    data['trial_expired'] = False
                    data['access_restricted'] = False
                elif subscription.trial_expired and data.get('status') not in ('expired', 'past_due', 'cancelled'):
                    data['status'] = 'expired'

                # Add trial-specific messaging
                if subscription.is_on_trial:
                    days = subscription.trial_days_remaining
                    data['trial_message'] = f"You have {days} day{'s' if days != 1 else ''} left in your free trial."
                    if days <= 3:
                        data['trial_warning'] = "Your trial is ending soon! Subscribe now to keep access."
                elif subscription.trial_expired:
                    data['trial_message'] = "Your free trial has expired."
                    data['trial_warning'] = "Please subscribe to continue using Netily."
                    data['access_restricted'] = True
                    data['activation_fee'] = str(subscription.plan.activation_fee)
            
            return Response(data)
            
        except Exception as e:
            logger.error(f"Error in CurrentSubscriptionView.get: {e}", exc_info=True)
            return Response(
                {'error': 'An error occurred retrieving subscription details'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


class SubscriptionUsageView(APIView):
    """
    Get current usage statistics against subscription limits.
    
    GET /api/v1/subscriptions/usage/
    """
    
    permission_classes = [IsAuthenticated]
    
    def get_company(self, request):
        """Get the company from tenant or user context"""
        # First, try to get from tenant
        tenant = getattr(request, 'tenant', None)
        if tenant:
            company = getattr(tenant, 'company', None)
            if company:
                return company
        
        # Fall back to user's company
        user = request.user
        if hasattr(user, 'company') and user.company:
            return user.company
        
        return None
    
    def get(self, request):
        company = self.get_company(request)
        
        if not company:
            # For superusers or users without company, return empty usage
            if request.user.is_superuser:
                return Response({
                    'message': 'Superuser account - no usage limits',
                    'is_superuser': True,
                    'usage': None
                })
            return Response(
                {'error': 'No company associated with your account'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Use public schema for subscriptions (they're in SHARED_APPS)
        with schema_context('public'):
            try:
                subscription = CompanySubscription.objects.select_related('plan').get(
                    company=company
                )
                plan = subscription.plan
            except CompanySubscription.DoesNotExist:
                return Response(
                    {'error': 'No active subscription'},
                    status=status.HTTP_404_NOT_FOUND
                )
        
        # Get current counts using tenant context
        # These queries run in the tenant schema
        from apps.customers.models import Customer
        from apps.network.models.router_models import Router
        from apps.core.models import User
        
        current_subscribers = Customer.objects.count()
        current_routers = Router.objects.filter(is_active=True).count()
        current_staff = User.objects.filter(
            role__in=['admin', 'staff', 'technician', 'accountant', 'support']
        ).count()
        
        # Calculate percentages
        def calc_percent(current, maximum):
            if maximum == 0:  # Unlimited
                return 0
            return min(100, int((current / maximum) * 100))
        
        subscribers_percent = calc_percent(current_subscribers, plan.max_subscribers)
        routers_percent = calc_percent(current_routers, plan.max_routers)
        staff_percent = calc_percent(current_staff, plan.max_staff)
        
        # Check for warnings
        warnings = []
        is_near_limit = False
        
        if plan.max_subscribers > 0 and subscribers_percent >= 80:
            is_near_limit = True
            if subscribers_percent >= 100:
                warnings.append(f"You've reached your subscriber limit ({plan.max_subscribers})")
            else:
                warnings.append(f"You're using {subscribers_percent}% of your subscriber limit")
        
        if plan.max_routers > 0 and routers_percent >= 80:
            is_near_limit = True
            if routers_percent >= 100:
                warnings.append(f"You've reached your router limit ({plan.max_routers})")
            else:
                warnings.append(f"You're using {routers_percent}% of your router limit")
        
        # Add trial warning if applicable
        if subscription.trial_expired:
            warnings.insert(0, "Your free trial has expired. Please subscribe to continue.")
        elif subscription.is_on_trial and subscription.trial_days_remaining <= 3:
            warnings.insert(0, f"Trial ending in {subscription.trial_days_remaining} days. Subscribe now!")

        tenant = getattr(request, 'tenant', None)
        if not tenant:
            tenant = getattr(company, 'tenant', None)
            if not tenant and hasattr(company, 'tenant_set'):
                tenant = company.tenant_set.first()

        metered_data = {
            'is_metered': bool(plan.is_metered),
            'billing_cycle_id': None,
            'billing_cycle_start': None,
            'billing_cycle_end': None,
            'hotspot_revenue_accrued': Decimal('0.00'),
            'hotspot_revenue_share_pct': Decimal(str(plan.hotspot_revenue_share_pct or 0)),
            'hotspot_revenue_share_amount': Decimal('0.00'),
            'hotspot_minimum_charge': Decimal('0.00'),
            'hotspot_billable_charge': Decimal('0.00'),
            'pppoe_count': 0,
            'billable_pppoe': 0,
            'pppoe_unit_price': Decimal(str(plan.pppoe_unit_price or 25)) if plan.is_metered else Decimal('0.00'),
            'pppoe_charge': Decimal('0.00'),
            'usage_subtotal': Decimal('0.00'),
            'minimum_charge': Decimal(str(plan.base_license_fee or 500)) if plan.is_metered else Decimal('0.00'),
            'minimum_adjustment': Decimal(str(plan.base_license_fee or 500)) if plan.is_metered else Decimal('0.00'),
            'total_estimate': Decimal(str(plan.base_license_fee or 500)) if plan.is_metered else Decimal('0.00'),
            'invoice_adjustment_amount': Decimal('0.00'),
            'invoice_discount_amount': Decimal('0.00'),
            'invoice_total_estimate': None,
            'invoice_number': '',
            'invoice_adjustment_note': '',
            'hotspot_revenue_note': 'Hotspot revenue is reconciled from paid hotspot sessions in the active billing cycle.',
        }

        if plan.is_metered and tenant:
            with schema_context('public'):
                active_cycle = BillingCycle.get_current_active_cycle(
                    tenant,
                    subscription,
                    create=subscription.status in ['active', 'trial'],
                )

                if not active_cycle:
                    logger.warning(
                        "No current active billing cycle found for company=%s tenant=%s",
                        company.id,
                        tenant.schema_name,
                    )

                if active_cycle:
                    fallback_pct = Decimal(str(plan.hotspot_revenue_share_pct or 0)) or Decimal('3.00')
                    updates = {}
                    if not active_cycle.snapshot_hotspot_share_pct:
                        active_cycle.snapshot_hotspot_share_pct = fallback_pct
                        updates['snapshot_hotspot_share_pct'] = fallback_pct
                    if not active_cycle.snapshot_base_fee:
                        active_cycle.snapshot_base_fee = Decimal(str(plan.base_license_fee or 500))
                        updates['snapshot_base_fee'] = active_cycle.snapshot_base_fee
                    if not active_cycle.snapshot_pppoe_price:
                        active_cycle.snapshot_pppoe_price = Decimal(str(plan.pppoe_unit_price or 25))
                        updates['snapshot_pppoe_price'] = active_cycle.snapshot_pppoe_price
                    if updates:
                        BillingCycle.objects.filter(pk=active_cycle.pk).update(**updates)

                    hotspot_revenue_details = active_cycle.get_actual_hotspot_revenue_details()
                    actual_hotspot_revenue = active_cycle.refresh_actual_hotspot_revenue(
                        hotspot_revenue_details["revenue"]
                    )
                    hotspot_share = active_cycle.calculate_hotspot_revenue_share(actual_hotspot_revenue)
                    billable_pppoe = active_cycle.calculate_total_pppoe()
                    pppoe_charge = active_cycle.calculate_pppoe_charge()
                    usage_subtotal = (pppoe_charge + hotspot_share).quantize(Decimal('0.01'))
                    minimum_charge = active_cycle.snapshot_base_fee or Decimal('500.00')
                    minimum_adjustment = max(
                        minimum_charge - usage_subtotal,
                        Decimal('0.00'),
                    ).quantize(Decimal('0.01'))
                    total_estimate = max(usage_subtotal, minimum_charge).quantize(Decimal('0.01'))
                    invoice_adjustment_amount = Decimal('0.00')
                    invoice_discount_amount = Decimal('0.00')
                    invoice_total_estimate = None
                    invoice_number = ''
                    invoice_adjustment_note = ''

                    if active_cycle.invoice_reference:
                        try:
                            with schema_context(tenant.schema_name):
                                from apps.billing.models import Invoice
                                invoice = Invoice.objects.filter(pk=active_cycle.invoice_reference).first()
                                if invoice:
                                    invoice_adjustment_amount = (
                                        invoice.items.filter(service_type='netily_manual_adjustment')
                                        .aggregate(total=Sum('total'))['total'] or Decimal('0.00')
                                    ).quantize(Decimal('0.01'))
                                    invoice_discount_amount = (invoice.discount_amount or Decimal('0.00')).quantize(Decimal('0.01'))
                                    invoice_total_estimate = (invoice.total_amount or Decimal('0.00')).quantize(Decimal('0.01'))
                                    invoice_number = invoice.invoice_number
                                    if invoice_adjustment_amount or invoice_discount_amount:
                                        invoice_adjustment_note = 'Adjusted by Netily Support on the linked invoice.'
                        except Exception as invoice_err:
                            logger.warning(
                                "Failed loading adjusted subscription invoice for %s: %s",
                                tenant.schema_name,
                                invoice_err,
                            )

                    metered_data.update({
                        'billing_cycle_id': str(active_cycle.id),
                        'billing_cycle_start': active_cycle.start_date,
                        'billing_cycle_end': active_cycle.end_date,
                        'hotspot_revenue_accrued': actual_hotspot_revenue,
                        'hotspot_revenue_share_pct': active_cycle.snapshot_hotspot_share_pct,
                        'hotspot_revenue_share_amount': hotspot_share,
                        'hotspot_minimum_charge': hotspot_share,
                        'hotspot_billable_charge': hotspot_share,
                        'pppoe_count': billable_pppoe,
                        'billable_pppoe': billable_pppoe,
                        'pppoe_unit_price': active_cycle.snapshot_pppoe_price,
                        'pppoe_charge': pppoe_charge,
                        'usage_subtotal': usage_subtotal,
                        'minimum_charge': minimum_charge,
                        'minimum_adjustment': minimum_adjustment,
                        'total_estimate': invoice_total_estimate or total_estimate,
                        'invoice_adjustment_amount': invoice_adjustment_amount,
                        'invoice_discount_amount': invoice_discount_amount,
                        'invoice_total_estimate': invoice_total_estimate,
                        'invoice_number': invoice_number,
                        'invoice_adjustment_note': invoice_adjustment_note,
                        'hotspot_revenue_count': hotspot_revenue_details["count"],
                        'hotspot_revenue_source': hotspot_revenue_details["source"],
                        'hotspot_revenue_note': (
                            'Hotspot revenue is reconciled from completed hotspot payments in the active billing cycle.'
                            if hotspot_revenue_details["source"] == "completed_hotspot_payments"
                            else 'Hotspot revenue is reconciled from legacy paid hotspot sessions because no completed hotspot payment records were found in this cycle.'
                        ),
                    })
        
        data = {
            'plan_name': plan.name,
            'plan_code': plan.code,
            'current_subscribers': current_subscribers,
            'current_routers': current_routers,
            'current_staff': current_staff,
            'max_subscribers': plan.max_subscribers,
            'max_routers': plan.max_routers,
            'max_staff': plan.max_staff,
            'subscribers_usage_percent': subscribers_percent,
            'routers_usage_percent': routers_percent,
            'staff_usage_percent': staff_percent,
            'is_near_limit': is_near_limit,
            'warnings': warnings,
            # Trial status
            'is_on_trial': subscription.is_on_trial,
            'trial_days_remaining': subscription.trial_days_remaining,
            'trial_expired': subscription.trial_expired,
            'subscription_status': subscription.status,
            **metered_data,
        }
        
        serializer = SubscriptionUsageSerializer(data)
        return Response(serializer.data)


class MeteredBillingEstimateView(APIView):
    """
    Real-time estimate of the current billing cycle cost for metered plans.

    GET /api/v1/subscriptions/metered-estimate/
    Returns base fee, PPPoE charge breakdown, hotspot share %, and total estimate.
    For non-metered plans returns is_metered: false with no breakdown.
    Result is cached in Redis for 8 hours; Celery task refreshes it 3× / day.
    """

    permission_classes = [IsAuthenticated]

    def _get_company(self, request):
        tenant = getattr(request, 'tenant', None)
        if tenant:
            company = getattr(tenant, 'company', None)
            if company:
                return company
        user = request.user
        if hasattr(user, 'company') and user.company:
            return user.company
        return None

    def get(self, request):
        company = self._get_company(request)
        if not company:
            return Response({'error': 'No company associated with your account'},
                            status=status.HTTP_400_BAD_REQUEST)

        cache_key = f'metered_estimate:{company.pk}'
        from django.core.cache import cache
        cached = cache.get(cache_key)
        if cached:
            return Response(cached)

        with schema_context('public'):
            try:
                subscription = CompanySubscription.objects.select_related('plan').get(
                    company=company
                )
                plan = subscription.plan
            except CompanySubscription.DoesNotExist:
                return Response({'error': 'No active subscription'}, status=status.HTTP_404_NOT_FOUND)

        if not plan.is_metered:
            return Response({'is_metered': False, 'plan_name': plan.name})

        tenant = getattr(request, 'tenant', None)
        if not tenant:
            tenant = getattr(company, 'tenant', None)
            if not tenant and hasattr(company, 'tenant_set'):
                tenant = company.tenant_set.first()

        # Count current PPPoE/customer footprint for the live estimate.
        from apps.customers.models import Customer
        pppoe_count = Customer.objects.count()

        pppoe_unit = Decimal(str(plan.pppoe_unit_price))
        minimum_charge = Decimal(str(plan.base_license_fee or 500))
        hotspot_share_pct = Decimal(str(plan.hotspot_revenue_share_pct or 0)) or Decimal('3.00')
        hotspot_revenue = Decimal('0.00')
        cycle_id = None
        cycle_start = None
        cycle_end = None

        if tenant:
            with schema_context('public'):
                active_cycle = BillingCycle.objects.filter(
                    tenant=tenant,
                    subscription=subscription,
                    status='active',
                ).select_related('tenant').order_by('-start_date').first()
                if active_cycle:
                    if not active_cycle.snapshot_hotspot_share_pct:
                        active_cycle.snapshot_hotspot_share_pct = hotspot_share_pct
                        BillingCycle.objects.filter(pk=active_cycle.pk).update(
                            snapshot_hotspot_share_pct=hotspot_share_pct
                        )
                    hotspot_share_pct = active_cycle.snapshot_hotspot_share_pct
                    minimum_charge = active_cycle.snapshot_base_fee or minimum_charge
                    pppoe_unit = active_cycle.snapshot_pppoe_price or pppoe_unit
                    hotspot_revenue = active_cycle.refresh_actual_hotspot_revenue()
                    cycle_id = str(active_cycle.id)
                    cycle_start = active_cycle.start_date.isoformat()
                    cycle_end = active_cycle.end_date.isoformat()
                else:
                    active_cycle = BillingCycle.objects.create(
                        tenant=tenant,
                        subscription=subscription,
                        start_date=subscription.current_period_start or timezone.now(),
                        end_date=subscription.current_period_end or (timezone.now() + timedelta(days=30)),
                        status='active',
                    )
                    minimum_charge = active_cycle.snapshot_base_fee or minimum_charge
                    pppoe_unit = active_cycle.snapshot_pppoe_price or pppoe_unit
                    hotspot_share_pct = active_cycle.snapshot_hotspot_share_pct or hotspot_share_pct
                    hotspot_revenue = active_cycle.refresh_actual_hotspot_revenue()
                    cycle_id = str(active_cycle.id)
                    cycle_start = active_cycle.start_date.isoformat()
                    cycle_end = active_cycle.end_date.isoformat()

        billable_pppoe = pppoe_count
        pppoe_charge = Decimal(billable_pppoe) * pppoe_unit
        hotspot_share_amount = (hotspot_revenue * hotspot_share_pct / Decimal('100.0')).quantize(Decimal('0.01'))
        usage_subtotal = (pppoe_charge + hotspot_share_amount).quantize(Decimal('0.01'))
        minimum_adjustment = max(minimum_charge - usage_subtotal, Decimal('0.00')).quantize(Decimal('0.01'))
        total_estimate = max(usage_subtotal, minimum_charge).quantize(Decimal('0.01'))

        data = {
            'is_metered': True,
            'plan_name': plan.name,
            'billing_cycle_id': cycle_id,
            'billing_cycle_start': cycle_start,
            'billing_cycle_end': cycle_end,
            'activation_fee': str(minimum_charge),
            'minimum_charge': str(minimum_charge),
            'base_fee': '0.00',
            'pppoe_count': pppoe_count,
            'pppoe_min_clients': 0,
            'pppoe_unit_price': str(pppoe_unit),
            'billable_pppoe': billable_pppoe,
            'pppoe_charge': str(pppoe_charge),
            'hotspot_share_pct': str(hotspot_share_pct),
            'hotspot_revenue_accrued': str(hotspot_revenue),
            'hotspot_revenue_share_amount': str(hotspot_share_amount),
            'hotspot_billable_charge': str(hotspot_share_amount),
            'usage_subtotal': str(usage_subtotal),
            'minimum_adjustment': str(minimum_adjustment),
            'total_estimate': str(total_estimate),
            'note': 'Estimate uses current PPPoE footprint plus actual hotspot revenue reconciled from the active billing cycle.',
        }
        cache.set(cache_key, data, timeout=60 * 60 * 8)  # 8-hour TTL
        return Response(data)


class BillingCycleBreakdownHistoryView(APIView):
    """
    Return previous Netily billing cycle breakdowns for the current tenant.

    This is intentionally separate from the live estimate endpoint. Closed
    cycles should be audit/history data, while the estimate may change as usage
    is reconciled during the active cycle.
    """

    permission_classes = [IsAuthenticated]

    def _get_company(self, request):
        tenant = getattr(request, 'tenant', None)
        if tenant:
            company = getattr(tenant, 'company', None)
            if company:
                return company
        user = request.user
        if hasattr(user, 'company') and user.company:
            return user.company
        return None

    def _get_tenant(self, request, company):
        tenant = getattr(request, 'tenant', None)
        if tenant:
            return tenant
        tenant = getattr(company, 'tenant', None)
        if tenant:
            return tenant
        if hasattr(company, 'tenant_set'):
            return company.tenant_set.first()
        return None

    def _invoice_payload(self, tenant, cycle):
        payload = {
            'invoice_id': None,
            'invoice_number': '',
            'invoice_status': '',
            'invoice_total': None,
            'invoice_amount_paid': None,
            'invoice_balance': None,
            'invoice_items': [],
        }
        if not tenant or not cycle.invoice_reference:
            return payload

        try:
            with schema_context(tenant.schema_name):
                from apps.billing.models import Invoice

                invoice = Invoice.objects.filter(pk=cycle.invoice_reference).first()
                if not invoice:
                    return payload

                payload.update({
                    'invoice_id': str(invoice.id),
                    'invoice_number': invoice.invoice_number or '',
                    'invoice_status': invoice.status or '',
                    'invoice_total': invoice.total_amount,
                    'invoice_amount_paid': invoice.amount_paid,
                    'invoice_balance': invoice.balance,
                })
                items = []
                for item in invoice.items.all().order_by('id'):
                    quantity = item.quantity or Decimal('0')
                    unit_price = item.unit_price or Decimal('0')
                    amount = getattr(item, 'total', None) or (quantity * unit_price)
                    items.append({
                        'description': item.description or '',
                        'quantity': quantity,
                        'unit_price': unit_price,
                        'amount': amount,
                    })
                payload['invoice_items'] = items
        except Exception as exc:
            logger.warning(
                "Failed loading invoice breakdown for billing cycle %s tenant=%s: %s",
                cycle.id,
                getattr(tenant, 'schema_name', None),
                exc,
            )
        return payload

    def _cycle_payload(self, tenant, cycle):
        hotspot_revenue = cycle.hotspot_revenue_accumulated or Decimal('0.00')
        pppoe_count = cycle.calculate_total_pppoe()
        pppoe_charge = cycle.calculate_pppoe_charge()
        hotspot_share_amount = cycle.calculate_hotspot_revenue_share(hotspot_revenue)
        usage_subtotal = cycle.calculate_usage_subtotal(hotspot_revenue)
        minimum_adjustment = cycle.calculate_minimum_adjustment(hotspot_revenue)
        total_charge = cycle.calculate_total_charge()

        payload = {
            'id': str(cycle.id),
            'status': cycle.status,
            'start_date': cycle.start_date,
            'end_date': cycle.end_date,
            'is_first_paid_cycle': cycle.is_first_paid_cycle,
            'minimum_charge': cycle.snapshot_base_fee or Decimal('0.00'),
            'pppoe_count': pppoe_count,
            'pppoe_unit_price': cycle.snapshot_pppoe_price or Decimal('0.00'),
            'pppoe_charge': pppoe_charge,
            'hotspot_revenue': hotspot_revenue,
            'hotspot_share_pct': cycle.snapshot_hotspot_share_pct or Decimal('0.00'),
            'hotspot_share_amount': hotspot_share_amount,
            'usage_subtotal': usage_subtotal,
            'minimum_adjustment': minimum_adjustment,
            'total_charge': total_charge,
        }
        payload.update(self._invoice_payload(tenant, cycle))
        if payload['invoice_total'] is not None:
            payload['total_charge'] = payload['invoice_total']
        return payload

    def get(self, request):
        company = self._get_company(request)
        if not company:
            return Response(
                {'error': 'No company associated with your account'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        tenant = self._get_tenant(request, company)
        if not tenant:
            return Response(
                {'error': 'No tenant associated with your account'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        limit = request.query_params.get('limit', '12')
        try:
            limit = max(1, min(int(limit), 36))
        except (TypeError, ValueError):
            limit = 12

        with schema_context('public'):
            try:
                subscription = CompanySubscription.objects.select_related('plan').get(
                    company=company
                )
            except CompanySubscription.DoesNotExist:
                return Response({'error': 'No active subscription'}, status=status.HTTP_404_NOT_FOUND)

            cycles = list(
                BillingCycle.objects.filter(
                    tenant=tenant,
                    subscription=subscription,
                )
                .select_related('tenant', 'subscription', 'subscription__plan')
                .order_by('-start_date', '-end_date')[:limit]
            )
            data = [self._cycle_payload(tenant, cycle) for cycle in cycles]

        serializer = BillingCycleBreakdownSerializer(data, many=True)
        return Response({'count': len(data), 'results': serializer.data})


class InitiateSubscriptionPaymentView(APIView):
    """
    Initiate payment for subscription via PayHero.
    
    POST /api/v1/subscriptions/pay/
    {
        "plan_id": "professional",
        "payment_method": "mpesa_stk",
        "phone_number": "254712345678",
        "billing_period": "monthly"
    }
    """
    
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        try:
            return self._do_post(request)
        except Exception as e:
            # Top-level safety net: NEVER return a silent 500.
            # Log the full traceback and return the real error to the caller.
            logger.exception("InitiateSubscriptionPaymentView unhandled error")
            return Response({
                'status': 'error',
                'message': f'Payment service error: {type(e).__name__}: {e}',
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    def _do_post(self, request):
        logger.debug(f"Subscription payment request data: {request.data}")
        
        serializer = InitiateSubscriptionPaymentSerializer(data=request.data)
        if not serializer.is_valid():
            logger.warning(f"Subscription payment validation errors: {serializer.errors}")
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        # Get the current tenant/company from request
        tenant = getattr(request, 'tenant', None)
        
        if not tenant:
            return Response(
                {'error': 'No tenant context available'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        company = getattr(tenant, 'company', None)
        if not company:
            return Response(
                {'error': 'Tenant has no associated company'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        plan = serializer.validated_data['plan']
        payment_method = serializer.validated_data['payment_method']
        billing_period = serializer.validated_data['billing_period']
        phone_number = serializer.validated_data.get('phone_number')
        amount_override = serializer.validated_data.get('amount')
        defer_billing = serializer.validated_data.get('defer_billing_to_trial_end', False)
        outstanding_invoice_balance = Decimal('0.00')
        outstanding_invoice_number = ''

        try:
            with schema_context(tenant.schema_name):
                from apps.billing.models import Invoice
                invoice = (
                    Invoice.objects.filter(invoice_number__startswith='NET-BILL')
                    .exclude(status__in=['VOIDED', 'WRITTEN_OFF', 'CANCELLED'])
                    .filter(balance__gt=0)
                    .order_by('created_at', 'id')
                    .first()
                )
                if invoice:
                    outstanding_invoice_balance = Decimal(str(invoice.balance or invoice.total_amount or '0')).quantize(Decimal('0.01'))
                    outstanding_invoice_number = invoice.invoice_number
        except Exception as invoice_err:
            logger.warning("Could not resolve outstanding NET-BILL balance for %s: %s", tenant.schema_name, invoice_err)
        
        # ─────────────────────────────────────────────────────────────
        # Amount priority:
        # 1. Explicit amount (when paying an outstanding invoice)
        # 2. base_license_fee (metered plans — first payment / trial conversion)
        # 3. price_yearly / price_monthly (flat-rate plans)
        # ─────────────────────────────────────────────────────────────
        if outstanding_invoice_balance > 0:
            amount = outstanding_invoice_balance
            logger.info(
                "Using outstanding NET-BILL balance KES %s for %s (%s); requested override was %s",
                amount,
                tenant.schema_name,
                outstanding_invoice_number,
                amount_override,
            )
        elif amount_override:
            amount = amount_override
        elif plan.is_metered:
            amount = plan.base_license_fee
        elif billing_period == 'yearly':
            amount = plan.price_yearly
        else:
            amount = plan.price_monthly
        
        logger.info(f"Calculated payment amount: KES {amount} for plan {plan.name} (is_metered={plan.is_metered}, billing_period={billing_period})")
        
        # ── DB operations ONLY inside the atomic block ──────────────
        # IMPORTANT: The external API call (STK push) must happen OUTSIDE
        # any atomic block. Mixing DB transactions with network calls causes
        # TransactionManagementError when the network call fails and the
        # except handler tries to save the failure status.
        #
        # All subscription models live in the PUBLIC schema only, so we
        # must switch context before any ORM operations.
        with schema_context('public'):
            with transaction.atomic():
                # Get or create subscription
                subscription, created = CompanySubscription.objects.get_or_create(
                    company=company,
                    defaults={
                        'plan': plan,
                        'billing_period': billing_period,
                        'current_period_start': timezone.now(),
                        'current_period_end': timezone.now(),  # Will be updated on payment
                        'status': 'pending',
                    }
                )
                
                # NOTE: Do NOT update subscription.plan here.
                # The plan switch is deferred to payment success (webhook/polling)
                # to prevent phantom plan changes from unpaid STK pushes.
                
                # Create payment record with intended plan
                payment = SubscriptionPayment.objects.create(
                    subscription=subscription,
                    intended_plan=plan,
                    intended_billing_period=billing_period,
                    amount=amount,
                    payment_method=payment_method,
                    phone_number=phone_number,
                    status='pending',
                    defer_billing_to_trial_end=defer_billing,
                    period_start=subscription.current_period_end or timezone.now(),
                    period_end=(subscription.current_period_end or timezone.now()) + timedelta(
                        days=365 if billing_period == 'yearly' else 30
                    ),
                )
        # ── End atomic block — payment record is now committed ───────
        # External API calls happen below, outside any DB transaction.
        
        # Handle different payment methods
        if payment_method == 'mpesa_stk':
            return self._handle_stk_push(payment, phone_number, amount, plan)
        
        elif payment_method == 'mpesa_paybill':
            return self._handle_paybill(payment, amount, company)
        
        elif payment_method == 'bank_transfer':
            return self._handle_bank_transfer(payment, amount, company)
        
        return Response(
            {'error': 'Unsupported payment method'},
            status=status.HTTP_400_BAD_REQUEST
        )
    
    def _handle_stk_push(self, payment, phone_number, amount, plan):
        """Initiate M-Pesa STK Push via Netily's OWN paybill (replaces Tuma passthrough).

        Runs outside any DB transaction — external API calls must never be
        wrapped in transaction.atomic(). payment.save() below is safe because
        SubscriptionPayment lives in the public schema only.
        """
        try:
            reference = f"NETILY-{plan.code.upper()}-{payment.id.hex[:8].upper()}"[:20]
            callback_url = getattr(settings, 'NETILY_SUBSCRIPTION_PAYBILL_CALLBACK', '') or settings.NETILY_PAYBILL_CALLBACK_URL

            logger.info(
                "Initiating Netily paybill STK push: phone=%s, amount=%s, ref=%s",
                phone_number, amount, reference,
            )

            result = stk_push_own_paybill(
                amount=amount,
                phone_number=phone_number,
                account_reference=reference,
                transaction_desc=f"Netily {plan.name}"[:13],
                callback_url=callback_url,
            )

            with schema_context('public'):
                payment.payhero_checkout_id = result['checkout_request_id']
                payment.payhero_reference = reference
                payment.status = 'processing'
                payment.save()

            return Response({
                'status': 'pending',
                'payment_id': str(payment.id),
                'checkout_request_id': result['checkout_request_id'],
                'merchant_request_id': result['merchant_request_id'],
                'message': result.get('customer_message') or 'STK Push sent. Check your phone and enter your M-Pesa PIN.',
            })

        except NetilyPaybillError as e:
            logger.error(f"Netily paybill STK push failed for subscription payment {payment.id}: {e}")
            with schema_context('public'):
                payment.status = 'failed'
                payment.failure_reason = str(e)
                payment.save()
            return Response({'status': 'error', 'message': str(e)}, status=status.HTTP_502_BAD_GATEWAY)

        except Exception as e:
            logger.exception(f"Unexpected error during subscription STK push: {e}")
            with schema_context('public'):
                payment.status = 'failed'
                payment.failure_reason = str(e)
                payment.save()
            return Response({
                'status': 'error',
                'message': f'Payment initiation failed: {type(e).__name__}: {e}',
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    def _handle_paybill(self, payment, amount, company):
        """Return Paybill details for manual payment"""
        # Generate unique account number
        account_number = f"NETILY-{company.slug.upper()[:10]}-{payment.id.hex[:6].upper()}"
        
        with schema_context('public'):
            payment.payhero_reference = account_number
            payment.status = 'pending'
            payment.save()
        
        return Response({
            'status': 'awaiting_payment',
            'payment_id': str(payment.id),
            'paybill_number': getattr(settings, 'NETILY_PAYBILL_NUMBER', '247247'),
            'account_number': account_number,
            'amount': int(amount),
            'message': 'Use the Paybill details to complete payment',
        })
    
    def _handle_bank_transfer(self, payment, amount, company):
        """Return bank details for manual payment"""
        reference = f"NETILY-{company.slug.upper()[:10]}-{payment.id.hex[:6].upper()}"
        
        with schema_context('public'):
            payment.bank_reference = reference
            payment.status = 'pending'
            payment.save()
        
        return Response({
            'status': 'awaiting_payment',
            'payment_id': str(payment.id),
            'bank_details': {
                'bank_name': getattr(settings, 'NETILY_BANK_NAME', 'Equity Bank'),
                'account_name': getattr(settings, 'NETILY_BANK_ACCOUNT_NAME', 'Netily Technologies Ltd'),
                'account_number': getattr(settings, 'NETILY_BANK_ACCOUNT_NUMBER', '0123456789012'),
                'branch': getattr(settings, 'NETILY_BANK_BRANCH', 'Westlands'),
            },
            'amount': int(amount),
            'reference': reference,
            'message': 'Use the bank details to complete payment. Include the reference in your transfer.',
        })


class SubscriptionPaymentViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for subscription payment history.
    
    GET /api/v1/subscriptions/payments/
    GET /api/v1/subscriptions/payments/{id}/
    """
    
    serializer_class = SubscriptionPaymentSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        user = self.request.user
        if not user.company:
            return SubscriptionPayment.objects.none()
        
        # SubscriptionPayment lives in the public schema
        with schema_context('public'):
            return SubscriptionPayment.objects.filter(
                subscription__company=user.company
            ).select_related('subscription__plan').order_by('-created_at')

    def _billing_cycle_payload(self, payment):
        subscription = payment.subscription
        company = subscription.company
        tenant = getattr(company, 'tenant', None)
        if not tenant and hasattr(company, 'tenant_set'):
            tenant = company.tenant_set.first()
        if not tenant:
            return {}

        cycle = BillingCycle.objects.filter(
            tenant=tenant,
            subscription=subscription,
        ).order_by('-start_date', '-end_date').first()
        if not cycle:
            return {}

        payload = {
            'billing_cycle_id': str(cycle.id),
            'billing_cycle_start': cycle.start_date,
            'billing_cycle_end': cycle.end_date,
            'billing_cycle_status': cycle.status,
            'invoice_reference': cycle.invoice_reference,
        }

        if cycle.invoice_reference:
            try:
                with schema_context(tenant.schema_name):
                    from apps.billing.models import Invoice
                    invoice = Invoice.objects.filter(pk=cycle.invoice_reference).first()
                    if invoice:
                        payload['invoice_number'] = invoice.invoice_number
            except Exception as exc:
                logger.warning(
                    "Failed loading invoice metadata for subscription payment %s: %s",
                    payment.id,
                    exc,
                )

        return payload

    def _subscription_is_current(self, subscription):
        return bool(
            subscription.status == 'active'
            and subscription.current_period_end
            and subscription.current_period_end > timezone.now()
        )
    
    @action(detail=True, methods=['get'])
    def status(self, request, pk=None):
        """
        Poll payment status. Source of truth is the webhook — this endpoint
        only reads DB state, it never calls out to a payment gateway.
        """
        with schema_context('public'):
            payment = self.get_object()

            if payment.status in ['completed', 'failed', 'cancelled']:
                subscription_activated = self._subscription_is_current(payment.subscription)
                if payment.status == 'completed' and not subscription_activated:
                    try:
                        from .billing_lifecycle import complete_subscription_stk_payment

                        payment, _invoice = complete_subscription_stk_payment(
                            payment,
                            mpesa_receipt=payment.mpesa_receipt or "",
                        )
                        payment.refresh_from_db()
                        subscription_activated = self._subscription_is_current(payment.subscription)
                    except Exception:
                        logger.exception("Failed repairing completed subscription payment %s during status polling", payment.id)

                return Response({
                    'payment_id': str(payment.id),
                    'status': payment.status,
                    'message': (
                        self._get_status_message(payment)
                        if payment.status != 'completed' or subscription_activated
                        else 'Payment received, but the subscription invoice still has an outstanding balance.'
                    ),
                    'mpesa_receipt': payment.mpesa_receipt,
                    'completed_at': payment.completed_at,
                    'subscription_activated': subscription_activated,
                    **self._billing_cycle_payload(payment),
                })

            return Response({
                'payment_id': str(payment.id),
                'status': 'pending',
                'message': 'Waiting for payment confirmation...',
                'mpesa_receipt': None,
                'completed_at': None,
            })
    
    def _get_status_message(self, payment):
        messages = {
            'completed': 'Payment successful! Your subscription is now active.',
            'failed': payment.failure_reason or 'Payment failed. Please try again.',
            'cancelled': 'Payment was cancelled.',
            'pending': 'Waiting for payment...',
            'processing': 'Processing payment...',
        }
        return messages.get(payment.status, 'Unknown status')


class CancelSubscriptionView(APIView):
    """
    Cancel subscription.
    
    POST /api/v1/subscriptions/cancel/
    {
        "immediate": false  // Cancel at end of period if false
    }
    """
    
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        user = request.user
        
        if not user.company:
            return Response(
                {'error': 'User is not associated with a company'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        with schema_context('public'):
            try:
                subscription = CompanySubscription.objects.get(company=user.company)
            except CompanySubscription.DoesNotExist:
                return Response(
                    {'error': 'No active subscription to cancel'},
                    status=status.HTTP_404_NOT_FOUND
                )
            
            immediate = request.data.get('immediate', False)
            subscription.cancel(immediate=immediate)
        
        if immediate:
            message = 'Subscription cancelled immediately.'
        else:
            message = f'Subscription will be cancelled at the end of the current period ({subscription.current_period_end.date()}).'
        
        return Response({
            'status': 'cancelled',
            'message': message,
            'cancel_at_period_end': subscription.cancel_at_period_end,
            'current_period_end': subscription.current_period_end,
        })


# ─────────────────────────────────────────────────────────────
#  ISP PAYOUT CONFIGURATION
# ─────────────────────────────────────────────────────────────

class ISPPayoutConfigView(APIView):
    """
    Get and update ISP payout configuration.
    
    GET /api/v1/core/payout-config/
    PATCH /api/v1/core/payout-config/
    """
    
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        user = request.user
        
        if not user.company:
            return Response(
                {'error': 'User is not associated with a company'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        config, created = ISPPayoutConfig.objects.get_or_create(
            company=user.company
        )
        
        serializer = ISPPayoutConfigSerializer(config)
        return Response(serializer.data)
    
    def patch(self, request):
        user = request.user
        
        if not user.company:
            return Response(
                {'error': 'User is not associated with a company'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Only admins can update payout config
        if not user.is_admin:
            return Response(
                {'error': 'Only administrators can update payout settings'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        config, created = ISPPayoutConfig.objects.get_or_create(
            company=user.company
        )
        
        serializer = ISPPayoutConfigUpdateSerializer(
            config,
            data=request.data,
            partial=True
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        
        # Return full config
        return Response(ISPPayoutConfigSerializer(config).data)


class VerifyPayoutView(APIView):
    """
    Verify payout destination by sending a test payment.
    
    POST /api/v1/core/payout-config/verify/
    """
    
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        user = request.user
        
        if not user.company:
            return Response(
                {'error': 'User is not associated with a company'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            config = ISPPayoutConfig.objects.get(company=user.company)
        except ISPPayoutConfig.DoesNotExist:
            return Response(
                {'error': 'Please configure payout settings first'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Check if already verified
        if config.is_verified:
            return Response({
                'status': 'already_verified',
                'message': 'Payout destination is already verified',
                'verified_at': config.verified_at,
            })
        
        # In production, send a small test payment (KES 1-10)
        # For now, we'll just mark as verified
        # TODO: Implement actual verification via PayHero B2C
        
        config.is_verified = True
        config.verified_at = timezone.now()
        config.verification_amount = Decimal('1.00')
        config.save()
        
        return Response({
            'status': 'verified',
            'message': 'Payout destination verified successfully',
            'verified_at': config.verified_at,
        })


# ─────────────────────────────────────────────────────────────
#  SETTLEMENTS
# ─────────────────────────────────────────────────────────────

class SettlementSummaryView(APIView):
    """
    Get settlement summary for dashboard.
    
    GET /api/v1/core/settlements/summary/
    """
    
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        user = request.user
        
        if not user.company:
            return Response(
                {'error': 'User is not associated with a company'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        company = user.company
        
        # Get payout config
        try:
            config = ISPPayoutConfig.objects.get(company=company)
            is_payout_configured = config.is_verified
            pending_balance = config.pending_balance
            settlement_frequency = config.get_settlement_frequency_display()
            payout_method = config.get_payout_method_display()
        except ISPPayoutConfig.DoesNotExist:
            is_payout_configured = False
            pending_balance = Decimal('0.00')
            settlement_frequency = 'Not configured'
            payout_method = 'Not configured'
        
        # Get this month's commission ledger
        from datetime import date
        first_of_month = date.today().replace(day=1)
        
        month_totals = CommissionLedger.objects.filter(
            company=company,
            created_at__date__gte=first_of_month
        ).aggregate(
            total_gross=Sum('gross_amount'),
            total_commission=Sum('commission_amount'),
            total_isp=Sum('isp_amount'),
        )
        
        # Calculate next settlement date
        next_settlement_date = None
        if is_payout_configured:
            # Simple calculation - in production this would be more sophisticated
            from datetime import date, timedelta
            today = date.today()
            if settlement_frequency == 'Daily':
                next_settlement_date = today + timedelta(days=1)
            elif settlement_frequency == 'Weekly':
                days_until_monday = (7 - today.weekday()) % 7 or 7
                next_settlement_date = today + timedelta(days=days_until_monday)
            elif settlement_frequency == 'Bi-Weekly':
                next_settlement_date = today + timedelta(days=14)
            else:  # Monthly
                next_month = today.replace(day=1) + timedelta(days=32)
                next_settlement_date = next_month.replace(day=1)
        
        data = {
            'pending_balance': pending_balance,
            'total_collected_this_month': month_totals['total_gross'] or Decimal('0.00'),
            'total_commission_this_month': month_totals['total_commission'] or Decimal('0.00'),
            'total_earnings_this_month': month_totals['total_isp'] or Decimal('0.00'),
            'next_settlement_date': next_settlement_date,
            'settlement_frequency': settlement_frequency,
            'payout_method': payout_method,
            'is_payout_configured': is_payout_configured,
        }
        
        serializer = SettlementSummarySerializer(data)
        return Response(serializer.data)


class SettlementHistoryViewSet(viewsets.ReadOnlyModelViewSet):
    """
    ViewSet for settlement history.
    
    GET /api/v1/core/settlements/
    GET /api/v1/core/settlements/{id}/
    """
    
    serializer_class = ISPSettlementSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        user = self.request.user
        if not user.company:
            return ISPSettlement.objects.none()
        
        return ISPSettlement.objects.filter(
            company=user.company
        ).order_by('-created_at')


# ─────────────────────────────────────────────────────────────
#  PUBLIC BILLING CALCULATOR — Landing page widget
# ─────────────────────────────────────────────────────────────

class BillingCalculatorView(APIView):
    """
    Public endpoint for the landing-page billing calculator.
    Lets ISPs estimate their monthly cost before signing up.

    GET  /api/v1/subscriptions/calculator/
         Returns all active plans with default estimate.

    POST /api/v1/subscriptions/calculator/
         Body: { "pppoe_clients": 50, "monthly_hotspot_revenue": 10000 }
         Returns per-plan estimated monthly cost.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def _build_estimates(self, pppoe_clients, hotspot_revenue):
        results = []
        with schema_context('public'):
            plans = NetilyPlan.objects.filter(is_active=True).order_by('sort_order', 'price_monthly')
            for plan in plans:
                if plan.is_metered:
                    billable = max(pppoe_clients, plan.pppoe_min_clients)
                    pppoe_charge = billable * plan.pppoe_unit_price
                    hotspot_share = (hotspot_revenue * plan.hotspot_revenue_share_pct / Decimal('100')).quantize(Decimal('0.01'))
                    estimated_monthly = plan.base_license_fee + pppoe_charge + hotspot_share
                else:
                    billable = pppoe_clients
                    pppoe_charge = Decimal('0')
                    hotspot_share = Decimal('0')
                    estimated_monthly = plan.price_monthly

                results.append({
                    'plan_id': plan.id,
                    'plan_name': plan.name,
                    'plan_code': plan.code,
                    'tagline': plan.tagline,
                    'is_metered': plan.is_metered,
                    'base_fee': plan.base_license_fee if plan.is_metered else plan.price_monthly,
                    'pppoe_unit_price': plan.pppoe_unit_price,
                    'pppoe_min_clients': plan.pppoe_min_clients,
                    'hotspot_share_pct': plan.hotspot_revenue_share_pct,
                    'max_subscribers': plan.max_subscribers,
                    'max_routers': plan.max_routers,
                    'max_staff': plan.max_staff,
                    'features': plan.features,
                    'is_popular': plan.is_popular,
                    'input_pppoe_clients': pppoe_clients,
                    'billable_pppoe_clients': billable,
                    'pppoe_charge': pppoe_charge,
                    'input_hotspot_revenue': hotspot_revenue,
                    'hotspot_share': hotspot_share,
                    'estimated_monthly': estimated_monthly,
                    'price_yearly': plan.price_yearly,
                })
        return results

    def _parse_inputs(self, request):
        raw_pppoe_clients = request.query_params.get('pppoe_clients', request.data.get('pppoe_clients', 30))
        raw_hotspot_revenue = request.query_params.get(
            'monthly_hotspot_revenue',
            request.data.get('monthly_hotspot_revenue', '5000'),
        )

        try:
            pppoe_clients = int(raw_pppoe_clients)
        except (TypeError, ValueError):
            pppoe_clients = 30
        pppoe_clients = max(0, min(pppoe_clients, 100000))

        try:
            hotspot_revenue = Decimal(str(raw_hotspot_revenue))
        except Exception:
            hotspot_revenue = Decimal('5000')
        hotspot_revenue = max(Decimal('0'), hotspot_revenue)
        return pppoe_clients, hotspot_revenue

    def get(self, request):
        pppoe_clients, hotspot_revenue = self._parse_inputs(request)
        results = self._build_estimates(pppoe_clients, hotspot_revenue)
        return Response(results)

    def post(self, request):
        pppoe_clients, hotspot_revenue = self._parse_inputs(request)
        results = self._build_estimates(pppoe_clients, hotspot_revenue)
        return Response(results)
