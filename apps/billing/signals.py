from django.db import connection
from django.db.models.signals import post_save, pre_save, post_delete
from django.dispatch import receiver
from django.utils import timezone
from .models.billing_models import Invoice, InvoiceItem
from .models.payment_models import Payment
from .models.voucher_models import Voucher
from .models.hotspot_models import HotspotPlan, HotspotBranding
from .integrations.africastalking import SMSService
from apps.core.cache_versioning import bump_cache_version
import logging

logger = logging.getLogger(__name__)


@receiver(pre_save, sender=Invoice)
def calculate_invoice_totals(sender, instance, **kwargs):
    """Calculate invoice totals before saving"""
    # Track if status changed for use in post_save
    if instance.pk:
        try:
            old_instance = sender.objects.get(pk=instance.pk)
            instance._status_changed = (old_instance.status != instance.status)
        except sender.DoesNotExist:
            instance._status_changed = False
    else:
        instance._status_changed = True
    
    if instance.pk:
        # Update totals based on items
        items = instance.items.all()
        instance.subtotal = sum(item.total for item in items)
        instance.tax_amount = sum(item.tax_amount for item in items)
        instance.total_amount = instance.subtotal + instance.tax_amount - instance.discount_amount
        instance.balance = instance.total_amount - instance.amount_paid


@receiver(pre_save, sender=Payment)
def track_payment_status_change(sender, instance, **kwargs):
    """Track if payment status changed to COMPLETED"""
    if instance.pk:
        try:
            old = sender.objects.get(pk=instance.pk)
            instance._status_changed = (old.status != instance.status)
        except sender.DoesNotExist:
            instance._status_changed = True
    else:
        instance._status_changed = True  # new object


# ============================================================
# FIX 5: handle_payment_completion - Fixed invoice status update
# ============================================================
@receiver(post_save, sender=Payment)
def handle_payment_completion(sender, instance, created, **kwargs):
    """Handle actions when payment is completed"""
    # FIX: send SMS for both newly created completed payments AND status changes to COMPLETED
    is_completed = instance.status == 'COMPLETED'
    status_just_set = created or getattr(instance, '_status_changed', False)
    
    if not is_completed:
        return

    if not status_just_set:
        return

    customer = instance.customer
    if customer:
        from decimal import Decimal
        customer.outstanding_balance = max(
            Decimal('0'),
            (customer.outstanding_balance or Decimal('0')) - instance.amount
        )
        customer.save(update_fields=['outstanding_balance', 'updated_at'])
    
    # ============================================================
    # FIX 5: Recalculate invoice totals from all completed payments
    # to avoid drift and correctly set status
    # ============================================================
    if instance.invoice:
        from django.db.models import Sum
        invoice = instance.invoice
        # Recalculate from all completed payments to avoid drift
        total_paid = invoice.payments.filter(
            status='COMPLETED'
        ).aggregate(s=Sum('amount'))['s'] or 0
        
        invoice.amount_paid = total_paid
        invoice.balance = max(0, invoice.total_amount - invoice.amount_paid)
        
        was_paid = (invoice.status == 'PAID')
        if invoice.balance <= 0 and not was_paid:
            invoice.status = 'PAID'
            invoice.paid_at = timezone.now()
            invoice.paid_by = instance.created_by
        elif invoice.amount_paid > 0 and invoice.balance > 0:
            invoice.status = 'PARTIAL'
        
        invoice.save(update_fields=['amount_paid', 'balance', 'status', 'paid_at', 'paid_by'])

        # If invoice status became PAID due to this payment, restore credentials and send resume SMS
        if invoice.status == 'PAID' and not was_paid and customer:
            try:
                if hasattr(customer, 'radius_credentials'):
                    creds = customer.radius_credentials
                    if not creds.is_enabled:
                        creds.is_enabled = True
                        creds.disabled_reason = ''
                        creds.save()
                        from apps.messaging.services.notification_sender import SMSNotifier
                        # Pass schema_name for tenant isolation
                        from django.db import connection as _conn
                        SMSNotifier.pppoe_resumed(customer, schema_name=_conn.schema_name)
            except Exception as e:
                logger.warning(f"Resume SMS after invoice paid failed: {e}")

    # ============================================================
    # REMOVED: Duplicate payment confirmation SMS
    # The webhook (C2B) already calls pppoe_renewal which sends the
    # payment confirmation. This signal call was causing double SMS.
    # Keeping the email notification below as it's separate.
    # ============================================================
    # Send payment confirmation SMS with proper schema context
    # if customer:
    #     try:
    #         from apps.messaging.services.notification_sender import SMSNotifier
    #         from django.db import connection as _conn
    #         SMSNotifier.pppoe_payment(
    #             customer=customer,
    #             amount=float(instance.amount),
    #             reference=instance.payment_reference or instance.mpesa_receipt or '',
    #             schema_name=_conn.schema_name,
    #         )
    #     except Exception as e:
    #         logger.warning(f"Payment confirmation SMS failed: {e}")

    # Send payment confirmation email (kept - separate channel)
    if customer:
        try:
            from django.db import connection
            from apps.billing.tasks import send_payment_confirmation_email
            send_payment_confirmation_email.delay(
                customer_id=customer.id,
                amount=float(instance.amount),
                reference=instance.payment_reference or '',
                payment_method=str(instance.payment_method) if instance.payment_method else '',
                tenant_schema=connection.schema_name,
            )
        except Exception as e:
            logger.warning(f"Payment email task failed: {e}")


