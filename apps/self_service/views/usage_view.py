from datetime import datetime, timedelta
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Sum, Avg, Count
import json

from ..permissions import CustomerOnlyPermission
from apps.bandwidth.models import DataUsage
from apps.customers.models import Customer


class UsageView(APIView):
    """
    Customer usage data and analytics
    """
    permission_classes = [IsAuthenticated, CustomerOnlyPermission]
    
    def get(self, request):
        customer = request.user.customer_profile
        
        period = request.GET.get('period', 'monthly')  # daily, weekly, monthly, yearly
        chart_type = request.GET.get('chart', 'line')  # line, bar, area
        
        if period == 'daily':
            data = self._get_daily_usage(customer)
        elif period == 'weekly':
            data = self._get_weekly_usage(customer)
        elif period == 'yearly':
            data = self._get_yearly_usage(customer)
        else:  # monthly
            data = self._get_monthly_usage(customer)
        
        return Response({
            'customer': customer.name,
            'period': period,
            'chart_type': chart_type,
            'data': data,
            'summary': self._get_usage_summary(customer, period),
        })
    
    def _get_daily_usage(self, customer):
        """Get daily usage for last 30 days"""
        end_date = timezone.now()
        start_date = end_date - timedelta(days=30)
        
        usage_data = DataUsage.objects.filter(
            customer=customer,
            timestamp__gte=start_date,
            timestamp__lte=end_date
        ).extra({
            'date': "DATE(timestamp)"
        }).values('date').annotate(
            upload=Sum('upload_bytes'),
            download=Sum('download_bytes')
        ).order_by('date')
        
        result = []
        for item in usage_data:
            result.append({
                'date': item['date'],
                'upload_gb': round(item['upload'] / (1024**3), 2),
                'download_gb': round(item['download'] / (1024**3), 2),
                'total_gb': round((item['upload'] + item['download']) / (1024**3), 2),
            })
        
        return result
    
    def _get_weekly_usage(self, customer):
        """Get weekly usage for last 12 weeks"""
        end_date = timezone.now()
        start_date = end_date - timedelta(weeks=12)
        
        usage_data = DataUsage.objects.filter(
            customer=customer,
            timestamp__gte=start_date,
            timestamp__lte=end_date
        ).extra({
            'week': "EXTRACT(WEEK FROM timestamp)",
            'year': "EXTRACT(YEAR FROM timestamp)"
        }).values('year', 'week').annotate(
            upload=Sum('upload_bytes'),
            download=Sum('download_bytes')
        ).order_by('year', 'week')
        
        result = []
        for item in usage_data:
            result.append({
                'week': f"Week {item['week']}, {item['year']}",
                'upload_gb': round(item['upload'] / (1024**3), 2),
                'download_gb': round(item['download'] / (1024**3), 2),
                'total_gb': round((item['upload'] + item['download']) / (1024**3), 2),
            })
        
        return result
    
    def _get_monthly_usage(self, customer):
        """Get monthly usage for last 12 months"""
        end_date = timezone.now()
        start_date = end_date - timedelta(days=365)
        
        usage_data = DataUsage.objects.filter(
            customer=customer,
            timestamp__gte=start_date,
            timestamp__lte=end_date
        ).extra({
            'month': "EXTRACT(MONTH FROM timestamp)",
            'year': "EXTRACT(YEAR FROM timestamp)"
        }).values('year', 'month').annotate(
            upload=Sum('upload_bytes'),
            download=Sum('download_bytes')
        ).order_by('year', 'month')
        
        result = []
        for item in usage_data:
            result.append({
                'month': f"{item['month']}/{item['year']}",
                'upload_gb': round(item['upload'] / (1024**3), 2),
                'download_gb': round(item['download'] / (1024**3), 2),
                'total_gb': round((item['upload'] + item['download']) / (1024**3), 2),
            })
        
        return result
    
    def _get_yearly_usage(self, customer):
        """Get yearly usage"""
        usage_data = DataUsage.objects.filter(
            customer=customer
        ).extra({
            'year': "EXTRACT(YEAR FROM timestamp)"
        }).values('year').annotate(
            upload=Sum('upload_bytes'),
            download=Sum('download_bytes')
        ).order_by('year')
        
        result = []
        for item in usage_data:
            result.append({
                'year': item['year'],
                'upload_gb': round(item['upload'] / (1024**3), 2),
                'download_gb': round(item['download'] / (1024**3), 2),
                'total_gb': round((item['upload'] + item['download']) / (1024**3), 2),
            })
        
        return result
    
    def _get_usage_summary(self, customer, period):
        """Get usage summary statistics"""
        end_date = timezone.now()
        
        if period == 'daily':
            start_date = end_date - timedelta(days=1)
        elif period == 'weekly':
            start_date = end_date - timedelta(days=7)
        elif period == 'yearly':
            start_date = end_date - timedelta(days=365)
        else:  # monthly
            start_date = end_date - timedelta(days=30)
        
        usage_data = DataUsage.objects.filter(
            customer=customer,
            timestamp__gte=start_date,
            timestamp__lte=end_date
        ).aggregate(
            total_upload=Sum('upload_bytes'),
            total_download=Sum('download_bytes'),
            avg_speed=Avg('current_speed'),
            peak_speed=Avg('peak_speed')
        )
        
        total_upload_gb = (usage_data['total_upload'] or 0) / (1024**3)
        total_download_gb = (usage_data['total_download'] or 0) / (1024**3)
        
        return {
            'total_upload_gb': round(total_upload_gb, 2),
            'total_download_gb': round(total_download_gb, 2),
            'total_usage_gb': round(total_upload_gb + total_download_gb, 2),
            'average_speed_mbps': round(usage_data['avg_speed'] or 0, 2),
            'peak_speed_mbps': round(usage_data['peak_speed'] or 0, 2),
            'period_days': (end_date - start_date).days,
        }