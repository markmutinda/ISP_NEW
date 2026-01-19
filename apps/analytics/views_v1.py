"""
Analytics API v1 - Frontend compatible endpoints
"""
from datetime import datetime, timedelta
from dateutil.relativedelta import relativedelta
from apps.customers.models import ServiceConnection
from django.utils import timezone

from django.db.models import (
    Sum, Count, Avg, Max, Min, Q, F,
    Func, Value, CharField, IntegerField, FloatField
)
from django.db.models.functions import (
    TruncMonth, TruncWeek, TruncDay,
    Coalesce, Cast, Extract
)
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from django.http import HttpResponse
import csv
import json
from io import StringIO

# Import your models
from rest_framework.permissions import IsAdminUser
from apps.customers.models import Customer
from apps.billing.models.billing_models import Invoice, Plan
from apps.billing.models.payment_models import Payment
from apps.network.models import OLTDevice, CPEDevice, MikrotikQueue  
from apps.bandwidth.models import DataUsage, BandwidthProfile

# Try to import Router model, fallback to OLTDevice
try:
    from apps.network.models import Router
except ImportError:
    Router = OLTDevice  # Fallback


class AnalyticsDashboardView(APIView):
    """
    GET /api/v1/analytics/dashboard/
    Returns complete dashboard data in one request
    """
    permission_classes = [IsAdminUser]
    
    def get_date_range(self, time_range):
        """Convert time_range parameter to date range"""
        now = timezone.now()
        
        if time_range == '7d':
            start_date = now - timedelta(days=7)
        elif time_range == '30d':
            start_date = now - timedelta(days=30)
        elif time_range == '90d':
            start_date = now - timedelta(days=90)
        elif time_range == '12m':
            start_date = now - relativedelta(months=12)
        elif time_range == 'ytd':
            start_date = datetime(now.year, 1, 1)
            start_date = timezone.make_aware(start_date)
        else:
            start_date = now - timedelta(days=30)  # Default
        
        return start_date
    
    def get(self, request):
        time_range = request.query_params.get('time_range', '30d')
        start_date = self.get_date_range(time_range)
        
        # Build comprehensive analytics response
        data = {
            'kpis': self.get_kpis(start_date),
            'revenue_data': self.get_revenue_data(start_date),
            'user_growth_data': self.get_user_growth_data(start_date),
            'plan_performance': self.get_plan_performance(start_date),
            'location_analytics': self.get_location_analytics(start_date),
            'router_analytics': self.get_router_analytics(),
            'payment_methods': self.get_payment_methods(start_date),
            'payment_stats': self.get_payment_stats(start_date),
            'user_distribution': self.get_user_distribution(),
            'revenue_by_type': self.get_revenue_by_type(start_date),
            'revenue_forecast': self.get_revenue_forecast(),
            'revenue_target': self.get_revenue_target(),
            'network_stats': self.get_network_stats(),
            'time_range': time_range,
            'timestamp': timezone.now().isoformat(),
        }
        
        return Response(data)
    
    def get_kpis(self, start_date):
        """Get key performance indicators"""
        # Get payments in period
        payments = Payment.objects.filter(
            payment_date__gte=start_date,
            status='completed'
        )
        
        # Get customer stats
        total_customers = Customer.objects.filter(status='active').count()
        new_customers = Customer.objects.filter(
            created_at__gte=start_date,
            status='active'
        ).count()
        
        churned_customers = Customer.objects.filter(
            status='terminated',
            deactivation_date__gte=start_date
        ).count()
        
        # Calculate metrics
        total_revenue = payments.aggregate(total=Sum('amount'))['total'] or 0
        arpu = total_revenue / total_customers if total_customers > 0 else 0
        churn_rate = (churned_customers / total_customers * 100) if total_customers > 0 else 0
        
        # Previous period for comparison
        prev_start_date = start_date - (timezone.now() - start_date)
        prev_payments = Payment.objects.filter(
            payment_date__gte=prev_start_date,
            payment_date__lt=start_date,
            status='completed'
        )
        prev_revenue = prev_payments.aggregate(total=Sum('amount'))['total'] or 0
        revenue_change = ((total_revenue - prev_revenue) / prev_revenue * 100) if prev_revenue > 0 else 0
        
        return {
            'total_revenue': float(total_revenue),
            'total_users': total_customers,
            'new_users': new_customers,
            'arpu': float(arpu),
            'churn_rate': round(churn_rate, 2),
            'conversion_rate': 23.5,  # Placeholder - calculate from leads if available
            'revenue_change': round(revenue_change, 1),
            'users_change': 8.3,  # Placeholder
            'new_users_change': 15.0,  # Placeholder
            'churn_change': -2.1,  # Placeholder
        }
    
    def get_revenue_data(self, start_date):
        """Get revenue trend data by month"""
        payments = Payment.objects.filter(
            payment_date__gte=start_date,
            status='completed'
        ).annotate(
            month=TruncMonth('payment_date')
        ).values('month').annotate(
            revenue=Sum('amount'),
            users=Count('invoice__customer', distinct=True)
        ).order_by('month')
        
        result = []
        for p in payments:
            # Calculate target (90% of revenue as target for simplicity)
            target = float(p['revenue']) * 0.9 if p['revenue'] else 0
            
            result.append({
                'month': p['month'].strftime('%b'),
                'revenue': float(p['revenue'] or 0),
                'target': target,
                'users': p['users'],
            })
        
        return result
    
    def get_user_growth_data(self, start_date):
        """Get user growth and churn data by month"""
        from django.db.models.functions import TruncMonth
        
        months = []
        current = start_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        
        while current <= timezone.now():
            month_end = (current + relativedelta(months=1)) - timedelta(days=1)
            
            new_users = Customer.objects.filter(
                created_at__gte=current,
                created_at__lte=month_end,
                status='active'
            ).count()
            
            churned = Customer.objects.filter(
                status='terminated',
                deactivation_date__gte=current,
                deactivation_date__lte=month_end
            ).count()
            
            months.append({
                'month': current.strftime('%b'),
                'new_users': new_users,
                'churn': churned,
                'net_growth': new_users - churned,
            })
            
            current += relativedelta(months=1)
        
        return months
    
    def get_plan_performance(self, start_date):
        """Get plan performance analytics"""
        plans = Plan.objects.filter(is_active=True)
        
        result = []
        for plan in plans:
            # Get active customers on this plan
            active_customers = ServiceConnection.objects.filter(
                plan=plan,
                status='active'
            ).count()
            
            # Get revenue from this plan
            plan_revenue = Payment.objects.filter(
                invoice__plan=plan,
                payment_date__gte=start_date,
                status='completed'
            ).aggregate(total=Sum('amount'))['total'] or 0
            
            # Calculate ARPU
            arpu = plan_revenue / active_customers if active_customers > 0 else 0
            
            result.append({
                'id': plan.id,
                'name': plan.name,
                'type': plan.plan_type.lower() if hasattr(plan, 'plan_type') else 'standard',
                'users': active_customers,
                'revenue': float(plan_revenue),
                'arpu': float(arpu),
                'share': round((active_customers / Customer.objects.filter(status='active').count() * 100), 1) if active_customers > 0 else 0,
            })
        
        return result
    
    def get_location_analytics(self, start_date):
        """Get location-based analytics"""
        # Group by customer addresses
        from apps.customers.models import CustomerAddress
        
        locations = CustomerAddress.objects.filter(
            customer__status='active',
            is_primary=True
        ).values(
            'county'
        ).annotate(
            users=Count('customer'),
            revenue=Sum('customer__payments__amount', filter=Q(
                customer__payments__status='completed',
                customer__payments__payment_date__gte=start_date
            ))
        ).order_by('-revenue')[:10]
        
        result = []
        for idx, loc in enumerate(locations, 1):
            location_name = f"{loc.get('city', 'Unknown')}, {loc.get('county', 'Unknown')}"
            
            result.append({
                'id': idx,
                'name': location_name,
                'users': loc['users'],
                'revenue': float(loc['revenue'] or 0),
                'growth': 10.0,  # Placeholder
                'share': 25.6,  # Placeholder
            })
        
        return result
    
    def get_router_analytics(self):
        """Get router performance analytics"""
        # Use OLTDevice as routers
        routers = OLTDevice.objects.filter(status='online')
        
        result = []
        for router in routers:
            # Estimate active users (count CPE devices connected to this OLT)
            active_users = CPEDevice.objects.filter(
                olt_device=router,
                status='online'
            ).count()
            
            result.append({
                'id': router.id,
                'name': router.name,
                'users': active_users,
                'uptime': 99.9,  # Placeholder - should calculate from logs
                'bandwidth': 70,  # Placeholder
                'status': 'healthy' if router.status == 'online' else 'warning',
            })
        
        return result
    
    def get_payment_methods(self, start_date):
        """Get payment method breakdown"""
        payment_methods = Payment.objects.filter(
            payment_date__gte=start_date,
            status='completed'
        ).values('payment_method').annotate(
            transactions=Count('id'),
            amount=Sum('amount')
        ).order_by('-amount')
        
        total_amount = sum(pm['amount'] for pm in payment_methods)
        
        result = []
        for pm in payment_methods:
            percentage = (pm['amount'] / total_amount * 100) if total_amount > 0 else 0
            
            result.append({
                'method': pm['payment_method'] or 'Other',
                'transactions': pm['transactions'],
                'amount': float(pm['amount']),
                'percentage': round(percentage, 1),
            })
        
        return result
    
    def get_payment_stats(self, start_date):
        """Get payment statistics"""
        payments = Payment.objects.filter(payment_date__gte=start_date)
        successful = payments.filter(status='completed')
        failed = payments.filter(status='failed')
        
        total = payments.count()
        success_rate = (successful.count() / total * 100) if total > 0 else 0
        failure_rate = (failed.count() / total * 100) if total > 0 else 0
        
        avg_amount = successful.aggregate(avg=Avg('amount'))['avg'] or 0
        max_amount = successful.aggregate(max=Max('amount'))['max'] or 0
        
        # Calculate collection rate (payments vs invoices)
        total_invoices = Invoice.objects.filter(
            billing_date__gte=start_date
        ).aggregate(total=Sum('total_amount'))['total'] or 0
        
        total_payments = successful.aggregate(total=Sum('amount'))['total'] or 0
        collection_rate = (total_payments / total_invoices * 100) if total_invoices > 0 else 0
        
        return {
            'success_rate': round(success_rate, 1),
            'failure_rate': round(failure_rate, 1),
            'total_transactions': total,
            'average_transaction': float(avg_amount),
            'highest_transaction': float(max_amount),
            'collection_rate': round(collection_rate, 1),
        }
    
    def get_user_distribution(self):
        hotspot = ServiceConnection.objects.filter(
            status='ACTIVE',
            auth_connection_type='HOTSPOT'
        ).count()
        
        pppoe = ServiceConnection.objects.filter(
            status='ACTIVE',
            auth_connection_type='PPPOE'
        ).count()
        
        static = ServiceConnection.objects.filter(
            status='ACTIVE',
            auth_connection_type='STATIC'
        ).count()
        
        total = hotspot + pppoe + static
        
        return {
            'hotspot_users': hotspot,
            'pppoe_users': pppoe,
            'static_users': static,
            'hotspot_percentage': round((hotspot / total * 100), 1) if total else 0,
            'pppoe_percentage': round((pppoe / total * 100), 1) if total else 0,
            'static_percentage': round((static / total * 100), 1) if total else 0,
        }
    
    def get_revenue_by_type(self, start_date):
        hotspot_revenue = Payment.objects.filter(
            invoice__service_connection__auth_connection_type='HOTSPOT',
            payment_date__gte=start_date,
            status='COMPLETED'
        ).aggregate(total=Sum('amount'))['total'] or 0
        
        pppoe_revenue = Payment.objects.filter(
            invoice__service_connection__auth_connection_type='PPPOE',
            payment_date__gte=start_date,
            status='COMPLETED'
        ).aggregate(total=Sum('amount'))['total'] or 0
        
        static_revenue = Payment.objects.filter(
            invoice__service_connection__auth_connection_type='STATIC',
            payment_date__gte=start_date,
            status='COMPLETED'
        ).aggregate(total=Sum('amount'))['total'] or 0
        
        total = hotspot_revenue + pppoe_revenue + static_revenue
        
        return {
            'hotspot_revenue': float(hotspot_revenue),
            'pppoe_revenue': float(pppoe_revenue),
            'static_revenue': float(static_revenue),
            'hotspot_percentage': round((hotspot_revenue / total * 100), 1) if total else 0,
            'pppoe_percentage': round((pppoe_revenue / total * 100), 1) if total else 0,
            'static_percentage': round((static_revenue / total * 100), 1) if total else 0,
        }
    
    def get_revenue_forecast(self):
        """Get 3-month revenue forecast"""
        # Simple linear forecast based on last 3 months
        three_months_ago = timezone.now() - relativedelta(months=3)
        
        monthly_revenue = []
        for i in range(3):
            month_start = three_months_ago + relativedelta(months=i)
            month_end = month_start + relativedelta(months=1)
            
            revenue = Payment.objects.filter(
                payment_date__gte=month_start,
                payment_date__lt=month_end,
                status='completed'
            ).aggregate(total=Sum('amount'))['total'] or 0
            
            monthly_revenue.append(float(revenue))
        
        # Simple average growth
        avg_growth = 1.065  # 6.5% growth
        
        forecast = []
        current_month = timezone.now().replace(day=1)
        last_revenue = monthly_revenue[-1] if monthly_revenue else 1000000
        
        for i in range(1, 4):
            month = current_month + relativedelta(months=i)
            projected = last_revenue * (avg_growth ** i)
            
            forecast.append({
                'month': month.strftime('%B %Y'),
                'projected_revenue': round(projected, 2),
                'growth_rate': round((avg_growth - 1) * 100, 1),
            })
        
        return forecast
    
    def get_revenue_target(self):
        """Get revenue target progress"""
        current_year = timezone.now().year
        year_start = datetime(current_year, 1, 1)
        year_start = timezone.make_aware(year_start)
        
        current_revenue = Payment.objects.filter(
            payment_date__gte=year_start,
            status='completed'
        ).aggregate(total=Sum('amount'))['total'] or 0
        
        # Target: 10,000,000 for the year (example)
        target_revenue = 10000000
        progress = (current_revenue / target_revenue * 100) if target_revenue > 0 else 0
        
        # Monthly average
        months_passed = timezone.now().month
        monthly_average = current_revenue / months_passed if months_passed > 0 else 0
        
        # Best month (find max monthly revenue)
        best_month = 0
        for month in range(1, months_passed + 1):
            month_start = datetime(current_year, month, 1)
            month_end = datetime(current_year, month + 1, 1) if month < 12 else datetime(current_year + 1, 1, 1)
            month_start = timezone.make_aware(month_start)
            month_end = timezone.make_aware(month_end)
            
            month_revenue = Payment.objects.filter(
                payment_date__gte=month_start,
                payment_date__lt=month_end,
                status='completed'
            ).aggregate(total=Sum('amount'))['total'] or 0
            
            if month_revenue > best_month:
                best_month = month_revenue
        
        # Projected annual
        projected_annual = monthly_average * 12
        
        return {
            'current_revenue': float(current_revenue),
            'target_revenue': float(target_revenue),
            'progress_percentage': round(progress, 1),
            'monthly_average': float(monthly_average),
            'best_month_revenue': float(best_month),
            'projected_annual': float(projected_annual),
        }
    
    def get_network_stats(self):
        """Get network statistics summary"""
        # Use created_at as time filter (or period_start if you prefer)
        recent_usage = DataUsage.objects.filter(
            created_at__gte=timezone.now() - timedelta(days=1)
        )
        
        # Aggregate real fields from your model
        bandwidth_usage = recent_usage.aggregate(
            avg_download=Avg('download_bytes'),  # or daily_usage if that's average
            max_peak_download=Max('peak_download_speed'),
            max_peak_upload=Max('peak_upload_speed'),
            total_bytes=Sum('total_bytes')
        )
        
        # Fallback/placeholders if no data
        avg_bandwidth = bandwidth_usage['avg_download'] or 72  # in bytes or convert to Mbps if needed
        warning_count = 0  # Placeholder - add logic if you have status field
        
        # Router count (using OLTDevice as example)
        routers = OLTDevice.objects.filter(status='online')
        active_routers = routers.count()
        
        # Average uptime - placeholder (add real field if available)
        avg_uptime = 99.5
        
        return {
            'avg_uptime': round(avg_uptime, 1),
            'active_routers': active_routers,
            'avg_bandwidth': round(float(avg_bandwidth) / (1024*1024), 1) if avg_bandwidth else 72,  # Convert bytes to MB if needed
            'warning_count': warning_count,
        }
