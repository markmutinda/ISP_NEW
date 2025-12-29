from rest_framework import permissions
from django.shortcuts import get_object_or_404

from apps.customers.models import Customer


class CustomerAccessPermission(permissions.BasePermission):
    """
    Custom permission to allow:
    - Admin/Staff: Access all customers
    - Technician: Access assigned customers
    - Customer: Access only their own data
    """
    
    def has_permission(self, request, view):
        # Allow all authenticated users to access list/create
        if request.user.is_authenticated:
            return True
        return False
    
    def has_object_permission(self, request, view, obj):
        # Allow admin and staff to access all
        if request.user.role in ['ADMIN', 'STAFF']:
            return True
        
        # Technicians can access if assigned
        if request.user.role == 'TECHNICIAN':
            # Check if technician is assigned to this customer
            # You'll need to implement assignment logic
            return True
        
        # Customers can only access their own data
        if request.user.role == 'CUSTOMER':
            if hasattr(request.user, 'customer_profile'):
                return obj == request.user.customer_profile
        
        return False


class CanManageCustomers(permissions.BasePermission):
    """Permission to manage customers (create, update, delete)"""
    
    def has_permission(self, request, view):
        # Allow admin and staff to manage customers
        if request.user.role in ['ADMIN', 'STAFF']:
            return True
        
        # Allow customers to view (but not modify) their own data
        if view.action in ['list', 'retrieve', 'dashboard']:
            return request.user.is_authenticated
        
        return False
    
    def has_object_permission(self, request, view, obj):
        # Admin and staff can manage all customers
        if request.user.role in ['ADMIN', 'STAFF']:
            return True
        
        # Customers can only view their own data
        if request.user.role == 'CUSTOMER':
            if hasattr(request.user, 'customer_profile'):
                return obj == request.user.customer_profile
        
        return False


class CanUploadDocuments(permissions.BasePermission):
    """Permission to upload customer documents"""
    
    def has_permission(self, request, view):
        # Admin, staff, and customers can upload documents
        if request.user.role in ['ADMIN', 'STAFF']:
            return True
        
        # Customers can upload their own documents
        if request.user.role == 'CUSTOMER':
            customer_id = view.kwargs.get('customer_pk')
            if customer_id:
                customer = get_object_or_404(Customer, pk=customer_id)
                return customer.user == request.user
        
        return False


class CanManageServices(permissions.BasePermission):
    """Permission to manage services"""
    
    def has_permission(self, request, view):
        # Admin and staff can manage all services
        if request.user.role in ['ADMIN', 'STAFF']:
            return True
        
        # Technicians can view and update services
        if request.user.role == 'TECHNICIAN':
            return view.action in ['list', 'retrieve', 'activate', 'suspend']
        
        # Customers can view their own services
        if request.user.role == 'CUSTOMER':
            return view.action in ['list', 'retrieve', 'stats']
        
        return False
    
    def has_object_permission(self, request, view, obj):
        # Admin and staff can manage all services
        if request.user.role in ['ADMIN', 'STAFF']:
            return True
        
        # Technicians can update services
        if request.user.role == 'TECHNICIAN':
            return view.action in ['retrieve', 'activate', 'suspend']
        
        # Customers can view their own services
        if request.user.role == 'CUSTOMER':
            if hasattr(request.user, 'customer_profile'):
                return obj.customer == request.user.customer_profile
        
        return False