# apps/analytics/views.py
from datetime import datetime, timedelta
from django.utils import timezone
from django.db.models import Q, Count, Sum
from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.http import HttpResponse
import json
import csv

# Try to import optional dependencies
try:
    import xlwt
    XLSX_AVAILABLE = True
except ImportError:
    XLSX_AVAILABLE = False

from .models import ReportDefinition, DashboardWidget, AnalyticsCache
from .reports import financial_reports, network_reports, customer_reports
from .serializers import ReportDefinitionSerializer, DashboardWidgetSerializer

# Correct import from your custom permissions
from apps.core.permissions import IsAdminOrStaff


class ReportGeneratorView(generics.ListCreateAPIView):
    """
    Generate and manage reports
    """
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    serializer_class = ReportDefinitionSerializer
    
    def get_queryset(self):
        return ReportDefinition.objects.filter(is_active=True)
    
    def post(self, request, *args, **kwargs):
        """
        Generate a custom report
        """
        report_type = request.data.get('report_type')
        start_date = request.data.get('start_date')
        end_date = request.data.get('end_date')
        format = request.data.get('format', 'json')
        
        if not all([report_type, start_date, end_date]):
            return Response(
                {'error': 'Missing required parameters'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            start_date = timezone.make_aware(datetime.strptime(start_date, '%Y-%m-%d'))
            end_date = timezone.make_aware(datetime.strptime(end_date, '%Y-%m-%d') + timedelta(days=1))  # inclusive end
        except ValueError:
            return Response(
                {'error': 'Invalid date format. Use YYYY-MM-DD'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Generate report based on type
        company = request.user.company if hasattr(request.user, 'company') else None
        
        if report_type == 'financial':
            report_data = financial_reports.FinancialReports.revenue_report(
                start_date, end_date, company
            )
        elif report_type == 'collection':
            report_data = financial_reports.FinancialReports.collection_report(
                start_date, end_date, company
            )
        elif report_type == 'arpu':
            report_data = financial_reports.FinancialReports.arpu_report(
                start_date, end_date, company
            )
        elif report_type == 'uptime':
            report_data = network_reports.NetworkReports.uptime_report(
                start_date, end_date
            )
        elif report_type == 'bandwidth':
            report_data = network_reports.NetworkReports.bandwidth_report(
                start_date, end_date
            )
        elif report_type == 'acquisition':
            report_data = customer_reports.CustomerReports.acquisition_report(
                start_date, end_date
            )
        elif report_type == 'churn':
            report_data = customer_reports.CustomerReports.churn_report(
                start_date, end_date
            )
        elif report_type == 'satisfaction':
            report_data = customer_reports.CustomerReports.satisfaction_report(
                start_date, end_date
            )
        else:
            return Response(
                {'error': 'Invalid report type'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Handle different formats
        if format == 'csv':
            return self._generate_csv(report_data, report_type)
        elif format == 'excel' and XLSX_AVAILABLE:
            return self._generate_excel(report_data, report_type)
        elif format == 'pdf':
            return self._generate_pdf(report_data, report_type)
        else:
            return Response(report_data)
    
    def _generate_csv(self, data, report_name):
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="{report_name}_{timezone.now().date()}.csv"'
        
        writer = csv.writer(response)
        
        if 'summary' in data:
            writer.writerow(['Metric', 'Value'])
            for key, value in data['summary'].items():
                writer.writerow([key.replace('_', ' ').title(), value])
            writer.writerow([])  # empty row for separation
        
        if 'details' in data and data['details']:
            headers = data['details'][0].keys()
            writer.writerow(headers)
            for row in data['details']:
                writer.writerow(row.values())
        
        return response
    
    def _generate_excel(self, data, report_name):
        if not XLSX_AVAILABLE:
            return Response(
                {'error': 'Excel export requires xlwt library. Please install it.'},
                status=status.HTTP_501_NOT_IMPLEMENTED
            )
        
        response = HttpResponse(content_type='application/ms-excel')
        response['Content-Disposition'] = f'attachment; filename="{report_name}_{timezone.now().date()}.xls"'
        
        wb = xlwt.Workbook(encoding='utf-8')
        ws = wb.add_sheet('Report')
        
        row_num = 0
        bold_style = xlwt.XFStyle()
        bold_style.font.bold = True
        
        if 'summary' in data:
            ws.write(row_num, 0, 'Metric', bold_style)
            ws.write(row_num, 1, 'Value', bold_style)
            row_num += 1
            for key, value in data['summary'].items():
                ws.write(row_num, 0, key.replace('_', ' ').title())
                ws.write(row_num, 1, value)
                row_num += 1
            row_num += 1  # spacing
        
        wb.save(response)
        return response
    
    def _generate_pdf(self, data, report_name):
        return Response(
            {'message': 'PDF export coming soon', 'data': data},
            status=status.HTTP_501_NOT_IMPLEMENTED
        )


class DashboardView(APIView):
    """
    Get dashboard widgets and data
    """
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    
    def get(self, request):
        widgets = DashboardWidget.objects.filter(is_visible=True).order_by('position')
        
        dashboard_data = []
        for widget in widgets:
            widget_data = self._get_widget_data(widget, request)
            dashboard_data.append({
                'id': widget.id,
                'name': widget.name,
                'type': widget.widget_type,
                'chart_type': widget.chart_type,
                'data': widget_data,
                'size': widget.size,
                'refresh_interval': widget.refresh_interval,
                'config': widget.config,
            })
        
        overall_metrics = self._get_overall_metrics(request)
        
        return Response({
            'widgets': dashboard_data,
            'metrics': overall_metrics,
            'last_updated': timezone.now(),
        })
    
    def _get_widget_data(self, widget, request):
        cache_key = f"widget_{widget.id}_{timezone.now().date()}"
        cache_entry = AnalyticsCache.objects.filter(
            cache_key=cache_key,
            expires_at__gt=timezone.now()
        ).first()
        
        if cache_entry:
            return cache_entry.data
        
        company = request.user.company if hasattr(request.user, 'company') else None
        
        if widget.data_source == 'revenue_trend':
            data = financial_reports.FinancialReports.revenue_report(
                timezone.now() - timedelta(days=30), timezone.now(), company
            )
        elif widget.data_source == 'customer_growth':
            data = customer_reports.CustomerReports.acquisition_report(
                timezone.now() - timedelta(days=90), timezone.now()
            )
        elif widget.data_source == 'network_health':
            data = network_reports.NetworkReports.uptime_report(
                timezone.now() - timedelta(days=7), timezone.now()
            )
        elif widget.data_source == 'ticket_status':
            from apps.support.models import Ticket
            data = list(Ticket.objects.values('status').annotate(count=Count('id')))
        elif widget.data_source == 'top_plans':
            from apps.billing.models import Plan
            data = list(Plan.objects.annotate(
                subscriber_count=Count('customer')
            ).order_by('-subscriber_count')[:5].values('name', 'price', 'subscriber_count'))
        else:
            data = {'message': 'Widget data source not implemented'}
        
        AnalyticsCache.objects.create(
            cache_key=cache_key,
            data=data,
            expires_at=timezone.now() + timedelta(minutes=max(widget.refresh_interval // 60, 5))
        )
        
        return data
    
    def _get_overall_metrics(self, request):
        from apps.customers.models import Customer
        from apps.billing.models import Invoice, Payment
        from apps.support.models import Ticket
        
        today = timezone.now().date()
        month_start = today.replace(day=1)
        company = request.user.company if hasattr(request.user, 'company') else None
        
        company_filter = Q(company=company) if company else Q()
        
        return {
            'total_customers': Customer.objects.filter(company_filter).count(),
            'active_customers': Customer.objects.filter(company_filter, status='active').count(),
            'monthly_revenue': Invoice.objects.filter(
                invoice_date__gte=month_start,
                customer__company=company
            ).aggregate(total=Sum('total_amount'))['total'] or 0,
            'collections_today': Payment.objects.filter(
                payment_date=today,
                invoice__customer__company=company
            ).aggregate(total=Sum('amount'))['total'] or 0,
            'open_tickets': Ticket.objects.filter(status__in=['open', 'in_progress']).count(),
            'network_uptime': 99.8,
        }


class ExportView(APIView):
    """
    Export data in various formats
    """
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    
    def post(self, request):
        export_type = request.data.get('type')
        filters = request.data.get('filters', {})
        company = request.user.company if hasattr(request.user, 'company') else None
        
        if export_type == 'customers':
            return self._export_customers(filters, company)
        elif export_type == 'invoices':
            return self._export_invoices(filters, company)
        elif export_type == 'payments':
            return self._export_payments(filters, company)
        else:
            return Response(
                {'error': 'Invalid export type'},
                status=status.HTTP_400_BAD_REQUEST
            )
    
    def _export_customers(self, filters, company):
        from apps.customers.models import Customer
        from apps.customers.serializers import CustomerSerializer
        
        queryset = Customer.objects.filter(company=company)
        
        if 'status' in filters:
            queryset = queryset.filter(status=filters['status'])
        if 'plan' in filters:
            queryset = queryset.filter(plan__name__icontains=filters['plan'])
        if 'date_from' in filters:
            queryset = queryset.filter(created_at__gte=filters['date_from'])
        if 'date_to' in filters:
            queryset = queryset.filter(created_at__lte=filters['date_to'])
        
        serializer = CustomerSerializer(queryset, many=True)
        return self._generate_csv_response(serializer.data, 'customers')
    
    def _export_invoices(self, filters, company):
        from apps.billing.models import Invoice
        from apps.billing.serializers import InvoiceSerializer
        
        queryset = Invoice.objects.filter(customer__company=company)
        
        if 'status' in filters:
            queryset = queryset.filter(status=filters['status'])
        if 'date_from' in filters:
            queryset = queryset.filter(invoice_date__gte=filters['date_from'])
        if 'date_to' in filters:
            queryset = queryset.filter(invoice_date__lte=filters['date_to'])
        
        serializer = InvoiceSerializer(queryset, many=True)
        return self._generate_csv_response(serializer.data, 'invoices')
    
    def _generate_csv_response(self, data, filename_prefix):
        if not data:
            return Response(
                {'error': 'No data to export'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="{filename_prefix}_{timezone.now().date()}.csv"'
        
        writer = csv.writer(response)
        headers = data[0].keys()
        writer.writerow(headers)
        
        for row in data:
            writer.writerow(row.values())
        
        return response