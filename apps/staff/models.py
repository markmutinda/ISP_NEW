from django.db import models
from django.contrib.auth import get_user_model
from django.core.validators import MinValueValidator
from django.utils import timezone
from apps.core.models import BaseModel


User = get_user_model()


class Department(models.Model):
    """
    Staff departments
    """
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, null=True)
    parent_department = models.ForeignKey(
        'self', 
        on_delete=models.SET_NULL, 
        null=True, 
        blank=True,
        related_name='subdepartments'
    )
    manager = models.ForeignKey(
        'Employee',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='managed_departments'  # This prevents clash
    )
    budget = models.DecimalField(
        max_digits=12, 
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)]
    )
    location = models.CharField(max_length=255, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    

    class Meta:
        app_label = 'staff'
        ordering = ['name']
        verbose_name = "Department"
        verbose_name_plural = "Departments"
    
    def __str__(self):
        return self.name
    
    @property
    def employee_count(self):
        return self.employees.filter(is_active=True).count()


class Employee(models.Model):
    """
    Employee profiles linked to User accounts
    """
    EMPLOYMENT_TYPE_CHOICES = [
        ('permanent', 'Permanent'),
        ('contract', 'Contract'),
        ('probation', 'Probation'),
        ('temporary', 'Temporary'),
        ('intern', 'Intern'),
    ]
    
    STATUS_CHOICES = [
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('suspended', 'Suspended'),
        ('on_leave', 'On Leave'),
        ('terminated', 'Terminated'),
    ]
    
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name='employee_profile'
    )
    employee_id = models.CharField(max_length=50, unique=True)
    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        related_name='employees'
    )
    
    position = models.CharField(max_length=100)
    employment_type = models.CharField(
        max_length=20,
        choices=EMPLOYMENT_TYPE_CHOICES,
        default='permanent'
    )
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='active'
    )
    
    hire_date = models.DateField()
    contract_start_date = models.DateField(null=True, blank=True)
    contract_end_date = models.DateField(null=True, blank=True)
    termination_date = models.DateField(null=True, blank=True)
    
    # Contact Information
    phone = models.CharField(max_length=20)
    alternate_phone = models.CharField(max_length=20, blank=True, null=True)
    personal_email = models.EmailField(blank=True, null=True)
    address = models.TextField()
    city = models.CharField(max_length=100)
    county = models.CharField(max_length=100, blank=True, null=True)
    postal_code = models.CharField(max_length=20, blank=True, null=True)
    
    # Emergency Contact
    emergency_contact_name = models.CharField(max_length=100)
    emergency_contact_phone = models.CharField(max_length=20)
    emergency_contact_relationship = models.CharField(max_length=50)
    
    # Employment Details
    salary = models.DecimalField(
        max_digits=10, 
        decimal_places=2,
        validators=[MinValueValidator(0)],
        null=True,
        blank=True
    )
    bank_name = models.CharField(max_length=100, blank=True, null=True)
    bank_account = models.CharField(max_length=50, blank=True, null=True)
    bank_branch = models.CharField(max_length=100, blank=True, null=True)
    
    # Next of Kin
    next_of_kin_name = models.CharField(max_length=100, blank=True, null=True)
    next_of_kin_phone = models.CharField(max_length=20, blank=True, null=True)
    next_of_kin_relationship = models.CharField(max_length=50, blank=True, null=True)
    next_of_kin_id_number = models.CharField(max_length=20, blank=True, null=True)
    
    # Documentation
    id_number = models.CharField(max_length=20, blank=True, null=True)
    kra_pin = models.CharField(max_length=20, blank=True, null=True)
    nssf_number = models.CharField(max_length=20, blank=True, null=True)
    nhif_number = models.CharField(max_length=20, blank=True, null=True)
    
    # Physical Details
    date_of_birth = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=10, choices=[
        ('male', 'Male'),
        ('female', 'Female'),
        ('other', 'Other')
    ], blank=True, null=True)
    blood_group = models.CharField(max_length=5, blank=True, null=True)
    
    # System Access
    has_system_access = models.BooleanField(default=True)
    access_level = models.CharField(
        max_length=20,
        choices=[
            ('basic', 'Basic'),
            ('standard', 'Standard'),
            ('admin', 'Administrator'),
            ('super_admin', 'Super Administrator')
        ],
        default='basic'
    )
    
    # Additional Information
    qualifications = models.TextField(blank=True, null=True)
    skills = models.TextField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    
    # Status Flags
    is_active = models.BooleanField(default=True)
    

    class Meta:
        app_label = 'staff'
        ordering = ['employee_id']
        verbose_name = "Employee"
        verbose_name_plural = "Employees"
        indexes = [
            models.Index(fields=['employee_id']),
            models.Index(fields=['status']),
            models.Index(fields=['department']),
            models.Index(fields=['hire_date']),
        ]
    
    def __str__(self):
        return f"{self.user.get_full_name()} ({self.employee_id})"
    
    def save(self, *args, **kwargs):
        if not self.employee_id:
            # Generate employee ID
            department_code = self.department.name[:3].upper() if self.department else 'EMP'
            year = timezone.now().strftime('%y')
            count = Employee.objects.filter(
                department=self.department,
                hire_date__year=timezone.now().year
            ).count() + 1
            self.employee_id = f"{department_code}{year}{count:03d}"
        
        # Update user's staff status
        if self.user:
            self.user.is_staff = self.has_system_access
            self.user.save()
        
        super().save(*args, **kwargs)
    
    def get_full_name(self):
        return self.user.get_full_name()
    
    def get_email(self):
        return self.user.email
    
    def calculate_service_years(self):
        if self.hire_date:
            today = timezone.now().date()
            years = today.year - self.hire_date.year
            if (today.month, today.day) < (self.hire_date.month, self.hire_date.day):
                years -= 1
            return years
        return 0
    
    @property
    def is_on_leave(self):
        return self.status == 'on_leave' or self.attendances.filter(
            status='on_leave',
            date=timezone.now().date()
        ).exists()




