"""
Serializers for core app
"""
import logging
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import authenticate
from .models import GlobalSystemSettings  # Add this
from rest_framework_simplejwt.exceptions import InvalidToken  # Add this
from rest_framework_simplejwt.serializers import TokenRefreshSerializer  # Already there or add
from django.utils import timezone
from django.contrib.auth.password_validation import validate_password
from .models import User, Company, Tenant, SystemSettings, AuditLog, Changelog, FeatureRequest, FeatureUpvote, RoleAccessPolicy  # Add FeatureRequest and FeatureUpvote here

# Import country/currency constants
from utils.constants import COUNTRY_CHOICES, COUNTRY_CURRENCY_MAP

logger = logging.getLogger(__name__)


NON_DELEGABLE_RBAC_PATHS = {"/admin/staff"}


def validate_dashboard_access_tokens(value):
    if value is None:
        return None
    if not isinstance(value, list):
        raise serializers.ValidationError("Dashboard access must be a list of route tokens.")
    cleaned = []
    for token in value:
        if not isinstance(token, str) or not token.startswith("/admin/"):
            raise serializers.ValidationError("Each dashboard access token must be an admin route path.")
        route = token.split("::", 1)[0]
        if route in NON_DELEGABLE_RBAC_PATHS:
            raise serializers.ValidationError("Staff access management cannot be delegated.")
        if token not in cleaned:
            cleaned.append(token)
    return cleaned


class UserSerializer(serializers.ModelSerializer):
    """Serializer for User model"""
    
    password = serializers.CharField(write_only=True, required=False)
    confirm_password = serializers.CharField(write_only=True, required=False)
    role_display = serializers.CharField(source='get_role_display_name', read_only=True)
    full_name = serializers.SerializerMethodField()
    company_name = serializers.CharField(read_only=True)  # Use denormalized field
    tenant_subdomain = serializers.CharField(read_only=True)  # Use denormalized field
    
    class Meta:
        model = User
        fields = [
            'id', 'email', 'first_name', 'last_name', 'full_name',
            'phone_number', 'id_number', 'gender', 'date_of_birth',
            'profile_picture', 'role', 'role_display', 'is_active',
            'custom_allowed_paths',
            'is_verified', 'is_staff', 'is_superuser',
            'company', 'company_name', 'tenant', 'tenant_subdomain',
            'password', 'confirm_password', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'created_at', 'updated_at', 'is_staff', 'is_superuser',
            'custom_allowed_paths',
        ]
    
    def get_full_name(self, obj):
        return obj.get_full_name()
    
    def validate(self, data):
        """Validate password confirmation - only if confirm_password is provided"""
        password = data.get('password')
        confirm_password = data.get('confirm_password')
        
        # Only check if confirm_password is provided in the request
        if confirm_password is not None and password != confirm_password:
            raise serializers.ValidationError({
                "password": "Passwords do not match."
            })
        
        # Remove confirm_password from validated data if present
        if 'confirm_password' in data:
            del data['confirm_password']
        
        return data
    
    def validate_password(self, value):
        """Validate password strength"""
        if value:  # Only validate if password is provided
            validate_password(value)
        return value
    
    def create(self, validated_data):
        """Create a new user with hashed password"""
        password = validated_data.pop('password', None)
        
        # Set default role if not provided
        if 'role' not in validated_data:
            validated_data['role'] = 'customer'
        
        user = User.objects.create(**validated_data)
        
        if password:
            user.set_password(password)
            user.save()
        
        return user
    
    def update(self, instance, validated_data):
        """Update user instance"""
        password = validated_data.pop('password', None)
        
        # Update other fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        
        # Update password if provided
        if password:
            instance.set_password(password)
        
        instance.save()
        return instance


