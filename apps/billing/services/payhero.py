"""
PayHero Payment Gateway Client

This module provides a robust client for interacting with the PayHero API.
All customer payments flow through Netily's PayHero account, with settlements
to individual ISPs after commission deduction.

Documentation: https://payhero.co.ke/docs
"""

import hashlib
import hmac
import json
import logging
import requests
import base64
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional, Union
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


class PayHeroError(Exception):
    """Base exception for PayHero API errors"""
    
    def __init__(self, message: str, code: str = None, response: Dict = None):
        self.message = message
        self.code = code
        self.response = response or {}
        super().__init__(self.message)


class PayHeroAuthError(PayHeroError):
    """Authentication error with PayHero API"""
    pass


class PayHeroValidationError(PayHeroError):
    """Validation error from PayHero API"""
    pass


class PayHeroTimeoutError(PayHeroError):
    """Timeout error when calling PayHero API"""
    pass


class PaymentStatus(Enum):
    """Payment status enum matching PayHero statuses"""
    PENDING = 'pending'
    QUEUED = 'queued'
    SUCCESS = 'success'
    FAILED = 'failed'
    CANCELLED = 'cancelled'
    EXPIRED = 'expired'


class PaymentChannel(Enum):
    """Payment channel types supported by PayHero"""
    MPESA_STK = 'mpesa_stk'
    MPESA_PAYBILL = 'mpesa_paybill'
    MPESA_TILL = 'mpesa_till'
    BANK_TRANSFER = 'bank_transfer'
    CARD = 'card'


@dataclass
class STKPushResponse:
    """Response from STK Push initiation"""
    success: bool
    checkout_request_id: Optional[str]
    reference: Optional[str]
    message: str
    status: PaymentStatus
    raw_response: Dict


@dataclass
class PaymentStatusResponse:
    """Response from payment status query"""
    status: PaymentStatus
    amount: Optional[Decimal]
    mpesa_receipt: Optional[str]
    phone_number: Optional[str]
    completed_at: Optional[datetime]
    failure_reason: Optional[str]
    raw_response: Dict


@dataclass
class B2CPayoutResponse:
    """Response from B2C payout (for ISP settlements)"""
    success: bool
    transaction_id: Optional[str]
    message: str
    raw_response: Dict


