from datetime import datetime, timedelta
from decimal import Decimal
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


class CustomerDashboardView(APIView):
    """
    Customer portal dashboard - Returns data in format expected by frontend.
    
    GET /api/v1/self-service/dashboard/
    
    Response format matches CustomerDashboardData TypeScript interface.
    """
    permission_classes = [IsAuthenticated, CustomerOnlyPermission]
    
    def get(self, request, *args, **kwargs):
        user = request.user
        
        # Get customer profile
        try:
            customer = user.customer_profile
        except Customer.DoesNotExist:
            return Response({
                'error': 'Customer profile not found'
            }, status=status.HTTP_404_NOT_FOUND)
        
        # Build customer data - use full_name property or fallback to user name
        full_name = getattr(customer, 'full_name', None)
        if not full_name or full_name == ' ':
            full_name = f"{user.first_name} {user.last_name}".strip() or user.email
        
        customer_data = {
            'id': customer.id,
            'customer_code': customer.customer_code,
            'full_name': full_name,
            'email': user.email,
            'phone_number': getattr(customer, 'phone_number', '') or getattr(user, 'phone_number', '') or '',
            'status': customer.status,
            'balance': str(getattr(customer, 'outstanding_balance', Decimal('0.00')) or Decimal('0.00')),
            'created_at': customer.created_at.isoformat() if customer.created_at else None,
        }
        
        # Get current plan from active service
        current_plan = None
        try:
            # Try to get active service with plan
            service = getattr(customer, 'services', Customer.objects.none())
            if hasattr(service, 'filter'):
                active_service = service.filter(status='ACTIVE').select_related('plan').first()
            else:
                active_service = None
            
            if active_service and active_service.plan:
                plan = active_service.plan
                expiry_date = getattr(active_service, 'expiry_date', None) or getattr(active_service, 'expires_at', None)
                days_remaining = None
                
                if expiry_date:
                    if isinstance(expiry_date, str):
                        expiry_date = datetime.fromisoformat(expiry_date.replace('Z', '+00:00'))
                    days_remaining = max(0, (expiry_date.date() - timezone.now().date()).days)
                
                current_plan = {
                    'id': plan.id,
                    'name': plan.name,
                    'price': str(plan.price),
                    'speed_down': str(getattr(plan, 'download_speed', '') or getattr(plan, 'speed_down', '') or ''),
                    'speed_up': str(getattr(plan, 'upload_speed', '') or getattr(plan, 'speed_up', '') or ''),
                    'expiry_date': expiry_date.isoformat() if expiry_date else None,
                    'days_remaining': days_remaining,
                }
            elif customer.plan:
                # Fallback to customer.plan if no active service
                plan = customer.plan
                current_plan = {
                    'id': plan.id,
                    'name': plan.name,
                    'price': str(plan.price),
                    'speed_down': str(getattr(plan, 'download_speed', '') or getattr(plan, 'speed_down', '') or ''),
                    'speed_up': str(getattr(plan, 'upload_speed', '') or getattr(plan, 'speed_up', '') or ''),
                    'expiry_date': None,
                    'days_remaining': None,
                }
        except Exception:
            pass
        
        # Get usage data
        usage = self._get_usage(customer)
        
        # Get recent payments
        recent_payments = self._get_recent_payments(customer)
        
        # Get pending invoices
        pending_invoices = self._get_pending_invoices(customer)
        
        return Response({
            'customer': customer_data,
            'current_plan': current_plan,
            'usage': usage,
            'recent_payments': recent_payments,
            'pending_invoices': pending_invoices,
        })
    
    def _get_usage(self, customer):
        """Get data usage in frontend-expected format"""
        today = timezone.now().date()
        month_start = today.replace(day=1)
        
        try:
            monthly_usage = DataUsage.objects.filter(
                customer=customer,
                timestamp__gte=month_start
            ).aggregate(
                total_upload=Sum('upload_bytes'),
                total_download=Sum('download_bytes')
            )
            
            upload_bytes = monthly_usage['total_upload'] or 0
            download_bytes = monthly_usage['total_download'] or 0
            total_bytes = upload_bytes + download_bytes
            
            # Convert to human-readable format
            if total_bytes >= 1024**3:
                data_used = f"{total_bytes / (1024**3):.1f} GB"
            elif total_bytes >= 1024**2:
                data_used = f"{total_bytes / (1024**2):.1f} MB"
            else:
                data_used = f"{total_bytes / 1024:.1f} KB"
            
            # Get data limit from plan
            data_limit = None
            percentage = 0
            if customer.plan and hasattr(customer.plan, 'data_cap_gb') and customer.plan.data_cap_gb:
                data_limit = f"{customer.plan.data_cap_gb} GB"
                limit_bytes = customer.plan.data_cap_gb * (1024**3)
                percentage = min(100, (total_bytes / limit_bytes) * 100) if limit_bytes > 0 else 0
            
            return {
                'data_used': data_used,
                'data_limit': data_limit,
                'percentage': round(percentage, 1),
            }
        except Exception:
            return {
                'data_used': '0 GB',
                'data_limit': None,
                'percentage': 0,
            }
    
    def _get_recent_payments(self, customer):
        """Get recent payments in frontend-expected format"""
        try:
            payments = Payment.objects.filter(
                customer=customer
            ).order_by('-created_at')[:5]
            
            return [
                {
                    'id': p.id,
                    'amount': str(p.amount),
                    'method': str(p.payment_method) if p.payment_method else 'Unknown',
                    'status': p.status.lower() if p.status else 'pending',
                    'created_at': p.created_at.isoformat() if p.created_at else None,
                }
                for p in payments
            ]
        except Exception:
            return []
    
    def _get_pending_invoices(self, customer):
        """Get pending invoices in frontend-expected format"""
        try:
            invoices = Invoice.objects.filter(
                customer=customer,
                status__in=['PENDING', 'OVERDUE', 'pending', 'overdue']
            ).order_by('-created_at')[:10]
            
            return [
                {
                    'id': inv.id,
                    'invoice_number': inv.invoice_number,
                    'amount': str(inv.amount),
                    'due_date': inv.due_date.isoformat() if inv.due_date else None,
                    'status': inv.status.lower() if inv.status else 'pending',
                }
                for inv in invoices
            ]
        except Exception:
            return []
