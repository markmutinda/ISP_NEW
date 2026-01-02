import requests
import json
import base64
from datetime import datetime
from django.utils import timezone
from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
import logging
from cryptography.fernet import Fernet
from decimal import Decimal

logger = logging.getLogger(__name__)


class MpesaSTKPush:
    """
    M-Pesa STK Push Integration for Lipa Na M-Pesa Online
    """
    
    def __init__(self, company=None):
        self.company = company
        self.config = self._get_config()
        
    def _get_config(self):
        """Get M-Pesa configuration for the company"""
        if self.company and hasattr(self.company, 'mpesa_config'):
            return self.company.mpesa_config
        else:
            # Default configuration from settings
            return {
                'consumer_key': settings.MPESA_CONSUMER_KEY,
                'consumer_secret': settings.MPESA_CONSUMER_SECRET,
                'business_shortcode': settings.MPESA_BUSINESS_SHORTCODE,
                'passkey': settings.MPESA_PASSKEY,
                'callback_url': settings.MPESA_CALLBACK_URL,
                'environment': settings.MPESA_ENVIRONMENT,
            }
    
    def _get_access_token(self):
        """Get OAuth access token from Safaricom API"""
        try:
            if self.config['environment'] == 'sandbox':
                url = 'https://sandbox.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials'
            else:
                url = 'https://api.safaricom.co.ke/oauth/v1/generate?grant_type=client_credentials'
            
            auth = (self.config['consumer_key'], self.config['consumer_secret'])
            response = requests.get(url, auth=auth)
            
            if response.status_code == 200:
                return response.json()['access_token']
            else:
                logger.error(f"Failed to get access token: {response.text}")
                return None
        except Exception as e:
            logger.error(f"Error getting access token: {str(e)}")
            return None
    
    def _generate_password(self, timestamp):
        """Generate password for STK Push"""
        business_shortcode = self.config['business_shortcode']
        passkey = self.config['passkey']
        
        data = business_shortcode + passkey + timestamp
        encoded = base64.b64encode(data.encode()).decode()
        return encoded
    
    def _encrypt_phone_number(self, phone_number):
        """Encrypt phone number for security"""
        # Remove any non-digit characters
        phone = ''.join(filter(str.isdigit, phone_number))
        
        # Ensure it starts with 254
        if phone.startswith('0'):
            phone = '254' + phone[1:]
        elif phone.startswith('+254'):
            phone = phone[1:]
        
        return phone
    
    def initiate_stk_push(self, phone_number, amount, account_reference, transaction_desc):
        """
        Initiate STK Push payment request
        
        Args:
            phone_number: Customer's phone number
            amount: Amount to charge
            account_reference: Invoice number or customer reference
            transaction_desc: Description of the transaction
        
        Returns:
            dict: Response from M-Pesa API
        """
        try:
            # Get access token
            access_token = self._get_access_token()
            if not access_token:
                return {
                    'success': False,
                    'message': 'Failed to authenticate with M-Pesa API'
                }
            
            # Prepare request
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
            password = self._generate_password(timestamp)
            encrypted_phone = self._encrypt_phone_number(phone_number)
            
            payload = {
                "BusinessShortCode": self.config['business_shortcode'],
                "Password": password,
                "Timestamp": timestamp,
                "TransactionType": "CustomerPayBillOnline",
                "Amount": str(int(amount)),  # M-Pesa expects integer amount
                "PartyA": encrypted_phone,
                "PartyB": self.config['business_shortcode'],
                "PhoneNumber": encrypted_phone,
                "CallBackURL": self.config['callback_url'],
                "AccountReference": account_reference[:12],  # Max 12 characters
                "TransactionDesc": transaction_desc[:13]  # Max 13 characters
            }
            
            # Determine API URL based on environment
            if self.config['environment'] == 'sandbox':
                url = 'https://sandbox.safaricom.co.ke/mpesa/stkpush/v1/processrequest'
            else:
                url = 'https://api.safaricom.co.ke/mpesa/stkpush/v1/processrequest'
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            # Send request
            response = requests.post(url, json=payload, headers=headers)
            response_data = response.json()
            
            if response.status_code == 200:
                if response_data.get('ResponseCode') == '0':
                    return {
                        'success': True,
                        'message': 'STK Push initiated successfully',
                        'data': {
                            'checkout_request_id': response_data['CheckoutRequestID'],
                            'merchant_request_id': response_data['MerchantRequestID'],
                            'customer_message': response_data['CustomerMessage']
                        }
                    }
                else:
                    return {
                        'success': False,
                        'message': f"M-Pesa Error: {response_data.get('ResponseDescription', 'Unknown error')}",
                        'error_code': response_data.get('ResponseCode')
                    }
            else:
                logger.error(f"STK Push failed: {response.text}")
                return {
                    'success': False,
                    'message': 'Failed to initiate STK Push',
                    'error': response.text
                }
                
        except Exception as e:
            logger.error(f"Error in STK Push: {str(e)}")
            return {
                'success': False,
                'message': f'Internal server error: {str(e)}'
            }
    
    def query_stk_status(self, checkout_request_id):
        """
        Query status of an STK Push transaction
        
        Args:
            checkout_request_id: The checkout request ID from STK Push
        
        Returns:
            dict: Transaction status
        """
        try:
            access_token = self._get_access_token()
            if not access_token:
                return {
                    'success': False,
                    'message': 'Failed to authenticate with M-Pesa API'
                }
            
            timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
            password = self._generate_password(timestamp)
            
            payload = {
                "BusinessShortCode": self.config['business_shortcode'],
                "Password": password,
                "Timestamp": timestamp,
                "CheckoutRequestID": checkout_request_id
            }
            
            if self.config['environment'] == 'sandbox':
                url = 'https://sandbox.safaricom.co.ke/mpesa/stkpushquery/v1/query'
            else:
                url = 'https://api.safaricom.co.ke/mpesa/stkpushquery/v1/query'
            
            headers = {
                'Authorization': f'Bearer {access_token}',
                'Content-Type': 'application/json'
            }
            
            response = requests.post(url, json=payload, headers=headers)
            response_data = response.json()
            
            if response.status_code == 200:
                return {
                    'success': True,
                    'data': response_data
                }
            else:
                return {
                    'success': False,
                    'message': 'Failed to query transaction status',
                    'error': response.text
                }
                
        except Exception as e:
            logger.error(f"Error querying STK status: {str(e)}")
            return {
                'success': False,
                'message': f'Internal server error: {str(e)}'
            }


