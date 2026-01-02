import django_filters
from .models import Employee, Attendance, LeaveRequest


class EmployeeFilter(django_filters.FilterSet):
    department = django_filters.CharFilter(field_name='department__name')
    status = django_filters.CharFilter(field_name='status')
    employment_type = django_filters.CharFilter(field_name='employment_type')
    
    hire_date_from = django_filters.DateFilter(
        field_name='hire_date',
        lookup_expr='gte'
    )
    hire_date_to = django_filters.DateFilter(
        field_name='hire_date',
        lookup_expr='lte'
    )
    
    min_salary = django_filters.NumberFilter(
        field_name='salary',
        lookup_expr='gte'
    )
    max_salary = django_filters.NumberFilter(
        field_name='salary',
        lookup_expr='lte'
    )
    
    has_system_access = django_filters.BooleanFilter(field_name='has_system_access')
    
    class Meta:
        model = Employee
        fields = {
            'department': ['exact'],
            'status': ['exact'],
            'employment_type': ['exact'],
        }


class AttendanceFilter(django_filters.FilterSet):
    employee = django_filters.CharFilter(field_name='employee__employee_id')
    department = django_filters.CharFilter(field_name='employee__department__name')
    status = django_filters.CharFilter(field_name='status')
    
    date_from = django_filters.DateFilter(
        field_name='date',
        lookup_expr='gte'
    )
    date_to = django_filters.DateFilter(
        field_name='date',
        lookup_expr='lte'
    )
    
    class Meta:
        model = Attendance
        fields = {
            'status': ['exact'],
        }


class LeaveRequestFilter(django_filters.FilterSet):
    employee = django_filters.CharFilter(field_name='employee__employee_id')
    department = django_filters.CharFilter(field_name='employee__department__name')
    leave_type = django_filters.CharFilter(field_name='leave_type')
    status = django_filters.CharFilter(field_name='status')
    
    start_date_from = django_filters.DateFilter(
        field_name='start_date',
        lookup_expr='gte'
    )
    start_date_to = django_filters.DateFilter(
        field_name='start_date',
        lookup_expr='lte'
    )
    
    end_date_from = django_filters.DateFilter(
        field_name='end_date',
        lookup_expr='gte'
    )
    end_date_to = django_filters.DateFilter(
        field_name='end_date',
        lookup_expr='lte'
    )
    
    class Meta:
        model = LeaveRequest
        fields = {
            'leave_type': ['exact'],
            'status': ['exact'],
        }