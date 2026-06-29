"""
Hotspot Models for WiFi Access Payments

These models handle hotspot/captive portal functionality where
end-users pay for WiFi access at hotspot locations.

These models live in TENANT schema as they're per-ISP.
"""

import secrets
import uuid
from datetime import timedelta
from decimal import Decimal

from django.db import models
from django.db.models import F
from django.utils import timezone


# ═══════════════════════════════════════════════════════════════════
# HELPER: Canonical Username Generator (Tenant-aware & Collision-free)
# ═══════════════════════════════════════════════════════════════════

def _generate_canonical_username(schema_name="default"):
    """
    Generates a unique 9-char username based on the tenant name.
    Example: tenant_meraki -> MERA-72UY
    """
    import random
    import string
    
    # 1. Extract prefix from schema name (remove 'tenant_')
    raw_name = schema_name.replace('tenant_', '').replace('_', '')
    
    # 2. Get first 4 letters, uppercase, pad with 'X' if too short
    prefix = ''.join(e for e in raw_name if e.isalnum()).upper()
    prefix = (prefix + "XXXX")[:4] 
    
    # 3. Generate unique suffix
    while True:
        suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
        username = f"{prefix}-{suffix}"
        
        # Ensure it is globally unique
        from apps.billing.models.hotspot_models import HotspotClient
        if not HotspotClient.objects.filter(canonical_username=username).exists():
            return username


