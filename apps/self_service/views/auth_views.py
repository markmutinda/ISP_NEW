"""
Customer Authentication Views

Public endpoints for customer self-registration and login.
"""

import logging
import random
import string

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from rest_framework import status, generics
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from apps.customers.models import Customer
from apps.core.models import AuditLog

from ..serializers import (
    CustomerSelfRegisterSerializer,
    PhoneVerificationSerializer,
    ResendOTPSerializer,
)

logger = logging.getLogger(__name__)
User = get_user_model()


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
        logger.info(f"OTP for {phone_number}: {otp}")
        
        # TODO: Send via SMS gateway (Africa's Talking, etc.)
        
        return otp


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
            # Normalize to +254XXXXXXXXX format (how phones are stored in DB)
            phone = phone_number.replace(' ', '').replace('-', '')
            digits = ''.join(c for c in phone if c.isdigit())
            if digits.startswith('0') and len(digits) >= 9:
                phone = '+254' + digits[1:]
            elif digits.startswith('254') and len(digits) >= 12:
                phone = '+' + digits
            elif len(digits) == 9:
                phone = '+254' + digits
            elif phone.startswith('+'):
                phone = phone  # already has +, keep as-is
            else:
                phone = '+' + digits

            try:
                user = User.objects.get(phone_number=phone)
            except User.DoesNotExist:
                # Fallback: try without the + (in case some records stored without it)
                try:
                    user = User.objects.get(phone_number=phone.lstrip('+'))
                except User.DoesNotExist:
                    pass

        # NEW: Try portal username (stored as "username@portal.local")
        if not user and phone_number and '@' not in phone_number:
            raw = phone_number.strip()
            try:
                user = User.objects.get(email=f"{raw}@portal.local")
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
        
        # Check password — accept hashed password OR the user's own phone number as password
        # This covers all existing customers whose password was set to their phone number
        password_valid = user.check_password(password) or (password == user.phone_number)
        if not password_valid:
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
        from apps.billing.models import Plan
        
        # Verify we're on a tenant subdomain
        if not hasattr(request, 'tenant') or request.tenant.schema_name == 'public':
            return Response({
                'error': 'Must access from ISP subdomain'
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Get active, public plans
        plans = Plan.objects.filter(is_active=True, is_public=True).order_by('base_price')
        
        plans_data = [
            {
                'id': plan.id,
                'name': plan.name,
                'description': plan.description or '',
                'price': str(plan.base_price),
                'speed_down': plan.download_speed,
                'speed_up': plan.upload_speed,
                'speed_unit': plan.speed_unit or 'MBPS',
                'data_limit': plan.data_limit,
                'validity_days': plan.duration_days,
                'validity_display': plan.validity_display,
                'speed_display': plan.speed_display,
                'plan_type': plan.plan_type,
                'is_popular': getattr(plan, 'is_popular', False),
                'features': plan.features or [],
                'is_active': plan.is_active,
            }
            for plan in plans
        ]
        
        # Get ISP branding info
        branding = None
        try:
            if hasattr(request, 'tenant') and hasattr(request.tenant, 'company') and request.tenant.company:
                company = request.tenant.company
                logo_url = None
                if company.logo:
                    logo_url = request.build_absolute_uri(company.logo.url)
                branding = {
                    'company_name': company.name,
                    'logo_url': logo_url,
                    'phone': company.phone_number,
                    'email': company.email,
                }
        except Exception:
            pass
        
        return Response({
            'plans': plans_data,
            'branding': branding,
        })