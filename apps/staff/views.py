from django.db.models import Q, Count, Sum, Avg
from django.utils import timezone
from django_filters import rest_framework as filters
from rest_framework import viewsets, generics, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.filters import SearchFilter, OrderingFilter
from datetime import timedelta, datetime
import pandas as pd
import calendar

# Correct imports from your custom permissions
from apps.core.permissions import IsAdmin, IsAdminOrStaff

from .models import (
    Department, Employee, Attendance, LeaveRequest,
    PerformanceReview, Payroll
)
from .serializers import (
    DepartmentSerializer, EmployeeSerializer, AttendanceSerializer,
    LeaveRequestSerializer, PerformanceReviewSerializer, PayrollSerializer,
    DepartmentReportSerializer, AttendanceReportSerializer, PayrollSummarySerializer
)
from .filters import EmployeeFilter, AttendanceFilter, LeaveRequestFilter


class DepartmentViewSet(viewsets.ModelViewSet):
    """
    CRUD operations for departments
    """
    queryset = Department.objects.filter(is_active=True)
    serializer_class = DepartmentSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]  # Staff + Admin access
    filter_backends = [SearchFilter, OrderingFilter]
    search_fields = ['name', 'description', 'location']
    ordering_fields = ['name', 'budget', 'created_at']
    
    def get_queryset(self):
        queryset = super().get_queryset()
        
        # Annotate with counts
        queryset = queryset.annotate(
            employee_count=Count('employees', filter=Q(employees__is_active=True)),
            subdepartment_count=Count('subdepartments', filter=Q(subdepartments__is_active=True))
        )
        
        return queryset
    
    def perform_destroy(self, instance):
        # Soft delete instead of actual delete
        instance.is_active = False
        instance.save()
    
    @action(detail=True, methods=['get'])
    def employees(self, request, pk=None):
        """Get employees in this department"""
        department = self.get_object()
        employees = department.employees.filter(is_active=True)
        serializer = EmployeeSerializer(employees, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def subdepartments(self, request, pk=None):
        """Get subdepartments"""
        department = self.get_object()
        subdepartments = department.subdepartments.filter(is_active=True)
        serializer = self.get_serializer(subdepartments, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def hierarchy(self, request):
        """Get department hierarchy"""
        def build_hierarchy(dept, level=0):
            hierarchy = {
                'id': dept.id,
                'name': dept.name,
                'level': level,
                'manager': dept.manager.get_full_name() if dept.manager else None,
                'employee_count': dept.employee_count,
                'subdepartments': []
            }
            
            for subdept in dept.subdepartments.filter(is_active=True):
                hierarchy['subdepartments'].append(
                    build_hierarchy(subdept, level + 1)
                )
            
            return hierarchy
        
        top_level = Department.objects.filter(
            parent_department__isnull=True,
            is_active=True
        )
        
        result = []
        for dept in top_level:
            result.append(build_hierarchy(dept))
        
        return Response(result)


class EmployeeViewSet(viewsets.ModelViewSet):
    """
    CRUD operations for employees
    """
    queryset = Employee.objects.filter(is_active=True)
    serializer_class = EmployeeSerializer
    permission_classes = [IsAuthenticated, IsAdmin]  # Only admins/HR can create/update/delete
    filter_backends = [filters.DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_class = EmployeeFilter
    search_fields = [
        'user__first_name', 'user__last_name', 'employee_id',
        'position', 'phone', 'email'
    ]
    ordering_fields = [
        'employee_id', 'hire_date', 'position',
        'salary', 'created_at'
    ]
    
    def get_queryset(self):
        queryset = super().get_queryset()
        
        # HR and admins can see all, managers can see their department
        user = self.request.user
        
        if not user.is_superuser:
            if hasattr(user, 'employee_profile'):
                employee = user.employee_profile
                
                # If user is a department manager, show their department employees
                if employee.department and employee.department.manager == employee:
                    queryset = queryset.filter(department=employee.department)
                # Regular staff can only see themselves
                elif not user.is_staff:
                    queryset = queryset.filter(user=user)
        
        return queryset
    
    def get_permissions(self):
        # List and retrieve can be seen by staff
        if self.action in ['list', 'retrieve']:
            return [IsAuthenticated(), IsAdminOrStaff()]
        return super().get_permissions()
    
    @action(detail=True, methods=['get'])
    def attendance(self, request, pk=None):
        """Get attendance records for employee"""
        employee = self.get_object()
        
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        
        attendances = employee.attendances.all()
        
        if start_date:
            attendances = attendances.filter(date__gte=start_date)
        if end_date:
            attendances = attendances.filter(date__lte=end_date)
        
        attendances = attendances.order_by('-date')
        serializer = AttendanceSerializer(attendances, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def leaves(self, request, pk=None):
        """Get leave requests for employee"""
        employee = self.get_object()
        
        leaves = employee.leave_requests.all().order_by('-start_date')
        serializer = LeaveRequestSerializer(leaves, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def performance(self, request, pk=None):
        """Get performance reviews for employee"""
        employee = self.get_object()
        
        reviews = employee.performance_reviews.all().order_by('-review_date')
        serializer = PerformanceReviewSerializer(reviews, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['get'])
    def payroll(self, request, pk=None):
        """Get payroll records for employee"""
        employee = self.get_object()
        
        payrolls = employee.payroll_records.all().order_by('-payment_date')
        serializer = PayrollSerializer(payrolls, many=True)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def terminate(self, request, pk=None):
        """Terminate employee"""
        employee = self.get_object()
        
        if employee.status == 'terminated':
            return Response(
                {'error': 'Employee already terminated'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        termination_date = request.data.get('termination_date', timezone.now().date())
        termination_reason = request.data.get('reason', '')
        
        employee.status = 'terminated'
        employee.termination_date = termination_date
        employee.is_active = False
        employee.has_system_access = False
        employee.notes = f"{employee.notes}\n\nTerminated on {termination_date}: {termination_reason}"
        employee.save()
        
        # Deactivate user account
        if employee.user:
            employee.user.is_active = False
            employee.user.save()
        
        serializer = self.get_serializer(employee)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def activate(self, request, pk=None):
        """Reactivate employee"""
        employee = self.get_object()
        
        if employee.status != 'terminated':
            return Response(
                {'error': 'Only terminated employees can be reactivated'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        employee.status = 'active'
        employee.termination_date = None
        employee.is_active = True
        employee.has_system_access = True
        employee.save()
        
        # Reactivate user account
        if employee.user:
            employee.user.is_active = True
            employee.user.save()
        
        serializer = self.get_serializer(employee)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def birthday(self, request):
        """Get employees with birthdays this month"""
        today = timezone.now().date()
        current_month = today.month
        
        employees = self.get_queryset().filter(
            date_of_birth__month=current_month,
            is_active=True
        ).order_by('date_of_birth__day')
        
        serializer = self.get_serializer(employees, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def anniversary(self, request):
        """Get employees with service anniversaries this month"""
        today = timezone.now().date()
        current_month = today.month
        
        employees = self.get_queryset().filter(
            hire_date__month=current_month,
            is_active=True
        ).order_by('hire_date__day')
        
        # Calculate service years
        result = []
        for emp in employees:
            years = emp.calculate_service_years()
            result.append({
                'employee': self.get_serializer(emp).data,
                'service_years': years
            })
        
        return Response(result)


class AttendanceViewSet(viewsets.ModelViewSet):
    """
    CRUD operations for attendance
    """
    queryset = Attendance.objects.all()
    serializer_class = AttendanceSerializer
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    filter_backends = [filters.DjangoFilterBackend, OrderingFilter]
    filterset_class = AttendanceFilter
    ordering_fields = ['date', 'check_in', 'check_out']
    
    def get_queryset(self):
        queryset = super().get_queryset()
        
        # Regular staff can only see their own attendance
        user = self.request.user
        if not user.is_staff and hasattr(user, 'employee_profile'):
            queryset = queryset.filter(employee__user=user)
        
        return queryset
    
    @action(detail=False, methods=['post'])
    def bulk_check_in(self, request):
        """Bulk check-in for multiple employees"""
        employee_ids = request.data.get('employee_ids', [])
        check_in_time = request.data.get('check_in', timezone.now().time())
        
        today = timezone.now().date()
        
        created = 0
        updated = 0
        
        for emp_id in employee_ids:
            try:
                employee = Employee.objects.get(id=emp_id, is_active=True)
                
                # Check if attendance already exists
                attendance, created_flag = Attendance.objects.get_or_create(
                    employee=employee,
                    date=today,
                    defaults={
                        'check_in': check_in_time,
                        'status': 'present'
                    }
                )
                
                if not created_flag:
                    attendance.check_in = check_in_time
                    attendance.save()
                    updated += 1
                else:
                    created += 1
                    
            except Employee.DoesNotExist:
                continue
        
        return Response({
            'message': f'Created {created} new records, updated {updated} existing records',
            'created': created,
            'updated': updated
        })
    
    @action(detail=False, methods=['post'])
    def bulk_check_out(self, request):
        """Bulk check-out for multiple employees"""
        employee_ids = request.data.get('employee_ids', [])
        check_out_time = request.data.get('check_out', timezone.now().time())
        
        today = timezone.now().date()
        
        updated = 0
        
        for emp_id in employee_ids:
            try:
                attendance = Attendance.objects.get(
                    employee_id=emp_id,
                    date=today
                )
                
                attendance.check_out = check_out_time
                attendance.save()
                updated += 1
                
            except (Employee.DoesNotExist, Attendance.DoesNotExist):
                continue
        
        return Response({
            'message': f'Updated {updated} records',
            'updated': updated
        })
    
    @action(detail=False, methods=['get'])
    def today(self, request):
        """Get today's attendance"""
        today = timezone.now().date()
        
        attendances = self.get_queryset().filter(date=today)
        
        # Get all active employees for comparison
        active_employees = Employee.objects.filter(is_active=True)
        
        present = attendances.filter(status__in=['present', 'late'])
        absent = active_employees.exclude(
            id__in=attendances.values('employee')
        )
        on_leave = attendances.filter(status='on_leave')
        
        data = {
            'date': today,
            'total_employees': active_employees.count(),
            'present': AttendanceSerializer(present, many=True).data,
            'present_count': present.count(),
            'absent_count': absent.count(),
            'on_leave_count': on_leave.count(),
            'absent_employees': EmployeeSerializer(absent, many=True).data
        }
        
        return Response(data)
    
    @action(detail=False, methods=['get'])
    def report(self, request):
        """Generate attendance report"""
        start_date = request.query_params.get('start_date')
        end_date = request.query_params.get('end_date')
        department_id = request.query_params.get('department')
        
        if not start_date or not end_date:
            return Response(
                {'error': 'start_date and end_date are required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Convert string dates to date objects
        start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
        end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
        
        # Get employees based on filters
        employees = Employee.objects.filter(is_active=True)
        if department_id:
            employees = employees.filter(department_id=department_id)
        
        report_data = []
        
        for employee in employees:
            # Get attendance for this period
            attendances = Attendance.objects.filter(
                employee=employee,
                date__gte=start_date,
                date__lte=end_date
            )
            
            total_days = (end_date - start_date).days + 1
            present_days = attendances.filter(status__in=['present', 'late']).count()
            absent_days = total_days - present_days
            late_days = attendances.filter(status='late').count()
            leave_days = attendances.filter(status='on_leave').count()
            
            attendance_rate = (present_days / total_days * 100) if total_days > 0 else 0
            
            report_data.append({
                'employee': employee.get_full_name(),
                'employee_id': employee.employee_id,
                'department': employee.department.name if employee.department else '',
                'total_days': total_days,
                'present_days': present_days,
                'absent_days': absent_days,
                'late_days': late_days,
                'leave_days': leave_days,
                'attendance_rate': round(attendance_rate, 2)
            })
        
        serializer = AttendanceReportSerializer(report_data, many=True)
        return Response(serializer.data)


class LeaveRequestViewSet(viewsets.ModelViewSet):
    """
    CRUD operations for leave requests
    """
    queryset = LeaveRequest.objects.all()
    serializer_class = LeaveRequestSerializer
    permission_classes = [IsAuthenticated]  # Employees can create their own
    filter_backends = [filters.DjangoFilterBackend, OrderingFilter]
    filterset_class = LeaveRequestFilter
    ordering_fields = ['start_date', 'end_date', 'created_at']
    
    def get_queryset(self):
        queryset = super().get_queryset()
        
        user = self.request.user
        
        # Regular employees can only see their own leave requests
        if not user.is_staff and hasattr(user, 'employee_profile'):
            queryset = queryset.filter(employee__user=user)
        
        return queryset
    
    def get_permissions(self):
        if self.action in ['approve', 'reject', 'list', 'pending']:
            return [IsAuthenticated(), IsAdminOrStaff()]
        return super().get_permissions()
    
    def perform_create(self, serializer):
        # Automatically set employee for regular users
        if hasattr(self.request.user, 'employee_profile'):
            serializer.save(employee=self.request.user.employee_profile)
        else:
            serializer.save()
    
    @action(detail=True, methods=['post'])
    def approve(self, request, pk=None):
        """Approve a leave request"""
        leave_request = self.get_object()
        
        if leave_request.status != 'pending':
            return Response(
                {'error': 'Only pending leave requests can be approved'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        leave_request.status = 'approved'
        leave_request.approved_by = request.user
        leave_request.approved_date = timezone.now()
        leave_request.save()
        
        # Create attendance records for the leave period
        current_date = leave_request.start_date
        while current_date <= leave_request.end_date:
            Attendance.objects.update_or_create(
                employee=leave_request.employee,
                date=current_date,
                defaults={
                    'status': 'on_leave',
                    'leave_type': leave_request.leave_type,
                    'leave_reason': leave_request.reason
                }
            )
            current_date += timedelta(days=1)
        
        serializer = self.get_serializer(leave_request)
        return Response(serializer.data)
    
    @action(detail=True, methods=['post'])
    def reject(self, request, pk=None):
        """Reject a leave request"""
        leave_request = self.get_object()
        
        if leave_request.status != 'pending':
            return Response(
                {'error': 'Only pending leave requests can be rejected'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        rejection_reason = request.data.get('reason', '')
        
        leave_request.status = 'rejected'
        leave_request.approved_by = request.user
        leave_request.approved_date = timezone.now()
        leave_request.rejection_reason = rejection_reason
        leave_request.save()
        
        serializer = self.get_serializer(leave_request)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def pending(self, request):
        """Get pending leave requests"""
        pending_leaves = self.get_queryset().filter(status='pending')
        serializer = self.get_serializer(pending_leaves, many=True)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def calendar(self, request):
        """Get leave calendar"""
        year = request.query_params.get('year', timezone.now().year)
        month = request.query_params.get('month', timezone.now().month)
        
        try:
            year = int(year)
            month = int(month)
        except ValueError:
            return Response(
                {'error': 'Invalid year or month'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get first and last day of month
        first_day = datetime(year, month, 1).date()
        last_day = datetime(year, month, calendar.monthrange(year, month)[1]).date()
        
        leaves = self.get_queryset().filter(
            status='approved',
            start_date__lte=last_day,
            end_date__gte=first_day
        )
        
        calendar_data = []
        for leave in leaves:
            calendar_data.append({
                'id': leave.id,
                'title': f"{leave.employee.get_full_name()} - {leave.leave_type}",
                'start': leave.start_date.isoformat(),
                'end': leave.end_date.isoformat(),
                'employee': leave.employee.get_full_name(),
                'leave_type': leave.get_leave_type_display(),
                'days': leave.total_days
            })
        
        return Response(calendar_data)


class PerformanceReviewViewSet(viewsets.ModelViewSet):
    """
    CRUD operations for performance reviews
    """
    queryset = PerformanceReview.objects.all()
    serializer_class = PerformanceReviewSerializer
    permission_classes = [IsAuthenticated, IsAdmin]  # Only HR/Admins manage reviews
    filter_backends = [filters.DjangoFilterBackend, OrderingFilter]
    filterset_fields = ['employee', 'reviewed_by', 'review_date']
    ordering_fields = ['review_date', 'overall_rating']
    
    def get_queryset(self):
        queryset = super().get_queryset()
        
        # Regular employees can only see their own reviews
        user = self.request.user
        if not user.is_staff and hasattr(user, 'employee_profile'):
            queryset = queryset.filter(employee__user=user)
        
        return queryset
    
    @action(detail=True, methods=['post'])
    def acknowledge(self, request, pk=None):
        """Employee acknowledges review"""
        review = self.get_object()
        
        if review.employee.user != request.user:
            return Response(
                {'error': 'You can only acknowledge your own reviews'},
                status=status.HTTP_403_FORBIDDEN
            )
        
        review.employee_acknowledged = True
        review.employee_acknowledged_date = timezone.now()
        review.save()
        
        serializer = self.get_serializer(review)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def overdue(self, request):
        """Get overdue performance reviews"""
        six_months_ago = timezone.now().date() - timedelta(days=180)
        
        # Get employees without review in last 6 months
        employees_with_recent_review = PerformanceReview.objects.filter(
            review_date__gte=six_months_ago
        ).values_list('employee', flat=True)
        
        overdue_employees = Employee.objects.filter(
            is_active=True
        ).exclude(
            id__in=employees_with_recent_review
        )
        
        serializer = EmployeeSerializer(overdue_employees, many=True)
        return Response(serializer.data)


class PayrollViewSet(viewsets.ModelViewSet):
    """
    CRUD operations for payroll
    """
    queryset = Payroll.objects.all()
    serializer_class = PayrollSerializer
    permission_classes = [IsAuthenticated, IsAdmin]  # Only HR/Admins
    filter_backends = [filters.DjangoFilterBackend, OrderingFilter]
    filterset_fields = ['employee', 'is_paid', 'payment_date']
    ordering_fields = ['payment_date', 'net_pay']
    
    def get_queryset(self):
        queryset = super().get_queryset()
        
        # Regular employees can only see their own payroll
        user = self.request.user
        if not user.is_staff and hasattr(user, 'employee_profile'):
            queryset = queryset.filter(employee__user=user)
        
        return queryset
    
    @action(detail=True, methods=['post'])
    def mark_paid(self, request, pk=None):
        """Mark payroll as paid"""
        payroll = self.get_object()
        
        if payroll.is_paid:
            return Response(
                {'error': 'Payroll already marked as paid'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        payroll.is_paid = True
        payroll.paid_date = timezone.now().date()
        payroll.save()
        
        serializer = self.get_serializer(payroll)
        return Response(serializer.data)
    
    @action(detail=False, methods=['get'])
    def summary(self, request):
        """Get payroll summary"""
        year = request.query_params.get('year', timezone.now().year)
        month = request.query_params.get('month')
        
        try:
            year = int(year)
            if month:
                month = int(month)
        except ValueError:
            return Response(
                {'error': 'Invalid year or month'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if month:
            # Monthly summary
            payrolls = Payroll.objects.filter(
                payment_date__year=year,
                payment_date__month=month
            )
            period = f"{year}-{month:02d}"
        else:
            # Yearly summary
            payrolls = Payroll.objects.filter(payment_date__year=year)
            period = str(year)
        
        summary = payrolls.aggregate(
            total_employees=Count('employee', distinct=True),
            total_gross_pay=Sum('gross_pay'),
            total_deductions=Sum('total_deductions'),
            total_net_pay=Sum('net_pay'),
            avg_salary=Avg('net_pay')
        )
        
        data = {
            'period': period,
            'total_employees': summary['total_employees'] or 0,
            'total_gross_pay': summary['total_gross_pay'] or 0,
            'total_deductions': summary['total_deductions'] or 0,
            'total_net_pay': summary['total_net_pay'] or 0,
            'avg_salary': summary['avg_salary'] or 0
        }
        
        serializer = PayrollSummarySerializer(data)
        return Response(serializer.data)


class AttendanceReportView(generics.GenericAPIView):
    """
    Generate attendance reports
    """
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    
    def get(self, request):
        report_type = request.query_params.get('type', 'monthly')
        department_id = request.query_params.get('department')
        
        if report_type == 'monthly':
            # Monthly attendance report
            today = timezone.now().date()
            first_day = today.replace(day=1)
            last_day = today.replace(day=calendar.monthrange(today.year, today.month)[1])
            
            employees = Employee.objects.filter(is_active=True)
            if department_id:
                employees = employees.filter(department_id=department_id)
            
            report_data = []
            
            for employee in employees:
                attendances = Attendance.objects.filter(
                    employee=employee,
                    date__gte=first_day,
                    date__lte=last_day
                )
                
                total_days = (last_day - first_day).days + 1
                present_days = attendances.filter(status__in=['present', 'late']).count()
                absent_days = total_days - present_days
                late_days = attendances.filter(status='late').count()
                leave_days = attendances.filter(status='on_leave').count()
                
                attendance_rate = (present_days / total_days * 100) if total_days > 0 else 0
                
                report_data.append({
                    'employee': employee.get_full_name(),
                    'employee_id': employee.employee_id,
                    'department': employee.department.name if employee.department else '',
                    'total_days': total_days,
                    'present_days': present_days,
                    'absent_days': absent_days,
                    'late_days': late_days,
                    'leave_days': leave_days,
                    'attendance_rate': round(attendance_rate, 2)
                })
            
            serializer = AttendanceReportSerializer(report_data, many=True)
            return Response(serializer.data)
        
        return Response(
            {'error': 'Invalid report type'},
            status=status.HTTP_400_BAD_REQUEST
        )


class DepartmentReportView(generics.GenericAPIView):
    """
    Generate department reports
    """
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    
    def get(self, request):
        departments = Department.objects.filter(is_active=True)
        
        report_data = []
        
        for dept in departments:
            employees = dept.employees.filter(is_active=True)
            
            active_count = employees.filter(status='active').count()
            on_leave_count = employees.filter(status='on_leave').count()
            
            # Calculate average service years
            service_years = []
            for emp in employees:
                years = emp.calculate_service_years()
                service_years.append(years)
            
            avg_service_years = sum(service_years) / len(service_years) if service_years else 0
            
            # Calculate total salary
            total_salary = employees.aggregate(
                total=Sum('salary')
            )['total'] or 0
            
            report_data.append({
                'department': dept.name,
                'employee_count': employees.count(),
                'active_count': active_count,
                'on_leave_count': on_leave_count,
                'avg_service_years': round(avg_service_years, 1),
                'total_salary': total_salary
            })
        
        serializer = DepartmentReportSerializer(report_data, many=True)
        return Response(serializer.data)
