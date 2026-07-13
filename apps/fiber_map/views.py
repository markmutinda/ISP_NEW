from django.db.models import Count
from django.utils import timezone
from rest_framework import viewsets, permissions
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.permissions import IsCompanyStaff
from .models import NetworkMapElement
from .serializers import NetworkMapElementSerializer


class NetworkMapElementViewSet(viewsets.ModelViewSet):
    queryset = NetworkMapElement.objects.all()
    serializer_class = NetworkMapElementSerializer
    permission_classes = [permissions.IsAuthenticated, IsCompanyStaff]

    def get_queryset(self):
        qs = super().get_queryset()
        element_type = self.request.query_params.get('element_type')
        status_param = self.request.query_params.get('status')
        if element_type:
            qs = qs.filter(element_type=element_type)
        if status_param:
            qs = qs.filter(status=status_param)
        return qs

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user, updated_by=self.request.user)

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)

    @action(detail=True, methods=['post'])
    def report_fault(self, request, pk=None):
        element = self.get_object()
        element.status = 'FAULTY'
        element.severity = request.data.get('severity') or element.severity or 'MEDIUM'
        note = (request.data.get('note') or '').strip()
        if note:
            stamp = timezone.now().strftime('%Y-%m-%d %H:%M')
            element.notes = f"{element.notes}\n[{stamp}] {note}".strip()
        element.resolved_at = None
        element.updated_by = request.user
        element.save(update_fields=['status', 'severity', 'notes', 'resolved_at', 'updated_by', 'updated_at'])
        return Response(self.get_serializer(element).data)

    @action(detail=True, methods=['post'])
    def mark_resolved(self, request, pk=None):
        element = self.get_object()
        element.status = 'ACTIVE'
        element.resolved_at = timezone.now()
        element.updated_by = request.user
        element.save(update_fields=['status', 'resolved_at', 'updated_by', 'updated_at'])
        return Response(self.get_serializer(element).data)

    @action(detail=False, methods=['get'])
    def summary(self, request):
        qs = self.get_queryset()
        return Response({
            'total': qs.count(),
            'faulty': qs.filter(status='FAULTY').count(),
            'active': qs.filter(status='ACTIVE').count(),
            'planned': qs.filter(status='PLANNED').count(),
            'by_type': list(qs.values('element_type').annotate(count=Count('id')).order_by('element_type')),
        })