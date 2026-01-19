# Re-export all views from the sub-modules so they can be imported as "from .views import ..."

from .customer_dashboard import CustomerDashboardView
from .payment_view import PaymentView
from .service_request_view import (
    ServiceRequestListCreateView,
    ServiceRequestDetailView,
    ServiceRequestTypesView,
)

__all__ = [
    'CustomerDashboardView',
    'PaymentView',
    'ServiceRequestListCreateView',
    'ServiceRequestDetailView',
    'ServiceRequestTypesView',
]
