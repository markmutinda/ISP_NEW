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
from apps.core.permissions import IsAdminOrStaff, IsTechnician
from apps.network.models import IPAddress, IPPool
from utils.pagination import StandardResultsSetPagination

logger = logging.getLogger(__name__)


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
        elif self.action in ['activate', 'suspend', 'terminate', 'extend', 'change_ip']:
            return [IsAuthenticated(), IsAdminStaffOrTechnician()]
        return [IsAuthenticated(), CustomerAccessPermission()]
    
    def get_queryset(self):
        """
        - Superuser: sees everything (optional company filter)
        - Company admin/staff: only their company's services
        - Customer: only their own services
        """
        qs = super().get_queryset()
        user = self.request.user
        
        # With django-tenants, schema-level scoping handles tenant isolation.
        # Superusers and staff see all services in the current tenant schema.
        if user.is_superuser or user.is_staff:
            return qs
        
        # Admin/staff roles (tenant-level)
        if hasattr(user, 'role') and user.role in ('admin', 'staff', 'technician'):
            return qs
        
        # Customers see only their own
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
            serializer.save(customer=customer)
        else:
            serializer.save()
    
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
                    credentials = customer.radius_credentials
                    has_credentials = True
                except CustomerRadiusCredentials.DoesNotExist:
                    has_credentials = False
                
                if has_credentials:
                    credentials.expiration_date = new_expiration
                    credentials.is_enabled = True
                    credentials.disabled_reason = ''
                    
                    if assigned_ip and service.auth_connection_type == 'STATIC':
                        credentials.framed_ip_address = assigned_ip
                    
                    credentials.save()
                    
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
                        
                        credentials = CustomerRadiusCredentials.objects.create(**credentials_data)
                        
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
                
            except Exception as radius_err:
                logger.error(
                    f"RADIUS setup failed for service {service.id} "
                    f"(customer {customer.customer_code}): {radius_err}",
                    exc_info=True,
                )
            
            # SMS notification for new activation
            try:
                from apps.messaging.services.notification_sender import SMSNotifier
                SMSNotifier.pppoe_new_subscription(
                    customer=customer,
                    plan_name=service.plan.name if service.plan else "",
                    amount=float(service.monthly_price or 0),
                    expires_at=new_expiration,
                )
            except Exception as e:
                logger.warning(f"New subscription SMS failed: {e}")
        
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
            
            # Reduce outstanding balance
            if customer.outstanding_balance is not None:
                customer.outstanding_balance = max(
                    Decimal('0'),
                    customer.outstanding_balance - payment_amount
                )
                customer.save(update_fields=['outstanding_balance'])
            
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
        
        return Response(response_data)
    
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
        P3: Extend a service subscription by adding time, with optional plan change.
        
        POST /customers/{customer_pk}/services/{pk}/extend/
        Body: {
            "duration_amount": 10,
            "duration_unit": "DAYS",       // MINUTES, HOURS, DAYS
            "plan_id": 2                   // optional — change plan at the same time
        }
        """
        from apps.billing.models import Plan
        from apps.radius.signals_auto_sync import _get_or_create_bandwidth_profile
        from apps.radius.models import CustomerRadiusCredentials
        from apps.radius.signals_auto_sync import (
            _get_or_create_bandwidth_profile, 
            generate_pppoe_username, 
            generate_password
        )
        service = self.get_object()
        customer = service.customer
        
        duration_amount = request.data.get('duration_amount')
        duration_unit = request.data.get('duration_unit', 'DAYS').upper()
        plan_id = request.data.get('plan_id')
        
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
        
        # Handle optional plan change
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
            if service.auth_connection_type in ('PPPOE', 'HOTSPOT', 'STATIC'):
                phone = customer.user.phone_number or ''
                username = generate_pppoe_username(phone, customer.customer_code)
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
        
        try:
            from apps.messaging.services.notification_sender import SMSNotifier
            SMSNotifier.pppoe_renewal(
                customer=customer,
                plan_name=new_plan.name if new_plan else (service.plan.name if service.plan else ""),
                expires_at=credentials.expiration_date,
            )
        except Exception as e:
            logger.warning(f"Renewal SMS failed: {e}")

        if plan_changed and new_plan:
            try:
                from apps.messaging.services.notification_sender import SMSNotifier
                SMSNotifier.pppoe_plan_changed(
                    customer=customer,
                    old_plan=old_plan_name,
                    new_plan=new_plan.name,
                )
            except Exception as e:
                logger.warning(f"Plan change SMS failed: {e}")
        
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
            creds = None

        # Assign new IP
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