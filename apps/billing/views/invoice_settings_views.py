"""
Invoice Settings Views
"""
import logging
from django.db import connection
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated

logger = logging.getLogger(__name__)


class InvoiceSettingsView(APIView):
    """GET/PATCH invoice auto-generation settings for current tenant"""
    permission_classes = [IsAuthenticated]

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


class CustomerSearchView(APIView):
    """Efficient customer search for invoice creation (first 5, then searchable)"""
    permission_classes = [IsAuthenticated]

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
    """Generate PDF for an invoice"""
    permission_classes = [IsAuthenticated]

    def get(self, request, invoice_id):
        from apps.billing.models.billing_models import Invoice
        from django.http import HttpResponse
        import io

        try:
            invoice = Invoice.objects.select_related(
                'customer__user', 'plan'
            ).prefetch_related('items').get(id=invoice_id)
        except Invoice.DoesNotExist:
            return Response({'error': 'Invoice not found'}, status=404)

        try:
            pdf_bytes = self._generate_pdf(invoice, request)
            response = HttpResponse(pdf_bytes, content_type='application/pdf')
            response['Content-Disposition'] = f'attachment; filename="invoice-{invoice.invoice_number}.pdf"'
            return response
        except Exception as e:
            logger.error(f"PDF generation failed for invoice {invoice_id}: {e}")
            return Response({'error': 'PDF generation failed'}, status=500)

    def _generate_pdf(self, invoice, request):
        """Generate PDF using reportlab (fallback: HTML bytes)"""
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.lib import colors
            from reportlab.lib.units import cm
            from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.lib.enums import TA_RIGHT, TA_CENTER
            import io

            buffer = io.BytesIO()
            doc = SimpleDocTemplate(buffer, pagesize=A4,
                                    rightMargin=2*cm, leftMargin=2*cm,
                                    topMargin=2*cm, bottomMargin=2*cm)

            styles = getSampleStyleSheet()
            story = []

            # Company name (try branding)
            company_name = "ISP Management"
            try:
                from apps.core.models import Company
                company = Company.objects.first()
                if company:
                    company_name = company.name
            except Exception:
                pass

            # Header
            header_style = ParagraphStyle('header', fontSize=20, spaceAfter=6, fontName='Helvetica-Bold')
            story.append(Paragraph(company_name, header_style))
            story.append(Paragraph("INVOICE", ParagraphStyle('inv', fontSize=14, textColor=colors.HexColor('#2563eb'), spaceAfter=20)))
            story.append(Spacer(1, 0.3*cm))

            # Invoice meta
            meta_data = [
                ['Invoice Number:', invoice.invoice_number or str(invoice.id)],
                ['Date:', str(invoice.billing_date or '')],
                ['Due Date:', str(invoice.due_date or '')],
                ['Status:', invoice.status.upper()],
            ]
            meta_table = Table(meta_data, colWidths=[4*cm, 8*cm])
            meta_table.setStyle(TableStyle([
                ('FONTNAME', (0, 0), (0, -1), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ]))
            story.append(meta_table)
            story.append(Spacer(1, 0.5*cm))

            # Bill To
            story.append(Paragraph("Bill To:", ParagraphStyle('bt', fontName='Helvetica-Bold', fontSize=11, spaceAfter=4)))
            customer = invoice.customer
            if customer:
                story.append(Paragraph(customer.user.get_full_name(), styles['Normal']))
                story.append(Paragraph(customer.user.phone_number or '', styles['Normal']))
                story.append(Paragraph(customer.user.email or '', styles['Normal']))
            story.append(Spacer(1, 0.5*cm))

            # Items table
            items_header = [['Description', 'Qty', 'Unit Price', 'Total']]
            items_data = items_header
            for item in invoice.items.all():
                items_data.append([
                    str(item.description),
                    str(item.quantity),
                    f"KES {item.unit_price:,.2f}",
                    f"KES {item.total:,.2f}",
                ])

            items_table = Table(items_data, colWidths=[9*cm, 2*cm, 3.5*cm, 3.5*cm])
            items_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2563eb')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('ALIGN', (1, 0), (-1, -1), 'RIGHT'),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f8fafc')]),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
            ]))
            story.append(items_table)
            story.append(Spacer(1, 0.4*cm))

            # Totals
            totals_data = [
                ['Subtotal:', f"KES {invoice.subtotal or invoice.total_amount:,.2f}"],
            ]
            if invoice.tax_amount and float(invoice.tax_amount) > 0:
                totals_data.append(['Tax:', f"KES {invoice.tax_amount:,.2f}"])
            if invoice.discount_amount and float(invoice.discount_amount) > 0:
                totals_data.append(['Discount:', f"-KES {invoice.discount_amount:,.2f}"])
            totals_data.append(['TOTAL:', f"KES {invoice.total_amount:,.2f}"])
            if invoice.amount_paid and float(invoice.amount_paid) > 0:
                totals_data.append(['Amount Paid:', f"KES {invoice.amount_paid:,.2f}"])
                balance = float(invoice.total_amount) - float(invoice.amount_paid)
                totals_data.append(['Balance Due:', f"KES {balance:,.2f}"])

            totals_table = Table(totals_data, colWidths=[13*cm, 5*cm])
            totals_table.setStyle(TableStyle([
                ('ALIGN', (1, 0), (1, -1), 'RIGHT'),
                ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, -1), 10),
                ('LINEABOVE', (0, -1), (-1, -1), 1, colors.black),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ]))
            story.append(totals_table)

            if invoice.notes:
                story.append(Spacer(1, 0.5*cm))
                story.append(Paragraph("Notes:", ParagraphStyle('notes_h', fontName='Helvetica-Bold', fontSize=10)))
                story.append(Paragraph(invoice.notes, styles['Normal']))

            doc.build(story)
            return buffer.getvalue()

        except ImportError:
            # Fallback: return simple text as bytes if reportlab not installed
            content = f"Invoice {invoice.invoice_number}\nCustomer: {invoice.customer.user.get_full_name() if invoice.customer else 'N/A'}\nTotal: KES {invoice.total_amount}"
            return content.encode('utf-8')