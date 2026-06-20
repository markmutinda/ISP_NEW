from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from ..permissions import CustomerOnlyPermission
from apps.customers.models import CustomerAddress
from apps.core.models import User

class CustomerProfileView(APIView):
    """
    Handles fetching and updating the customer's personal profile.
    GET /api/v1/self-service/profile/
    PATCH /api/v1/self-service/profile/
    """
    permission_classes = [IsAuthenticated, CustomerOnlyPermission]

    def get(self, request, *args, **kwargs):
        customer = request.user.customer_profile
        user = request.user
        
        # Try to get the primary address, or fallback to any address they have
        address_text = ""
        address_obj = customer.addresses.filter(is_primary=True).first() or customer.addresses.first()
        if address_obj:
            address_text = address_obj.street_address

        return Response({
            'id': customer.id,
            'customer_code': customer.customer_code,
            'full_name': customer.full_name,
            'first_name': user.first_name,
            'last_name': user.last_name,
            'email': user.email,
            'phone_number': user.phone_number,
            'status': customer.status,
            'category': customer.category,
            'balance': str(customer.outstanding_balance),
            'created_at': customer.created_at.isoformat() if customer.created_at else None,
            'address': address_text
        })

    def patch(self, request, *args, **kwargs):
        customer = request.user.customer_profile
        user = request.user
        data = request.data

        # 1. Update core User fields
        if 'first_name' in data:
            user.first_name = data['first_name']
        if 'last_name' in data:
            user.last_name = data['last_name']
        if 'email' in data:
            email = str(data['email'] or '').strip().lower()
            if email and User.objects.exclude(pk=user.pk).filter(email__iexact=email).exists():
                return Response(
                    {'email': 'This email is already in use by another account.'},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            user.email = email
        user.save()

        # 2. Update or Create Address
        if 'address' in data:
            address_text = data['address']
            address_obj = customer.addresses.filter(is_primary=True).first()
            
            if address_obj:
                address_obj.street_address = address_text
                address_obj.save()
            else:
                # Create a new address record if they don't have one
                CustomerAddress.objects.create(
                    customer=customer,
                    address_type='HOME',
                    is_primary=True,
                    street_address=address_text,
                    county='NAIROBI', # Required by your model
                    sub_county='N/A', # Required by your model
                    ward='N/A'        # Required by your model
                )

        # Return the newly updated profile using the GET logic
        return self.get(request)
