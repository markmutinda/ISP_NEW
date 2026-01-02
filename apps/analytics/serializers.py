from rest_framework import serializers
from .models import ReportDefinition, DashboardWidget


class ReportDefinitionSerializer(serializers.ModelSerializer):
    created_by_name = serializers.CharField(source='created_by.get_full_name', read_only=True)
    
    class Meta:
        model = ReportDefinition
        fields = [
            'id', 'name', 'report_type', 'description', 'query',
            'parameters', 'format', 'is_active', 'created_by',
            'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['created_by', 'created_at', 'updated_at']
    
    def create(self, validated_data):
        validated_data['created_by'] = self.context['request'].user
        return super().create(validated_data)


class DashboardWidgetSerializer(serializers.ModelSerializer):
    class Meta:
        model = DashboardWidget
        fields = [
            'id', 'name', 'widget_type', 'chart_type', 'data_source',
            'refresh_interval', 'position', 'size', 'config',
            'is_visible', 'created_at', 'updated_at'
        ]