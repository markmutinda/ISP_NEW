# apps/billing/services/tuma_service.py
import requests
import logging
from django.conf import settings


class TumaError(Exception):
    pass


class TumaClient:
    def __init__(self):
        self.base = settings.TUMA_API_BASE_URL.rstrip("/")

    def auth_token(self, email, api_key):
        """
        Authenticate with Tuma API and get access token.
        
        Args:
            email: Business email (master or child)
            api_key: API key for the business
            
        Returns:
            str: Access token
        """
        response = requests.post(
            f"{self.base}/auth/token",
            json={"email": email, "api_key": api_key},
            timeout=20
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise TumaError(payload.get("message", "Authentication failed"))
        return payload["data"]["token"]

    def list_banks(self, token):
        """
        Get list of available banks from Tuma reference data.
        
        Args:
            token: Valid authentication token
            
        Returns:
            dict: Response containing list of banks
        """
        response = requests.get(
            f"{self.base}/reference/banks",
            headers={"Authorization": f"Bearer {token}"},
            timeout=20
        )
        response.raise_for_status()
        return response.json()

    def get_banks_map(self, token):
        """
        Get a dictionary mapping bank_id to bank details.
        Useful for quick lookups when updating business configurations.
        
        Args:
            token: Valid authentication token
            
        Returns:
            dict: Mapping of bank_id -> bank data
        """
        payload = self.list_banks(token)
        items = payload.get("data", []) if isinstance(payload, dict) else []
        return {str(x.get("id")): x for x in items}

    def create_business(self, token, body):
        """
        Create a child business under the master account.
        
        Args:
            token: Master authentication token
            body: Business creation payload containing:
                - name: Business name
                - email: Business email
                - mobile: Contact phone number
                - bank_id: Selected bank ID
                - account_number: Bank account number
                - logo: Logo URL (optional)
                - description: Business description (optional)
                
        Returns:
            dict: Created business data
        """
        response = requests.post(
            f"{self.base}/businesses",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            timeout=20
        )
        response.raise_for_status()
        return response.json()

    def update_business(self, token, business_id, body):
        """
        Update an existing child business.
        Called when tenant changes their bank, account number, or other details.
        
        Args:
            token: Master authentication token
            business_id: ID of the business to update
            body: Update payload containing fields to change:
                - name: Business name (optional)
                - email: Business email (optional)
                - mobile: Contact phone (optional)
                - bank_id: New bank ID (optional)
                - account_number: New account number (optional)
                - logo: New logo URL (optional)
                - description: New description (optional)
                - is_active: Toggle business active status (optional)
                
        Returns:
            dict: Updated business data
        """
        response = requests.put(
            f"{self.base}/businesses/{business_id}",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            timeout=20
        )
        response.raise_for_status()
        return response.json()

    def delete_business(self, token, business_id):
        """
        Delete a child business (soft delete or hard delete per Tuma API).
        Called when a tenant is deactivated or removed from the platform.
        
        Args:
            token: Master authentication token
            business_id: ID of the business to delete
            
        Returns:
            dict: Deletion confirmation
        """
        response = requests.delete(
            f"{self.base}/businesses/{business_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=20
        )
        response.raise_for_status()
        return response.json()

    def stk_push(self, token, amount, phone, callback_url, description):
        """
        Initiates an STK Push.
        
        Args:
            token: Child business authentication token
            amount: Payment amount (will be converted to integer for Safaricom)
            phone: Customer phone number (format: 2547XXXXXXXX)
            callback_url: Webhook URL for payment confirmation
            description: Original description that will be cleaned and used as unique reference
            
        Returns:
            dict: Contains merchant_request_id and checkout_request_id
        """
        # 1. Clean Amount: Safaricom wants an integer
        clean_amount = int(float(amount))
        
        # 2. Clean Description/Reference: Tuma maps this to PayHero's external_reference.
        # It MUST NOT have spaces, and must be short. We append a timestamp to ensure it's always unique.
        import time
        safe_desc = str(description).replace(" ", "-").replace("/", "-")[:10]
        unique_reference = f"{safe_desc}-{int(time.time())}"

        response = requests.post(
            f"{self.base}/payment/stk-push",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "amount": clean_amount, 
                "phone": phone,
                "callback_url": callback_url,
                "description": unique_reference,  # This is now perfectly safe
            },
            timeout=20
        )
        
        # If Tuma returns 400, this helps us see exactly why in the logs
        if response.status_code != 200:
            logging.error(f"TUMA API ERROR: {response.text}")
            
        response.raise_for_status()
        return response.json()

    def query_payment_status(self, token, merchant_request_id=None, checkout_request_id=None):
        """
        Query the status of a payment transaction.
        Useful for manual reconciliation or debugging.
        
        Args:
            token: Child business authentication token
            merchant_request_id: Merchant request ID from stk_push response
            checkout_request_id: Checkout request ID from stk_push response
            
        Returns:
            dict: Payment status details
        """
        if not merchant_request_id and not checkout_request_id:
            raise TumaError("Either merchant_request_id or checkout_request_id is required")
        
        payload = {}
        if merchant_request_id:
            payload["merchant_request_id"] = merchant_request_id
        if checkout_request_id:
            payload["checkout_request_id"] = checkout_request_id
        
        response = requests.post(
            f"{self.base}/payment/query",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=20
        )
        response.raise_for_status()
        return response.json()