class UserCreateSerializer(serializers.ModelSerializer):
    """Serializer for user registration/creation"""
    
    password = serializers.CharField(
        write_only=True, 
        required=True, 
        validators=[validate_password],
        style={'input_type': 'password'}
    )
    confirm_password = serializers.CharField(
        write_only=True, 
        required=False,  # Changed from required=True to required=False
        style={'input_type': 'password'}
    )
    
    class Meta:
        model = User
        fields = [
            'id', 'email', 'password', 'confirm_password',
            'first_name', 'last_name', 'phone_number', 'id_number',
            'gender', 'date_of_birth', 'role', 'company', 'tenant',
            'custom_allowed_paths',
        ]
        read_only_fields = ['id']
    
    def validate(self, attrs):
        """Validate that passwords match - only if confirm_password is provided"""
        password = attrs.get('password')
        confirm_password = attrs.get('confirm_password')
        
        # Only check if confirm_password is provided in the request
        if confirm_password is not None and password != confirm_password:
            raise serializers.ValidationError({
                "password": "Password fields didn't match."
            })
        if attrs.get('custom_allowed_paths') is not None:
            role = attrs.get('role', 'customer')
            if role not in {'staff', 'technician', 'accountant', 'support'}:
                raise serializers.ValidationError({
                    'custom_allowed_paths': 'Custom dashboard access is only available for staff roles.'
                })
        return attrs

    def validate_custom_allowed_paths(self, value):
        return validate_dashboard_access_tokens(value)
    
    def create(self, validated_data):
        # Remove confirm_password from validated data if present
        validated_data.pop('confirm_password', None)
        password = validated_data.pop('password')
        
        # Set default role if not provided
        if 'role' not in validated_data:
            validated_data['role'] = 'customer'
        
        user = User.objects.create(**validated_data)
        user.set_password(password)
        user.save()
        
        return user


class UserUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating user profile"""
    
    current_password = serializers.CharField(write_only=True, required=False)
    new_password = serializers.CharField(write_only=True, required=False)
    
    class Meta:
        model = User
        fields = [
            'id', 'email', 'first_name', 'last_name',
            'phone_number', 'profile_picture', 'gender',
            'date_of_birth', 'current_password', 'new_password',
            'is_active'  # 🟢 FIX: Added is_active to allow deactivation/reactivation
        ]
        read_only_fields = ['id']

    def validate_email(self, value):
        if value in (None, ""):
            return value
        normalized = User.objects.normalize_email(value).strip().lower()
        qs = User.objects.filter(email__iexact=normalized)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return normalized
    
    def validate(self, data):
        current_password = data.get('current_password')
        new_password = data.get('new_password')
        
        # If new password provided, current password must also be provided
        if new_password and not current_password:
            raise serializers.ValidationError({
                "current_password": "Current password is required to set a new password."
            })
        
        return data
    
    def validate_new_password(self, value):
        if value:
            validate_password(value)
        return value
    
    def update(self, instance, validated_data):
        current_password = validated_data.pop('current_password', None)
        new_password = validated_data.pop('new_password', None)
        
        # Update password if provided
        if new_password and current_password:
            if not instance.check_password(current_password):
                raise serializers.ValidationError({
                    "current_password": "Current password is incorrect."
                })
            instance.set_password(new_password)
        
        # Update other fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        
        instance.save()
        return instance


# ============================================================
# NEW: AdminUserUpdateSerializer - Admin-only user updates
# ============================================================

class AdminUserUpdateSerializer(serializers.ModelSerializer):
    """Admin-only serializer: can change email, role, and reset password without current_password"""
    
    new_password = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = User
        fields = ['email', 'role', 'new_password', 'is_active', 'custom_allowed_paths']

    def validate_custom_allowed_paths(self, value):
        return validate_dashboard_access_tokens(value)

    def validate(self, attrs):
        if attrs.get('custom_allowed_paths') is not None:
            role = attrs.get('role', getattr(self.instance, 'role', None))
            if role not in {'staff', 'technician', 'accountant', 'support'}:
                raise serializers.ValidationError({
                    'custom_allowed_paths': 'Custom dashboard access is only available for staff roles.'
                })
        return attrs

    def validate_email(self, value):
        if value in (None, ""):
            return value
        normalized = User.objects.normalize_email(value).strip().lower()
        qs = User.objects.filter(email__iexact=normalized)
        if self.instance and self.instance.pk:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return normalized

    def validate_new_password(self, value):
        if value:
            validate_password(value)
        return value

    def update(self, instance, validated_data):
        new_password = validated_data.pop('new_password', None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if new_password:
            instance.set_password(new_password)
        instance.save()
        return instance


class RoleAccessPolicySerializer(serializers.ModelSerializer):
    class Meta:
        model = RoleAccessPolicy
        fields = ["id", "role", "allowed_paths", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_role(self, value):
        editable_roles = {"staff", "technician", "accountant", "support"}
        if value not in editable_roles:
            raise serializers.ValidationError("Only staff, technician, accountant, and support roles can be customized.")
        return value

    def validate_allowed_paths(self, value):
        return validate_dashboard_access_tokens(value)


class LoginSerializer(serializers.Serializer):
    """Serializer for user login"""
    
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)
    token = serializers.CharField(read_only=True)
    refresh_token = serializers.CharField(read_only=True)
    user = UserSerializer(read_only=True)
    
    def validate(self, data):
        email = data.get('email')
        password = data.get('password')
        
        # Authenticate user
        user = authenticate(email=email, password=password)
        
        if not user:
            logger.warning("Login failed for email=%s", email)
            raise serializers.ValidationError({
                "email": "Invalid email or password."
            })
        
        if not user.is_active:
            logger.warning("Login attempted for inactive account: email=%s", email)
            raise serializers.ValidationError({
                "email": "Account is deactivated."
            })
        
        # Generate tokens
        refresh = RefreshToken.for_user(user)
        
        # Update last login
        user.last_login = timezone.now()
        user.save(update_fields=['last_login'])
        
        logger.info("Login successful for user_id=%s, email=%s", user.id, user.email)
        
        data['user'] = user
        data['token'] = str(refresh.access_token)
        data['refresh_token'] = str(refresh)
        
        return data


class TokenRefreshSerializer(serializers.Serializer):
    """Serializer for token refresh"""
    
    refresh = serializers.CharField()
    
    def validate(self, data):
        refresh = data.get('refresh')
        
        try:
            token = RefreshToken(refresh)
        except Exception as e:
            logger.warning("Token refresh failed: invalid token")
            raise serializers.ValidationError({
                "refresh": "Invalid refresh token."
            })
        
        # Get user from token
        user_id = token.payload.get('user_id')
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            logger.warning("Token refresh failed: user not found - user_id=%s", user_id)
            raise serializers.ValidationError({
                "refresh": "User not found."
            })
        
        # Generate new access token
        new_access_token = RefreshToken.for_user(user).access_token
        
        logger.debug("Token refreshed successfully for user_id=%s", user.id)
        
        data['access'] = str(new_access_token)
        return data


class ProfileSerializer(serializers.ModelSerializer):
    """Serializer for user profile"""
    
    full_name = serializers.SerializerMethodField()
    role_display = serializers.CharField(source='get_role_display_name', read_only=True)
    company_name = serializers.CharField(read_only=True)
    tenant_subdomain = serializers.CharField(read_only=True)
    
    class Meta:
        model = User
        fields = [
            'id', 'email', 'first_name', 'last_name', 'full_name',
            'phone_number', 'id_number', 'gender', 'date_of_birth',
            'profile_picture', 'role', 'role_display', 'is_verified',
            'is_staff', 'is_superuser',
            'last_login', 'created_at', 'updated_at',
            'company_name', 'tenant_subdomain', 'company', 'company_id'
        ]
        read_only_fields = ['id', 'email', 'role', 'is_verified', 'is_staff', 'is_superuser', 'last_login', 'created_at', 'updated_at']
    
    def get_full_name(self, obj):
        return obj.get_full_name()

class PasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(required=True, write_only=True)
    new_password = serializers.CharField(required=True, write_only=True)

    def validate_current_password(self, value):
        user = self.context['request'].user
        if not user.check_password(value):
            logger.warning("Password change failed: incorrect current password for user_id=%s", user.id)
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def validate_new_password(self, value):
        validate_password(value)
        return value

    def save(self):
        user = self.context['request'].user
        user.set_password(self.validated_data['new_password'])
        user.save()
        logger.info("Password changed successfully for user_id=%s", user.id)

class CompanySerializer(serializers.ModelSerializer):
    """Serializer for Company model"""
    
    total_customers = serializers.IntegerField(read_only=True)
    active_customers = serializers.IntegerField(read_only=True)
    logo_url = serializers.SerializerMethodField()
    
    class Meta:
        model = Company
        fields = [
            'id', 'name', 'slug', 'company_type', 'email', 'phone_number',
            'address', 'city', 'county', 'postal_code', 'registration_number',
            'tax_pin', 'website', 'logo', 'logo_url', 'is_active', 'subscription_plan',
            'country', 'base_currency',
            'subscription_expiry', 'total_customers', 'active_customers',
            'created_at', 'updated_at', 'created_by'
        ]
        read_only_fields = ['id', 'slug', 'created_at', 'updated_at']
    
    def validate_name(self, value):
        """Validate company name uniqueness"""
        if self.instance:
            if Company.objects.exclude(id=self.instance.id).filter(name=value).exists():
                raise serializers.ValidationError("A company with this name already exists.")
        else:
            if Company.objects.filter(name=value).exists():
                raise serializers.ValidationError("A company with this name already exists.")
        return value
    
    def create(self, validated_data):
        """Create company and generate slug"""
        from django.utils.text import slugify
        
        name = validated_data.get('name')
        validated_data['slug'] = slugify(name)
        
        # Set created_by if not provided
        if 'created_by' not in validated_data:
            request = self.context.get('request')
            if request and request.user.is_authenticated:
                validated_data['created_by'] = request.user
        
        return super().create(validated_data)

    def get_logo_url(self, obj):
        if not obj.logo:
            return None
        request = self.context.get('request')
        if request:
            return request.build_absolute_uri(obj.logo.url)
        return obj.logo.url


class CompanyBrandingSerializer(serializers.ModelSerializer):
    """Focused serializer for tenant dashboard branding."""

    logo_url = serializers.SerializerMethodField()
    remove_logo = serializers.BooleanField(write_only=True, required=False, default=False)

    class Meta:
        model = Company
        fields = ['id', 'name', 'logo', 'logo_url', 'remove_logo', 'updated_at']
        read_only_fields = ['id', 'logo_url', 'updated_at']

    def validate_name(self, value):
        value = value.strip()
        if not value:
            raise serializers.ValidationError("Company name is required.")

        queryset = Company.objects.filter(name__iexact=value)
        if self.instance:
            queryset = queryset.exclude(id=self.instance.id)
        if queryset.exists():
            raise serializers.ValidationError("A company with this name already exists.")
        return value

    def update(self, instance, validated_data):
        remove_logo = validated_data.pop('remove_logo', False)
        if remove_logo and instance.logo:
            instance.logo.delete(save=False)
            instance.logo = None
        return super().update(instance, validated_data)

    def get_logo_url(self, obj):
        if not obj.logo:
            return None
        request = self.context.get('request')
        if request:
            return request.build_absolute_uri(obj.logo.url)
        return obj.logo.url


class TenantSerializer(serializers.ModelSerializer):
    """Serializer for Tenant model"""
    
    company = CompanySerializer(read_only=True)
    company_id = serializers.PrimaryKeyRelatedField(
        queryset=Company.objects.all(),
        write_only=True,
        source='company'
    )
    
    class Meta:
        model = Tenant
        fields = [
            'id', 'company', 'company_id', 'subdomain', 'domain',
            'database_name', 'is_active', 'status', 'max_users',
            'max_customers', 'features', 'billing_cycle', 'monthly_rate',
            'next_billing_date', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def validate_subdomain(self, value):
        """Validate subdomain uniqueness"""
        if self.instance:
            if Tenant.objects.exclude(id=self.instance.id).filter(subdomain=value).exists():
                raise serializers.ValidationError("This subdomain is already taken.")
        else:
            if Tenant.objects.filter(subdomain=value).exists():
                raise serializers.ValidationError("This subdomain is already taken.")
        
        # Validate subdomain format
        import re
        if not re.match(r'^[a-z0-9]+(-[a-z0-9]+)*$', value):
            raise serializers.ValidationError(
                "Subdomain can only contain lowercase letters, numbers, and hyphens."
            )
        
        return value


class SystemSettingsSerializer(serializers.ModelSerializer):
    """Serializer for SystemSettings model"""
    
    class Meta:
        model = SystemSettings
        fields = [
            'id', 'key', 'name', 'value', 'setting_type',
            'data_type', 'is_public', 'description',
            'created_at', 'updated_at', 'updated_by'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
    
    def validate_key(self, value):
        """Validate setting key format"""
        import re
        if not re.match(r'^[a-z][a-z0-9_]*$', value):
            raise serializers.ValidationError(
                "Key must start with a letter and contain only lowercase letters, numbers, and underscores."
            )
        return value


class AuditLogSerializer(serializers.ModelSerializer):
    """Serializer for AuditLog model"""
    
    user_email = serializers.EmailField(source='user.email', read_only=True)
    user_full_name = serializers.SerializerMethodField()
    action_display = serializers.CharField(source='get_action_display', read_only=True)
    
    class Meta:
        model = AuditLog
        fields = [
            'id', 'user', 'user_email', 'user_full_name',
            'action', 'action_display', 'model_name', 'object_id',
            'object_repr', 'changes', 'ip_address', 'user_agent',
            'timestamp', 'tenant'
        ]
        read_only_fields = ['id', 'timestamp']
    
    def get_user_full_name(self, obj):
        if obj.user:
            return obj.user.get_full_name()
        return None


class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Custom token serializer with additional user data - SIMPLE FIX"""
    
    # Add email field
    email = serializers.EmailField()
    
    def validate(self, attrs):
        logger.debug("JWT login attempt received")
        
        # Get email from attrs
        email = attrs.get('email')
        if not email:
            logger.warning("JWT login failed: missing email")
            raise serializers.ValidationError("Email is required")
        
        # Map email to username for parent class
        attrs['username'] = email
        
        try:
            data = super().validate(attrs)
            
            # Add user data to response
            user = self.user
            tenant_subdomain = getattr(user, "tenant_subdomain", None)
            company_name = getattr(user, "company_name", None)

            if tenant_subdomain or company_name:
                from django_tenants.utils import get_public_schema_name, schema_context
                from apps.core.models import Company, Tenant

                with schema_context(get_public_schema_name()):
                    tenant_exists = (
                        Tenant.objects.filter(subdomain=tenant_subdomain).exists()
                        if tenant_subdomain
                        else False
                    )
                    company_exists = (
                        Company.objects.filter(name=company_name).exists()
                        if company_name
                        else False
                    )

                if (tenant_subdomain and not tenant_exists) or (company_name and not company_exists):
                    logger.warning(
                        "JWT login blocked for deleted tenant account user_id=%s tenant_subdomain=%s company_name=%s",
                        user.id,
                        tenant_subdomain,
                        company_name,
                    )
                    raise serializers.ValidationError({
                        "detail": "This tenant account no longer exists."
                    })

            data['user'] = {
                'id': user.id,
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name,
                'role': user.role,
                'is_verified': user.is_verified,
                'is_superuser': user.is_superuser,
            }
            
            logger.info("JWT login success for user_id=%s", user.id)
            return data
            
        except Exception as e:
            logger.warning("JWT login failed: %s", str(e))
            raise serializers.ValidationError({
                "detail": "Invalid email or password."
            })


