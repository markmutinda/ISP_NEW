from decimal import Decimal
from datetime import timedelta
from django.utils import timezone
from django.db.models import Sum, Q
from apps.customers.models import ServiceConnection
from ..models.billing_models import Plan, Invoice, InvoiceItem


class InvoiceCalculator:
    @staticmethod
    def calculate_prorated_amount(plan_price, start_date, end_date, billing_cycle_days):
        """Calculate prorated amount for partial billing period"""
        days_in_period = (end_date - start_date).days + 1
        daily_rate = plan_price / billing_cycle_days
        return daily_rate * days_in_period

    @staticmethod
    def calculate_tax_amount(subtotal, tax_rate, is_inclusive=False):
        """Calculate tax amount"""
        tax_rate = Decimal(str(tax_rate))
        if is_inclusive:
            # Tax is included in the price
            return (subtotal * tax_rate) / (100 + tax_rate)
        else:
            # Tax is added to the price
            return (subtotal * tax_rate) / 100

    @staticmethod
    def calculate_invoice_totals(invoice):
        """Calculate all totals for an invoice"""
        items = invoice.items.all()
        
        subtotal = sum(item.total for item in items)
        tax_amount = sum(item.tax_amount for item in items)
        
        # Calculate discount if any
        discount_amount = invoice.discount_amount or Decimal('0')
        
        total_amount = subtotal + tax_amount - discount_amount
        
        return {
            'subtotal': subtotal,
            'tax_amount': tax_amount,
            'discount_amount': discount_amount,
            'total_amount': total_amount,
            'balance': total_amount - invoice.amount_paid
        }

    @staticmethod
    def generate_invoice_for_service(service_connection, billing_date=None):
        """Generate invoice for a service connection"""
        if not billing_date:
            billing_date = timezone.now().date()
        
        customer = service_connection.customer
        plan = service_connection.plan
        
        # Determine service period
        if service_connection.installation_date:
            period_start = service_connection.installation_date
        else:
            period_start = billing_date - timedelta(days=30)
        
        period_end = billing_date
        
        # Calculate prorated amount if needed
        if plan.prorated_billing and service_connection.installation_date:
            days_in_month = 30  # Standard month
            days_active = (billing_date - service_connection.installation_date).days
            if days_active < days_in_month:
                amount = plan.base_price * (days_active / days_in_month)
            else:
                amount = plan.base_price
        else:
            amount = plan.base_price
        
        # Create invoice
        invoice = Invoice.objects.create(
            company=customer.company,
            customer=customer,
            service_connection=service_connection,
            plan=plan,
            billing_date=billing_date,
            due_date=billing_date + timedelta(days=15),  # 15 days due
            service_period_start=period_start,
            service_period_end=period_end,
            status='DRAFT',
            created_by=customer.user
        )
        
        # Add invoice items
        InvoiceItem.objects.create(
            invoice=invoice,
            description=f"{plan.name} - {plan.billing_cycle} Service",
            quantity=1,
            unit_price=amount,
            tax_rate=plan.tax_rate,
            service_type=plan.plan_type,
            service_period_start=period_start,
            service_period_end=period_end
        )
        
        # Calculate totals
        totals = InvoiceCalculator.calculate_invoice_totals(invoice)
        invoice.subtotal = totals['subtotal']
        invoice.tax_amount = totals['tax_amount']
        invoice.total_amount = totals['total_amount']
        invoice.balance = totals['total_amount']
        invoice.save()
        
        return invoice

    @staticmethod
    def generate_bulk_invoices(company, billing_cycle):
        """Generate invoices for all active services in a company"""
        active_services = ServiceConnection.objects.filter(
            customer__company=company,
            status='ACTIVE',
            is_active=True
        )
        
        invoices = []
        for service in active_services:
            invoice = InvoiceCalculator.generate_invoice_for_service(service)
            invoice.billing_cycle = billing_cycle
            invoice.save()
            invoices.append(invoice)
        
        return invoices

    @staticmethod
    def calculate_outstanding_balance(customer):
        """Calculate total outstanding balance for a customer"""
        outstanding_invoices = Invoice.objects.filter(
            customer=customer,
            status__in=['ISSUED', 'SENT', 'PARTIAL', 'OVERDUE'],
            balance__gt=0
        )
        
        total_outstanding = outstanding_invoices.aggregate(
            Sum('balance')
        )['balance__sum'] or Decimal('0')
        
        return total_outstanding

    @staticmethod
    def apply_discount(invoice, discount_amount, discount_reason=""):
        """Apply discount to an invoice"""
        if discount_amount > invoice.total_amount:
            raise ValueError("Discount amount cannot exceed invoice total")
        
        invoice.discount_amount = discount_amount
        invoice.total_amount = invoice.subtotal + invoice.tax_amount - discount_amount
        invoice.balance = invoice.total_amount - invoice.amount_paid
        
        if discount_reason:
            invoice.notes = f"{invoice.notes}\nDiscount Applied: {discount_reason}"
        
        invoice.save()
        return invoice

    @staticmethod
    def calculate_penalty(invoice, penalty_rate=0.05, grace_period=5):
        """Calculate penalty for overdue invoice"""
        if invoice.status != 'OVERDUE':
            return Decimal('0')
        
        overdue_days = (timezone.now().date() - invoice.due_date).days
        if overdue_days <= grace_period:
            return Decimal('0')
        
        penalty_days = overdue_days - grace_period
        daily_penalty_rate = penalty_rate / 30  # Monthly rate to daily
        
        penalty_amount = invoice.balance * daily_penalty_rate * penalty_days
        return penalty_amount