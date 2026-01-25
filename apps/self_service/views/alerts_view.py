"""
Customer Alerts Views

Endpoints for managing customer usage alerts.
"""

import logging

from rest_framework import status, generics
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.customers.models import Customer
from ..models import UsageAlert
from ..permissions import CustomerOnlyPermission

logger = logging.getLogger(__name__)


class UsageAlertSerializer:
    """Simple serializer placeholder - should be defined in serializers.py"""
    pass


class CustomerAlertsView(generics.ListAPIView):
    """
    List customer alerts.
    
    AUTHENTICATED ENDPOINT - Requires customer JWT token.
    
    GET /api/v1/self-service/alerts/
    """
    
    permission_classes = [IsAuthenticated, CustomerOnlyPermission]
    # serializer_class = UsageAlertSerializer
    
    def get_queryset(self):
        user = self.request.user
        
        try:
            customer = Customer.objects.get(user=user)
            return UsageAlert.objects.filter(customer=customer).order_by('-triggered_at')[:50]
        except Customer.DoesNotExist:
            return UsageAlert.objects.none()
    
    def list(self, request, *args, **kwargs):
        queryset = self.get_queryset()
        
        alerts_data = [
            {
                'id': alert.id,
                'alert_type': alert.alert_type,
                'message': alert.message,
                'threshold_value': float(alert.threshold_value) if alert.threshold_value else None,
                'current_value': float(alert.current_value) if alert.current_value else None,
                'is_read': alert.is_read,
                'triggered_at': alert.triggered_at.isoformat(),
            }
            for alert in queryset
        ]
        
        return Response({
            'alerts': alerts_data,
            'count': len(alerts_data),
            'unread_count': len([a for a in alerts_data if not a['is_read']]),
        })


class MarkAlertReadView(APIView):
    """
    Mark an alert as read.
    
    AUTHENTICATED ENDPOINT - Requires customer JWT token.
    
    POST /api/v1/self-service/alerts/{id}/read/
    """
    
    permission_classes = [IsAuthenticated, CustomerOnlyPermission]
    
    def post(self, request, pk):
        user = request.user
        
        try:
            customer = Customer.objects.get(user=user)
            alert = UsageAlert.objects.get(id=pk, customer=customer)
            alert.is_read = True
            alert.save(update_fields=['is_read'])
            
            return Response({
                'status': 'success',
                'message': 'Alert marked as read'
            })
        
        except Customer.DoesNotExist:
            return Response({
                'error': 'Customer profile not found'
            }, status=status.HTTP_404_NOT_FOUND)
        
        except UsageAlert.DoesNotExist:
            return Response({
                'error': 'Alert not found'
            }, status=status.HTTP_404_NOT_FOUND)


class MarkAllAlertsReadView(APIView):
    """
    Mark all alerts as read.
    
    AUTHENTICATED ENDPOINT - Requires customer JWT token.
    
    POST /api/v1/self-service/alerts/mark-all-read/
    """
    
    permission_classes = [IsAuthenticated, CustomerOnlyPermission]
    
    def post(self, request):
        user = request.user
        
        try:
            customer = Customer.objects.get(user=user)
            updated_count = UsageAlert.objects.filter(
                customer=customer, 
                is_read=False
            ).update(is_read=True)
            
            return Response({
                'status': 'success',
                'message': f'{updated_count} alerts marked as read'
            })
        
        except Customer.DoesNotExist:
            return Response({
                'error': 'Customer profile not found'
            }, status=status.HTTP_404_NOT_FOUND)
