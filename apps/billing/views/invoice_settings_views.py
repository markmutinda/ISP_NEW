"""
Invoice Settings Views
"""
import logging
from django.db import connection
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

from apps.core.permissions import HasRoleAccessPolicy

logger = logging.getLogger(__name__)


class InvoiceSettingsView(APIView):
    """GET/PATCH invoice auto-generation settings for current tenant"""
    permission_classes = [IsAuthenticated, HasRoleAccessPolicy]
    required_rbac_path = "/admin/settings/billing"

    def get(self, request):
        from apps.billing.models.billing_models import InvoiceSettings
        settings = InvoiceSettings.get_settings()
        return Response({
            'auto_generate_enabled': settings.auto_generate_enabled,
            'days_before_expiry': settings.days_before_expiry,
        })

    def patch(self, request):
        from apps.billing.models.billing_models import InvoiceSettings
        settings = InvoiceSettings.get_settings()
        if 'auto_generate_enabled' in request.data:
            settings.auto_generate_enabled = bool(request.data['auto_generate_enabled'])
        if 'days_before_expiry' in request.data:
            settings.days_before_expiry = int(request.data['days_before_expiry'])
        settings.save()
        return Response({
            'auto_generate_enabled': settings.auto_generate_enabled,
            'days_before_expiry': settings.days_before_expiry,
        })


class HotspotPruneSettingsView(APIView):
    """GET/PATCH hotspot client auto-pruning settings for current tenant"""
    permission_classes = [IsAuthenticated, HasRoleAccessPolicy]
    required_rbac_path = "/admin/settings/billing"

    def get(self, request):
        from apps.billing.models.hotspot_models import HotspotPruneSettings
        s = HotspotPruneSettings.get_settings()
        return Response({
            'prune_window_days': s.prune_window_days,
            'is_enabled': s.is_enabled,
            'last_pruned_at': s.last_pruned_at,
            'choices': HotspotPruneSettings.PRUNE_WINDOW_CHOICES,
        })

    def patch(self, request):
        from apps.billing.models.hotspot_models import HotspotPruneSettings
        s = HotspotPruneSettings.get_settings()

        if 'prune_window_days' in request.data:
            try:
                value = int(request.data['prune_window_days'])
            except (TypeError, ValueError):
                return Response({'error': 'prune_window_days must be an integer'}, status=400)
            valid = [c[0] for c in HotspotPruneSettings.PRUNE_WINDOW_CHOICES]
            if value not in valid:
                return Response({'error': f'prune_window_days must be one of {valid}'}, status=400)
            s.prune_window_days = value

        if 'is_enabled' in request.data:
            s.is_enabled = bool(request.data['is_enabled'])

        s.save()
        return Response({
            'prune_window_days': s.prune_window_days,
            'is_enabled': s.is_enabled,
            'last_pruned_at': s.last_pruned_at,
        })


class CustomerSearchView(APIView):
    """Efficient customer search for invoice creation (first 5, then searchable)"""
    permission_classes = [IsAuthenticated, HasRoleAccessPolicy]
    required_rbac_path = "/admin/invoices"

    def get(self, request):
        from apps.customers.models import Customer
        from django.db.models import Q

        q = request.query_params.get('q', '').strip()
        limit = min(int(request.query_params.get('limit', 5)), 50)

        qs = Customer.objects.select_related('user').filter(
            status__in=['ACTIVE', 'PENDING']
        )

        if q:
            qs = qs.filter(
                Q(user__first_name__icontains=q) |
                Q(user__last_name__icontains=q) |
                Q(user__phone_number__icontains=q) |
                Q(customer_code__icontains=q)
            )

        qs = qs.order_by('user__first_name')[:limit]

        results = []
        for c in qs:
            results.append({
                'id': c.id,
                'customer_code': c.customer_code,
                'full_name': c.user.get_full_name(),
                'phone_number': c.user.phone_number or '',
                'email': c.user.email or '',
            })

        return Response({'results': results, 'count': len(results)})


class InvoicePDFView(APIView):
    """Branded invoice PDF (tenant logo + company details)"""
    permission_classes = [IsAuthenticated, HasRoleAccessPolicy]
    required_rbac_path = "/admin/invoices"

    def get(self, request, invoice_id):
        from apps.billing.models.billing_models import Invoice
        from apps.billing.services.billing_pdfs import build_invoice_pdf
        from apps.core.pdf_reports import pdf_response, safe_filename

        try:
            invoice = (
                Invoice.objects
                .select_related('customer__user', 'plan', 'service_connection')
                .prefetch_related('items')
                .get(id=invoice_id)
            )
        except Invoice.DoesNotExist:
            return Response({'error': 'Invoice not found'}, status=404)

        try:
            pdf_bytes = build_invoice_pdf(invoice, request)
        except Exception:
            logger.exception(f"PDF generation failed for invoice {invoice_id}")
            return Response({'error': 'PDF generation failed'}, status=500)

        return pdf_response(pdf_bytes, f"invoice-{safe_filename(invoice.invoice_number or invoice.id)}.pdf")