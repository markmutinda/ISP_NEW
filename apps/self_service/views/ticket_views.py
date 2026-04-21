from rest_framework import generics, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.shortcuts import get_object_or_404

from ..permissions import CustomerOnlyPermission
from apps.support.models import SupportTicket, SupportTicketMessage
from apps.support.serializers.serializers import (
    SupportTicketListSerializer,
    SupportTicketDetailSerializer,
    TicketCreateSerializer
)

class CustomerTicketListView(generics.ListCreateAPIView):
    """
    GET: List all support tickets for the logged-in customer.
    POST: Create a new support ticket.
    """
    permission_classes = [IsAuthenticated, CustomerOnlyPermission]
    
    def get_serializer_class(self):
        if self.request.method == 'POST':
            return TicketCreateSerializer
        return SupportTicketListSerializer
    
    def get_queryset(self):
        customer = self.request.user.customer_profile
        # Fetch tickets, including related messages to avoid N+1 queries later
        return SupportTicket.objects.filter(customer=customer).prefetch_related('messages')

    def perform_create(self, serializer):
        # Automatically assign the ticket to the current customer
        customer = self.request.user.customer_profile
        serializer.save(
            customer=customer,
            status='open',
            # You can set default priority/category if the frontend doesn't send it
            priority=self.request.data.get('priority', 'medium'),
            category=self.request.data.get('category', 'technical')
        )


class CustomerTicketDetailView(generics.RetrieveAPIView):
    """
    GET: Retrieve full details of a specific ticket, including all replies.
    """
    permission_classes = [IsAuthenticated, CustomerOnlyPermission]
    serializer_class = SupportTicketDetailSerializer
    
    def get_queryset(self):
        customer = self.request.user.customer_profile
        return SupportTicket.objects.filter(customer=customer)


class CustomerTicketReplyView(APIView):
    """
    POST: Add a reply to an existing ticket.
    """
    permission_classes = [IsAuthenticated, CustomerOnlyPermission]

    def post(self, request, pk, *args, **kwargs):
        customer = request.user.customer_profile
        ticket = get_object_or_404(SupportTicket, pk=pk, customer=customer)
        
        message_text = request.data.get('message')
        if not message_text:
            return Response({'error': 'Message text is required.'}, status=status.HTTP_400_BAD_REQUEST)

        # Create the reply
        SupportTicketMessage.objects.create(
            ticket=ticket,
            sender_type='customer',
            sender=request.user,
            message=message_text,
            is_internal=False
        )

        # If a customer replies, reopen the ticket so staff see it
        if ticket.status in ['resolved', 'closed']:
            ticket.status = 'open'
            ticket.save(update_fields=['status', 'updated_at'])
        else:
            # Just touch the updated_at timestamp
            ticket.save(update_fields=['updated_at'])

        # Return the updated ticket with all messages
        serializer = SupportTicketDetailSerializer(ticket)
        return Response(serializer.data, status=status.HTTP_201_CREATED)