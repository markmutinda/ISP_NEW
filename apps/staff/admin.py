from django.contrib import admin
from django.utils.html import format_html
from .models import (
    Department, Employee, Attendance, LeaveRequest,
    PerformanceReview, Payroll
)


@admin.register(Department)
class DepartmentAdmin(admin.ModelAdmin):
    list_display = ['name', 'manager', 'employee_count', 'budget', 'is_active']
    list_filter = ['is_active']
    search_fields = ['name', 'description']
    list_editable = ['is_active']
    
    def employee_count(self, obj):
        return obj.employee_count
    employee_count.short_description = 'Employees'


@admin.register(Employee)
class EmployeeAdmin(admin.ModelAdmin):
    list_display = [
        'employee_id', 'user', 'department', 'position',
        'employment_type', 'status', 'hire_date', 'is_active'
    ]
    list_filter = [
        'status', 'employment_type', 'department',
        'hire_date', 'is_active'
    ]
    search_fields = [
        'user__first_name', 'user__last_name', 'employee_id',
        'position', 'phone', 'email'
    ]
    readonly_fields = ['employee_id']  # removed created_at, updated_at
    fieldsets = (
        ('Basic Information', {
            'fields': ('user', 'employee_id', 'department', 'position')
        }),
        ('Employment Details', {
            'fields': (
                'employment_type', 'status', 'hire_date',
                'contract_start_date', 'contract_end_date',
                'termination_date'
            )
        }),
        ('Contact Information', {
            'fields': (
                'phone', 'alternate_phone', 'personal_email',
                'address', 'city', 'county', 'postal_code'
            )
        }),
        ('Emergency Contact', {
            'fields': (
                'emergency_contact_name', 'emergency_contact_phone',
                'emergency_contact_relationship'
            )
        }),
        ('Financial Information', {
            'fields': ('salary', 'bank_name', 'bank_account', 'bank_branch'),
            'classes': ('collapse',)
        }),
        ('Next of Kin', {
            'fields': (
                'next_of_kin_name', 'next_of_kin_phone',
                'next_of_kin_relationship', 'next_of_kin_id_number'
            ),
            'classes': ('collapse',)
        }),
        ('Documentation', {
            'fields': ('id_number', 'kra_pin', 'nssf_number', 'nhif_number'),
            'classes': ('collapse',)
        }),
        ('Personal Details', {
            'fields': ('date_of_birth', 'gender', 'blood_group'),
            'classes': ('collapse',)
        }),
        ('System Access', {
            'fields': ('has_system_access', 'access_level')
        }),
        ('Additional Information', {
            'fields': ('qualifications', 'skills', 'notes', 'is_active'),
            'classes': ('collapse',)
        })
    )


@admin.register(Attendance)
class AttendanceAdmin(admin.ModelAdmin):
    list_display = [
        'employee', 'date', 'check_in', 'check_out',
        'status', 'hours_worked', 'is_approved'
    ]
    list_filter = ['status', 'date', 'employee__department']
    search_fields = ['employee__user__first_name', 'employee__user__last_name']
    readonly_fields = ['hours_worked']  # removed created_at, updated_at
    
    def is_approved(self, obj):
        if obj.approved_by:
            return format_html('<span style="color: green;">✓ Approved</span>')
        return format_html('<span style="color: orange;">⏳ Pending</span>')
    is_approved.short_description = 'Approval Status'


@admin.register(LeaveRequest)
class LeaveRequestAdmin(admin.ModelAdmin):
    list_display = [
        'employee', 'leave_type', 'start_date', 'end_date',
        'total_days', 'status', 'approved_by', 'is_approved'
    ]
    list_filter = ['status', 'leave_type', 'start_date']
    search_fields = [
        'employee__user__first_name', 'employee__user__last_name',
        'reason'
    ]
    readonly_fields = ['total_days']  # removed created_at, updated_at
    
    def is_approved(self, obj):
        if obj.status == 'approved':
            return format_html('<span style="color: green;">✓ Approved</span>')
        elif obj.status == 'rejected':
            return format_html('<span style="color: red;">✗ Rejected</span>')
        return format_html('<span style="color: orange;">⏳ Pending</span>')
    is_approved.short_description = 'Approval Status'


@admin.register(PerformanceReview)
class PerformanceReviewAdmin(admin.ModelAdmin):
    list_display = [
        'employee', 'review_date', 'reviewed_by',
        'overall_rating', 'employee_acknowledged'
    ]
    list_filter = ['review_date', 'employee__department']
    search_fields = [
        'employee__user__first_name', 'employee__user__last_name',
        'reviewed_by__first_name', 'reviewed_by__last_name'
    ]
    readonly_fields = ['overall_rating']  # removed created_at, updated_at


@admin.register(Payroll)
class PayrollAdmin(admin.ModelAdmin):
    list_display = [
        'employee', 'pay_period_start', 'pay_period_end',
        'payment_date', 'net_pay', 'is_paid'
    ]
    list_filter = ['is_paid', 'payment_date', 'employee__department']
    search_fields = ['employee__user__first_name', 'employee__user__last_name']
    readonly_fields = ['gross_pay', 'total_deductions', 'net_pay']  # removed created_at, updated_at
