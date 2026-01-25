"""
Self-Service Serializers

Serializers for customer self-registration and self-service functionality.
"""

from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.validators import RegexValidator
from django.utils import timezone

from .models import CustomerSession, ServiceRequest, UsageAlert
from apps.customers.models import Customer

User = get_user_model()


# =============================================================================
# CUSTOMER SELF-REGISTRATION SERIALIZERS
# =============================================================================

class CustomerSelfRegisterSerializer(serializers.Serializer):
    """
    Serializer for customer self-registration on ISP subdomain.
    
    This creates both a User and a Customer profile in the tenant schema.
    """
    
    # Contact Information
    email = serializers.EmailField(required=True)
    phone_number = serializers.CharField(
        required=True,
        max_length=15,
        help_text="Phone number in format 254XXXXXXXXX"
    )
    
    # Personal Information
    first_name = serializers.CharField(max_length=100, required=True)
    last_name = serializers.CharField(max_length=100, required=True)
    
    # Authentication
    password = serializers.CharField(
        write_only=True,
        required=True,
        style={'input_type': 'password'},
        validators=[validate_password]
    )
    password_confirm = serializers.CharField(
        write_only=True,
        required=True,
        style={'input_type': 'password'}
    )
    
    # Optional fields
    id_number = serializers.CharField(max_length=50, required=False, allow_blank=True)
    alternative_phone = serializers.CharField(max_length=20, required=False, allow_blank=True)
    address = serializers.CharField(required=False, allow_blank=True)
    city = serializers.CharField(max_length=100, required=False, allow_blank=True)
    
    def validate_email(self, value):
        """Check email uniqueness within tenant"""
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value.lower()
    
    def validate_phone_number(self, value):
        """Normalize and validate phone number"""
        # Remove any spaces or dashes
        phone = value.replace(' ', '').replace('-', '')
        
        # Convert 07XX to 254XX format
        if phone.startswith('0'):
            phone = '254' + phone[1:]
        
        # Convert +254 to 254
        if phone.startswith('+'):
            phone = phone[1:]
        
        # Validate format
        if not phone.startswith('254') or len(phone) != 12:
            raise serializers.ValidationError(
                "Phone number must be in format 254XXXXXXXXX (e.g., 254712345678)"
            )
        
        # Check uniqueness
        if User.objects.filter(phone_number=phone).exists():
            raise serializers.ValidationError("A user with this phone number already exists.")
        
        return phone
    
    def validate_id_number(self, value):
        """Check ID number uniqueness if provided"""
        if value:
            if Customer.objects.filter(id_number=value).exists():
                raise serializers.ValidationError("A customer with this ID number already exists.")
        return value
    
    def validate(self, data):
        """Cross-field validation"""
        # Check password match
        if data.get('password') != data.get('password_confirm'):
            raise serializers.ValidationError({
                "password_confirm": "Passwords do not match."
            })
        
        return data
    
    def create(self, validated_data):
        """Create User and Customer profile"""
        # Remove password_confirm from data
        validated_data.pop('password_confirm', None)
        
        # Extract customer-specific fields
        id_number = validated_data.pop('id_number', '')
        alternative_phone = validated_data.pop('alternative_phone', '')
        address = validated_data.pop('address', '')
        city = validated_data.pop('city', '')
        
        # Get tenant info from request context
        request = self.context.get('request')
        tenant_subdomain = getattr(request.tenant, 'subdomain', '') if hasattr(request, 'tenant') else ''
        company_name = ''
        if hasattr(request, 'tenant') and request.tenant.company:
            company_name = request.tenant.company.name
        
        # Create User
        user = User.objects.create_user(
            email=validated_data['email'],
            phone_number=validated_data['phone_number'],
            first_name=validated_data['first_name'],
            last_name=validated_data['last_name'],
            password=validated_data['password'],
            role='customer',
            is_active=True,
            is_verified=False,  # Will be verified via OTP
            company_name=company_name,
            tenant_subdomain=tenant_subdomain,
        )
        
        # Generate customer code
        customer_code = self._generate_customer_code()
        
        # Create Customer profile
        customer = Customer.objects.create(
            user=user,
            customer_code=customer_code,
            id_number=id_number or f"TEMP-{user.id}",  # Temporary if not provided
            alternative_phone=alternative_phone,
            status='PENDING',  # Requires approval or verification
            customer_type='RESIDENTIAL',
            category='PREPAID',
        )
        
        return {
            'user': user,
            'customer': customer,
        }
    
    def _generate_customer_code(self):
        """Generate unique customer code"""
        # Get count for today
        today = timezone.now().strftime('%Y%m%d')
        prefix = f"CUST-{today}"
        
        # Get last customer for today
        last = Customer.objects.filter(
            customer_code__startswith=prefix
        ).order_by('-customer_code').first()
        
        if last:
            try:
                last_num = int(last.customer_code.split('-')[-1])
                new_num = last_num + 1
            except (ValueError, IndexError):
                new_num = 1
        else:
            new_num = 1
        
        return f"{prefix}-{new_num:04d}"


