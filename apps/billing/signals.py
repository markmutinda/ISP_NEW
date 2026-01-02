from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone
from .models.billing_models import Invoice, InvoiceItem
from .models.payment_models import Payment
from .models.voucher_models import Voucher
from .integrations.africastalking import SMSService


@receiver(pre_save, sender=Invoice)
def calculate_invoice_totals(sender, instance, **kwargs):
    """Calculate invoice totals before saving"""
    if instance.pk:
        # Update totals based on items
        items = instance.items.all()
        instance.subtotal = sum(item.total for item in items)
        instance.tax_amount = sum(item.tax_amount for item in items)
        instance.total_amount = instance.subtotal + instance.tax_amount - instance.discount_amount
        instance.balance = instance.total_amount - instance.amount_paid


@receiver(post_save, sender=Payment)
def handle_payment_completion(sender, instance, created, **kwargs):
    """Handle actions when payment is completed"""
    if not created and instance.status == 'COMPLETED':
        # Update customer balance
        customer = instance.customer
        if customer:
            customer.outstanding_balance = max(
                0,
                customer.outstanding_balance - instance.amount
            )
            customer.save(update_fields=['outstanding_balance', 'updated_at'])
        
        # Update invoice if exists
        if instance.invoice:
            invoice = instance.invoice
            invoice.amount_paid += instance.amount
            invoice.balance = invoice.total_amount - invoice.amount_paid
            
            if invoice.balance <= 0:
                invoice.status = 'PAID'
                invoice.paid_at = timezone.now()
                invoice.paid_by = instance.created_by
            
            invoice.save()


@receiver(post_save, sender=Voucher)
def handle_voucher_sale(sender, instance, created, **kwargs):
    """Handle actions when voucher is sold"""
    if instance.sold_to and instance.sold_at:
        # Send voucher PIN via SMS
        try:
            sms_service = SMSService(instance.batch.company)
            sms_service.send_voucher_pin(instance.sold_to, instance)
        except Exception as e:
            # Log error but don't raise
            pass


@receiver(post_save, sender=Invoice)
def send_invoice_notification(sender, instance, created, **kwargs):
    """Send notification when invoice is issued"""
    if not created and instance.status == 'ISSUED':
        # Send invoice notification via SMS
        try:
            sms_service = SMSService(instance.company)
            sms_service.send_invoice_reminder(instance.customer, instance)
        except Exception as e:
            # Log error but don't raise
            pass