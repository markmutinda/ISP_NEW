import logging
from decimal import Decimal

from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context

from apps.core.email_delivery import send_transactional_email

logger = logging.getLogger(__name__)


def money(value):
    return Decimal(str(value or "0")).quantize(Decimal("0.01"))


def get_tenant_for_subscription(subscription):
    company = subscription.company
    tenant = getattr(company, "tenant", None)
    if not tenant and hasattr(company, "tenant_set"):
        tenant = company.tenant_set.first()
    return tenant


def subscription_receipt_number(payment):
    if payment.mpesa_receipt:
        return payment.mpesa_receipt
    return f"NET-RCPT-{str(payment.id).split('-')[0].upper()}"


def sync_subscription_invoice_payment(payment, *, notify=True):
    """
    Mirror a successful public SubscriptionPayment onto the tenant NET-BILL invoice.

    Subscription payments live in public schema, while tenant-facing invoices live in
    the tenant schema. This helper keeps them consistent after webhook or polling
    completion and sends a receipt-style tenant notification.
    """
    subscription = payment.subscription
    tenant = get_tenant_for_subscription(subscription)
    if not tenant:
        logger.warning("No tenant found while syncing subscription payment %s", payment.id)
        return None

    paid_at = payment.completed_at or timezone.now()
    receipt_number = subscription_receipt_number(payment)
    invoice = None

    with schema_context(tenant.schema_name):
        from django.contrib.auth import get_user_model

        from apps.billing.models import Invoice, InvoiceItem
        from apps.customers.models import Customer

        User = get_user_model()
        billing_user, _ = User.objects.get_or_create(
            email="billing@netily.io",
            defaults={
                "first_name": "Netily",
                "last_name": "Platform",
                "role": "admin",
                "is_staff": True,
                "is_active": True,
            },
        )
        sys_customer, _ = Customer.objects.get_or_create(
            customer_code="NET-001",
            defaults={"user": billing_user, "status": "active"},
        )

        unpaid = (
            Invoice.objects.filter(invoice_number__startswith="NET-BILL")
            .exclude(status__in=["VOIDED", "WRITTEN_OFF", "CANCELLED"])
            .order_by("created_at", "id")
        )

        amount_remaining = money(payment.amount)
        for candidate in unpaid:
            candidate_balance = money(candidate.balance if candidate.balance is not None else candidate.total_amount)
            if candidate_balance <= 0:
                continue

            applied = min(amount_remaining, candidate_balance)
            candidate.amount_paid = money((candidate.amount_paid or Decimal("0.00")) + applied)
            candidate.balance = money(max(candidate_balance - applied, Decimal("0.00")))
            if candidate.balance <= 0:
                candidate.status = "PAID"
                candidate.paid_at = paid_at
            else:
                candidate.status = "PARTIAL"
            candidate.save(update_fields=["amount_paid", "balance", "status", "paid_at", "updated_at"])
            invoice = candidate
            amount_remaining = money(amount_remaining - applied)
            if amount_remaining <= 0:
                break

        if not invoice:
            plan_name = subscription.plan.name if subscription.plan else "Netily Platform"
            invoice = Invoice.objects.create(
                invoice_number=f'NET-BILL-{paid_at.strftime("%y%m%d%H%M%S")}',
                customer=sys_customer,
                subtotal=money(payment.amount),
                total_amount=money(payment.amount),
                amount_paid=money(payment.amount),
                balance=Decimal("0.00"),
                status="PAID",
                paid_at=paid_at,
                due_date=paid_at.date(),
                billing_date=paid_at.date(),
                service_period_start=(subscription.current_period_start or paid_at).date(),
                service_period_end=(subscription.current_period_end or paid_at).date(),
                notes="Netily platform subscription payment receipt.",
            )
            InvoiceItem.objects.create(
                invoice=invoice,
                description=f"Netily Platform Subscription - {plan_name}",
                quantity=1,
                unit_price=money(payment.amount),
                tax_rate=0,
                tax_amount=0,
                total=money(payment.amount),
            )

        note = (
            f"Subscription payment received. Receipt: {receipt_number}. "
            f"Amount: KES {money(payment.amount)}. Balance: KES {money(invoice.balance)}"
        )
        existing_notes = invoice.internal_notes or ""
        if receipt_number not in existing_notes:
            invoice.internal_notes = f"{existing_notes}\n{note}".strip()
            invoice.save(update_fields=["internal_notes", "updated_at"])

        if notify:
            if money(invoice.balance) <= 0:
                notify_subscription_payment_received(tenant, payment, invoice)
            else:
                notify_subscription_partial_payment_received(tenant, payment, invoice)

    return invoice


