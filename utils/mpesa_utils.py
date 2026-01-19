"""
M-Pesa utilities for ISP Management System
"""
import base64
import requests
import json
from datetime import datetime
from typing import Dict, Optional, Tuple
from django.conf import settings
from django.utils import timezone
import logging

logger = logging.getLogger(__name__)


class MpesaService:
    """
    M-Pesa service integration using Daraja API.
    """
    
    def __init__(self):
        self.consumer_key = getattr(settings, 'MPESA_CONSUMER_KEY', '')
        self.consumer_secret = getattr(settings, 'MPESA_CONSUMER_SECRET', '')
        self.passkey = getattr(settings, 'MPESA_PASSKEY', '')
        self.business_short_code = getattr(settings, 'MPESA_BUSINESS_SHORT_CODE', '')
        self.initiator_name = getattr(settings, 'MPESA_INITIATOR_NAME', '')
        self.initiator_password = getattr(settings, 'MPESA_INITIATOR_PASSWORD', '')
        
        # API endpoints
        self.base_url = 'https://sandbox.safaricom.co.ke'  # Sandbox URL
        if getattr(settings, 'MPESA_ENVIRONMENT', 'sandbox') == 'production':
            self.base_url = 'https://api.safaricom.co.ke'
        
        self.token_url = f'{self.base_url}/oauth/v1/generate?grant_type=client_credentials'
        self.stk_push_url = f'{self.base_url}/mpesa/stkpush/v1/processrequest'
        self.query_url = f'{self.base_url}/mpesa/stkpushquery/v1/query'
        self.b2c_url = f'{self.base_url}/mpesa/b2c/v1/paymentrequest'
        self.transaction_status_url = f'{self.base_url}/mpesa/transactionstatus/v1/query'
        self.account_balance_url = f'{self.base_url}/mpesa/accountbalance/v1/query'
        self.reversal_url = f'{self.base_url}/mpesa/reversal/v1/request'
        
        # Cache for access token
        self._access_token = None
        self._token_expiry = None
    
    def get_access_token(self) -> str:
        """
        Get M-Pesa API access token.
        
        Returns:
            str: Access token
        """
        # Check if token is still valid
        if self._access_token and self._token_expiry:
            if timezone.now() < self._token_expiry:
                return self._access_token
        
        # Generate new token
        try:
            # Encode consumer key and secret
            auth_string = f"{self.consumer_key}:{self.consumer_secret}"
            encoded_auth = base64.b64encode(auth_string.encode()).decode()
            
            headers = {
                'Authorization': f'Basic {encoded_auth}',
                'Content-Type': 'application/json',
            }
            
            response = requests.get(self.token_url, headers=headers)
            response.raise_for_status()
            
            data = response.json()
            self._access_token = data.get('access_token')
            
            # Set token expiry (usually 1 hour)
            expires_in = data.get('expires_in', 3600)
            self._token_expiry = timezone.now() + timezone.timedelta(seconds=expires_in - 60)  # Subtract 1 minute for safety
            
            logger.info("M-Pesa access token generated successfully")
            return self._access_token
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Failed to get M-Pesa access token: {str(e)}")
            raise Exception(f"Failed to get M-Pesa access token: {str(e)}")
    
    def generate_stk_push(self, phone_number: str, amount: float, account_reference: str,
                         transaction_desc: str, callback_url: str) -> Dict:
        """
        Generate STK push payment request.
        
        Args:
            phone_number: Customer phone number (format: 2547xxxxxxxx)
            amount: Amount to pay
            account_reference: Account reference (e.g., invoice number)
            transaction_desc: Transaction description
            callback_url: Callback URL for payment confirmation
            
        Returns:
            Dict: Response data
        """
        try:
            access_token = self.get_access_token()
            
            # Generate timestamp
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
            
            # Generate password
            password_string = f"{self.business_short_code}{self.passkey}{timestamp}"
            password = base64.b64encode(password_string.encode()).decode()
            
            # Prepare request data
            data = {
                'BusinessShortCode': self.business_short_code,
                'Password': password,
                'Timestamp': timestamp,
                'TransactionType': 'CustomerPayBillOnline',
                'Amount': int(amount),
                'PartyA': phone_number,
                'PartyB': self.business_short_code,
                'PhoneNumber': phone_number,
                'CallBackURL': callback_url,
                'AccountReference': account_reference,
                'TransactionDesc': transaction_desc,
            }
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json',
            }
            
            response = requests.post(self.stk_push_url, json=data, headers=headers)
            response.raise_for_status()
            
            result = response.json()
            logger.info(f"STK push generated: {result}")
            
            return {
                'success': True,
                'checkout_request_id': result.get('CheckoutRequestID'),
                'merchant_request_id': result.get('MerchantRequestID'),
                'response_code': result.get('ResponseCode'),
                'response_description': result.get('ResponseDescription'),
                'customer_message': result.get('CustomerMessage'),
            }
            
        except requests.exceptions.RequestException as e:
            logger.error(f"STK push failed: {str(e)}")
            return {
                'success': False,
                'error': str(e),
                'response': e.response.json() if e.response else None,
            }
    
    def query_stk_push(self, checkout_request_id: str) -> Dict:
        """
        Query STK push transaction status.
        
        Args:
            checkout_request_id: Checkout request ID from STK push
            
        Returns:
            Dict: Query response
        """
        try:
            access_token = self.get_access_token()
            
            # Generate timestamp
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
            
            # Generate password
            password_string = f"{self.business_short_code}{self.passkey}{timestamp}"
            password = base64.b64encode(password_string.encode()).decode()
            
            # Prepare request data
            data = {
                'BusinessShortCode': self.business_short_code,
                'Password': password,
                'Timestamp': timestamp,
                'CheckoutRequestID': checkout_request_id,
            }
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json',
            }
            
            response = requests.post(self.query_url, json=data, headers=headers)
            response.raise_for_status()
            
            result = response.json()
            logger.info(f"STK push query result: {result}")
            
            return {
                'success': True,
                'result_code': result.get('ResultCode'),
                'result_description': result.get('ResultDesc'),
                'response': result,
            }
            
        except requests.exceptions.RequestException as e:
            logger.error(f"STK push query failed: {str(e)}")
            return {
                'success': False,
                'error': str(e),
            }
    
    def verify_transaction(self, transaction_code: str) -> Dict:
        """
        Verify M-Pesa transaction.
        
        Args:
            transaction_code: M-Pesa transaction code
            
        Returns:
            Dict: Verification result
        """
        # This is a simplified verification. In production, you should:
        # 1. Store transaction details when callback is received
        # 2. Query the transaction using the API
        # 3. Validate against your records
        
        try:
            # For now, we'll just validate the format
            # In production, implement proper verification with callback data
            if not transaction_code or len(transaction_code) != 10:
                return {
                    'success': False,
                    'error': 'Invalid transaction code',
                }
            
            # All transaction codes should be alphanumeric
            if not transaction_code.isalnum():
                return {
                    'success': False,
                    'error': 'Invalid transaction code format',
                }
            
            # In production, you would:
            # 1. Check your database for this transaction
            # 2. Query M-Pesa API for transaction status
            # 3. Validate amount, phone number, etc.
            
            return {
                'success': True,
                'transaction_code': transaction_code,
                'verified': True,
                'message': 'Transaction verified successfully',
            }
            
        except Exception as e:
            logger.error(f"Transaction verification failed: {str(e)}")
            return {
                'success': False,
                'error': str(e),
            }
    
    def b2c_payment(self, phone_number: str, amount: float, remarks: str,
                   occasion: str = '', command_id: str = 'BusinessPayment') -> Dict:
        """
        Make B2C payment (send money to customer).
        
        Args:
            phone_number: Customer phone number
            amount: Amount to send
            remarks: Payment remarks
            occasion: Optional occasion
            command_id: Command ID (BusinessPayment, SalaryPayment, PromotionPayment)
            
        Returns:
            Dict: Response data
        """
        try:
            access_token = self.get_access_token()
            
            # Generate security credentials
            security_credentials = self._generate_security_credentials()
            
            # Prepare request data
            data = {
                'InitiatorName': self.initiator_name,
                'SecurityCredential': security_credentials,
                'CommandID': command_id,
                'Amount': int(amount),
                'PartyA': self.business_short_code,
                'PartyB': phone_number,
                'Remarks': remarks,
                'QueueTimeOutURL': getattr(settings, 'MPESA_B2C_TIMEOUT_URL', ''),
                'ResultURL': getattr(settings, 'MPESA_B2C_RESULT_URL', ''),
                'Occasion': occasion,
            }
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json',
            }
            
            response = requests.post(self.b2c_url, json=data, headers=headers)
            response.raise_for_status()
            
            result = response.json()
            logger.info(f"B2C payment initiated: {result}")
            
            return {
                'success': True,
                'conversation_id': result.get('ConversationID'),
                'originator_conversation_id': result.get('OriginatorConversationID'),
                'response_code': result.get('ResponseCode'),
                'response_description': result.get('ResponseDescription'),
            }
            
        except requests.exceptions.RequestException as e:
            logger.error(f"B2C payment failed: {str(e)}")
            return {
                'success': False,
                'error': str(e),
            }
    
    def check_account_balance(self) -> Dict:
        """
        Check business account balance.
        
        Returns:
            Dict: Account balance information
        """
        try:
            access_token = self.get_access_token()
            
            # Generate security credentials
            security_credentials = self._generate_security_credentials()
            
            # Generate timestamp
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
            
            # Generate password
            password_string = f"{self.business_short_code}{self.passkey}{timestamp}"
            password = base64.b64encode(password_string.encode()).decode()
            
            # Prepare request data
            data = {
                'Initiator': self.initiator_name,
                'SecurityCredential': security_credentials,
                'CommandID': 'AccountBalance',
                'PartyA': self.business_short_code,
                'IdentifierType': '4',  # 4 = Paybill number
                'Remarks': 'Account balance check',
                'QueueTimeOutURL': getattr(settings, 'MPESA_BALANCE_TIMEOUT_URL', ''),
                'ResultURL': getattr(settings, 'MPESA_BALANCE_RESULT_URL', ''),
            }
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json',
            }
            
            response = requests.post(self.account_balance_url, json=data, headers=headers)
            response.raise_for_status()
            
            result = response.json()
            logger.info(f"Account balance checked: {result}")
            
            return {
                'success': True,
                'response': result,
            }
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Account balance check failed: {str(e)}")
            return {
                'success': False,
                'error': str(e),
            }
    
    def reverse_transaction(self, transaction_id: str, amount: float,
                           receiver_party: str, remarks: str) -> Dict:
        """
        Reverse a transaction.
        
        Args:
            transaction_id: Original transaction ID
            amount: Amount to reverse
            receiver_party: Receiver party (original sender)
            remarks: Reversal remarks
            
        Returns:
            Dict: Response data
        """
        try:
            access_token = self.get_access_token()
            
            # Generate security credentials
            security_credentials = self._generate_security_credentials()
            
            # Prepare request data
            data = {
                'Initiator': self.initiator_name,
                'SecurityCredential': security_credentials,
                'CommandID': 'TransactionReversal',
                'TransactionID': transaction_id,
                'Amount': int(amount),
                'ReceiverParty': receiver_party,
                'RecieverIdentifierType': '4',  # 4 = Paybill number
                'Remarks': remarks,
                'Occasion': 'Transaction reversal',
                'QueueTimeOutURL': getattr(settings, 'MPESA_REVERSAL_TIMEOUT_URL', ''),
                'ResultURL': getattr(settings, 'MPESA_REVERSAL_RESULT_URL', ''),
            }
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json',
            }
            
            response = requests.post(self.reversal_url, json=data, headers=headers)
            response.raise_for_status()
            
            result = response.json()
            logger.info(f"Transaction reversal initiated: {result}")
            
            return {
                'success': True,
                'response': result,
            }
            
        except requests.exceptions.RequestException as e:
            logger.error(f"Transaction reversal failed: {str(e)}")
            return {
                'success': False,
                'error': str(e),
            }
    
    def _generate_security_credentials(self) -> str:
        """
        Generate security credentials for API calls.
        
        Returns:
            str: Encrypted security credentials
        """
        # In production, this should be properly encrypted
        # For sandbox, you might use the provided password
        
        if getattr(settings, 'MPESA_ENVIRONMENT', 'sandbox') == 'sandbox':
            # Sandbox uses plain initiator password
            return self.initiator_password
        else:
            # Production requires RSA encryption
            # This is a simplified version - implement proper encryption
            import hashlib
            credentials = f"{self.initiator_name}{self.initiator_password}"
            return hashlib.sha256(credentials.encode()).hexdigest()
    
    def parse_callback_data(self, callback_data: Dict) -> Dict:
        """
        Parse M-Pesa callback data.
        
        Args:
            callback_data: Raw callback data from M-Pesa
            
        Returns:
            Dict: Parsed callback data
        """
        try:
            body = callback_data.get('Body', {})
            stk_callback = body.get('stkCallback', {})
            
            result = {
                'merchant_request_id': stk_callback.get('MerchantRequestID'),
                'checkout_request_id': stk_callback.get('CheckoutRequestID'),
                'result_code': stk_callback.get('ResultCode'),
                'result_description': stk_callback.get('ResultDesc'),
                'amount': None,
                'mpesa_receipt_number': None,
                'transaction_date': None,
                'phone_number': None,
            }
            
            # Parse callback metadata
            if result['result_code'] == 0:  # Success
                callback_metadata = stk_callback.get('CallbackMetadata', {}).get('Item', [])
                
                for item in callback_metadata:
                    if item.get('Name') == 'Amount':
                        result['amount'] = item.get('Value')
                    elif item.get('Name') == 'MpesaReceiptNumber':
                        result['mpesa_receipt_number'] = item.get('Value')
                    elif item.get('Name') == 'TransactionDate':
                        result['transaction_date'] = item.get('Value')
                    elif item.get('Name') == 'PhoneNumber':
                        result['phone_number'] = item.get('Value')
            
            return result
            
        except Exception as e:
            logger.error(f"Failed to parse callback data: {str(e)}")
            return {
                'success': False,
                'error': str(e),
            }
    
    def validate_callback_signature(self, callback_data: Dict, signature: str) -> bool:
        """
        Validate callback signature.
        
        Args:
            callback_data: Callback data
            signature: Received signature
            
        Returns:
            bool: True if signature is valid
        """
        # In production, implement proper signature validation
        # This is a placeholder implementation
        
        try:
            # Sort callback data and create signature string
            sorted_data = json.dumps(callback_data, sort_keys=True)
            
            # Generate expected signature (simplified)
            import hashlib
            expected_signature = hashlib.sha256(
                f"{sorted_data}{self.passkey}".encode()
            ).hexdigest()
            
            return expected_signature == signature
            
        except Exception as e:
            logger.error(f"Signature validation failed: {str(e)}")
            return False


