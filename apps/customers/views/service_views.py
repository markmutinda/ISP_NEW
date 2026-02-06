from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django_filters.rest_framework import DjangoFilterBackend
from django.shortcuts import get_object_or_404
from django.utils import timezone
from datetime import timedelta

import logging

from apps.customers.models import Customer, ServiceConnection
from apps.customers.serializers import (
    ServiceConnectionSerializer, ServiceCreateSerializer,
    ServiceActivationSerializer, ServiceSuspensionSerializer
)
from apps.customers.permissions import CustomerAccessPermission
from apps.core.permissions import IsAdminOrStaff, IsTechnician
from utils.pagination import StandardResultsSetPagination

logger = logging.getLogger(__name__)


class ServiceConnectionViewSet(viewsets.ModelViewSet):
    """ViewSet for managing service connections - company filtered"""
    queryset = ServiceConnection.objects.select_related(
        'customer', 'customer__user', 'installation_address', 'customer__company'
    ).prefetch_related('customer__company')
    
    serializer_class = ServiceConnectionSerializer
    permission_classes = [IsAuthenticated, CustomerAccessPermission]
    filter_backends = [DjangoFilterBackend]
    filterset_fields = ['service_type', 'status', 'connection_type']
    pagination_class = StandardResultsSetPagination
    
    def get_serializer_class(self):
        if self.action == 'create':
            return ServiceCreateSerializer
        elif self.action == 'activate':
            return ServiceActivationSerializer
        elif self.action == 'suspend':
            return ServiceSuspensionSerializer
        return ServiceConnectionSerializer
    
    def get_permissions(self):
        if self.action in ['create', 'update', 'partial_update', 'destroy']:
            return [IsAuthenticated(), IsAdminOrStaff()]
        elif self.action in ['activate', 'suspend', 'terminate']:
            return [IsAuthenticated(), IsAdminOrStaff() | IsTechnician()]
        return [IsAuthenticated(), CustomerAccessPermission()]
    
    def get_queryset(self):
        """
        - Superuser: sees everything (optional company filter)
        - Company admin/staff: only their company's services
        - Customer: only their own services
        """
        qs = super().get_queryset()
        user = self.request.user
        
        if user.is_superuser:
            # Optional: filter by company via query param
            company_id = self.request.query_params.get('company_id')
            if company_id:
                return qs.filter(customer__company_id=company_id)
            return qs
        
        # Company users only see their company's services
        if hasattr(user, 'company') and user.company:
            return qs.filter(customer__company=user.company)
        
        # Customers see only their own
        if hasattr(user, 'customer_profile'):
            return qs.filter(customer=user.customer_profile)
        
        return qs.none()
    
    def perform_create(self, serializer):
        """
        Auto-assign customer when creating service
        With django-tenants, tenant scoping is automatic
        """
        # If customer_pk in URL (nested router), use that customer
        customer_pk = self.kwargs.get('customer_pk')
        if customer_pk:
            customer = get_object_or_404(Customer, pk=customer_pk)
            # With django-tenants, tenant scoping is automatic - no need to check company
            serializer.save(customer=customer)
        else:
            # Fallback - should not happen if using nested router
            serializer.save()
    @action(detail=True, methods=['post'])
    def activate(self, request, customer_pk=None, pk=None):
        """
        P4: Activate a PENDING service — starts the timer NOW.
        
        This is the "Activate Later" workflow:
        - Customer was created with activate_now=False → status=PENDING
        - Admin clicks "Activate" → this endpoint
        - Calculates expiration from NOW (not from create time)
        - Creates/updates RADIUS credentials with correct expiration
        - Syncs to FreeRADIUS
        
        POST /customers/{customer_pk}/services/{pk}/activate/
        """
        from apps.radius.signals_auto_sync import calculate_expiration_from_plan
        
        service = self.get_object()
        
        if service.status == 'ACTIVE':
            return Response(
                {'error': 'Service is already active.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Activate the service (sets status=ACTIVE, activation_date=now)
        service.activate_service(request.user)
        
        # Update customer status to ACTIVE if still PENDING
        customer = service.customer
        if customer.status in ('PENDING', 'LEAD'):
            customer.status = 'ACTIVE'
            customer.save()
        
        # Calculate and set expiration based on plan validity, starting from NOW
        if service.plan:
            new_expiration = calculate_expiration_from_plan(
                service.plan, start_time=timezone.now()
            )
            
            # Update RADIUS credentials with correct expiration
            if hasattr(customer, 'radius_credentials'):
                credentials = customer.radius_credentials
                credentials.expiration_date = new_expiration
                credentials.is_enabled = True
                credentials.disabled_reason = ''
                credentials.save()  # Triggers sync_credentials_to_radius signal
                
                logger.info(
                    f"Activated service {service.id} for {customer.customer_code}: "
                    f"Plan={service.plan.name}, "
                    f"Expiration={new_expiration.isoformat() if new_expiration else 'Unlimited'}"
                )
            else:
                # No RADIUS credentials exist yet — the auto_create_radius_for_service
                # signal should handle this when ServiceConnection status changes to ACTIVE.
                logger.info(
                    f"Activated service {service.id} for {customer.customer_code} "
                    f"(RADIUS credentials will be created by signal)"
                )
        
        return Response({
            'status': 'success',
            'message': f'Service activated for {customer.customer_code}',
            'activation_date': service.activation_date.isoformat() if service.activation_date else None,
            'expiration': (
                customer.radius_credentials.expiration_date.isoformat()
                if hasattr(customer, 'radius_credentials') and customer.radius_credentials.expiration_date
                else None
            ),
        })
    
    @action(detail=True, methods=['post'])
    def suspend(self, request, customer_pk=None, pk=None):
        """Suspend a service"""
        service = self.get_object()
        serializer = self.get_serializer(
            service, 
            data=request.data, 
            partial=True
        )
        
        if serializer.is_valid():
            reason = request.data.get('reason', '')
            service.suspend_service(reason)
            
            return Response(
                {'status': 'Service suspended successfully'},
                status=status.HTTP_200_OK
            )
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    
    @action(detail=True, methods=['post'])
    def terminate(self, request, customer_pk=None, pk=None):
        """Terminate a service"""
        service = self.get_object()
        reason = request.data.get('reason', 'No reason provided')
        
        service.terminate_service(reason)
        
        return Response(
            {'status': 'Service terminated successfully'},
            status=status.HTTP_200_OK
        )
    
    @action(detail=False, methods=['get'])
    def stats(self, request, customer_pk=None):
        """Get service statistics"""
        customer_id = customer_pk
        
        if customer_id:
            customer = get_object_or_404(Customer, pk=customer_id)
            self.check_object_permissions(request, customer)
            
            services = ServiceConnection.objects.filter(customer=customer)
        else:
            # Global stats for admin
            if request.user.role not in ['ADMIN', 'STAFF']:
                return Response(
                    {'error': 'Permission denied'},
                    status=status.HTTP_403_FORBIDDEN
                )
            services = ServiceConnection.objects.all()
        
        stats = {
            'total': services.count(),
            'active': services.filter(status='ACTIVE').count(),
            'pending': services.filter(status='PENDING').count(),
            'suspended': services.filter(status='SUSPENDED').count(),
            'terminated': services.filter(status='TERMINATED').count(),
            'by_type': {},
            'by_connection': {},
        }
        
        # Count by service type
        for service_type, label in ServiceConnection.SERVICE_TYPE_CHOICES:
            stats['by_type'][label] = services.filter(
                service_type=service_type
            ).count()
        
        # Count by connection type
        for conn_type, label in ServiceConnection.CONNECTION_TYPE_CHOICES:
            stats['by_connection'][label] = services.filter(
                connection_type=conn_type
            ).count()
        
        return Response(stats)
    
    @action(detail=False, methods=['get'])
    def pending_activations(self, request):
        """Get services pending activation"""
        if request.user.role not in ['ADMIN', 'STAFF', 'TECHNICIAN']:
            return Response(
                {'error': 'Permission denied'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        pending_services = ServiceConnection.objects.filter(
            status='PENDING'
        ).select_related('customer', 'customer__user')
        
        serializer = self.get_serializer(pending_services, many=True)
        return Response(serializer.data)

    @action(detail=True, methods=['post'])
    def extend(self, request, customer_pk=None, pk=None):
        """
        P3: Extend a service subscription by adding time.
        
        POST /customers/{customer_pk}/services/{pk}/extend/
        Body: {
            "duration_amount": 10,
            "duration_unit": "DAYS"   // MINUTES, HOURS, DAYS
        }
        
        Calculation:
        - If current expiration is in the future: add time to it
        - If current expiration is in the past (expired): add time from NOW
        """
        service = self.get_object()
        customer = service.customer
        
        # Validate input
        duration_amount = request.data.get('duration_amount')
        duration_unit = request.data.get('duration_unit', 'DAYS').upper()
        
        if not duration_amount or int(duration_amount) <= 0:
            return Response(
                {'error': 'duration_amount must be a positive integer.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        duration_amount = int(duration_amount)
        
        if duration_unit not in ('MINUTES', 'HOURS', 'DAYS'):
            return Response(
                {'error': 'duration_unit must be MINUTES, HOURS, or DAYS.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Calculate the delta
        if duration_unit == 'MINUTES':
            delta = timedelta(minutes=duration_amount)
            human_label = f"{duration_amount} minute{'s' if duration_amount != 1 else ''}"
        elif duration_unit == 'HOURS':
            delta = timedelta(hours=duration_amount)
            human_label = f"{duration_amount} hour{'s' if duration_amount != 1 else ''}"
        else:
            delta = timedelta(days=duration_amount)
            human_label = f"{duration_amount} day{'s' if duration_amount != 1 else ''}"
        
        if not hasattr(customer, 'radius_credentials'):
            return Response(
                {'error': 'This customer has no RADIUS credentials to extend.'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        credentials = customer.radius_credentials
        now = timezone.now()
        
        # Determine base time: current expiration or now (if expired/null)
        if credentials.expiration_date and credentials.expiration_date > now:
            base_time = credentials.expiration_date
        else:
            base_time = now
        
        new_expiration = base_time + delta
        
        # Update credentials and sync to RADIUS
        credentials.expiration_date = new_expiration
        credentials.is_enabled = True
        credentials.disabled_reason = ''
        credentials.save()  # Triggers sync_credentials_to_radius signal
        
        # Also update the RADIUS expiration directly via the service
        try:
            from apps.radius.services.radius_sync_service import RadiusSyncService
            sync_service = RadiusSyncService()
            sync_service.set_user_expiration(credentials.username, new_expiration)
        except Exception as e:
            logger.warning(f"Direct RADIUS expiration update failed (signal should cover it): {e}")
        
        # Re-activate service if it was suspended/terminated
        if service.status in ('SUSPENDED', 'TERMINATED', 'PENDING'):
            service.status = 'ACTIVE'
            service.activation_date = service.activation_date or now
            service.save()
        
        logger.info(
            f"Extended service {service.id} for {customer.customer_code} "
            f"by {human_label}. New expiration: {new_expiration.isoformat()}"
        )
        
        return Response({
            'status': 'success',
            'message': f'Subscription extended by {human_label}',
            'username': credentials.username,
            'previous_expiration': base_time.isoformat(),
            'new_expiration': new_expiration.isoformat(),
            'is_enabled': credentials.is_enabled,
        })