class MpesaCallback:
    """
    Handle M-Pesa callback responses
    """
    
    @staticmethod
    def handle_stk_callback(data):
        """
        Handle STK Push callback from M-Pesa
        
        Args:
            data: Callback data from M-Pesa
        
        Returns:
            dict: Processed callback data
        """
        try:
            callback_data = data.get('Body', {}).get('stkCallback', {})
            
            result = {
                'merchant_request_id': callback_data.get('MerchantRequestID'),
                'checkout_request_id': callback_data.get('CheckoutRequestID'),
                'result_code': callback_data.get('ResultCode'),
                'result_desc': callback_data.get('ResultDesc'),
                'callback_items': []
            }
            
            # Extract callback items if available
            if 'CallbackMetadata' in callback_data:
                for item in callback_data['CallbackMetadata']['Item']:
                    result['callback_items'].append({
                        'name': item.get('Name'),
                        'value': item.get('Value')
                    })
            
            # Process based on result code
            if result['result_code'] == 0:
                # Successful transaction
                transaction_data = {}
                for item in result['callback_items']:
                    if item['name'] == 'Amount':
                        transaction_data['amount'] = item['value']
                    elif item['name'] == 'MpesaReceiptNumber':
                        transaction_data['mpesa_receipt'] = item['value']
                    elif item['name'] == 'TransactionDate':
                        transaction_data['transaction_date'] = item['value']
                    elif item['name'] == 'PhoneNumber':
                        transaction_data['phone_number'] = item['value']
                
                result['transaction_data'] = transaction_data
                result['status'] = 'SUCCESS'
                
            else:
                # Failed transaction
                result['status'] = 'FAILED'
                result['error_message'] = result['result_desc']
            
            return result
            
        except Exception as e:
            logger.error(f"Error processing M-Pesa callback: {str(e)}")
            return {
                'success': False,
                'message': f'Error processing callback: {str(e)}'
            }
    
    @staticmethod
    def handle_c2b_callback(data):
        """
        Handle C2B callback from M-Pesa
        
        Args:
            data: C2B callback data
        
        Returns:
            dict: Processed callback data
        """
        try:
            result = {
                'transaction_type': data.get('TransactionType'),
                'trans_id': data.get('TransID'),
                'trans_time': data.get('TransTime'),
                'trans_amount': data.get('TransAmount'),
                'business_shortcode': data.get('BusinessShortCode'),
                'bill_ref_number': data.get('BillRefNumber'),
                'invoice_number': data.get('InvoiceNumber'),
                'org_account_balance': data.get('OrgAccountBalance'),
                'third_party_trans_id': data.get('ThirdPartyTransID'),
                'msisdn': data.get('MSISDN'),
                'first_name': data.get('FirstName'),
                'middle_name': data.get('MiddleName'),
                'last_name': data.get('LastName'),
            }
            
            return {
                'success': True,
                'data': result
            }
            
        except Exception as e:
            logger.error(f"Error processing C2B callback: {str(e)}")
            return {
                'success': False,
                'message': f'Error processing callback: {str(e)}'
            }
    
    @staticmethod
    def handle_b2c_callback(data):
        """
        Handle B2C callback from M-Pesa
        
        Args:
            data: B2C callback data
        
        Returns:
            dict: Processed callback data
        """
        try:
            result = {
                'result_type': data.get('ResultType'),
                'result_code': data.get('ResultCode'),
                'result_desc': data.get('ResultDesc'),
                'originator_conversation_id': data.get('OriginatorConversationID'),
                'conversation_id': data.get('ConversationID'),
                'transaction_id': data.get('TransactionID'),
                'transaction_amount': data.get('TransactionAmount'),
                'transaction_receipt': data.get('TransactionReceipt'),
                'b2c_recipient_is_registered_customer': data.get('B2CRecipientIsRegisteredCustomer'),
                'b2c_charges_paid_account_available_funds': data.get('B2CChargesPaidAccountAvailableFunds'),
                'receiver_party_public_name': data.get('ReceiverPartyPublicName'),
                'transaction_completed_date_time': data.get('TransactionCompletedDateTime'),
                'b2c_utility_account_available_funds': data.get('B2CUtilityAccountAvailableFunds'),
                'b2c_working_account_available_funds': data.get('B2CWorkingAccountAvailableFunds'),
            }
            
            return {
                'success': True,
                'data': result
            }
            
        except Exception as e:
            logger.error(f"Error processing B2C callback: {str(e)}")
            return {
                'success': False,
                'message': f'Error processing callback: {str(e)}'
            }


