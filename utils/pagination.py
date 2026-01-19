"""
Custom pagination classes for ISP Management System
"""
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class StandardResultsSetPagination(PageNumberPagination):
    """
    Standard pagination for API results.
    """
    page_size = 25
    page_size_query_param = 'page_size'
    max_page_size = 100
    
    def get_paginated_response(self, data):
        return Response({
            'pagination': {
                'count': self.page.paginator.count,
                'next': self.get_next_link(),
                'previous': self.get_previous_link(),
                'page_size': self.get_page_size(self.request),
                'current_page': self.page.number,
                'total_pages': self.page.paginator.num_pages,
            },
            'results': data
        })


class LargeResultsSetPagination(PageNumberPagination):
    """
    Pagination for large result sets.
    """
    page_size = 100
    page_size_query_param = 'page_size'
    max_page_size = 500


class SmallResultsSetPagination(PageNumberPagination):
    """
    Pagination for small result sets.
    """
    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 50





    
