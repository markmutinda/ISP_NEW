from rest_framework import viewsets, status, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.db.models import Q, Sum, Avg, Max, Count
from django.utils import timezone
from datetime import timedelta
import logging

from .models import BandwidthProfile, TrafficRule, DataUsage, BandwidthAlert, SpeedTestResult
from .serializers import (
    BandwidthProfileSerializer,
    TrafficRuleSerializer,
    DataUsageSerializer,
    BandwidthAlertSerializer,
    SpeedTestResultSerializer,
    TrafficAnalysisSerializer
)
from apps.core.permissions import IsAdmin, IsAdminOrStaff, IsCustomer, IsTechnician
from .monitoring.traffic_analyzer import TrafficAnalyzer

logger = logging.getLogger(__name__)


class BandwidthProfileViewSet(viewsets.ModelViewSet):
    """ViewSet for managing bandwidth profiles"""
    queryset = BandwidthProfile.objects.filter(is_active=True)
    serializer_class = BandwidthProfileSerializer
    
    def get_permissions(self):
        """Set permissions based on action"""
        if self.action in ['list', 'retrieve']:
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = [IsAuthenticated, IsAdmin | IsAdminOrStaff]
        return [permission() for permission in permission_classes]
    
    def get_queryset(self):
        """Filter queryset based on user permissions"""
        user = self.request.user
        
        # Superusers and admins see all
        if user.is_superuser or user.role == 'admin':
            return BandwidthProfile.objects.all()
        
        # Staff see profiles from their company
        if user.role in ['staff', 'technician', 'support']:
            if hasattr(user, 'company'):
                return BandwidthProfile.objects.filter(company=user.company)
        
        # Customers see only active profiles
        return BandwidthProfile.objects.filter(is_active=True)
    
    @action(detail=False, methods=['get'])
    def available_for_customer(self, request):
        """Get bandwidth profiles available for customer assignment"""
        customer_id = request.query_params.get('customer_id')
        if not customer_id:
            return Response(
                {'error': 'customer_id parameter required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Check permissions
        if not (request.user.role in ['admin', 'staff'] or request.user.is_superuser):
            return Response(
                {'error': 'Permission denied'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        try:
            from apps.customers.models import Customer
            customer = Customer.objects.get(id=customer_id)
            
            # Get profiles not exceeding customer's location capabilities
            profiles = self.get_queryset().filter(is_active=True)
            serializer = self.get_serializer(profiles, many=True)
            return Response(serializer.data)
            
        except Customer.DoesNotExist:
            return Response(
                {'error': 'Customer not found'},
                status=status.HTTP_404_NOT_FOUND
            )


class TrafficRuleViewSet(viewsets.ModelViewSet):
    """ViewSet for managing traffic rules"""
    queryset = TrafficRule.objects.all()
    serializer_class = TrafficRuleSerializer
    permission_classes = [IsAuthenticated, IsAdmin | IsAdminOrStaff]
    
    def get_queryset(self):
        """Filter queryset based on user permissions"""
        user = self.request.user
        
        # Superusers and admins see all
        if user.is_superuser or user.role == 'admin':
            return TrafficRule.objects.all()
        
        # Staff and technicians see rules from their company
        if user.role in ['staff', 'technician']:
            if hasattr(user, 'company'):
                return TrafficRule.objects.filter(company=user.company)
        
        return TrafficRule.objects.none()
    
    @action(detail=True, methods=['post'])
    def apply_rule(self, request, pk=None):
        """Apply traffic rule to network devices"""
        rule = self.get_object()
        
        try:
            # This would call the appropriate integration
            # For now, just mark as applied
            rule.is_applied = True
            rule.applied_at = timezone.now()
            rule.save()
            
            logger.info(f"Applied traffic rule {rule.name} to devices")
            
            return Response({
                'status': 'success',
                'message': f'Rule {rule.name} applied successfully',
                'applied_at': rule.applied_at
            })
            
        except Exception as e:
            logger.error(f"Error applying rule {rule.name}: {e}")
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=False, methods=['get'])
    def by_device(self, request):
        """Get traffic rules for a specific device"""
        device_id = request.query_params.get('device_id')
        device_type = request.query_params.get('device_type')  # mikrotik, cpe, olt
        
        if not device_id or not device_type:
            return Response(
                {'error': 'device_id and device_type parameters required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        rules = self.get_queryset().filter(
            target_device_id=device_id,
            target_device_type=device_type,
            is_active=True
        )
        
        serializer = self.get_serializer(rules, many=True)
        return Response(serializer.data)


class DataUsageViewSet(viewsets.ReadOnlyModelViewSet):
    """ViewSet for viewing data usage"""
    serializer_class = DataUsageSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        """Filter queryset based on user permissions"""
        user = self.request.user
        
        if user.is_superuser or user.role in ['admin', 'staff']:
            return DataUsage.objects.all()
        
        # Customer can only see their own usage
        from apps.customers.models import Customer
        try:
            customer = Customer.objects.get(user=user)
            return DataUsage.objects.filter(customer=customer)
        except Customer.DoesNotExist:
            return DataUsage.objects.none()
    
    @action(detail=False, methods=['get'])
    def current_period(self, request):
        """Get current billing period usage"""
        user = request.user
        
        try:
            from apps.customers.models import Customer
            customer = Customer.objects.get(user=user)
            
            # Get current period
            current_month = timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            next_month = (current_month + timedelta(days=32)).replace(day=1)
            
            usage, created = DataUsage.objects.get_or_create(
                customer=customer,
                period_start=current_month,
                period_end=next_month - timedelta(seconds=1),
                defaults={
                    'download_bytes': 0,
                    'upload_bytes': 0,
                    'total_bytes': 0
                }
            )
            
            serializer = self.get_serializer(usage)
            return Response(serializer.data)
            
        except Customer.DoesNotExist:
            return Response(
                {'error': 'Customer profile not found'},
                status=status.HTTP_404_NOT_FOUND
            )
    
    @action(detail=False, methods=['get'])
    def historical(self, request):
        """Get historical usage data"""
        user = request.user
        months = int(request.query_params.get('months', 6))
        
        try:
            from apps.customers.models import Customer
            customer = Customer.objects.get(user=user)
            
            end_date = timezone.now()
            start_date = end_date - timedelta(days=months*30)
            
            usage_data = DataUsage.objects.filter(
                customer=customer,
                period_start__gte=start_date,
                period_end__lte=end_date
            ).order_by('period_start')
            
            # Format for charts
            chart_data = {
                'labels': [u.period_start.strftime('%b %Y') for u in usage_data],
                'download': [u.download_gb for u in usage_data],
                'upload': [u.upload_gb for u in usage_data],
                'total': [u.total_gb for u in usage_data]
            }
            
            # Summary statistics
            if usage_data:
                total_download = sum([u.download_bytes for u in usage_data])
                total_upload = sum([u.upload_bytes for u in usage_data])
                avg_daily = (total_download + total_upload) / (months*30) / (1024**2)
                
                summary = {
                    'total_download_gb': round(total_download / (1024**3), 2),
                    'total_upload_gb': round(total_upload / (1024**3), 2),
                    'average_daily_mb': round(avg_daily, 2),
                    'peak_download_mbps': max([u.peak_download_speed for u in usage_data]),
                    'peak_upload_mbps': max([u.peak_upload_speed for u in usage_data]),
                }
            else:
                summary = {}
            
            return Response({
                'chart_data': chart_data,
                'summary': summary,
                'usage_data': DataUsageSerializer(usage_data, many=True).data
            })
            
        except Customer.DoesNotExist:
            return Response(
                {'error': 'Customer profile not found'},
                status=status.HTTP_404_NOT_FOUND
            )


class BandwidthAlertViewSet(viewsets.ModelViewSet):
    """ViewSet for managing bandwidth alerts"""
    serializer_class = BandwidthAlertSerializer
    
    def get_permissions(self):
        """Set permissions based on action"""
        if self.action in ['list', 'retrieve', 'acknowledge', 'resolve']:
            permission_classes = [IsAuthenticated]
        else:
            permission_classes = [IsAuthenticated, IsAdmin | IsAdminOrStaff]
        return [permission() for permission in permission_classes]
    
    def get_queryset(self):
        """Filter queryset based on user permissions"""
        user = self.request.user
        
        if user.is_superuser or user.role in ['admin', 'staff', 'technician']:
            return BandwidthAlert.objects.all()
        
        # Customer can only see their own alerts
        from apps.customers.models import Customer
        try:
            customer = Customer.objects.get(user=user)
            return BandwidthAlert.objects.filter(customer=customer)
        except Customer.DoesNotExist:
            return BandwidthAlert.objects.none()
    
    @action(detail=True, methods=['post'])
    def acknowledge(self, request, pk=None):
        """Acknowledge an alert"""
        alert = self.get_object()
        
        if alert.acknowledged:
            return Response({'message': 'Alert already acknowledged'})
        
        alert.acknowledged = True
        alert.acknowledged_by = request.user
        alert.acknowledged_at = timezone.now()
        alert.save()
        
        return Response({'message': 'Alert acknowledged successfully'})
    
    @action(detail=True, methods=['post'])
    def resolve(self, request, pk=None):
        """Resolve an alert"""
        alert = self.get_object()
        
        if alert.resolved:
            return Response({'message': 'Alert already resolved'})
        
        resolution_notes = request.data.get('resolution_notes', '')
        
        alert.resolved = True
        alert.resolved_at = timezone.now()
        alert.resolution_notes = resolution_notes
        alert.save()
        
        return Response({'message': 'Alert resolved successfully'})
    
    @action(detail=False, methods=['get'])
    def active(self, request):
        """Get active (unresolved) alerts"""
        queryset = self.get_queryset().filter(resolved=False)
        
        # Filter by alert level if provided
        alert_level = request.query_params.get('level')
        if alert_level:
            queryset = queryset.filter(alert_level=alert_level)
        
        serializer = self.get_serializer(queryset, many=True)
        return Response(serializer.data)


class SpeedTestResultViewSet(viewsets.ModelViewSet):
    """ViewSet for speed test results"""
    serializer_class = SpeedTestResultSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        """Filter queryset based on user permissions"""
        user = self.request.user
        
        if user.is_superuser or user.role in ['admin', 'staff']:
            return SpeedTestResult.objects.all()
        
        # Customer can only see their own tests
        from apps.customers.models import Customer
        try:
            customer = Customer.objects.get(user=user)
            return SpeedTestResult.objects.filter(customer=customer)
        except Customer.DoesNotExist:
            return SpeedTestResult.objects.none()
    
    def create(self, request, *args, **kwargs):
        """Create a new speed test result"""
        user = request.user
        
        try:
            from apps.customers.models import Customer
            customer = Customer.objects.get(user=user)
        except Customer.DoesNotExist:
            return Response(
                {'error': 'Customer profile not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Add customer to request data
        request.data['customer'] = customer.id
        if hasattr(customer, 'company'):
            request.data['company'] = customer.company.id
        
        return super().create(request, *args, **kwargs)
    
    @action(detail=False, methods=['get'])
    def recent(self, request):
        """Get recent speed test results"""
        days = int(request.query_params.get('days', 7))
        
        cutoff_date = timezone.now() - timedelta(days=days)
        queryset = self.get_queryset().filter(test_time__gte=cutoff_date)
        
        # Calculate averages
        if queryset.exists():
            avg_download = queryset.aggregate(Avg('download_speed'))['download_speed__avg']
            avg_upload = queryset.aggregate(Avg('upload_speed'))['upload_speed__avg']
            avg_latency = queryset.aggregate(Avg('latency'))['latency__avg']
        else:
            avg_download = avg_upload = avg_latency = 0
        
        serializer = self.get_serializer(queryset, many=True)
        
        return Response({
            'results': serializer.data,
            'summary': {
                'average_download_mbps': round(avg_download, 2) if avg_download else 0,
                'average_upload_mbps': round(avg_upload, 2) if avg_upload else 0,
                'average_latency_ms': round(avg_latency, 2) if avg_latency else 0,
                'total_tests': queryset.count()
            }
        })


class TrafficAnalysisViewSet(viewsets.ViewSet):
    """ViewSet for traffic analysis"""
    permission_classes = [IsAuthenticated, IsAdmin | IsAdminOrStaff]
    
    @action(detail=False, methods=['get'])
    def customer_analysis(self, request):
        """Analyze traffic patterns for a customer"""
        customer_id = request.query_params.get('customer_id')
        days = int(request.query_params.get('days', 30))
        
        if not customer_id:
            return Response(
                {'error': 'customer_id parameter required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        try:
            from apps.customers.models import Customer
            customer = Customer.objects.get(id=customer_id)
            
            analyzer = TrafficAnalyzer()
            analysis = analyzer.analyze_customer_patterns(customer, days)
            
            return Response(analysis)
            
        except Customer.DoesNotExist:
            return Response(
                {'error': 'Customer not found'},
                status=status.HTTP_404_NOT_FOUND
            )
    
    @action(detail=False, methods=['get'])
    def network_analysis(self, request):
        """Analyze overall network traffic patterns"""
        days = int(request.query_params.get('days', 7))
        
        # Get aggregate network usage
        end_date = timezone.now()
        start_date = end_date - timedelta(days=days)
        
        # Total usage
        total_usage = DataUsage.objects.filter(
            period_start__gte=start_date,
            period_end__lte=end_date
        ).aggregate(
            total_download=Sum('download_bytes'),
            total_upload=Sum('upload_bytes'),
            avg_download_speed=Avg('peak_download_speed'),
            avg_upload_speed=Avg('peak_upload_speed')
        )
        
        # Active customers count
        from apps.customers.models import Customer
        active_customers = Customer.objects.filter(status='ACTIVE').count()
        
        return Response({
            'analysis_period': {
                'start': start_date.date(),
                'end': end_date.date(),
                'days': days
            },
            'network_usage': {
                'total_download_tb': round((total_usage['total_download'] or 0) / (1024**4), 3),
                'total_upload_tb': round((total_usage['total_upload'] or 0) / (1024**4), 3),
                'average_download_speed_mbps': round(total_usage['avg_download_speed'] or 0, 2),
                'average_upload_speed_mbps': round(total_usage['avg_upload_speed'] or 0, 2),
            },
            'customer_metrics': {
                'active_customers': active_customers,
                'average_usage_per_customer_gb': round(
                    ((total_usage['total_download'] or 0) + (total_usage['total_upload'] or 0)) / 
                    (active_customers * (1024**3)), 2
                ) if active_customers > 0 else 0
            },
            'recommendations': [
                "Monitor peak hour congestion",
                "Consider bandwidth upgrades for top 10% users",
                "Review QoS policies for business customers"
            ]
        })