"""
Serializers for Support App
"""
from rest_framework import serializers
from django.utils.text import slugify
from django.utils import timezone
from django.contrib.auth import get_user_model


User = get_user_model()


from ..models import (

    TicketCategory, TicketStatus, Technician, Ticket,
    TicketMessage, TicketActivity, KnowledgeBaseArticle, FAQ,
    ServiceOutage
)
from apps.customers.serializers import CustomerSerializer


class TicketCategorySerializer(serializers.ModelSerializer):
    class Meta:
        model = TicketCategory
        fields = '__all__'


class TicketStatusSerializer(serializers.ModelSerializer):
    class Meta:
        model = TicketStatus
        fields = '__all__'


class TechnicianDetailSerializer(serializers.ModelSerializer):
    user = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = Technician
        fields = '__all__'




class TechnicianAvailabilitySerializer(serializers.ModelSerializer):
    class Meta:
        model = Technician
        fields = ['id', 'is_available', 'availability_schedule']


class TechnicianSerializer(serializers.ModelSerializer):
    user_name = serializers.SerializerMethodField()
    user_email = serializers.SerializerMethodField()
    current_load = serializers.SerializerMethodField()
    department_display = serializers.CharField(source='get_department_display', read_only=True)
    
    class Meta:
        model = Technician
        fields = ['id', 'user', 'user_name', 'user_email', 'employee_id', 'department',
                 'department_display', 'expertise', 'certification', 'is_available',
                 'current_active_tickets', 'max_active_tickets', 'current_load',
                 'average_rating', 'total_tickets_resolved', 'efficiency_score',
                 'work_phone', 'mobile_phone', 'emergency_contact', 'hire_date',
                 'created_at', 'updated_at']
        read_only_fields = ['current_active_tickets', 'average_rating', 
                           'total_tickets_resolved', 'efficiency_score', 'created_at', 'updated_at']
    
    def get_user_name(self, obj):
        return obj.user.get_full_name()
    
    def get_user_email(self, obj):
        return obj.user.email
    
    def get_current_load(self, obj):
        if obj.max_active_tickets > 0:
            return round((obj.current_active_tickets / obj.max_active_tickets) * 100, 2)
        return 0

class TechnicianPerformanceSerializer(serializers.Serializer):
    technician = TechnicianSerializer(read_only=True)
    metrics = serializers.DictField(read_only=True)


class TechnicianCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating technicians"""
    user_id = serializers.PrimaryKeyRelatedField(
        queryset=User.objects.all(),
        source='user',
        write_only=True
    )
    email = serializers.EmailField(write_only=True)
    first_name = serializers.CharField(write_only=True)
    last_name = serializers.CharField(write_only=True)
    
    class Meta:
        model = Technician
        fields = ['user_id', 'email', 'first_name', 'last_name', 'employee_id', 'department',
                 'expertise', 'certification', 'work_phone', 'mobile_phone', 
                 'emergency_contact', 'hire_date', 'max_active_tickets', 'availability_schedule']
    
    def validate(self, data):
        # Check if user already has a technician profile
        user = data.get('user')
        if user and Technician.objects.filter(user=user).exists():
            raise serializers.ValidationError(
                "This user already has a technician profile"
            )
        
        # Validate availability schedule
        availability_schedule = data.get('availability_schedule', {})
        if availability_schedule:
            days = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
            for day in days:
                if day in availability_schedule:
                    schedule = availability_schedule[day]
                    if 'start' not in schedule or 'end' not in schedule:
                        raise serializers.ValidationError(
                            f"{day.capitalize()} schedule must have start and end times"
                        )
        
        return data


class TicketCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ticket
        fields = ['subject', 'description', 'customer', 'category', 'priority', 
                 'contact_person', 'contact_phone', 'contact_email', 'source_channel',
                 'related_device_id', 'related_device_type', 'related_service_id']
    
    def validate(self, data):
        # Validate that customer users can only create tickets for themselves
        request = self.context.get('request')
        if request and not (request.user.role in ['admin', 'staff'] or request.user.is_superuser):
            from apps.customers.models import Customer
            try:
                customer = Customer.objects.get(user=request.user)
                if 'customer' in data and data['customer'] != customer:
                    raise serializers.ValidationError(
                        "You can only create tickets for your own account"
                    )
            except Customer.DoesNotExist:
                raise serializers.ValidationError(
                    "Customer profile not found"
                )
        
        return data


class TicketUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ticket
        fields = ['category', 'priority', 'status', 'assigned_to', 'internal_notes',
                 'resolution_notes', 'resolution_category', 'customer_rating', 'customer_feedback']
    
    def validate(self, data):
        # Only staff can update certain fields
        request = self.context.get('request')
        if request and not (request.user.role in ['admin', 'staff'] or request.user.is_superuser):
            # Customers can only update rating and feedback
            allowed_fields = ['customer_rating', 'customer_feedback']
            for field in data.keys():
                if field not in allowed_fields:
                    raise serializers.ValidationError(
                        f"You are not allowed to update {field}"
                    )
        
        return data


class TicketSerializer(serializers.ModelSerializer):
    customer_details = CustomerSerializer(source='customer', read_only=True)
    category_name = serializers.CharField(source='category.name', read_only=True)
    status_name = serializers.CharField(source='status.name', read_only=True)
    status_color = serializers.CharField(source='status.color', read_only=True)
    assigned_to_name = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    age_hours = serializers.SerializerMethodField()
    is_overdue = serializers.SerializerMethodField()
    time_to_first_response = serializers.SerializerMethodField()
    last_message_preview = serializers.SerializerMethodField()
    
    class Meta:
        model = Ticket
        fields = ['id', 'ticket_number', 'subject', 'description', 'customer', 'customer_details',
                 'category', 'category_name', 'priority', 'status', 'status_name', 'status_color',
                 'assigned_to', 'assigned_to_name', 'created_by', 'created_by_name',
                 'source_channel', 'created_at', 'updated_at', 'sla_due_at', 'first_response_at',
                 'resolved_at', 'closed_at', 'age_hours', 'is_overdue', 'time_to_first_response',
                 'last_message_preview', 'customer_rating', 'is_escalated', 'escalation_level']
        read_only_fields = ['ticket_number', 'created_at', 'updated_at', 'first_response_at',
                           'resolved_at', 'closed_at', 'sla_due_at']
    
    def get_assigned_to_name(self, obj):
        if obj.assigned_to and obj.assigned_to.user:
            return obj.assigned_to.user.get_full_name()
        return None
    
    def get_created_by_name(self, obj):
        if obj.created_by:
            return obj.created_by.get_full_name()
        return None
    
    def get_age_hours(self, obj):
        return round(obj.age, 2)
    
    def get_is_overdue(self, obj):
        return obj.is_overdue
    
    def get_time_to_first_response(self, obj):
        return round(obj.time_to_first_response, 2) if obj.time_to_first_response else None
    
    def get_last_message_preview(self, obj):
        last_message = obj.messages.filter(is_internal=False).order_by('-created_at').first()
        if last_message:
            # Truncate message for preview
            message = last_message.message
            if len(message) > 100:
                return message[:100] + '...'
            return message
        return None


class TicketDetailSerializer(TicketSerializer):
    """Extended serializer for ticket details"""
    messages = serializers.SerializerMethodField()
    activities = serializers.SerializerMethodField()
    
    class Meta(TicketSerializer.Meta):
        fields = TicketSerializer.Meta.fields + [
            'contact_person', 'contact_phone', 'contact_email',
            'related_device_id', 'related_device_type', 'related_service_id',
            'internal_notes', 'resolution_notes', 'resolution_category',
            'customer_feedback', 'escalation_reason', 'messages', 'activities'
        ]
    
    def get_messages(self, obj):
        # Get messages (non-internal for customers, all for staff)
        request = self.context.get('request')
        messages = obj.messages.all()
        
        if request and not (request.user.role in ['admin', 'staff'] or request.user.is_superuser):
            messages = messages.filter(is_internal=False)
        
        return TicketMessageSerializer(messages, many=True).data
    
    def get_activities(self, obj):
        # Only show activities to staff
        request = self.context.get('request')
        if request and (request.user.role in ['admin', 'staff'] or request.user.is_superuser):
            activities = obj.activities.all()[:20]  # Limit to 20 most recent
            return TicketActivitySerializer(activities, many=True).data
        return []


class TicketMessageSerializer(serializers.ModelSerializer):
    sender_name = serializers.SerializerMethodField()
    sender_email = serializers.SerializerMethodField()
    sender_type_display = serializers.CharField(source='get_sender_type_display', read_only=True)
    formatted_time = serializers.SerializerMethodField()
    
    class Meta:
        model = TicketMessage
        fields = ['id', 'ticket', 'message', 'is_internal', 'sender', 'sender_name', 
                 'sender_email', 'sender_type', 'sender_type_display', 'attachments',
                 'read_by_customer', 'read_by_staff', 'read_at', 'created_at', 
                 'updated_at', 'formatted_time']
        read_only_fields = ['created_at', 'updated_at', 'read_at']
    
    def get_sender_name(self, obj):
        if obj.sender:
            return obj.sender.get_full_name()
        return 'System'
    
    def get_sender_email(self, obj):
        if obj.sender:
            return obj.sender.email
        return None
    
    def get_formatted_time(self, obj):
        return obj.created_at.strftime('%Y-%m-%d %H:%M')


class TicketActivitySerializer(serializers.ModelSerializer):
    performed_by_name = serializers.SerializerMethodField()
    activity_type_display = serializers.CharField(source='get_activity_type_display', read_only=True)
    formatted_time = serializers.SerializerMethodField()
    
    class Meta:
        model = TicketActivity
        fields = ['id', 'ticket', 'activity_type', 'activity_type_display', 'description',
                 'performed_by', 'performed_by_name', 'changes', 'created_at', 'formatted_time']
    
    def get_performed_by_name(self, obj):
        if obj.performed_by:
            return obj.performed_by.get_full_name()
        return 'System'
    
    def get_formatted_time(self, obj):
        return obj.created_at.strftime('%Y-%m-%d %H:%M')


class KnowledgeBaseArticleSerializer(serializers.ModelSerializer):
    author_name = serializers.SerializerMethodField()
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    category_display = serializers.CharField(source='get_category_display', read_only=True)
    helpful_percentage = serializers.SerializerMethodField()
    read_time = serializers.SerializerMethodField()
    
    class Meta:
        model = KnowledgeBaseArticle
        fields = ['id', 'title', 'slug', 'excerpt', 'content', 'author', 'author_name',
                 'category', 'category_display', 'subcategory', 'tags', 'status', 'status_display',
                 'is_featured', 'is_pinned', 'view_count', 'helpful_yes', 'helpful_no',
                 'helpful_percentage', 'created_at', 'updated_at', 'published_at', 'read_time']
        read_only_fields = ['slug', 'view_count', 'helpful_yes', 'helpful_no', 'created_at', 
                           'updated_at', 'published_at']
    
    def get_author_name(self, obj):
        if obj.author:
            return obj.author.get_full_name()
        return None
    
    def get_helpful_percentage(self, obj):
        return round(obj.helpful_percentage, 2)
    
    def get_read_time(self, obj):
        """Calculate estimated read time (words per minute = 200)"""
        word_count = len(obj.content.split())
        read_time_minutes = word_count / 200
        if read_time_minutes < 1:
            return "1 min"
        return f"{int(read_time_minutes)} min"


class KnowledgeBaseArticleDetailSerializer(KnowledgeBaseArticleSerializer):
    """Extended serializer for article details"""
    related_articles = KnowledgeBaseArticleSerializer(many=True, read_only=True)
    
    class Meta(KnowledgeBaseArticleSerializer.Meta):
        fields = KnowledgeBaseArticleSerializer.Meta.fields + [
            'meta_title', 'meta_description', 'keywords', 'attachments', 'related_articles'
        ]


class KnowledgeBaseArticleCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = KnowledgeBaseArticle
        fields = ['title', 'content', 'excerpt', 'category', 'subcategory', 'tags',
                 'status', 'is_featured', 'is_pinned', 'meta_title', 'meta_description',
                 'keywords', 'attachments', 'related_articles']
    
    def validate(self, data):
        # Auto-generate slug from title
        if 'title' in data and not data.get('slug'):
            data['slug'] = slugify(data['title'])
        
        # Auto-generate excerpt if not provided
        if 'content' in data and not data.get('excerpt'):
            # Take first 150 characters as excerpt
            excerpt = data['content'][:150]
            if len(data['content']) > 150:
                excerpt += '...'
            data['excerpt'] = excerpt
        
        return data


class KnowledgeBaseSearchSerializer(serializers.Serializer):
    """Serializer for knowledge base search"""
    query = serializers.CharField(required=False)
    category = serializers.CharField(required=False)
    tags = serializers.ListField(child=serializers.CharField(), required=False)
    date_from = serializers.DateField(required=False)
    date_to = serializers.DateField(required=False)
    sort_by = serializers.ChoiceField(
        choices=['relevance', 'newest', 'oldest', 'popular'],
        default='relevance'
    )


class FAQSerializer(serializers.ModelSerializer):
    category_display = serializers.SerializerMethodField()
    helpful_percentage = serializers.SerializerMethodField()
    
    class Meta:
        model = FAQ
        fields = ['id', 'question', 'answer', 'category', 'category_display', 'subcategory',
                 'tags', 'display_order', 'view_count', 'helpful_yes', 'helpful_no',
                 'helpful_percentage', 'is_active', 'is_featured', 'created_at', 'updated_at']
        read_only_fields = ['view_count', 'helpful_yes', 'helpful_no', 'created_at', 'updated_at']
    
    def get_category_display(self, obj):
        # You might want to create a proper category model
        return obj.category
    
    def get_helpful_percentage(self, obj):
        return round(obj.helpful_percentage, 2)