"""
Dispatch views – TechnicianViewSet & DispatchJobViewSet
"""
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.utils import timezone

from .models import Technician, DispatchJob
from .serializers_dispatch import (
    TechnicianSerializer,
    TechnicianCreateSerializer,
    DispatchJobSerializer,
    AssignJobSerializer,
    UpdateStatusSerializer,
)


class TechnicianViewSet(viewsets.ModelViewSet):
    """
    CRUD for technicians.
    GET  /staff/technicians/
    POST /staff/technicians/
    GET  /staff/technicians/{id}/
    """
    queryset = Technician.objects.select_related('user').filter(is_active=True)
    serializer_class = TechnicianSerializer
    permission_classes = [IsAuthenticated]

    def get_serializer_class(self):
        if self.action == 'create':
            return TechnicianCreateSerializer
        return TechnicianSerializer

    def perform_destroy(self, instance):
        # Soft delete
        instance.is_active = False
        instance.save(update_fields=['is_active'])


class DispatchJobViewSet(viewsets.ModelViewSet):
    """
    CRUD + custom actions for dispatch jobs.

    Endpoints:
        GET/POST   /staff/dispatch/jobs/
        GET/PATCH  /staff/dispatch/jobs/{id}/
        POST       /staff/dispatch/jobs/{id}/assign/
        POST       /staff/dispatch/jobs/{id}/status/
    """
    queryset = DispatchJob.objects.select_related(
        'customer', 'customer__user', 'assigned_to', 'assigned_to__user', 'ticket'
    ).all()
    serializer_class = DispatchJobSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        params = self.request.query_params

        status_filter = params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)

        job_type = params.get('job_type')
        if job_type:
            qs = qs.filter(job_type=job_type)

        priority = params.get('priority')
        if priority:
            qs = qs.filter(priority=priority)

        technician = params.get('technician')
        if technician:
            qs = qs.filter(assigned_to_id=technician)

        search = params.get('search')
        if search:
            from django.db.models import Q
            qs = qs.filter(
                Q(job_number__icontains=search) |
                Q(customer__user__first_name__icontains=search) |
                Q(customer__user__last_name__icontains=search) |
                Q(description__icontains=search)
            )

        return qs

    @action(detail=True, methods=['post'])
    def assign(self, request, pk=None):
        """Assign a technician to a job."""
        job = self.get_object()
        serializer = AssignJobSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        tech_id = serializer.validated_data['technician_id']
        try:
            technician = Technician.objects.get(pk=tech_id, is_active=True)
        except Technician.DoesNotExist:
            return Response(
                {'detail': 'Technician not found.'},
                status=status.HTTP_404_NOT_FOUND
            )

        job.assigned_to = technician
        job.status = 'assigned'
        job.save(update_fields=['assigned_to', 'status', 'updated_at'])

        return Response(DispatchJobSerializer(job).data)

    @action(detail=True, methods=['post'], url_path='status')
    def update_status(self, request, pk=None):
        """
        Update a job's status.
        Accepted statuses: in_progress, completed, cancelled.
        """
        job = self.get_object()
        serializer = UpdateStatusSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        new_status = serializer.validated_data['status']
        notes = serializer.validated_data.get('notes', '')

        if new_status == 'in_progress':
            job.status = 'in_progress'
            job.started_at = timezone.now()
        elif new_status == 'completed':
            job.status = 'completed'
            job.completed_at = timezone.now()
            # Update technician stats
            if job.assigned_to:
                job.assigned_to.total_jobs_completed += 1
                job.assigned_to.save(update_fields=['total_jobs_completed'])
            # If linked to a ticket, resolve it
            if job.ticket and job.ticket.status not in ('resolved', 'closed'):
                job.ticket.status = 'resolved'
                job.ticket.resolved_at = timezone.now()
                job.ticket.resolution = f"Resolved via dispatch job {job.job_number}"
                job.ticket.save(update_fields=['status', 'resolved_at', 'resolution', 'updated_at'])
        elif new_status == 'cancelled':
            job.status = 'cancelled'

        if notes:
            job.notes = f"{job.notes}\n[{timezone.now().strftime('%Y-%m-%d %H:%M')}] {notes}".strip()

        job.save()
        return Response(DispatchJobSerializer(job).data)
