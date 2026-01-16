"""
Base ViewSets with automatic company filtering for multi-tenancy
"""
from rest_framework import viewsets, permissions
from django.db.models import Q


class CompanyFilteredViewSet(viewsets.ModelViewSet):
    """
    Base ViewSet that automatically filters queryset by user's company
    - Superusers can see everything
    - Company admins/staff can only see their company's data
    - Automatically sets company on create
    """
    
    def get_queryset(self):
        """
        Override to filter by user's company
        """
        queryset = super().get_queryset()
        user = self.request.user
        
        # Superusers can see everything (with optional company filter)
        if user.is_superuser:
            company_id = self.request.query_params.get('company_id')
            if company_id:
                return queryset.filter(company_id=company_id)
            return queryset
        
        # Users with company can only see their company's data
        if hasattr(user, 'company') and user.company:
            return queryset.filter(company=user.company)
        
        # Users without company - return empty
        return queryset.none()
    
    def perform_create(self, serializer):
        """Automatically set company when creating objects"""
        user = self.request.user
        
        # Only set company if not already provided in data
        if 'company' not in serializer.validated_data:
            if hasattr(user, 'company') and user.company:
                serializer.save(company=user.company)
            else:
                serializer.save()
        else:
            serializer.save()


class RelatedCompanyFilteredViewSet(viewsets.ModelViewSet):
    """
    Base ViewSet for models that don't have direct company field,
    but are related to models that do (e.g., through foreign key chains)
    """
    
    # Override these in subclasses
    company_filter_path = None  # e.g., 'customer__company' or 'subnet__company'
    
    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        
        if user.is_superuser:
            return queryset
        
        if hasattr(user, 'company') and user.company and self.company_filter_path:
            # Build filter dynamically: e.g., filter(customer__company=user.company)
            filter_kwargs = {self.company_filter_path: user.company}
            return queryset.filter(**filter_kwargs)
        
        return queryset.none()


class CompanyStaffPermission(permissions.BasePermission):
    """Permission to allow only company staff/admin to access"""
    
    def has_permission(self, request, view):
        # Allow superusers
        if request.user.is_superuser:
            return True
        
        # Check if user has a company and is staff/admin
        if hasattr(request.user, 'company') and request.user.company:
            return request.user.role in ['admin', 'staff', 'technician', 'accountant', 'support']
        
        return False


class CompanyMemberPermission(permissions.BasePermission):
    """Permission to allow company members (including customers)"""
    
    def has_permission(self, request, view):
        # Allow superusers and users with company
        if request.user.is_superuser:
            return True
        
        # Check if user has a company
        if hasattr(request.user, 'company') and request.user.company:
            return True
        
        return False