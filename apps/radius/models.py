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