class HotspotPlan(models.Model):
    """
    Hotspot access plans configured per router.
    End users select these plans when connecting to WiFi.
    """
    
    SPEED_CHOICES = (
        ('1', '1 Mbps'),
        ('2', '2 Mbps'),
        ('5', '5 Mbps'),
        ('10', '10 Mbps'),
        ('15', '15 Mbps'),
        ('20', '20 Mbps'),
        ('50', '50 Mbps'),
        ('100', '100 Mbps'),
    )
    
    VALIDITY_TYPE_CHOICES = (
        ('MINUTES', 'Minutes'),
        ('HOURS', 'Hours'),
        ('DAYS', 'Days'),
        ('UNLIMITED', 'Unlimited'),
    )
    
    SPEED_UNIT_CHOICES = (
        ('MBPS', 'Mbps'),
        ('KBPS', 'Kbps'),
    )
    
    LIMITATION_TYPE_CHOICES = (
        ('UNLIMITED', 'Unlimited'),
        ('DATA', 'Data Plan'),
    )
    
    DATA_UNIT_CHOICES = (
        ('MB', 'MB'),
        ('GB', 'GB'),
    )
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Relationship to router (primary router, can also be linked to multiple via routers M2M)
    router = models.ForeignKey(
        'network.Router',
        on_delete=models.CASCADE,
        related_name='hotspot_plans'
    )
    
    # Multiple routers support
    routers = models.ManyToManyField(
        'network.Router',
        related_name='available_hotspot_plans',
        blank=True,
        help_text="Additional routers where this plan is available"
    )
    
    # Plan Details
    name = models.CharField(max_length=100, help_text="e.g., '1 Hour', 'Daily Pass'")
    description = models.TextField(blank=True)
    
    # Pricing
    price = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3, default='KES')
    
    # ════════════════════════════════════════════════════════════════
    # VALIDITY - Flexible time-based (Minutes/Hours/Days)
    # ════════════════════════════════════════════════════════════════
    validity_type = models.CharField(
        max_length=20,
        choices=VALIDITY_TYPE_CHOICES,
        default='HOURS',
        help_text="Validity period type"
    )
    validity_value = models.PositiveIntegerField(
        default=1,
        help_text="Number of minutes/hours/days based on validity_type"
    )
    
    # Legacy field - keep for backward compatibility
    duration_minutes = models.PositiveIntegerField(
        help_text="Access duration in minutes (e.g., 60 for 1 hour)",
        default=60
    )
    
    # ════════════════════════════════════════════════════════════════
    # DATA LIMITS
    # ════════════════════════════════════════════════════════════════
    limitation_type = models.CharField(
        max_length=20,
        choices=LIMITATION_TYPE_CHOICES,
        default='UNLIMITED',
        help_text="Whether plan has data limits"
    )
    data_limit_value = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Data limit value"
    )
    data_limit_unit = models.CharField(
        max_length=5,
        choices=DATA_UNIT_CHOICES,
        default='MB',
        help_text="Data limit unit (MB or GB)"
    )
    
    # Legacy field - keep for backward compatibility
    data_limit_mb = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Data limit in MB. Null = unlimited"
    )
    
    # ════════════════════════════════════════════════════════════════
    # SPEED SETTINGS - Separate Download/Upload
    # ════════════════════════════════════════════════════════════════
    download_speed = models.PositiveIntegerField(
        default=5,
        help_text="Download speed value"
    )
    upload_speed = models.PositiveIntegerField(
        default=5,
        help_text="Upload speed value"
    )
    speed_unit = models.CharField(
        max_length=10,
        choices=SPEED_UNIT_CHOICES,
        default='MBPS',
        help_text="Speed unit (Mbps or Kbps)"
    )
    
    # Legacy field - keep for backward compatibility
    speed_limit_mbps = models.CharField(
        max_length=10,
        choices=SPEED_CHOICES,
        default='5',
        help_text="Speed limit in Mbps"
    )
    
    # ════════════════════════════════════════════════════════════════
    # SESSION LIMITS
    # ════════════════════════════════════════════════════════════════
    simultaneous_devices = models.PositiveIntegerField(
        default=1,
        help_text="Number of devices that can use this plan simultaneously"
    )
    
    # ════════════════════════════════════════════════════════════════
    # VALID DAYS (Days of week when plan is available)
    # ════════════════════════════════════════════════════════════════
    valid_monday = models.BooleanField(default=True)
    valid_tuesday = models.BooleanField(default=True)
    valid_wednesday = models.BooleanField(default=True)
    valid_thursday = models.BooleanField(default=True)
    valid_friday = models.BooleanField(default=True)
    valid_saturday = models.BooleanField(default=True)
    valid_sunday = models.BooleanField(default=True)
    
    # MikroTik Integration
    mikrotik_profile = models.CharField(
        max_length=100,
        blank=True,
        help_text="MikroTik hotspot user profile name"
    )
    
    # Display
    is_active = models.BooleanField(default=True)
    is_popular = models.BooleanField(default=False)
    is_global_template = models.BooleanField(
        default=False,
        help_text="If True, this plan is cloned automatically to any new router that gets created"
    )
    sort_order = models.PositiveIntegerField(default=0)
    
    # ════════════════════════════════════════════════════════════════
    # FREE TRIAL FIELDS
    # ════════════════════════════════════════════════════════════════
    is_free_trial = models.BooleanField(
        default=False,
        help_text="If True, this plan is free — no payment required. One claim per device (MAC) ever."
    )
    trial_duration_minutes = models.PositiveIntegerField(
        default=30,
        help_text="Duration in minutes for the free trial session"
    )
    
    # Metadata
    created_by = models.ForeignKey(
        'core.User',
        on_delete=models.SET_NULL,
        null=True,
        related_name='created_hotspot_plans'
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['router', 'sort_order', 'price']
        verbose_name = 'Hotspot Plan'
        verbose_name_plural = 'Hotspot Plans'
        unique_together = ['router', 'name']
        indexes = [
            models.Index(
                fields=['router', 'is_active', 'sort_order', 'price'],
                name='hotspot_plan_fast_list_idx',
            ),
        ]
    
    def __str__(self):
        return f"{self.router.name} - {self.name} (KES {self.price})"
    
    def save(self, *args, **kwargs):
        # Sync new fields to legacy fields for backward compatibility
        self._sync_legacy_fields()
        super().save(*args, **kwargs)
    
    def _sync_legacy_fields(self):
        """Sync new fields to legacy fields for backward compatibility"""
        # Sync validity to duration_minutes
        if self.validity_type == 'MINUTES':
            self.duration_minutes = self.validity_value
        elif self.validity_type == 'HOURS':
            self.duration_minutes = self.validity_value * 60
        elif self.validity_type == 'DAYS':
            self.duration_minutes = self.validity_value * 1440
        elif self.validity_type == 'UNLIMITED':
            self.duration_minutes = 525600  # 1 year in minutes
        
        # Sync data limit
        if self.limitation_type == 'DATA' and self.data_limit_value:
            if self.data_limit_unit == 'GB':
                self.data_limit_mb = self.data_limit_value * 1024
            else:
                self.data_limit_mb = self.data_limit_value
        else:
            self.data_limit_mb = None
        
        # Sync speed to legacy field
        self.speed_limit_mbps = str(self.download_speed) if self.speed_unit == 'MBPS' else str(self.download_speed // 1024)
    
    @property
    def duration_display(self) -> str:
        """Human-readable duration"""
        if self.validity_type == 'UNLIMITED':
            return "Unlimited"
        elif self.validity_type == 'MINUTES':
            return f"{self.validity_value} minute{'s' if self.validity_value > 1 else ''}"
        elif self.validity_type == 'HOURS':
            return f"{self.validity_value} hour{'s' if self.validity_value > 1 else ''}"
        elif self.validity_type == 'DAYS':
            return f"{self.validity_value} day{'s' if self.validity_value > 1 else ''}"
        # Fallback to legacy field
        minutes = self.duration_minutes
        if minutes < 60:
            return f"{minutes} minutes"
        elif minutes < 1440:
            hours = minutes // 60
            return f"{hours} hour{'s' if hours > 1 else ''}"
        else:
            days = minutes // 1440
            return f"{days} day{'s' if days > 1 else ''}"
    
    @property
    def data_limit_display(self) -> str:
        """Human-readable data limit"""
        if self.limitation_type == 'UNLIMITED' or not self.data_limit_value:
            return "Unlimited"
        if self.data_limit_unit == 'GB':
            return f"{self.data_limit_value} GB"
        return f"{self.data_limit_value} MB"
    
    @property
    def speed_display(self) -> str:
        """Human-readable speed"""
        unit = 'Mbps' if self.speed_unit == 'MBPS' else 'Kbps'
        if self.download_speed == self.upload_speed:
            return f"{self.download_speed} {unit}"
        return f"{self.download_speed}/{self.upload_speed} {unit}"
    
    @property
    def valid_days_list(self) -> list:
        """List of valid days"""
        days = []
        if self.valid_monday: days.append('Monday')
        if self.valid_tuesday: days.append('Tuesday')
        if self.valid_wednesday: days.append('Wednesday')
        if self.valid_thursday: days.append('Thursday')
        if self.valid_friday: days.append('Friday')
        if self.valid_saturday: days.append('Saturday')
        if self.valid_sunday: days.append('Sunday')
        return days
    
    @property
    def total_validity_minutes(self) -> int:
        """Total validity in minutes for RADIUS"""
        if self.validity_type == 'UNLIMITED':
            return 525600  # 1 year
        elif self.validity_type == 'MINUTES':
            return self.validity_value
        elif self.validity_type == 'HOURS':
            return self.validity_value * 60
        elif self.validity_type == 'DAYS':
            return self.validity_value * 1440
        return self.duration_minutes


# ═══════════════════════════════════════════════════════════════════
# FREE TRIAL USAGE TRACKING
# ═══════════════════════════════════════════════════════════════════

class HotspotFreeTrialUsage(models.Model):
    """Tracks which MAC addresses have claimed the free trial. One claim per MAC, ever."""
    mac_address = models.CharField(max_length=17, unique=True, db_index=True)
    claimed_at = models.DateTimeField(auto_now_add=True)
    router = models.ForeignKey('network.Router', on_delete=models.SET_NULL, null=True)
    access_code = models.CharField(max_length=25, blank=True)

    class Meta:
        verbose_name = 'Free Trial Usage'
        verbose_name_plural = 'Free Trial Usages'

    def __str__(self):
        return f"{self.mac_address} claimed at {self.claimed_at}"


# ═══════════════════════════════════════════════════════════════════
# CLIENT IDENTITY MODELS FOR MAC RANDOMIZATION RESILIENCE
# ═══════════════════════════════════════════════════════════════════

class HotspotClient(models.Model):
    """
    First-class client identity for hotspot users.
    Tracks a real human across multiple devices and MAC randomization.
    """
    schema_name = models.SlugField(max_length=63, db_index=True, editable=False)
    
    # Primary identity - phone number is the stable identifier
    canonical_phone = models.CharField(
        max_length=15, 
        blank=True, 
        null=True, 
        db_index=True,
        help_text="Primary phone number for this client (stable across devices)"
    )
    
    # NEW: Permanent RADIUS username (generated once, reused forever)
    canonical_username = models.CharField(
        max_length=20,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        help_text=(
            "Permanent RADIUS username for this hotspot client (e.g. MXA-BKCS). "
            "Auto-generated on first purchase. Reused across all sessions on any device."
        ),
    )
    
    # Timestamps
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    
    # Analytics
    total_sessions = models.PositiveIntegerField(default=0)
    total_spend = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    
    # Optional: Email as secondary identifier
    email = models.EmailField(blank=True, null=True, db_index=True)
    
    # Optional: Custom client ID from external systems
    external_client_id = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    
    class Meta:
        verbose_name = 'Hotspot Client'
        verbose_name_plural = 'Hotspot Clients'
        indexes = [
            models.Index(fields=['schema_name', 'canonical_phone']),
            models.Index(fields=['schema_name', 'email']),
            models.Index(fields=['schema_name', 'external_client_id']),
            models.Index(fields=['schema_name', 'last_seen_at']),
            models.Index(fields=['canonical_username']),  # Index for username lookups
        ]
        unique_together = [
            ['schema_name', 'canonical_phone'],
            ['schema_name', 'email'],
        ]
    
    def __str__(self):
        identifier = self.canonical_phone or self.email or self.canonical_username or 'Anonymous'
        return f"{identifier} - {self.total_sessions} sessions"
    
    def update_analytics(self, session_amount: Decimal = None):
        """Update analytics after a session completes"""
        from apps.billing.models.hotspot_models import HotspotSession
        self.total_sessions = HotspotSession.objects.filter(hotspot_client=self).count()
        if session_amount:
            self.total_spend += session_amount
        self.last_seen_at = timezone.now()
        self.save(update_fields=['total_sessions', 'total_spend', 'last_seen_at'])
    
    @classmethod
    def get_or_create_by_phone(cls, schema_name: str, phone_number: str):
        """
        Get or create a client by phone number.
        Always ensures canonical_username is set (generated on first purchase).
        """
        if not phone_number:
            return None

        from django.utils import timezone as tz

        client, created = cls.objects.get_or_create(
            schema_name=schema_name,
            canonical_phone=phone_number,
            defaults={"first_seen_at": tz.now()},
        )

        # Generate username if missing (new client OR migrated existing without one)
        if not client.canonical_username:
            client.canonical_username = _generate_canonical_username(schema_name)
            client.save(update_fields=["canonical_username"])

        if not created:
            client.last_seen_at = tz.now()
            client.save(update_fields=["last_seen_at"])

        return client
    
    @classmethod
    def get_or_create_by_mac(cls, schema_name: str, mac_address: str,
                              phone_number: str = None):
        """
        Resolve a hotspot client identity, preferring MAC lookup then phone.

        Decision tree:
        1. MAC is already registered → return that client (handles returning devices)
        2. Phone number provided → get_or_create_by_phone (phone is more stable than MAC)
        3. Neither → create an anonymous client keyed by MAC (unlikely edge case)

        This intentionally does NOT make MAC the primary key because modern devices
        randomise MACs per network. Phone is the stable anchor; MAC is a hint.
        """
        from apps.billing.models.hotspot_models import HotspotClientDevice

        # 1. MAC lookup via device registry
        device = (
            HotspotClientDevice.objects
            .filter(mac_address=mac_address)
            .select_related("client")
            .first()
        )
        if device and device.client:
            client = device.client
            # Backfill username if this client predates the feature
            if not client.canonical_username:
                client.canonical_username = _generate_canonical_username(schema_name)
                client.save(update_fields=["canonical_username"])
            return client

        # 2. Phone lookup (primary stable identifier)
        if phone_number:
            return cls.get_or_create_by_phone(schema_name, phone_number)

        # 3. Truly anonymous: key by MAC (e.g. Smart TV with no SIM)
        anon_phone = f"MAC-{mac_address.replace(':', '').upper()}"
        return cls.get_or_create_by_phone(schema_name, anon_phone)


class HotspotClientDevice(models.Model):
    """
    Tracks devices belonging to a hotspot client.
    Handles MAC randomization by linking multiple MACs to same client.
    """
    client = models.ForeignKey(
        HotspotClient, 
        on_delete=models.CASCADE, 
        related_name='devices'
    )
    
    mac_address = models.CharField(
        max_length=17, 
        db_index=True,
        help_text="Device MAC address (may change due to randomization)"
    )
    
    # Device token for push notifications (optional)
    device_token = models.CharField(
        max_length=128, 
        blank=True, 
        null=True, 
        db_index=True,
        help_text="Push notification token for this device"
    )
    
    # Device info
    device_name = models.CharField(max_length=100, blank=True, null=True)
    device_model = models.CharField(max_length=100, blank=True, null=True)
    os_version = models.CharField(max_length=50, blank=True, null=True)
    
    # Timestamps
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    
    # Metadata
    user_agent = models.TextField(blank=True, null=True)
    
    class Meta:
        verbose_name = 'Hotspot Client Device'
        verbose_name_plural = 'Hotspot Client Devices'
        unique_together = [('client', 'mac_address')]
        indexes = [
            models.Index(fields=['mac_address']),
            models.Index(fields=['device_token']),
            models.Index(fields=['client', 'last_seen_at']),
        ]
    
    def __str__(self):
        return f"{self.client.canonical_phone or self.client.canonical_username or self.client.id} - {self.mac_address}"
    
    @classmethod
    def record_device(cls, client: HotspotClient, mac_address: str, **kwargs):
        """Record or update a device for a client"""
        if not mac_address:
            return None
        
        device, created = cls.objects.get_or_create(
            client=client,
            mac_address=mac_address,
            defaults={
                'device_name': kwargs.get('device_name'),
                'device_model': kwargs.get('device_model'),
                'os_version': kwargs.get('os_version'),
                'user_agent': kwargs.get('user_agent'),
            }
        )
        if not created:
            # Update last_seen and any provided fields
            device.last_seen_at = timezone.now()
            if kwargs.get('device_name'):
                device.device_name = kwargs['device_name']
            if kwargs.get('device_model'):
                device.device_model = kwargs['device_model']
            if kwargs.get('os_version'):
                device.os_version = kwargs['os_version']
            if kwargs.get('user_agent'):
                device.user_agent = kwargs['user_agent']
            device.save()
        return device


class HotspotSession(models.Model):
    """
    Tracks a hotspot purchase/session from payment to activation.
    Created when user initiates payment, updated on payment completion.
    """
    
    STATUS_CHOICES = (
        ('pending', 'Pending Payment'),
        ('paid', 'Paid - Activating'),
        ('active', 'Active'),
        ('expired', 'Expired'),
        ('failed', 'Payment Failed'),
        ('cancelled', 'Cancelled'),
    )
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Unique session identifier (shown to user)
    session_id = models.CharField(
        max_length=50,
        unique=True,
        help_text="Unique session ID like HS_1234567890_ABCD"
    )
    
    # Relationships
    router = models.ForeignKey(
        'network.Router',
        on_delete=models.CASCADE,
        related_name='hotspot_sessions'
    )
    plan = models.ForeignKey(
        HotspotPlan,
        on_delete=models.CASCADE,
        related_name='sessions'
    )
    
    # Client Identity (for MAC randomization resilience)
    hotspot_client = models.ForeignKey(
        'billing.HotspotClient',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='sessions',
        help_text="The client who owns this session (stable across MAC randomization)"
    )
    
    # User Details (no auth required for hotspot)
    phone_number = models.CharField(max_length=15)
    mac_address = models.CharField(
        max_length=17,
        help_text="Device MAC address (AA:BB:CC:DD:EE:FF)"
    )
    
    # Payment Details
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payhero_checkout_id = models.CharField(max_length=100, blank=True, null=True)
    mpesa_receipt = models.CharField(max_length=50, blank=True, null=True)
    
    # Tuma Payment Request IDs for tracking
    tuma_merchant_request_id = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        db_index=True,
        help_text="Tuma merchant request ID for this session's payment"
    )
    tuma_checkout_request_id = models.CharField(
        max_length=120,
        blank=True,
        null=True,
        db_index=True,
        help_text="Tuma checkout request ID for this session's payment"
    )
    
    # Explicit Payment Link (replaces unsafe writes)
    payment = models.ForeignKey(
        'billing.Payment',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='hotspot_sessions',
        help_text="The payment record associated with this hotspot session"
    )
    
    # Session Details
    access_code = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        help_text="WiFi access code (e.g., WIFI-1234)"
    )
    
    # NEW: Store the actual RADIUS username used for this session
    radius_username = models.CharField(
        max_length=25,
        blank=True,
        null=True,
        help_text="RADIUS credential used (may differ from access_code for multi-device clients)"
    )
    
    # Status & Timing
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    activated_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    
    # ROAMING ANALYTICS
    is_roaming = models.BooleanField(
        default=False, 
        help_text="True if purchased at a different router than their last session"
    )
    roamed_from = models.CharField(
        max_length=100, 
        blank=True, 
        null=True,
        help_text="The name of the previous router they connected to"
    )
    
    # Data Usage (updated by MikroTik)
    data_used_mb = models.PositiveIntegerField(default=0)
    
    # Failure tracking
    failure_reason = models.TextField(blank=True, null=True)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Hotspot Session'
        verbose_name_plural = 'Hotspot Sessions'
        indexes = [
            models.Index(fields=['session_id']),
            models.Index(fields=['payhero_checkout_id']),
            models.Index(fields=['phone_number']),
            models.Index(fields=['mac_address']),
            models.Index(fields=['status']),
            models.Index(fields=['tuma_merchant_request_id']),
            models.Index(fields=['tuma_checkout_request_id']),
            models.Index(fields=['radius_username']),  # Index for RADIUS username lookups
        ]
    
    def __str__(self):
        client_info = f" - Client:{self.hotspot_client.canonical_phone or self.hotspot_client.canonical_username}" if self.hotspot_client else ""
        return f"{self.session_id} - {self.phone_number}{client_info} ({self.status})"
    
    @classmethod
    def generate_session_id(cls) -> str:
        """Generate unique session ID"""
        timestamp = int(timezone.now().timestamp())
        random_part = secrets.token_hex(2).upper()
        return f"HS_{timestamp}_{random_part}"
    
    @classmethod
    def generate_access_code(cls) -> str:
        """Generate WiFi access code"""
        return f"WIFI-{secrets.token_hex(2).upper()}"
    
    def link_to_client(self, client: 'HotspotClient'):
        """Link this session to a client"""
        self.hotspot_client = client
        self.save(update_fields=['hotspot_client'])
        
        # Update client analytics
        client.update_analytics(self.amount)
    
    def get_or_create_client(self) -> 'HotspotClient':
        """
        Get or create a client for this session based on phone number.
        
        FIXED: Uses connection.schema_name instead of self._state.db
        which was causing schema resolution issues in multi-tenant context.
        """
        if self.hotspot_client:
            return self.hotspot_client
        
        if self.phone_number:
            # Get the current schema from the database connection
            from django.db import connection
            current_schema = connection.schema_name
            
            # Use the current schema name for client lookup/creation
            client = HotspotClient.get_or_create_by_phone(
                schema_name=current_schema,
                phone_number=self.phone_number
            )
            if client:
                self.hotspot_client = client
                self.save(update_fields=['hotspot_client'])
                return client
        
        return None
    
    def activate(self, access_code: str = None):
        """
        Mark session as active after successful payment.
        Uses select_for_update to prevent concurrent double-activation.
        Safe to call both inside and outside an existing transaction.
        """
        from django.db import transaction
        from apps.billing.models.hotspot_models import HotspotSession  # avoid circular at module level

        # Guarantee a transaction for select_for_update()
        if not transaction.get_connection().in_atomic_block:
            with transaction.atomic():
                return self.activate(access_code)

        # Atomic idempotency guard — re-fetch with a row lock
        locked = (
            HotspotSession.objects
            .select_for_update()
            .filter(pk=self.pk, status__in=('pending', 'paid'))
            .first()
        )
        if locked is None:
            # Already active (or failed/expired) — nothing to do
            return

        # ADD THESE TWO LINES BEFORE CHANGING THE STATUS
        prev_status = locked.status  
        locked._prev_status = prev_status  

        locked.status = 'active'
        locked.access_code = access_code or locked.access_code or self.generate_access_code()
        locked.radius_username = access_code or locked.access_code or self.generate_access_code()
        locked.activated_at = timezone.now()
        locked.expires_at = timezone.now() + timedelta(minutes=locked.plan.duration_minutes)
        locked.save()

        # Copy updated fields back to self so callers see the new state
        self.status = locked.status
        self.access_code = locked.access_code
        self.radius_username = locked.radius_username
        self.activated_at = locked.activated_at
        self.expires_at = locked.expires_at

        # Update client analytics if client exists
        if self.hotspot_client:
            self.hotspot_client.update_analytics(self.amount)

        # METERED BILLING HOOK (Hotspot Revenue) — runs only once due to row lock
        try:
            from django.db import connection
            from django_tenants.utils import schema_context, get_public_schema_name
            from apps.subscriptions.models import BillingCycle
            from apps.core.models import Tenant
            from decimal import Decimal

            tenant_schema = connection.schema_name

            with schema_context(get_public_schema_name()):
                current_tenant = Tenant.objects.get(schema_name=tenant_schema)
                active_cycle = BillingCycle.objects.filter(
                    tenant=current_tenant,
                    status='active'
                ).first()

                if active_cycle:
                    BillingCycle.objects.filter(id=active_cycle.id).update(
                        hotspot_revenue_accumulated=F('hotspot_revenue_accumulated') + Decimal(str(self.amount))
                    )
        except Exception as e:
            import logging as _log
            _log.getLogger(__name__).error("Failed to record hotspot revenue: %s", e)
    
    def refund_or_cancel(self, reason: str = "Refunded"):
        """Reverses a previously active session and decrements the ledger."""
        was_active = self.status == 'active'
        self.status = 'cancelled'
        self.failure_reason = reason
        self.save()

        if was_active:
            # Decrease client spend if client exists
            if self.hotspot_client:
                self.hotspot_client.total_spend -= self.amount
                self.hotspot_client.save(update_fields=['total_spend'])
            
            try:
                from django.db import connection
                from django_tenants.utils import schema_context, get_public_schema_name
                from apps.subscriptions.models import BillingCycle
                from apps.core.models import Tenant
                
                tenant_schema = connection.schema_name
                
                with schema_context(get_public_schema_name()):
                    current_tenant = Tenant.objects.get(schema_name=tenant_schema)
                    active_cycle = BillingCycle.objects.filter(
                        tenant=current_tenant, 
                        status='active'
                    ).first()
                    
                    if active_cycle:
                        # Use F() expression to safely decrement the revenue
                        BillingCycle.objects.filter(id=active_cycle.id).update(
                            hotspot_revenue_accumulated=F('hotspot_revenue_accumulated') - Decimal(str(self.amount))
                        )
            except Exception as e:
                import logging
                logging.getLogger(__name__).error(f"Failed to reverse hotspot revenue: {e}")
    
    def mark_paid(self, mpesa_receipt: str = None):
        """Mark as paid, pending activation"""
        self.status = 'paid'
        if mpesa_receipt:
            self.mpesa_receipt = mpesa_receipt
        self.save()
    
    def mark_failed(self, reason: str = None):
        """Mark payment as failed"""
        self.status = 'failed'
        self.failure_reason = reason
        self.save()
    
    def mark_expired(self):
        """Mark session as expired"""
        self.status = 'expired'
        self.save()
    
    def link_payment(self, payment):
        """Link a payment record to this hotspot session"""
        self.payment = payment
        self.save()
    
    def set_tuma_request_ids(self, merchant_request_id: str = None, checkout_request_id: str = None):
        """Set Tuma request IDs for tracking"""
        if merchant_request_id:
            self.tuma_merchant_request_id = merchant_request_id
        if checkout_request_id:
            self.tuma_checkout_request_id = checkout_request_id
        self.save()
    
    @property
    def is_active(self) -> bool:
        """Check if session is currently active"""
        if self.status != 'active':
            return False
        if self.expires_at and self.expires_at < timezone.now():
            return False
        return True
    
    @property
    def time_remaining_minutes(self) -> int:
        """Minutes remaining in session"""
        if not self.is_active or not self.expires_at:
            return 0
        delta = self.expires_at - timezone.now()
        return max(0, int(delta.total_seconds() / 60))
    
    @property
    def data_remaining_mb(self) -> int:
        """Data remaining in MB (None if unlimited)"""
        if not self.plan.data_limit_mb:
            return None
        return max(0, self.plan.data_limit_mb - self.data_used_mb)


