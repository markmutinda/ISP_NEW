"""
Serializers for CustomerAddress model
"""
from rest_framework import serializers
from apps.customers.models import CustomerAddress


class CustomerAddressSerializer(serializers.ModelSerializer):
    """Serializer for customer addresses"""
    
    class Meta:
        model = CustomerAddress
        fields = [
            'id', 'address_type', 'is_primary',
            'building_name', 'floor', 'room', 'street_address', 'landmark',
            'county', 'sub_county', 'ward', 'estate',
            'contact_person', 'contact_phone',
            'latitude', 'longitude', 'installation_notes',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']


class CustomerAddressCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating customer addresses"""
    
    class Meta:
        model = CustomerAddress
        fields = [
            'address_type', 'is_primary',
            'building_name', 'floor', 'room', 'street_address', 'landmark',
            'county', 'sub_county', 'ward', 'estate',
            'contact_person', 'contact_phone',
            'latitude', 'longitude', 'installation_notes'
        ]