# apps/network/serializers/mikrotik_serializers.py
from rest_framework import serializers
from apps.network.models.mikrotik_models import (
    MikrotikDevice, MikrotikInterface, HotspotUser,
    PPPoEUser, MikrotikQueue
)


class MikrotikDeviceSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source='company.name', read_only=True)
    device_type_display = serializers.CharField(source='get_device_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    
    class Meta:
        model = MikrotikDevice
        fields = [
            'id', 'company', 'company_name', 'name', 'hostname',
            'ip_address', 'api_port', 'ssh_port', 'winbox_port',
            'device_type', 'device_type_display', 'model',
            'serial_number', 'firmware_version', 'location',
            'api_username', 'api_password', 'ssh_username',
            'ssh_password', 'status', 'status_display',
            'last_sync', 'cpu_load', 'memory_usage', 'disk_usage',
            'uptime', 'notes', 'created_at', 'updated_at'
        ]
        extra_kwargs = {
            'api_password': {'write_only': True},
            'ssh_password': {'write_only': True},
        }


class MikrotikInterfaceSerializer(serializers.ModelSerializer):
    mikrotik_name = serializers.CharField(source='mikrotik.name', read_only=True)
    interface_type_display = serializers.CharField(source='get_interface_type_display', read_only=True)
    
    class Meta:
        model = MikrotikInterface
        fields = [
            'id', 'mikrotik', 'mikrotik_name', 'interface_name',
            'interface_type', 'interface_type_display', 'mac_address',
            'mtu', 'rx_bytes', 'tx_bytes', 'rx_packets', 'tx_packets',
            'rx_errors', 'tx_errors', 'admin_state', 'operational_state',
            'last_change', 'created_at', 'updated_at'
        ]


class HotspotUserSerializer(serializers.ModelSerializer):
    mikrotik_name = serializers.CharField(source='mikrotik.name', read_only=True)
    customer_name = serializers.CharField(source='service_connection.customer.full_name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    
    class Meta:
        model = HotspotUser
        fields = [
            'id', 'mikrotik', 'mikrotik_name', 'service_connection',
            'customer_name', 'username', 'password', 'mac_address',
            'ip_address', 'bytes_in', 'bytes_out', 'packets_in',
            'packets_out', 'session_time', 'idle_time', 'status',
            'status_display', 'profile', 'limit_uptime',
            'limit_bytes_in', 'limit_bytes_out', 'last_login',
            'last_logout', 'created_at', 'updated_at'
        ]
        extra_kwargs = {
            'password': {'write_only': True},
        }


class PPPoEUserSerializer(serializers.ModelSerializer):
    mikrotik_name = serializers.CharField(source='mikrotik.name', read_only=True)
    customer_name = serializers.CharField(source='service_connection.customer.full_name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    
    class Meta:
        model = PPPoEUser
        fields = [
            'id', 'mikrotik', 'mikrotik_name', 'service_connection',
            'customer_name', 'username', 'password', 'service',
            'caller_id', 'local_address', 'remote_address',
            'bytes_in', 'bytes_out', 'packets_in', 'packets_out',
            'session_time', 'idle_time', 'status', 'status_display',
            'profile', 'last_connection', 'last_disconnection',
            'created_at', 'updated_at'
        ]
        extra_kwargs = {
            'password': {'write_only': True},
        }


class MikrotikQueueSerializer(serializers.ModelSerializer):
    mikrotik_name = serializers.CharField(source='mikrotik.name', read_only=True)
    queue_type_display = serializers.CharField(source='get_queue_type_display', read_only=True)
    hotspot_username = serializers.CharField(source='hotspot_user.username', read_only=True)
    pppoe_username = serializers.CharField(source='pppoe_user.username', read_only=True)
    
    class Meta:
        model = MikrotikQueue
        fields = [
            'id', 'mikrotik', 'mikrotik_name', 'queue_name',
            'queue_type', 'queue_type_display', 'target',
            'max_limit', 'burst_limit', 'burst_threshold',
            'burst_time', 'priority', 'packet_mark', 'disabled',
            'comment', 'hotspot_user', 'hotspot_username',
            'pppoe_user', 'pppoe_username', 'created_at', 'updated_at'
        ]