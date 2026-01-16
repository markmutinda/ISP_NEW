"""
Admin configuration for core app
"""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from .models import User, Company, Tenant, SystemSettings, AuditLog, GlobalSystemSettings


class UserCreationForm(UserCreationForm):
    """Custom user creation form for admin"""
    
    class Meta:
        model = User
        fields = ('email', 'company', 'tenant')  # ADDED company and tenant


class UserChangeForm(UserChangeForm):
    """Custom user change form for admin"""
    
    class Meta:
        model = User
        fields = '__all__'


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    """Admin configuration for User model"""
    
    form = UserChangeForm
    add_form = UserCreationForm
    
    list_display = ('email', 'first_name', 'last_name', 'company', 'phone_number', 
                   'role', 'is_active', 'is_verified', 'is_staff', 'last_login')
    list_filter = ('company', 'role', 'is_active', 'is_verified', 'is_staff', 'is_superuser')  # ADDED company
    search_fields = ('email', 'first_name', 'last_name', 'phone_number', 'id_number', 'company__name')
    ordering = ('-created_at',)
    readonly_fields = ('created_at', 'updated_at', 'last_login')
    
    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        (_('Personal Info'), {
            'fields': ('first_name', 'last_name', 'phone_number', 'id_number',
                      'gender', 'date_of_birth', 'profile_picture')
        }),
        (_('Company & Tenant'), {  # NEW SECTION
            'fields': ('company', 'tenant'),
            'classes': ('collapse', 'wide')
        }),
        (_('Permissions'), {
            'fields': ('role', 'is_active', 'is_verified', 'is_staff', 'is_superuser',
                      'groups', 'user_permissions')
        }),
        (_('Important Dates'), {
            'fields': ('last_login', 'created_at', 'updated_at')
        }),
    )
    
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('email', 'password1', 'password2', 'first_name', 'last_name',
                      'phone_number', 'role', 'company', 'tenant'),  # ADDED company and tenant
        }),
    )
    
    def get_queryset(self, request):
        """Filter users by company for non-superusers"""
        qs = super().get_queryset(request)
        
        if request.user.is_superuser:
            return qs
        
        # Non-superusers can only see users from their own company
        if hasattr(request.user, 'company') and request.user.company:
            return qs.filter(company=request.user.company)
        
        # If user has no company, show only themselves
        return qs.filter(id=request.user.id)
    
    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """Limit company/tenant choices based on user permissions"""
        if db_field.name == "company":
            if not request.user.is_superuser:
                # Non-superusers can only assign users to their own company
                if hasattr(request.user, 'company') and request.user.company:
                    kwargs["queryset"] = Company.objects.filter(id=request.user.company.id)
                else:
                    kwargs["queryset"] = Company.objects.none()
        
        if db_field.name == "tenant":
            if not request.user.is_superuser:
                # Non-superusers can only assign tenants from their company
                if hasattr(request.user, 'company') and request.user.company:
                    kwargs["queryset"] = Tenant.objects.filter(company=request.user.company)
                else:
                    kwargs["queryset"] = Tenant.objects.none()
        
        return super().formfield_for_foreignkey(db_field, request, **kwargs)
    
    def has_delete_permission(self, request, obj=None):
        """Prevent non-superusers from deleting superusers"""
        if obj and obj.is_superuser and not request.user.is_superuser:
            return False
        return super().has_delete_permission(request, obj)
    
    def save_model(self, request, obj, form, change):
        """Set created_by when creating new users and auto-assign company if needed"""
        if not change:
            obj.created_by = request.user
            
            # Auto-assign company if not set and user has a company
            if not obj.company and hasattr(request.user, 'company') and request.user.company:
                obj.company = request.user.company
                
            # Auto-assign tenant if not set and company has a tenant
            if not obj.tenant and obj.company and hasattr(obj.company, 'tenant'):
                obj.tenant = obj.company.tenant
        
        super().save_model(request, obj, form, change)