class HotspotBranding(models.Model):
    """
    Branding configuration for captive portal.
    Customizes the look and feel of the WiFi login page.
    """
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Can be per-router or per-tenant
    router = models.OneToOneField(
        'network.Router',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='hotspot_branding'
    )
    is_default = models.BooleanField(
        default=False,
        help_text="Default branding for all routers without specific branding"
    )
    
    # Visual Branding
    company_name = models.CharField(max_length=100)
    logo = models.ImageField(
        upload_to='hotspot/logos/',
        null=True,
        blank=True
    )
    background_image = models.ImageField(
        upload_to='hotspot/backgrounds/',
        null=True,
        blank=True
    )
    
    # Colors
    primary_color = models.CharField(max_length=7, default='#3B82F6')
    secondary_color = models.CharField(max_length=7, default='#1E40AF')
    text_color = models.CharField(max_length=7, default='#1F2937')
    background_color = models.CharField(max_length=7, default='#FFFFFF')
    
    # Content
    welcome_title = models.CharField(max_length=200, default='Welcome to WiFi')
    welcome_message = models.TextField(blank=True)
    terms_and_conditions = models.TextField(blank=True)
    support_phone = models.CharField(max_length=20, blank=True)
    support_email = models.EmailField(blank=True)
    
    # Social Links
    facebook_url = models.URLField(blank=True)
    twitter_url = models.URLField(blank=True)
    instagram_url = models.URLField(blank=True)
    website_url = models.URLField(blank=True)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = 'Hotspot Branding'
        verbose_name_plural = 'Hotspot Branding'
    
    def __str__(self):
        if self.router:
            return f"Branding for {self.router.name}"
        return f"Default Branding - {self.company_name}"