@receiver(post_save, sender=Voucher)
def handle_voucher_sale(sender, instance, created, **kwargs):
    """Handle actions when voucher is sold"""
    if instance.sold_to and instance.sold_at:
        # Send voucher PIN via SMS using SMSNotifier
        try:
            from apps.messaging.services.notification_sender import SMSNotifier
            from django.db import connection as _conn
            # Check if the voucher is for hotspot or PPPoE
            if hasattr(instance, 'batch') and instance.batch and instance.batch.hotspot_plan:
                # Hotspot voucher
                SMSNotifier.hotspot_voucher_sold(instance, schema_name=_conn.schema_name)
            else:
                # Generic voucher
                SMSNotifier.voucher_sold(instance, schema_name=_conn.schema_name)
        except Exception as e:
            logger.warning(f"Voucher SMS failed: {e}")


@receiver(post_save, sender=Invoice)
def send_invoice_notification(sender, instance, created, **kwargs):
    """
    Send notification when invoice is issued.
    
    Fires when:
    1. A new invoice is created with status 'ISSUED' (created=True)
    2. An existing draft invoice is updated to 'ISSUED' (status changed)
    
    This ensures that invoices generated by the metered billing system
    (which are created directly as ISSUED) will send notifications.
    """
    # Only send notification when invoice status is ISSUED
    if instance.status == 'ISSUED':
        # Check if this is either:
        # - A newly created invoice (created=True)
        # - An existing invoice that just had its status changed to ISSUED
        status_just_changed = getattr(instance, '_status_changed', False)
        
        if created or status_just_changed:
            try:
                # Get the customer
                customer = instance.customer
                if customer:
                    # Use SMSNotifier for invoice notification with schema context
                    from apps.messaging.services.notification_sender import SMSNotifier
                    from django.db import connection as _conn
                    SMSNotifier.pppoe_invoice_issued(customer, instance, schema_name=_conn.schema_name)
            except Exception as e:
                logger.error(f"Failed to send invoice notification: {e}")


# ============================================================
# FIX 4: Cache invalidation signals for captive portal
# Bumps cache version when hotspot plans or branding change
# ============================================================

@receiver(post_save, sender=HotspotPlan)
@receiver(post_delete, sender=HotspotPlan)
def _bump_captive_cache_on_plan_change(sender, instance, **kwargs):
    """
    Bump cache version when a HotspotPlan is created, updated, or deleted.
    This instantly invalidates all cached captive portal payloads for this tenant.
    """
    try:
        schema_name = connection.schema_name
        bump_cache_version(schema_name)
        logger.debug(f"Cache version bumped for schema {schema_name} due to HotspotPlan change")
    except Exception as e:
        logger.error(f"Failed to bump cache version on HotspotPlan change: {e}")


@receiver(post_save, sender=HotspotBranding)
@receiver(post_delete, sender=HotspotBranding)
def _bump_captive_cache_on_branding_change(sender, instance, **kwargs):
    """
    Bump cache version when HotspotBranding is created, updated, or deleted.
    This instantly invalidates all cached captive portal payloads for this tenant.
    """
    try:
        schema_name = connection.schema_name
        bump_cache_version(schema_name)
        logger.debug(f"Cache version bumped for schema {schema_name} due to HotspotBranding change")
    except Exception as e:
        logger.error(f"Failed to bump cache version on HotspotBranding change: {e}")