from rest_framework import viewsets, status, generics, filters
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView
from django_filters.rest_framework import DjangoFilterBackend
from django.utils import timezone
from django.db.models import Q, Count, F
from datetime import timedelta
import logging

from .models import (
    NotificationTemplate, 
    Notification, 
    AlertRule,
    NotificationPreference,
    BulkNotification,
    NotificationLog
)
from .serializers import (
    NotificationTemplateSerializer,
    NotificationSerializer,
    AlertRuleSerializer,
    NotificationPreferenceSerializer,
    BulkNotificationSerializer,
    NotificationLogSerializer,
    SendNotificationSerializer,
    TestNotificationSerializer
)
from .permissions import (
    IsAdminOrStaff,
    CanManageNotifications,
    CanManageTemplates,
    CanManageAlertRules,
    CanSendBulkNotifications,
    CanViewOwnNotifications,
    CanManageOwnPreferences,
    IsCustomerSelfService
)
from .services import NotificationManager

logger = logging.getLogger(__name__)

class NotificationTemplateViewSet(viewsets.ModelViewSet):
    """ViewSet for managing notification templates"""
    queryset = NotificationTemplate.objects.all()
    serializer_class = NotificationTemplateSerializer
    permission_classes = [IsAuthenticated, CanManageTemplates]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['notification_type', 'trigger_event', 'is_active']
    search_fields = ['name', 'subject', 'message_template']
    ordering_fields = ['name', 'priority', 'created_at']
    ordering = ['name']
    
    def get_queryset(self):
        queryset = super().get_queryset()
        
        # Filter by notification type if specified
        notification_type = self.request.query_params.get('notification_type')
        if notification_type:
            queryset = queryset.filter(notification_type=notification_type)
        
        return queryset
    
    @action(detail=True, methods=['post'])
    def duplicate(self, request, pk=None):
        """Duplicate a template"""
        template = self.get_object()
        template.pk = None
        template.name = f"{template.name} (Copy)"
        template.save()
        
        serializer = self.get_serializer(template)
        return Response(serializer.data, status=status.HTTP_201_CREATED)
    
    @action(detail=False, methods=['get'])
    def variables(self, request):
        """Get available template variables"""
        variables = {
            'customer': [
                '{{customer_name}}', '{{customer_email}}', '{{customer_phone}}',
                '{{customer_address}}', '{{customer_id}}', '{{customer_code}}'
            ],
            'invoice': [
                '{{invoice_number}}', '{{invoice_amount}}', '{{invoice_date}}',
                '{{due_date}}', '{{balance_due}}', '{{invoice_url}}'
            ],
            'payment': [
                '{{payment_amount}}', '{{payment_method}}', '{{payment_date}}',
                '{{transaction_id}}', '{{receipt_number}}'
            ],
            'service': [
                '{{service_type}}', '{{service_status}}', '{{activation_date}}',
                '{{plan_name}}', '{{bandwidth}}', '{{ip_address}}'
            ],
            'ticket': [
                '{{ticket_id}}', '{{ticket_subject}}', '{{ticket_status}}',
                '{{ticket_priority}}', '{{assigned_to}}'
            ],
            'system': [
                '{{company_name}}', '{{support_email}}', '{{support_phone}}',
                '{{website_url}}', '{{current_date}}', '{{current_time}}'
            ]
        }
        return Response(variables)

