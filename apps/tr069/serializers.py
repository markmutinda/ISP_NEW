from rest_framework import serializers

from . import services
from .models import CPEDevice, CPETaskLog
from .services import provisioning


class SubscriberMiniSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    plan = serializers.CharField(allow_null=True)


class CPETaskLogSerializer(serializers.ModelSerializer):
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = CPETaskLog
        fields = ['id', 'task_type', 'status', 'error_message', 'created_by_name', 'created_at', 'completed_at']

    def get_created_by_name(self, obj):
        return obj.created_by.get_full_name() if obj.created_by else None


def _subscriber_payload(device):
    sc = device.service_connection
    if not sc:
        return None
    customer = getattr(sc, 'customer', None)
    return {
        'id': sc.id,
        'name': getattr(customer, 'full_name', None) or str(customer) if customer else str(sc),
        'plan': getattr(getattr(sc, 'plan', None), 'name', None),
    }


class CPEDeviceListSerializer(serializers.ModelSerializer):
    subscriber = serializers.SerializerMethodField()
    has_connected = serializers.BooleanField(read_only=True)
    pending_commands = serializers.IntegerField(read_only=True)

    class Meta:
        model = CPEDevice
        fields = [
            'id', 'uuid', 'label', 'serial_number', 'manufacturer', 'model_name',
            'status', 'weak_signal', 'rx_power_dbm', 'last_inform_at',
            'has_connected', 'pending_commands', 'subscriber',
        ]

    def get_subscriber(self, obj):
        return _subscriber_payload(obj)


def _clean_wifi_networks(networks):
    return [
        {k: v for k, v in n.items() if k != '_paths'}
        for n in (networks or [])
    ]


class CPEDeviceDetailSerializer(CPEDeviceListSerializer):
    wifi_networks = serializers.SerializerMethodField()
    connected_hosts_count = serializers.IntegerField(read_only=True)
    wifi_network_count = serializers.IntegerField(read_only=True)

    class Meta(CPEDeviceListSerializer.Meta):
        fields = CPEDeviceListSerializer.Meta.fields + [
            'data_model', 'oui', 'product_class', 'software_version', 'hardware_version',
            'uptime_seconds', 'wan_ip', 'tx_power_dbm', 'connected_hosts_count',
            'wifi_network_count', 'wifi_networks', 'hosts', 'inform_interval',
            'last_sync_at', 'created_at',
        ]

    def get_wifi_networks(self, obj):
        return _clean_wifi_networks(obj.wifi_networks)


class CPEDeviceCreateSerializer(serializers.Serializer):
    serial_number = serializers.CharField(max_length=255)
    label = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    service_connection = serializers.IntegerField(required=False, allow_null=True)


class CPEDevicePatchSerializer(serializers.Serializer):
    label = serializers.CharField(max_length=100, required=False, allow_blank=True)
    service_connection = serializers.IntegerField(required=False, allow_null=True)


class CredentialsSerializer(serializers.Serializer):
    acs_url = serializers.CharField()
    username = serializers.CharField()
    password = serializers.CharField()
    recommended_interval_seconds = serializers.IntegerField()


class SetWifiEntrySerializer(serializers.Serializer):
    id = serializers.CharField()
    ssid = serializers.CharField(min_length=1, max_length=32, required=False)
    password = serializers.CharField(min_length=8, max_length=63, required=False)
    enabled = serializers.BooleanField(required=False)

    def validate(self, attrs):
        if not any(k in attrs for k in ('ssid', 'password', 'enabled')):
            raise serializers.ValidationError("Provide at least one field to change.")
        return attrs


class SetWifiSerializer(serializers.Serializer):
    networks = SetWifiEntrySerializer(many=True)

    def validate_networks(self, value):
        if not value:
            raise serializers.ValidationError("At least one network entry is required.")
        return value


class FactoryResetSerializer(serializers.Serializer):
    confirm = serializers.BooleanField()

    def validate_confirm(self, value):
        if not value:
            raise serializers.ValidationError("Confirmation required.")
        return value


class PppoeSubscriberSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()
    plan = serializers.CharField(allow_null=True)
    already_linked = serializers.BooleanField()