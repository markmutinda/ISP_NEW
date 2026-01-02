from rest_framework import serializers
from django.utils import timezone

from .models import (
    BandwidthProfile, TrafficRule, DataUsage, 
    BandwidthAlert, SpeedTestResult
)


class BandwidthProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = BandwidthProfile
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']
    
    def validate(self, data):
        """Validate bandwidth profile data"""
        # Ensure upload speed is not greater than download speed (usually)
        if data.get('upload_speed', 0) > data.get('download_speed', 0):
            raise serializers.ValidationError(
                "Upload speed cannot be greater than download speed"
            )
        
        # Ensure burst duration is provided if burst limit is set
        if data.get('burst_limit', 0) > 0 and data.get('burst_duration', 0) == 0:
            raise serializers.ValidationError(
                "Burst duration must be specified when burst limit is set"
            )
        
        return data


class TrafficRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = TrafficRule
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at', 'applied_at', 'is_applied']
    
    def validate(self, data):
        """Validate traffic rule data"""
        # Validate schedule times if schedule is enabled
        if data.get('schedule_enabled', False):
            if not data.get('schedule_start') or not data.get('schedule_end'):
                raise serializers.ValidationError(
                    "Schedule start and end times are required when schedule is enabled"
                )
            
            if data['schedule_start'] >= data['schedule_end']:
                raise serializers.ValidationError(
                    "Schedule start time must be before end time"
                )
        
        # Validate port ranges
        source_port = data.get('source_port')
        destination_port = data.get('destination_port')
        
        if source_port and (source_port < 1 or source_port > 65535):
            raise serializers.ValidationError(
                "Source port must be between 1 and 65535"
            )
        
        if destination_port and (destination_port < 1 or destination_port > 65535):
            raise serializers.ValidationError(
                "Destination port must be between 1 and 65535"
            )
        
        return data


class DataUsageSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.user.get_full_name', read_only=True)
    customer_code = serializers.CharField(source='customer.customer_code', read_only=True)
    download_gb = serializers.SerializerMethodField()
    upload_gb = serializers.SerializerMethodField()
    total_gb = serializers.SerializerMethodField()
    usage_percentage = serializers.SerializerMethodField()
    
    class Meta:
        model = DataUsage
        fields = '__all__'
        read_only_fields = ['created_at', 'updated_at']
    
    def get_download_gb(self, obj):
        return obj.download_gb
    
    def get_upload_gb(self, obj):
        return obj.upload_gb
    
    def get_total_gb(self, obj):
        return obj.total_gb
    
    def get_usage_percentage(self, obj):
        return obj.usage_percentage


class BandwidthAlertSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.user.get_full_name', read_only=True)
    customer_code = serializers.CharField(source='customer.customer_code', read_only=True)
    acknowledged_by_name = serializers.SerializerMethodField()
    duration = serializers.SerializerMethodField()
    
    class Meta:
        model = BandwidthAlert
        fields = '__all__'
        read_only_fields = [
            'created_at', 'updated_at', 'triggered_at', 
            'acknowledged_at', 'resolved_at'
        ]
    
    def get_acknowledged_by_name(self, obj):
        if obj.acknowledged_by:
            return obj.acknowledged_by.get_full_name()
        return None
    
    def get_duration(self, obj):
        if obj.triggered_at:
            if obj.resolved_at:
                duration = obj.resolved_at - obj.triggered_at
            else:
                duration = timezone.now() - obj.triggered_at
            
            # Format duration
            total_seconds = int(duration.total_seconds())
            hours, remainder = divmod(total_seconds, 3600)
            minutes, seconds = divmod(remainder, 60)
            
            if hours > 0:
                return f"{hours}h {minutes}m"
            elif minutes > 0:
                return f"{minutes}m {seconds}s"
            else:
                return f"{seconds}s"
        return None


class SpeedTestResultSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source='customer.user.get_full_name', read_only=True)
    customer_code = serializers.CharField(source='customer.customer_code', read_only=True)
    
    class Meta:
        model = SpeedTestResult
        fields = '__all__'
        read_only_fields = ['created_at']


class TrafficAnalysisSerializer(serializers.Serializer):
    """Serializer for traffic analysis results"""
    customer_id = serializers.IntegerField()
    customer_name = serializers.CharField()
    analysis_period = serializers.DictField()
    statistics = serializers.DictField()
    hourly_patterns = serializers.DictField()
    anomalies_detected = serializers.IntegerField()
    anomalies = serializers.ListField()
    traffic_composition = serializers.DictField()
    recommendations = serializers.ListField()