class PhoneVerificationSerializer(serializers.Serializer):
    """Serializer for phone verification via OTP"""
    
    phone_number = serializers.CharField(required=True)
    otp_code = serializers.CharField(required=True, max_length=6, min_length=4)


class ResendOTPSerializer(serializers.Serializer):
    """Serializer for resending OTP"""
    
    phone_number = serializers.CharField(required=True)


class CustomerLoginSerializer(serializers.Serializer):
    """Serializer for customer login"""
    
    phone_number = serializers.CharField(required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    password = serializers.CharField(required=True, write_only=True)
    
    def validate(self, data):
        if not data.get('phone_number') and not data.get('email'):
            raise serializers.ValidationError({
                "phone_number": "Phone number or email is required."
            })
        return data


class CustomerProfileSerializer(serializers.ModelSerializer):
    """Serializer for Customer profile (self-service)"""
    
    email = serializers.EmailField(source='user.email', read_only=True)
    first_name = serializers.CharField(source='user.first_name', read_only=True)
    last_name = serializers.CharField(source='user.last_name', read_only=True)
    phone_number = serializers.CharField(source='user.phone_number', read_only=True)
    
    class Meta:
        model = Customer
        fields = [
            'id',
            'customer_code',
            'email',
            'first_name',
            'last_name',
            'phone_number',
            'alternative_phone',
            'status',
            'customer_type',
            'category',
            'created_at',
        ]
        read_only_fields = ['id', 'customer_code', 'created_at', 'status']


# =============================================================================
# EXISTING SERIALIZERS (CustomerSession, ServiceRequest, UsageAlert)
# =============================================================================

class CustomerSessionSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    duration = serializers.SerializerMethodField()
    
    class Meta:
        model = CustomerSession
        fields = [
            'id', 'customer', 'customer_name', 'session_key',
            'ip_address', 'user_agent', 'login_time',
            'logout_time', 'last_activity', 'is_active', 'duration'
        ]
        read_only_fields = fields
    
    def get_duration(self, obj):
        if obj.logout_time:
            duration = obj.logout_time - obj.login_time
        else:
            duration = timezone.now() - obj.login_time
        return str(duration)


class ServiceRequestSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    current_plan_name = serializers.CharField(source='current_plan.name', read_only=True, allow_null=True)
    requested_plan_name = serializers.CharField(source='requested_plan.name', read_only=True, allow_null=True)
    assigned_to_name = serializers.CharField(source='assigned_to.get_full_name', read_only=True, allow_null=True)
    
    class Meta:
        model = ServiceRequest
        fields = [
            'id', 'customer', 'customer_name', 'request_type', 'subject',
            'description', 'status', 'priority', 'current_plan',
            'current_plan_name', 'requested_plan', 'requested_plan_name',
            'current_location', 'requested_location', 'assigned_to',
            'assigned_to_name', 'estimated_completion', 'actual_completion',
            'customer_notes', 'staff_notes', 'created_at', 'updated_at'
        ]
        read_only_fields = ['customer', 'status', 'assigned_to', 'actual_completion', 'staff_notes']


class UsageAlertSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.name', read_only=True)
    customer_email = serializers.CharField(source='customer.email', read_only=True)
    
    class Meta:
        model = UsageAlert
        fields = [
            'id', 'customer', 'customer_name', 'customer_email',
            'alert_type', 'trigger_type', 'threshold_value',
            'current_value', 'message', 'is_read', 'is_resolved',
            'triggered_at', 'resolved_at'
        ]
        read_only_fields = fields


class CustomerDashboardSerializer(serializers.Serializer):
    # This is a non-model serializer for dashboard data
    customer = serializers.DictField()
    usage = serializers.DictField()
    billing = serializers.DictField()
    recent_activity = serializers.DictField()
    alerts = UsageAlertSerializer(many=True)
    quick_actions = serializers.ListField()