class NotificationViewSet(viewsets.ModelViewSet):
    """ViewSet for managing notifications"""
    serializer_class = NotificationSerializer
    permission_classes = [IsAuthenticated]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['notification_type', 'status', 'priority']
    search_fields = ['subject', 'message', 'recipient_email', 'recipient_phone']
    ordering_fields = ['created_at', 'sent_at', 'priority']
    ordering = ['-created_at']
    
    def get_permissions(self):
        """Set permissions based on action"""
        if self.action in ['list', 'retrieve']:
            permission_classes = [IsAuthenticated]
        elif self.action in ['create', 'update', 'partial_update', 'destroy']:
            permission_classes = [IsAuthenticated, CanManageNotifications]
        else:
            permission_classes = [IsAuthenticated]
        return [permission() for permission in permission_classes]
    
    def get_queryset(self):
        user = self.request.user
        
        if user.is_superuser or user.is_staff:
            # Admins and staff can see all notifications
            queryset = Notification.objects.all()
        else:
            # Users can only see their own notifications
            queryset = Notification.objects.filter(
                Q(user=user) |
                Q(recipient_email=user.email) |
                Q(recipient_phone=user.phone)
            )
        
        # Filter by date range if specified
        start_date = self.request.query_params.get('start_date')
        end_date = self.request.query_params.get('end_date')
        
        if start_date:
            queryset = queryset.filter(created_at__gte=start_date)
        if end_date:
            queryset = queryset.filter(created_at__lte=end_date)
        
        # Filter by read status
        read_status = self.request.query_params.get('read')
        if read_status is not None:
            if read_status.lower() == 'true':
                queryset = queryset.filter(read_at__isnull=False)
            else:
                queryset = queryset.filter(read_at__isnull=True)
        
        return queryset
    
    def perform_create(self, serializer):
        """Create notification with notification manager"""
        notification = serializer.save()
        
        # Send notification if requested
        send_now = self.request.data.get('send_now', False)
        if send_now:
            manager = NotificationManager()
            manager.send_notification(notification)
    
    @action(detail=True, methods=['post'])
    def send(self, request, pk=None):
        """Send a specific notification"""
        notification = self.get_object()
        
        if notification.status in ['sent', 'delivered']:
            return Response(
                {'error': 'Notification already sent'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        manager = NotificationManager()
        success = manager.send_notification(notification)
        
        if success:
            return Response({'status': 'Notification sent successfully'})
        else:
            return Response(
                {'error': 'Failed to send notification'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['post'])
    def mark_as_read(self, request, pk=None):
        """Mark notification as read"""
        notification = self.get_object()
        
        # Check permission
        if not (notification.user == request.user or 
                notification.recipient_email == request.user.email or
                request.user.is_staff):
            return Response(
                {'error': 'Permission denied'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        notification.mark_as_read()
        return Response({'status': 'Notification marked as read'})
    
    @action(detail=False, methods=['post'])
    def mark_all_as_read(self, request):
        """Mark all user's notifications as read"""
        user = request.user
        
        notifications = Notification.objects.filter(
            Q(user=user) |
            Q(recipient_email=user.email) |
            Q(recipient_phone=user.phone),
            read_at__isnull=True
        )
        
        count = notifications.count()
        notifications.update(
            status='read',
            read_at=timezone.now()
        )
        
        return Response({'status': f'{count} notifications marked as read'})
    
    @action(detail=False, methods=['get'])
    def stats(self, request):
        """Get notification statistics"""
        user = request.user
        queryset = self.get_queryset()
        
        # Total notifications
        total = queryset.count()
        
        # By status
        by_status = queryset.values('status').annotate(
            count=Count('id')
        ).order_by('status')
        
        # By type
        by_type = queryset.values('notification_type').annotate(
            count=Count('id')
        ).order_by('notification_type')
        
        # Recent activity
        recent = queryset.order_by('-created_at')[:10]
        recent_serializer = self.get_serializer(recent, many=True)
        
        # Unread count
        unread = queryset.filter(read_at__isnull=True).count()
        
        return Response({
            'total': total,
            'unread': unread,
            'by_status': by_status,
            'by_type': by_type,
            'recent': recent_serializer.data
        })
    
    @action(detail=False, methods=['post'])
    def send_test(self, request):
        """Send a test notification"""
        serializer = TestNotificationSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        data = serializer.validated_data
        manager = NotificationManager()
        
        try:
            if data['notification_type'] == 'email':
                success = manager.send_test_email(
                    recipient=data['recipient_email'],
                    message=data['message']
                )
            elif data['notification_type'] == 'sms':
                success = manager.send_test_sms(
                    recipient=data['recipient_phone'],
                    message=data['message']
                )
            else:
                return Response(
                    {'error': 'Test not supported for this notification type'},
                    status=status.HTTP_400_BAD_REQUEST
                )
            
            if success:
                return Response({'status': 'Test notification sent successfully'})
            else:
                return Response(
                    {'error': 'Failed to send test notification'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
                
        except Exception as e:
            logger.error(f"Error sending test notification: {str(e)}")
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class AlertRuleViewSet(viewsets.ModelViewSet):
    """ViewSet for managing alert rules"""
    queryset = AlertRule.objects.all()
    serializer_class = AlertRuleSerializer
    permission_classes = [IsAuthenticated, CanManageAlertRules]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter]
    filterset_fields = ['alert_type', 'is_active']
    search_fields = ['name', 'description', 'model_name', 'field_name']
    
    def get_queryset(self):
        queryset = super().get_queryset()
        
        # Filter by alert type if specified
        alert_type = self.request.query_params.get('alert_type')
        if alert_type:
            queryset = queryset.filter(alert_type=alert_type)
        
        # Only show active rules for non-admins
        if not self.request.user.is_superuser:
            queryset = queryset.filter(is_active=True)
        
        return queryset
    
    @action(detail=True, methods=['post'])
    def test(self, request, pk=None):
        """Test an alert rule"""
        alert_rule = self.get_object()
        manager = NotificationManager()
        
        try:
            # Test the rule by running a check
            triggered = manager.test_alert_rule(alert_rule)
            
            return Response({
                'triggered': triggered,
                'message': 'Alert rule test completed'
            })
            
        except Exception as e:
            logger.error(f"Error testing alert rule: {str(e)}")
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
    
    @action(detail=True, methods=['post'])
    def toggle(self, request, pk=None):
        """Toggle alert rule active status"""
        alert_rule = self.get_object()
        alert_rule.is_active = not alert_rule.is_active
        alert_rule.save()
        
        status_text = 'activated' if alert_rule.is_active else 'deactivated'
        return Response({
            'status': f'Alert rule {status_text}',
            'is_active': alert_rule.is_active
        })

class NotificationPreferenceViewSet(viewsets.ModelViewSet):
    """ViewSet for managing notification preferences"""
    serializer_class = NotificationPreferenceSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        user = self.request.user
        
        if user.is_superuser:
            # Admins can see all preferences
            return NotificationPreference.objects.all()
        else:
            # Users can only see their own preferences
            return NotificationPreference.objects.filter(user=user)
    
    def get_permissions(self):
        """Set permissions based on action"""
        if self.action in ['list', 'retrieve', 'update', 'partial_update']:
            permission_classes = [IsAuthenticated]
        elif self.action in ['create', 'destroy']:
            permission_classes = [IsAuthenticated, CanManageNotifications]
        else:
            permission_classes = [IsAuthenticated]
        return [permission() for permission in permission_classes]
    
    def perform_create(self, serializer):
        """Create preference for current user if not specified"""
        if 'user' not in serializer.validated_data:
            serializer.save(user=self.request.user)
        else:
            serializer.save()
    
    @action(detail=False, methods=['get'])
    def my_preferences(self, request):
        """Get current user's preferences"""
        user = request.user
        
        try:
            preference = NotificationPreference.objects.get(user=user)
            serializer = self.get_serializer(preference)
            return Response(serializer.data)
        except NotificationPreference.DoesNotExist:
            # Create default preferences
            preference = NotificationPreference.objects.create(user=user)
            serializer = self.get_serializer(preference)
            return Response(serializer.data, status=status.HTTP_201_CREATED)

class BulkNotificationViewSet(viewsets.ModelViewSet):
    """ViewSet for managing bulk notifications"""
    queryset = BulkNotification.objects.all()
    serializer_class = BulkNotificationSerializer
    permission_classes = [IsAuthenticated, CanSendBulkNotifications]
    filter_backends = [DjangoFilterBackend, filters.SearchFilter, filters.OrderingFilter]
    filterset_fields = ['notification_type', 'status', 'target_segment']
    search_fields = ['name', 'subject', 'message']
    ordering_fields = ['scheduled_for', 'created_at', 'status']
    ordering = ['-created_at']
    
    def perform_create(self, serializer):
        """Create bulk notification with created_by user"""
        serializer.save(created_by=self.request.user)
    
    @action(detail=True, methods=['post'])
    def schedule(self, request, pk=None):
        """Schedule a bulk notification"""
        bulk_notification = self.get_object()
        
        if bulk_notification.status != 'draft':
            return Response(
                {'error': 'Only draft notifications can be scheduled'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        scheduled_for = request.data.get('scheduled_for')
        if not scheduled_for:
            return Response(
                {'error': 'scheduled_for is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        bulk_notification.scheduled_for = scheduled_for
        bulk_notification.status = 'scheduled'
        bulk_notification.save()
        
        return Response({'status': 'Bulk notification scheduled'})
    
    @action(detail=True, methods=['post'])
    def send_now(self, request, pk=None):
        """Send bulk notification immediately"""
        from django.db import transaction
        from apps.customers.models import Customer
        
        bulk_notification = self.get_object()
        
        if bulk_notification.status not in ['draft', 'scheduled']:
            return Response(
                {'error': 'Notification cannot be sent in current status'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get recipients based on target segment
        recipients = []
        
        if bulk_notification.target_segment == 'all_customers':
            customers = Customer.objects.filter(is_active=True)
            recipients = [customer.user.email for customer in customers if customer.user]
        elif bulk_notification.target_segment == 'active_customers':
            customers = Customer.objects.filter(is_active=True, status='active')
            recipients = [customer.user.email for customer in customers if customer.user]
        elif bulk_notification.target_segment == 'overdue_customers':
            # This would need integration with billing app
            recipients = []  # Implement based on your billing models
        elif bulk_notification.target_segment == 'custom_list':
            recipients = bulk_notification.custom_recipients or []
        
        bulk_notification.total_recipients = len(recipients)
        bulk_notification.status = 'processing'
        bulk_notification.started_at = timezone.now()
        bulk_notification.save()
        
        # Start sending in background (would use Celery in production)
        # For now, we'll simulate
        bulk_notification.sent_count = len(recipients)  # Simulate success
        bulk_notification.status = 'completed'
        bulk_notification.completed_at = timezone.now()
        bulk_notification.save()
        
        return Response({
            'status': 'Bulk notification sending started',
            'recipients': len(recipients)
        })
    
    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        """Cancel a scheduled bulk notification"""
        bulk_notification = self.get_object()
        
        if bulk_notification.status not in ['scheduled', 'processing']:
            return Response(
                {'error': 'Only scheduled or processing notifications can be cancelled'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        bulk_notification.status = 'cancelled'
        bulk_notification.save()
        
        return Response({'status': 'Bulk notification cancelled'})

class SendNotificationView(APIView):
    """API view for sending manual notifications"""
    permission_classes = [IsAuthenticated, CanManageNotifications]
    
    def post(self, request):
        serializer = SendNotificationSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
        
        data = serializer.validated_data
        manager = NotificationManager()
        
        try:
            # Determine recipient
            if data['recipient_type'] == 'user':
                from django.contrib.auth import get_user_model
                User = get_user_model()
                try:
                    user = User.objects.get(id=data['user_id'])
                    recipient_email = user.email
                    recipient_phone = user.phone if hasattr(user, 'phone') else None
                except User.DoesNotExist:
                    return Response(
                        {'error': 'User not found'},
                        status=status.HTTP_404_NOT_FOUND
                    )
            elif data['recipient_type'] == 'email':
                recipient_email = data['email']
                recipient_phone = None
                user = None
            else:  # phone
                recipient_email = None
                recipient_phone = data['phone']
                user = None
            
            # Use template if specified
            if data.get('template_id'):
                try:
                    template = NotificationTemplate.objects.get(id=data['template_id'])
                    # Apply template variables
                    message = template.message_template
                    for key, value in data.get('template_variables', {}).items():
                        placeholder = f'{{{{{key}}}}}'
                        message = message.replace(placeholder, str(value))
                    subject = template.subject
                except NotificationTemplate.DoesNotExist:
                    return Response(
                        {'error': 'Template not found'},
                        status=status.HTTP_404_NOT_FOUND
                    )
            else:
                message = data['message']
                subject = data.get('subject', 'Notification')
            
            # Create and send notification
            notification = Notification.objects.create(
                user=user,
                notification_type=data['notification_type'],
                subject=subject,
                message=message,
                recipient_email=recipient_email,
                recipient_phone=recipient_phone,
                priority=data['priority'],
                metadata={
                    'manual_send': True,
                    'sent_by': request.user.id,
                    'template_variables': data.get('template_variables', {})
                }
            )
            
            # Send notification
            success = manager.send_notification(notification)
            
            if success:
                return Response({
                    'status': 'Notification sent successfully',
                    'notification_id': notification.id
                })
            else:
                return Response(
                    {'error': 'Failed to send notification'},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )
                
        except Exception as e:
            logger.error(f"Error sending manual notification: {str(e)}")
            return Response(
                {'error': str(e)},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

class NotificationStatsView(APIView):
    """API view for notification statistics"""
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    
    def get(self, request):
        from django.db.models import Count, Avg, Q
        from datetime import datetime, timedelta
        
        # Time periods
        now = timezone.now()
        today = now.date()
        yesterday = today - timedelta(days=1)
        last_7_days = today - timedelta(days=7)
        last_30_days = today - timedelta(days=30)
        
        # Overall stats
        total_notifications = Notification.objects.count()
        sent_today = Notification.objects.filter(
            sent_at__date=today
        ).count()
        failed_today = Notification.objects.filter(
            status='failed',
            created_at__date=today
        ).count()
        
        # By type
        by_type = Notification.objects.values('notification_type').annotate(
            count=Count('id')
        ).order_by('-count')
        
        # By status
        by_status = Notification.objects.values('status').annotate(
            count=Count('id')
        ).order_by('status')
        
        # Daily stats for last 7 days
        daily_stats = []
        for i in range(6, -1, -1):
            date = today - timedelta(days=i)
            day_stats = Notification.objects.filter(
                created_at__date=date
            ).aggregate(
                total=Count('id'),
                sent=Count('id', filter=Q(status='sent')),
                failed=Count('id', filter=Q(status='failed'))
            )
            daily_stats.append({
                'date': date,
                'total': day_stats['total'] or 0,
                'sent': day_stats['sent'] or 0,
                'failed': day_stats['failed'] or 0
            })
        
        # Alert rules stats
        total_alerts = AlertRule.objects.count()
        active_alerts = AlertRule.objects.filter(is_active=True).count()
        
        # SMS/Email balance (would integrate with provider APIs)
        sms_balance = 0
        email_balance = 0
        
        try:
            from .services import SMSService
            sms_service = SMSService()
            sms_balance_info = sms_service.get_balance()
            sms_balance = sms_balance_info.get('balance', 0)
        except:
            pass
        
        return Response({
            'overview': {
                'total_notifications': total_notifications,
                'sent_today': sent_today,
                'failed_today': failed_today,
                'success_rate_today': (
                    (sent_today - failed_today) / sent_today * 100 
                    if sent_today > 0 else 100
                )
            },
            'by_type': list(by_type),
            'by_status': list(by_status),
            'daily_stats': daily_stats,
            'alerts': {
                'total': total_alerts,
                'active': active_alerts,
                'inactive': total_alerts - active_alerts
            },
            'balances': {
                'sms': sms_balance,
                'email': email_balance
            }
        })

class SelfServiceNotificationView(APIView):
    """API view for customer self-service notifications"""
    permission_classes = [IsAuthenticated, IsCustomerSelfService]
    
    def get(self, request):
        """Get customer's notifications"""
        user = request.user
        
        notifications = Notification.objects.filter(
            Q(user=user) |
            Q(recipient_email=user.email) |
            Q(recipient_phone=user.phone)
        ).order_by('-created_at')[:50]
        
        unread_count = notifications.filter(read_at__isnull=True).count()
        
        serializer = NotificationSerializer(
            notifications, 
            many=True,
            context={'request': request}
        )
        
        return Response({
            'notifications': serializer.data,
            'unread_count': unread_count
        })
    
    def post(self, request):
        """Mark notifications as read"""
        notification_ids = request.data.get('notification_ids', [])
        
        if not notification_ids:
            return Response(
                {'error': 'No notification IDs provided'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        user = request.user
        
        # Get user's notifications
        notifications = Notification.objects.filter(
            id__in=notification_ids
        ).filter(
            Q(user=user) |
            Q(recipient_email=user.email) |
            Q(recipient_phone=user.phone)
        )
        
        count = notifications.count()
        notifications.update(
            status='read',
            read_at=timezone.now()
        )
        
        return Response({
            'status': f'{count} notifications marked as read'
        })
