from rest_framework import serializers
from django.contrib.auth import get_user_model
from .models import (
    NotificationTemplate, 
    Notification, 
    AlertRule,
    NotificationPreference,
    BulkNotification,
    NotificationLog
)

User = get_user_model()

class NotificationTemplateSerializer(serializers.ModelSerializer):
    notification_type_display = serializers.CharField(
        source='get_notification_type_display', 
        read_only=True
    )
    trigger_event_display = serializers.CharField(
        source='get_trigger_event_display', 
        read_only=True
    )
    
    class Meta:
        model = NotificationTemplate
        fields = [
            'id', 'name', 'notification_type', 'notification_type_display',
            'trigger_event', 'trigger_event_display', 'subject', 'message_template',
            'is_active', 'priority', 'available_variables',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']

class NotificationSerializer(serializers.ModelSerializer):
    user = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        required=False,
        allow_null=True
    )
    user_details = serializers.SerializerMethodField()
    notification_type_display = serializers.CharField(
        source='get_notification_type_display', 
        read_only=True
    )
    status_display = serializers.CharField(
        source='get_status_display', 
        read_only=True
    )
    priority_display = serializers.CharField(
        source='get_priority_display', 
        read_only=True
    )
    
    class Meta:
        model = Notification
        fields = [
            'id', 'user', 'user_details', 'template', 'notification_type',
            'notification_type_display', 'subject', 'message',
            'recipient_email', 'recipient_phone', 'recipient_device_token',
            'status', 'status_display', 'priority', 'priority_display',
            'sent_at', 'delivered_at', 'read_at', 'retry_count',
            'max_retries', 'metadata', 'error_message',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'created_at', 'updated_at', 'sent_at', 'delivered_at',
            'read_at', 'status', 'error_message', 'retry_count'
        ]
    
    def get_user_details(self, obj):
        if obj.user:
            return {
                'id': obj.user.id,
                'email': obj.user.email,
                'first_name': obj.user.first_name,
                'last_name': obj.user.last_name
            }
        return None
    
    def create(self, validated_data):
        # Add created_by user if available
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            validated_data['metadata'] = validated_data.get('metadata', {})
            validated_data['metadata']['created_by'] = request.user.id
        
        return super().create(validated_data)

class AlertRuleSerializer(serializers.ModelSerializer):
    alert_type_display = serializers.CharField(
        source='get_alert_type_display', 
        read_only=True
    )
    condition_type_display = serializers.CharField(
        source='get_condition_type_display', 
        read_only=True
    )
    notification_templates_details = NotificationTemplateSerializer(
        source='notification_templates',
        many=True,
        read_only=True
    )
    specific_users_details = serializers.SerializerMethodField()
    
    class Meta:
        model = AlertRule
        fields = [
            'id', 'name', 'description', 'alert_type', 'alert_type_display',
            'model_name', 'field_name', 'condition_type', 'condition_type_display',
            'condition_value', 'notification_templates', 'notification_templates_details',
            'check_interval', 'is_active', 'enabled_days', 'enabled_hours',
            'cooldown_minutes', 'target_roles', 'specific_users', 'specific_users_details',
            'created_at', 'updated_at', 'last_checked', 'last_triggered'
        ]
        read_only_fields = [
            'created_at', 'updated_at', 'last_checked', 'last_triggered'
        ]
    
    def get_specific_users_details(self, obj):
        return [
            {
                'id': user.id,
                'email': user.email,
                'first_name': user.first_name,
                'last_name': user.last_name
            }
            for user in obj.specific_users.all()
        ]
    
    def validate_model_name(self, value):
        """Validate that model exists"""
        from django.apps import apps
        
        try:
            app_label, model_name = value.split('.')
            apps.get_model(app_label, model_name)
        except (ValueError, LookupError):
            raise serializers.ValidationError(f"Model '{value}' does not exist")
        
        return value
    
    def validate_enabled_days(self, value):
        """Validate days format"""
        try:
            days = [int(d.strip()) for d in value.split(',')]
            for day in days:
                if day < 0 or day > 6:
                    raise ValueError
        except ValueError:
            raise serializers.ValidationError(
                "Days must be comma-separated numbers 0-6 (0=Sunday)"
            )
        return value
    
    def validate_enabled_hours(self, value):
        """Validate hours format"""
        try:
            if '-' in value:
                start, end = value.split('-')
                start_hour = int(start.strip())
                end_hour = int(end.strip())
                if start_hour < 0 or start_hour > 23 or end_hour < 0 or end_hour > 23:
                    raise ValueError
        except ValueError:
            raise serializers.ValidationError(
                "Hours must be in format 'start-end' (e.g., '8-17')"
            )
        return value

