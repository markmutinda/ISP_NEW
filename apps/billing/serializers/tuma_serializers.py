# apps/billing/serializers/tuma_serializers.py
from rest_framework import serializers
from apps.billing.models.payment_models import TenantTumaConfig


class TenantTumaConfigSerializer(serializers.ModelSerializer):
    """
    Simplified serializer for Tuma tenant configuration.
    Frontend only needs to submit dropdown selection + account number.
    
    Expected input from frontend:
    {
        "collection_reference_id": "bank_id_123",  # Selected from dropdown
        "collection_account_number": "1234567890"  # User's account/till number
    }
    
    The backend will:
    1. Look up the reference details from Tuma reference list
    2. Auto-populate reference_name and reference_code
    3. Determine active_mode based on reference type
    4. Sync deprecated fields for backward compatibility
    """
    
    class Meta:
        model = TenantTumaConfig
        fields = [
            "collection_reference_id",
            "collection_account_number",
        ]
    
    def validate_collection_reference_id(self, value):
        """Validate that the reference ID is provided and not empty."""
        if not value or not value.strip():
            raise serializers.ValidationError("Payment method selection is required")
        return value.strip()
    
    def validate_collection_account_number(self, value):
        """Validate that the account number is provided and not empty."""
        if not value or not value.strip():
            raise serializers.ValidationError("Account number is required")
        
        # Basic format validation (can be adjusted based on requirements)
        if not value.strip().isdigit():
            raise serializers.ValidationError("Account number must contain only digits")
        
        return value.strip()
    
    def validate(self, data):
        """
        Validate that both required fields are present.
        The actual reference lookup and mode determination happens in the view.
        """
        ref = data.get("collection_reference_id", "").strip()
        acc = data.get("collection_account_number", "").strip()
        
        if not ref:
            raise serializers.ValidationError({
                "collection_reference_id": "Payment method selection is required"
            })
        
        if not acc:
            raise serializers.ValidationError({
                "collection_account_number": "Account number is required"
            })
        
        return data
    
    def to_representation(self, instance):
        """
        Customize the output representation to be frontend-friendly.
        Returns a simplified view of the configuration.
        """
        representation = super().to_representation(instance)
        
        # Add computed display fields for frontend
        representation["active_mode"] = instance.active_mode
        representation["display_name"] = instance.collection_reference_name or ""
        representation["display_code"] = instance.collection_reference_code or ""
        representation["formatted_display"] = instance.get_collection_display() if instance.active_mode else None
        
        # Include backward compatibility fields for existing frontend code
        if instance.active_mode == "TILL":
            representation["till_number"] = instance.collection_account_number
        elif instance.active_mode == "BANK":
            representation["bank_id"] = instance.collection_reference_id
            representation["bank_name"] = instance.collection_reference_name
            representation["bank_account_number"] = instance.collection_account_number
        
        return representation


# Optional: Full serializer for admin/internal use (includes all fields)
class TenantTumaConfigFullSerializer(serializers.ModelSerializer):
    """
    Full serializer for admin/internal use.
    Includes all fields for complete configuration management.
    """
    
    class Meta:
        model = TenantTumaConfig
        fields = [
            "id",
            "schema_name",
            "tenant",
            "tuma_business_id",
            "tuma_business_email",
            "tuma_business_api_key",
            "active_mode",
            "collection_reference_id",
            "collection_reference_code",
            "collection_reference_name",
            "collection_account_number",
            "till_number",  # deprecated
            "bank_id",  # deprecated
            "bank_name",  # deprecated
            "bank_account_number",  # deprecated
            "is_active",
            "updated_at",
        ]
        read_only_fields = ["id", "schema_name", "updated_at"]
    
    def validate(self, data):
        """Validate based on active_mode."""
        mode = data.get("active_mode")
        
        if mode == "TILL":
            if not data.get("collection_account_number"):
                raise serializers.ValidationError({
                    "collection_account_number": "Account number is required for TILL mode"
                })
        elif mode == "BANK":
            if not data.get("collection_reference_id"):
                raise serializers.ValidationError({
                    "collection_reference_id": "Bank selection is required for BANK mode"
                })
            if not data.get("collection_account_number"):
                raise serializers.ValidationError({
                    "collection_account_number": "Account number is required for BANK mode"
                })
        
        return data
    
    def to_representation(self, instance):
        """Add computed display fields."""
        representation = super().to_representation(instance)
        representation["display_info"] = instance.get_collection_display()
        return representation