class Attendance(models.Model):
    """
    Employee attendance tracking
    """
    STATUS_CHOICES = [
        ('present', 'Present'),
        ('absent', 'Absent'),
        ('late', 'Late'),
        ('half_day', 'Half Day'),
        ('on_leave', 'On Leave'),
        ('holiday', 'Holiday'),
        ('weekend', 'Weekend'),
    ]
    
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='attendances'
    )
    date = models.DateField()
    
    check_in = models.TimeField(null=True, blank=True)
    check_out = models.TimeField(null=True, blank=True)
    
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='absent'
    )
    
    # For manual entries
    hours_worked = models.DecimalField(
        max_digits=4, 
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)]
    )
    
    # Leave details (if applicable)
    leave_type = models.CharField(max_length=50, blank=True, null=True)
    leave_reason = models.TextField(blank=True, null=True)
    
    # Overtime
    overtime_hours = models.DecimalField(
        max_digits=4, 
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)]
    )
    
    notes = models.TextField(blank=True, null=True)
    approved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_attendances'
    )
    

    class Meta:
        app_label = 'staff'
        ordering = ['-date', 'employee']
        verbose_name = "Attendance"
        verbose_name_plural = "Attendances"
        unique_together = ['employee', 'date']
        indexes = [
            models.Index(fields=['date']),
            models.Index(fields=['status']),
            models.Index(fields=['employee', 'date']),
        ]
    
    def __str__(self):
        return f"{self.employee} - {self.date} ({self.status})"
    
    def save(self, *args, **kwargs):
        # Calculate hours worked if check_in and check_out are provided
        if self.check_in and self.check_out:
            from datetime import datetime
            
            # Convert times to datetime objects for calculation
            check_in_dt = datetime.combine(self.date, self.check_in)
            check_out_dt = datetime.combine(self.date, self.check_out)
            
            # Calculate hours worked
            delta = check_out_dt - check_in_dt
            self.hours_worked = round(delta.total_seconds() / 3600, 2)
            
            # Determine if late (assuming work starts at 8:00 AM)
            work_start = datetime.combine(self.date, datetime.strptime('08:00', '%H:%M').time())
            if check_in_dt > work_start:
                self.status = 'late'
            else:
                self.status = 'present'
        
        super().save(*args, **kwargs)
    
    @property
    def is_approved(self):
        return self.approved_by is not None
    
    @property
    def total_hours(self):
        return self.hours_worked + self.overtime_hours


class LeaveRequest(models.Model):
    """
    Employee leave requests
    """
    LEAVE_TYPE_CHOICES = [
        ('annual', 'Annual Leave'),
        ('sick', 'Sick Leave'),
        ('maternity', 'Maternity Leave'),
        ('paternity', 'Paternity Leave'),
        ('compassionate', 'Compassionate Leave'),
        ('study', 'Study Leave'),
        ('unpaid', 'Unpaid Leave'),
    ]
    
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('approved', 'Approved'),
        ('rejected', 'Rejected'),
        ('cancelled', 'Cancelled'),
    ]
    
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='leave_requests'
    )
    leave_type = models.CharField(max_length=20, choices=LEAVE_TYPE_CHOICES)
    start_date = models.DateField()
    end_date = models.DateField()
    
    total_days = models.PositiveIntegerField()
    
    reason = models.TextField()
    emergency_contact = models.CharField(max_length=100, blank=True, null=True)
    emergency_phone = models.CharField(max_length=20, blank=True, null=True)
    
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default='pending'
    )
    
    approved_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approved_leaves'
    )
    approved_date = models.DateTimeField(null=True, blank=True)
    
    rejection_reason = models.TextField(blank=True, null=True)
    
    # Handover information
    handover_to = models.ForeignKey(
        Employee,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='handover_taken'
    )
    handover_notes = models.TextField(blank=True, null=True)
    

    class Meta:
        app_label = 'staff'
        ordering = ['-start_date']
        verbose_name = "Leave Request"
        verbose_name_plural = "Leave Requests"
    
    def __str__(self):
        return f"{self.employee} - {self.leave_type} ({self.start_date} to {self.end_date})"
    
    def save(self, *args, **kwargs):
        # Calculate total days
        if self.start_date and self.end_date:
            delta = self.end_date - self.start_date
            self.total_days = delta.days + 1  # Inclusive of both dates
        
        super().save(*args, **kwargs)
    
    @property
    def is_approved(self):
        return self.status == 'approved'
    
    @property
    def is_pending(self):
        return self.status == 'pending'


