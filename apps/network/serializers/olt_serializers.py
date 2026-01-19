# apps/network/serializers/olt_serializers.py
from rest_framework import serializers
from apps.network.models.olt_models import (
    OLTDevice, OLTPort, PONPort, ONUDevice, OLTConfig
)
from apps.core.models import Company


class OLTDeviceSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source='company.name', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    vendor_display = serializers.CharField(source='get_vendor_display', read_only=True)
    
    class Meta:
        model = OLTDevice
        fields = [
            'id', 'company', 'company_name', 'name', 'hostname', 'ip_address',
            'vendor', 'vendor_display', 'model', 'serial_number',
            'firmware_version', 'location', 'community_string',
            'ssh_username', 'ssh_password', 'telnet_port', 'api_port',
            'status', 'status_display', 'last_sync', 'notes',
            'created_at', 'updated_at'
        ]
        extra_kwargs = {
            'ssh_password': {'write_only': True},
            'community_string': {'write_only': True},
        }


class OLTPortSerializer(serializers.ModelSerializer):
    olt_name = serializers.CharField(source='olt.name', read_only=True)
    port_type_display = serializers.CharField(source='get_port_type_display', read_only=True)
    
    class Meta:
        model = OLTPort
        fields = [
            'id', 'olt', 'olt_name', 'port_number', 'port_type',
            'port_type_display', 'description', 'admin_state',
            'operational_state', 'speed', 'mtu', 'last_change',
            'created_at', 'updated_at'
        ]


class PONPortSerializer(serializers.ModelSerializer):
    olt_port_name = serializers.CharField(source='olt_port.port_number', read_only=True)
    olt_name = serializers.CharField(source='olt_port.olt.name', read_only=True)
    pon_type_display = serializers.CharField(source='get_pon_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    registered_onus_count = serializers.IntegerField(read_only=True)
    
    class Meta:
        model = PONPort
        fields = [
            'id', 'olt_port', 'olt_port_name', 'olt_name', 'pon_index',
            'pon_type', 'pon_type_display', 'splitter_ratio',
            'total_onus', 'registered_onus', 'registered_onus_count',
            'rx_power', 'tx_power', 'distance', 'status', 'status_display',
            'created_at', 'updated_at'
        ]


class ONUDeviceSerializer(serializers.ModelSerializer):
    pon_port_name = serializers.CharField(source='pon_port.pon_index', read_only=True)
    olt_name = serializers.CharField(source='pon_port.olt_port.olt.name', read_only=True)
    customer_name = serializers.CharField(source='service_connection.customer.full_name', read_only=True)
    service_id = serializers.CharField(source='service_connection.service_id', read_only=True)
    onu_type_display = serializers.CharField(source='get_onu_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    
    class Meta:
        model = ONUDevice
        fields = [
            'id', 'pon_port', 'pon_port_name', 'olt_name',
            'service_connection', 'customer_name', 'service_id',
            'serial_number', 'mac_address', 'onu_type', 'onu_type_display',
            'onu_index', 'rx_power', 'tx_power', 'distance',
            'status', 'status_display', 'last_seen', 'registration_date',
            'config_version', 'created_at', 'updated_at'
        ]


class OLTConfigSerializer(serializers.ModelSerializer):
    olt_name = serializers.CharField(source='olt.name', read_only=True)
    config_type_display = serializers.CharField(source='get_config_type_display', read_only=True)
    applied_by_name = serializers.CharField(source='applied_by.get_full_name', read_only=True)
    
    class Meta:
        model = OLTConfig
        fields = [
            'id', 'olt', 'olt_name', 'config_type', 'config_type_display',
            'version', 'config_data', 'checksum', 'applied_by',
            'applied_by_name', 'applied_date', 'is_active',
            'created_at', 'updated_at'
        ]
        extra_kwargs = {
            'config_data': {'write_only': True},  # Large field, usually not needed in lists
        }
