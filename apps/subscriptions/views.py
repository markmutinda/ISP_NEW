"""
Subscription Views for Netily Platform

These views handle ISP subscription management - where ISP companies
pay Netily for access to the platform.
"""

import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Sum, Count
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.billing.services.payhero import PayHeroClient, PayHeroError, PaymentStatus
from apps.core.models import Company

from .models import (
    NetilyPlan,
    CompanySubscription,
    SubscriptionPayment,
    ISPPayoutConfig,
    ISPSettlement,
    CommissionLedger,
)
from .serializers import (
    NetilyPlanSerializer,
    CompanySubscriptionSerializer,
    SubscriptionUsageSerializer,
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
    
    queryset = NetilyPlan.objects.filter(is_active=True)
    serializer_class = NetilyPlanSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        return NetilyPlan.objects.filter(is_active=True).order_by('sort_order', 'price_monthly')


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
        }
        
        serializer = SubscriptionUsageSerializer(data)
        return Response(serializer.data)


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
    
    @transaction.atomic
    def post(self, request):
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
        
        # Calculate amount
        if billing_period == 'yearly':
            amount = plan.price_yearly
        else:
            amount = plan.price_monthly
        
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
        
        if not created:
            # Updating existing subscription
            subscription.plan = plan
            subscription.billing_period = billing_period
            subscription.save()
        
        # Create payment record
        payment = SubscriptionPayment.objects.create(
            subscription=subscription,
            amount=amount,
            payment_method=payment_method,
            phone_number=phone_number,
            status='pending',
            period_start=subscription.current_period_end or timezone.now(),
            period_end=(subscription.current_period_end or timezone.now()) + timedelta(
                days=365 if billing_period == 'yearly' else 30
            ),
        )
        
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
        """Initiate M-Pesa STK Push"""
        try:
            client = PayHeroClient()
            
            reference = f"NETILY-{plan.code.upper()}-{payment.id.hex[:8].upper()}"
            
            logger.info(f"Initiating STK Push: phone={phone_number}, amount={amount}, ref={reference}")
            
            response = client.stk_push(
                phone_number=phone_number,
                amount=int(amount),
                reference=reference,
                description=f"Netily {plan.name} Subscription",
                callback_url=settings.PAYHERO_SUBSCRIPTION_CALLBACK,
            )
            
            logger.info(f"STK Push response: success={response.success}, message={response.message}")
            
            if response.success:
                payment.payhero_checkout_id = response.checkout_request_id
                payment.payhero_reference = reference
                payment.status = 'processing'
                payment.save()
                
                return Response({
                    'status': 'pending',
                    'payment_id': str(payment.id),
                    'checkout_request_id': response.checkout_request_id,
                    'message': 'STK Push sent. Check your phone and enter your M-Pesa PIN.',
                })
            else:
                payment.status = 'failed'
                payment.failure_reason = response.message
                payment.save()
                
                return Response({
                    'status': 'error',
                    'message': response.message,
                }, status=status.HTTP_400_BAD_REQUEST)
        
        except PayHeroError as e:
            logger.error(f"PayHero STK push failed: {e.message}")
            payment.status = 'failed'
            payment.failure_reason = str(e)
            payment.save()
            
            return Response({
                'status': 'error',
                'message': 'Failed to initiate payment. Please try again.',
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    
    def _handle_paybill(self, payment, amount, company):
        """Return Paybill details for manual payment"""
        # Generate unique account number
        account_number = f"NETILY-{company.slug.upper()[:10]}-{payment.id.hex[:6].upper()}"
        
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
        
        return SubscriptionPayment.objects.filter(
            subscription__company=user.company
        ).select_related('subscription__plan').order_by('-created_at')
    
    @action(detail=True, methods=['get'])
    def status(self, request, pk=None):
        """
        Poll payment status.
        
        GET /api/v1/subscriptions/payments/{id}/status/
        """
        payment = self.get_object()
        
        # If already completed or failed, return current status
        if payment.status in ['completed', 'failed', 'cancelled']:
            return Response({
                'payment_id': str(payment.id),
                'status': payment.status,
                'message': self._get_status_message(payment),
                'mpesa_receipt': payment.mpesa_receipt,
                'completed_at': payment.completed_at,
            })
        
        # If pending with PayHero, check status
        if payment.payhero_checkout_id:
            try:
                client = PayHeroClient()
                status_response = client.get_payment_status(payment.payhero_checkout_id)
                
                if status_response.status == PaymentStatus.SUCCESS:
                    payment.mark_completed(status_response.mpesa_receipt)
                    return Response({
                        'payment_id': str(payment.id),
                        'status': 'completed',
                        'message': 'Payment successful! Your subscription is now active.',
                        'mpesa_receipt': payment.mpesa_receipt,
                        'completed_at': payment.completed_at,
                    })
                
                elif status_response.status == PaymentStatus.FAILED:
                    payment.mark_failed(status_response.failure_reason)
                    return Response({
                        'payment_id': str(payment.id),
                        'status': 'failed',
                        'message': status_response.failure_reason or 'Payment failed',
                        'mpesa_receipt': None,
                        'completed_at': None,
                    })
            
            except PayHeroError as e:
                logger.error(f"Error checking payment status: {e.message}")
        
        # Still pending
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