class DashboardStatsSerializer(serializers.Serializer):
    """Serializer for dashboard statistics"""
    
    total_users = serializers.IntegerField(default=0)
    total_companies = serializers.IntegerField(default=0)
    total_customers = serializers.IntegerField(default=0)
    total_staff = serializers.IntegerField(default=0)
    total_active_customers = serializers.IntegerField(default=0)
    total_inactive_customers = serializers.IntegerField(default=0)
    recent_activity = serializers.ListField(child=serializers.DictField(), required=False)


class GlobalSystemSettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = GlobalSystemSettings
        fields = '__all__'
        read_only_fields = ['id']


class CustomTokenRefreshSerializer(TokenRefreshSerializer):
    def validate(self, attrs):
        try:
            return super().validate(attrs)
        except User.DoesNotExist:
            logger.warning("Token refresh failed: user no longer exists")
            raise InvalidToken('User no longer exists')

class CompanyRegisterSerializer(serializers.Serializer):
    """Serializer for public ISP/company registration"""
    
    # Company fields (only name and email required)
    company_name = serializers.CharField(max_length=255, required=True)
    company_email = serializers.EmailField(required=True)
    
    # Optional company fields
    company_phone = serializers.CharField(max_length=20, required=False, allow_blank=True)
    company_address = serializers.CharField(required=False, allow_blank=True)
    company_city = serializers.CharField(max_length=100, required=False, allow_blank=True)
    company_county = serializers.CharField(max_length=100, required=False, allow_blank=True)
    company_registration_number = serializers.CharField(max_length=100, required=False, allow_blank=True)
    company_tax_pin = serializers.CharField(max_length=50, required=False, allow_blank=True)
    company_website = serializers.URLField(required=False, allow_blank=True)
    
    # Country field for multi-country expansion
    company_country = serializers.ChoiceField(choices=COUNTRY_CHOICES, required=False, default='KE')
    
    # Admin user fields
    admin_first_name = serializers.CharField(max_length=100, required=True)
    admin_last_name = serializers.CharField(max_length=100, required=True)
    admin_email = serializers.EmailField(required=True)
    admin_phone = serializers.CharField(max_length=20, required=True)
    admin_password = serializers.CharField(
        write_only=True,
        required=True,
        style={'input_type': 'password'},
        validators=[validate_password]
    )
    
    def validate_company_email(self, value):
        """Check company email uniqueness"""
        if Company.objects.filter(email=value).exists():
            raise serializers.ValidationError("A company with this email already exists.")
        return value
    
    def validate_admin_email(self, value):
        """Check admin email uniqueness"""
        if User.objects.filter(email=value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value
    
    def validate(self, data):
        """Additional cross-field validation if needed"""
        # Optional: ensure admin_email != company_email if you want them separate
        if data.get('admin_email') == data.get('company_email'):
            raise serializers.ValidationError({
                "admin_email": "Admin email should be different from company email."
            })
        return data


class ChangelogSerializer(serializers.ModelSerializer):
    """Serializer for Changelog model"""
    
    update_type_display = serializers.CharField(source='get_update_type_display', read_only=True)
    notify_email = serializers.BooleanField(write_only=True, required=False, default=False)
    notify_sms = serializers.BooleanField(write_only=True, required=False, default=False)
    notify_in_app = serializers.BooleanField(write_only=True, required=False, default=True)
    
    class Meta:
        model = Changelog
        fields = [
            'id', 'title', 'version', 'content', 'update_type',
            'update_type_display', 'is_published', 'release_date',
            'notification_channels', 'notification_sent_at', 'notification_summary',
            'notify_email', 'notify_sms', 'notify_in_app',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'notification_channels', 'notification_sent_at', 'notification_summary', 'created_at', 'updated_at']

    def validate(self, data):
        data = super().validate(data)
        notify_email = data.get('notify_email', False)
        notify_sms = data.get('notify_sms', False)
        notify_in_app = data.get('notify_in_app', False)
        is_published = data.get('is_published', getattr(self.instance, 'is_published', True))

        if (notify_email or notify_sms or notify_in_app) and not is_published:
            raise serializers.ValidationError({
                'is_published': 'Publish the changelog before sending tenant notifications.'
            })
        return data

    def create(self, validated_data):
        validated_data.pop('notify_email', None)
        validated_data.pop('notify_sms', None)
        validated_data.pop('notify_in_app', None)
        return super().create(validated_data)

    def update(self, instance, validated_data):
        validated_data.pop('notify_email', None)
        validated_data.pop('notify_sms', None)
        validated_data.pop('notify_in_app', None)
        return super().update(instance, validated_data)

    def get_notification_channels_requested(self):
        channels = []
        if self.validated_data.get('notify_email'):
            channels.append('email')
        if self.validated_data.get('notify_sms'):
            channels.append('sms')
        if self.validated_data.get('notify_in_app'):
            channels.append('in_app')
        return channels


class FeatureRequestSerializer(serializers.ModelSerializer):
    """Serializer for FeatureRequest model"""
    
    requested_by_name = serializers.ReadOnlyField(source='requested_by_tenant.company.name')
    has_upvoted = serializers.SerializerMethodField()

    class Meta:
        model = FeatureRequest
        fields = [
            'id', 'title', 'description', 'category', 'status', 
            'requested_by_name', 'admin_comment', 'upvotes_count', 
            'has_upvoted', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'upvotes_count', 'created_at', 'updated_at']

    def get_has_upvoted(self, obj):
        """Check if the current tenant has upvoted this feature request"""
        request = self.context.get('request')
        if request and hasattr(request, 'tenant'):
            return FeatureUpvote.objects.filter(feature_request=obj, tenant=request.tenant).exists()
        return False