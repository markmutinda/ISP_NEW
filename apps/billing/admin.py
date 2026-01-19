from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from django.utils import timezone
from django.contrib import messages
from apps.core.models import User
from .models.billing_models import Plan, BillingCycle, Invoice, InvoiceItem
from .models.payment_models import Payment, InvoiceItemPayment, Receipt


# ------------------------------
# Billing Models Admin
# ------------------------------

@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = (
        'name', 'code', 'plan_type', 'base_price', 'download_speed', 'upload_speed', 
        'is_active', 'is_public', 'is_popular', 'subscriber_count'
    )
    list_filter = ('plan_type', 'is_active', 'is_public', 'is_popular')
    search_fields = ('name', 'code', 'description')
    readonly_fields = ('code', 'subscriber_count', 'created_at', 'updated_at')
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'code', 'plan_type', 'description')
        }),
        ('Pricing', {
            'fields': ('base_price', 'setup_fee')
        }),
        ('Speed & Data', {
            'fields': ('download_speed', 'upload_speed', 'data_limit')
        }),
        ('Validity', {
            'fields': ('duration_days', 'validity_hours')
        }),
        ('Fair Usage Policy', {
            'fields': ('fup_limit', 'fup_speed'),
            'classes': ('collapse',)
        }),
        ('Features', {
            'fields': ('features',),
            'classes': ('collapse',)
        }),
        ('Visibility & Status', {
            'fields': ('is_active', 'is_public', 'is_popular')
        }),
        ('Metadata', {
            'fields': ('created_by', 'updated_by', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)

    def has_delete_permission(self, request, obj=None):
        if obj and not request.user.is_superuser:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(BillingCycle)
class BillingCycleAdmin(admin.ModelAdmin):
    list_display = ('cycle_code', 'name', 'start_date', 'end_date', 'due_date', 'status', 'is_locked')
    list_filter = ('status', 'is_locked')
    search_fields = ('cycle_code', 'name', 'notes')
    readonly_fields = (
        'total_invoices', 'total_amount', 'total_paid', 'total_outstanding', 'created_at', 'updated_at'
    )
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'cycle_code')
        }),
        ('Dates', {
            'fields': ('start_date', 'end_date', 'due_date')
        }),
        ('Status', {
            'fields': ('status', 'is_locked')
        }),
        ('Totals', {
            'fields': ('total_invoices', 'total_amount', 'total_paid', 'total_outstanding')
        }),
        ('Closure', {
            'fields': ('closed_by', 'closed_at', 'notes')
        }),
        ('Metadata', {
            'fields': ('created_by', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )

    actions = ['calculate_totals', 'close_cycle']

    def calculate_totals(self, request, queryset):
        for cycle in queryset:
            cycle.calculate_totals()
        self.message_user(request, f"Calculated totals for {queryset.count()} billing cycles.")
    calculate_totals.short_description = "Calculate totals for selected cycles"

    def close_cycle(self, request, queryset):
        for cycle in queryset:
            if not cycle.is_locked:
                cycle.close_cycle(request.user)
        self.message_user(request, f"Closed {queryset.count()} billing cycles.")
    close_cycle.short_description = "Close selected billing cycles"

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)

    def has_delete_permission(self, request, obj=None):
        if obj and not request.user.is_superuser:
            return False
        return super().has_delete_permission(request, obj)


# ------------------------------
# Invoice Admin
# ------------------------------

class InvoiceItemInline(admin.TabularInline):
    model = InvoiceItem
    extra = 1
    readonly_fields = ('total',)
    fields = ('description', 'quantity', 'unit_price', 'tax_rate', 'tax_amount', 'total')


@admin.register(Invoice)
class InvoiceAdmin(admin.ModelAdmin):
    list_display = (
        'invoice_number', 'customer_link', 'billing_date', 'due_date', 'total_amount',
        'amount_paid', 'balance', 'status', 'is_overdue'
    )
    list_filter = ('status', 'is_overdue', 'billing_date')
    search_fields = (
        'invoice_number', 'customer__customer_code', 'customer__user__first_name',
        'customer__user__last_name'
    )
    readonly_fields = (
        'subtotal', 'tax_amount', 'total_amount', 'balance', 'overdue_days',
        'created_at', 'updated_at', 'issued_at', 'paid_at'
    )
    fieldsets = (
        ('Basic Information', {
            'fields': ('invoice_number', 'customer', 'billing_cycle')
        }),
        ('Dates', {
            'fields': ('billing_date', 'due_date', 'payment_terms', 
                       'service_period_start', 'service_period_end')
        }),
        ('Amounts', {
            'fields': ('subtotal', 'tax_amount', 'discount_amount', 'total_amount', 
                       'amount_paid', 'balance')
        }),
        ('Status', {
            'fields': ('status', 'is_overdue', 'overdue_days')
        }),
        ('Payment', {
            'fields': ('paid_at', 'paid_by')
        }),
        ('References', {
            'fields': ('service_connection', 'plan')
        }),
        ('Notes', {
            'fields': ('notes', 'internal_notes')
        }),
        ('Issuance', {
            'fields': ('issued_by', 'issued_at'),
            'classes': ('collapse',)
        }),
        ('Metadata', {
            'fields': ('created_by', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )
    inlines = [InvoiceItemInline]

    def customer_link(self, obj):
        url = reverse('admin:customers_customer_change', args=[obj.customer.id])
        return format_html('<a href="{}">{}</a>', url, obj.customer.customer_code)
    customer_link.short_description = 'Customer'

    actions = ['issue_invoices', 'mark_as_sent', 'mark_as_paid']

    def issue_invoices(self, request, queryset):
        for invoice in queryset:
            if invoice.status == 'DRAFT':
                invoice.issue_invoice(request.user)
        self.message_user(request, f"Issued {queryset.count()} invoices.")
    issue_invoices.short_description = "Issue selected invoices"

    def mark_as_sent(self, request, queryset):
        for invoice in queryset:
            invoice.mark_as_sent()
        self.message_user(request, f"Marked {queryset.count()} invoices as sent.")
    mark_as_sent.short_description = "Mark as sent"

    def mark_as_paid(self, request, queryset):
        for invoice in queryset:
            invoice.status = 'PAID'
            invoice.amount_paid = invoice.total_amount
            invoice.balance = 0
            invoice.paid_at = timezone.now()
            invoice.paid_by = request.user
            invoice.save()
        self.message_user(request, f"Marked {queryset.count()} invoices as paid.")
    mark_as_paid.short_description = "Mark as paid"

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)

    def has_delete_permission(self, request, obj=None):
        if obj and not request.user.is_superuser:
            return False
        return super().has_delete_permission(request, obj)


# Payment Models Admin

@admin.register(InvoiceItemPayment)
class InvoiceItemPaymentAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'method_type', 'is_active', 'minimum_amount', 'maximum_amount')
    list_filter = ('method_type', 'is_active')
    search_fields = ('name', 'code', 'description')
    readonly_fields = ('created_at', 'updated_at', 'last_used')
    fieldsets = (
        ('Basic Information', {
            'fields': ('name', 'code', 'method_type', 'description')
        }),
        ('Configuration', {
            'fields': ('is_active', 'requires_confirmation', 'confirmation_timeout')
        }),
        ('Fees', {
            'fields': ('transaction_fee', 'fee_type')
        }),
        ('Limits', {
            'fields': ('minimum_amount', 'maximum_amount')
        }),
        ('Integration', {
            'fields': ('integration_class', 'config_json')
        }),
        ('Status', {
            'fields': ('status', 'last_used')
        }),
        ('Metadata', {
            'fields': ('created_by', 'updated_by', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)



@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ('payment_number', 'customer_link', 'amount', 'payment_method', 'status', 'payment_date', 'is_reconciled')
    list_filter = ('status', 'payment_method', 'is_reconciled', 'payment_date')
    search_fields = ('payment_number', 'customer__customer_code', 'transaction_id', 'mpesa_receipt', 'payer_phone')
    readonly_fields = ('net_amount', 'created_at', 'updated_at', 'processed_at', 'reconciled_at')
    fieldsets = (
        ('Basic Information', {
            'fields': ('payment_number', 'customer', 'invoice')
        }),
        ('Amount', {
            'fields': ('amount', 'transaction_fee', 'net_amount', 'currency')
        }),
        ('Payment Method', {
            'fields': ('payment_method', 'payment_reference', 'transaction_id')
        }),
        ('Status', {
            'fields': ('status', 'is_reconciled')
        }),
        ('Dates', {
            'fields': ('payment_date', 'processed_at', 'reconciled_at')
        }),
        ('Payer Information', {
            'fields': ('payer_name', 'payer_phone', 'payer_email', 'payer_id_number')
        }),
        ('Bank/Mobile Details', {
            'fields': ('bank_name', 'account_number', 'branch', 'cheque_number',
                       'mpesa_receipt', 'mpesa_phone', 'mpesa_name')
        }),
        ('Notes', {
            'fields': ('notes', 'failure_reason')
        }),
        ('Processors', {
            'fields': ('processed_by', 'reconciled_by'),
            'classes': ('collapse',)
        }),
        ('Metadata', {
            'fields': ('created_by', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )

    def customer_link(self, obj):
        url = reverse('admin:customers_customer_change', args=[obj.customer.id])
        return format_html('<a href="{}">{}</a>', url, obj.customer.customer_code)
    customer_link.short_description = 'Customer'

    actions = ['mark_as_completed', 'mark_as_failed', 'reconcile_payments']

    def mark_as_completed(self, request, queryset):
        for payment in queryset:
            payment.mark_as_completed(request.user)

    def mark_as_failed(self, request, queryset):
        for payment in queryset:
            payment.mark_as_failed("Manually marked as failed by admin")

    def reconcile_payments(self, request, queryset):
        for payment in queryset:
            if not payment.is_reconciled:
                payment.is_reconciled = True
                payment.reconciled_at = timezone.now()
                payment.reconciled_by = request.user
                payment.save()


@admin.register(Receipt)
class ReceiptAdmin(admin.ModelAdmin):
    list_display = ('receipt_number', 'customer_link', 'amount', 'payment_method', 'status', 'receipt_date')
    list_filter = ('status', 'receipt_date')
    search_fields = ('receipt_number', 'customer__customer_code', 'payment_reference')
    readonly_fields = ('qr_code', 'created_at', 'updated_at', 'issued_at')
    fieldsets = (
        ('Basic Information', {
            'fields': ('receipt_number', 'customer', 'payment')
        }),
        ('Amount', {
            'fields': ('amount', 'amount_in_words', 'currency')
        }),
        ('Payment Details', {
            'fields': ('payment_method', 'payment_reference')
        }),
        ('Status', {
            'fields': ('status',)
        }),
        ('Dates', {
            'fields': ('receipt_date', 'issued_at')
        }),
        ('Issuer', {
            'fields': ('issued_by',)
        }),
        ('Notes', {
            'fields': ('notes',)
        }),
        ('Digital Features', {
            'fields': ('digital_signature', 'qr_code'),
            'classes': ('collapse',)
        }),
        ('Metadata', {
            'fields': ('created_by', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        })
    )

    def customer_link(self, obj):
        url = reverse('admin:customers_customer_change', args=[obj.customer.id])
        return format_html('<a href="{}">{}</a>', url, obj.customer.customer_code)
    customer_link.short_description = 'Customer'


# class VoucherInline(admin.TabularInline):
#     model = Voucher
#     extra = 0
#     readonly_fields = ('status', 'remaining_value', 'use_count')
#     can_delete = False


# @admin.register(VoucherBatch)
# class VoucherBatchAdmin(admin.ModelAdmin):
#     pass


# class VoucherUsageInline(admin.TabularInline):
#     model = VoucherUsage
#     extra = 0
#     readonly_fields = ('amount', 'remaining_balance', 'created_at')
#     can_delete = False


# @admin.register(Voucher)
# class VoucherAdmin(admin.ModelAdmin):
#     pass


# @admin.register(VoucherUsage)
# class VoucherUsageAdmin(admin.ModelAdmin):
#     pass
