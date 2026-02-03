"""
RADIUS Models - FreeRADIUS Database Schema

These models mirror the FreeRADIUS SQL schema for:
- User authentication (radcheck, radreply)
- Session accounting (radacct)
- NAS/Router management (nas)
- User groups (radusergroup, radgroupcheck, radgroupreply)

Reference: https://wiki.freeradius.org/guide/SQL-HOWTO
"""

from django.db import models
from django.utils import timezone
import uuid


# ────────────────────────────────────────────────────────────────
# RADIUS CHECK - User Authentication Attributes
# ────────────────────────────────────────────────────────────────

class RadCheck(models.Model):
    """
    RADIUS check attributes for user authentication.
    Maps to FreeRADIUS 'radcheck' table.
    
    Common attributes:
    - Cleartext-Password: Plain text password
    - NT-Password: NTLM hash
    - Expiration: Account expiry date
    - Simultaneous-Use: Max concurrent sessions
    """
    OPERATORS = [
        (':=', 'Set (override)'),
        ('==', 'Equal'),
        ('!=', 'Not Equal'),
        ('>', 'Greater Than'),
        ('>=', 'Greater Than or Equal'),
        ('<', 'Less Than'),
        ('<=', 'Less Than or Equal'),
        ('=~', 'Regex Match'),
        ('!~', 'Regex Not Match'),
        ('=*', 'Attribute Exists'),
        ('!*', 'Attribute Not Exists'),
    ]
    
    id = models.AutoField(primary_key=True)
    username = models.CharField(max_length=64, db_index=True)
    attribute = models.CharField(max_length=64)
    op = models.CharField(max_length=2, choices=OPERATORS, default=':=')
    value = models.CharField(max_length=253)
    
    # Link to customer (optional - for Netily integration)
    customer = models.ForeignKey(
        'customers.Customer',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='radius_checks'
    )
    
    class Meta:
        db_table = 'radcheck'
        verbose_name = 'RADIUS Check'
        verbose_name_plural = 'RADIUS Checks'
        indexes = [
            models.Index(fields=['username', 'attribute']),
        ]
    
    def __str__(self):
        return f"{self.username}: {self.attribute} {self.op} {self.value}"


class RadReply(models.Model):
    """
    RADIUS reply attributes sent back to NAS after authentication.
    Maps to FreeRADIUS 'radreply' table.
    
    Common attributes:
    - Framed-IP-Address: Assigned IP
    - Framed-IP-Netmask: Subnet mask
    - Mikrotik-Rate-Limit: Bandwidth limit
    - Session-Timeout: Session duration
    """
    OPERATORS = [
        ('=', 'Add'),
        (':=', 'Set (override)'),
        ('+=', 'Append'),
    ]
    
    id = models.AutoField(primary_key=True)
    username = models.CharField(max_length=64, db_index=True)
    attribute = models.CharField(max_length=64)
    op = models.CharField(max_length=2, choices=OPERATORS, default=':=')
    value = models.CharField(max_length=253)
    
    # Link to customer
    customer = models.ForeignKey(
        'customers.Customer',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='radius_replies'
    )
    
    class Meta:
        db_table = 'radreply'
        verbose_name = 'RADIUS Reply'
        verbose_name_plural = 'RADIUS Replies'
        indexes = [
            models.Index(fields=['username', 'attribute']),
        ]
    
    def __str__(self):
        return f"{self.username}: {self.attribute} {self.op} {self.value}"


# ────────────────────────────────────────────────────────────────
# RADIUS GROUPS - Group-based Policy Management
# ────────────────────────────────────────────────────────────────

