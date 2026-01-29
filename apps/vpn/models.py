"""
VPN Models - Certificate Authority, Certificates, Connections

This module handles:
1. Certificate Authority (CA) management for OpenVPN
2. Client/Server certificate generation and storage
3. VPN connection tracking and status
4. Connection logging and analytics
"""

from django.db import models
from django.utils import timezone
from django.core.validators import MinValueValidator, MaxValueValidator
import uuid
import secrets


def generate_serial_number():
    """Generate a unique serial number for certificates"""
    return secrets.token_hex(16).upper()


class CertificateAuthority(models.Model):
    """
    Certificate Authority for issuing VPN certificates.
    Each tenant can have their own CA or use a shared one.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, unique=True)
    common_name = models.CharField(max_length=100, help_text="CN for the CA certificate")
    organization = models.CharField(max_length=100, blank=True, default="Netily ISP")
    country = models.CharField(max_length=2, default="KE", help_text="2-letter country code")
    
    # CA Certificate and Key (encrypted in production)
    ca_certificate = models.TextField(help_text="PEM-encoded CA certificate")
    ca_private_key = models.TextField(help_text="PEM-encoded CA private key (encrypted)")
    
    # Diffie-Hellman parameters for key exchange
    dh_parameters = models.TextField(blank=True, help_text="DH parameters for OpenVPN")
    
    # TLS Auth key for additional security
    tls_auth_key = models.TextField(blank=True, help_text="OpenVPN TLS Auth key")
    
    # Validity
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    validity_days = models.IntegerField(default=3650, help_text="CA validity in days (default 10 years)")
    
    # Status
    is_active = models.BooleanField(default=True)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = "Certificate Authority"
        verbose_name_plural = "Certificate Authorities"
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.name} ({self.common_name})"
    
    @property
    def is_valid(self):
        """Check if CA certificate is still valid"""
        now = timezone.now()
        if self.valid_from and self.valid_until:
            return self.valid_from <= now <= self.valid_until
        return self.is_active
    
    @property
    def days_until_expiry(self):
        """Days until CA expires"""
        if self.valid_until:
            delta = self.valid_until - timezone.now()
            return max(0, delta.days)
        return None


class VPNCertificate(models.Model):
    """
    VPN Certificates for routers (clients) and servers.
    Issued by a Certificate Authority.
    """
    CERTIFICATE_TYPES = [
        ('client', 'Client Certificate'),
        ('server', 'Server Certificate'),
    ]
    
    STATUS_CHOICES = [
        ('pending', 'Pending Generation'),
        ('active', 'Active'),
        ('revoked', 'Revoked'),
        ('expired', 'Expired'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Certificate Authority that issued this cert
    ca = models.ForeignKey(
        CertificateAuthority,
        on_delete=models.CASCADE,
        related_name='certificates'
    )
    
    # For client certificates, link to router
    router = models.ForeignKey(
        'network.Router',
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name='vpn_certificates'
    )
    
    # Certificate details
    common_name = models.CharField(max_length=100, help_text="CN for the certificate")
    certificate_type = models.CharField(max_length=10, choices=CERTIFICATE_TYPES, default='client')
    serial_number = models.CharField(max_length=64, unique=True, default=generate_serial_number)
    
    # Certificate data (PEM encoded)
    certificate = models.TextField(blank=True, help_text="PEM-encoded certificate")
    private_key = models.TextField(blank=True, help_text="PEM-encoded private key (encrypted)")
    certificate_request = models.TextField(blank=True, help_text="CSR if using external signing")
    
    # Validity
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    validity_days = models.IntegerField(default=365, help_text="Certificate validity in days")
    
    # Status
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending')
    revoked_at = models.DateTimeField(null=True, blank=True)
    revocation_reason = models.CharField(max_length=255, blank=True)
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        verbose_name = "VPN Certificate"
        verbose_name_plural = "VPN Certificates"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['router', 'status']),
            models.Index(fields=['certificate_type', 'status']),
            models.Index(fields=['serial_number']),
        ]
    
    def __str__(self):
        return f"{self.common_name} ({self.get_certificate_type_display()})"
    
    @property
    def is_valid(self):
        """Check if certificate is still valid"""
        if self.status != 'active':
            return False
        now = timezone.now()
        if self.valid_from and self.valid_until:
            return self.valid_from <= now <= self.valid_until
        return True
    
    @property
    def days_until_expiry(self):
        """Days until certificate expires"""
        if self.valid_until:
            delta = self.valid_until - timezone.now()
            return max(0, delta.days)
        return None
    
    def revoke(self, reason=""):
        """Revoke this certificate"""
        self.status = 'revoked'
        self.revoked_at = timezone.now()
        self.revocation_reason = reason
        self.save(update_fields=['status', 'revoked_at', 'revocation_reason', 'updated_at'])


class VPNServer(models.Model):
    """
    OpenVPN Server configuration.
    Tracks server instances and their status.
    """
    PROTOCOL_CHOICES = [
        ('udp', 'UDP'),
        ('tcp', 'TCP'),
    ]
    
    STATUS_CHOICES = [
        ('running', 'Running'),
        ('stopped', 'Stopped'),
        ('starting', 'Starting'),
        ('error', 'Error'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100, default="Primary VPN Server")
    
    # Server configuration
    server_address = models.CharField(max_length=255, help_text="Public hostname or IP")
    port = models.IntegerField(default=1194, validators=[MinValueValidator(1), MaxValueValidator(65535)])
    protocol = models.CharField(max_length=3, choices=PROTOCOL_CHOICES, default='udp')
    
    # VPN Network
    vpn_network = models.CharField(max_length=18, default="10.8.0.0/24", help_text="VPN subnet CIDR")
    dns_servers = models.CharField(max_length=100, default="8.8.8.8,8.8.4.4", help_text="Comma-separated DNS servers")
    
    # Certificate
    certificate = models.ForeignKey(
        VPNCertificate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='servers',
        limit_choices_to={'certificate_type': 'server'}
    )
    
    # CA
    ca = models.ForeignKey(
        CertificateAuthority,
        on_delete=models.PROTECT,
        related_name='vpn_servers'
    )
    
    # Status
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='stopped')
    max_clients = models.IntegerField(default=100)
    connected_clients = models.IntegerField(default=0)
    
    # Docker container info
    container_id = models.CharField(max_length=64, blank=True, help_text="Docker container ID")
    container_name = models.CharField(max_length=100, default="openvpn-server")
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    last_status_check = models.DateTimeField(null=True, blank=True)
    
    class Meta:
        verbose_name = "VPN Server"
        verbose_name_plural = "VPN Servers"
    
    def __str__(self):
        return f"{self.name} ({self.server_address}:{self.port})"


class VPNConnection(models.Model):
    """
    Active and historical VPN connections.
    Tracks router connections to the VPN server.
    """
    STATUS_CHOICES = [
        ('connected', 'Connected'),
        ('disconnected', 'Disconnected'),
        ('connecting', 'Connecting'),
        ('error', 'Error'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Router that connected
    router = models.ForeignKey(
        'network.Router',
        on_delete=models.CASCADE,
        related_name='vpn_connections'
    )
    
    # VPN Server
    server = models.ForeignKey(
        VPNServer,
        on_delete=models.CASCADE,
        related_name='connections'
    )
    
    # Certificate used
    certificate = models.ForeignKey(
        VPNCertificate,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='connections'
    )
    
    # Connection details
    vpn_ip = models.GenericIPAddressField(null=True, blank=True, help_text="Assigned VPN IP")
    real_ip = models.GenericIPAddressField(null=True, blank=True, help_text="Router's public IP")
    
    # Status
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='disconnected')
    
    # Timestamps
    connected_at = models.DateTimeField(null=True, blank=True)
    disconnected_at = models.DateTimeField(null=True, blank=True)
    last_activity = models.DateTimeField(null=True, blank=True)
    
    # Traffic statistics
    bytes_sent = models.BigIntegerField(default=0)
    bytes_received = models.BigIntegerField(default=0)
    
    # Session info
    session_id = models.CharField(max_length=64, blank=True)
    
    class Meta:
        verbose_name = "VPN Connection"
        verbose_name_plural = "VPN Connections"
        ordering = ['-connected_at']
        indexes = [
            models.Index(fields=['router', 'status']),
            models.Index(fields=['server', 'status']),
            models.Index(fields=['vpn_ip']),
        ]
    
    def __str__(self):
        return f"{self.router.name} -> {self.vpn_ip or 'Not assigned'} ({self.status})"
    
    @property
    def duration(self):
        """Connection duration in seconds"""
        if self.connected_at:
            end_time = self.disconnected_at or timezone.now()
            return (end_time - self.connected_at).total_seconds()
        return 0
    
    @property
    def is_active(self):
        return self.status == 'connected'


class VPNConnectionLog(models.Model):
    """
    Log of VPN connection events for auditing and analytics.
    """
    EVENT_TYPES = [
        ('connect', 'Connected'),
        ('disconnect', 'Disconnected'),
        ('auth_success', 'Authentication Success'),
        ('auth_failure', 'Authentication Failure'),
        ('cert_verify', 'Certificate Verified'),
        ('cert_error', 'Certificate Error'),
        ('ip_assigned', 'IP Assigned'),
        ('error', 'Error'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    # Router
    router = models.ForeignKey(
        'network.Router',
        on_delete=models.CASCADE,
        related_name='vpn_logs'
    )
    
    # Connection (if applicable)
    connection = models.ForeignKey(
        VPNConnection,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='logs'
    )
    
    # Event details
    event_type = models.CharField(max_length=20, choices=EVENT_TYPES)
    message = models.TextField(blank=True)
    
    # Connection info at time of event
    vpn_ip = models.GenericIPAddressField(null=True, blank=True)
    real_ip = models.GenericIPAddressField(null=True, blank=True)
    
    # Additional data
    details = models.JSONField(default=dict, blank=True)
    
    # Timestamp
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        verbose_name = "VPN Connection Log"
        verbose_name_plural = "VPN Connection Logs"
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['router', 'event_type']),
            models.Index(fields=['created_at']),
        ]
    
    def __str__(self):
        return f"{self.router.name} - {self.get_event_type_display()} at {self.created_at}"
