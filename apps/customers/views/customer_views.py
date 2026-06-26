from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.db.models import Q, Prefetch
from apps.billing.models import Plan

from apps.customers.models import (
    Customer, CustomerAddress, CustomerDocument, 
    NextOfKin, CustomerNotes, ServiceConnection
)
from apps.customers.serializers import (
    CustomerCreateSerializer, CustomerUpdateSerializer,
    CustomerListSerializer, CustomerDetailSerializer,
    CustomerAddressSerializer, CustomerAddressCreateSerializer,
    CustomerDocumentSerializer, DocumentUploadSerializer,
    NextOfKinSerializer, CustomerNotesSerializer
)
from apps.customers.permissions import (
    CustomerAccessPermission, CanManageCustomers
)
from apps.core.permissions import IsAdminOrStaff
# FIX: Import LargeResultsSetPagination along with StandardResultsSetPagination
from utils.pagination import StandardResultsSetPagination, LargeResultsSetPagination

import logging
logger = logging.getLogger(__name__)


def _sync_service_to_plan(service, plan):
    """Apply the selected plan's billable and network defaults to a service."""
    auth_mapping = {
        'HOTSPOT': 'HOTSPOT',
        'PPPOE': 'PPPOE',
        'STATIC': 'STATIC',
        'INTERNET': 'PPPOE',
    }
    service.plan = plan
    service.monthly_price = plan.base_price
    service.download_speed = plan.download_speed or 0
    service.upload_speed = plan.upload_speed or 0
    service.data_cap = plan.data_limit
    service.auth_connection_type = auth_mapping.get(plan.plan_type, service.auth_connection_type or 'OTHER')
    return service