from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from .models import Company  # Make sure this import is present


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    """Admin configuration for Company model"""
    
    list_display = (
        'name', 
        'company_type', 
        'email', 
        'phone_number', 
        'city', 
        'is_active', 
        'created_at', 
        'total_customers'
    )
    list_filter = ('company_type', 'is_active', 'county', 'city')
    search_fields = ('name', 'email', 'phone_number', 'registration_number', 'tax_pin')
    readonly_fields = ('created_at', 'updated_at', 'total_customers')  # slug is editable here
    prepopulated_fields = {'slug': ('name',)}  # this will now work
    
    fieldsets = (
        (_('Company Info'), {
            'fields': ('name', 'slug', 'company_type', 'logo')  # ← slug explicitly here
        }),
        (_('Contact Info'), {
            'fields': ('email', 'phone_number', 'address', 'city', 
                       'county', 'postal_code', 'website')
        }),
        (_('Business Info'), {
            'fields': ('registration_number', 'tax_pin')
        }),
        (_('Settings'), {
            'fields': ('is_active', 'subscription_plan', 'subscription_expiry')
        }),
        (_('Statistics'), {
            'fields': ('total_customers',),
            'classes': ('collapse',)
        }),
        (_('Metadata'), {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def get_queryset(self, request):
        """Filter companies for non-superusers — only show their own company"""
        qs = super().get_queryset(request)
        
        if request.user.is_superuser:
            return qs
        
        if hasattr(request.user, 'company') and request.user.company:
            return qs.filter(id=request.user.company.id)
        
        return qs.none()
    
    def total_customers(self, obj):
        """Display total customers count"""
        return obj.customers.count()
    total_customers.short_description = 'Total Customers'
    
    def has_delete_permission(self, request, obj=None):
        """Prevent non-superusers from deleting companies"""
        if obj and not request.user.is_superuser:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    """Admin configuration for Tenant model"""
    
    list_display = ('subdomain', 'company', 'status', 'is_active', 
                   'billing_cycle', 'next_billing_date', 'created_at', 'total_users')
    list_filter = ('status', 'is_active', 'billing_cycle')
    search_fields = ('subdomain', 'domain', 'company__name', 'database_name')
    readonly_fields = ('created_at', 'updated_at', 'total_users')
    autocomplete_fields = ['company']
    
    fieldsets = (
        (_('Tenant Info'), {
            'fields': ('company', 'subdomain', 'domain', 'database_name')
        }),
        (_('Status'), {
            'fields': ('is_active', 'status')
        }),
        (_('Subscription'), {
            'fields': ('max_users', 'max_customers', 'features', 
                      'billing_cycle', 'monthly_rate', 'next_billing_date')
        }),
        (_('Statistics'), {  # NEW SECTION
            'fields': ('total_users',),
            'classes': ('collapse',)
        }),
        (_('Metadata'), {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def get_queryset(self, request):
        """Filter tenants for non-superusers"""
        qs = super().get_queryset(request)
        
        if request.user.is_superuser:
            return qs
        
        # Non-superusers can only see tenant from their company
        if hasattr(request.user, 'company') and request.user.company:
            return qs.filter(company=request.user.company)
        
        # If user has no company but has a tenant, show only that tenant
        if hasattr(request.user, 'tenant') and request.user.tenant:
            return qs.filter(id=request.user.tenant.id)
        
        return qs.none()
    
    def total_users(self, obj):
        """Display total users in this tenant"""
        return obj.users.count()
    total_users.short_description = 'Total Users'
    
    def get_readonly_fields(self, request, obj=None):
        """Make database_name read-only after creation"""
        readonly_fields = list(self.readonly_fields)
        if obj:
            readonly_fields.append('database_name')
            readonly_fields.append('company')  # Also make company read-only after creation
        return readonly_fields
    
    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """Limit company choices based on user permissions"""
        if db_field.name == "company":
            if not request.user.is_superuser:
                # Non-superusers can only create tenants for their own company
                if hasattr(request.user, 'company') and request.user.company:
                    kwargs["queryset"] = Company.objects.filter(id=request.user.company.id)
                else:
                    kwargs["queryset"] = Company.objects.none()
        
        return super().formfield_for_foreignkey(db_field, request, **kwargs)
    
    def has_delete_permission(self, request, obj=None):
        """Prevent non-superusers from deleting tenants"""
        if obj and not request.user.is_superuser:
            return False
        return super().has_delete_permission(request, obj)


@admin.register(SystemSettings)
class SystemSettingsAdmin(admin.ModelAdmin):
    """Admin configuration for SystemSettings model"""
    
    list_display = ('key', 'name', 'setting_type', 'data_type', 
                   'is_public', 'updated_at')
    list_filter = ('setting_type', 'data_type', 'is_public')
    search_fields = ('key', 'name', 'description')
    readonly_fields = ('created_at', 'updated_at')
    
    fieldsets = (
        (_('Setting Info'), {
            'fields': ('key', 'name', 'description')
        }),
        (_('Value'), {
            'fields': ('value', 'data_type')
        }),
        (_('Configuration'), {
            'fields': ('setting_type', 'is_public')
        }),
        (_('Metadata'), {
            'fields': ('updated_by', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def get_queryset(self, request):
        """Filter system settings for non-superusers"""
        qs = super().get_queryset(request)
        
        if request.user.is_superuser:
            return qs
        
        # Non-superusers can only see public settings
        return qs.filter(is_public=True)
    
    def save_model(self, request, obj, form, change):
        """Set updated_by when saving settings"""
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)
    
    def get_form(self, request, obj=None, **kwargs):
        """Add help text for data type field"""
        form = super().get_form(request, obj, **kwargs)
        form.base_fields['data_type'].help_text = (
            'Select the data type of the value. Changing this may affect how '
            'the value is processed.'
        )
        return form
    
    def has_add_permission(self, request):
        """Only superusers can add system settings"""
        return request.user.is_superuser
    
    def has_change_permission(self, request, obj=None):
        """Only superusers can change system settings"""
        return request.user.is_superuser
    
    def has_delete_permission(self, request, obj=None):
        """Only superusers can delete system settings"""
        return request.user.is_superuser


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Admin configuration for AuditLog model"""
    
    list_display = ('action', 'model_name', 'object_repr', 'user', 'company',
                   'ip_address', 'timestamp')
    list_filter = ('action', 'model_name', 'timestamp', 'tenant__company')  # ADDED company filter
    search_fields = ('user__email', 'object_repr', 'ip_address', 'user_agent', 'tenant__company__name')
    readonly_fields = ('timestamp', 'changes_formatted')
    date_hierarchy = 'timestamp'
    
    fieldsets = (
        (_('Action Info'), {
            'fields': ('user', 'action', 'model_name', 'object_id', 'object_repr')
        }),
        (_('Technical Details'), {
            'fields': ('ip_address', 'user_agent', 'tenant')
        }),
        (_('Changes'), {
            'fields': ('changes_formatted',)
        }),
        (_('Timestamp'), {
            'fields': ('timestamp',)
        }),
    )
    
    def get_queryset(self, request):
        """Filter audit logs for non-superusers"""
        qs = super().get_queryset(request)
        
        if request.user.is_superuser:
            return qs
        
        # Non-superusers can only see audit logs from their company/tenant
        if hasattr(request.user, 'company') and request.user.company:
            # Filter by company through tenant
            return qs.filter(tenant__company=request.user.company)
        
        if hasattr(request.user, 'tenant') and request.user.tenant:
            # Filter by tenant
            return qs.filter(tenant=request.user.tenant)
        
        # Show only user's own actions
        return qs.filter(user=request.user)
    
    def company(self, obj):
        """Display company name from tenant"""
        if obj.tenant and obj.tenant.company:
            return obj.tenant.company.name
        return "-"
    company.short_description = 'Company'
    
    def changes_formatted(self, obj):
        """Format JSON changes for display"""
        if obj.changes:
            import json
            try:
                return json.dumps(obj.changes, indent=2, ensure_ascii=False)
            except:
                return str(obj.changes)
        return '-'
    
    changes_formatted.short_description = 'Changes (Formatted)'
    
    def has_add_permission(self, request):
        """Prevent manual creation of audit logs"""
        return False
    
    def has_change_permission(self, request, obj=None):
        """Prevent editing of audit logs"""
        return False
    
    def has_delete_permission(self, request, obj=None):
        """Only allow superusers to delete audit logs"""
        return request.user.is_superuser


@admin.register(GlobalSystemSettings)
class GlobalSystemSettingsAdmin(admin.ModelAdmin):
    """Admin configuration for GlobalSystemSettings model"""
    
    def has_add_permission(self, request):
        """Only one settings instance should exist"""
        return False if GlobalSystemSettings.objects.count() > 0 else super().has_add_permission(request)
    
    def has_delete_permission(self, request, obj=None):
        """Prevent deletion of global settings"""
        return False


# Custom admin site configuration
class ISPMgmtAdminSite(admin.AdminSite):
    """Custom admin site for ISP Management"""
    
    site_header = 'ISP Management System Administration'
    site_title = 'ISP Management Admin'
    index_title = 'Welcome to ISP Management Administration'
    
    def get_app_list(self, request):
        """Customize app list ordering"""
        app_list = super().get_app_list(request)
        
        # Reorder apps
        app_ordering = {
            'core': 1,
            'customers': 2,
            'network': 3,
            'billing': 4,
            'support': 5,
            'analytics': 6,
            'staff': 7,
            'self_service': 8,
            'inventory': 9,
            'notifications': 10,
            'bandwidth': 11,
            'auth': 100,
        }
        
        app_list.sort(key=lambda x: app_ordering.get(x['app_label'], 99))
        return app_list
    
    def each_context(self, request):
        """Add company context to admin template"""
        context = super().each_context(request)
        
        # Add company info to context for template use
        if hasattr(request.user, 'company') and request.user.company:
            context['user_company'] = request.user.company
        
        return context


# Register custom admin site
admin_site = ISPMgmtAdminSite(name='isp_admin')

# Re-register models with custom admin site
admin_site.register(User, UserAdmin)
admin_site.register(Company, CompanyAdmin)
admin_site.register(Tenant, TenantAdmin)
admin_site.register(SystemSettings, SystemSettingsAdmin)
admin_site.register(AuditLog, AuditLogAdmin)
admin_site.register(GlobalSystemSettings, GlobalSystemSettingsAdmin)

