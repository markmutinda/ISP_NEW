import logging
from decimal import Decimal

from django.db import transaction
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


def _payment_window(payment, subscription):
    start = getattr(payment, "period_start", None) or getattr(subscription, "current_period_start", None)
    end = getattr(payment, "period_end", None) or getattr(subscription, "current_period_end", None)
    if start and not end:
        end = subscription.next_period_end(start)
    return start, end


def _candidate_invoice_references(subscription, tenant, payment=None):
    """
    Prefer the NET-BILL invoice that belongs to the payment window so a current
    renewal is not swallowed by an unrelated older open invoice.
    """
    if not tenant or not subscription:
        return []

    from .models import BillingCycle

    start, end = _payment_window(payment, subscription)
    with schema_context(get_public_schema_name()):
        qs = (
            BillingCycle.objects.filter(
                tenant=tenant,
                subscription=subscription,
                invoice_reference__isnull=False,
            )
            .exclude(invoice_reference="")
            .order_by("-end_date", "-start_date")
        )
        if start and end:
            overlap = list(qs.filter(start_date__lt=end, end_date__gt=start).values_list("invoice_reference", flat=True))
            if overlap:
                return [str(ref) for ref in overlap if ref]

        return [
            str(ref)
            for ref in qs.filter(status__in=["invoiced", "active"]).values_list("invoice_reference", flat=True)[:5]
            if ref
        ]


def get_target_net_bill_invoice_balance(subscription, tenant):
    """
    Return the most relevant outstanding tenant NET-BILL invoice and balance,
    falling back to the oldest open invoice for legacy records.
    """
    candidate_refs = _candidate_invoice_references(subscription, tenant)
    with schema_context(tenant.schema_name):
        from apps.billing.models import Invoice

        invoices = (
            Invoice.objects.filter(invoice_number__startswith="NET-BILL")
            .exclude(status__in=["VOIDED", "WRITTEN_OFF", "CANCELLED"])
            .filter(balance__gt=0)
        )
        if candidate_refs:
            by_cycle = {str(invoice.pk): invoice for invoice in invoices.filter(pk__in=candidate_refs)}
            for ref in candidate_refs:
                invoice = by_cycle.get(str(ref))
                if invoice:
                    return invoice, money(invoice.balance or invoice.total_amount)

        invoice = invoices.order_by("created_at", "id").first()
        if invoice:
            return invoice, money(invoice.balance or invoice.total_amount)
    return None, Decimal("0.00")


@transaction.atomic
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
    candidate_refs = _candidate_invoice_references(subscription, tenant, payment)

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
            Invoice.objects.select_for_update().filter(invoice_number__startswith="NET-BILL")
            .exclude(status__in=["VOIDED", "WRITTEN_OFF", "CANCELLED"])
            .order_by("created_at", "id")
        )

        # Use the immutable payment ID: Daraja query recovery may precede the receipt.
        payment_marker = f"[subscription-payment:{payment.id}]"
        existing_invoice = unpaid.filter(internal_notes__contains=payment_marker).last()
        if existing_invoice:
            return existing_invoice
        amount_remaining = money(payment.amount)

        if candidate_refs:
            targeted = {str(candidate.pk): candidate for candidate in unpaid.filter(pk__in=candidate_refs)}
            for ref in candidate_refs:
                candidate = targeted.get(str(ref))
                if not candidate:
                    continue
                existing_notes = candidate.internal_notes or ""
                if receipt_number and receipt_number in existing_notes:
                    invoice = candidate
                    amount_remaining = Decimal("0.00")
                    break

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
                candidate.internal_notes = f"{existing_notes}\n{payment_marker} Receipt: {receipt_number}. Applied: KES {applied}.".strip()
                candidate.save(update_fields=["internal_notes", "updated_at"])
                if amount_remaining <= 0:
                    break

        for candidate in unpaid:
            if amount_remaining <= 0:
                break
            if str(candidate.pk) in set(candidate_refs):
                continue
            existing_notes = candidate.internal_notes or ""
            if receipt_number and receipt_number in existing_notes:
                invoice = candidate
                amount_remaining = Decimal("0.00")
                break

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
            candidate.internal_notes = f"{existing_notes}\n{payment_marker} Receipt: {receipt_number}. Applied: KES {applied}.".strip()
            candidate.save(update_fields=["internal_notes", "updated_at"])
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
            f"{payment_marker} Subscription payment received. Receipt: {receipt_number}. "
            f"Amount: KES {money(payment.amount)}. Balance: KES {money(invoice.balance)}"
        )
        existing_notes = invoice.internal_notes or ""
        if payment_marker not in existing_notes:
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


