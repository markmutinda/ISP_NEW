from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.html import format_html
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from apps.customers.models import (
    Customer, CustomerAddress, CustomerDocument, 
    NextOfKin, CustomerNotes, ServiceConnection
)
from apps.customers.forms import (
    CustomerForm, AddressForm, DocumentUploadForm,
    NextOfKinForm, ServiceConnectionForm
)


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    form = CustomerForm
    list_display = [
        'customer_code', 'full_name_display', 'phone_display',
        'customer_type', 'status', 'category', 'balance_display',
        'activation_date', 'created_at'
    ]
    list_filter = [
        'status', 'customer_type', 'category', 'gender',
        'activation_date', 'created_at'
    ]
    search_fields = [
        'customer_code', 'user__first_name', 'user__last_name',
        'user__email', 'user__phone_number', 'id_number'
    ]
    readonly_fields = [
        'customer_code', 'created_at', 'updated_at',
        'outstanding_balance', 'activation_date', 'deactivation_date'
    ]
    fieldsets = (
        ('Personal Information', {
            'fields': (
                'user', 'customer_code', 
                ('date_of_birth', 'gender'),
                ('id_type', 'id_number'),
                ('marital_status', 'occupation', 'employer'),
            )
        }),
        ('Contact Information', {
            'fields': (
                'phone_display', 'alternative_phone',
            )
        }),
        ('Customer Details', {
            'fields': (
                ('customer_type', 'status', 'category'),
                ('activation_date', 'deactivation_date'),
                'referral_source',
            )
        }),
        ('Billing Information', {
            'fields': (
                ('billing_cycle', 'credit_limit', 'outstanding_balance'),
            )
        }),
        ('Preferences', {
            'fields': (
                ('receive_sms', 'receive_email', 'receive_promotions'),
            )
        }),
        ('Notes & Internal', {
            'fields': (
                'notes',
                ('created_at', 'updated_at'),
            ),
            'classes': ('collapse',)
        }),
    )
    
    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related('user', 'company')
        if request.user.is_superuser:
            return qs
        if hasattr(request.user, 'company') and request.user.company:
            return qs.filter(company=request.user.company)
        return qs.none()
    
    def save_model(self, request, obj, form, change):
        if not change:
            obj.company = request.user.company
        super().save_model(request, obj, form, change)

    def full_name_display(self, obj):
        return obj.user.get_full_name()
    full_name_display.short_description = 'Name'
    full_name_display.admin_order_field = 'user__last_name'
    
    def phone_display(self, obj):
        return obj.user.phone_number
    phone_display.short_description = 'Phone'
    
    def balance_display(self, obj):
        color = 'red' if obj.outstanding_balance > 0 else 'green'
        return format_html(
            '<span style="color: {};">KES {:,}</span>',
            color,
            obj.outstanding_balance
        )
    balance_display.short_description = 'Balance'
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        qs = qs.select_related('user')
        return qs
    
    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        
        # Generate customer code if not exists
        if not obj.customer_code:
            from utils.helpers import generate_customer_code
            obj.customer_code = generate_customer_code(obj.company)
            obj.save()
    
    def view_on_site(self, obj):
        return reverse('admin:customer_dashboard', args=[obj.pk])
    
    actions = ['activate_customers', 'suspend_customers']
    
    @admin.action(description="Activate selected customers")
    def activate_customers(self, request, queryset):
        updated = queryset.update(status='ACTIVE')
        self.message_user(request, f'{updated} customers activated.')
    
    @admin.action(description="Suspend selected customers")
    def suspend_customers(self, request, queryset):
        updated = queryset.update(status='SUSPENDED')
        self.message_user(request, f'{updated} customers suspended.')


class CustomerAddressInline(admin.TabularInline):
    model = CustomerAddress
    form = AddressForm
    extra = 1
    fields = [
        'address_type', 'is_primary', 'street_address',
        'county', 'contact_person', 'contact_phone'
    ]


class CustomerDocumentInline(admin.TabularInline):
    model = CustomerDocument
    form = DocumentUploadForm
    extra = 1
    fields = [
        'document_type', 'title', 'document_file',
        'verified', 'expiry_date'
    ]
    readonly_fields = ['verified', 'verified_by', 'verified_at']


class NextOfKinInline(admin.StackedInline):
    model = NextOfKin
    form = NextOfKinForm
    extra = 1
    fields = [
        'full_name', 'relationship', 'phone_number', 'email',
        'id_type', 'id_number', 'address', 'county'
    ]


class CustomerNotesInline(admin.TabularInline):
    model = CustomerNotes
    extra = 1
    fields = ['note_type', 'note', 'priority', 'created_by']
    readonly_fields = ['created_by', 'created_at']
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        return qs.select_related('created_by')
    
    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


class ServiceConnectionInline(admin.TabularInline):
    model = ServiceConnection
    form = ServiceConnectionForm
    extra = 1
    fields = [
        'service_type', 'service_plan', 'status',
        'download_speed', 'upload_speed', 'monthly_price'
    ]


@admin.register(CustomerAddress)
class CustomerAddressAdmin(admin.ModelAdmin):
    form = AddressForm
    list_display = [
        'customer', 'address_type', 'is_primary',
        'street_address', 'county', 'contact_person'
    ]
    list_filter = ['address_type', 'is_primary', 'county']
    search_fields = [
        'customer__customer_code', 'customer__user__first_name',
        'customer__user__last_name', 'street_address', 'contact_person'
    ]
    raw_id_fields = ['customer']
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        qs = qs.select_related('customer', 'customer__user')
        return qs