class PerformanceReview(models.Model):
    """
    Employee performance reviews
    """
    RATING_CHOICES = [
        (1, '1 - Needs Improvement'),
        (2, '2 - Below Expectations'),
        (3, '3 - Meets Expectations'),
        (4, '4 - Exceeds Expectations'),
        (5, '5 - Outstanding'),
    ]
    
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='performance_reviews'
    )
    review_date = models.DateField()
    review_period_start = models.DateField()
    review_period_end = models.DateField()
    
    reviewed_by = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='conducted_reviews'
    )
    
    # Performance Metrics
    quality_of_work = models.IntegerField(choices=RATING_CHOICES)
    productivity = models.IntegerField(choices=RATING_CHOICES)
    technical_skills = models.IntegerField(choices=RATING_CHOICES)
    communication = models.IntegerField(choices=RATING_CHOICES)
    teamwork = models.IntegerField(choices=RATING_CHOICES)
    initiative = models.IntegerField(choices=RATING_CHOICES)
    attendance_punctuality = models.IntegerField(choices=RATING_CHOICES)
    
    # Overall
    overall_rating = models.DecimalField(
        max_digits=3, 
        decimal_places=1,
        validators=[MinValueValidator(1)]
    )
    
    # Strengths
    strengths = models.TextField()
    
    # Areas for improvement
    areas_for_improvement = models.TextField()
    
    # Goals for next period
    goals = models.TextField()
    
    # Comments
    employee_comments = models.TextField(blank=True, null=True)
    reviewer_comments = models.TextField()
    
    # Acknowledgment
    employee_acknowledged = models.BooleanField(default=False)
    employee_acknowledged_date = models.DateTimeField(null=True, blank=True)
    

    class Meta:
        app_label = 'staff'
        ordering = ['-review_date']
        verbose_name = "Performance Review"
        verbose_name_plural = "Performance Reviews"
    
    def __str__(self):
        return f"{self.employee} - {self.review_date}"
    
    def save(self, *args, **kwargs):
        # Calculate overall rating (average of all metrics)
        metrics = [
            self.quality_of_work,
            self.productivity,
            self.technical_skills,
            self.communication,
            self.teamwork,
            self.initiative,
            self.attendance_punctuality,
        ]
        self.overall_rating = sum(metrics) / len(metrics)
        
        super().save(*args, **kwargs)


class Payroll(models.Model):
    """
    Employee payroll records
    """
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name='payroll_records'
    )
    pay_period_start = models.DateField()
    pay_period_end = models.DateField()
    payment_date = models.DateField()
    
    # Earnings
    basic_salary = models.DecimalField(
        max_digits=10, 
        decimal_places=2,
        validators=[MinValueValidator(0)]
    )
    overtime_pay = models.DecimalField(
        max_digits=10, 
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)]
    )
    allowances = models.DecimalField(
        max_digits=10, 
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)]
    )
    bonuses = models.DecimalField(
        max_digits=10, 
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)]
    )
    
    # Deductions
    paye = models.DecimalField(
        max_digits=10, 
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)]
    )
    nssf = models.DecimalField(
        max_digits=10, 
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)]
    )
    nhif = models.DecimalField(
        max_digits=10, 
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)]
    )
    other_deductions = models.DecimalField(
        max_digits=10, 
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)]
    )
    
    # Totals
    gross_pay = models.DecimalField(
        max_digits=10, 
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)]
    )
    total_deductions = models.DecimalField(
        max_digits=10, 
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)]
    )
    net_pay = models.DecimalField(
        max_digits=10, 
        decimal_places=2,
        default=0,
        validators=[MinValueValidator(0)]
    )
    
    # Payment Information
    payment_method = models.CharField(max_length=50, default='bank_transfer')
    bank_name = models.CharField(max_length=100, blank=True, null=True)
    bank_account = models.CharField(max_length=50, blank=True, null=True)
    
    # Status
    is_paid = models.BooleanField(default=False)
    paid_date = models.DateField(null=True, blank=True)
    
    notes = models.TextField(blank=True, null=True)
    

    class Meta:
        app_label = 'staff'
        ordering = ['-payment_date']
        verbose_name = "Payroll"
        verbose_name_plural = "Payroll Records"
        unique_together = ['employee', 'pay_period_start', 'pay_period_end']
    
    def __str__(self):
        return f"{self.employee} - {self.pay_period_start} to {self.pay_period_end}"
    
    def save(self, *args, **kwargs):
        # Calculate totals
        self.gross_pay = (
            self.basic_salary + 
            self.overtime_pay + 
            self.allowances + 
            self.bonuses
        )
        
        self.total_deductions = (
            self.paye + 
            self.nssf + 
            self.nhif + 
            self.other_deductions
        )
        
        self.net_pay = self.gross_pay - self.total_deductions
        
        super().save(*args, **kwargs)