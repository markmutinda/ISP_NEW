"""
Serializers for NextOfKin model
"""
from rest_framework import serializers
from apps.customers.models import NextOfKin


class NextOfKinSerializer(serializers.ModelSerializer):
    """Serializer for next of kin"""
    
    class Meta:
        model = NextOfKin
        fields = [
            'id', 'full_name', 'relationship', 'phone_number', 'email',
            'id_type', 'id_number', 'address', 'county',
            'is_primary_contact', 'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']