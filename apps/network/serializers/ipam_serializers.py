# apps/network/serializers/ipam_serializers.py
from rest_framework import serializers
from apps.network.models.ipam_models import (
    Subnet, VLAN, IPPool, IPAddress, DHCPRange,
    SUBNET_PREFIX_CHOICES, CIDR_CHOICES, BLOCKED_PREFIXES
)


class SubnetSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source='company.name', read_only=True)
    version_display = serializers.CharField(source='get_version_display', read_only=True)
    network_cidr = serializers.SerializerMethodField()
    
    class Meta:
        model = Subnet
        fields = [
            'id', 'company', 'company_name', 'name',
            'network_address', 'subnet_mask', 'cidr',
            'network_cidr', 'version', 'version_display',
            'description', 'vlan_id', 'location', 'is_public',
            'total_ips', 'used_ips', 'available_ips',
            'utilization_percentage', 'created_at', 'updated_at'
        ]
    
    def get_network_cidr(self, obj):
        return f"{obj.network_address}/{obj.cidr}"


class VLANSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source='company.name', read_only=True)
    subnet_cidr = serializers.CharField(source='subnet.network_cidr', read_only=True)
    
    class Meta:
        model = VLAN
        fields = [
            'id', 'company', 'company_name', 'vlan_id', 'name',
            'description', 'subnet', 'subnet_cidr',
            'created_at', 'updated_at'
        ]


class IPPoolSerializer(serializers.ModelSerializer):
    """Serializer for IP Pools — Cloud-Led IPAM with subnet builder fields."""
    subnet_cidr = serializers.SerializerMethodField()
    pool_type_display = serializers.CharField(source='get_pool_type_display', read_only=True)
    ip_range = serializers.SerializerMethodField()
    cidr_notation = serializers.CharField(read_only=True)
    router_name = serializers.CharField(source='router.name', read_only=True, default=None)
    router_ip = serializers.CharField(source='router.ip_address', read_only=True, default=None)
    router_status = serializers.CharField(source='router.status', read_only=True, default=None)
    available_ips = serializers.SerializerMethodField()
    utilization_percentage = serializers.SerializerMethodField()
    
    class Meta:
        model = IPPool
        fields = [
            'id', 'router', 'router_name', 'router_ip', 'router_status',
            'subnet', 'subnet_cidr', 'name', 'pool_type',
            'pool_type_display',
            # Cloud-Led subnet builder fields
            'subnet_prefix', 'subnet_octet', 'cidr_prefix',
            'network_address', 'broadcast_address', 'cidr_notation',
            # Computed range
            'start_ip', 'end_ip', 'ip_range',
            'gateway', 'dns_servers', 'lease_time', 'description',
            'is_active', 'total_ips', 'used_ips', 'available_ips',
            'utilization_percentage', 'created_at', 'updated_at'
        ]
    
    def get_subnet_cidr(self, obj):
        if obj.subnet:
            return f"{obj.subnet.network_address}/{obj.subnet.cidr}"
        return None

    def get_ip_range(self, obj):
        return f"{obj.start_ip} - {obj.end_ip}"
    
    def get_available_ips(self, obj):
        return max(0, obj.total_ips - obj.used_ips)
    
    def get_utilization_percentage(self, obj):
        if obj.total_ips > 0:
            return round((obj.used_ips / obj.total_ips) * 100, 1)
        return 0.0


class IPAddressSerializer(serializers.ModelSerializer):
    subnet_cidr = serializers.CharField(source='subnet.network_cidr', read_only=True, default=None)
    pool_name = serializers.CharField(source='ip_pool.name', read_only=True, default=None)
    customer_name = serializers.SerializerMethodField()
    assigned_to_name = serializers.SerializerMethodField()
    assignment_type_display = serializers.CharField(source='get_assignment_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    
    class Meta:
        model = IPAddress
        fields = [
            'id', 'subnet', 'subnet_cidr', 'ip_pool', 'pool_name',
            'ip_address', 'assignment_type', 'assignment_type_display',
            'status', 'status_display', 'mac_address', 'hostname',
            'description', 'service_connection', 'customer_name',
            'assigned_to', 'assigned_to_name',
            'lease_start', 'lease_end', 'last_seen', 'device_type',
            'manufacturer', 'created_at', 'updated_at'
        ]
    
    def get_customer_name(self, obj):
        if obj.service_connection and obj.service_connection.customer:
            return obj.service_connection.customer.full_name
        return None
    
    def get_assigned_to_name(self, obj):
        if obj.assigned_to:
            return obj.assigned_to.full_name
        return None


class DHCPRangeSerializer(serializers.ModelSerializer):
    pool_name = serializers.CharField(source='ip_pool.name', read_only=True)
    subnet_cidr = serializers.CharField(source='ip_pool.subnet.network_cidr', read_only=True)
    ip_range = serializers.SerializerMethodField()
    
    class Meta:
        model = DHCPRange
        fields = [
            'id', 'ip_pool', 'pool_name', 'subnet_cidr', 'name',
            'start_ip', 'end_ip', 'ip_range', 'router', 'dns_server',
            'domain_name', 'lease_time', 'is_active',
            'created_at', 'updated_at'
        ]
    
    def get_ip_range(self, obj):
        return f"{obj.start_ip} - {obj.end_ip}"
