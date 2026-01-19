import django_filters
from .models import EquipmentItem, PurchaseOrder


class EquipmentItemFilter(django_filters.FilterSet):
    status = django_filters.CharFilter(field_name='status')
    condition = django_filters.CharFilter(field_name='condition')
    equipment_type = django_filters.CharFilter(field_name='equipment_type__name')
    supplier = django_filters.CharFilter(field_name='supplier__name')
    
    purchase_date_from = django_filters.DateFilter(
        field_name='purchase_date',
        lookup_expr='gte'
    )
    purchase_date_to = django_filters.DateFilter(
        field_name='purchase_date',
        lookup_expr='lte'
    )
    
    warranty_expiry_from = django_filters.DateFilter(
        field_name='warranty_expiry',
        lookup_expr='gte'
    )
    warranty_expiry_to = django_filters.DateFilter(
        field_name='warranty_expiry',
        lookup_expr='lte'
    )
    
    min_price = django_filters.NumberFilter(
        field_name='purchase_price',
        lookup_expr='gte'
    )
    max_price = django_filters.NumberFilter(
        field_name='purchase_price',
        lookup_expr='lte'
    )
    
    class Meta:
        model = EquipmentItem
        fields = {
            'status': ['exact'],
            'condition': ['exact'],
            'equipment_type': ['exact'],
            'supplier': ['exact'],
        }


class PurchaseOrderFilter(django_filters.FilterSet):
    status = django_filters.CharFilter(field_name='status')
    supplier = django_filters.CharFilter(field_name='supplier__name')
    
    order_date_from = django_filters.DateFilter(
        field_name='order_date',
        lookup_expr='gte'
    )
    order_date_to = django_filters.DateFilter(
        field_name='order_date',
        lookup_expr='lte'
    )
    
    min_amount = django_filters.NumberFilter(
        field_name='total_amount',
        lookup_expr='gte'
    )
    max_amount = django_filters.NumberFilter(
        field_name='total_amount',
        lookup_expr='lte'
    )
    
    class Meta:
        model = PurchaseOrder
        fields = {
            'status': ['exact'],
            'supplier': ['exact'],
        }