class MpesaValidation:
    """
    Validate M-Pesa transactions and receipts
    """
    
    @staticmethod
    def validate_phone_number(phone_number):
        """
        Validate Kenyan phone number format
        
        Args:
            phone_number: Phone number to validate
        
        Returns:
            tuple: (is_valid, formatted_number, error_message)
        """
        # Remove any non-digit characters
        phone = ''.join(filter(str.isdigit, phone_number))
        
        # Check length
        if len(phone) < 9 or len(phone) > 12:
            return False, None, "Invalid phone number length"
        
        # Convert to 254 format
        if phone.startswith('0'):
            formatted = '254' + phone[1:]
        elif phone.startswith('7') and len(phone) == 9:
            formatted = '254' + phone
        elif phone.startswith('254') and len(phone) == 12:
            formatted = phone
        elif phone.startswith('+254'):
            formatted = phone[1:]
        else:
            return False, None, "Invalid phone number format"
        
        # Final validation
        if formatted.startswith('254') and len(formatted) == 12:
            return True, formatted, None
        else:
            return False, None, "Invalid phone number format"
    
    @staticmethod
    def validate_amount(amount):
        """
        Validate payment amount for M-Pesa
        
        Args:
            amount: Amount to validate
        
        Returns:
            tuple: (is_valid, error_message)
        """
        try:
            amount_decimal = Decimal(str(amount))
            
            # M-Pesa minimum and maximum limits
            if amount_decimal < 1:
                return False, "Amount must be at least KES 1"
            if amount_decimal > 150000:
                return False, "Amount cannot exceed KES 150,000"
            
            # Must be whole number (no decimals for M-Pesa)
            if amount_decimal != amount_decimal.to_integral_value():
                return False, "Amount must be a whole number (no decimals)"
            
            return True, None
            
        except (ValueError, TypeError):
            return False, "Invalid amount format"
    
    @staticmethod
    def validate_receipt_number(receipt_number):
        """
        Validate M-Pesa receipt number format
        
        Args:
            receipt_number: Receipt number to validate
        
        Returns:
            bool: True if valid
        """
        if not receipt_number:
            return False
        
        # M-Pesa receipt numbers are typically alphanumeric, 10 characters
        if len(receipt_number) != 10:
            return False
        
        # Should contain only uppercase letters and digits
        if not receipt_number.isalnum():
            return False
        
        return True
    
    @staticmethod
    def verify_transaction(transaction_data, expected_amount=None, expected_phone=None):
        """
        Verify M-Pesa transaction details
        
        Args:
            transaction_data: Transaction data from callback
            expected_amount: Expected amount (optional)
            expected_phone: Expected phone number (optional)
        
        Returns:
            dict: Verification result
        """
        try:
            result = {
                'is_valid': False,
                'errors': [],
                'warnings': []
            }
            
            # Check required fields
            required_fields = ['mpesa_receipt', 'amount', 'phone_number']
            for field in required_fields:
                if field not in transaction_data:
                    result['errors'].append(f"Missing required field: {field}")
            
            if result['errors']:
                return result
            
            # Validate receipt number
            if not MpesaValidation.validate_receipt_number(transaction_data['mpesa_receipt']):
                result['errors'].append("Invalid M-Pesa receipt number")
            
            # Validate amount
            is_valid_amount, amount_error = MpesaValidation.validate_amount(transaction_data['amount'])
            if not is_valid_amount:
                result['errors'].append(amount_error)
            
            # Validate phone number
            is_valid_phone, formatted_phone, phone_error = MpesaValidation.validate_phone_number(
                transaction_data['phone_number']
            )
            if not is_valid_phone:
                result['errors'].append(phone_error)
            else:
                transaction_data['formatted_phone'] = formatted_phone
            
            # Verify against expected values if provided
            if expected_amount:
                if Decimal(str(transaction_data['amount'])) != Decimal(str(expected_amount)):
                    result['warnings'].append(f"Amount mismatch: expected {expected_amount}, got {transaction_data['amount']}")
            
            if expected_phone:
                is_valid_expected, formatted_expected, _ = MpesaValidation.validate_phone_number(expected_phone)
                if is_valid_expected and is_valid_phone:
                    if formatted_phone != formatted_expected:
                        result['warnings'].append(f"Phone number mismatch: expected {formatted_expected}, got {formatted_phone}")
            
            # Check for duplicate transaction
            from ..models.payment_models import Payment
            duplicate_payments = Payment.objects.filter(
                mpesa_receipt=transaction_data['mpesa_receipt'],
                status='COMPLETED'
            )
            
            if duplicate_payments.exists():
                result['errors'].append(f"Duplicate transaction detected. Receipt {transaction_data['mpesa_receipt']} already used.")
            
            result['is_valid'] = len(result['errors']) == 0
            
            return result
            
        except Exception as e:
            logger.error(f"Error verifying transaction: {str(e)}")
            return {
                'is_valid': False,
                'errors': [f"Verification error: {str(e)}"],
                'warnings': []
            }