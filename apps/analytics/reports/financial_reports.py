from datetime import datetime, timedelta
from decimal import Decimal
from django.db.models import Sum, Count, Avg, Q
from django.utils import timezone
from apps.billing.models import Invoice, Payment, Plan
from apps.customers.models import Customer
from utils.constants import INVOICE_STATUS, PAYMENT_STATUS


class FinancialReports:
    @staticmethod
    def revenue_report(start_date, end_date, company=None):
        """
        Generate revenue report for specified period
        """
        filters = Q(invoice_date__gte=start_date, invoice_date__lte=end_date)
        if company:
            filters &= Q(customer__company=company)
        
        invoices = Invoice.objects.filter(filters)
        
        total_invoices = invoices.count()
        total_amount = invoices.aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
        total_paid = invoices.filter(status='paid').aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
        total_due = invoices.filter(status='pending').aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
        
        # Revenue by plan
        revenue_by_plan = invoices.values('plan__name').annotate(
            total=Sum('total_amount'),
            count=Count('id')
        ).order_by('-total')
        
        # Daily revenue trend
        daily_revenue = invoices.values('invoice_date').annotate(
            daily_total=Sum('total_amount')
        ).order_by('invoice_date')
        
        return {
            'period': {'start': start_date, 'end': end_date},
            'summary': {
                'total_invoices': total_invoices,
                'total_revenue': float(total_amount),
                'total_collected': float(total_paid),
                'total_outstanding': float(total_due),
                'collection_rate': float((total_paid / total_amount * 100) if total_amount > 0 else 0),
            },
            'revenue_by_plan': list(revenue_by_plan),
            'daily_trend': list(daily_revenue),
        }
    
    @staticmethod
    def collection_report(start_date, end_date, company=None):
        """
        Generate collection efficiency report
        """
        payments = Payment.objects.filter(
            payment_date__gte=start_date,
            payment_date__lte=end_date
        )
        
        if company:
            payments = payments.filter(invoice__customer__company=company)
        
        # Payment methods breakdown
        payment_methods = payments.values('payment_method').annotate(
            total=Sum('amount'),
            count=Count('id')
        ).order_by('-total')
        
        # Collections by day
        collections_by_day = payments.values('payment_date').annotate(
            daily_total=Sum('amount')
        ).order_by('payment_date')
        
        # Aging analysis
        aging_filters = Q(invoice_date__gte=start_date, invoice_date__lte=end_date)
        if company:
            aging_filters &= Q(customer__company=company)
        
        invoices_aging = Invoice.objects.filter(aging_filters)
        
        aging_report = {
            'current': invoices_aging.filter(due_date__gte=timezone.now()).aggregate(total=Sum('total_amount'))['total'] or Decimal('0'),
            '1_30': invoices_aging.filter(
                due_date__lt=timezone.now(),
                due_date__gte=timezone.now() - timedelta(days=30)
            ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0'),
            '31_60': invoices_aging.filter(
                due_date__lt=timezone.now() - timedelta(days=30),
                due_date__gte=timezone.now() - timedelta(days=60)
            ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0'),
            '61_90': invoices_aging.filter(
                due_date__lt=timezone.now() - timedelta(days=60),
                due_date__gte=timezone.now() - timedelta(days=90)
            ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0'),
            'over_90': invoices_aging.filter(
                due_date__lt=timezone.now() - timedelta(days=90)
            ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0'),
        }
        
        return {
            'period': {'start': start_date, 'end': end_date},
            'payment_methods': list(payment_methods),
            'daily_collections': list(collections_by_day),
            'aging_analysis': {k: float(v) for k, v in aging_report.items()},
            'total_collected': float(payments.aggregate(total=Sum('amount'))['total'] or Decimal('0')),
        }
    
    @staticmethod
    def arpu_report(start_date, end_date, company=None):
        """
        Calculate Average Revenue Per User (ARPU)
        """
        filters = Q(created_at__gte=start_date, created_at__lte=end_date)
        if company:
            filters &= Q(company=company)
        
        active_customers = Customer.objects.filter(
            filters,
            status='active'
        ).count()
        
        total_revenue = Invoice.objects.filter(
            invoice_date__gte=start_date,
            invoice_date__lte=end_date
        ).aggregate(total=Sum('total_amount'))['total'] or Decimal('0')
        
        arpu = float(total_revenue / active_customers) if active_customers > 0 else 0
        
        # ARPU by plan
        arpu_by_plan = Plan.objects.annotate(
            total_revenue=Sum('invoice__total_amount'),
            customer_count=Count('invoice__customer', distinct=True)
        ).filter(total_revenue__gt=0).values('name', 'price').annotate(
            arpu=Avg('invoice__total_amount')
        )
        
        return {
            'period': {'start': start_date, 'end': end_date},
            'active_customers': active_customers,
            'total_revenue': float(total_revenue),
            'arpu': arpu,
            'arpu_by_plan': list(arpu_by_plan),
        }
