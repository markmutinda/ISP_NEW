from rest_framework import viewsets, status, permissions, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Q, Count, Avg, F, ExpressionWrapper, DurationField
from django.utils import timezone
from datetime import timedelta
from django_filters.rest_framework import DjangoFilterBackend
import logging

from ..models import Ticket, TicketMessage, TicketActivity, TicketCategory, TicketStatus
from ..serializers import (
    TicketSerializer, TicketDetailSerializer, TicketMessageSerializer,
    TicketActivitySerializer, TicketCategorySerializer, TicketStatusSerializer,
    TicketCreateSerializer, TicketUpdateSerializer
)
from apps.core.permissions import IsAdmin, IsAdminOrStaff, IsCustomer, IsTechnician

logger = logging.getLogger(__name__)


class TicketCategoryViewSet(viewsets.ModelViewSet):
    """ViewSet for ticket categories"""
    queryset = TicketCategory.objects.filter(is_active=True)
    serializer_class = TicketCategorySerializer
    permission_classes = [IsAuthenticated, IsAdmin | IsAdminOrStaff]
    pagination_class = None


class TicketStatusViewSet(viewsets.ModelViewSet):
    """ViewSet for ticket statuses"""
    queryset = TicketStatus.objects.all()
    serializer_class = TicketStatusSerializer
    permission_classes = [IsAuthenticated, IsAdmin | IsAdminOrStaff]
    pagination_class = None