def complete_subscription_stk_payment(payment, mpesa_receipt=""):
    """
    Idempotently mark a subscription STK payment completed, sync the tenant
    NET-BILL invoice, and advance the subscription lifecycle (trial
    conversion or cycle extension). Safe to call from the webhook — it
    locks the row so a duplicate Safaricom callback is a no-op.
    """
    from django.db import transaction
    from .models import CompanySubscription, SubscriptionPayment

    with schema_context(get_public_schema_name()):
        with transaction.atomic():
            locked = SubscriptionPayment.objects.select_for_update().get(id=payment.id)
            subscription = CompanySubscription.objects.select_for_update().get(pk=locked.subscription_id)
            locked.subscription = subscription
            if locked.activation_applied_at:
                if mpesa_receipt and not locked.mpesa_receipt:
                    locked.mpesa_receipt = mpesa_receipt
                    locked.save(update_fields=['mpesa_receipt'])
                return locked, None
            # Legacy completions have no activation marker. Do not replay payments
            # already covered by a later billing period, even after it expires.
            period_already_advanced = (
                locked.completed_at and subscription.current_period_start
                and subscription.current_period_start >= locked.completed_at
            ) or (
                locked.period_end and subscription.current_period_end
                and subscription.current_period_end >= locked.period_end
            )
            if locked.status == 'completed' and period_already_advanced:
                locked.activation_applied_at = locked.completed_at or timezone.now()
                locked.save(update_fields=['activation_applied_at'])
                return locked, None
            newly_received = locked.status != 'completed'
            if newly_received:
                locked.mark_completed(mpesa_receipt=mpesa_receipt)
            invoice = sync_subscription_invoice_payment(locked, notify=False)
            if mpesa_receipt and not locked.mpesa_receipt:
                locked.mpesa_receipt = mpesa_receipt
                locked.save(update_fields=['mpesa_receipt'])
            invoice_fully_paid = subscription_invoice_is_fully_paid(invoice)
            if invoice_fully_paid:
                locked.apply_intended_plan()
                if subscription.is_trial or subscription.status in ('trialing', 'expired', 'pending'):
                    subscription.convert_from_trial(
                        billing_period=subscription.billing_period,
                        defer_to_trial_end=locked.defer_billing_to_trial_end,
                        paid_at=locked.completed_at,
                    )
                else:
                    subscription.extend_subscription()
                locked.activation_applied_at = timezone.now()
                locked.save(update_fields=['activation_applied_at'])
                logger.info('Subscription payment activated: payment=%s subscription=%s period_end=%s',
                            locked.id, subscription.id, subscription.current_period_end)
                transaction.on_commit(lambda: _queue_completed_subscription_notice(locked.id, invoice.pk))
            else:
                logger.info('Subscription payment received; settlement pending: payment=%s invoice=%s',
                            locked.id, getattr(invoice, 'pk', None))
                if newly_received and invoice:
                    transaction.on_commit(lambda: _queue_completed_subscription_notice(locked.id, invoice.pk, activated=False))
            return locked, invoice


def _notify_completed_subscription(payment, invoice):
    """Notification outages must never roll back payment settlement or renewal."""
    try:
        from .tasks import send_cycle_activated_email
        send_cycle_activated_email.delay(payment.subscription.company_id)
    except Exception:
        logger.exception('Activation email enqueue failed: payment=%s', payment.id)
    try:
        tenant = get_tenant_for_subscription(payment.subscription)
        if tenant and invoice:
            notify_subscription_payment_received(tenant, payment, invoice)
    except Exception:
        logger.exception('Subscription receipt notification failed: payment=%s', payment.id)
    notify_telegram_subscription_payment(payment, invoice)


def _queue_completed_subscription_notice(payment_id, invoice_id, activated=True):
    try:
        from .tasks import send_subscription_payment_receipt
        send_subscription_payment_receipt.apply_async(args=[str(payment_id), str(invoice_id), activated], retry=False)
    except Exception:
        logger.exception('Subscription receipt enqueue failed: payment=%s', payment_id)


def notify_telegram_subscription_payment(payment, invoice=None):
    """Send a highlighted Telegram alert when a tenant pays their Netily subscription."""
    try:
        from apps.notifications.tasks import send_telegram_payment_alert_task

        subscription = payment.subscription
        company_name = subscription.company.name
        plan_name = subscription.plan.name if subscription.plan else "Netily Plan"
        amount = money(payment.amount)
        receipt = payment.mpesa_receipt or "N/A"
        balance = money(getattr(invoice, 'balance', 0)) if invoice else Decimal("0.00")

        message = (
            "💰 <b>SUBSCRIPTION PAYMENT RECEIVED</b>\n\n"
            f"🏢 Tenant: <b>{company_name}</b>\n"
            f"📦 Plan: {plan_name}\n"
            f"💵 Amount: <b>KES {amount:,.2f}</b>\n"
            f"🧾 Receipt: {receipt}\n"
            f"📞 Phone: {payment.phone_number or 'N/A'}\n"
            + (
                f"⚠️ Remaining balance: KES {balance:,.2f}\n"
                if balance > 0
                else "✅ Subscription invoice fully settled\n"
            )
        )
        send_telegram_payment_alert_task.apply_async(args=[message], retry=False)
    except Exception as exc:
        logger.warning("Telegram subscription payment alert failed: %s", exc)


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
