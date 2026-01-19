from django.db import models
from django.contrib.auth import get_user_model
from django.db.models import JSONField
from django.utils import timezone
  # ← Add this import
from apps.customers.models import Customer
from apps.network.models import OLTDevice, CPEDevice

User = get_user_model()


class ReportDefinition(models.Model):
    """Definition of reports available to tenants"""
    
    REPORT_TYPES = [
        ('financial', 'Financial Report'),
        ('network', 'Network Report'),
        ('customer', 'Customer Report'),
        ('custom', 'Custom Report'),
    ]
    
    FORMAT_CHOICES = [
        ('json', 'JSON'),
        ('csv', 'CSV'),
        ('pdf', 'PDF'),
        ('excel', 'Excel'),
    ]
    
    name = models.CharField(max_length=255)
    report_type = models.CharField(max_length=50, choices=REPORT_TYPES)
    description = models.TextField(blank=True)
    query = models.TextField()  # SQL or ORM query template
    parameters = JSONField(default=dict)
    format = models.CharField(max_length=20, choices=FORMAT_CHOICES, default='json')
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # TenantMixin required field with default
    def __str__(self):
        return self.name
    
    class Meta:
        app_label = 'analytics'
        ordering = ['-created_at']


class DashboardWidget(models.Model):
    """Widgets displayed on tenant dashboards"""
    
    WIDGET_TYPES = [
        ('chart', 'Chart'),
        ('metric', 'Metric'),
        ('table', 'Table'),
        ('list', 'List'),
    ]
    
    CHART_TYPES = [
        ('line', 'Line Chart'),
        ('bar', 'Bar Chart'),
        ('pie', 'Pie Chart'),
        ('area', 'Area Chart'),
    ]
    
    name = models.CharField(max_length=255)
    widget_type = models.CharField(max_length=50, choices=WIDGET_TYPES)
    chart_type = models.CharField(max_length=50, choices=CHART_TYPES, null=True, blank=True)
    data_source = models.CharField(max_length=255)  # URL or function name
    refresh_interval = models.IntegerField(default=300)  # seconds
    position = models.IntegerField(default=0)
    size = models.CharField(max_length=20, default='medium')  # small, medium, large
    config = JSONField(default=dict, blank=True)
    is_visible = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # TenantMixin required field with default
    def __str__(self):
        return self.name
    
    class Meta:
        app_label = 'analytics'
        ordering = ['position']


class AnalyticsCache(models.Model):
    """Cached analytics data per tenant"""
    
    cache_key = models.CharField(max_length=255, unique=True)
    data = JSONField(default=dict)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    
    # TenantMixin required field with default
    def is_valid(self):
        return self.expires_at > timezone.now()
    
    def __str__(self):
        return self.cache_key
    
    class Meta:
        app_label = 'analytics'
        ordering = ['-created_at']
