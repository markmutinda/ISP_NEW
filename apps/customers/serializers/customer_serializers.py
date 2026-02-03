"""
Serializers for Customer model
"""
from rest_framework import serializers
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.customers.models import Customer
from utils.helpers import validate_phone_number

User = get_user_model()


class CustomerCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating new customers"""
    email = serializers.EmailField(required=True)
    first_name = serializers.CharField(max_length=100, required=True)
    last_name = serializers.CharField(max_length=100, required=True)
    phone_number = serializers.CharField(required=True)
    password = serializers.CharField(write_only=True, required=True)
    id_number = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    
    class Meta:
        model = Customer
        fields = [
            'email', 'first_name', 'last_name', 'phone_number', 'password',
            'date_of_birth', 'gender', 'id_type', 'id_number',
            'marital_status', 'occupation', 'employer', 
            'customer_type', 'category', 'referral_source',
            'billing_cycle', 'credit_limit'
        ]
    
    def validate(self, data):
        # Check if user with email exists
        if User.objects.filter(email=data['email']).exists():
            raise serializers.ValidationError(
                {"email": "A user with this email already exists."}
            )
        
        # Validate and format phone number first
        try:
            formatted_phone = validate_phone_number(data['phone_number'])
            data['phone_number'] = formatted_phone
        except Exception as e:
            raise serializers.ValidationError({"phone_number": str(e)})
        
        # Check if phone number already exists (User.phone_number is unique)
        if User.objects.filter(phone_number=data['phone_number']).exists():
            raise serializers.ValidationError(
                {"phone_number": "A user with this phone number already exists."}
            )
        
        # Check if ID number exists (only if provided)
        id_number = data.get('id_number')
        if id_number and Customer.objects.filter(id_number=id_number).exists():
            raise serializers.ValidationError(
                {"id_number": "A customer with this ID number already exists."}
            )
        
        return data
    
    def create(self, validated_data):
        # Extract user data
        user_data = {
            'email': validated_data.pop('email'),
            'first_name': validated_data.pop('first_name'),
            'last_name': validated_data.pop('last_name'),
            'phone_number': validated_data.pop('phone_number'),
            'password': validated_data.pop('password'),
        }
        
        # Create user
        user = User.objects.create_user(
            email=user_data['email'],
            first_name=user_data['first_name'],
            last_name=user_data['last_name'],
            phone_number=user_data['phone_number'],
            password=user_data['password'],
            role='customer'
        )
        
        # Note: With django-tenants, tenant scoping is automatic
        # No need to pass company - it's handled by the schema context
        
        # Generate customer code
        from utils.helpers import generate_customer_code_legacy
        customer_code = generate_customer_code_legacy()
        
        # Create customer (tenant-scoped automatically)
        customer = Customer.objects.create(
            user=user,
            customer_code=customer_code,
            **validated_data
        )
        
        return customer
    
    def to_representation(self, instance):
        """Custom representation to include user fields in response"""
        return {
            'id': instance.id,
            'customer_code': instance.customer_code,
            'first_name': instance.user.first_name,
            'last_name': instance.user.last_name,
            'full_name': instance.user.get_full_name(),
            'email': instance.user.email,
            'phone_number': instance.user.phone_number,
            'status': getattr(instance, 'status', 'ACTIVE'),
            'date_of_birth': instance.date_of_birth,
            'gender': instance.gender,
            'created_at': instance.created_at.isoformat() if hasattr(instance, 'created_at') and instance.created_at else None,
        }


class CustomerUpdateSerializer(serializers.ModelSerializer):
    """Serializer for updating customer details"""
    email = serializers.EmailField(source='user.email', required=False)
    first_name = serializers.CharField(source='user.first_name', required=False)
    last_name = serializers.CharField(source='user.last_name', required=False)
    phone_number = serializers.CharField(source='user.phone_number', required=False)
    
    class Meta:
        model = Customer
        fields = [
            'email', 'first_name', 'last_name', 'phone_number',
            'date_of_birth', 'gender', 'id_type', 'id_number',
            'marital_status', 'occupation', 'employer',
            'customer_type', 'status', 'category',
            'billing_cycle', 'credit_limit', 'outstanding_balance',
            'receive_sms', 'receive_email', 'receive_promotions',
            'notes'
        ]
        read_only_fields = ['id_number', 'outstanding_balance']
    
    def validate_phone_number(self, value):
        if value:
            return validate_phone_number(value)
        return value
    
    def update(self, instance, validated_data):
        # Update user fields if provided
        user_data = validated_data.pop('user', {})
        if user_data:
            user = instance.user
            for attr, value in user_data.items():
                setattr(user, attr, value)
            user.save()
        
        # Update customer fields
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        
        instance.save()
        return instance


class CustomerListSerializer(serializers.ModelSerializer):
    """Serializer for listing customers"""
    full_name = serializers.CharField(source='user.get_full_name')
    email = serializers.EmailField(source='user.email')
    phone_number = serializers.CharField(source='user.phone_number')
    first_name = serializers.CharField(source='user.first_name', read_only=True)
    last_name = serializers.CharField(source='user.last_name', read_only=True)
    phone = serializers.CharField(source='user.phone_number', read_only=True)
    customer_number = serializers.CharField(source='customer_code', read_only=True)
    balance = serializers.CharField(source='outstanding_balance', read_only=True)
    services = serializers.SerializerMethodField()
    radius_credentials = serializers.SerializerMethodField()
    
    class Meta:
        model = Customer
        fields = [
            'id', 'customer_code', 'customer_number', 'first_name', 'last_name',
            'full_name', 'email', 'phone', 'phone_number',
            'customer_type', 'status', 'category', 'activation_date',
            'outstanding_balance', 'balance', 'services', 'radius_credentials',
            'created_at', 'updated_at'
        ]
    
    def get_services(self, obj):
        """Get customer services with nested plan data"""
        from apps.customers.serializers.service_serializers import ServiceConnectionSerializer
        services = obj.services.select_related('plan').all()[:5]  # Limit to 5 for list view
        return ServiceConnectionSerializer(services, many=True).data
    
    def get_radius_credentials(self, obj):
        """Get customer RADIUS credentials for PPPoE/Hotspot login"""
        if hasattr(obj, 'radius_credentials'):
            creds = obj.radius_credentials
            return {
                'username': creds.username,
                'password': creds.password,
                'is_enabled': creds.is_enabled,
                'connection_type': creds.connection_type,
            }
        return None


class CustomerDetailSerializer(serializers.ModelSerializer):
    """Serializer for customer detail view"""
    full_name = serializers.CharField(source='user.get_full_name')
    email = serializers.EmailField(source='user.email')
    phone_number = serializers.CharField(source='user.phone_number')
    
    # Related data counts
    addresses_count = serializers.SerializerMethodField()
    documents_count = serializers.SerializerMethodField()
    services_count = serializers.SerializerMethodField()
    active_services_count = serializers.SerializerMethodField()
    
    class Meta:
        model = Customer
        fields = [
            'id', 'customer_code', 'full_name', 'email', 
            'phone_number', 'alternative_phone', 'date_of_birth', 'gender',
            'id_type', 'id_number', 'marital_status', 'occupation', 'employer',
            'customer_type', 'status', 'category', 'activation_date',
            'deactivation_date', 'referral_source', 'billing_cycle',
            'credit_limit', 'outstanding_balance', 'receive_sms',
            'receive_email', 'receive_promotions', 'notes',
            'addresses_count', 'documents_count', 'services_count',
            'active_services_count', 'created_at', 'updated_at'
        ]
    
    def get_addresses_count(self, obj):
        return obj.addresses.count()
    
    def get_documents_count(self, obj):
        return obj.documents.count()
    
    def get_services_count(self, obj):
        return obj.services.count()
    
    def get_active_services_count(self, obj):
        return obj.services.filter(status='ACTIVE').count()


class CustomerSerializer(serializers.ModelSerializer):
    """General purpose customer serializer"""
    full_name = serializers.CharField(source='user.get_full_name', read_only=True)
    
    class Meta:
        model = Customer
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at', 'customer_code']
