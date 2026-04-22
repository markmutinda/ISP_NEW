import django_filters
from .models import EquipmentItem, PurchaseOrder


class EquipmentItemFilter(django_filters.FilterSet):
    status = django_filters.CharFilter(field_name='status', lookup_expr='exact')
    condition = django_filters.CharFilter(field_name='condition', lookup_expr='exact')

    # Filter by equipment type ID (integer PK) — frontend sends the ID from the Select
    equipment_type = django_filters.NumberFilter(field_name='equipment_type__id')

    # Also support filtering by name for convenience
    equipment_type_name = django_filters.CharFilter(
        field_name='equipment_type__name', lookup_expr='icontains'
    )

    supplier = django_filters.NumberFilter(field_name='supplier__id')

    purchase_date_from = django_filters.DateFilter(field_name='purchase_date', lookup_expr='gte')
    purchase_date_to = django_filters.DateFilter(field_name='purchase_date', lookup_expr='lte')
    warranty_expiry_from = django_filters.DateFilter(field_name='warranty_expiry', lookup_expr='gte')
    warranty_expiry_to = django_filters.DateFilter(field_name='warranty_expiry', lookup_expr='lte')
    min_price = django_filters.NumberFilter(field_name='purchase_price', lookup_expr='gte')
    max_price = django_filters.NumberFilter(field_name='purchase_price', lookup_expr='lte')

    class Meta:
        model = EquipmentItem
        fields = ['status', 'condition', 'equipment_type', 'supplier']


class PurchaseOrderFilter(django_filters.FilterSet):
    status = django_filters.CharFilter(field_name='status')
    supplier = django_filters.NumberFilter(field_name='supplier__id')
    order_date_from = django_filters.DateFilter(field_name='order_date', lookup_expr='gte')
    order_date_to = django_filters.DateFilter(field_name='order_date', lookup_expr='lte')
    min_amount = django_filters.NumberFilter(field_name='total_amount', lookup_expr='gte')
    max_amount = django_filters.NumberFilter(field_name='total_amount', lookup_expr='lte')

    class Meta:
        model = PurchaseOrder
        fields = ['status', 'supplier']