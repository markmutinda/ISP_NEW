from rest_framework import viewsets, status, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Q, Count, Avg, F, ExpressionWrapper, DurationField
from django.utils import timezone
from datetime import timedelta
from django_filters.rest_framework import DjangoFilterBackend

from ..models import Technician, Ticket, TicketActivity
from ..serializers import (
    TechnicianSerializer, TechnicianDetailSerializer,
    TechnicianPerformanceSerializer, TechnicianCreateSerializer,
    TechnicianAvailabilitySerializer
)
from apps.core.permissions import IsAdmin, IsAdminOrStaff, IsTechnician


class TechnicianViewSet(viewsets.ModelViewSet):
    """ViewSet for technicians"""
    queryset = Technician.objects.all()
    serializer_class = TechnicianSerializer
    permission_classes = [IsAuthenticated, IsAdmin | IsAdminOrStaff]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['department', 'is_available']
    search_fields = ['user__first_name', 'user__last_name', 'user__email', 'employee_id']
    
    def get_serializer_class(self):
        """Return appropriate serializer class"""
        if self.action == 'retrieve':
            return TechnicianDetailSerializer
        elif self.action == 'create':
            return TechnicianCreateSerializer
        elif self.action == 'performance':
            return TechnicianPerformanceSerializer
        elif self.action == 'availability':
            return TechnicianAvailabilitySerializer
        return TechnicianSerializer
    
    @action(detail=True, methods=['get'])
    def performance(self, request, pk=None):
        """Get performance metrics for a technician"""
        technician = self.get_object()
        
        # Get performance metrics
        current_month = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        next_month = (current_month + timedelta(days=32)).replace(day=1)
        
        # Current month tickets
        current_month_tickets = Ticket.objects.filter(
            assigned_to=technician,
            created_at__gte=current_month,
            created_at__lt=next_month
        )
        
        # Resolved tickets this month
        resolved_this_month = current_month_tickets.filter(status__is_closed=True)
        
        # Average resolution time this month
        resolution_times = resolved_this_month.annotate(
            resolution_duration=ExpressionWrapper(
                F('resolved_at') - F('created_at'),
                output_field=DurationField()
            )
        ).values_list('resolution_duration', flat=True)
        
        avg_resolution_hours = 0
        if resolution_times:
            total_seconds = sum([rt.total_seconds() for rt in resolution_times])
            avg_resolution_hours = total_seconds / len(resolution_times) / 3600
        
        # Customer satisfaction
        rated_tickets = resolved_this_month.exclude(customer_rating__isnull=True)
        avg_rating = rated_tickets.aggregate(Avg('customer_rating'))['customer_rating__avg'] or 0
        
        # Active tickets
        active_tickets = Ticket.objects.filter(
            assigned_to=technician,
            status__is_closed=False
        ).count()
        
        return Response({
            'technician': TechnicianSerializer(technician).data,
            'metrics': {
                'current_month': {
                    'tickets_assigned': current_month_tickets.count(),
                    'tickets_resolved': resolved_this_month.count(),
                    'resolution_rate': round(
                        (resolved_this_month.count() / current_month_tickets.count() * 100)
                        if current_month_tickets.count() > 0 else 0, 2
                    ),
                    'average_resolution_hours': round(avg_resolution_hours, 2),
                    'average_rating': round(avg_rating, 2)
                },
                'current_load': {
                    'active_tickets': active_tickets,
                    'max_capacity': technician.max_active_tickets,
                    'load_percentage': round(
                        (active_tickets / technician.max_active_tickets * 100)
                        if technician.max_active_tickets > 0 else 0, 2
                    )
                }
            }
        })
    
    @action(detail=True, methods=['post'])
    def set_availability(self, request, pk=None):
        """Set technician availability"""
        technician = self.get_object()
        is_available = request.data.get('is_available')
        
        if is_available is None:
            return Response(
                {'error': 'is_available parameter is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        technician.is_available = is_available
        technician.save()
        
        return Response({
            'status': 'success',
            'message': f'Availability set to {is_available}',
            'is_available': technician.is_available
        })