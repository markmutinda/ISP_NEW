"""
RADIUS Serializers
"""
from django.utils import timezone
from django.db import models
from rest_framework import serializers
from .models import (
    RadCheck,
    RadReply,
    RadUserGroup,
    RadGroupCheck,
    RadGroupReply,
    RadAcct,
    Nas,
    RadPostAuth,
    RadiusBandwidthProfile,
    RadiusTenantConfig,
    CustomerRadiusCredentials,
)


class RadCheckSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.full_name', read_only=True, allow_null=True)
    
    class Meta:
        model = RadCheck
        fields = ['id', 'username', 'attribute', 'op', 'value', 'customer', 'customer_name']


class RadReplySerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.full_name', read_only=True, allow_null=True)
    
    class Meta:
        model = RadReply
        fields = ['id', 'username', 'attribute', 'op', 'value', 'customer', 'customer_name']


class RadUserGroupSerializer(serializers.ModelSerializer):
    class Meta:
        model = RadUserGroup
        fields = ['id', 'username', 'groupname', 'priority']


class RadGroupCheckSerializer(serializers.ModelSerializer):
    class Meta:
        model = RadGroupCheck
        fields = ['id', 'groupname', 'attribute', 'op', 'value']


class RadGroupReplySerializer(serializers.ModelSerializer):
    class Meta:
        model = RadGroupReply
        fields = ['id', 'groupname', 'attribute', 'op', 'value']


class RadAcctSerializer(serializers.ModelSerializer):
    is_active = serializers.ReadOnlyField()
    total_bytes = serializers.ReadOnlyField()
    duration_formatted = serializers.ReadOnlyField()
    customer_name = serializers.CharField(source='customer.full_name', read_only=True, allow_null=True)
    router_name = serializers.CharField(source='router.name', read_only=True, allow_null=True)
    
    class Meta:
        model = RadAcct
        fields = [
            'radacctid', 'acctsessionid', 'acctuniqueid', 'username',
            'nasipaddress', 'nasportid', 'nasporttype',
            'acctstarttime', 'acctupdatetime', 'acctstoptime',
            'acctsessiontime', 'acctinputoctets', 'acctoutputoctets',
            'framedipaddress', 'callingstationid', 'calledstationid',
            'acctterminatecause', 'is_active', 'total_bytes', 'duration_formatted',
            'customer', 'customer_name', 'router', 'router_name'
        ]
        read_only_fields = fields


