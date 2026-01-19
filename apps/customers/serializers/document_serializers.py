"""
Serializers for CustomerDocument model
"""
from rest_framework import serializers
from apps.customers.models import CustomerDocument


class CustomerDocumentSerializer(serializers.ModelSerializer):
    """Serializer for customer documents"""
    
    class Meta:
        model = CustomerDocument
        fields = [
            'id', 'document_type', 'title', 'description',
            'document_file', 'file_size', 'mime_type',
            'verified', 'verified_by', 'verified_at', 'verification_notes',
            'expiry_date', 'is_expired',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'file_size', 'mime_type', 'verified_by', 'verified_at', 
            'is_expired', 'created_at', 'updated_at'
        ]


class DocumentUploadSerializer(serializers.ModelSerializer):
    """Serializer for uploading documents"""
    
    class Meta:
        model = CustomerDocument
        fields = [
            'document_type', 'title', 'description',
            'document_file', 'expiry_date'
        ]
    
    def validate_document_file(self, value):
        # Validate file size (5MB max)
        max_size = 5 * 1024 * 1024  # 5MB
        if value.size > max_size:
            raise serializers.ValidationError(
                f'File size must be less than 5MB. Current size: {value.size / 1024 / 1024:.1f}MB'
            )
        
        # Validate file type
        allowed_types = [
            'application/pdf',
            'image/jpeg',
            'image/png',
            'image/jpg',
        ]
        if value.content_type not in allowed_types:
            raise serializers.ValidationError(
                'Only PDF, JPEG, and PNG files are allowed'
            )
        
        return value
