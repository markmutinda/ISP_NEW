"""
Dispatch views – TechnicianViewSet & DispatchJobViewSet
"""
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.utils import timezone
from django.core.mail import send_mail

from .models import Technician, DispatchJob
from .serializers_dispatch import (
    TechnicianSerializer,
    TechnicianCreateSerializer,
    DispatchJobSerializer,
    AssignJobSerializer,
    UpdateStatusSerializer,
    NotifyTechnicianSerializer,
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

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        technician = serializer.save()
        return Response(
            TechnicianSerializer(technician, context={'request': request}).data,
            status=status.HTTP_201_CREATED,
        )

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

    @action(detail=True, methods=['post'], url_path='notify-technician')
    def notify_technician(self, request, pk=None):
        """Send the prepared job assignment message to the assigned technician."""
        job = self.get_object()
        if not job.assigned_to:
            return Response(
                {'detail': 'Assign a technician before sending a notification.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        serializer = NotifyTechnicianSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        channels = serializer.validated_data.get('channels') or ['sms']
        technician = job.assigned_to
        tech_user = technician.user

        default_sms = (
            f"Hi {technician.name}, you have been assigned {job.job_number}: "
            f"{job.get_job_type_display()} for {job.customer_name} on "
            f"{job.scheduled_date} at {job.scheduled_time or 'TBA'}. "
            f"Phone: {job.customer_phone or 'N/A'}."
        )
        sms_message = serializer.validated_data.get('sms_message') or default_sms
        email_subject = serializer.validated_data.get('email_subject') or f"Dispatch assignment {job.job_number}"
        email_body = serializer.validated_data.get('email_body') or (
            f"Hi {technician.name},\n\n"
            f"You have been assigned {job.job_number}.\n\n"
            f"Customer: {job.customer_name}\n"
            f"Phone: {job.customer_phone or 'N/A'}\n"
            f"Address: {job.customer_address or 'N/A'}\n"
            f"Scheduled: {job.scheduled_date} at {job.scheduled_time or 'TBA'}\n"
            f"Priority: {job.get_priority_display()}\n\n"
            f"Description:\n{job.description or 'No description provided.'}\n"
        )

        result = {'sms': None, 'email': None}

        if 'sms' in channels:
            try:
                from apps.messaging.services.notification_sender import _dispatch, _log_sms
                sms_sent = _dispatch(technician.phone, sms_message)
                _log_sms(
                    technician.phone,
                    sms_message,
                    status='sent' if sms_sent else 'failed',
                    msg_type='dispatch',
                    recipient_name=technician.name,
                )
                result['sms'] = sms_sent
            except Exception as exc:
                result['sms'] = False
                result['sms_error'] = str(exc)

        if 'email' in channels:
            if not tech_user.email:
                result['email'] = False
                result['email_error'] = 'Technician has no email address.'
            else:
                try:
                    sent = send_mail(
                        email_subject,
                        email_body,
                        None,
                        [tech_user.email],
                        fail_silently=False,
                    )
                    result['email'] = sent > 0
                except Exception as exc:
                    result['email'] = False
                    result['email_error'] = str(exc)

        return Response(result)
