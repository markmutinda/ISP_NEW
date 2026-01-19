from rest_framework import serializers
from django.contrib.auth import get_user_model
from .models import (
    Department, Employee, Attendance, LeaveRequest,
    PerformanceReview, Payroll
)

User = get_user_model()


class DepartmentSerializer(serializers.ModelSerializer):
    manager_name = serializers.CharField(
        source='manager.get_full_name',
        read_only=True
    )
    employee_count = serializers.IntegerField(read_only=True)
    subdepartment_count = serializers.IntegerField(read_only=True)
    
    class Meta:
        model = Department
        fields = [
            'id', 'name', 'description', 'parent_department',
            'manager', 'manager_name', 'budget', 'location',
            'is_active', 'employee_count', 'subdepartment_count',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at']


class EmployeeSerializer(serializers.ModelSerializer):
    user_details = serializers.SerializerMethodField()
    department_name = serializers.CharField(
        source='department.name',
        read_only=True
    )
    service_years = serializers.IntegerField(read_only=True)
    is_on_leave = serializers.BooleanField(read_only=True)
    
    class Meta:
        model = Employee
        fields = [
            'id', 'user', 'user_details', 'employee_id',
            'department', 'department_name', 'position',
            'employment_type', 'status', 'hire_date',
            'contract_start_date', 'contract_end_date',
            'termination_date', 'phone', 'alternate_phone',
            'personal_email', 'address', 'city', 'county',
            'postal_code', 'emergency_contact_name',
            'emergency_contact_phone', 'emergency_contact_relationship',
            'salary', 'bank_name', 'bank_account', 'bank_branch',
            'next_of_kin_name', 'next_of_kin_phone',
            'next_of_kin_relationship', 'next_of_kin_id_number',
            'id_number', 'kra_pin', 'nssf_number', 'nhif_number',
            'date_of_birth', 'gender', 'blood_group',
            'has_system_access', 'access_level', 'qualifications',
            'skills', 'notes', 'is_active', 'service_years',
            'is_on_leave', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'created_at', 'updated_at', 'employee_id',
            'service_years'
        ]
    
    def get_user_details(self, obj):
        if obj.user:
            return {
                'id': obj.user.id,
                'first_name': obj.user.first_name,
                'last_name': obj.user.last_name,
                'email': obj.user.email,
                'username': obj.user.username,
                'is_active': obj.user.is_active
            }
        return None
    
    def validate(self, data):
        # Validate contract dates
        contract_start = data.get('contract_start_date')
        contract_end = data.get('contract_end_date')
        
        if contract_start and contract_end and contract_start > contract_end:
            raise serializers.ValidationError(
                "Contract start date must be before end date"
            )
        
        # Validate hire date vs contract dates
        hire_date = data.get('hire_date')
        if hire_date and contract_start and hire_date > contract_start:
            raise serializers.ValidationError(
                "Hire date cannot be after contract start date"
            )
        
        return data
    
    def create(self, validated_data):
        # Generate employee ID if not provided
        if 'employee_id' not in validated_data or not validated_data['employee_id']:
            department = validated_data.get('department')
            if department:
                department_code = department.name[:3].upper()
            else:
                department_code = 'EMP'
            
            from django.utils import timezone
            year = timezone.now().strftime('%y')
            
            count = Employee.objects.filter(
                department=department,
                hire_date__year=timezone.now().year
            ).count() + 1
            
            validated_data['employee_id'] = f"{department_code}{year}{count:03d}"
        
        return super().create(validated_data)


class AttendanceSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(
        source='employee.get_full_name',
        read_only=True
    )
    employee_id = serializers.CharField(
        source='employee.employee_id',
        read_only=True
    )
    approved_by_name = serializers.CharField(
        source='approved_by.get_full_name',
        read_only=True
    )
    total_hours = serializers.DecimalField(
        max_digits=4, 
        decimal_places=2,
        read_only=True
    )
    is_approved = serializers.BooleanField(read_only=True)
    
    class Meta:
        model = Attendance
        fields = [
            'id', 'employee', 'employee_name', 'employee_id',
            'date', 'check_in', 'check_out', 'status',
            'hours_worked', 'leave_type', 'leave_reason',
            'overtime_hours', 'notes', 'approved_by',
            'approved_by_name', 'total_hours', 'is_approved',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['created_at', 'updated_at', 'hours_worked']
    
    def validate(self, data):
        date = data.get('date') or self.instance.date if self.instance else None
        
        # Check if attendance already exists for this employee on this date
        if date:
            employee = data.get('employee') or self.instance.employee if self.instance else None
            
            if employee and self.instance is None:  # Only for new records
                existing = Attendance.objects.filter(
                    employee=employee,
                    date=date
                ).exists()
                
                if existing:
                    raise serializers.ValidationError(
                        f"Attendance already recorded for {employee} on {date}"
                    )
        
        # Validate check-in/check-out times
        check_in = data.get('check_in')
        check_out = data.get('check_out')
        
        if check_in and check_out and check_in >= check_out:
            raise serializers.ValidationError(
                "Check-in time must be before check-out time"
            )
        
        return data


class LeaveRequestSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(
        source='employee.get_full_name',
        read_only=True
    )
    employee_id = serializers.CharField(
        source='employee.employee_id',
        read_only=True
    )
    department = serializers.CharField(
        source='employee.department.name',
        read_only=True
    )
    approved_by_name = serializers.CharField(
        source='approved_by.get_full_name',
        read_only=True
    )
    handover_to_name = serializers.CharField(
        source='handover_to.get_full_name',
        read_only=True
    )
    is_approved = serializers.BooleanField(read_only=True)
    is_pending = serializers.BooleanField(read_only=True)
    
    class Meta:
        model = LeaveRequest
        fields = [
            'id', 'employee', 'employee_name', 'employee_id',
            'department', 'leave_type', 'start_date', 'end_date',
            'total_days', 'reason', 'emergency_contact',
            'emergency_phone', 'status', 'approved_by',
            'approved_by_name', 'approved_date', 'rejection_reason',
            'handover_to', 'handover_to_name', 'handover_notes',
            'is_approved', 'is_pending', 'created_at', 'updated_at'
        ]
        read_only_fields = [
            'created_at', 'updated_at', 'total_days',
            'approved_by', 'approved_date', 'rejection_reason'
        ]
    
    def validate(self, data):
        start_date = data.get('start_date')
        end_date = data.get('end_date')
        
        if start_date and end_date and start_date > end_date:
            raise serializers.ValidationError(
                "Start date must be before end date"
            )
        
        # Check for overlapping leave requests
        if start_date and end_date:
            employee = data.get('employee') or self.instance.employee if self.instance else None
            
            if employee:
                overlapping = LeaveRequest.objects.filter(
                    employee=employee,
                    status='approved',
                    start_date__lte=end_date,
                    end_date__gte=start_date
                )
                
                if self.instance:
                    overlapping = overlapping.exclude(id=self.instance.id)
                
                if overlapping.exists():
                    raise serializers.ValidationError(
                        "Overlapping approved leave exists for this period"
                    )
        
        return data


class PerformanceReviewSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(
        source='employee.get_full_name',
        read_only=True
    )
    employee_id = serializers.CharField(
        source='employee.employee_id',
        read_only=True
    )
    reviewed_by_name = serializers.CharField(
        source='reviewed_by.get_full_name',
        read_only=True
    )
    
    class Meta:
        model = PerformanceReview
        fields = [
            'id', 'employee', 'employee_name', 'employee_id',
            'review_date', 'review_period_start', 'review_period_end',
            'reviewed_by', 'reviewed_by_name', 'quality_of_work',
            'productivity', 'technical_skills', 'communication',
            'teamwork', 'initiative', 'attendance_punctuality',
            'overall_rating', 'strengths', 'areas_for_improvement',
            'goals', 'employee_comments', 'reviewer_comments',
            'employee_acknowledged', 'employee_acknowledged_date',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'created_at', 'updated_at', 'overall_rating',
            'employee_acknowledged_date'
        ]
    
    def validate(self, data):
        review_period_start = data.get('review_period_start')
        review_period_end = data.get('review_period_end')
        
        if review_period_start and review_period_end:
            if review_period_start > review_period_end:
                raise serializers.ValidationError(
                    "Review period start must be before end"
                )
            
            # Check if review period overlaps with existing review
            employee = data.get('employee') or self.instance.employee if self.instance else None
            
            if employee:
                overlapping = PerformanceReview.objects.filter(
                    employee=employee,
                    review_period_start__lte=review_period_end,
                    review_period_end__gte=review_period_start
                )
                
                if self.instance:
                    overlapping = overlapping.exclude(id=self.instance.id)
                
                if overlapping.exists():
                    raise serializers.ValidationError(
                        "Overlapping performance review exists for this period"
                    )
        
        return data


class PayrollSerializer(serializers.ModelSerializer):
    employee_name = serializers.CharField(
        source='employee.get_full_name',
        read_only=True
    )
    employee_id = serializers.CharField(
        source='employee.employee_id',
        read_only=True
    )
    
    class Meta:
        model = Payroll
        fields = [
            'id', 'employee', 'employee_name', 'employee_id',
            'pay_period_start', 'pay_period_end', 'payment_date',
            'basic_salary', 'overtime_pay', 'allowances', 'bonuses',
            'paye', 'nssf', 'nhif', 'other_deductions',
            'gross_pay', 'total_deductions', 'net_pay',
            'payment_method', 'bank_name', 'bank_account',
            'is_paid', 'paid_date', 'notes',
            'created_at', 'updated_at'
        ]
        read_only_fields = [
            'created_at', 'updated_at', 'gross_pay',
            'total_deductions', 'net_pay'
        ]
    
    def validate(self, data):
        pay_period_start = data.get('pay_period_start')
        pay_period_end = data.get('pay_period_end')
        
        if pay_period_start and pay_period_end:
            if pay_period_start > pay_period_end:
                raise serializers.ValidationError(
                    "Pay period start must be before end"
                )
            
            # Check for duplicate payroll for same period
            employee = data.get('employee') or self.instance.employee if self.instance else None
            
            if employee:
                duplicate = Payroll.objects.filter(
                    employee=employee,
                    pay_period_start=pay_period_start,
                    pay_period_end=pay_period_end
                )
                
                if self.instance:
                    duplicate = duplicate.exclude(id=self.instance.id)
                
                if duplicate.exists():
                    raise serializers.ValidationError(
                        "Payroll already exists for this period"
                    )
        
        return data


class DepartmentReportSerializer(serializers.Serializer):
    department = serializers.CharField()
    employee_count = serializers.IntegerField()
    active_count = serializers.IntegerField()
    on_leave_count = serializers.IntegerField()
    avg_service_years = serializers.FloatField()
    total_salary = serializers.DecimalField(max_digits=12, decimal_places=2)


class AttendanceReportSerializer(serializers.Serializer):
    employee = serializers.CharField()
    employee_id = serializers.CharField()
    department = serializers.CharField()
    total_days = serializers.IntegerField()
    present_days = serializers.IntegerField()
    absent_days = serializers.IntegerField()
    late_days = serializers.IntegerField()
    leave_days = serializers.IntegerField()
    attendance_rate = serializers.FloatField()


class PayrollSummarySerializer(serializers.Serializer):
    period = serializers.CharField()
    total_employees = serializers.IntegerField()
    total_gross_pay = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_deductions = serializers.DecimalField(max_digits=12, decimal_places=2)
    total_net_pay = serializers.DecimalField(max_digits=12, decimal_places=2)
    avg_salary = serializers.DecimalField(max_digits=10, decimal_places=2)
