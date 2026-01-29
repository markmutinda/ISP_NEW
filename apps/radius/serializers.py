"""
RADIUS Serializers
"""
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
    RadiusBandwidthProfile
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
    password = serializers.CharField(max_length=253, write_only=True)
    
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
