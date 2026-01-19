from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import get_object_or_404

from ..models import ServiceRequest
from ..serializers import ServiceRequestSerializer
from ..permissions import CustomerOnlyPermission
from apps.customers.models import Customer


class ServiceRequestListCreateView(generics.ListCreateAPIView):
    """
    List and create service requests
    """
    permission_classes = [IsAuthenticated, CustomerOnlyPermission]
    serializer_class = ServiceRequestSerializer
    
    def get_queryset(self):
        return ServiceRequest.objects.filter(customer=self.request.user.customer_profile)
    
    def perform_create(self, serializer):
        serializer.save(customer=self.request.user.customer_profile)
    
    def post(self, request, *args, **kwargs):
        """Create a new service request with validation"""
        customer = request.user.customer_profile
        
        # Check if customer is active
        if customer.status != 'active':
            return Response(
                {'error': 'Only active customers can submit service requests'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Validate request type
        request_type = request.data.get('request_type')
        if not request_type:
            return Response(
                {'error': 'Request type is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Additional validation based on request type
        if request_type == 'upgrade' or request_type == 'downgrade':
            if not request.data.get('requested_plan'):
                return Response(
                    {'error': 'Requested plan is required for plan changes'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
        return super().post(request, *args, **kwargs)


class ServiceRequestDetailView(generics.RetrieveUpdateAPIView):
    """
    Retrieve, update, or cancel service request
    """
    permission_classes = [IsAuthenticated, CustomerOnlyPermission]
    serializer_class = ServiceRequestSerializer
    
    def get_queryset(self):
        return ServiceRequest.objects.filter(customer=self.request.user.customer_profile)
    
    def update(self, request, *args, **kwargs):
        """Update service request (only allowed for certain fields)"""
        instance = self.get_object()
        
        # Customers can only update certain fields
        allowed_fields = ['description', 'customer_notes']
        
        for field in request.data:
            if field not in allowed_fields:
                return Response(
                    {'error': f'Field {field} cannot be updated'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
        return super().update(request, *args, **kwargs)
    
    def delete(self, request, *args, **kwargs):
        """Cancel a service request"""
        instance = self.get_object()
        
        # Only allow cancellation if request is pending
        if instance.status != 'pending':
            return Response(
                {'error': 'Only pending requests can be cancelled'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        instance.status = 'cancelled'
        instance.save()
        
        return Response({
            'message': 'Service request cancelled successfully',
            'request_id': instance.id,
            'status': instance.status,
        })


class ServiceRequestTypesView(APIView):
    """
    Get available service request types
    """
    permission_classes = [IsAuthenticated, CustomerOnlyPermission]
    
    def get(self, request):
        """Get available service request types with requirements"""
        customer = request.user.customer_profile
        
        request_types = [
            {
                'id': 'connection',
                'name': 'New Connection',
                'description': 'Request for new internet connection',
                'icon': 'wifi',
                'requirements': ['valid_id', 'proof_of_address'],
                'available': True,  # Always available
            },
            {
                'id': 'upgrade',
                'name': 'Plan Upgrade',
                'description': 'Upgrade to a higher bandwidth plan',
                'icon': 'trending-up',
                'requirements': ['active_account', 'no_outstanding_balance'],
                'available': customer.status == 'active' and customer.current_balance == 0,
            },
            {
                'id': 'downgrade',
                'name': 'Plan Downgrade',
                'description': 'Downgrade to a lower bandwidth plan',
                'icon': 'trending-down',
                'requirements': ['active_account', 'minimum_3_months'],
                'available': customer.status == 'active' and customer.created_at < timezone.now() - timedelta(days=90),
            },
            {
                'id': 'transfer',
                'name': 'Location Transfer',
                'description': 'Transfer service to new location',
                'icon': 'map-pin',
                'requirements': ['active_account', 'new_address', 'transfer_fee'],
                'available': customer.status == 'active',
            },
            {
                'id': 'suspension',
                'name': 'Service Suspension',
                'description': 'Temporarily suspend service',
                'icon': 'pause',
                'requirements': ['active_account', 'minimum_1_month'],
                'available': customer.status == 'active' and customer.created_at < timezone.now() - timedelta(days=30),
            },
            {
                'id': 'termination',
                'name': 'Service Termination',
                'description': 'Permanently terminate service',
                'icon': 'x-circle',
                'requirements': ['active_account', 'settle_balance'],
                'available': customer.status == 'active',
            },
            {
                'id': 'billing',
                'name': 'Billing Issue',
                'description': 'Report billing discrepancy or issue',
                'icon': 'file-text',
                'requirements': ['invoice_details'],
                'available': True,
            },
            {
                'id': 'technical',
                'name': 'Technical Support',
                'description': 'Report technical issues or request support',
                'icon': 'tool',
                'requirements': ['issue_description'],
                'available': True,
            },
            {
                'id': 'other',
                'name': 'Other Request',
                'description': 'Any other service request',
                'icon': 'help-circle',
                'requirements': ['request_details'],
                'available': True,
            },
        ]
        
        # Get available plans for upgrade/downgrade
        from apps.billing.models import Plan
        available_plans = Plan.objects.filter(is_active=True).values('id', 'name', 'price', 'download_speed', 'upload_speed')
        
        return Response({
            'request_types': request_types,
            'available_plans': list(available_plans),
            'customer_status': customer.status,
            'customer_since': customer.created_at,
        })