def subscription_invoice_is_fully_paid(invoice):
    return bool(invoice and money(getattr(invoice, "balance", 0)) <= 0 and str(getattr(invoice, "status", "")).upper() == "PAID")


def notify_subscription_payment_received(tenant, payment, invoice):
    """Send email + in-app receipt feedback to tenant admins after payment success."""
    subject = f"Payment received - {invoice.invoice_number}"
    receipt_number = subscription_receipt_number(payment)
    amount = money(payment.amount)
    paid_at = payment.completed_at or timezone.now()
    message = (
        f"Your Netily subscription payment of KES {amount:,.2f} has been received.\n\n"
        f"Invoice: {invoice.invoice_number}\n"
        f"Receipt: {receipt_number}\n"
        f"Paid at: {paid_at.strftime('%Y-%m-%d %H:%M')}\n\n"
        "Your account is active. Thank you for keeping your Netily subscription current."
    )

    with schema_context(tenant.schema_name):
        from apps.core.models import User
        from apps.notifications.models import Notification
        from apps.notifications.services.notification_manager import NotificationManager

        admins = list(User.objects.filter(is_active=True, role__in=["admin", "super_admin", "superadmin", "owner", "accountant", "support"]))
        notification_manager = NotificationManager()

        for admin in admins:
            if admin.email:
                send_transactional_email(
                    subject=subject,
                    recipient=admin.email,
                    plain_message=message,
                    html_message=message.replace("\n", "<br>"),
                )

            notification = Notification.objects.create(
                user=admin,
                notification_type="in_app",
                subject=subject,
                message=message,
                priority=4,
                metadata={
                    "source": "subscription_payment_receipt",
                    "payment_id": str(payment.id),
                    "invoice_id": invoice.id,
                    "receipt_number": receipt_number,
                },
            )
            notification_manager.send_notification(notification)


def notify_subscription_partial_payment_received(tenant, payment, invoice):
    """Send receipt feedback without claiming the subscription is active."""
    subject = f"Partial payment received - {invoice.invoice_number}"
    receipt_number = subscription_receipt_number(payment)
    amount = money(payment.amount)
    balance = money(invoice.balance)
    paid_at = payment.completed_at or timezone.now()
    message = (
        f"Your Netily subscription payment of KES {amount:,.2f} has been received.\n\n"
        f"Invoice: {invoice.invoice_number}\n"
        f"Receipt: {receipt_number}\n"
        f"Paid at: {paid_at.strftime('%Y-%m-%d %H:%M')}\n"
        f"Remaining balance: KES {balance:,.2f}\n\n"
        "Your account will reactivate once the remaining invoice balance is settled."
    )

    with schema_context(tenant.schema_name):
        from apps.core.models import User
        from apps.notifications.models import Notification
        from apps.notifications.services.notification_manager import NotificationManager

        admins = list(User.objects.filter(is_active=True, role__in=["admin", "super_admin", "superadmin", "owner", "accountant", "support"]))
        notification_manager = NotificationManager()

        for admin in admins:
            if admin.email:
                send_transactional_email(
                    subject=subject,
                    recipient=admin.email,
                    plain_message=message,
                    html_message=message.replace("\n", "<br>"),
                )

            notification = Notification.objects.create(
                user=admin,
                notification_type="in_app",
                subject=subject,
                message=message,
                priority=4,
                metadata={
                    "source": "subscription_partial_payment_receipt",
                    "payment_id": str(payment.id),
                    "invoice_id": invoice.id,
                    "receipt_number": receipt_number,
                    "remaining_balance": str(balance),
                },
            )
            notification_manager.send_notification(notification)


def latest_subscription_receipt(subscription):
    with schema_context(get_public_schema_name()):
        payment = subscription.payments.filter(status="completed").order_by("-completed_at", "-created_at").first()
        if not payment:
            return None
        return {
            "payment_id": str(payment.id),
            "amount": str(money(payment.amount)),
            "currency": payment.currency,
            "receipt_number": subscription_receipt_number(payment),
            "mpesa_receipt": payment.mpesa_receipt or "",
            "phone_number": payment.phone_number or "",
            "completed_at": payment.completed_at.isoformat() if payment.completed_at else None,
            "status": payment.status,
        }
