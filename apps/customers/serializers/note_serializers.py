"""
Serializers for CustomerNotes model
"""
from rest_framework import serializers
from apps.customers.models import CustomerNotes


class CustomerNotesSerializer(serializers.ModelSerializer):
    """Serializer for customer notes"""
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    
    class Meta:
        model = CustomerNotes
        fields = [
            'id', 'note_type', 'note', 'priority',
            'requires_followup', 'followup_date', 'followup_completed',
            'internal_only', 'attachment',
            'created_by', 'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['created_by', 'created_at', 'updated_at']