@admin.register(CustomerDocument)
class CustomerDocumentAdmin(admin.ModelAdmin):
    form = DocumentUploadForm
    list_display = [
        'customer', 'document_type', 'title',
        'verified', 'expiry_date', 'created_at'
    ]
    list_filter = ['document_type', 'verified', 'is_expired']
    search_fields = [
        'customer__customer_code', 'customer__user__first_name',
        'customer__user__last_name', 'title'
    ]
    readonly_fields = [
        'file_size', 'mime_type', 'verified_by',
        'verified_at', 'is_expired'
    ]
    raw_id_fields = ['customer', 'verified_by']
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        qs = qs.select_related('customer', 'customer__user', 'verified_by')
        return qs
    
    def save_model(self, request, obj, form, change):
        if 'verified' in form.changed_data and obj.verified:
            obj.verified_by = request.user
            obj.verified_at = timezone.now()
        super().save_model(request, obj, form, change)


@admin.register(NextOfKin)
class NextOfKinAdmin(admin.ModelAdmin):
    form = NextOfKinForm
    list_display = [
        'customer', 'full_name', 'relationship',
        'phone_number', 'is_primary_contact'
    ]
    search_fields = [
        'customer__customer_code', 'full_name',
        'phone_number', 'id_number'
    ]
    raw_id_fields = ['customer']
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        qs = qs.select_related('customer', 'customer__user')
        return qs


@admin.register(CustomerNotes)
class CustomerNotesAdmin(admin.ModelAdmin):
    list_display = [
        'customer', 'note_type', 'priority',
        'requires_followup', 'created_by', 'created_at'
    ]
    list_filter = ['note_type', 'priority', 'requires_followup']
    search_fields = [
        'customer__customer_code', 'note',
        'created_by__email', 'created_by__first_name'
    ]
    readonly_fields = ['created_by', 'created_at']
    raw_id_fields = ['customer', 'created_by']
    
    def get_queryset(self, request):
        qs = super().get_queryset(request)
        qs = qs.select_related('customer', 'customer__user', 'created_by')
        return qs
    
    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(ServiceConnection)
class ServiceConnectionAdmin(admin.ModelAdmin):
    form = ServiceConnectionForm
    list_display = [
        'customer', 'service_type', 'plan',
        'status', 'download_speed', 'upload_speed',
        'monthly_price', 'activation_date'
    ]
    list_filter = [
        'service_type', 'status', 'connection_type',
        'activation_date'
    ]
    search_fields = [
        'customer__customer_code', 'customer__user__first_name',
        'customer__user__last_name', 'ip_address', 'mac_address'
    ]
    readonly_fields = [
        'activation_date', 'suspension_date',
        'termination_date', 'installed_by'
    ]
    raw_id_fields = ['customer', 'installation_address', 'installed_by']
    
    fieldsets = (
        ('Service Details', {
            'fields': (
                'customer', ('service_type', 'service_plan'),
                ('connection_type', 'status'),
            )
        }),
        ('Network Configuration', {
            'fields': (
                ('ip_address', 'mac_address', 'vlan_id'),
            )
        }),
        ('Equipment Details', {
            'fields': (
                ('router_model', 'router_serial'),
                ('ont_model', 'ont_serial'),
            )
        }),
        ('Bandwidth & QoS', {
            'fields': (
                ('download_speed', 'upload_speed'),
                'data_cap', 'qos_profile',
            )
        }),
        ('Installation', {
            'fields': (
                'installation_address', 'installation_notes',
                'installed_by',
            )
        }),
        ('Billing', {
            'fields': (
                ('monthly_price', 'setup_fee'),
                'prorated_billing',
            )
        }),
        ('Contract', {
            'fields': (
                ('auto_renew', 'contract_period'),
            )
        }),
        ('Dates', {
            'fields': (
                ('activation_date', 'suspension_date', 'termination_date'),
            ),
            'classes': ('collapse',)
        }),
    )
    
    def get_queryset(self, request):
        qs = super().get_queryset(request).select_related(
            'customer', 'customer__user', 'customer__company'
        )
        if request.user.is_superuser:
            return qs
        if hasattr(request.user, 'company') and request.user.company:
            return qs.filter(customer__company=request.user.company)
        return qs.none()
    
    def save_model(self, request, obj, form, change):
        if not change:
            obj.company = request.user.company
        super().save_model(request, obj, form, change)
    
    actions = ['activate_services', 'suspend_services']
    
    @admin.action(description="Activate selected services")
    def activate_services(self, request, queryset):
        from django.utils import timezone
        updated = queryset.filter(status__in=['PENDING', 'SUSPENDED'])
        for service in updated:
            service.activate_service(request.user)
        self.message_user(request, f'{updated.count()} services activated.')
    
    @admin.action(description="Suspend selected services")
    def suspend_services(self, request, queryset):
        updated = queryset.filter(status='ACTIVE')
        for service in updated:
            service.suspend_service("Admin action")
        self.message_user(request, f'{updated.count()} services suspended.')


# Customize admin site
admin.site.site_header = "ISP Management System"
admin.site.site_title = "ISP Management"
admin.site.index_title = "Customer Management"