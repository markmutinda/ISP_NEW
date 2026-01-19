"""
Serializers for core app
"""
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import authenticate
from .models import GlobalSystemSettings  # Add this
from rest_framework_simplejwt.exceptions import InvalidToken  # Add this
from rest_framework_simplejwt.serializers import TokenRefreshSerializer  # Already there or add
from django.utils import timezone
from django.contrib.auth.password_validation import validate_password
from .models import User, Company, Tenant, SystemSettings, AuditLog


class UserSerializer(serializers.ModelSerializer):
    """Serializer for User model"""
    
    password = serializers.CharField(write_only=True, required=False)
    confirm_password = serializers.CharField(write_only=True, required=False)
    role_display = serializers.CharField(source='get_role_display_name', read_only=True)
    full_name = serializers.SerializerMethodField()
    company_name = serializers.CharField(source='company.name', read_only=True)  # NEW
    tenant_subdomain = serializers.CharField(source='tenant.subdomain', read_only=True)  # NEW
    
    class Meta:
        model = User
        fields = [
            'id', 'email', 'first_name', 'last_name', 'full_name',
            'phone_number', 'id_number', 'gender', 'date_of_birth',
            'profile_picture', 'role', 'role_display', 'is_active',
            'is_verified', 'is_staff', 'is_superuser',
            'company', 'company_name', 'tenant', 'tenant_subdomain',
            'password', 'confirm_password', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'is_staff', 'is_superuser']
    
    def get_full_name(self, obj):
        return obj.get_full_name()
    
    def validate(self, data):
        """Validate password confirmation"""
        password = data.get('password')
        confirm_password = data.get('confirm_password')
        
        if password and password != confirm_password:
            raise serializers.ValidationError({
                "password": "Passwords do not match."
            })
        
        # Remove confirm_password from validated data
        if 'confirm_password' in data:
            del data['confirm_password']
        
        return data
    
    def validate_password(self, value):
        """Validate password strength"""
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
        required=False,
        style={'input_type': 'password'}
    )
    
    class Meta:
        model = User
        fields = [
            'id', 'email', 'password', 'confirm_password',
            'first_name', 'last_name', 'phone_number', 'id_number',
            'gender', 'date_of_birth', 'role', 'company', 'tenant' 
        ]
        read_only_fields = ['id']
    
    def validate(self, attrs):
        if attrs.get('password') != attrs.get('confirm_password'):
            raise serializers.ValidationError({
                "password": "Password fields didn't match."
            })
        return attrs
    
    def create(self, validated_data):
        validated_data.pop('confirm_password')
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
            'date_of_birth', 'current_password', 'new_password'
        ]
        read_only_fields = ['id', 'email']
    
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
            raise serializers.ValidationError({
                "email": "Invalid email or password."
            })
        
        if not user.is_active:
            raise serializers.ValidationError({
                "email": "Account is deactivated."
            })
        
        # Generate tokens
        refresh = RefreshToken.for_user(user)
        
        # Update last login
        user.last_login = timezone.now()
        user.save(update_fields=['last_login'])
        
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
            raise serializers.ValidationError({
                "refresh": "Invalid refresh token."
            })
        
        # Get user from token
        user_id = token.payload.get('user_id')
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            raise serializers.ValidationError({
                "refresh": "User not found."
            })
        
        # Generate new access token
        new_access_token = RefreshToken.for_user(user).access_token
        
        data['access'] = str(new_access_token)
        return data


class ProfileSerializer(serializers.ModelSerializer):
    """Serializer for user profile"""
    
    full_name = serializers.SerializerMethodField()
    role_display = serializers.CharField(source='get_role_display_name', read_only=True)
    
    class Meta:
        model = User
        fields = [
            'id', 'email', 'first_name', 'last_name', 'full_name',
            'phone_number', 'id_number', 'gender', 'date_of_birth',
            'profile_picture', 'role', 'role_display', 'is_verified',
            'last_login', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'email', 'role', 'is_verified', 'last_login', 'created_at', 'updated_at']
    
    def get_full_name(self, obj):
        return obj.get_full_name()


class PasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(required=True, write_only=True)
    new_password = serializers.CharField(required=True, write_only=True)

    def validate_current_password(self, value):
        user = self.context['request'].user
        if not user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def validate_new_password(self, value):
        validate_password(value)
        return value

    def save(self):
        user = self.context['request'].user
        user.set_password(self.validated_data['new_password'])
        user.save()

class CompanySerializer(serializers.ModelSerializer):
    """Serializer for Company model"""
    
    total_customers = serializers.IntegerField(read_only=True)
    active_customers = serializers.IntegerField(read_only=True)
    
    class Meta:
        model = Company
        fields = [
            'id', 'name', 'slug', 'company_type', 'email', 'phone_number',
            'address', 'city', 'county', 'postal_code', 'registration_number',
            'tax_pin', 'website', 'logo', 'is_active', 'subscription_plan',
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
    """Custom token serializer with additional user data"""
    
    def validate(self, attrs):
        data = super().validate(attrs)
        
        # Add user data to response
        user = self.user
        data['user'] = {
            'id': user.id,
            'email': user.email,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'role': user.role,
            'is_verified': user.is_verified,
        }
        
        return data


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
