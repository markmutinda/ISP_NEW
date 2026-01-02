# apps/network/serializers/tr069_serializers.py
from rest_framework import serializers
from apps.network.models.tr069_models import (
    ACSConfiguration, CPEDevice, TR069Parameter, TR069Session
)


class ACSConfigurationSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source='company.name', read_only=True)
    
    class Meta:
        model = ACSConfiguration
        fields = [
            'id', 'company', 'company_name', 'name', 'acs_url',
            'acs_username', 'acs_password', 'connection_request_url',
            'cpe_username', 'cpe_password', 'periodic_interval',
            'is_active', 'created_at', 'updated_at'
        ]
        extra_kwargs = {
            'acs_password': {'write_only': True},
            'cpe_password': {'write_only': True},
        }


class CPEDeviceSerializer(serializers.ModelSerializer):
    company_name = serializers.CharField(source='company.name', read_only=True)
    customer_name = serializers.CharField(source='service_connection.customer.full_name', read_only=True)
    customer_id = serializers.CharField(source='service_connection.customer.customer_id', read_only=True)
    manufacturer_display = serializers.CharField(source='get_manufacturer_display', read_only=True)
    connection_status_display = serializers.CharField(source='get_connection_status_display', read_only=True)
    
    class Meta:
        model = CPEDevice
        fields = [
            'id', 'company', 'company_name', 'service_connection',
            'customer_name', 'customer_id', 'acs_config',
            'manufacturer', 'manufacturer_display', 'model',
            'serial_number', 'product_class', 'hardware_version',
            'software_version', 'oui', 'cpe_id',
            'connection_status', 'connection_status_display',
            'wan_ip', 'wan_mac', 'lan_ip', 'last_connection',
            'last_boot', 'provisioned', 'configuration_file',
            'custom_parameters', 'created_at', 'updated_at'
        ]


class TR069ParameterSerializer(serializers.ModelSerializer):
    cpe_serial = serializers.CharField(source='cpe_device.serial_number', read_only=True)
    parameter_type_display = serializers.CharField(source='get_parameter_type_display', read_only=True)
    access_type_display = serializers.CharField(source='get_access_type_display', read_only=True)
    
    class Meta:
        model = TR069Parameter
        fields = [
            'id', 'cpe_device', 'cpe_serial', 'parameter_name',
            'parameter_type', 'parameter_type_display',
            'access_type', 'access_type_display',
            'current_value', 'configured_value', 'min_value',
            'max_value', 'default_value', 'notification',
            'description', 'last_updated', 'created_at', 'updated_at'
        ]


class TR069SessionSerializer(serializers.ModelSerializer):
    cpe_serial = serializers.CharField(source='cpe_device.serial_number', read_only=True)
    session_type_display = serializers.CharField(source='get_session_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    initiated_by_name = serializers.CharField(source='initiated_by.get_full_name', read_only=True)
    
    class Meta:
        model = TR069Session
        fields = [
            'id', 'cpe_device', 'cpe_serial', 'session_type',
            'session_type_display', 'session_id', 'start_time',
            'end_time', 'duration', 'status', 'status_display',
            'request_data', 'response_data', 'error_message',
            'initiated_by', 'initiated_by_name', 'created_at', 'updated_at'
        ]