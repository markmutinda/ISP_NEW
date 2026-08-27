from django.conf import settings
from django.db import transaction, connection
from django.shortcuts import get_object_or_404
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from django_tenants.utils import schema_context, get_public_schema_name

from apps.core.permissions import HasRoleAccessPolicy
from apps.core.models import Tenant, Company
from apps.billing.models.payment_models import (
    TenantTumaConfig,
    Payment,
    InvoiceItemPayment,
    StkCancellationTracker
)
from apps.customers.models import Customer
from apps.billing.serializers.tuma_serializers import TenantTumaConfigSerializer
from apps.billing.services.tuma_service import TumaClient, ensure_child_business

import logging
logger = logging.getLogger(__name__)


class TumaBanksView(APIView):
    """
    🚨 UPDATED: Returns banks from local BANK_PAYBILL_MAP instead of calling Tuma API.
    This removes the dependency on Tuma for bank listings.
    """
    permission_classes = [IsAuthenticated, HasRoleAccessPolicy]
    required_rbac_path = "/admin/payment-methods"

    def get(self, request):
        from apps.billing.constants.bank_paybills import BANK_PAYBILL_MAP
        data = [
            {"id": name, "code": name.upper().replace(" ", "_"), "name": name}
            for name in BANK_PAYBILL_MAP.keys()
        ]
        return Response({"success": True, "data": data})


class TumaCreateChildBusinessView(APIView):
    """
    ⚠️ DEPRECATED: This view is no longer used since we bypass Tuma.
    Kept for backward compatibility but will return an error.
    """
    permission_classes = [IsAuthenticated, HasRoleAccessPolicy]
    required_rbac_path = "/admin/payment-methods"

    def post(self, request):
        return Response(
            {"success": False, "error": "Tuma integration is deprecated. Please use Netily Paybill directly."},
            status=status.HTTP_410_GONE
        )


class TumaTenantModeView(APIView):
    """
    ⚠️ DEPRECATED: This view is no longer used since we bypass Tuma.
    Kept for backward compatibility but will return an error.
    """
    permission_classes = [IsAuthenticated, HasRoleAccessPolicy]
    required_rbac_path = "/admin/payment-methods"

    def _resolve_tenant_identity(self, schema_name):
        """
        Safely resolve tenant and company details without crashing if profile is missing.
        """
        with schema_context(get_public_schema_name()):
            tenant = Tenant.objects.get(schema_name=schema_name)
            try:
                # FIX: Safely try to get the company, catch the error if it doesn't exist
                company = tenant.company 
                name = company.name or tenant.schema_name
                email = company.email or f"{tenant.schema_name}@netily.co.ke"
                mobile = company.phone_number or "254700000000"
            except (Company.DoesNotExist, AttributeError):
                # FALLBACK: If no company profile, use the schema name
                name = tenant.schema_name
                email = f"{tenant.schema_name}@netily.co.ke"
                mobile = "254700000000"

        return tenant, name, email, mobile

    def get(self, request):
        return Response(
            {"success": False, "error": "Tuma integration is deprecated. Please use Netily Paybill directly."},
            status=status.HTTP_410_GONE
        )

    @transaction.atomic
    def put(self, request):
        return Response(
            {"success": False, "error": "Tuma integration is deprecated. Please use Netily Paybill directly."},
            status=status.HTTP_410_GONE
        )


class TumaInitiatePaymentView(APIView):
    """
    ⚠️ DEPRECATED: This view is no longer used since we bypass Tuma.
    Kept for backward compatibility but will return an error.
    """
    permission_classes = [IsAuthenticated]

    def _check_stk_cancellation_block(self, schema_name, phone_number):
        tracker = StkCancellationTracker.get_or_create_tracker(schema_name, phone_number)
        if tracker.is_currently_blocked() or tracker.consecutive_1032_count >= 3:
            return Response({
                "success": False,
                "error": "STK requests blocked due to multiple cancellations.",
                "detail": "Please contact support to unblock your number."
            }, status=429)
        return None

    @transaction.atomic
    def post(self, request):
        return Response(
            {"success": False, "error": "Tuma integration is deprecated. Please use Netily Paybill directly."},
            status=status.HTTP_410_GONE
        )