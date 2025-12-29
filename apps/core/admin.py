"""
Admin configuration for core app
"""
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _
from django.contrib.auth.forms import UserChangeForm, UserCreationForm
from .models import User, Company, Tenant, SystemSettings, AuditLog


class UserCreationForm(UserCreationForm):
    """Custom user creation form for admin"""
    
    class Meta:
        model = User
        fields = ('email',)


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
    
    list_display = ('email', 'first_name', 'last_name', 'phone_number', 
                   'role', 'is_active', 'is_verified', 'is_staff', 'last_login')
    list_filter = ('role', 'is_active', 'is_verified', 'is_staff', 'is_superuser')
    search_fields = ('email', 'first_name', 'last_name', 'phone_number', 'id_number')
    ordering = ('-created_at',)
    readonly_fields = ('created_at', 'updated_at', 'last_login')
    
    fieldsets = (
        (None, {'fields': ('email', 'password')}),
        (_('Personal Info'), {
            'fields': ('first_name', 'last_name', 'phone_number', 'id_number',
                      'gender', 'date_of_birth', 'profile_picture')
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
                      'phone_number', 'role'),
        }),
    )
    
    def get_queryset(self, request):
        """Limit queryset for non-superusers"""
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        return qs.exclude(is_superuser=True)
    
    def has_delete_permission(self, request, obj=None):
        """Prevent non-superusers from deleting superusers"""
        if obj and obj.is_superuser and not request.user.is_superuser:
            return False
        return super().has_delete_permission(request, obj)
    
    def save_model(self, request, obj, form, change):
        """Set created_by when creating new users"""
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(Company)
class CompanyAdmin(admin.ModelAdmin):
    """Admin configuration for Company model"""
    
    list_display = ('name', 'company_type', 'email', 'phone_number', 
                   'city', 'is_active', 'created_at')
    list_filter = ('company_type', 'is_active', 'county', 'city')
    search_fields = ('name', 'email', 'phone_number', 'registration_number', 'tax_pin')
    readonly_fields = ('slug', 'created_at', 'updated_at')
    prepopulated_fields = {'slug': ('name',)}
    
    fieldsets = (
        (_('Company Info'), {
            'fields': ('name', 'slug', 'company_type', 'logo')
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
        (_('Metadata'), {
            'fields': ('created_by', 'created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def save_model(self, request, obj, form, change):
        """Set created_by when creating new companies"""
        if not change and not obj.created_by:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
    
    def get_queryset(self, request):
        """Filter companies for non-superusers"""
        qs = super().get_queryset(request)
        if request.user.is_superuser:
            return qs
        
        # Non-superusers can only see companies they created
        # or companies they belong to (if implemented)
        return qs.filter(created_by=request.user)


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    """Admin configuration for Tenant model"""
    
    list_display = ('subdomain', 'company', 'status', 'is_active', 
                   'billing_cycle', 'next_billing_date', 'created_at')
    list_filter = ('status', 'is_active', 'billing_cycle')
    search_fields = ('subdomain', 'domain', 'company__name', 'database_name')
    readonly_fields = ('created_at', 'updated_at')
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
        (_('Metadata'), {
            'fields': ('created_at', 'updated_at'),
            'classes': ('collapse',)
        }),
    )
    
    def get_readonly_fields(self, request, obj=None):
        """Make database_name read-only after creation"""
        if obj:
            return self.readonly_fields + ('database_name',)
        return self.readonly_fields


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


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Admin configuration for AuditLog model"""
    
    list_display = ('action', 'model_name', 'object_repr', 'user', 
                   'ip_address', 'timestamp')
    list_filter = ('action', 'model_name', 'timestamp')
    search_fields = ('user__email', 'object_repr', 'ip_address', 'user_agent')
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


# Register custom admin site
admin_site = ISPMgmtAdminSite(name='isp_admin')

# Re-register models with custom admin site
admin_site.register(User, UserAdmin)
admin_site.register(Company, CompanyAdmin)
admin_site.register(Tenant, TenantAdmin)
admin_site.register(SystemSettings, SystemSettingsAdmin)
admin_site.register(AuditLog, AuditLogAdmin)