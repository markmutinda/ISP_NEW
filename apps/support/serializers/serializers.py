from rest_framework import serializers
from django.utils import timezone

from ..models import SupportTicket, SupportTicketMessage


class SupportTicketMessageSerializer(serializers.ModelSerializer):
    """
    Serializer for individual ticket messages.
    Used in both ticket detail and standalone message endpoints.
    """
    sender_name = serializers.SerializerMethodField()
    sender_id = serializers.IntegerField(source='sender.id', read_only=True)

    class Meta:
        model = SupportTicketMessage
        fields = [
            'id',
            'ticket',
            'sender_type',
            'sender',           # user pk
            'sender_id',
            'sender_name',
            'message',
            'is_internal',
            'attachments',
            'created_at',
        ]
        read_only_fields = ['created_at', 'sender', 'sender_id']

    def get_sender_name(self, obj):
        # Uses the @property from model
        return obj.sender_name


class SupportTicketListSerializer(serializers.ModelSerializer):
    """
    Lightweight serializer used for list views (/tickets/ and /my/)
    → avoids loading full message history → much better performance
    """
    customer_name = serializers.CharField(source='customer_name', read_only=True)
    customer_email = serializers.CharField(source='customer_email', read_only=True)
    customer_phone = serializers.CharField(source='customer_phone', read_only=True)
    customer_plan = serializers.SerializerMethodField()
    assigned_to_name = serializers.CharField(source='assigned_to_name', read_only=True, allow_null=True)

    class Meta:
        model = SupportTicket
        fields = [
            'id',
            'ticket_number',
            'subject',
            'status',
            'priority',
            'category',
            'customer',
            'customer_name',
            'customer_email',
            'customer_phone',
            'customer_plan',
            'assigned_to',
            'assigned_to_name',
            'sla_breached',
            'created_at',
            'updated_at',
            # Intentionally NOT including 'description' or 'messages' here
        ]
        read_only_fields = [
            'ticket_number', 'sla_breached', 'created_at', 'updated_at',
            'customer_name', 'customer_email', 'customer_phone', 'customer_plan',
            'assigned_to_name',
        ]

    def get_customer_plan(self, obj):
        try:
            active_service = obj.customer.services.filter(status='ACTIVE').first()
            return active_service.service_plan.name if active_service and active_service.service_plan else None
        except (AttributeError, Exception):
            return None


class SupportTicketDetailSerializer(serializers.ModelSerializer):
    """
    Full serializer used for retrieve (/tickets/<id>/) and when messages are needed
    """
    customer_name = serializers.CharField(source='customer_name', read_only=True)
    customer_email = serializers.CharField(source='customer_email', read_only=True)
    customer_phone = serializers.CharField(source='customer_phone', read_only=True)
    customer_plan = serializers.SerializerMethodField()
    assigned_to_name = serializers.CharField(source='assigned_to_name', read_only=True, allow_null=True)
    messages = SupportTicketMessageSerializer(many=True, read_only=True)

    class Meta:
        model = SupportTicket
        fields = [
            'id',
            'ticket_number',
            'subject',
            'description',
            'status',
            'priority',
            'category',
            'customer',
            'customer_name',
            'customer_email',
            'customer_phone',
            'customer_plan',
            'assigned_to',
            'assigned_to_name',
            'sla_breached',
            'first_response_at',
            'resolved_at',
            'created_at',
            'updated_at',
            'messages',
        ]
        read_only_fields = [
            'ticket_number',
            'sla_breached',
            'first_response_at',
            'resolved_at',
            'created_at',
            'updated_at',
            'customer_name', 'customer_email', 'customer_phone', 'customer_plan',
            'assigned_to_name',
            'messages',
        ]

    def get_customer_plan(self, obj):
        try:
            active_service = obj.customer.services.filter(status='ACTIVE').first()
            return active_service.service_plan.name if active_service and active_service.service_plan else None
        except (AttributeError, Exception):
            return None


class TicketCreateSerializer(serializers.ModelSerializer):
    """
    Used only when customers or admins create new tickets
    """
    class Meta:
        model = SupportTicket
        fields = [
            'subject',
            'description',
            'category',
            'priority',
        ]


class TicketUpdateSerializer(serializers.ModelSerializer):
    """
    Used for PATCH /tickets/<id>/ (admin only usually)
    """
    class Meta:
        model = SupportTicket
        fields = [
            'status',
            'priority',
            'category',
            'assigned_to',
            # description/subject usually not changeable after creation
        ]


class TicketReplySerializer(serializers.ModelSerializer):
    """
    Used for POST /tickets/<id>/reply/
    """
    class Meta:
        model = SupportTicketMessage
        fields = [
            'message',
            'is_internal',
            'attachments',
        ]


class TicketAssignSerializer(serializers.Serializer):
    assigned_to = serializers.IntegerField(min_value=1)


class TicketStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=SupportTicket.STATUS_CHOICES)


class TicketStatsSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    open = serializers.IntegerField()
    in_progress = serializers.IntegerField()
    pending = serializers.IntegerField()
    resolved = serializers.IntegerField()
    closed = serializers.IntegerField()
    avg_response_time = serializers.CharField(allow_null=True)
    avg_resolution_time = serializers.CharField(allow_null=True)
    sla_compliance_rate = serializers.FloatField()
    tickets_today = serializers.IntegerField()
    tickets_this_week = serializers.IntegerField()


# Optional: if you want to expose message creation separately (rare)
class SupportTicketMessageCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = SupportTicketMessage
        fields = ['message', 'is_internal', 'attachments']