class NotificationPreferenceSerializer(serializers.ModelSerializer):
    user = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        required=False
    )
    user_details = serializers.SerializerMethodField()
    preferred_language_display = serializers.CharField(
        source='get_preferred_language_display',
        read_only=True
    )
    
    class Meta:
        model = NotificationPreference
        fields = [
            'id', 'user', 'user_details',
            'receive_email', 'receive_sms', 'receive_push', 'receive_in_app',
            'billing_notifications', 'service_notifications',
            'support_notifications', 'marketing_notifications',
            'system_notifications', 'quiet_hours_enabled',
            'quiet_start_time', 'quiet_end_time',
            'daily_notification_limit', 'preferred_language',
            'preferred_language_display', 'updated_at'
        ]
        read_only_fields = ['updated_at']
    
    def get_user_details(self, obj):
        return {
            'id': obj.user.id,
            'email': obj.user.email,
            'first_name': obj.user.first_name,
            'last_name': obj.user.last_name
        }
    
    def create(self, validated_data):
        # Ensure one preference per user
        user = validated_data.get('user')
        if user and NotificationPreference.objects.filter(user=user).exists():
            raise serializers.ValidationError(
                "Notification preference already exists for this user"
            )
        return super().create(validated_data)

class BulkNotificationSerializer(serializers.ModelSerializer):
    notification_type_display = serializers.CharField(
        source='get_notification_type_display',
        read_only=True
    )
    status_display = serializers.CharField(
        source='get_status_display',
        read_only=True
    )
    target_segment_display = serializers.CharField(
        source='get_target_segment_display',
        read_only=True
    )
    created_by_details = serializers.SerializerMethodField()
    
    class Meta:
        model = BulkNotification
        fields = [
            'id', 'name', 'notification_type', 'notification_type_display',
            'subject', 'message', 'target_segment', 'target_segment_display',
            'custom_recipients', 'status', 'status_display',
            'scheduled_for', 'started_at', 'completed_at',
            'total_recipients', 'sent_count', 'failed_count',
            'created_by', 'created_by_details', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'started_at', 'completed_at', 'total_recipients',
            'sent_count', 'failed_count', 'created_at', 'updated_at'
        ]
    
    def get_created_by_details(self, obj):
        if obj.created_by:
            return {
                'id': obj.created_by.id,
                'email': obj.created_by.email,
                'first_name': obj.created_by.first_name,
                'last_name': obj.created_by.last_name
            }
        return None
    
    def validate_scheduled_for(self, value):
        """Ensure scheduled time is in future"""
        from django.utils import timezone
        if value and value < timezone.now():
            raise serializers.ValidationError(
                "Scheduled time must be in the future"
            )
        return value

class NotificationLogSerializer(serializers.ModelSerializer):
    notification_details = NotificationSerializer(
        source='notification',
        read_only=True
    )
    user_details = serializers.SerializerMethodField()
    
    class Meta:
        model = NotificationLog
        fields = [
            'id', 'notification', 'notification_details',
            'user', 'user_details', 'action', 'details',
            'timestamp', 'ip_address'
        ]
        read_only_fields = ['timestamp', 'ip_address']
    
    def get_user_details(self, obj):
        if obj.user:
            return {
                'id': obj.user.id,
                'email': obj.user.email,
                'first_name': obj.user.first_name,
                'last_name': obj.user.last_name
            }
        return None

class SendNotificationSerializer(serializers.Serializer):
    """Serializer for sending manual notifications"""
    notification_type = serializers.ChoiceField(
        choices=NotificationTemplate.NOTIFICATION_TYPES
    )
    recipient_type = serializers.ChoiceField(
        choices=[('user', 'User'), ('email', 'Email'), ('phone', 'Phone')]
    )
    user_id = serializers.IntegerField(required=False)
    email = serializers.EmailField(required=False)
    phone = serializers.CharField(required=False)
    subject = serializers.CharField(required=False)
    message = serializers.CharField()
    template_id = serializers.IntegerField(required=False)
    template_variables = serializers.JSONField(required=False, default=dict)
    priority = serializers.IntegerField(
        min_value=1,
        max_value=5,
        default=2
    )
    
    def validate(self, data):
        recipient_type = data.get('recipient_type')
        
        if recipient_type == 'user' and not data.get('user_id'):
            raise serializers.ValidationError({
                'user_id': 'User ID is required when recipient_type is "user"'
            })
        elif recipient_type == 'email' and not data.get('email'):
            raise serializers.ValidationError({
                'email': 'Email is required when recipient_type is "email"'
            })
        elif recipient_type == 'phone' and not data.get('phone'):
            raise serializers.ValidationError({
                'phone': 'Phone is required when recipient_type is "phone"'
            })
        
        return data

class TestNotificationSerializer(serializers.Serializer):
    """Serializer for testing notifications"""
    notification_type = serializers.ChoiceField(
        choices=NotificationTemplate.NOTIFICATION_TYPES
    )
    recipient_email = serializers.EmailField(required=False)
    recipient_phone = serializers.CharField(required=False)
    message = serializers.CharField()
    
    def validate(self, data):
        notification_type = data.get('notification_type')
        
        if notification_type == 'email' and not data.get('recipient_email'):
            raise serializers.ValidationError({
                'recipient_email': 'Email is required for email notifications'
            })
        elif notification_type == 'sms' and not data.get('recipient_phone'):
            raise serializers.ValidationError({
                'recipient_phone': 'Phone is required for SMS notifications'
            })
        
        return data