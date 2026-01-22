from django.contrib import admin
from .models import (
    NetilyPlan,
    CompanySubscription,
    SubscriptionPayment,
    ISPPayoutConfig,
    ISPSettlement,
    CommissionLedger,
)


@admin.register(NetilyPlan)
class NetilyPlanAdmin(admin.ModelAdmin):
    list_display = ['name', 'code', 'price_monthly', 'price_yearly', 'max_subscribers', 'is_active']
    list_filter = ['is_active', 'is_popular']
    search_fields = ['name', 'code']
    ordering = ['price_monthly']


@admin.register(CompanySubscription)
class CompanySubscriptionAdmin(admin.ModelAdmin):
    list_display = ['company', 'plan', 'status', 'billing_period', 'current_period_end', 'is_active']
    list_filter = ['status', 'billing_period', 'plan']
    search_fields = ['company__name']
    raw_id_fields = ['company', 'plan']
    date_hierarchy = 'created_at'


@admin.register(SubscriptionPayment)
class SubscriptionPaymentAdmin(admin.ModelAdmin):
    list_display = ['subscription', 'amount', 'status', 'payment_method', 'mpesa_receipt', 'created_at']
    list_filter = ['status', 'payment_method']
    search_fields = ['subscription__company__name', 'mpesa_receipt', 'payhero_reference']
    raw_id_fields = ['subscription']
    date_hierarchy = 'created_at'
    readonly_fields = ['payhero_checkout_id', 'mpesa_receipt', 'completed_at']


@admin.register(ISPPayoutConfig)
class ISPPayoutConfigAdmin(admin.ModelAdmin):
    list_display = ['company', 'payout_method', 'is_verified', 'settlement_frequency', 'minimum_payout']
    list_filter = ['payout_method', 'is_verified', 'settlement_frequency']
    search_fields = ['company__name', 'mpesa_phone', 'bank_account_number']
    raw_id_fields = ['company']


@admin.register(ISPSettlement)
class ISPSettlementAdmin(admin.ModelAdmin):
    list_display = ['company', 'period_start', 'period_end', 'gross_amount', 'commission_amount', 'net_amount', 'status']
    list_filter = ['status', 'payout_method']
    search_fields = ['company__name', 'payout_reference']
    raw_id_fields = ['company']
    date_hierarchy = 'created_at'
    readonly_fields = ['payout_reference', 'processed_at']


@admin.register(CommissionLedger)
class CommissionLedgerAdmin(admin.ModelAdmin):
    list_display = ['company', 'payment_type', 'gross_amount', 'commission_amount', 'created_at']
    list_filter = ['payment_type', 'is_settled']
    search_fields = ['company__name', 'payment_reference']
    raw_id_fields = ['company', 'settlement']
    date_hierarchy = 'created_at'