class RadUserGroup(models.Model):
    """
    User to group mapping.
    Maps to FreeRADIUS 'radusergroup' table.
    """
    id = models.AutoField(primary_key=True)
    username = models.CharField(max_length=64, db_index=True)
    groupname = models.CharField(max_length=64, db_index=True)
    priority = models.IntegerField(default=1)
    
    class Meta:
        db_table = 'radusergroup'
        verbose_name = 'RADIUS User Group'
        verbose_name_plural = 'RADIUS User Groups'
        unique_together = ['username', 'groupname']
    
    def __str__(self):
        return f"{self.username} -> {self.groupname}"


class RadGroupCheck(models.Model):
    """
    Group-level check attributes.
    Maps to FreeRADIUS 'radgroupcheck' table.
    """
    OPERATORS = RadCheck.OPERATORS
    
    id = models.AutoField(primary_key=True)
    groupname = models.CharField(max_length=64, db_index=True)
    attribute = models.CharField(max_length=64)
    op = models.CharField(max_length=2, choices=OPERATORS, default=':=')
    value = models.CharField(max_length=253)
    
    class Meta:
        db_table = 'radgroupcheck'
        verbose_name = 'RADIUS Group Check'
        verbose_name_plural = 'RADIUS Group Checks'
    
    def __str__(self):
        return f"{self.groupname}: {self.attribute} {self.op} {self.value}"


class RadGroupReply(models.Model):
    """
    Group-level reply attributes.
    Maps to FreeRADIUS 'radgroupreply' table.
    """
    OPERATORS = RadReply.OPERATORS
    
    id = models.AutoField(primary_key=True)
    groupname = models.CharField(max_length=64, db_index=True)
    attribute = models.CharField(max_length=64)
    op = models.CharField(max_length=2, choices=OPERATORS, default=':=')
    value = models.CharField(max_length=253)
    
    class Meta:
        db_table = 'radgroupreply'
        verbose_name = 'RADIUS Group Reply'
        verbose_name_plural = 'RADIUS Group Replies'
    
    def __str__(self):
        return f"{self.groupname}: {self.attribute} {self.op} {self.value}"


# ────────────────────────────────────────────────────────────────
# RADIUS ACCOUNTING - Session Tracking
# ────────────────────────────────────────────────────────────────