class PayHeroClient:
    """
    PayHero API Client for M-Pesa STK Push, Status Queries, and B2C Payouts.
    
    This client is configured with Netily's master PayHero credentials.
    All ISP customer payments flow through this single account, with
    settlements made to ISPs after commission deduction.
    
    Usage:
        client = PayHeroClient()
        response = client.stk_push(
            phone_number='254712345678',
            amount=1000,
            reference='INV-001',
            description='Invoice Payment'
        )
    """
    
    # PayHero API Endpoints
    SANDBOX_BASE_URL = 'https://backend.payhero.co.ke/api/v2'
    PRODUCTION_BASE_URL = 'https://backend.payhero.co.ke/api/v2'
    
    # Default timeout in seconds
    DEFAULT_TIMEOUT = 30
    
    # Netily commission rate (5%)
    COMMISSION_RATE = Decimal('0.05')
    
    def __init__(
        self,
        api_username: str = None,
        api_password: str = None,
        environment: str = None,
        callback_base_url: str = None
    ):
        """
        Initialize PayHero client with credentials.
        
        Args:
            api_username: PayHero API username (defaults to settings)
            api_password: PayHero API password (defaults to settings)
            environment: 'sandbox' or 'production' (defaults to settings)
            callback_base_url: Base URL for webhooks (defaults to settings)
        """
        self.api_username = api_username or getattr(settings, 'PAYHERO_API_USERNAME', '')
        self.api_password = api_password or getattr(settings, 'PAYHERO_API_PASSWORD', '')
        self.environment = environment or getattr(settings, 'PAYHERO_ENVIRONMENT', 'sandbox')
        self.callback_base_url = callback_base_url or getattr(settings, 'PAYHERO_CALLBACK_URL', '')
        
        # Select base URL based on environment
        self.base_url = (
            self.PRODUCTION_BASE_URL 
            if self.environment == 'production' 
            else self.SANDBOX_BASE_URL
        )
        
        # Validate credentials
        if not self.api_username or not self.api_password:
            logger.warning("PayHero credentials not configured")
        else:
            logger.debug(f"PayHero client initialized: env={self.environment}, username={self.api_username[:5]}...")
    
    def _validate_credentials(self):
        """Ensure credentials are configured before making API calls"""
        if not self.api_username or not self.api_password:
            raise PayHeroAuthError(
                message="PayHero credentials not configured. Set PAYHERO_API_USERNAME and PAYHERO_API_PASSWORD in environment.",
                code="MISSING_CREDENTIALS"
            )
    
    def _get_auth_header(self) -> str:
        """Generate Basic Auth header for PayHero API"""
        credentials = f"{self.api_username}:{self.api_password}"
        encoded = base64.b64encode(credentials.encode()).decode()
        return f"Basic {encoded}"
    
    def _make_request(
        self,
        method: str,
        endpoint: str,
        data: Dict = None,
        params: Dict = None,
        timeout: int = None
    ) -> Dict:
        """
        Make HTTP request to PayHero API.
        
        Args:
            method: HTTP method (GET, POST, etc.)
            endpoint: API endpoint path
            data: Request body data
            params: Query parameters
            timeout: Request timeout in seconds
            
        Returns:
            API response as dictionary
            
        Raises:
            PayHeroAuthError: If authentication fails
            PayHeroValidationError: If request validation fails
            PayHeroTimeoutError: If request times out
            PayHeroError: For other API errors
        """
        # Validate credentials before making request
        self._validate_credentials()
        
        url = f"{self.base_url}{endpoint}"
        headers = {
            'Authorization': self._get_auth_header(),
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        }
        
        timeout = timeout or self.DEFAULT_TIMEOUT
        
        logger.info(f"PayHero API Request: {method} {url}")
        logger.debug(f"Request data: {json.dumps(data, default=str) if data else 'None'}")
        
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=headers,
                json=data,
                params=params,
                timeout=timeout
            )
            
            logger.info(f"PayHero API Response: {response.status_code}")
            logger.debug(f"Response body: {response.text[:500]}")
            
            # Handle different response codes
            if response.status_code == 401:
                raise PayHeroAuthError(
                    message="Invalid PayHero credentials",
                    code="AUTH_ERROR",
                    response=response.json() if response.text else {}
                )
            
            if response.status_code == 400:
                error_data = response.json() if response.text else {}
                raise PayHeroValidationError(
                    message=error_data.get('message', 'Validation error'),
                    code="VALIDATION_ERROR",
                    response=error_data
                )
            
            if response.status_code >= 500:
                raise PayHeroError(
                    message="PayHero server error",
                    code="SERVER_ERROR",
                    response=response.json() if response.text else {}
                )
            
            # Parse successful response
            if response.text:
                return response.json()
            return {}
            
        except requests.exceptions.Timeout:
            raise PayHeroTimeoutError(
                message=f"Request to PayHero timed out after {timeout}s",
                code="TIMEOUT"
            )
        except requests.exceptions.ConnectionError as e:
            raise PayHeroError(
                message=f"Failed to connect to PayHero: {str(e)}",
                code="CONNECTION_ERROR"
            )
        except requests.exceptions.RequestException as e:
            raise PayHeroError(
                message=f"PayHero request failed: {str(e)}",
                code="REQUEST_ERROR"
            )
    
    def stk_push(
        self,
        phone_number: str,
        amount: Union[int, float, Decimal],
        reference: str,
        description: str = None,
        callback_url: str = None,
        channel_id: int = None
    ) -> STKPushResponse:
        """
        Initiate M-Pesa STK Push payment.
        
        Args:
            phone_number: Customer phone number (format: 254XXXXXXXXX)
            amount: Payment amount in KES
            reference: Unique payment reference (e.g., invoice number)
            description: Payment description shown to customer
            callback_url: Override default callback URL
            channel_id: PayHero channel ID (if configured)
            
        Returns:
            STKPushResponse with checkout details
            
        Example:
            >>> client = PayHeroClient()
            >>> response = client.stk_push(
            ...     phone_number='254712345678',
            ...     amount=1000,
            ...     reference='INV-001'
            ... )
            >>> print(response.checkout_request_id)
        """
        # Normalize phone number
        phone = self._normalize_phone_number(phone_number)
        
        # Build request payload
        payload = {
            'amount': int(amount),
            'phone_number': phone,
            'channel_id': channel_id or getattr(settings, 'PAYHERO_CHANNEL_ID', 1180),
            'provider': 'm-pesa',
            'external_reference': reference,
            'callback_url': callback_url or self.callback_base_url,
        }
        
        if description:
            payload['description'] = description
        
        try:
            response = self._make_request(
                method='POST',
                endpoint='/payments',
                data=payload
            )
            
            # Parse PayHero response
            success = response.get('success', False) or response.get('status') == 'QUEUED'
            
            return STKPushResponse(
                success=success,
                checkout_request_id=response.get('reference') or response.get('id'),
                reference=reference,
                message=response.get('message', 'STK Push initiated'),
                status=PaymentStatus.QUEUED if success else PaymentStatus.FAILED,
                raw_response=response
            )
            
        except PayHeroError as e:
            logger.error(f"STK Push failed: {e.message}")
            return STKPushResponse(
                success=False,
                checkout_request_id=None,
                reference=reference,
                message=e.message,
                status=PaymentStatus.FAILED,
                raw_response=e.response
            )
    
    def get_payment_status(self, reference: str) -> PaymentStatusResponse:
        """
        Query payment status from PayHero.
        
        Args:
            reference: Payment reference or checkout request ID
            
        Returns:
            PaymentStatusResponse with current status
        """
        try:
            response = self._make_request(
                method='GET',
                endpoint=f'/payments/{reference}'
            )
            
            # Map PayHero status to our enum
            payhero_status = response.get('status', '').upper()
            status_mapping = {
                'SUCCESS': PaymentStatus.SUCCESS,
                'SUCCESSFUL': PaymentStatus.SUCCESS,
                'COMPLETED': PaymentStatus.SUCCESS,
                'PENDING': PaymentStatus.PENDING,
                'QUEUED': PaymentStatus.QUEUED,
                'FAILED': PaymentStatus.FAILED,
                'CANCELLED': PaymentStatus.CANCELLED,
                'EXPIRED': PaymentStatus.EXPIRED,
            }
            
            status = status_mapping.get(payhero_status, PaymentStatus.PENDING)
            
            # Parse completion time
            completed_at = None
            if response.get('completed_at'):
                try:
                    completed_at = datetime.fromisoformat(
                        response['completed_at'].replace('Z', '+00:00')
                    )
                except (ValueError, AttributeError):
                    pass
            
            return PaymentStatusResponse(
                status=status,
                amount=Decimal(str(response.get('amount', 0))) if response.get('amount') else None,
                mpesa_receipt=response.get('provider_reference') or response.get('mpesa_receipt'),
                phone_number=response.get('phone_number'),
                completed_at=completed_at,
                failure_reason=response.get('failure_reason') or response.get('result_description'),
                raw_response=response
            )
            
        except PayHeroError as e:
            logger.error(f"Payment status query failed: {e.message}")
            return PaymentStatusResponse(
                status=PaymentStatus.PENDING,
                amount=None,
                mpesa_receipt=None,
                phone_number=None,
                completed_at=None,
                failure_reason=e.message,
                raw_response=e.response
            )
    
    def b2c_payout(
        self,
        phone_number: str,
        amount: Union[int, float, Decimal],
        reference: str,
        reason: str = "Settlement"
    ) -> B2CPayoutResponse:
        """
        Initiate B2C payout to ISP (for settlements).
        
        Args:
            phone_number: Recipient phone number (format: 254XXXXXXXXX)
            amount: Payout amount in KES
            reference: Unique payout reference
            reason: Reason for payout
            
        Returns:
            B2CPayoutResponse with transaction details
        """
        phone = self._normalize_phone_number(phone_number)
        
        payload = {
            'phone_number': phone,
            'amount': int(amount),
            'reference': reference,
            'reason': reason,
        }
        
        try:
            response = self._make_request(
                method='POST',
                endpoint='/disbursements',
                data=payload
            )
            
            return B2CPayoutResponse(
                success=response.get('success', False),
                transaction_id=response.get('transaction_id') or response.get('reference'),
                message=response.get('message', 'Payout initiated'),
                raw_response=response
            )
            
        except PayHeroError as e:
            logger.error(f"B2C payout failed: {e.message}")
            return B2CPayoutResponse(
                success=False,
                transaction_id=None,
                message=e.message,
                raw_response=e.response
            )
    
    def bank_transfer(
        self,
        bank_code: str,
        account_number: str,
        account_name: str,
        amount: Union[int, float, Decimal],
        reference: str,
        narration: str = "Settlement"
    ) -> B2CPayoutResponse:
        """
        Initiate bank transfer to ISP (for settlements).
        
        Args:
            bank_code: Bank code (e.g., '01' for KCB)
            account_number: Bank account number
            account_name: Account holder name
            amount: Transfer amount in KES
            reference: Unique transfer reference
            narration: Transfer narration
            
        Returns:
            B2CPayoutResponse with transaction details
        """
        payload = {
            'bank_code': bank_code,
            'account_number': account_number,
            'account_name': account_name,
            'amount': int(amount),
            'reference': reference,
            'narration': narration,
        }
        
        try:
            response = self._make_request(
                method='POST',
                endpoint='/bank-transfers',
                data=payload
            )
            
            return B2CPayoutResponse(
                success=response.get('success', False),
                transaction_id=response.get('transaction_id') or response.get('reference'),
                message=response.get('message', 'Bank transfer initiated'),
                raw_response=response
            )
            
        except PayHeroError as e:
            logger.error(f"Bank transfer failed: {e.message}")
            return B2CPayoutResponse(
                success=False,
                transaction_id=None,
                message=e.message,
                raw_response=e.response
            )
    
    def verify_webhook_signature(
        self,
        payload: Union[str, bytes, Dict],
        signature: str,
        secret: str = None
    ) -> bool:
        """
        Verify PayHero webhook signature.
        
        Args:
            payload: Raw request body (string, bytes, or dict)
            signature: Signature from X-PayHero-Signature header
            secret: Webhook secret (defaults to settings)
            
        Returns:
            True if signature is valid, False otherwise
        """
        secret = secret or getattr(settings, 'PAYHERO_WEBHOOK_SECRET', '')
        
        if not secret:
            logger.warning("PayHero webhook secret not configured, skipping verification")
            return True  # Skip verification if no secret configured
        
        # Convert payload to string if necessary
        if isinstance(payload, dict):
            payload = json.dumps(payload, separators=(',', ':'))
        elif isinstance(payload, bytes):
            payload = payload.decode('utf-8')
        
        # Compute HMAC signature
        expected_signature = hmac.new(
            secret.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()
        
        # Compare signatures using constant-time comparison
        return hmac.compare_digest(expected_signature, signature)
    
    def _normalize_phone_number(self, phone: str) -> str:
        """
        Normalize phone number to format 254XXXXXXXXX.
        
        Args:
            phone: Phone number in various formats
            
        Returns:
            Normalized phone number
        """
        # Remove any whitespace and special characters
        phone = ''.join(filter(str.isdigit, str(phone)))
        
        # Handle different formats
        if phone.startswith('0'):
            phone = '254' + phone[1:]
        elif phone.startswith('+'):
            phone = phone[1:]
        elif not phone.startswith('254'):
            phone = '254' + phone
        
        return phone
    
    @staticmethod
    def calculate_commission(amount: Union[int, float, Decimal]) -> Dict[str, Decimal]:
        """
        Calculate Netily commission and ISP amount from payment.
        
        Args:
            amount: Total payment amount
            
        Returns:
            Dictionary with commission_amount and isp_amount
        """
        amount = Decimal(str(amount))
        commission = amount * PayHeroClient.COMMISSION_RATE
        isp_amount = amount - commission
        
        return {
            'total_amount': amount,
            'commission_rate': PayHeroClient.COMMISSION_RATE,
            'commission_amount': commission.quantize(Decimal('0.01')),
            'isp_amount': isp_amount.quantize(Decimal('0.01')),
        }
    
    def test_connection(self) -> Dict[str, Any]:
        """
        Test PayHero API connection.
        
        Returns:
            Dictionary with connection status
        """
        try:
            # Try to get account balance or similar endpoint
            response = self._make_request(
                method='GET',
                endpoint='/merchant/balance'
            )
            
            return {
                'success': True,
                'message': 'PayHero connection successful',
                'environment': self.environment,
                'balance': response.get('balance'),
            }
            
        except PayHeroAuthError:
            return {
                'success': False,
                'message': 'Invalid PayHero credentials',
                'environment': self.environment,
            }
        except PayHeroError as e:
            return {
                'success': False,
                'message': f'PayHero connection failed: {e.message}',
                'environment': self.environment,
            }


# Singleton instance for convenience
def get_payhero_client() -> PayHeroClient:
    """Get a configured PayHero client instance."""
    return PayHeroClient()
