# apps/customers/views/service_views.py
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, BasePermission
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
from apps.core.models import AuditLog
from apps.core.permissions import HasRoleAccessPolicy, IsAdminOrStaff, IsTechnician
from apps.network.models import IPAddress, IPPool
from apps.billing.models import Plan
from utils.pagination import StandardResultsSetPagination

logger = logging.getLogger(__name__)


def sync_service_plan_fields(service, plan):
    """Keep service configuration aligned with the chosen plan."""
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


class IsAdminStaffOrTechnician(BasePermission):
    """Combined permission: allows admin, staff, or technician roles."""
    def has_permission(self, request, view):
        return (
            IsAdminOrStaff().has_permission(request, view) or
            IsTechnician().has_permission(request, view)
        )


class ServiceConnectionViewSet(viewsets.ModelViewSet):
    """ViewSet for managing service connections - company filtered"""
    queryset = ServiceConnection.objects.select_related(
        'customer', 'customer__user', 'installation_address'
    ).all()
    
    serializer_class = ServiceConnectionSerializer
    permission_classes = [IsAuthenticated, CustomerAccessPermission]
    required_rbac_path = "/admin/users"
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
        if self.action in ['create', 'update', 'partial_update', 'destroy', 'change_plan']:
            return [IsAuthenticated(), IsAdminOrStaff(), HasRoleAccessPolicy()]
        elif self.action in ['activate', 'suspend', 'terminate', 'extend', 'change_ip']:
            return [IsAuthenticated(), IsAdminStaffOrTechnician(), HasRoleAccessPolicy()]
        return [IsAuthenticated(), CustomerAccessPermission()]
    
    def get_queryset(self):
        """
        Always scope to the customer in the nested URL first, then apply role-based filtering.
        - Superuser/staff: sees all services for that customer (or all if no customer_pk)
        - Company admin/staff/technician: same (tenant-level)
        - Customer: only their own services
        """
        qs = super().get_queryset()
        user = self.request.user

        # Always scope to the customer in the nested URL first
        customer_pk = self.kwargs.get('customer_pk')
        if customer_pk:
            qs = qs.filter(customer_id=customer_pk)

        # With django-tenants, schema-level scoping handles tenant isolation.
        if user.is_superuser or user.is_staff:
            return qs

        if hasattr(user, 'role') and user.role in ('admin', 'staff', 'technician'):
            return qs

        if hasattr(user, 'customer_profile'):
            return qs.filter(customer=user.customer_profile)

        return qs.none()
    
    def perform_create(self, serializer):
        """
        Auto-assign customer when creating service
        With django-tenants, tenant scoping is automatic
        """
        customer_pk = self.kwargs.get('customer_pk')
        if customer_pk:
            customer = get_object_or_404(Customer, pk=customer_pk)
            service = serializer.save(customer=customer)
        else:
            service = serializer.save()
        AuditLog.log_action(
            user=self.request.user,
            action="create",
            model_name="Customer Service",
            object_id=str(service.id),
            object_repr=f"{service.customer.customer_code} - {service.service_type}",
            changes={
                "customer": service.customer.customer_code,
                "service_type": service.service_type,
                "status": service.status,
                "plan": getattr(service.plan, "name", None),
            },
            ip_address=self.request.META.get("REMOTE_ADDR"),
            user_agent=self.request.META.get("HTTP_USER_AGENT", ""),
            tenant=getattr(self.request, "tenant", None),
        )

    def perform_update(self, serializer):
        before = {
            "status": serializer.instance.status,
            "plan": getattr(serializer.instance.plan, "name", None),
            "service_type": serializer.instance.service_type,
        }
        service = serializer.save(updated_by=self.request.user)
        if 'plan' in serializer.validated_data and service.plan:
            sync_service_plan_fields(service, service.plan)
            service.updated_by = self.request.user
            service.save()
        AuditLog.log_action(
            user=self.request.user,
            action="update",
            model_name="Customer Service",
            object_id=str(service.id),
            object_repr=f"{service.customer.customer_code} - {service.service_type}",
            changes={
                "before": before,
                "after": {
                    "status": service.status,
                    "plan": getattr(service.plan, "name", None),
                    "service_type": service.service_type,
                },
            },
            ip_address=self.request.META.get("REMOTE_ADDR"),
            user_agent=self.request.META.get("HTTP_USER_AGENT", ""),
            tenant=getattr(self.request, "tenant", None),
        )

    def perform_destroy(self, instance):
        object_id = str(instance.id)
        object_repr = f"{instance.customer.customer_code} - {instance.service_type}"
        changes = {
            "customer": instance.customer.customer_code,
            "service_type": instance.service_type,
            "status": instance.status,
            "plan": getattr(instance.plan, "name", None),
        }
        instance.delete()
        AuditLog.log_action(
            user=self.request.user,
            action="delete",
            model_name="Customer Service",
            object_id=object_id,
            object_repr=object_repr,
            changes=changes,
            ip_address=self.request.META.get("REMOTE_ADDR"),
            user_agent=self.request.META.get("HTTP_USER_AGENT", ""),
            tenant=getattr(self.request, "tenant", None),
        )
    
    def _assign_ip_from_pool(self, service, customer):
        """
        Helper method to assign an IP from the plan's pool to the service.
        Returns the assigned IP address or None if no pool/available IP.
        """
        if not service.plan or not service.plan.ip_pool:
            logger.warning(f"No IP pool assigned to plan for service {service.id}")
            return None
        
        pool = service.plan.ip_pool
        
        available_ip = IPAddress.objects.filter(
            ip_pool=pool,
            status='AVAILABLE'
        ).first()
        
        if available_ip:
            available_ip.assign_to_customer(customer, service_connection=service)
            service.ip_address = available_ip.ip_address
            service.save(update_fields=['ip_address'])
            
            logger.info(
                f"Assigned IP {available_ip.ip_address} from pool '{pool.name}' "
                f"to service {service.id} for customer {customer.customer_code}"
            )
            
            return available_ip.ip_address
        else:
            logger.error(
                f"No available IPs in pool '{pool.name}' for service {service.id}. "
                f"Pool stats: total={pool.total_ips}, used={pool.used_ips}"
            )
            return None

    @action(detail=True, methods=['post'])
    def activate(self, request, customer_pk=None, pk=None):
        """
        Activate a service OR record a payment for an already-active service.
        
        This endpoint now handles two scenarios:
        1. Activating a PENDING service (with optional initial payment)
        2. Recording a payment against an already ACTIVE service
        
        POST /customers/{customer_pk}/services/{pk}/activate/
        Body:
        {
            "record_payment": true,           # optional, default false
            "payment_amount": 2500.00,        # required if record_payment=true
            "payment_method_id": 3,           # optional, defaults to CASH
            "payment_reference": "CASH-001",  # optional
            "payment_notes": "Payment on installation"  # optional
        }
        """
        from apps.radius.signals_auto_sync import (
            calculate_expiration_from_plan,
            generate_pppoe_username,
            generate_password,
            _get_or_create_bandwidth_profile,
        )
        from apps.radius.models import CustomerRadiusCredentials
        from apps.billing.models.payment_models import Payment, InvoiceItemPayment
        from decimal import Decimal
        from django.db import connection as db_conn
        
        service = self.get_object()
        customer = service.customer
        
        # Track if we actually performed activation
        was_activated = False
        new_expiration = None
        assigned_ip = None
        creds_data = None
        creds_obj = None
        
        # ── STEP 1: Activation block (only if service is NOT already active) ──
        if service.status != 'ACTIVE':
            # Calculate expiration based on plan, starting from NOW
            if service.plan:
                new_expiration = calculate_expiration_from_plan(
                    service.plan, start_time=timezone.now()
                )
            
            # IP ASSIGNMENT
            if service.plan and service.plan.ip_pool:
                assigned_ip = self._assign_ip_from_pool(service, customer)
                if not assigned_ip and service.auth_connection_type in ('PPPOE', 'STATIC'):
                    return Response(
                        {'error': f'No available IPs in pool "{service.plan.ip_pool.name}". Please add more IPs or contact administrator.'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            
            # Activate the service
            service.activate_service(request.user)
            was_activated = True
            
            # Update customer status to ACTIVE if still PENDING/LEAD
            if customer.status in ('PENDING', 'LEAD'):
                customer.status = 'ACTIVE'
                customer.save()
            
            # Create/update RADIUS credentials (wrapped in try/except)
            try:
                has_credentials = False
                try:
                    creds_obj = customer.radius_credentials
                    has_credentials = True
                except CustomerRadiusCredentials.DoesNotExist:
                    has_credentials = False
                
                if has_credentials:
                    creds_obj.expiration_date = new_expiration
                    creds_obj.is_enabled = True
                    creds_obj.disabled_reason = ''
                    
                    if assigned_ip and service.auth_connection_type == 'STATIC':
                        creds_obj.framed_ip_address = assigned_ip
                    
                    creds_obj.save()
                    
                    logger.info(
                        f"Activated service {service.id} for {customer.customer_code}: "
                        f"Updated existing RADIUS credentials."
                    )
                else:
                    auth_type = (service.auth_connection_type or '').upper()
                    if auth_type in ['PPPOE', 'HOTSPOT', 'STATIC']:
                        username = generate_pppoe_username(customer)
                        password = generate_password(8)
                        conn_type = 'PPPOE' if auth_type == 'PPPOE' else (
                            'HOTSPOT' if auth_type == 'HOTSPOT' else 'STATIC'
                        )
                        profile = _get_or_create_bandwidth_profile(service) if service.plan else None
                        
                        credentials_data = {
                            'customer': customer,
                            'username': username,
                            'password': password,
                            'bandwidth_profile': profile,
                            'connection_type': conn_type,
                            'is_enabled': True,
                            'simultaneous_use': 1,
                            'expiration_date': new_expiration,
                        }
                        
                        if assigned_ip and auth_type == 'STATIC':
                            credentials_data['framed_ip_address'] = assigned_ip
                        
                        creds_obj = CustomerRadiusCredentials.objects.create(**credentials_data)
                        
                        logger.info(
                            f"Activated service {service.id} for {customer.customer_code}: "
                            f"Created RADIUS credentials username={username}."
                        )
                
                # Refresh for response
                customer.refresh_from_db()
                creds_obj = customer.radius_credentials
                creds_data = {
                    'username': creds_obj.username,
                    'expiration': creds_obj.expiration_date.isoformat() if creds_obj.expiration_date else None,
                    'is_enabled': creds_obj.is_enabled,
                }
                
                # ────────────────────────────────────────────────────────────
                # SEND WELCOME SMS WITH PPPOE CREDENTIALS
                # ────────────────────────────────────────────────────────────
                # Send welcome SMS with username/password for new activations
                try:
                    from apps.messaging.services.notification_sender import SMSNotifier
                    
                    SMSNotifier.pppoe_welcome(
                        customer=customer,
                        username=creds_obj.username,
                        password=creds_obj.password
                    )
                    logger.info(f"Welcome SMS sent to customer {customer.id} with PPPoE credentials")
                    
                except Exception as e:
                    logger.warning(f"Welcome SMS failed for customer {customer.id}: {e}")
                # ────────────────────────────────────────────────────────────
                
                # ============================================================
                # REMOVED: pppoe_resumed SMS call (toggle no longer exists)
                # The service resumed notification is no longer supported.
                # ============================================================
                
                # ────────────────────────────────────────────────────────────
                # MANUAL ADMIN ACTIVATION: SYNCHRONISE SUBSCRIPTION MODEL
                # ────────────────────────────────────────────────────────────
                try:
                    from apps.billing.models.subscription_models import Subscription
                    from django.db import connection as db_conn
                    
                    # Deactivate old historical tracking entries
                    Subscription.objects.filter(customer=customer, status='ACTIVE').update(status='EXPIRED')
                    
                    expiry_for_sub = None
                    if service.plan:
                        expiry_for_sub = service.plan.calculate_expiration(start_time=timezone.now())
                        # Also update RADIUS credentials to be absolutely certain
                        if creds_obj:
                            creds_obj.expiration_date = expiry_for_sub
                            creds_obj.save(update_fields=['expiration_date'])
                            try:
                                creds_obj.sync_to_radius()
                            except Exception as sync_err:
                                logger.warning(f"RADIUS sync after subscription creation failed: {sync_err}")
                    
                    Subscription.objects.create(
                        customer=customer,
                        service_connection=service,
                        plan=service.plan,
                        payment=None,  # Manual admin action override flag
                        amount_paid=service.monthly_price or 0,
                        status='ACTIVE',
                        started_at=timezone.now(),
                        expires_at=expiry_for_sub,
                        schema_name=db_conn.schema_name,
                    )
                    logger.info(f"Subscription record created for manual activation of {customer.customer_code}")
                except Exception as sub_err:
                    logger.error(f"Manual backfill subscription logging failed for {customer.customer_code}: {sub_err}")
                # ────────────────────────────────────────────────────────────
                
            except Exception as radius_err:
                logger.error(
                    f"RADIUS setup failed for service {service.id} "
                    f"(customer {customer.customer_code}): {radius_err}",
                    exc_info=True,
                )
            
            # ============================================================
            # REMOVED: pppoe_new_subscription SMS call (toggle no longer exists)
            # New subscription notification is no longer supported.
            # Only the merged payment/renewal notification should be used.
            # ============================================================
        
        # ── STEP 2: Payment block (runs regardless of activation status) ──
        # This allows recording payments against already-active services
        record_payment = request.data.get('record_payment', False)
        payment_obj = None
        
        if record_payment:
            payment_amount = request.data.get('payment_amount')
            payment_method_id = request.data.get('payment_method_id')
            payment_reference = request.data.get('payment_reference', '')
            payment_notes = request.data.get(
                'payment_notes', 
                'Manual payment recorded on service activation' if was_activated 
                else 'Manual payment recorded for active service'
            )
            
            if not payment_amount:
                return Response(
                    {'error': 'payment_amount is required when record_payment=true'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            try:
                payment_amount = Decimal(str(payment_amount))
                if payment_amount <= 0:
                    raise ValueError()
            except (ValueError, TypeError):
                return Response(
                    {'error': 'Invalid payment_amount'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Resolve payment method
            pay_method = None
            if payment_method_id:
                try:
                    pay_method = InvoiceItemPayment.objects.get(id=payment_method_id)
                except InvoiceItemPayment.DoesNotExist:
                    return Response(
                        {'error': 'Payment method not found'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            else:
                # Use any active method, preferring the default one
                pay_method = InvoiceItemPayment.objects.filter(
                    schema_name=db_conn.schema_name,
                    is_active=True,
                ).order_by('-is_default', 'id').first()
                
                if not pay_method:
                    # Last resort: create a CASH fallback with a unique code
                    import random as _r, string as _s
                    _code = 'CASH_' + ''.join(_r.choices(_s.ascii_uppercase + _s.digits, k=8))
                    pay_method = InvoiceItemPayment.objects.create(
                        method_type='CASH',
                        schema_name=db_conn.schema_name,
                        name='Cash',
                        code=_code,
                        is_active=True,
                        minimum_amount=Decimal('1.00'),
                        maximum_amount=Decimal('9999999.00'),
                    )
            
            # Create the payment record - UPDATED with proper reference and notes
            payment_obj = Payment.objects.create(
                customer=customer,
                amount=payment_amount,
                payment_method=pay_method,
                status='COMPLETED',
                # Change: Use 'MANUAL' as default when no reference provided
                payment_reference=payment_reference if payment_reference else 'MANUAL',
                # Only set transaction_id if there's a real reference (not 'MANUAL')
                transaction_id=payment_reference if (payment_reference and payment_reference != 'MANUAL') else '',
                payment_date=timezone.now(),
                processed_at=timezone.now(),
                is_reconciled=True,
                reconciled_at=timezone.now(),
                payer_name=customer.full_name,
                payer_phone=customer.user.phone_number if customer.user else '',
                # Updated notes to clearly identify this as a PPPoE service payment
                notes=payment_notes or 'Manual payment - PPPoE service activation',
                created_by=request.user,
                schema_name=db_conn.schema_name,
            )
            
            # REMOVED: manual outstanding_balance reduction here
            # The payment post_save signal in billing/signals.py handles this automatically
            # to avoid double-reducing the balance.
            
            logger.info(
                f"Payment {payment_obj.payment_number} recorded for "
                f"{customer.customer_code}: KES {payment_amount} "
                f"({'during activation' if was_activated else 'for active service'})"
            )
        
        # ── Build response ──
        service.refresh_from_db()
        
        response_data = {
            'status': 'success',
            'was_activated': was_activated,
            'message': (
                f'Service activated for {customer.customer_code}' if was_activated
                else f'Payment recorded for active service of {customer.customer_code}'
            ),
            'activation_date': service.activation_date.isoformat() if service.activation_date else None,
            'billing_account_number': service.billing_account_number,
        }
        
        if creds_data:
            response_data['radius_credentials'] = creds_data
        
        if assigned_ip:
            response_data['assigned_ip'] = assigned_ip
            response_data['ip_pool'] = service.plan.ip_pool.name if service.plan and service.plan.ip_pool else None
        
        if payment_obj:
            response_data['payment'] = {
                'id': payment_obj.id,
                'payment_number': payment_obj.payment_number,
                'amount': float(payment_obj.amount),
                'status': payment_obj.status,
            }

        AuditLog.log_action(
            user=request.user,
            action="update",
            model_name="Service Activation",
            object_id=str(service.id),
            object_repr=f"{customer.customer_code} - {getattr(service.plan, 'name', 'No plan')}",
            changes={
                "customer": customer.customer_code,
                "was_activated": was_activated,
                "plan": getattr(service.plan, "name", None),
                "record_payment": bool(record_payment),
                "payment_amount": str(payment_obj.amount) if payment_obj else None,
                "payment_reference": getattr(payment_obj, "payment_reference", None) if payment_obj else None,
                "expires_at": new_expiration.isoformat() if new_expiration else None,
                "assigned_ip": assigned_ip,
            },
            ip_address=request.META.get("REMOTE_ADDR"),
            user_agent=request.META.get("HTTP_USER_AGENT", ""),
            tenant=getattr(request, "tenant", None),
        )
        
        return Response(response_data)
    
    @action(detail=True, methods=['post'])
    def change_plan(self, request, customer_pk=None, pk=None):
        """
        Change the plan for a service connection.
        
        POST /customers/{customer_pk}/services/{pk}/change_plan/
        Body: { "plan_id": 2 }
        """
        service = self.get_object()
        plan_id = request.data.get('plan_id')

        if not plan_id:
            return Response(
                {'error': 'plan_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )

        new_plan = get_object_or_404(Plan.objects.filter(is_active=True), id=plan_id)
        
        # Capture old plan name BEFORE changing
        old_plan_name = service.plan.name if service.plan else None
        
        # Update the service with new plan
        sync_service_plan_fields(service, new_plan)
        service.updated_by = request.user
        service.save()
        
        # ============================================================
        # REMOVED: pppoe_plan_changed SMS call (toggle no longer exists)
        # Plan change notification is no longer supported.
        # ============================================================

        return Response({
            'status': 'success',
            'message': f'Plan changed from {old_plan_name or "No Plan"} to {new_plan.name}.',
            'service_id': service.id,
            'plan_id': service.plan_id,
            'plan_name': service.plan.name if service.plan else None,
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
        
        for service_type, label in ServiceConnection.SERVICE_TYPE_CHOICES:
            stats['by_type'][label] = services.filter(
                service_type=service_type
            ).count()
        
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
        Extend a service subscription OR set exact expiry date (with optional plan change).
        
        Two modes:
        1. Duration mode: Add time to subscription
           Body: { "duration_amount": 10, "duration_unit": "DAYS", "plan_id": 2 (optional) }
        
        2. Direct expiry mode: Set exact expiry date
           Body: { "expiry_date": "2025-12-31T23:59:59", "plan_id": 2 (optional) }
        """
        from apps.billing.models import Plan
        from apps.radius.signals_auto_sync import (
            _get_or_create_bandwidth_profile, 
            generate_pppoe_username, 
            generate_password
        )
        from apps.radius.models import CustomerRadiusCredentials
        from django.utils.dateparse import parse_datetime
        
        service = self.get_object()
        customer = service.customer
        
        # Handle optional plan change (common to both modes)
        plan_id = request.data.get('plan_id')
        plan_changed = False
        new_plan = None
        old_plan_name = None
        
        if plan_id:
            try:
                new_plan = Plan.objects.get(id=plan_id, is_active=True)
            except Plan.DoesNotExist:
                return Response(
                    {'error': 'Plan not found or inactive.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            if service.plan_id != new_plan.id:
                old_plan_name = service.plan.name if service.plan else 'previous plan'
                service.plan = new_plan
                service.download_speed = new_plan.download_speed or service.download_speed
                service.upload_speed = new_plan.upload_speed or service.upload_speed
                service.monthly_price = new_plan.base_price or service.monthly_price
                service.save()
                plan_changed = True
                logger.info(
                    f"Plan changed for service {service.id}: "
                    f"{old_plan_name} → {new_plan.name}"
                )
        
        # ── NEW: Direct expiry date mode (date-picker) ──────────────────
        expiry_date_str = request.data.get('expiry_date')
        if expiry_date_str:
            try:
                new_expiration = parse_datetime(expiry_date_str)
                if new_expiration is None:
                    return Response(
                        {'error': 'Invalid expiry_date format. Use ISO 8601.'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
                if timezone.is_naive(new_expiration):
                    new_expiration = timezone.make_aware(new_expiration)
            except Exception:
                return Response(
                    {'error': 'Invalid expiry_date format.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Ensure RADIUS credentials exist
            if not hasattr(customer, 'radius_credentials'):
                if service.auth_connection_type in ('PPPOE', 'HOTSPOT', 'STATIC'):
                    username = generate_pppoe_username(customer)
                    password = generate_password()
                    profile = _get_or_create_bandwidth_profile(service)
                    
                    credentials_data = {
                        'customer': customer,
                        'username': username,
                        'password': password,
                        'connection_type': service.auth_connection_type,
                        'bandwidth_profile': profile,
                        'is_enabled': True,
                    }
                    
                    if service.ip_address and service.auth_connection_type == 'STATIC':
                        credentials_data['framed_ip_address'] = service.ip_address
                    
                    credentials = CustomerRadiusCredentials.objects.create(**credentials_data)
                    customer.refresh_from_db()
                    logger.info(f"Auto-created RADIUS credentials for direct expiry: {username}")
                else:
                    return Response(
                        {'error': 'This service type does not use RADIUS credentials.'},
                        status=status.HTTP_400_BAD_REQUEST
                    )
            
            credentials = customer.radius_credentials
            credentials.expiration_date = new_expiration  # SET directly, no stacking
            credentials.is_enabled = True
            credentials.disabled_reason = ''
            
            if plan_changed and new_plan:
                profile = _get_or_create_bandwidth_profile(service)
                if profile:
                    credentials.bandwidth_profile = profile
            
            credentials.save()
            
            # Sync with RADIUS server
            try:
                from apps.radius.services.radius_sync_service import RadiusSyncService
                RadiusSyncService().set_user_expiration(credentials.username, new_expiration)
            except Exception as e:
                logger.warning(f"Direct RADIUS expiration sync failed: {e}")
            
            # Reactivate service if needed
            if service.status in ('SUSPENDED', 'TERMINATED', 'PENDING'):
                service.status = 'ACTIVE'
                service.activation_date = service.activation_date or timezone.now()
                service.save(update_fields=['status', 'activation_date'])
            
            # NOTE: No SMS sent here — this is a manual admin date change,
            # not a payment. Real payment/renewal SMS is sent only from the
            # payment webhooks (webhook_views.py, tuma_webhook_views.py,
            # PaymentViews.mpesa_callback) when money actually lands.
            logger.info(f"Expiry date manually set by admin for customer {customer.id} (no SMS sent)")
            
            # ============================================================
            # REMOVED: pppoe_plan_changed SMS call (toggle no longer exists)
            # ============================================================
            
            logger.info(
                f"Direct expiry set for service {service.id} for {customer.customer_code}: "
                f"Expiry = {new_expiration.isoformat()}"
                f"{f' Plan: {new_plan.name}' if plan_changed else ''}"
            )
            
            return Response({
                'status': 'success',
                'message': f'Expiry set to {new_expiration.strftime("%d %b %Y %H:%M")}' + 
                          (f'. Plan changed to {new_plan.name}' if plan_changed else ''),
                'username': credentials.username,
                'new_expiration': new_expiration.isoformat(),
                'is_enabled': credentials.is_enabled,
                'plan_changed': plan_changed,
                'plan_name': new_plan.name if new_plan else (service.plan.name if service.plan else None),
            })
        # ── END direct expiry date mode ─────────────────────────────────
        
        # ── DURATION-BASED EXTENSION (existing logic) ──
        duration_amount = request.data.get('duration_amount')
        duration_unit = request.data.get('duration_unit', 'DAYS').upper()
        
        if not duration_amount or int(duration_amount) <= 0:
            return Response(
                {'error': 'duration_amount must be a positive integer (or use expiry_date for exact date).'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        duration_amount = int(duration_amount)
        
        if duration_unit not in ('MINUTES', 'HOURS', 'DAYS'):
            return Response(
                {'error': 'duration_unit must be MINUTES, HOURDS, or DAYS.'},
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
        
        # Ensure RADIUS credentials exist
        if not hasattr(customer, 'radius_credentials'):
            if service.auth_connection_type in ('PPPOE', 'HOTSPOT', 'STATIC'):
                username = generate_pppoe_username(customer)
                password = generate_password()
                profile = _get_or_create_bandwidth_profile(service)
                
                credentials_data = {
                    'customer': customer,
                    'username': username,
                    'password': password,
                    'connection_type': service.auth_connection_type,
                    'bandwidth_profile': profile,
                    'is_enabled': True,
                }
                
                if service.ip_address and service.auth_connection_type == 'STATIC':
                    credentials_data['framed_ip_address'] = service.ip_address
                
                credentials = CustomerRadiusCredentials.objects.create(**credentials_data)
                customer.refresh_from_db()
                logger.info(f"Auto-created RADIUS credentials for extend: {username}")
            else:
                return Response(
                    {'error': 'This service type does not use RADIUS credentials.'},
                    status=status.HTTP_400_BAD_REQUEST
                )
        
        credentials = customer.radius_credentials
        now = timezone.now()
        
        if credentials.expiration_date and credentials.expiration_date > now:
            base_time = credentials.expiration_date
        else:
            base_time = now
        
        new_expiration = base_time + delta
        
        credentials.expiration_date = new_expiration
        credentials.is_enabled = True
        credentials.disabled_reason = ''
        
        if plan_changed and new_plan:
            profile = _get_or_create_bandwidth_profile(service)
            if profile:
                credentials.bandwidth_profile = profile
        
        credentials.save()
        
        try:
            from apps.radius.services.radius_sync_service import RadiusSyncService
            sync_service = RadiusSyncService()
            sync_service.set_user_expiration(credentials.username, new_expiration)
        except Exception as e:
            logger.warning(f"Direct RADIUS expiration update failed (signal should cover it): {e}")
        
        if service.status in ('SUSPENDED', 'TERMINATED', 'PENDING'):
            service.status = 'ACTIVE'
            service.activation_date = service.activation_date or now
            service.save()
        
        # NOTE: No SMS sent here — this is a manual admin duration extension,
        # not a payment. Real payment/renewal SMS is sent only from the
        # payment webhooks (webhook_views.py, tuma_webhook_views.py,
        # PaymentViews.mpesa_callback) when money actually lands.
        logger.info(f"Subscription manually extended by admin for customer {customer.id} (no SMS sent)")

        # ============================================================
        # REMOVED: pppoe_plan_changed SMS call (toggle no longer exists)
        # ============================================================
        
        msg_parts = [f'Subscription extended by {human_label}']
        if plan_changed:
            msg_parts.append(f'Plan changed to {new_plan.name}')
        
        logger.info(
            f"Extended service {service.id} for {customer.customer_code} "
            f"by {human_label}. New expiration: {new_expiration.isoformat()}"
            f"{f' Plan: {new_plan.name}' if plan_changed else ''}"
        )
        
        return Response({
            'status': 'success',
            'message': '. '.join(msg_parts),
            'username': credentials.username,
            'previous_expiration': base_time.isoformat(),
            'new_expiration': new_expiration.isoformat(),
            'is_enabled': credentials.is_enabled,
            'plan_changed': plan_changed,
            'plan_name': new_plan.name if new_plan else (service.plan.name if service.plan else None),
        })

    @action(detail=True, methods=['post'])
    def change_ip(self, request, customer_pk=None, pk=None):
        """
        Change the assigned static IP for a PPPoE service connection.
        Releases the old IP back to the pool and assigns the new one.

        POST /customers/{customer_pk}/services/{pk}/change_ip/
        Body: { "assigned_ip_id": <IPAddress pk> }
        """
        from apps.network.models.ipam_models import IPAddress
        from apps.radius.models import CustomerRadiusCredentials

        service = self.get_object()
        customer = service.customer
        new_ip_id = request.data.get('assigned_ip_id')

        if not new_ip_id:
            return Response({'error': 'assigned_ip_id is required'}, status=status.HTTP_400_BAD_REQUEST)

        try:
            new_ip = IPAddress.objects.get(pk=new_ip_id, status='AVAILABLE')
        except IPAddress.DoesNotExist:
            return Response({'error': 'IP address not found or not available'}, status=status.HTTP_404_NOT_FOUND)

        old_ip_str = None

        # Release old IP from RADIUS credentials if present
        try:
            creds = customer.radius_credentials
            if creds.assigned_ip_address:
                old_ip_str = creds.assigned_ip_address.ip_address
                creds.assigned_ip_address.release()
        except CustomerRadiusCredentials.DoesNotExist:
            creds = None
        except Exception as e:
            logger.warning(f"Could not release old IP for service {service.id}: {e}")
            creds = None        # Assign new IP
        new_ip.assign_to_customer(customer, service)
        service.ip_address = new_ip.ip_address
        service.save(update_fields=['ip_address'])

        # Update RADIUS credentials
        if creds:
            try:
                creds.assigned_ip_address = new_ip
                creds.static_ip = new_ip.ip_address
                creds.save()
            except Exception as e:
                logger.warning(f"RADIUS credential IP update failed for service {service.id}: {e}")

        logger.info(
            f"IP changed for customer {customer.customer_code}: "
            f"{old_ip_str or 'none'} → {new_ip.ip_address} (by {request.user})"
        )

        return Response({
            'status': 'success',
            'old_ip': old_ip_str,
            'new_ip': new_ip.ip_address,
            'message': f'IP changed from {old_ip_str or "none"} to {new_ip.ip_address}',
        })