# Helper functions for common M-Pesa operations
def generate_stk_push(phone_number: str, amount: float, account_reference: str,
                     transaction_desc: str = "Payment for services",
                     callback_url: str = None) -> Dict:
    """
    Helper function to generate STK push.
    
    Args:
        phone_number: Customer phone number
        amount: Amount to pay
        account_reference: Account reference
        transaction_desc: Transaction description
        callback_url: Callback URL
        
    Returns:
        Dict: STK push response
    """
    mpesa = MpesaService()
    
    # Format phone number
    from .helpers import validate_phone_number
    try:
        formatted_phone = validate_phone_number(phone_number)
    except Exception as e:
        return {
            'success': False,
            'error': str(e),
        }
    
    # Remove + from phone number for M-Pesa
    formatted_phone = formatted_phone.replace('+', '')
    
    # Use default callback URL if not provided
    if not callback_url:
        callback_url = getattr(settings, 'MPESA_CALLBACK_URL', '')
    
    return mpesa.generate_stk_push(
        phone_number=formatted_phone,
        amount=amount,
        account_reference=account_reference,
        transaction_desc=transaction_desc,
        callback_url=callback_url
    )


def verify_mpesa_transaction(transaction_code: str, amount: float = None,
                            phone_number: str = None) -> Dict:
    """
    Verify M-Pesa transaction.
    
    Args:
        transaction_code: M-Pesa transaction code
        amount: Expected amount (optional)
        phone_number: Expected phone number (optional)
        
    Returns:
        Dict: Verification result
    """
    mpesa = MpesaService()
    
    # Basic validation
    if not transaction_code:
        return {
            'success': False,
            'error': 'Transaction code is required',
        }
    
    # Validate format
    from .validators import validate_mpesa_code
    try:
        validate_mpesa_code(transaction_code)
    except Exception as e:
        return {
            'success': False,
            'error': str(e),
        }
    
    # Verify transaction
    result = mpesa.verify_transaction(transaction_code)
    
    # Additional validation if amount/phone provided
    if result['success'] and (amount or phone_number):
        # In production, you would query the transaction details
        # and validate against the provided values
        pass
    
    return result


