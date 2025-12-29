from rest_framework import status, views
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.decorators import api_view, permission_classes
from django.utils import timezone
from django.shortcuts import get_object_or_404

from apps.customers.models import Customer, CustomerDocument
from apps.customers.serializers import (
    CustomerCreateSerializer, CustomerDocumentSerializer,
    DocumentUploadSerializer
)
from apps.core.permissions import IsAdminOrStaff
from utils.helpers import generate_customer_code


class OnboardingWizardView(views.APIView):
    """Complete customer onboarding wizard"""
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    
    def post(self, request):
        """Multi-step onboarding process"""
        step = request.data.get('step', 1)
        
        if step == 1:
            # Step 1: Create customer account
            serializer = CustomerCreateSerializer(
                data=request.data,
                context={'request': request}
            )
            
            if serializer.is_valid():
                customer = serializer.save()
                
                return Response({
                    'step': 1,
                    'status': 'success',
                    'customer_id': customer.id,
                    'customer_code': customer.customer_code,
                    'message': 'Customer account created successfully',
                    'next_step': 2
                })
            
            return Response({
                'step': 1,
                'status': 'error',
                'errors': serializer.errors
            }, status=status.HTTP_400_BAD_REQUEST)
        
        elif step == 2:
            # Step 2: Upload required documents
            customer_id = request.data.get('customer_id')
            customer = get_object_or_404(Customer, pk=customer_id)
            
            documents_data = request.data.get('documents', [])
            uploaded_docs = []
            
            for doc_data in documents_data:
                doc_serializer = DocumentUploadSerializer(
                    data=doc_data,
                    context={'request': request}
                )
                
                if doc_serializer.is_valid():
                    document = doc_serializer.save(customer=customer)
                    uploaded_docs.append({
                        'id': document.id,
                        'type': document.document_type,
                        'title': document.title
                    })
                else:
                    return Response({
                        'step': 2,
                        'status': 'error',
                        'errors': doc_serializer.errors
                    }, status=status.HTTP_400_BAD_REQUEST)
            
            return Response({
                'step': 2,
                'status': 'success',
                'documents_uploaded': len(uploaded_docs),
                'documents': uploaded_docs,
                'next_step': 3
            })
        
        elif step == 3:
            # Step 3: Create service connection
            customer_id = request.data.get('customer_id')
            customer = get_object_or_404(Customer, pk=customer_id)
            
            # Create initial service (you'll need to implement this)
            # This is a placeholder - implement based on your service model
            service_data = request.data.get('service', {})
            
            # Update customer status
            customer.status = 'ACTIVE'
            customer.activation_date = timezone.now()
            customer.save()
            
            return Response({
                'step': 3,
                'status': 'success',
                'message': 'Onboarding completed successfully',
                'customer': {
                    'id': customer.id,
                    'code': customer.customer_code,
                    'status': customer.status
                },
                'next_step': None
            })
        
        return Response({
            'status': 'error',
            'message': 'Invalid step number'
        }, status=status.HTTP_400_BAD_REQUEST)


class DocumentUploadView(views.APIView):
    """View for uploading customer documents"""
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    
    def post(self, request, customer_id):
        customer = get_object_or_404(Customer, pk=customer_id)
        
        serializer = DocumentUploadSerializer(
            data=request.data,
            context={'request': request}
        )
        
        if serializer.is_valid():
            document = serializer.save(customer=customer)
            
            return Response({
                'status': 'success',
                'message': 'Document uploaded successfully',
                'document': CustomerDocumentSerializer(document).data
            })
        
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)


class OnboardingCompleteView(views.APIView):
    """Mark onboarding as complete"""
    permission_classes = [IsAuthenticated, IsAdminOrStaff]
    
    def post(self, request, customer_id):
        customer = get_object_or_404(Customer, pk=customer_id)
        
        # Check if all required documents are uploaded
        required_docs = ['NATIONAL_ID', 'KRA_PIN']  # Define your required docs
        
        uploaded_docs = customer.documents.values_list(
            'document_type', flat=True
        )
        
        missing_docs = [
            doc for doc in required_docs if doc not in uploaded_docs
        ]
        
        if missing_docs:
            return Response({
                'status': 'error',
                'message': 'Missing required documents',
                'missing_documents': missing_docs
            }, status=status.HTTP_400_BAD_REQUEST)
        
        # Complete onboarding
        customer.status = 'ACTIVE'
        customer.activation_date = timezone.now()
        customer.save()
        
        # Send welcome email/notification
        # self.send_welcome_notification(customer)
        
        return Response({
            'status': 'success',
            'message': 'Onboarding completed successfully',
            'customer': {
                'id': customer.id,
                'code': customer.customer_code,
                'status': customer.status,
                'activation_date': customer.activation_date
            }
        })
    
    def send_welcome_notification(self, customer):
        """Send welcome notification to customer"""
        # Implement email/SMS notification
        pass


@api_view(['GET'])
@permission_classes([IsAuthenticated, IsAdminOrStaff])
def onboarding_checklist(request, customer_id):
    """Get onboarding checklist for a customer"""
    customer = get_object_or_404(Customer, pk=customer_id)
    
    checklist = {
        'customer': {
            'id': customer.id,
            'code': customer.customer_code,
            'status': customer.status,
            'completed': customer.status == 'ACTIVE'
        },
        'steps': [
            {
                'id': 1,
                'name': 'Account Creation',
                'completed': True,
                'completed_at': customer.created_at
            },
            {
                'id': 2,
                'name': 'Document Upload',
                'completed': customer.documents.filter(
                    document_type__in=['NATIONAL_ID', 'KRA_PIN']
                ).count() >= 2,
                'documents_uploaded': customer.documents.count(),
                'documents_required': 2
            },
            {
                'id': 3,
                'name': 'Service Configuration',
                'completed': customer.services.exists(),
                'services_count': customer.services.count()
            },
            {
                'id': 4,
                'name': 'Payment Setup',
                'completed': False,  # Will be implemented in billing module
            },
            {
                'id': 5,
                'name': 'Onboarding Complete',
                'completed': customer.status == 'ACTIVE'
            }
        ],
        'progress': 0,
        'total_steps': 5,
        'completed_steps': 0
    }
    
    # Calculate progress
    completed_steps = sum(1 for step in checklist['steps'] if step['completed'])
    checklist['completed_steps'] = completed_steps
    checklist['progress'] = int((completed_steps / checklist['total_steps']) * 100)
    
    return Response(checklist)