class RadAcct(models.Model):
    """
    RADIUS accounting records - tracks user sessions.
    Maps to FreeRADIUS 'radacct' table.
    
    This is populated by RADIUS accounting packets from the NAS.
    """
    ACCT_STATUS_TYPES = [
        ('Start', 'Session Start'),
        ('Stop', 'Session Stop'),
        ('Interim-Update', 'Interim Update'),
        ('Accounting-On', 'NAS Reboot Start'),
        ('Accounting-Off', 'NAS Reboot Stop'),
    ]
    
    radacctid = models.BigAutoField(primary_key=True)
    acctsessionid = models.CharField(max_length=64, db_index=True)
    acctuniqueid = models.CharField(max_length=32, unique=True)
    username = models.CharField(max_length=64, db_index=True)
    
    # NAS Information
    nasipaddress = models.GenericIPAddressField(db_index=True)
    nasportid = models.CharField(max_length=32, blank=True, null=True)
    nasporttype = models.CharField(max_length=32, blank=True, null=True)
    
    # Session Timing
    acctstarttime = models.DateTimeField(null=True, blank=True, db_index=True)
    acctupdatetime = models.DateTimeField(null=True, blank=True)
    acctstoptime = models.DateTimeField(null=True, blank=True)
    acctinterval = models.IntegerField(null=True, blank=True)
    acctsessiontime = models.BigIntegerField(null=True, blank=True)  # Session duration in seconds
    
    # Authentication
    acctauthentic = models.CharField(max_length=32, blank=True, null=True)
    connectinfo_start = models.CharField(max_length=128, blank=True, null=True)
    connectinfo_stop = models.CharField(max_length=128, blank=True, null=True)
    
    # Traffic Statistics
    acctinputoctets = models.BigIntegerField(null=True, blank=True)  # Bytes received
    acctoutputoctets = models.BigIntegerField(null=True, blank=True)  # Bytes sent
    
    # Client Information
    calledstationid = models.CharField(max_length=64, blank=True, null=True)  # Router MAC/ID
    callingstationid = models.CharField(max_length=64, blank=True, null=True)  # Client MAC
    acctterminatecause = models.CharField(max_length=32, blank=True, null=True)
    servicetype = models.CharField(max_length=32, blank=True, null=True)
    framedprotocol = models.CharField(max_length=32, blank=True, null=True)
    framedipaddress = models.GenericIPAddressField(null=True, blank=True)
    framedipv6address = models.CharField(max_length=64, blank=True, null=True)
    framedipv6prefix = models.CharField(max_length=64, blank=True, null=True)
    framedinterfaceid = models.CharField(max_length=64, blank=True, null=True)
    delegatedipv6prefix = models.CharField(max_length=64, blank=True, null=True)
    
    # Link to customer/router
    customer = models.ForeignKey(
        'customers.Customer',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='radius_sessions'
    )
    router = models.ForeignKey(
        'network.Router',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='radius_sessions'
    )
    
    class Meta:
        db_table = 'radacct'
        verbose_name = 'RADIUS Accounting'
        verbose_name_plural = 'RADIUS Accounting Records'
        indexes = [
            models.Index(fields=['username', 'acctstarttime']),
            models.Index(fields=['nasipaddress', 'acctstarttime']),
            models.Index(fields=['acctstoptime']),
        ]
    
    def __str__(self):
        return f"{self.username} @ {self.nasipaddress} ({self.acctsessionid})"
    
    @property
    def is_active(self):
        """Check if session is still active"""
        return self.acctstoptime is None
    
    @property
    def total_bytes(self):
        """Total traffic (upload + download)"""
        return (self.acctinputoctets or 0) + (self.acctoutputoctets or 0)
    
    @property
    def duration_formatted(self):
        """Session duration in human readable format"""
        if not self.acctsessiontime:
            return "N/A"
        
        seconds = self.acctsessiontime
        hours, remainder = divmod(seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        
        if hours > 0:
            return f"{hours}h {minutes}m {seconds}s"
        elif minutes > 0:
            return f"{minutes}m {seconds}s"
        else:
            return f"{seconds}s"


# ────────────────────────────────────────────────────────────────
# NAS (Network Access Server) - Router Registration
# ────────────────────────────────────────────────────────────────

class Nas(models.Model):
    """
    NAS (Network Access Server) registration.
    Maps to FreeRADIUS 'nas' table.
    
    Each router that sends RADIUS requests must be registered here.
    """
    NAS_TYPES = [
        ('mikrotik', 'MikroTik'),
        ('cisco', 'Cisco'),
        ('ubiquiti', 'Ubiquiti'),
        ('other', 'Other'),
    ]
    
    id = models.AutoField(primary_key=True)
    nasname = models.CharField(max_length=128, unique=True, help_text="IP address or hostname")
    shortname = models.CharField(max_length=32, blank=True, null=True)
    type = models.CharField(max_length=30, choices=NAS_TYPES, default='other')
    ports = models.IntegerField(null=True, blank=True)
    secret = models.CharField(max_length=60, help_text="RADIUS shared secret")
    server = models.CharField(max_length=64, blank=True, null=True)
    community = models.CharField(max_length=50, blank=True, null=True)
    description = models.CharField(max_length=200, blank=True, null=True)
    
    # Link to router
    router = models.OneToOneField(
        'network.Router',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='nas_entry'
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        db_table = 'nas'
        verbose_name = 'NAS'
        verbose_name_plural = 'NAS Entries'
    
    def __str__(self):
        return f"{self.shortname or self.nasname} ({self.type})"


# ────────────────────────────────────────────────────────────────
# RADIUS POST-AUTH LOG
# ────────────────────────────────────────────────────────────────

class RadPostAuth(models.Model):
    """
    RADIUS post-authentication log.
    Maps to FreeRADIUS 'radpostauth' table.
    
    Logs all authentication attempts (success and failure).
    """
    id = models.BigAutoField(primary_key=True)
    username = models.CharField(max_length=64, db_index=True)
    password = models.CharField(max_length=64, blank=True, null=True)  # Usually empty for security
    reply = models.CharField(max_length=32)  # Access-Accept or Access-Reject
    authdate = models.DateTimeField(default=timezone.now, db_index=True)
    
    # Additional context
    nasipaddress = models.GenericIPAddressField(null=True, blank=True)
    callingstationid = models.CharField(max_length=64, blank=True, null=True)  # Client MAC
    
    class Meta:
        db_table = 'radpostauth'
        verbose_name = 'RADIUS Post-Auth Log'
        verbose_name_plural = 'RADIUS Post-Auth Logs'
        indexes = [
            models.Index(fields=['username', 'authdate']),
            models.Index(fields=['reply', 'authdate']),
        ]
    
    def __str__(self):
        return f"{self.username}: {self.reply} at {self.authdate}"
    
    @property
    def is_success(self):
        return self.reply == 'Access-Accept'


# ────────────────────────────────────────────────────────────────
# BANDWIDTH PROFILES (Netily Extension)
# ────────────────────────────────────────────────────────────────

class RadiusBandwidthProfile(models.Model):
    """
    Bandwidth profiles for RADIUS users.
    This is a Netily extension to manage bandwidth policies.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)
    description = models.TextField(blank=True)
    
    # Bandwidth limits (in kbps)
    download_speed = models.IntegerField(help_text="Download speed in kbps")
    upload_speed = models.IntegerField(help_text="Upload speed in kbps")
    
    # Burst settings (optional)
    burst_download = models.IntegerField(null=True, blank=True, help_text="Burst download in kbps")
    burst_upload = models.IntegerField(null=True, blank=True, help_text="Burst upload in kbps")
    burst_threshold = models.IntegerField(default=0, help_text="Burst threshold in kbps")
    burst_time = models.IntegerField(default=0, help_text="Burst time in seconds")
    
    # Priority (1-8, where 1 is highest)
    priority = models.IntegerField(default=8, help_text="Queue priority (1=highest, 8=lowest)")
    
    # Data limits (optional)
    daily_limit_mb = models.BigIntegerField(null=True, blank=True, help_text="Daily data limit in MB")
    monthly_limit_mb = models.BigIntegerField(null=True, blank=True, help_text="Monthly data limit in MB")
    
    # Session limits
    session_timeout = models.IntegerField(null=True, blank=True, help_text="Session timeout in seconds")
    idle_timeout = models.IntegerField(null=True, blank=True, help_text="Idle timeout in seconds")
    simultaneous_use = models.IntegerField(default=1, help_text="Max concurrent sessions")
    
    # Status
    is_active = models.BooleanField(default=True)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = 'Bandwidth Profile'
        verbose_name_plural = 'Bandwidth Profiles'
        ordering = ['name']
    
    def __str__(self):
        return f"{self.name} ({self.download_speed}k/{self.upload_speed}k)"
    
    @property
    def mikrotik_rate_limit(self):
        """
        Generate MikroTik rate-limit string.
        Format: rx-rate[/tx-rate] [rx-burst-rate[/tx-burst-rate] [rx-burst-threshold[/tx-burst-threshold] [rx-burst-time[/tx-burst-time] [priority]]]]
        """
        # Basic rate
        rate = f"{self.upload_speed}k/{self.download_speed}k"
        
        if self.burst_download and self.burst_upload:
            burst_rate = f"{self.burst_upload}k/{self.burst_download}k"
            threshold = f"{self.burst_threshold}k/{self.burst_threshold}k"
            burst_time = f"{self.burst_time}/{self.burst_time}"
            rate = f"{rate} {burst_rate} {threshold} {burst_time} {self.priority}"
        
        return rate
    
    def get_radius_attributes(self):
        """
        Generate RADIUS attributes for this profile.
        Returns dict of attribute -> value pairs.
        """
        attrs = {
            'Mikrotik-Rate-Limit': self.mikrotik_rate_limit,
        }
        
        if self.session_timeout:
            attrs['Session-Timeout'] = str(self.session_timeout)
        
        if self.idle_timeout:
            attrs['Idle-Timeout'] = str(self.idle_timeout)
        
        if self.simultaneous_use:
            attrs['Simultaneous-Use'] = str(self.simultaneous_use)
        
        return attrs


# ────────────────────────────────────────────────────────────────
# MULTI-TENANT RADIUS CONFIGURATION
# ────────────────────────────────────────────────────────────────

class RadiusTenantConfig(models.Model):
    """
    Stores RADIUS configuration for each tenant.
    
    This model is stored in the PUBLIC schema and maps tenant schemas
    to their RADIUS configuration, including:
    - RADIUS server ports (if isolated mode)
    - RADIUS secret
    - Configuration status
    """
    schema_name = models.CharField(
        max_length=100,
        unique=True,
        db_index=True,
        help_text="Tenant schema name (e.g., 'tenant_myisp')"
    )
    tenant_name = models.CharField(
        max_length=255,
        help_text="Human-readable tenant name"
    )
    
    # RADIUS Configuration
    radius_secret = models.CharField(
        max_length=128,
        blank=True,
        help_text="RADIUS shared secret for this tenant"
    )
    radius_port_auth = models.IntegerField(
        default=1812,
        help_text="Authentication port (for isolated mode)"
    )
    radius_port_acct = models.IntegerField(
        default=1813,
        help_text="Accounting port (for isolated mode)"
    )
    
    # Deployment Mode
    DEPLOYMENT_MODES = [
        ('SHARED', 'Shared RADIUS (single instance)'),
        ('ISOLATED', 'Isolated RADIUS (per-tenant container)'),
    ]
    deployment_mode = models.CharField(
        max_length=20,
        choices=DEPLOYMENT_MODES,
        default='SHARED',
        help_text="RADIUS deployment mode for this tenant"
    )
    
    # Container Info (for isolated mode)
    container_name = models.CharField(
        max_length=100,
        blank=True,
        help_text="Docker container name (isolated mode)"
    )
    container_status = models.CharField(
        max_length=50,
        blank=True,
        default='not_started',
        help_text="Container status"
    )
    
    # Status
    is_active = models.BooleanField(default=True)
    config_generated = models.BooleanField(
        default=False,
        help_text="Whether RADIUS config files have been generated"
    )
    last_config_update = models.DateTimeField(null=True, blank=True)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = 'RADIUS Tenant Configuration'
        verbose_name_plural = 'RADIUS Tenant Configurations'
        # This model should be in public schema
        # managed = True
    
    def __str__(self):
        return f"RADIUS Config: {self.tenant_name} ({self.schema_name})"
    
    def generate_secret(self):
        """Generate a secure RADIUS secret."""
        import secrets
        self.radius_secret = secrets.token_urlsafe(32)
        return self.radius_secret
    
    def save(self, *args, **kwargs):
        # Auto-generate secret if not set
        if not self.radius_secret:
            self.generate_secret()
        
        # Auto-set container name for isolated mode
        if self.deployment_mode == 'ISOLATED' and not self.container_name:
            self.container_name = f"netily_radius_{self.schema_name.replace('tenant_', '')}"
        
        super().save(*args, **kwargs)


class CustomerRadiusCredentials(models.Model):
    """
    Stores RADIUS credentials for customers.
    
    This links a customer to their RADIUS username/password,
    allowing automatic synchronization between Django and FreeRADIUS.
    """
    customer = models.OneToOneField(
        'customers.Customer',
        on_delete=models.CASCADE,
        related_name='radius_credentials',
        help_text="Customer this RADIUS account belongs to"
    )
    
    # RADIUS Credentials
    username = models.CharField(
        max_length=64,
        unique=True,
        db_index=True,
        help_text="RADIUS username (used for PPPoE/Hotspot login)"
    )
    password = models.CharField(
        max_length=253,
        help_text="RADIUS password (stored plaintext for FreeRADIUS)"
    )
    
    # Profile/Plan Link
    bandwidth_profile = models.ForeignKey(
        RadiusBandwidthProfile,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='customer_credentials',
        help_text="Bandwidth profile for this customer"
    )
    
    # Connection Type
    CONNECTION_TYPES = [
        ('PPPOE', 'PPPoE'),
        ('HOTSPOT', 'Hotspot'),
        ('BOTH', 'PPPoE + Hotspot'),
    ]
    connection_type = models.CharField(
        max_length=20,
        choices=CONNECTION_TYPES,
        default='PPPOE',
        help_text="Type of RADIUS authentication"
    )
    
    # Status
    is_enabled = models.BooleanField(
        default=True,
        help_text="Whether this RADIUS account is active"
    )
    disabled_reason = models.CharField(
        max_length=255,
        blank=True,
        help_text="Reason for disabling (if disabled)"
    )
    
    # Optional Settings
    static_ip = models.GenericIPAddressField(
        null=True,
        blank=True,
        help_text="Static IP to assign (optional)"
    )
    ip_pool = models.CharField(
        max_length=64,
        blank=True,
        help_text="IP pool name for dynamic assignment"
    )
    simultaneous_use = models.IntegerField(
        default=1,
        help_text="Max concurrent sessions allowed"
    )
    
    # Expiration
    expiration_date = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Account expiration date"
    )
    
    # Sync Status
    synced_to_radius = models.BooleanField(
        default=False,
        help_text="Whether synced to RADIUS tables"
    )
    last_sync = models.DateTimeField(
        null=True,
        blank=True,
        help_text="Last sync timestamp"
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = 'Customer RADIUS Credentials'
        verbose_name_plural = 'Customer RADIUS Credentials'
        ordering = ['-created_at']
    
    def __str__(self):
        return f"RADIUS: {self.username} ({self.customer})"
    
    def sync_to_radius(self):
        """
        Sync this customer's credentials to RADIUS tables.
        Called automatically via signals.
        """
        from .services.radius_sync_service import RadiusSyncService
        
        service = RadiusSyncService()
        
        # Build attributes
        check_attrs = {}
        reply_attrs = {}
        
        if self.simultaneous_use > 1:
            check_attrs['Simultaneous-Use'] = str(self.simultaneous_use)
        
        if self.expiration_date:
            check_attrs['Expiration'] = self.expiration_date.strftime('%b %d %Y %H:%M:%S')
        
        if self.static_ip:
            reply_attrs['Framed-IP-Address'] = str(self.static_ip)
        
        if self.ip_pool:
            reply_attrs['Framed-Pool'] = self.ip_pool
        
        # Create or update RADIUS user
        if self.is_enabled:
            service.create_radius_user(
                username=self.username,
                password=self.password,
                customer=self.customer,
                profile=self.bandwidth_profile,
                attributes=check_attrs,
                reply_attributes=reply_attrs,
            )
        else:
            service.disable_radius_user(
                username=self.username,
                reason=self.disabled_reason or "Account disabled"
            )
        
        # Update sync status
        self.synced_to_radius = True
        self.last_sync = timezone.now()
        self.save(update_fields=['synced_to_radius', 'last_sync'])
        
        return True
    
    def delete_from_radius(self):
        """Delete this user from RADIUS tables."""
        from .services.radius_sync_service import RadiusSyncService
        
        service = RadiusSyncService()
        service.delete_radius_user(self.username)
        
        self.synced_to_radius = False
        self.save(update_fields=['synced_to_radius'])