class OnlineUserSerializer(serializers.ModelSerializer):
    """Specific serializer for the Online Users frontend dashboard"""
    full_name = serializers.SerializerMethodField()
    phone_number = serializers.SerializerMethodField()
    mac_address = serializers.CharField(source='callingstationid', read_only=True)
    ip_address = serializers.CharField(source='framedipaddress', read_only=True)
    uptime = serializers.SerializerMethodField()
    usage = serializers.SerializerMethodField()
    router = serializers.SerializerMethodField()
    service_type = serializers.SerializerMethodField()

    class Meta:
        model = RadAcct
        fields = [
            'radacctid', 'acctsessionid', 'username', 'full_name', 
            'phone_number', 'mac_address', 'ip_address', 
            'uptime', 'usage', 'router', 'service_type'
        ]

    def get_full_name(self, obj):
        # 1. Try the direct link (if it exists)
        if obj.customer:
            return obj.customer.full_name
            
        # 2. FALLBACK: Resolve name manually from credentials
        from apps.radius.models import CustomerRadiusCredentials
        creds = CustomerRadiusCredentials.objects.filter(username=obj.username).select_related('customer').first()
        if creds and creds.customer:
            return creds.customer.full_name
            
        return f"Hotspot-{obj.username}"

    def get_phone_number(self, obj):
        # 1. Resolve phone number via direct link
        if obj.customer and hasattr(obj.customer, 'user') and obj.customer.user:
            return obj.customer.user.phone_number or "N/A"
        
        # 2. Manual lookup fallback
        from apps.radius.models import CustomerRadiusCredentials
        creds = CustomerRadiusCredentials.objects.filter(username=obj.username).select_related('customer__user').first()
        if creds and creds.customer and hasattr(creds.customer, 'user') and creds.customer.user:
            return creds.customer.user.phone_number or "N/A"
            
        return "N/A"

    def get_router(self, obj):
        # 1. Try the direct link
        if obj.router:
            return obj.router.name
            
        # 2. FALLBACK: Resolve router name from NAS IP Address
        from apps.network.models import Router
        r = Router.objects.filter(
            models.Q(vpn_ip_address=obj.nasipaddress) | 
            models.Q(ip_address=obj.nasipaddress)
        ).first()
        return r.name if r else "Unknown Router"

    def get_uptime(self, obj):
        # Calculate live uptime based on when session started
        if not obj.acctstarttime:
            return "0s"
        delta = timezone.now() - obj.acctstarttime
        total_seconds = int(delta.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m {seconds}s"

    def get_usage(self, obj):
        # Calculate total usage (Download + Upload) in MB/GB
        total_bytes = (obj.acctinputoctets or 0) + (obj.acctoutputoctets or 0)
        mb = total_bytes / (1024 * 1024)
        if mb > 1024:
            gb = mb / 1024
            return f"{gb:.2f} GB"
        return f"{mb:.2f} MB"

    def get_service_type(self, obj):
        # Heuristic based on framing protocol (Mikrotik standard)
        if obj.framedprotocol == 'PPP':
            return 'PPPOE'
        return 'HOTSPOT'


class RadAcctSummarySerializer(serializers.Serializer):
    """Summary statistics for RADIUS accounting"""
    total_sessions = serializers.IntegerField()
    active_sessions = serializers.IntegerField()
    total_bytes_in = serializers.IntegerField()
    total_bytes_out = serializers.IntegerField()
    avg_session_time = serializers.FloatField()
    unique_users = serializers.IntegerField()


class NasSerializer(serializers.ModelSerializer):
    router_name = serializers.CharField(source='router.name', read_only=True, allow_null=True)
    
    class Meta:
        model = Nas
        fields = [
            'id', 'nasname', 'shortname', 'type', 'ports',
            'secret', 'server', 'community', 'description',
            'router', 'router_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']
        extra_kwargs = {
            'secret': {'write_only': True}
        }


class NasDetailSerializer(NasSerializer):
    """Include secret for admin views"""
    class Meta(NasSerializer.Meta):
        extra_kwargs = {}


class RadPostAuthSerializer(serializers.ModelSerializer):
    is_success = serializers.ReadOnlyField()
    
    class Meta:
        model = RadPostAuth
        fields = [
            'id', 'username', 'reply', 'authdate',
            'nasipaddress', 'callingstationid', 'is_success'
        ]
        read_only_fields = fields


class RadiusBandwidthProfileSerializer(serializers.ModelSerializer):
    mikrotik_rate_limit = serializers.ReadOnlyField()
    
    class Meta:
        model = RadiusBandwidthProfile
        fields = [
            'id', 'name', 'description',
            'download_speed', 'upload_speed',
            'burst_download', 'burst_upload', 'burst_threshold', 'burst_time',
            'priority', 'daily_limit_mb', 'monthly_limit_mb',
            'session_timeout', 'idle_timeout', 'simultaneous_use',
            'is_active', 'mikrotik_rate_limit',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class RadiusUserCreateSerializer(serializers.Serializer):
    """Serializer for creating a RADIUS user with all attributes"""
    username = serializers.CharField(max_length=64)
    password = serializers.CharField(max_length=253, write_only=True, required=False, allow_blank=True)
    auto_generate_password = serializers.BooleanField(default=False, required=False)
    
    # Optional: Link to customer
    customer_id = serializers.UUIDField(required=False, allow_null=True)
    
    # Optional: Bandwidth profile
    profile_id = serializers.UUIDField(required=False, allow_null=True)
    
    # Optional: Direct attributes
    download_speed = serializers.IntegerField(required=False, help_text="Download speed in kbps")
    upload_speed = serializers.IntegerField(required=False, help_text="Upload speed in kbps")
    static_ip = serializers.IPAddressField(required=False, allow_null=True)
    session_timeout = serializers.IntegerField(required=False)
    simultaneous_use = serializers.IntegerField(required=False, default=1)
    
    # Expiration
    expiration = serializers.DateTimeField(required=False, allow_null=True)
    
    # Group assignment
    groupname = serializers.CharField(max_length=64, required=False)


class RadiusDashboardSerializer(serializers.Serializer):
    """Dashboard statistics"""
    total_users = serializers.IntegerField()
    active_sessions = serializers.IntegerField()
    total_nas = serializers.IntegerField()
    total_profiles = serializers.IntegerField()
    
    # Auth stats (last 24h)
    auth_success_24h = serializers.IntegerField()
    auth_failure_24h = serializers.IntegerField()
    
    # Traffic stats (today)
    bytes_in_today = serializers.IntegerField()
    bytes_out_today = serializers.IntegerField()
    
    # Top users by traffic
    top_users = serializers.ListField(child=serializers.DictField())


# ────────────────────────────────────────────────────────────────
# TENANT CONFIGURATION SERIALIZERS
# ────────────────────────────────────────────────────────────────

class RadiusTenantConfigSerializer(serializers.ModelSerializer):
    """Serializer for RADIUS tenant configuration."""
    
    class Meta:
        model = RadiusTenantConfig
        fields = [
            'id', 'schema_name', 'tenant_name',
            'radius_secret',
            'is_active', 'config_generated', 'last_config_update',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'last_config_update']
        extra_kwargs = {
            'radius_secret': {'write_only': True}  # Don't expose secret in GET
        }


class RadiusTenantConfigDetailSerializer(RadiusTenantConfigSerializer):
    """Detailed serializer that includes the secret (for admin)."""
    
    class Meta(RadiusTenantConfigSerializer.Meta):
        extra_kwargs = {}


class CustomerRadiusCredentialsSerializer(serializers.ModelSerializer):
    """Serializer for customer RADIUS credentials."""
    
    customer_name = serializers.CharField(source='customer.full_name', read_only=True)
    customer_code = serializers.CharField(source='customer.customer_code', read_only=True)
    profile_name = serializers.CharField(source='bandwidth_profile.name', read_only=True, allow_null=True)
    router_name = serializers.CharField(source='router.name', read_only=True, allow_null=True, default=None)
    
    class Meta:
        model = CustomerRadiusCredentials
        fields = [
            'id', 'customer', 'customer_name', 'customer_code',
            'username', 'password',
            'router', 'router_name',
            'bandwidth_profile', 'profile_name',
            'connection_type', 'is_enabled', 'disabled_reason',
            'static_ip', 'ip_pool', 'simultaneous_use',
            'expiration_date', 'synced_to_radius', 'last_sync',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'synced_to_radius', 'last_sync', 'created_at', 'updated_at']
        extra_kwargs = {
            'password': {'write_only': True}  # Don't expose password in GET by default
        }


class CustomerRadiusCredentialsDetailSerializer(CustomerRadiusCredentialsSerializer):
    """Detailed serializer that shows password (for admin)."""
    
    class Meta(CustomerRadiusCredentialsSerializer.Meta):
        extra_kwargs = {}