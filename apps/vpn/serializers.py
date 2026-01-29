"""
VPN Serializers
"""
from rest_framework import serializers
from .models import (
    CertificateAuthority,
    VPNCertificate,
    VPNConnection,
    VPNServer,
    VPNConnectionLog
)


class CertificateAuthoritySerializer(serializers.ModelSerializer):
    """Serializer for Certificate Authority"""
    is_valid = serializers.ReadOnlyField()
    days_until_expiry = serializers.ReadOnlyField()
    certificates_count = serializers.SerializerMethodField()
    
    class Meta:
        model = CertificateAuthority
        fields = [
            'id', 'name', 'common_name', 'organization', 'country',
            'valid_from', 'valid_until', 'validity_days', 'is_active',
            'is_valid', 'days_until_expiry', 'certificates_count',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'valid_from', 'valid_until', 'created_at', 'updated_at']
    
    def get_certificates_count(self, obj):
        return obj.certificates.count()


class CertificateAuthorityDetailSerializer(CertificateAuthoritySerializer):
    """Detailed serializer with certificate data (admin only)"""
    
    class Meta(CertificateAuthoritySerializer.Meta):
        fields = CertificateAuthoritySerializer.Meta.fields + [
            'ca_certificate', 'dh_parameters', 'tls_auth_key'
        ]


class VPNCertificateSerializer(serializers.ModelSerializer):
    """Serializer for VPN Certificates"""
    is_valid = serializers.ReadOnlyField()
    days_until_expiry = serializers.ReadOnlyField()
    ca_name = serializers.CharField(source='ca.name', read_only=True)
    router_name = serializers.CharField(source='router.name', read_only=True, allow_null=True)
    
    class Meta:
        model = VPNCertificate
        fields = [
            'id', 'ca', 'ca_name', 'router', 'router_name',
            'common_name', 'certificate_type', 'serial_number',
            'valid_from', 'valid_until', 'validity_days',
            'status', 'revoked_at', 'revocation_reason',
            'is_valid', 'days_until_expiry',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'id', 'serial_number', 'valid_from', 'valid_until',
            'revoked_at', 'created_at', 'updated_at'
        ]


class VPNCertificateDetailSerializer(VPNCertificateSerializer):
    """Detailed serializer with certificate data"""
    
    class Meta(VPNCertificateSerializer.Meta):
        fields = VPNCertificateSerializer.Meta.fields + [
            'certificate', 'certificate_request'
        ]


class VPNCertificateCreateSerializer(serializers.ModelSerializer):
    """Serializer for creating certificates"""
    
    class Meta:
        model = VPNCertificate
        fields = ['ca', 'router', 'common_name', 'certificate_type', 'validity_days']


class VPNServerSerializer(serializers.ModelSerializer):
    """Serializer for VPN Servers"""
    ca_name = serializers.CharField(source='ca.name', read_only=True)
    
    class Meta:
        model = VPNServer
        fields = [
            'id', 'name', 'server_address', 'port', 'protocol',
            'vpn_network', 'dns_servers', 'ca', 'ca_name',
            'status', 'max_clients', 'connected_clients',
            'container_id', 'container_name',
            'created_at', 'updated_at', 'last_status_check'
        ]
        read_only_fields = ['id', 'connected_clients', 'created_at', 'updated_at', 'last_status_check']


class VPNConnectionSerializer(serializers.ModelSerializer):
    """Serializer for VPN Connections"""
    router_name = serializers.CharField(source='router.name', read_only=True)
    server_name = serializers.CharField(source='server.name', read_only=True)
    duration = serializers.ReadOnlyField()
    is_active = serializers.ReadOnlyField()
    
    class Meta:
        model = VPNConnection
        fields = [
            'id', 'router', 'router_name', 'server', 'server_name',
            'certificate', 'vpn_ip', 'real_ip', 'status',
            'connected_at', 'disconnected_at', 'last_activity',
            'bytes_sent', 'bytes_received', 'session_id',
            'duration', 'is_active'
        ]
        read_only_fields = [
            'id', 'connected_at', 'disconnected_at', 'last_activity',
            'bytes_sent', 'bytes_received', 'session_id'
        ]


class VPNConnectionLogSerializer(serializers.ModelSerializer):
    """Serializer for VPN Connection Logs"""
    router_name = serializers.CharField(source='router.name', read_only=True)
    event_type_display = serializers.CharField(source='get_event_type_display', read_only=True)
    
    class Meta:
        model = VPNConnectionLog
        fields = [
            'id', 'router', 'router_name', 'connection',
            'event_type', 'event_type_display', 'message',
            'vpn_ip', 'real_ip', 'details', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']


class VPNDashboardStatsSerializer(serializers.Serializer):
    """Serializer for VPN Dashboard Statistics"""
    total_servers = serializers.IntegerField()
    active_servers = serializers.IntegerField()
    total_certificates = serializers.IntegerField()
    active_certificates = serializers.IntegerField()
    expiring_soon = serializers.IntegerField()
    revoked_certificates = serializers.IntegerField()
    total_connections = serializers.IntegerField()
    active_connections = serializers.IntegerField()
    total_bytes_sent = serializers.IntegerField()
    total_bytes_received = serializers.IntegerField()


class RouterVPNStatusSerializer(serializers.Serializer):
    """Serializer for router VPN status"""
    router_id = serializers.UUIDField()
    router_name = serializers.CharField()
    vpn_enabled = serializers.BooleanField()
    has_certificate = serializers.BooleanField()
    certificate_status = serializers.CharField(allow_null=True)
    certificate_expires = serializers.DateTimeField(allow_null=True)
    connection_status = serializers.CharField()
    vpn_ip = serializers.IPAddressField(allow_null=True)
    last_connected = serializers.DateTimeField(allow_null=True)
