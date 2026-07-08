from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Q, Count, Avg, F, ExpressionWrapper, DurationField
from django.utils import timezone
from datetime import timedelta
from django_filters.rest_framework import DjangoFilterBackend
import logging

from .models import SupportTicket, SupportTicketMessage
from .serializers import (
    SupportTicketListSerializer,
    SupportTicketDetailSerializer,
    TicketCreateSerializer,
    TicketUpdateSerializer,
    SupportTicketMessageSerializer,
    TicketReplySerializer,
    TicketAssignSerializer,
    TicketStatusSerializer,
    TicketStatsSerializer,
)

from apps.core.permissions import HasRoleAccessPolicy, IsAdminOrStaff, IsCustomer

logger = logging.getLogger(__name__)


class SupportTicketViewSet(viewsets.ModelViewSet):
    """
    API endpoint for support tickets - full CRUD + custom actions
    Matches frontend requirements exactly
    """
    queryset = SupportTicket.objects.none()  # will be overridden
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['status', 'priority', 'category', 'assigned_to']
    search_fields = [
        'ticket_number',
        'subject',
        'description',
        'customer__user__first_name',
        'customer__user__last_name',
        'customer__user__email',
    ]
    ordering_fields = ['created_at', 'updated_at', 'priority', 'status']
    ordering = ['-created_at']
    required_rbac_path = "/admin/tickets"

    def get_required_rbac_path(self, request):
        customer_actions = {"list", "retrieve", "messages", "reply", "create", "my"}
        if getattr(request.user, "role", None) == "customer" and self.action in customer_actions:
            return None
        return self.required_rbac_path

    def get_queryset(self):
        user = self.request.user
        qs = SupportTicket.objects.select_related(
            'customer', 'customer__user', 'assigned_to'
        ).prefetch_related('messages')

        # Query param filters (applied for everyone)
        for field in ['status', 'priority', 'category', 'assigned_to']:
            value = self.request.query_params.get(field)
            if value:
                qs = qs.filter(**{field: value})

        search = self.request.query_params.get('search')
        if search:
            qs = qs.filter(
                Q(ticket_number__icontains=search)
                | Q(subject__icontains=search)
                | Q(description__icontains=search)
                | Q(customer__user__first_name__icontains=search)
                | Q(customer__user__last_name__icontains=search)
                | Q(customer__user__email__icontains=search)
            )

        # Role-based visibility
        if user.is_authenticated:
            if user.role == 'customer':
                try:
                    customer = user.customer  # assuming reverse relation exists
                    qs = qs.filter(customer=customer)
                except AttributeError:
                    qs = SupportTicket.objects.none()
            # admins/staff see everything (or can be restricted further if needed)
        else:
            qs = SupportTicket.objects.none()

        return qs

    def get_serializer_class(self):
        if self.action == 'list':
            return SupportTicketListSerializer
        if self.action == 'retrieve':
            return SupportTicketDetailSerializer
        if self.action == 'create':
            return TicketCreateSerializer
        if self.action in ['update', 'partial_update']:
            return TicketUpdateSerializer
        if self.action == 'reply':
            return TicketReplySerializer
        if self.action == 'assign':
            return TicketAssignSerializer
        if self.action == 'status':
            return TicketStatusSerializer
        if self.action == 'stats':
            return TicketStatsSerializer
        # fallback for other custom actions
        return SupportTicketDetailSerializer

    def get_permissions(self):
        if self.action in ['list', 'stats']:
            # Everyone authenticated can list (but filtered by role)
            return [IsAuthenticated(), HasRoleAccessPolicy()]
        if self.action in ['retrieve', 'messages', 'reply']:
            return [IsAuthenticated(), HasRoleAccessPolicy()]
        if self.action in ['create', 'my']:
            return [IsAuthenticated(), HasRoleAccessPolicy()]
        if self.action in ['update', 'partial_update', 'destroy', 'assign', 'status', 'escalate']:
            return [IsAuthenticated(), IsAdminOrStaff(), HasRoleAccessPolicy()]
        return [IsAuthenticated(), HasRoleAccessPolicy()]

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        user = request.user

        if user.role == 'customer':
            try:
                customer = user.customer
            except AttributeError:
                return Response(
                    {"detail": "Customer profile not found"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            ticket = serializer.save(customer=customer)
        else:
            # Admin/staff: read customer_id from request body
            customer_id = request.data.get('customer_id') or request.data.get('customer')
            if not customer_id:
                return Response(
                    {"detail": "customer_id is required when creating a ticket as admin."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            from apps.customers.models import Customer
            try:
                customer = Customer.objects.get(pk=customer_id)
            except Customer.DoesNotExist:
                return Response(
                    {"detail": f"Customer with id {customer_id} not found."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            ticket = serializer.save(customer=customer)

        SupportTicketMessage.objects.create(
            ticket=ticket,
            sender_type='customer' if user.role == 'customer' else 'agent',
            sender=user,
            message=ticket.description,
            is_internal=False,
        )

        return Response(
            SupportTicketDetailSerializer(ticket, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=['post'], url_path='assign')
    def assign(self, request, pk=None):
        ticket = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        assigned_to_id = serializer.validated_data['assigned_to']

        try:
            from django.contrib.auth import get_user_model

            User = get_user_model()
            agent = User.objects.get(id=assigned_to_id, role__in=['admin', 'agent', 'support'])
        except User.DoesNotExist:
            return Response(
                {"detail": "Valid agent/support user not found"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        ticket.assigned_to = agent
        ticket.save(update_fields=['assigned_to'])

        SupportTicketMessage.objects.create(
            ticket=ticket,
            sender_type='system',
            sender=request.user,
            message=f"Ticket assigned to {agent.get_full_name()} by {request.user.get_full_name()}",
            is_internal=False,
        )

        return Response(
            {
                "status": "assigned",
                "assigned_to": agent.id,
                "assigned_to_name": agent.get_full_name(),
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=['post'], url_path='status')
    def status(self, request, pk=None):
        ticket = self.get_object()
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        new_status = serializer.validated_data['status']
        old_status = ticket.status

        ticket.status = new_status

        if new_status == 'resolved' and not ticket.resolved_at:
            ticket.resolved_at = timezone.now()
        if new_status in ['resolved', 'closed'] and not ticket.resolved_at:
            ticket.resolved_at = timezone.now()

        ticket.save(update_fields=['status', 'resolved_at'])

        SupportTicketMessage.objects.create(
            ticket=ticket,
            sender_type='system',
            sender=request.user,
            message=f"Status changed from {old_status} to {new_status}",
            is_internal=False,
        )

        return Response(
            {
                "status": "updated",
                "old_status": old_status,
                "new_status": new_status,
            },
            status=status.HTTP_200_OK,
        )

    @action(detail=True, methods=['post'], url_path='reply')
    def reply(self, request, pk=None):
        ticket = self.get_object()

        if request.user.role == 'customer' and ticket.customer.user != request.user:
            return Response(
                {"detail": "You can only reply to your own tickets"},
                status=status.HTTP_403_FORBIDDEN,
            )

        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        sender_type = 'customer' if request.user.role == 'customer' else 'agent'

        message = SupportTicketMessage.objects.create(
            ticket=ticket,
            sender_type=sender_type,
            sender=request.user,
            message=serializer.validated_data['message'],
            is_internal=serializer.validated_data.get('is_internal', False),
            attachments=serializer.validated_data.get('attachments', []),
        )

        # First agent response time
        if sender_type == 'agent' and not ticket.first_response_at:
            ticket.first_response_at = timezone.now()
            ticket.save(update_fields=['first_response_at'])

        # Bump updated_at - replaced ticket.touch() with explicit save
        ticket.updated_at = timezone.now()
        ticket.save(update_fields=['updated_at', 'first_response_at'])

        return Response(
            SupportTicketMessageSerializer(message).data,
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=['post'], url_path='escalate')
    def escalate(self, request, pk=None):
        ticket = self.get_object()
        old_priority = ticket.priority
        ticket.priority = 'urgent'
        ticket.save(update_fields=['priority'])

        SupportTicketMessage.objects.create(
            ticket=ticket,
            sender_type='system',
            sender=request.user,
            message=f"Ticket escalated from {old_priority} to URGENT priority",
            is_internal=False,
        )

        return Response(
            {"status": "escalated", "new_priority": "urgent"}, status=status.HTTP_200_OK
        )

    @action(detail=True, methods=['get'], url_path='messages')
    def messages(self, request, pk=None):
        ticket = self.get_object()

        if request.user.role == 'customer' and ticket.customer.user != request.user:
            return Response(
                {"detail": "You can only view messages of your own tickets"},
                status=status.HTTP_403_FORBIDDEN,
            )

        qs = ticket.messages.all()
        if request.user.role == 'customer':
            qs = qs.filter(is_internal=False)

        serializer = SupportTicketMessageSerializer(qs.order_by('created_at'), many=True)
        return Response(serializer.data)

    @action(detail=False, methods=['get'], url_path='stats')
    def stats(self, request):
        from django.db.models.functions import Coalesce

        qs = SupportTicket.objects.all()

        today = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = today - timedelta(days=today.weekday())

        base_agg = qs.aggregate(
            total=Count('id'),
            open=Count('id', filter=Q(status='open')),
            in_progress=Count('id', filter=Q(status='in_progress')),
            pending=Count('id', filter=Q(status='pending')),
            resolved=Count('id', filter=Q(status='resolved')),
            closed=Count('id', filter=Q(status='closed')),
            tickets_today=Count('id', filter=Q(created_at__gte=today)),
            tickets_this_week=Count('id', filter=Q(created_at__gte=week_start)),
        )

        # Average resolution time (only resolved tickets)
        resolved_qs = qs.filter(resolved_at__isnull=False)
        avg_resolution = resolved_qs.annotate(
            duration=ExpressionWrapper(
                F('resolved_at') - F('created_at'), output_field=DurationField()
            )
        ).aggregate(avg=Avg('duration'))['avg']

        avg_resolution_str = (
            f"{avg_resolution.total_seconds() / 3600:.1f} hrs" if avg_resolution else "N/A"
        )

        # Average first response time
        responded_qs = qs.filter(first_response_at__isnull=False)
        avg_response = responded_qs.annotate(
            duration=ExpressionWrapper(
                F('first_response_at') - F('created_at'), output_field=DurationField()
            )
        ).aggregate(avg=Avg('duration'))['avg']

        avg_response_str = (
            f"{avg_response.total_seconds() / 3600:.1f} hrs" if avg_response else "N/A"
        )

        # Simple SLA compliance (example: resolved < 24h)
        sla_compliant = resolved_qs.filter(
            resolved_at__lte=F('created_at') + timedelta(hours=24)
        ).count()
        sla_rate = (
            (sla_compliant / resolved_qs.count() * 100) if resolved_qs.exists() else 0
        )

        stats = {
            **base_agg,
            "avg_response_time": avg_response_str,
            "avg_resolution_time": avg_resolution_str,
            "sla_compliance_rate": round(sla_rate, 1),
        }

        return Response(TicketStatsSerializer(stats).data)

    @action(detail=False, methods=['get'], url_path='my')
    def my(self, request):
        if request.user.role != 'customer':
            return Response(
                {"detail": "This endpoint is for customers only"},
                status=status.HTTP_403_FORBIDDEN,
            )

        try:
            customer = request.user.customer
            tickets = SupportTicket.objects.filter(customer=customer)
            serializer = self.get_serializer(tickets, many=True)
            return Response(serializer.data)
        except AttributeError:
            return Response(
                {"detail": "Customer profile not found"},
                status=status.HTTP_404_NOT_FOUND,
            )