class TicketViewSet(viewsets.ModelViewSet):
    """ViewSet for support tickets"""
    serializer_class = TicketSerializer
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['priority', 'status', 'category', 'assigned_to', 'source_channel']
    search_fields = ['ticket_number', 'subject', 'description', 'customer__user__first_name', 'customer__user__last_name']
    ordering_fields = ['created_at', 'updated_at', 'priority', 'sla_due_at']
    ordering = ['-created_at']
    
    def get_permissions(self):
        """Set permissions based on action"""
        if self.action in ['create', 'retrieve', 'list', 'messages', 'add_message']:
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = [IsAuthenticated, IsAdmin | IsAdminOrStaff]
        return [permission() for permission in permission_classes]
    
    def get_queryset(self):
        """Filter tickets based on user role"""
        user = self.request.user
        
        if user.role in ['admin', 'staff', 'support'] or user.is_superuser:
            # Admin, staff, support can see all tickets
            return Ticket.objects.all()
        elif user.role == 'technician':
            # Technicians can see tickets assigned to them or unassigned
            from ..models import Technician
            try:
                technician = Technician.objects.get(user=user)
                return Ticket.objects.filter(
                    Q(assigned_to=technician) | Q(assigned_to__isnull=True)
                )
            except Technician.DoesNotExist:
                return Ticket.objects.none()
        elif user.role == 'customer':
            # Customer can only see their own tickets
            from apps.customers.models import Customer
            try:
                customer = Customer.objects.get(user=user)
                return Ticket.objects.filter(customer=customer)
            except Customer.DoesNotExist:
                return Ticket.objects.none()
        
        return Ticket.objects.none()
    
    def get_serializer_class(self):
        """Return appropriate serializer class"""
        if self.action == 'create':
            return TicketCreateSerializer
        elif self.action in ['update', 'partial_update']:
            return TicketUpdateSerializer
        elif self.action == 'retrieve':
            return TicketDetailSerializer
        return TicketSerializer
    
    def perform_create(self, serializer):
        """Create ticket with additional logic"""
        user = self.request.user
        
        # Set customer if not provided (for customer users)
        if not serializer.validated_data.get('customer'):
            from apps.customers.models import Customer
            try:
                customer = Customer.objects.get(user=user)
                serializer.validated_data['customer'] = customer
            except Customer.DoesNotExist:
                pass
        
        # Set created_by
        serializer.validated_data['created_by'] = user
        
        # Set company if customer is set
        if serializer.validated_data.get('customer'):
            serializer.validated_data['company'] = serializer.validated_data['customer'].company
        
        ticket = serializer.save()
        
        # Log activity
        TicketActivity.objects.create(
            ticket=ticket,
            activity_type='created',
            description=f'Ticket created by {user.get_full_name()}',
            performed_by=user,
            changes={'status': 'created'},
            company=ticket.company
        )
        
        logger.info(f"Ticket {ticket.ticket_number} created by {user}")
    
    def perform_update(self, serializer):
        """Update ticket with activity logging"""
        old_instance = self.get_object()
        old_status = old_instance.status.name if old_instance.status else None
        
        # Save changes
        ticket = serializer.save()
        
        # Log changes
        changes = {}
        if 'status' in serializer.validated_data:
            new_status = ticket.status.name if ticket.status else None
            changes['status'] = f'{old_status} -> {new_status}'
        
        if 'assigned_to' in serializer.validated_data:
            old_assignee = old_instance.assigned_to.user.get_full_name() if old_instance.assigned_to else 'Unassigned'
            new_assignee = ticket.assigned_to.user.get_full_name() if ticket.assigned_to else 'Unassigned'
            changes['assigned_to'] = f'{old_assignee} -> {new_assignee}'
        
        # Create activity log
        if changes:
            TicketActivity.objects.create(
                ticket=ticket,
                activity_type='updated',
                description=f'Ticket updated by {self.request.user.get_full_name()}',
                performed_by=self.request.user,
                changes=changes,
                company=ticket.company
            )
    
    @action(detail=True, methods=['post'])
    def assign(self, request, pk=None):
        """Assign ticket to a technician"""
        ticket = self.get_object()
        technician_id = request.data.get('technician_id')
        
        if not technician_id:
            return Response(
                {'error': 'technician_id is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            from ..models import Technician
            technician = Technician.objects.get(id=technician_id)
            
            # Check if technician is available
            if not technician.is_available:
                return Response(
                    {'error': 'Technician is not available'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            # Assign ticket
            ticket.assigned_to = technician
            ticket.assigned_at = timezone.now()
            ticket.save()
            
            # Log activity
            TicketActivity.objects.create(
                ticket=ticket,
                activity_type='assigned',
                description=f'Ticket assigned to {technician.user.get_full_name()}',
                performed_by=request.user,
                changes={'assigned_to': technician.user.get_full_name()},
                company=ticket.company
            )
            
            return Response({
                'status': 'success',
                'message': f'Ticket assigned to {technician.user.get_full_name()}',
                'assigned_at': ticket.assigned_at
            })
            
        except Technician.DoesNotExist:
            return Response(
                {'error': 'Technician not found'},
                status=status.HTTP_404_NOT_FOUND
            )
    
    @action(detail=True, methods=['post'])
    def escalate(self, request, pk=None):
        """Escalate ticket to higher level"""
        ticket = self.get_object()
        reason = request.data.get('reason', '')
        
        # Increment escalation level
        ticket.is_escalated = True
        ticket.escalation_level += 1
        ticket.escalation_reason = reason
        ticket.save()
        
        # Log activity
        TicketActivity.objects.create(
            ticket=ticket,
            activity_type='escalated',
            description=f'Ticket escalated to level {ticket.escalation_level}',
            performed_by=request.user,
            changes={'escalation_level': ticket.escalation_level},
            company=ticket.company
        )
        
        return Response({
            'status': 'success',
            'message': f'Ticket escalated to level {ticket.escalation_level}',
            'escalation_level': ticket.escalation_level
        })
    
    @action(detail=True, methods=['post'])
    def close(self, request, pk=None):
        """Close a resolved ticket"""
        ticket = self.get_object()
        
        if ticket.status and ticket.status.is_closed:
            return Response(
                {'error': 'Ticket is already closed'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        resolution_notes = request.data.get('resolution_notes', '')
        resolution_category = request.data.get('resolution_category', '')
        
        # Update ticket
        ticket.resolution_notes = resolution_notes
        ticket.resolution_category = resolution_category
        ticket.resolved_at = timezone.now()
        ticket.resolved_by = request.user
        ticket.closed_at = timezone.now()
        
        # Find closed status or create one
        closed_status, _ = TicketStatus.objects.get_or_create(
            name='Closed',
            defaults={'is_open': False, 'is_closed': True, 'order': 100}
        )
        ticket.status = closed_status
        ticket.save()
        
        # Log activity
        TicketActivity.objects.create(
            ticket=ticket,
            activity_type='closed',
            description=f'Ticket closed by {request.user.get_full_name()}',
            performed_by=request.user,
            changes={'status': 'closed'},
            company=ticket.company
        )
        
        return Response({
            'status': 'success',
            'message': 'Ticket closed successfully',
            'closed_at': ticket.closed_at
        })
    
    @action(detail=False, methods=['get'])
    def dashboard_stats(self, request):
        """Get dashboard statistics for tickets"""
        user = request.user
        
        # Base queryset based on user role
        if user.role in ['admin', 'staff', 'support'] or user.is_superuser:
            tickets = Ticket.objects.all()
        elif user.role == 'technician':
            from ..models import Technician
            try:
                technician = Technician.objects.get(user=user)
                tickets = Ticket.objects.filter(assigned_to=technician)
            except Technician.DoesNotExist:
                tickets = Ticket.objects.none()
        elif user.role == 'customer':
            from apps.customers.models import Customer
            try:
                customer = Customer.objects.get(user=user)
                tickets = Ticket.objects.filter(customer=customer)
            except Customer.DoesNotExist:
                tickets = Ticket.objects.none()
        else:
            tickets = Ticket.objects.none()
        
        # Calculate statistics
        total_tickets = tickets.count()
        open_tickets = tickets.filter(status__is_closed=False).count()
        closed_tickets = tickets.filter(status__is_closed=True).count()
        
        # Average response time
        response_times = tickets.exclude(first_response_at__isnull=True).annotate(
            response_time=ExpressionWrapper(
                F('first_response_at') - F('created_at'),
                output_field=DurationField()
            )
        ).values_list('response_time', flat=True)
        
        avg_response_hours = 0
        if response_times:
            total_seconds = sum([rt.total_seconds() for rt in response_times])
            avg_response_hours = total_seconds / len(response_times) / 3600
        
        # Overdue tickets
        overdue_tickets = tickets.filter(
            sla_due_at__lt=timezone.now(),
            first_response_at__isnull=True,
            status__is_closed=False
        ).count()
        
        # Priority breakdown
        priority_stats = tickets.values('priority').annotate(
            count=Count('id')
        ).order_by('priority')
        
        return Response({
            'total_tickets': total_tickets,
            'open_tickets': open_tickets,
            'closed_tickets': closed_tickets,
            'overdue_tickets': overdue_tickets,
            'average_response_hours': round(avg_response_hours, 2),
            'priority_breakdown': list(priority_stats),
            'time_period': 'all_time'
        })


class TicketMessageViewSet(viewsets.ModelViewSet):
    """ViewSet for ticket messages"""
    serializer_class = TicketMessageSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        """Filter messages based on user role"""
        user = self.request.user
        ticket_id = self.kwargs.get('ticket_pk')
        
        if not ticket_id:
            return TicketMessage.objects.none()
        
        try:
            ticket = Ticket.objects.get(id=ticket_id)
        except Ticket.DoesNotExist:
            return TicketMessage.objects.none()
        
        # Check if user has access to this ticket
        if user.role in ['admin', 'staff', 'support'] or user.is_superuser:
            return TicketMessage.objects.filter(ticket=ticket)
        elif user.role == 'technician':
            from ..models import Technician
            try:
                technician = Technician.objects.get(user=user)
                if ticket.assigned_to == technician:
                    return TicketMessage.objects.filter(ticket=ticket)
            except Technician.DoesNotExist:
                pass
        elif user.role == 'customer':
            from apps.customers.models import Customer
            try:
                customer = Customer.objects.get(user=user)
                if ticket.customer == customer:
                    return TicketMessage.objects.filter(ticket=ticket, is_internal=False)
            except Customer.DoesNotExist:
                pass
        
        return TicketMessage.objects.none()
    
    def perform_create(self, serializer):
        """Create ticket message with additional logic"""
        ticket_id = self.kwargs.get('ticket_pk')
        user = self.request.user
        
        try:
            ticket = Ticket.objects.get(id=ticket_id)
        except Ticket.DoesNotExist:
            return Response(
                {'error': 'Ticket not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Determine sender type
        if user.role in ['admin', 'staff', 'technician', 'support'] or user.is_superuser:
            sender_type = 'technician' if user.role == 'technician' else 'admin'
        else:
            sender_type = 'customer'
        
        # Save message
        message = serializer.save(
            ticket=ticket,
            sender=user,
            sender_type=sender_type,
            company=ticket.company
        )
        
        # Update ticket's updated timestamp
        ticket.updated_at = timezone.now()
        ticket.save()
        
        # Log activity
        TicketActivity.objects.create(
            ticket=ticket,
            activity_type='message_added',
            description=f'New message added by {user.get_full_name()}',
            performed_by=user,
            changes={'message_id': message.id},
            company=ticket.company
        )
        
        logger.info(f"Message added to ticket {ticket.ticket_number} by {user}")


class TicketActivityViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for ticket activities (audit log)"""
    serializer_class = TicketActivitySerializer
    permission_classes = [IsAuthenticated, IsAdmin | IsAdminOrStaff]
    
    def get_queryset(self):
        """Filter activities based on ticket"""
        ticket_id = self.kwargs.get('ticket_pk')
        
        if ticket_id:
            return TicketActivity.objects.filter(ticket_id=ticket_id)
        return TicketActivity.objects.all()