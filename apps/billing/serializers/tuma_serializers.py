# apps/billing/serializers/tuma_serializers.py
from rest_framework import serializers
from apps.billing.models.payment_models import TenantTumaConfig

class TenantTumaConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model = TenantTumaConfig
        fields = [
            "active_mode", "till_number",
            "bank_id", "bank_name", "bank_account_number"
        ]

    def validate(self, data):
        mode = data.get("active_mode")
        till = (data.get("till_number") or "").strip()
        bank_id = (data.get("bank_id") or "").strip()
        bank_acc = (data.get("bank_account_number") or "").strip()

        # Enforce exact exclusivity requested
        if mode == "TILL":
            if not till:
                raise serializers.ValidationError({"till_number": "Required for TILL mode"})
            data["bank_id"] = ""
            data["bank_name"] = ""
            data["bank_account_number"] = ""
        elif mode == "BANK":
            if not bank_id or not bank_acc:
                raise serializers.ValidationError({"bank_account_number": "Required for BANK mode"})
            data["till_number"] = ""
        else:
            raise serializers.ValidationError({"active_mode": "Must be TILL or BANK"})
        return data