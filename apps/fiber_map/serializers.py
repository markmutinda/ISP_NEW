from rest_framework import serializers
from .models import NetworkMapElement


class NetworkMapElementSerializer(serializers.ModelSerializer):
    element_type_display = serializers.CharField(source='get_element_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = NetworkMapElement
        fields = [
            'id', 'name', 'element_type', 'element_type_display', 'geometry_type',
            'status', 'status_display', 'severity', 'coordinates', 'properties',
            'color', 'notes', 'parent', 'linked_router_id', 'is_active',
            'resolved_at', 'created_by_name', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'resolved_at']

    def get_created_by_name(self, obj):
        if obj.created_by:
            return obj.created_by.get_full_name() or obj.created_by.email
        return None

    def validate_coordinates(self, value):
        if not isinstance(value, list) or not value:
            raise serializers.ValidationError("Coordinates must be a non-empty list of [lat, lng] pairs.")
        for point in value:
            if not (isinstance(point, (list, tuple)) and len(point) == 2):
                raise serializers.ValidationError("Each coordinate must be a [lat, lng] pair.")
            try:
                lat, lng = float(point[0]), float(point[1])
            except (TypeError, ValueError):
                raise serializers.ValidationError("Coordinates must be numeric.")
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                raise serializers.ValidationError("Coordinates out of range.")
        return value

    def validate(self, attrs):
        geometry_type = attrs.get('geometry_type', getattr(self.instance, 'geometry_type', 'POINT'))
        coordinates = attrs.get('coordinates', getattr(self.instance, 'coordinates', None))
        if coordinates is not None:
            if geometry_type == 'POINT' and len(coordinates) != 1:
                raise serializers.ValidationError({'coordinates': 'A point must have exactly 1 coordinate.'})
            if geometry_type == 'LINE' and len(coordinates) < 2:
                raise serializers.ValidationError({'coordinates': 'A line must have at least 2 coordinates.'})
        return attrs