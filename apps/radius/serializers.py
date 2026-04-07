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
    """Specific serializer for the Online Users frontend dashboard."""

    full_name = serializers.SerializerMethodField()
    phone_number = serializers.SerializerMethodField()
    mac_address = serializers.CharField(source='callingstationid', read_only=True)
    ip_address = serializers.CharField(source='framedipaddress', read_only=True)
    uptime = serializers.SerializerMethodField()
    usage = serializers.SerializerMethodField()
    router = serializers.SerializerMethodField()
    service_type = serializers.SerializerMethodField()
    # NEW: surfaced so the frontend can distinguish PPPoE customer vs hotspot user
    canonical_username = serializers.SerializerMethodField()

    class Meta:
        model = RadAcct
        fields = [
            'radacctid', 'acctsessionid', 'username', 'full_name',
            'phone_number', 'mac_address', 'ip_address',
            'uptime', 'usage', 'router', 'service_type',
            'canonical_username',
        ]

    # ─────────────────────────────────────────────────────────────────────────

    def _resolve_hotspot_session(self, obj):
        """
        Try to find the HotspotSession whose access_code == obj.username.
        Returns the session (with prefetched hotspot_client) or None.
        """
        from apps.billing.models.hotspot_models import HotspotSession
        return (
            HotspotSession.objects
            .filter(access_code=obj.username)
            .select_related('hotspot_client')
            .first()
        )

    def _resolve_radius_credentials(self, obj):
        """Look up CustomerRadiusCredentials by RADIUS username."""
        from apps.radius.models import CustomerRadiusCredentials
        return (
            CustomerRadiusCredentials.objects
            .filter(username=obj.username)
            .select_related('customer__user')
            .first()
        )

    # ─────────────────────────────────────────────────────────────────────────

    def get_full_name(self, obj) -> str:
        """
        PPPoE → customer full name (e.g. "John Doe")
        Hotspot → canonical username  (e.g. "MXA-BKCS")

        The frontend shows these differently:
          PPPoE:   "John Doe"   / phone below
          Hotspot: "MXA-BKCS"  / canonical_phone below
        """
        # 1. Direct FK (fastest)
        if obj.customer:
            return obj.customer.full_name

        # 2. PPPoE via RADIUS credentials
        creds = self._resolve_radius_credentials(obj)
        if creds and creds.customer:
            return creds.customer.full_name

        # 3. Hotspot session — return the username (it IS the identity)
        h_session = self._resolve_hotspot_session(obj)
        if h_session:
            return obj.username  # e.g. "MXA-BKCS"

        # 4. Absolute fallback
        return obj.username

    def get_phone_number(self, obj) -> str:
        """
        PPPoE → customer phone number
        Hotspot → canonical_phone from HotspotClient (the buyer's phone)
        """
        # 1. Direct FK
        if obj.customer and hasattr(obj.customer, 'user') and obj.customer.user:
            return obj.customer.user.phone_number or "N/A"

        # 2. PPPoE credentials
        creds = self._resolve_radius_credentials(obj)
        if creds and creds.customer and hasattr(creds.customer, 'user'):
            return creds.customer.user.phone_number or "N/A"

        # 3. Hotspot session
        h_session = self._resolve_hotspot_session(obj)
        if h_session:
            # Prefer the client's canonical_phone (normalised), fall back to session phone
            if h_session.hotspot_client and h_session.hotspot_client.canonical_phone:
                phone = h_session.hotspot_client.canonical_phone
                # Strip the synthetic "MAC-..." prefix used for anonymous clients
                return phone if not phone.startswith("MAC-") else "N/A"
            return h_session.phone_number or "N/A"

        return "N/A"

    def get_canonical_username(self, obj) -> str | None:
        """
        For hotspot sessions: the permanent RADIUS username (== access_code).
        For PPPoE: None (the customer name is the identity there).

        The frontend uses this to decide display mode:
          - None → show customer name prominently
          - "MXA-BKCS" → show username prominently, phone secondary
        """
        h_session = self._resolve_hotspot_session(obj)
        if h_session:
            # The session's access_code == the client's canonical_username
            return obj.username
        return None

    def get_router(self, obj) -> str:
        # 1. Direct FK
        if obj.router:
            return obj.router.name

        # 2. Resolve from NAS IP
        from apps.network.models import Router
        r = Router.objects.filter(
            models.Q(vpn_ip_address=obj.nasipaddress) |
            models.Q(ip_address=obj.nasipaddress)
        ).first()
        return r.name if r else obj.nasipaddress or "Unknown Router"

    def get_uptime(self, obj) -> str:
        if not obj.acctstarttime:
            return "0s"
        delta = timezone.now() - obj.acctstarttime
        total_seconds = int(delta.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        if hours > 0:
            return f"{hours}h {minutes}m"
        return f"{minutes}m {seconds}s"

    def get_usage(self, obj) -> str:
        """
        Calculate cumulative usage since the START of the user's current subscription period.
        This ensures usage resets to 0 when a subscription renews, not on a rolling 30-day window.
        
        Strategy:
        1. Look up the customer's RADIUS expiration date (set when subscription was activated)
        2. Derive the period start by subtracting the plan duration from expiration
        3. If we can't determine period start, fall back to 30 days ago
        """
        from django.db.models import Sum
        from datetime import datetime
        
        # ── Determine the subscription period start ────────────────────────
        period_start = None
        
        try:
            # Get the Expiration attribute for this RADIUS user
            expiration_check = RadCheck.objects.filter(
                username=obj.username,
                attribute='Expiration'
            ).first()
            
            if expiration_check and expiration_check.value:
                # Parse the FreeRADIUS expiration format: "Jan 06 2026 14:30:00"
                expiration_dt = datetime.strptime(
                    expiration_check.value, "%b %d %Y %H:%M:%S"
                )
                expiration_dt = timezone.make_aware(expiration_dt) if timezone.is_naive(expiration_dt) else expiration_dt
                
                # Get the plan duration from CustomerRadiusCredentials → customer → service → plan
                from apps.radius.models import CustomerRadiusCredentials
                creds = CustomerRadiusCredentials.objects.filter(
                    username=obj.username
                ).select_related('customer__services__plan').first()
                
                plan_duration_days = 30  # safe default
                if creds and creds.customer:
                    service = creds.customer.services.filter(
                        status='ACTIVE', plan__isnull=False
                    ).first()
                    if service and service.plan:
                        plan_duration_days = service.plan.duration_days or 30
                
                # Period start = expiration minus plan duration
                period_start = expiration_dt - timezone.timedelta(days=plan_duration_days)
                
        except Exception:
            # If anything goes wrong, fall back gracefully — don't crash the serializer
            pass
        
        # Fallback: use 30 days ago (original behavior)
        if period_start is None:
            period_start = timezone.now() - timezone.timedelta(days=30)
        
        # ── Sum usage since period_start ───────────────────────────────────
        historical = RadAcct.objects.filter(
            username=obj.username,
            acctstoptime__isnull=False,
            acctstarttime__gte=period_start  # ← NOW ANCHORED TO SUBSCRIPTION START
        ).aggregate(
            total_in=Sum('acctinputoctets'),
            total_out=Sum('acctoutputoctets')
        )
        
        # Current session bytes
        current_in = obj.acctinputoctets or 0
        current_out = obj.acctoutputoctets or 0
        
        # Historical bytes (default to 0 if None)
        hist_in = historical['total_in'] or 0
        hist_out = historical['total_out'] or 0
        
        # Total cumulative usage since subscription start
        total_bytes = current_in + current_out + hist_in + hist_out
        
        # Format nicely
        mb = total_bytes / (1024 * 1024)
        if mb >= 1024:
            return f"{mb / 1024:.2f} GB"
        return f"{mb:.2f} MB"

    def get_service_type(self, obj) -> str:
        """
        PPP framing → PPPoE.
        Everything else that matches a hotspot session → HOTSPOT.
        Fallback heuristic via nasporttype.
        """
        if obj.framedprotocol == 'PPP':
            return 'PPPOE'

        # Check if there's a hotspot session for this username
        from apps.billing.models.hotspot_models import HotspotSession
        if HotspotSession.objects.filter(access_code=obj.username).exists():
            return 'HOTSPOT'

        # nasporttype hints
        if obj.nasporttype in ('Wireless-802.11', 'Wireless-Other'):
            return 'HOTSPOT'

        return 'PPPOE'


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