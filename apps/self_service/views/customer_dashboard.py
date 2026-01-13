from datetime import datetime, timedelta
from django.utils import timezone
from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Sum, Count, Q

from ..models import ServiceRequest, UsageAlert, CustomerSession
from ..serializers import CustomerDashboardSerializer, ServiceRequestSerializer, UsageAlertSerializer
from ..permissions import CustomerOnlyPermission
from apps.customers.models import Customer
from apps.billing.models import Invoice, Payment
from apps.bandwidth.models import DataUsage
from apps.support.models import SupportTicket


class CustomerDashboardView(generics.RetrieveAPIView):
    """
    Customer portal dashboard
    """
    permission_classes = [IsAuthenticated, CustomerOnlyPermission]
    serializer_class = CustomerDashboardSerializer
    
    def get_object(self):
        return self.request.user.customer_profile
    
    def get(self, request, *args, **kwargs):
        customer = self.get_object()
        
        # Get current usage
        current_usage = self._get_current_usage(customer)
        
        # Get billing summary
        billing_summary = self._get_billing_summary(customer)
        
        # Get recent activity
        recent_activity = self._get_recent_activity(customer)
        
        # Get alerts
        alerts = UsageAlert.objects.filter(
            customer=customer,
            is_read=False
        ).order_by('-triggered_at')[:5]
        
        data = {
            'customer': {
                'name': customer.name,
                'customer_code': customer.customer_code,
                'plan': customer.plan.name if customer.plan else None,
                'status': customer.status,
                'join_date': customer.created_at,
            },
            'usage': current_usage,
            'billing': billing_summary,
            'recent_activity': recent_activity,
            'alerts': UsageAlertSerializer(alerts, many=True).data,
            'quick_actions': self._get_quick_actions(customer),
        }
        
        return Response(data)
    
    def _get_current_usage(self, customer):
        """Get current bandwidth usage"""
        today = timezone.now().date()
        month_start = today.replace(day=1)
        
        # Monthly usage
        monthly_usage = DataUsage.objects.filter(
            customer=customer,
            timestamp__gte=month_start
        ).aggregate(
            total_upload=Sum('upload_bytes'),
            total_download=Sum('download_bytes')
        )
        
        # Today's usage
        today_usage = DataUsage.objects.filter(
            customer=customer,
            timestamp__date=today
        ).aggregate(
            total_upload=Sum('upload_bytes'),
            total_download=Sum('download_bytes')
        )
        
        # Convert bytes to GB
        upload_gb = (monthly_usage['total_upload'] or 0) / (1024**3)
        download_gb = (monthly_usage['total_download'] or 0) / (1024**3)
        
        # Check data cap
        plan_data_cap = customer.plan.data_cap_gb if customer.plan and customer.plan.data_cap_gb else None
        usage_percentage = (upload_gb + download_gb) / plan_data_cap * 100 if plan_data_cap else None
        
        return {
            'monthly_upload_gb': round(upload_gb, 2),
            'monthly_download_gb': round(download_gb, 2),
            'total_usage_gb': round(upload_gb + download_gb, 2),
            'data_cap_gb': plan_data_cap,
            'usage_percentage': round(usage_percentage, 1) if usage_percentage else None,
            'today_upload_gb': round((today_usage['total_upload'] or 0) / (1024**3), 2),
            'today_download_gb': round((today_usage['total_download'] or 0) / (1024**3), 2),
        }
    
    def _get_billing_summary(self, customer):
        """Get billing and payment summary"""
        # Current invoice
        current_invoice = Invoice.objects.filter(
            customer=customer,
            status__in=['pending', 'overdue']
        ).order_by('-invoice_date').first()
        
        # Recent payments
        recent_payments = Payment.objects.filter(
            invoice__customer=customer
        ).order_by('-payment_date')[:5]
        
        # Payment summary
        payment_summary = Payment.objects.filter(
            invoice__customer=customer,
            payment_date__month=timezone.now().month
        ).aggregate(total_paid=Sum('amount'))
        
        return {
            'current_balance': float(customer.current_balance),
            'current_invoice': {
                'amount': float(current_invoice.total_amount) if current_invoice else 0,
                'due_date': current_invoice.due_date if current_invoice else None,
                'status': current_invoice.status if current_invoice else None,
            },
            'monthly_payment_total': float(payment_summary['total_paid'] or 0),
            'recent_payments': [
                {
                    'date': payment.payment_date,
                    'amount': float(payment.amount),
                    'method': payment.payment_method,
                }
                for payment in recent_payments
            ],
        }
    
    def _get_recent_activity(self, customer):
        """Get recent customer activity"""
        # Recent service requests
        recent_requests = ServiceRequest.objects.filter(
            customer=customer
        ).order_by('-created_at')[:5]
        
        # Recent support tickets
        recent_tickets = SupportTicket.objects.filter(
            customer=customer
        ).order_by('-created_at')[:5]
        
        # Recent payments
        recent_payments = Payment.objects.filter(
            invoice__customer=customer
        ).order_by('-payment_date')[:5]
        
        return {
            'service_requests': ServiceRequestSerializer(recent_requests, many=True).data,
            'support_tickets': [
                {
                    'id': ticket.id,
                    'subject': ticket.subject,
                    'status': ticket.status,
                    'created_at': ticket.created_at,
                }
                for ticket in recent_tickets
            ],
            'payments': [
                {
                    'date': payment.payment_date,
                    'amount': float(payment.amount),
                    'invoice': payment.invoice.invoice_number,
                }
                for payment in recent_payments
            ],
        }
    
    def _get_quick_actions(self, customer):
        """Get available quick actions for customer"""
        return [
            {
                'id': 'pay_invoice',
                'label': 'Pay Invoice',
                'icon': 'credit-card',
                'available': customer.current_balance > 0,
            },
            {
                'id': 'view_usage',
                'label': 'View Usage',
                'icon': 'bar-chart',
                'available': True,
            },
            {
                'id': 'request_support',
                'label': 'Request Support',
                'icon': 'help-circle',
                'available': True,
            },
            {
                'id': 'update_profile',
                'label': 'Update Profile',
                'icon': 'user',
                'available': True,
            },
            {
                'id': 'change_plan',
                'label': 'Change Plan',
                'icon': 'refresh-cw',
                'available': customer.status == 'active',
            },
        ]