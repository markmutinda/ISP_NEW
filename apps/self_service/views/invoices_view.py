"""
Customer Invoices View

Provides invoice listing for authenticated customers.
"""

from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.core.paginator import Paginator

from ..permissions import CustomerOnlyPermission
from apps.customers.models import Customer
from apps.billing.models import Invoice


class CustomerInvoicesView(APIView):
    """
    List customer invoices.
    
    GET /api/v1/self-service/invoices/
    
    Query Parameters:
        - page: Page number (default: 1)
        - page_size: Items per page (default: 10, max: 50)
        - status: Filter by status (PENDING, PAID, OVERDUE, CANCELLED)
    """
    
    permission_classes = [IsAuthenticated, CustomerOnlyPermission]
    
    def get(self, request):
        user = request.user
        
        # Get customer profile
        try:
            customer = Customer.objects.get(user=user)
        except Customer.DoesNotExist:
            return Response({
                'error': 'Customer profile not found'
            }, status=status.HTTP_404_NOT_FOUND)
        
        # Get query parameters
        page = int(request.query_params.get('page', 1))
        page_size = min(int(request.query_params.get('page_size', 10)), 50)
        status_filter = request.query_params.get('status')
        
        # Build queryset
        invoices = Invoice.objects.filter(customer=customer).order_by('-created_at')
        
        if status_filter:
            invoices = invoices.filter(status=status_filter.upper())
        
        # Paginate
        paginator = Paginator(invoices, page_size)
        page_obj = paginator.get_page(page)
        
        # Serialize
        results = []
        for invoice in page_obj:
            results.append({
                'id': invoice.id,
                'invoice_number': invoice.invoice_number,
                'amount': float(invoice.amount),
                'amount_paid': float(getattr(invoice, 'amount_paid', 0) or 0),
                'amount_due': float(getattr(invoice, 'amount_due', invoice.amount) or invoice.amount),
                'status': invoice.status,
                'due_date': invoice.due_date,
                'created_at': invoice.created_at,
                'description': getattr(invoice, 'description', '') or '',
            })
        
        return Response({
            'count': paginator.count,
            'total_pages': paginator.num_pages,
            'current_page': page,
            'results': results,
        })