class CustomerViewSet(viewsets.ModelViewSet):
    """ViewSet for managing customers"""
    # OPTIMIZED: Only fetch necessary related data with depth limits
    # - select_related: direct foreign keys (1:1 or belongs-to)
    # - prefetch_related with limit: only fetch the most recent active service, not all services
    queryset = Customer.objects.select_related(
        'user',                    # needed for name/phone/email
        'radius_credentials',      # needed for PPPoE username/expiry
    ).prefetch_related(
        # Only fetch the first active service — NOT ALL services
        Prefetch(
            'services',
            queryset=ServiceConnection.objects.select_related('plan').filter(
                status__in=['ACTIVE', 'PENDING']
            ).order_by('-activation_date', '-created_at')[:1],
            to_attr='active_services_list'
        )
    ).all()
    
    permission_classes = [IsAuthenticated, CanManageCustomers]
    # FIX: Add pagination_class to support large page sizes (up to 1000)
    pagination_class = LargeResultsSetPagination
    
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = [
        'status', 'customer_type', 'category', 
        'gender', 'id_type'
    ]
    search_fields = [
        'customer_code', 'user__first_name', 'user__last_name',
        'user__email', 'user__phone_number', 'id_number',
        'alternative_phone', 'services__plan__name', 'services__plan__code',
        'services__billing_account_number', 'services__mpesa_account_number',
        'services__paybill_account_number', 'services__ip_address',
        'services__mac_address', 'radius_credentials__username',
        'location'  # ADDED
    ]
    ordering_fields = ['created_at', 'customer_code', 'user__last_name']
    ordering = ['-created_at']
    
    def get_serializer_class(self):
        if self.action == 'create':
            return CustomerCreateSerializer
        elif self.action == 'update' or self.action == 'partial_update':
            return CustomerUpdateSerializer
        elif self.action == 'list':
            return CustomerListSerializer
        elif self.action == 'retrieve':
            return CustomerDetailSerializer
        return CustomerListSerializer
    
    def get_permissions(self):
        # FIX: Added 'retrieve', 'list', 'dashboard' to admin-accessible actions
        if self.action in ['create', 'update', 'partial_update', 'destroy',
                           'toggle_radius', 'change_status', 'available_plans',
                           'change_plan', 'retrieve', 'list', 'dashboard']:
            permission_classes = [IsAuthenticated, IsAdminOrStaff]
        else:
            permission_classes = [IsAuthenticated, CustomerAccessPermission]
        return [permission() for permission in permission_classes]
    
    def get_queryset(self):
        # With django-tenants, the queryset is automatically scoped to the current tenant's schema
        queryset = super().get_queryset()
        user = self.request.user
        
        # SUPERUSERS/STAFF: can see all customers in current tenant
        if user.is_superuser or user.is_staff:
            return queryset
        
        # CUSTOMERS: can only see themselves
        if hasattr(user, 'customer_profile'):
            queryset = queryset.filter(id=user.customer_profile.id)
        else:
            # No access
            if not (user.is_superuser or user.is_staff):
                return queryset.none()

        search_term = (self.request.query_params.get('search') or '').strip()
        if search_term:
            queryset = queryset.filter(
                Q(customer_code__icontains=search_term)
                | Q(user__first_name__icontains=search_term)
                | Q(user__last_name__icontains=search_term)
                | Q(user__email__icontains=search_term)
                | Q(user__phone_number__icontains=search_term)
                | Q(id_number__icontains=search_term)
                | Q(alternative_phone__icontains=search_term)
                | Q(services__plan__name__icontains=search_term)
                | Q(services__plan__code__icontains=search_term)
                | Q(services__billing_account_number__icontains=search_term)
                | Q(services__mpesa_account_number__icontains=search_term)
                | Q(services__paybill_account_number__icontains=search_term)
                | Q(services__ip_address__icontains=search_term)
                | Q(services__mac_address__icontains=search_term)
                | Q(radius_credentials__username__icontains=search_term)
                | Q(location__icontains=search_term)  # ADDED
            )

        return queryset.distinct()

    def _get_target_service(self, customer, service_id=None):
        """Get the target service, either by ID or the most recent active one."""
        # Use the prefetched active_services_list if available
        if hasattr(customer, 'active_services_list') and customer.active_services_list:
            services = customer.active_services_list
        else:
            # Fallback to normal query if prefetch not available
            services = list(customer.services.select_related('plan').filter(
                status__in=['ACTIVE', 'PENDING']
            ).order_by('-activation_date', '-created_at')[:1])
        
        if service_id:
            # If specific ID requested, we need to fetch it (could be not in prefetched list)
            return get_object_or_404(customer.services.select_related('plan'), id=service_id)
        
        return services[0] if services else None
    
    def perform_create(self, serializer):
        """Create customer - tenant scoping handled by django-tenants"""
        user = self.request.user
        
        # Set created_by if user is authenticated
        if user.is_authenticated:
            serializer.save(created_by=user)
        else:
            serializer.save()
    
    @action(detail=True, methods=['get'])
    def dashboard(self, request, pk=None):
        """Get customer dashboard data"""
        customer = self.get_object()
        
        # Use optimized count queries instead of loading all services
        total_services = customer.services.count()
        active_services = customer.services.filter(status='ACTIVE').count()
        pending_services = customer.services.filter(status='PENDING').count()
        
        data = {
            'customer_info': CustomerDetailSerializer(customer).data,
            'stats': {
                'total_services': total_services,
                'active_services': active_services,
                'pending_services': pending_services,
                'total_invoices': 0,  # Will be added in billing module
                'pending_invoices': 0,
                'total_tickets': 0,  # Will be added in support module
                'open_tickets': 0,
            }
        }
        
        return Response(data)
    
    @action(detail=True, methods=['post'])
    def change_status(self, request, pk=None):
        """Change customer status"""
        customer = self.get_object()
        new_status = request.data.get('status')
        
        if new_status not in dict(Customer.STATUS_CHOICES):
            return Response(
                {'error': 'Invalid status'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        customer.status = new_status
        
        if new_status == 'TERMINATED' or new_status == 'INACTIVE':
            customer.deactivation_date = timezone.now()
            # Suspend all active services
            customer.services.filter(status='ACTIVE').update(
                status='SUSPENDED',
                suspension_date=timezone.now()
            )
        
        customer.save()
        
        # Create note about status change
        CustomerNotes.objects.create(
            customer=customer,
            note=f"Status changed to {new_status}. Reason: {request.data.get('reason', 'No reason provided')}",
            note_type='GENERAL',
            created_by=request.user
        )
        
        return Response({'status': 'Status updated successfully'})

    def destroy(self, request, *args, **kwargs):
        """
        Delete a customer. Signals handle:
        - RADIUS cleanup (pre_delete removes radcheck/radreply entries)
        - User cleanup (post_delete removes the orphaned Django User)
        """
        customer = self.get_object()
        customer_code = customer.customer_code
        customer_name = customer.full_name

        # Delete all service connections first (triggers RADIUS cleanup)
        customer.services.all().delete()

        # Now delete the customer (signals handle RADIUS + User cleanup)
        self.perform_destroy(customer)

        logger.info(
            f"Customer {customer_code} ({customer_name}) deleted by {request.user}"
        )

        return Response(
            {
                'status': 'success',
                'message': f'Customer {customer_code} deleted successfully. '
                           f'RADIUS credentials and user account have been cleaned up.'
            },
            status=status.HTTP_200_OK
        )

    @action(detail=True, methods=['get'])
    def available_plans(self, request, pk=None):
        customer = self.get_object()
        service_id = request.query_params.get('service_id')
        service = self._get_target_service(customer, service_id=service_id)

        plans = Plan.objects.filter(is_active=True).order_by('name')
        if service and service.auth_connection_type:
            auth_type = service.auth_connection_type.upper()
            compatible_plan_types = {
                'HOTSPOT': ['HOTSPOT'],
                'PPPOE': ['PPPOE', 'INTERNET'],
                'STATIC': ['STATIC', 'PPPOE', 'INTERNET'],
                'DYNAMIC': ['INTERNET', 'PPPOE'],
            }.get(auth_type)
            if compatible_plan_types:
                plans = plans.filter(plan_type__in=compatible_plan_types)

        payload = []
        for plan in plans:
            payload.append({
                'id': plan.id,
                'name': plan.name,
                'code': plan.code,
                'plan_type': plan.plan_type,
                'price': str(plan.base_price),
                'download_speed': plan.download_speed,
                'upload_speed': plan.upload_speed,
                'data_limit': plan.data_limit,
                'is_public': plan.is_public,
                'is_popular': plan.is_popular,
            })

        return Response({
            'customer_id': customer.id,
            'service_id': service.id if service else None,
            'current_plan_id': service.plan_id if service else None,
            'current_plan_name': service.plan.name if service and service.plan else None,
            'plans': payload,
        })

    @action(detail=True, methods=['post'])
    def change_plan(self, request, pk=None):
        customer = self.get_object()
        plan_id = request.data.get('plan_id')
        service_id = request.data.get('service_id')

        if not plan_id:
            return Response(
                {'error': 'plan_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        service = self._get_target_service(customer, service_id=service_id)
        if not service:
            return Response(
                {'error': 'No service found for this customer'},
                status=status.HTTP_400_BAD_REQUEST
            )

        plan = get_object_or_404(Plan.objects.filter(is_active=True), id=plan_id)
        previous_plan_name = service.plan.name if service.plan else None

        _sync_service_to_plan(service, plan)
        service.updated_by = request.user
        service.save()

        CustomerNotes.objects.create(
            customer=customer,
            note=(
                f"Service plan changed from {previous_plan_name or 'No Plan'} "
                f"to {plan.name}."
            ),
            note_type='SERVICE_ISSUE',
            created_by=request.user
        )

        return Response({
            'status': 'success',
            'message': f'Plan changed to {plan.name} successfully.',
            'service': {
                'id': service.id,
                'plan_id': service.plan_id,
                'plan_name': service.plan.name if service.plan else None,
                'monthly_price': str(service.monthly_price),
                'download_speed': service.download_speed,
                'upload_speed': service.upload_speed,
                'data_cap': service.data_cap,
                'auth_connection_type': service.auth_connection_type,
            }
        })

    @action(detail=True, methods=['post'])
    def toggle_radius(self, request, pk=None):
        """
        P5: Disable/Enable RADIUS access without deleting the customer.
        
        POST /customers/{id}/toggle_radius/
        Body: { "enabled": true/false, "reason": "optional reason" }
        
        Kill switch: immediately blocks/restores FreeRADIUS authentication.
        """
        customer = self.get_object()

        if not hasattr(customer, 'radius_credentials'):
            return Response(
                {'error': 'This customer has no RADIUS credentials.'},
                status=status.HTTP_400_BAD_REQUEST
            )

        credentials = customer.radius_credentials
        enabled = request.data.get('enabled')
        reason = request.data.get('reason', '')

        if enabled is None:
            # Toggle: flip the current state
            enabled = not credentials.is_enabled

        if enabled:
            credentials.is_enabled = True
            credentials.disabled_reason = ''
            credentials.save()
            action_label = 'enabled'
        else:
            credentials.is_enabled = False
            credentials.disabled_reason = reason or 'Manually disabled by admin'
            credentials.save()

            # Belt-and-suspenders: explicitly disconnect active sessions
            try:
                from apps.radius.services.radius_sync_service import RadiusSyncService
                sync = RadiusSyncService()
                terminated = sync.disconnect_user(credentials.username)
                if terminated:
                    logger.info(f"Disconnected {terminated} active session(s) for {credentials.username}")
            except Exception as e:
                logger.warning(f"Failed to disconnect sessions for {credentials.username}: {e}")

            action_label = 'disabled'

        logger.info(
            f"RADIUS {action_label} for customer {customer.customer_code} "
            f"by {request.user}. Reason: {reason}"
        )

        return Response({
            'status': 'success',
            'message': f'RADIUS access {action_label} for {customer.customer_code}',
            'is_enabled': credentials.is_enabled,
            'username': credentials.username,
        })


class CustomerAddressViewSet(viewsets.ModelViewSet):
    """ViewSet for managing customer addresses"""
    serializer_class = CustomerAddressSerializer
    permission_classes = [IsAuthenticated, CustomerAccessPermission]
    pagination_class = StandardResultsSetPagination
    
    def get_queryset(self):
        customer_id = self.kwargs.get('customer_pk')
        customer = get_object_or_404(Customer, pk=customer_id)
        
        # Check permissions
        self.check_object_permissions(self.request, customer)
        
        return CustomerAddress.objects.filter(customer=customer)
    
    def get_serializer_class(self):
        if self.action in ['create', 'update']:
            return CustomerAddressCreateSerializer
        return CustomerAddressSerializer
    
    def perform_create(self, serializer):
        customer_id = self.kwargs.get('customer_pk')
        customer = get_object_or_404(Customer, pk=customer_id)
        serializer.save(customer=customer)
    
    @action(detail=True, methods=['post'])
    def set_primary(self, request, customer_pk=None, pk=None):
        """Set address as primary for its type"""
        address = self.get_object()
        address.is_primary = True
        address.save()
        return Response({'status': 'Address set as primary'})


class CustomerDocumentViewSet(viewsets.ModelViewSet):
    """ViewSet for managing customer documents"""
    serializer_class = CustomerDocumentSerializer
    permission_classes = [IsAuthenticated, CustomerAccessPermission]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['document_type', 'verified']
    
    def get_queryset(self):
        customer_id = self.kwargs.get('customer_pk')
        customer = get_object_or_404(Customer, pk=customer_id)
        
        # Check permissions
        self.check_object_permissions(self.request, customer)
        
        return CustomerDocument.objects.filter(customer=customer)
    
    def get_serializer_class(self):
        if self.action in ['create', 'update']:
            return DocumentUploadSerializer
        return CustomerDocumentSerializer
    
    def perform_create(self, serializer):
        customer_id = self.kwargs.get('customer_pk')
        customer = get_object_or_404(Customer, pk=customer_id)
        serializer.save(customer=customer)
    
    @action(detail=True, methods=['post'])
    def verify(self, request, customer_pk=None, pk=None):
        """Verify a document"""
        document = self.get_object()
        document.verified = True
        document.verified_by = request.user
        document.verified_at = timezone.now()
        document.verification_notes = request.data.get('notes', '')
        document.save()
        
        return Response({'status': 'Document verified successfully'})
    
    @action(detail=False, methods=['get'])
    def types(self, request, customer_pk=None):
        """Get available document types"""
        from utils.constants import DOCUMENT_TYPE_CHOICES
        return Response({'document_types': DOCUMENT_TYPE_CHOICES})


class NextOfKinViewSet(viewsets.ModelViewSet):
    """ViewSet for managing next of kin"""
    serializer_class = NextOfKinSerializer
    permission_classes = [IsAuthenticated, CustomerAccessPermission]
    
    def get_queryset(self):
        customer_id = self.kwargs.get('customer_pk')
        customer = get_object_or_404(Customer, pk=customer_id)
        
        # Check permissions
        self.check_object_permissions(self.request, customer)
        
        return NextOfKin.objects.filter(customer=customer)
    
    def perform_create(self, serializer):
        customer_id = self.kwargs.get('customer_pk')
        customer = get_object_or_404(Customer, pk=customer_id)
        serializer.save(customer=customer)


class CustomerNotesViewSet(viewsets.ModelViewSet):
    """ViewSet for managing customer notes"""
    serializer_class = CustomerNotesSerializer
    permission_classes = [IsAuthenticated, CustomerAccessPermission]
    filter_backends = [DjangoFilterBackend, filters.OrderingFilter]
    filterset_fields = ['note_type', 'priority', 'requires_followup']
    ordering_fields = ['created_at', 'priority']
    ordering = ['-created_at']
    
    def get_queryset(self):
        customer_id = self.kwargs.get('customer_pk')
        customer = get_object_or_404(Customer, pk=customer_id)
        
        # Check permissions
        self.check_object_permissions(self.request, customer)
        
        # Filter based on user role
        queryset = CustomerNotes.objects.filter(customer=customer)
        
        # Customers can only see non-internal notes
        if self.request.user.role == 'CUSTOMER':
            queryset = queryset.filter(internal_only=False)
        
        return queryset
    
    def perform_create(self, serializer):
        customer_id = self.kwargs.get('customer_pk')
        customer = get_object_or_404(Customer, pk=customer_id)
        serializer.save(customer=customer, created_by=self.request.user)
    
    @action(detail=True, methods=['post'])
    def mark_followup_completed(self, request, customer_pk=None, pk=None):
        """Mark followup as completed"""
        note = self.get_object()
        note.followup_completed = True
        note.save()
        return Response({'status': 'Followup marked as completed'})