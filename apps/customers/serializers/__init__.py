from .customer_serializers import (
    CustomerSerializer, CustomerCreateSerializer, 
    CustomerUpdateSerializer, CustomerListSerializer,
    CustomerDetailSerializer
)
from .address_serializers import (
    CustomerAddressSerializer, CustomerAddressCreateSerializer
)
from .document_serializers import (
    CustomerDocumentSerializer, DocumentUploadSerializer
)
from .kin_serializers import NextOfKinSerializer
from .note_serializers import CustomerNotesSerializer
from .service_serializers import (
    ServiceConnectionSerializer, ServiceCreateSerializer,
    ServiceActivationSerializer, ServiceSuspensionSerializer
)

__all__ = [
    'CustomerSerializer',
    'CustomerCreateSerializer',
    'CustomerUpdateSerializer',
    'CustomerListSerializer',
    'CustomerDetailSerializer',
    'CustomerAddressSerializer',
    'CustomerAddressCreateSerializer',
    'CustomerDocumentSerializer',
    'DocumentUploadSerializer',
    'NextOfKinSerializer',
    'CustomerNotesSerializer',
    'ServiceConnectionSerializer',
    'ServiceCreateSerializer',
    'ServiceActivationSerializer',
    'ServiceSuspensionSerializer',
]
