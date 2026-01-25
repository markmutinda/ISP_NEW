"""
Self-Service Views

Customer-facing views for self-registration, dashboard, and payments.
"""

import logging
import random
import string
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from rest_framework import status, generics
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from apps.customers.models import Customer
from apps.billing.models import Plan, Invoice, Payment
from apps.core.models import AuditLog

from .models import ServiceRequest, UsageAlert
from .serializers import (
    CustomerSelfRegisterSerializer,
    CustomerProfileSerializer,
    PhoneVerificationSerializer,
    ResendOTPSerializer,
    ServiceRequestSerializer,
    UsageAlertSerializer,
)

logger = logging.getLogger(__name__)


# =============================================================================
# CUSTOMER SELF-REGISTRATION (PUBLIC ENDPOINTS)
# =============================================================================

class CustomerSelfRegisterView(generics.CreateAPIView):
    """
    Customer self-registration endpoint.
    
    PUBLIC ENDPOINT - No authentication required.
    Must be accessed from a tenant subdomain.
    
    POST /api/v1/self-service/register/
    """
    
    permission_classes = [AllowAny]
    authentication_classes = []  # No auth required
    serializer_class = CustomerSelfRegisterSerializer
    
    def create(self, request, *args, **kwargs):
        # Verify we're on a tenant subdomain (not public)
        if not hasattr(request, 'tenant') or request.tenant.schema_name == 'public':
            return Response({
                'error': 'Registration must be done on an ISP subdomain',
                'message': 'Please access this page from your ISP\'s website'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        serializer = self.get_serializer(data=request.data)
        
        if serializer.is_valid():
            with transaction.atomic():
                result = serializer.save()
                user = result['user']
                customer = result['customer']
                
                # Generate JWT tokens
                refresh = RefreshToken.for_user(user)
                
                # Log the registration
                AuditLog.log_action(
                    user=user,
                    action='create',
                    model_name='Customer',
                    object_id=str(customer.id),
                    object_repr=f"Customer {customer.customer_code}",
                    ip_address=request.META.get('REMOTE_ADDR'),
                    user_agent=request.META.get('HTTP_USER_AGENT', ''),
                    changes={'action': 'self_registration'}
                )
                
                # TODO: Send verification OTP
                # self._send_verification_otp(user.phone_number)
                
                return Response({
                    'status': 'success',
                    'message': 'Registration successful. Please verify your phone number.',
                    'user': {
                        'id': user.id,
                        'email': user.email,
                        'first_name': user.first_name,
                        'last_name': user.last_name,
                        'phone_number': user.phone_number,
                        'is_verified': user.is_verified,
                    },
                    'customer': {
                        'id': customer.id,
                        'customer_code': customer.customer_code,
                        'status': customer.status,
                    },
                    'access': str(refresh.access_token),
                    'refresh': str(refresh),
                    'requires_verification': True,
                }, status=status.HTTP_201_CREATED)
        
        return Response({
            'status': 'error',
            'errors': serializer.errors,
        }, status=status.HTTP_400_BAD_REQUEST)
    
    def _send_verification_otp(self, phone_number: str) -> str:
        """Generate and send OTP via SMS"""
        # Generate 6-digit OTP
        otp = ''.join(random.choices(string.digits, k=6))
        
        # Store OTP (in cache or database)
        # For now, just log it (implement proper storage later)
        logger.info(f"OTP for {phone_number}: {otp}")
        
        # TODO: Send via SMS gateway (Africa's Talking, etc.)
        
        return otp


class VerifyPhoneView(APIView):
    """
    Verify phone number via OTP.
    
    PUBLIC ENDPOINT - No authentication required.
    
    POST /api/v1/self-service/verify-phone/
    """
    
    permission_classes = [AllowAny]
    authentication_classes = []
    
    def post(self, request):
        serializer = PhoneVerificationSerializer(data=request.data)
        
        if not serializer.is_valid():
            return Response({
                'status': 'error',
                'errors': serializer.errors,
            }, status=status.HTTP_400_BAD_REQUEST)
        
        phone_number = serializer.validated_data['phone_number']
        otp_code = serializer.validated_data['otp_code']
        
        # TODO: Implement proper OTP verification
        # For now, accept any 6-digit code in development
        if settings.DEBUG and len(otp_code) == 6:
            # Find user by phone number
            from django.contrib.auth import get_user_model
            User = get_user_model()
            
            try:
                user = User.objects.get(phone_number=phone_number)
                user.is_verified = True
                user.save(update_fields=['is_verified'])
                
                # Also update customer status
                try:
                    customer = user.customer_profile
                    customer.status = 'ACTIVE'
                    customer.save(update_fields=['status'])
                except Customer.DoesNotExist:
                    pass
                
                return Response({
                    'status': 'success',
                    'message': 'Phone number verified successfully',
                })
            
            except User.DoesNotExist:
                return Response({
                    'status': 'error',
                    'message': 'User not found',
                }, status=status.HTTP_404_NOT_FOUND)
        
        return Response({
            'status': 'error',
            'message': 'Invalid OTP code',
        }, status=status.HTTP_400_BAD_REQUEST)


class ResendOTPView(APIView):
    """
    Resend verification OTP.
    
    PUBLIC ENDPOINT - No authentication required.
    
    POST /api/v1/self-service/resend-otp/
    """
    
    permission_classes = [AllowAny]
    authentication_classes = []
    
    def post(self, request):
        serializer = ResendOTPSerializer(data=request.data)
        
        if not serializer.is_valid():
            return Response({
                'status': 'error',
                'errors': serializer.errors,
            }, status=status.HTTP_400_BAD_REQUEST)
        
        phone_number = serializer.validated_data['phone_number']
        
        # TODO: Implement OTP resend with rate limiting
        # Check if phone exists, rate limit, generate new OTP, send
        
        return Response({
            'status': 'success',
            'message': 'OTP sent to your phone number',
        })


class AvailablePlansView(APIView):
    """
    Get available ISP plans for customers to view.
    
    PUBLIC ENDPOINT - No authentication required.
    
    GET /api/v1/self-service/plans/
    """
    
    permission_classes = [AllowAny]
    authentication_classes = []
    
    def get(self, request):
        # Verify we're on a tenant subdomain
        if not hasattr(request, 'tenant') or request.tenant.schema_name == 'public':
            return Response({
                'error': 'Must access from ISP subdomain'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Get active plans
        plans = Plan.objects.filter(is_active=True).order_by('price')
        
        plans_data = [
            {
                'id': plan.id,
                'name': plan.name,
                'description': plan.description,
                'price': float(plan.price),
                'billing_cycle': plan.billing_cycle,
                'speed_mbps': getattr(plan, 'speed_mbps', None),
                'data_limit_gb': getattr(plan, 'data_limit_gb', None),
                'is_popular': getattr(plan, 'is_popular', False),
            }
            for plan in plans
        ]
        
        # Get ISP branding info
        branding = None
        if hasattr(request, 'tenant') and request.tenant.company:
            company = request.tenant.company
            branding = {
                'company_name': company.name,
                'logo_url': getattr(company, 'logo_url', None),
                'phone': company.phone_number,
                'email': company.email,
            }
        
        return Response({
            'plans': plans_data,
            'branding': branding,
        })


# =============================================================================
# CUSTOMER DASHBOARD (AUTHENTICATED ENDPOINTS)
# =============================================================================

class CustomerDashboardView(APIView):
    """
    Customer dashboard data.
    
    GET /api/v1/self-service/dashboard/
    """
    
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        user = request.user
        
        # Get customer profile
        try:
            customer = Customer.objects.get(user=user)
        except Customer.DoesNotExist:
            return Response({
                'error': 'Customer profile not found'
            }, status=status.HTTP_404_NOT_FOUND)
        
        # Get account balance
        account_balance = getattr(customer, 'account_balance', Decimal('0.00'))
        
        # Get current service/plan
        current_plan = None
        plan_expires_at = None
        try:
            service = customer.services.filter(status='ACTIVE').first()
            if service and service.plan:
                current_plan = {
                    'id': service.plan.id,
                    'name': service.plan.name,
                    'price': float(service.plan.price),
                }
                plan_expires_at = service.expires_at
        except Exception:
            pass
        
        # Get pending invoices
        pending_invoices = Invoice.objects.filter(
            customer=customer,
            status__in=['PENDING', 'OVERDUE']
        )
        pending_count = pending_invoices.count()
        pending_amount = sum(inv.amount_due for inv in pending_invoices)
        
        # Get recent payments
        recent_payments = Payment.objects.filter(
            customer=customer
        ).order_by('-created_at')[:5]
        
        # Get unread alerts
        unread_alerts = UsageAlert.objects.filter(
            customer=customer,
            is_read=False
        ).count()
        
        return Response({
            'customer': CustomerProfileSerializer(customer).data,
            'account_balance': float(account_balance),
            'current_plan': current_plan,
            'plan_expires_at': plan_expires_at,
            'usage': {
                'data_used_mb': 0,  # TODO: Get from usage tracking
                'data_limit_mb': None,
            },
            'billing': {
                'pending_invoices': pending_count,
                'pending_amount': float(pending_amount),
            },
            'recent_payments': [
                {
                    'id': p.id,
                    'amount': float(p.amount),
                    'status': p.status,
                    'date': p.created_at,
                    'mpesa_receipt': p.mpesa_receipt,
                }
                for p in recent_payments
            ],
            'unread_alerts': unread_alerts,
        })


class PaymentView(APIView):
    """
    Customer payment endpoint for self-service.
    
    POST /api/v1/self-service/payments/
    """
    
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        """Get customer's payment history"""
        user = request.user
        
        try:
            customer = Customer.objects.get(user=user)
        except Customer.DoesNotExist:
            return Response({
                'error': 'Customer profile not found'
            }, status=status.HTTP_404_NOT_FOUND)
        
        payments = Payment.objects.filter(
            customer=customer
        ).order_by('-created_at')[:20]
        
        return Response({
            'payments': [
                {
                    'id': p.id,
                    'payment_number': p.payment_number,
                    'amount': float(p.amount),
                    'status': p.status,
                    'payment_method': str(p.payment_method),
                    'mpesa_receipt': p.mpesa_receipt,
                    'created_at': p.created_at,
                }
                for p in payments
            ]
        })
    
    def post(self, request):
        """Initiate a new payment"""
        # This redirects to the billing payment endpoint
        # Import here to avoid circular imports
        from apps.billing.views.customer_payment_views import InitiateCustomerPaymentView
        
        view = InitiateCustomerPaymentView()
        view.request = request
        return view.post(request)


# =============================================================================
# SERVICE REQUESTS
# =============================================================================

class ServiceRequestListCreateView(generics.ListCreateAPIView):
    """
    List and create service requests.
    
    GET /api/v1/self-service/service-requests/
    POST /api/v1/self-service/service-requests/
    """
    
    permission_classes = [IsAuthenticated]
    serializer_class = ServiceRequestSerializer
    
    def get_queryset(self):
        user = self.request.user
        
        try:
            customer = Customer.objects.get(user=user)
            return ServiceRequest.objects.filter(customer=customer)
        except Customer.DoesNotExist:
            return ServiceRequest.objects.none()
    
    def perform_create(self, serializer):
        user = self.request.user
        customer = Customer.objects.get(user=user)
        serializer.save(customer=customer)


class ServiceRequestDetailView(generics.RetrieveAPIView):
    """
    Get service request details.
    
    GET /api/v1/self-service/service-requests/{id}/
    """
    
    permission_classes = [IsAuthenticated]
    serializer_class = ServiceRequestSerializer
    
    def get_queryset(self):
        user = self.request.user
        
        try:
            customer = Customer.objects.get(user=user)
            return ServiceRequest.objects.filter(customer=customer)
        except Customer.DoesNotExist:
            return ServiceRequest.objects.none()


class ServiceRequestTypesView(APIView):
    """
    Get available service request types.
    
    GET /api/v1/self-service/service-request-types/
    """
    
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        return Response({
            'types': [
                {'value': choice[0], 'label': choice[1]}
                for choice in ServiceRequest.REQUEST_TYPES
            ]
        })


# =============================================================================
# CUSTOMER LOGIN (PUBLIC ENDPOINT)
# =============================================================================

class CustomerLoginView(APIView):
    """
    Customer login endpoint - supports phone number or email login.
    
    PUBLIC ENDPOINT - No authentication required.
    Must be accessed from a tenant subdomain.
    
    POST /api/v1/self-service/login/
    {
        "phone_number": "254712345678",  // OR "email": "customer@example.com"
        "password": "password123"
    }
    """
    
    permission_classes = [AllowAny]
    authentication_classes = []  # No auth required
    
    def post(self, request):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        
        # Verify we're on a tenant subdomain (not public)
        if not hasattr(request, 'tenant') or request.tenant.schema_name == 'public':
            return Response({
                'error': 'Login must be done on an ISP subdomain',
                'message': 'Please access this page from your ISP\'s website'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        phone_number = request.data.get('phone_number')
        email = request.data.get('email')
        password = request.data.get('password')
        
        if not password:
            return Response({
                'error': 'Password is required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        if not phone_number and not email:
            return Response({
                'error': 'Phone number or email is required'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        user = None
        
        # Try to find user by phone number
        if phone_number:
            # Normalize phone number
            phone = phone_number.replace(' ', '').replace('-', '')
            if phone.startswith('0'):
                phone = '254' + phone[1:]
            if phone.startswith('+'):
                phone = phone[1:]
            
            try:
                user = User.objects.get(phone_number=phone)
            except User.DoesNotExist:
                pass
        
        # Try to find user by email if not found by phone
        if not user and email:
            try:
                user = User.objects.get(email=email.lower())
            except User.DoesNotExist:
                pass
        
        if not user:
            return Response({
                'error': 'Invalid credentials',
                'message': 'No account found with this phone number or email'
            }, status=status.HTTP_401_UNAUTHORIZED)
        
        # Check password
        if not user.check_password(password):
            return Response({
                'error': 'Invalid credentials',
                'message': 'Incorrect password'
            }, status=status.HTTP_401_UNAUTHORIZED)
        
        # Check if user is active
        if not user.is_active:
            return Response({
                'error': 'Account disabled',
                'message': 'Your account has been disabled. Please contact support.'
            }, status=status.HTTP_403_FORBIDDEN)
        
        # Check if user has customer role
        if user.role != 'customer':
            return Response({
                'error': 'Invalid account type',
                'message': 'This login is for customers only. Staff should use the admin portal.'
            }, status=status.HTTP_403_FORBIDDEN)
        
        # Get customer profile
        try:
            customer = Customer.objects.get(user=user)
        except Customer.DoesNotExist:
            return Response({
                'error': 'Customer profile not found',
                'message': 'Your customer profile could not be found.'
            }, status=status.HTTP_404_NOT_FOUND)
        
        # Generate JWT tokens
        refresh = RefreshToken.for_user(user)
        
        # Log the login
        AuditLog.log_action(
            user=user,
            action='login',
            model_name='User',
            object_id=str(user.id),
            object_repr=f"Customer login: {user.email}",
            ip_address=request.META.get('REMOTE_ADDR'),
            user_agent=request.META.get('HTTP_USER_AGENT', ''),
        )
        
        return Response({
            'status': 'success',
            'message': 'Login successful',
            'user': {
                'id': user.id,
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'phone_number': user.phone_number,
                'is_verified': user.is_verified,
            },
            'customer': {
                'id': customer.id,
                'customer_code': customer.customer_code,
                'status': customer.status,
            },
            'access': str(refresh.access_token),
            'refresh': str(refresh),
        })


# =============================================================================
# PAYMENT STATUS (AUTHENTICATED ENDPOINT)
# =============================================================================

class PaymentStatusView(APIView):
    """
    Check payment status.
    
    GET /api/v1/self-service/payments/{id}/status/
    """
    
    permission_classes = [IsAuthenticated]
    
    def get(self, request, payment_id):
        user = request.user
        
        # Get customer profile
        try:
            customer = Customer.objects.get(user=user)
        except Customer.DoesNotExist:
            return Response({
                'error': 'Customer profile not found'
            }, status=status.HTTP_404_NOT_FOUND)
        
        # Get payment
        try:
            payment = Payment.objects.get(id=payment_id, customer=customer)
        except Payment.DoesNotExist:
            return Response({
                'error': 'Payment not found'
            }, status=status.HTTP_404_NOT_FOUND)
        
        # If already finalized, return status
        if payment.status in ['COMPLETED', 'FAILED', 'CANCELLED', 'REFUNDED']:
            return Response({
                'payment_id': payment.id,
                'status': payment.status.lower(),
                'message': self._get_status_message(payment),
                'amount': float(payment.amount),
                'mpesa_receipt': payment.mpesa_receipt,
                'completed_at': payment.processed_at,
            })
        
        # If pending, try to check with PayHero
        if payment.status in ['PENDING', 'PROCESSING'] and hasattr(payment, 'payhero_external_reference') and payment.payhero_external_reference:
            try:
                from apps.billing.services.payhero import PayHeroClient, PaymentStatus as PHStatus
                
                client = PayHeroClient()
                status_response = client.get_payment_status(payment.payhero_external_reference)
                
                if status_response.status == PHStatus.SUCCESS:
                    payment.status = 'COMPLETED'
                    payment.mpesa_receipt = status_response.mpesa_receipt
                    payment.processed_at = timezone.now()
                    payment.save()
                    
                    return Response({
                        'payment_id': payment.id,
                        'status': 'completed',
                        'message': 'Payment successful!',
                        'amount': float(payment.amount),
                        'mpesa_receipt': payment.mpesa_receipt,
                        'completed_at': payment.processed_at,
                    })
                
                elif status_response.status == PHStatus.FAILED:
                    payment.status = 'FAILED'
                    payment.failure_reason = status_response.failure_reason
                    payment.save()
                    
                    return Response({
                        'payment_id': payment.id,
                        'status': 'failed',
                        'message': status_response.failure_reason or 'Payment failed',
                        'amount': float(payment.amount),
                    })
                
            except Exception as e:
                logger.error(f"Error checking payment status: {e}")
        
        # Still pending
        return Response({
            'payment_id': payment.id,
            'status': payment.status.lower(),
            'message': 'Waiting for payment confirmation...',
            'amount': float(payment.amount),
        })
    
    def _get_status_message(self, payment):
        """Get human-readable status message"""
        messages = {
            'COMPLETED': 'Payment successful!',
            'FAILED': payment.failure_reason or 'Payment failed. Please try again.',
            'CANCELLED': 'Payment was cancelled.',
            'REFUNDED': 'Payment has been refunded.',
            'PENDING': 'Waiting for payment...',
            'PROCESSING': 'Processing payment...',
        }
        return messages.get(payment.status, 'Unknown status')


# =============================================================================
# ALERTS
# =============================================================================

class CustomerAlertsView(generics.ListAPIView):
    """
    List customer alerts.
    
    GET /api/v1/self-service/alerts/
    """
    
    permission_classes = [IsAuthenticated]
    serializer_class = UsageAlertSerializer
    
    def get_queryset(self):
        user = self.request.user
        
        try:
            customer = Customer.objects.get(user=user)
            return UsageAlert.objects.filter(customer=customer).order_by('-triggered_at')[:50]
        except Customer.DoesNotExist:
            return UsageAlert.objects.none()


class MarkAlertReadView(APIView):
    """
    Mark an alert as read.
    
    POST /api/v1/self-service/alerts/{id}/read/
    """
    
    permission_classes = [IsAuthenticated]
    
    def post(self, request, pk):
        user = request.user
        
        try:
            customer = Customer.objects.get(user=user)
            alert = UsageAlert.objects.get(id=pk, customer=customer)
            alert.is_read = True
            alert.save(update_fields=['is_read'])
            
            return Response({'status': 'success'})
        
        except (Customer.DoesNotExist, UsageAlert.DoesNotExist):
            return Response({
                'error': 'Alert not found'
            }, status=status.HTTP_404_NOT_FOUND)
