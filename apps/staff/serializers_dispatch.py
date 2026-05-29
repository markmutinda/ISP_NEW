"""
Dispatch serializers – Technician & DispatchJob
"""
from rest_framework import serializers
from .models import Technician, DispatchJob


class TechnicianSerializer(serializers.ModelSerializer):
    name = serializers.SerializerMethodField()
    email = serializers.SerializerMethodField()

    class Meta:
        model = Technician
        fields = [
            'id', 'name', 'email', 'employee_id', 'phone',
            'skills', 'status', 'current_location',
            'latitude', 'longitude',
            'total_jobs_completed', 'average_rating',
            'is_active', 'created_at',
        ]
        read_only_fields = ['id', 'created_at', 'total_jobs_completed', 'average_rating']

    def get_name(self, obj):
        return obj.user.get_full_name()

    def get_email(self, obj):
        return obj.user.email


class TechnicianCreateSerializer(serializers.ModelSerializer):
    """Used when creating a technician – accepts user_id instead of nested user."""
    user_id = serializers.IntegerField(write_only=True)

    class Meta:
        model = Technician
        fields = [
            'user_id', 'employee_id', 'phone', 'skills',
            'status', 'current_location',
        ]

    def create(self, validated_data):
        from django.contrib.auth import get_user_model
        User = get_user_model()
        user_id = validated_data.pop('user_id')
        user = User.objects.get(pk=user_id)
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
