from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.shortcuts import get_object_or_404

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
from utils.pagination import StandardResultsSetPagination


class CustomerViewSet(viewsets.ModelViewSet):
    """ViewSet for managing customers"""
    queryset = Customer.objects.select_related(
        'user', 'created_by', 'updated_by', 'next_of_kin'
    ).prefetch_related(
        'addresses', 'documents', 'services'
    ).all()
    
    permission_classes = [IsAuthenticated, CanManageCustomers]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = [
        'status', 'customer_type', 'category', 
        'gender', 'id_type'
    ]
    search_fields = [
        'customer_code', 'user__first_name', 'user__last_name',
        'user__email', 'user__phone_number', 'id_number'
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
        if self.action in ['create', 'update', 'partial_update', 'destroy']:
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
            return queryset.filter(id=user.customer_profile.id)
        
        # No access
        return queryset.none()
    
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
        
        data = {
            'customer_info': CustomerDetailSerializer(customer).data,
            'stats': {
                'total_services': customer.services.count(),
                'active_services': customer.services.filter(status='ACTIVE').count(),
                'pending_services': customer.services.filter(status='PENDING').count(),
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
