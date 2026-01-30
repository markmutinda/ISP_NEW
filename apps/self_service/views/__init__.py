# Re-export all views from the sub-modules so they can be imported as "from .views import ..."

from .customer_dashboard import CustomerDashboardView
from .payment_view import PaymentView
from .service_request_view import (
    ServiceRequestListCreateView,
    ServiceRequestDetailView,
    ServiceRequestTypesView,
)

# Authentication and Registration Views
from .auth_views import (
    CustomerSelfRegisterView,
    CustomerLoginView,
    VerifyPhoneView,
    ResendOTPView,
    AvailablePlansView,
)

# Payment Status Views
from .payment_status_view import (
    PaymentStatusView,
    PaymentRefreshStatusView,
    CustomerPaymentsListView,
)

# Invoice Views
from .invoices_view import CustomerInvoicesView

# Alert Views
from .alerts_view import (
    CustomerAlertsView,
    MarkAlertReadView,
    MarkAllAlertsReadView,
)

__all__ = [
    # Dashboard
    'CustomerDashboardView',
    # Payments
    'PaymentView',
    'PaymentStatusView',
    'PaymentRefreshStatusView',
    'CustomerPaymentsListView',
    # Invoices
    'CustomerInvoicesView',
    # Service Requests
    'ServiceRequestListCreateView',
    'ServiceRequestDetailView',
    'ServiceRequestTypesView',
    # Authentication
    'CustomerSelfRegisterView',
    'CustomerLoginView',
    'VerifyPhoneView',
    'ResendOTPView',
    'AvailablePlansView',
    # Alerts
    'CustomerAlertsView',
    'MarkAlertReadView',
    'MarkAllAlertsReadView',
]