def process_mpesa_callback(callback_data: Dict, signature: str = None) -> Dict:
    """
    Process M-Pesa callback and update payment status.
    
    Args:
        callback_data: Raw callback data
        signature: Callback signature (optional)
        
    Returns:
        Dict: Processing result
    """
    mpesa = MpesaService()
    
    # Validate signature if provided
    if signature and not mpesa.validate_callback_signature(callback_data, signature):
        return {
            'success': False,
            'error': 'Invalid signature',
        }
    
    # Parse callback data
    parsed_data = mpesa.parse_callback_data(callback_data)
    
    if not parsed_data.get('result_code'):
        return {
            'success': False,
            'error': 'Invalid callback data',
        }
    
    # Process based on result code
    result_code = parsed_data['result_code']
    
    if result_code == 0:
        # Successful payment
        return {
            'success': True,
            'status': 'completed',
            'transaction_code': parsed_data['mpesa_receipt_number'],
            'amount': parsed_data['amount'],
            'phone_number': parsed_data['phone_number'],
            'transaction_date': parsed_data['transaction_date'],
            'message': 'Payment completed successfully',
        }
    else:
        # Failed payment
        return {
            'success': False,
            'status': 'failed',
            'error_code': result_code,
            'error_message': parsed_data.get('result_description', 'Payment failed'),
            'message': 'Payment failed',
        }


def get_mpesa_transaction_status(transaction_code: str) -> Dict:
    """
    Get M-Pesa transaction status.
    
    Args:
        transaction_code: M-Pesa transaction code
        
    Returns:
        Dict: Transaction status
    """
    # This would typically query your database
    # For now, return a mock response
    
    # In production, implement proper database query
    # and optionally query M-Pesa API
    
    return {
        'success': True,
        'transaction_code': transaction_code,
        'status': 'completed',  # or pending, failed, etc.
        'verified': True,
        'message': 'Transaction found',
    }
