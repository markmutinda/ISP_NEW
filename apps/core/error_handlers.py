"""
Error handlers for Django
"""
from django.http import JsonResponse
from rest_framework import status

def bad_request(request, exception):
    return JsonResponse({
        'error': 'Bad Request',
        'message': str(exception),
        'status_code': 400
    }, status=400)

def permission_denied(request, exception):
    return JsonResponse({
        'error': 'Permission Denied',
        'message': str(exception),
        'status_code': 403
    }, status=403)

def page_not_found(request, exception):
    return JsonResponse({
        'error': 'Page Not Found',
        'message': 'The requested resource was not found',
        'status_code': 404
    }, status=404)

def server_error(request):
    return JsonResponse({
        'error': 'Internal Server Error',
        'message': 'An unexpected error occurred',
        'status_code': 500
    }, status=500)