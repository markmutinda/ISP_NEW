"""
Dispatch serializers - Technician & DispatchJob.
"""
from django.utils.crypto import get_random_string
from django.utils import timezone
from rest_framework import serializers

from .models import Technician, DispatchJob


class TechnicianSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()
    email = serializers.SerializerMethodField()
    active_jobs = serializers.SerializerMethodField()
    completed_today = serializers.SerializerMethodField()

    class Meta:
        model = Technician
        fields = [
            'id', 'name', 'email', 'employee_id', 'phone',
            'skills', 'status', 'current_location',
            'latitude', 'longitude',
            'total_jobs_completed', 'average_rating',
            'active_jobs', 'completed_today',
            'is_active', 'created_at',
        ]
        read_only_fields = ['id', 'created_at', 'total_jobs_completed', 'average_rating']

    def get_name(self, obj):
        return obj.user.get_full_name()

    def get_email(self, obj):
        return obj.user.email

    def get_active_jobs(self, obj):
        return obj.jobs.exclude(status__in=['completed', 'cancelled']).count()

    def get_completed_today(self, obj):
        return obj.jobs.filter(status='completed', completed_at__date=timezone.localdate()).count()


class TechnicianCreateSerializer(serializers.ModelSerializer):
    """Create a technician from an existing user or create the user inline."""

    user_id = serializers.IntegerField(write_only=True, required=False)
    first_name = serializers.CharField(write_only=True, required=False, allow_blank=True)
    last_name = serializers.CharField(write_only=True, required=False, allow_blank=True)
    email = serializers.EmailField(write_only=True, required=False, allow_blank=True)
    password = serializers.CharField(write_only=True, required=False, allow_blank=True, min_length=6)

    class Meta:
        model = Technician
        fields = [
            'user_id', 'first_name', 'last_name', 'email', 'password',
            'employee_id', 'phone', 'skills',
            'status', 'current_location',
        ]
        extra_kwargs = {
            'employee_id': {'required': False, 'allow_blank': True},
            'phone': {'required': True},
            'skills': {'required': False},
            'status': {'required': False},
            'current_location': {'required': False, 'allow_blank': True},
        }

    def validate(self, attrs):
        attrs = super().validate(attrs)
        if attrs.get('user_id'):
            return attrs

        from django.contrib.auth import get_user_model
        User = get_user_model()
        email = (attrs.get('email') or '').strip()
        phone = (attrs.get('phone') or '').strip()

        if email and User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError({'email': 'A user with this email already exists.'})
        if phone and User.objects.filter(phone_number=phone).exists():
            raise serializers.ValidationError({'phone': 'A user with this phone number already exists.'})
        return attrs

    def create(self, validated_data):
        from django.contrib.auth import get_user_model
        User = get_user_model()

        user_id = validated_data.pop('user_id', None)
        first_name = validated_data.pop('first_name', '').strip()
        last_name = validated_data.pop('last_name', '').strip()
        email = (validated_data.pop('email', '') or '').strip() or None
        password = validated_data.pop('password', '') or get_random_string(12)

        if user_id:
            user = User.objects.get(pk=user_id)
        else:
            if not first_name and not last_name:
                raise serializers.ValidationError({'first_name': 'Technician name is required.'})
            if not email:
                raise serializers.ValidationError({'email': 'Email is required when creating a technician login.'})

            request = self.context.get('request')
            request_user = getattr(request, 'user', None)
            user = User.objects.create_user(
                email=email,
                password=password,
                first_name=first_name,
                last_name=last_name,
                phone_number=validated_data.get('phone'),
                role='technician',
                is_staff=True,
                is_active=True,
                is_verified=True,
                company=getattr(request_user, 'company', None),
                tenant=getattr(request_user, 'tenant', None),
                company_name=getattr(request_user, 'company_name', None),
                tenant_subdomain=getattr(request_user, 'tenant_subdomain', None),
            )

        return Technician.objects.create(user=user, **validated_data)


class DispatchJobSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(read_only=True)
    customer_phone = serializers.CharField(read_only=True)
    customer_address = serializers.CharField(read_only=True)
    technician_name = serializers.CharField(read_only=True)
    technician = serializers.PrimaryKeyRelatedField(
        source='assigned_to', read_only=True
    )
    ticket_number = serializers.SerializerMethodField()
    ticket_id = serializers.SerializerMethodField()

    class Meta:
        model = DispatchJob
        fields = [
            'id', 'job_number',
            'customer', 'customer_name', 'customer_phone', 'customer_address',
            'job_type', 'description', 'priority', 'status',
            'assigned_to', 'technician', 'technician_name',
            'scheduled_date', 'scheduled_time', 'estimated_duration',
            'started_at', 'completed_at',
            'notes',
            'ticket', 'ticket_id', 'ticket_number',
            'customer_rating', 'customer_feedback',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'job_number', 'customer_name', 'customer_phone',
            'customer_address', 'technician_name', 'technician',
            'started_at', 'completed_at',
            'created_at', 'updated_at',
        ]

    def get_ticket_number(self, obj):
        return obj.ticket.ticket_number if obj.ticket else None

    def get_ticket_id(self, obj):
        return obj.ticket_id


class AssignJobSerializer(serializers.Serializer):
    technician_id = serializers.IntegerField()


class UpdateStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=['in_progress', 'completed', 'cancelled'])
    notes = serializers.CharField(required=False, allow_blank=True, default='')


class NotifyTechnicianSerializer(serializers.Serializer):
    channels = serializers.ListField(
        child=serializers.ChoiceField(choices=['sms', 'email']),
        allow_empty=False,
        required=False,
        default=['sms'],
    )
    sms_message = serializers.CharField(required=False, allow_blank=True)
    email_subject = serializers.CharField(required=False, allow_blank=True)
    email_body = serializers.CharField(required=False, allow_blank=True)
