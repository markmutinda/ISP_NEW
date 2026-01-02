import requests
import json
import base64
from datetime import datetime
from django.utils import timezone
from django.conf import settings
import logging
from decimal import Decimal
import hashlib
import hmac

logger = logging.getLogger(__name__)


class BankTransferService:
    """
    Bank Transfer Integration Service
    Supports multiple Kenyan banks via their APIs
    """
    
    def __init__(self, company=None):
        self.company = company
        self.config = self._get_config()
    
    def _get_config(self):
        """Get bank integration configuration for the company"""
        if self.company and hasattr(self.company, 'bank_integration_config'):
            return self.company.bank_integration_config
        else:
            # Default configuration from settings
            return {
                'default_bank': settings.DEFAULT_BANK,
                'banks': {
                    'equity': {
                        'api_url': settings.EQUITY_BANK_API_URL,
                        'api_key': settings.EQUITY_BANK_API_KEY,
                        'secret_key': settings.EQUITY_BANK_SECRET_KEY,
                        'account_number': settings.EQUITY_BANK_ACCOUNT,
                        'branch_code': settings.EQUITY_BANK_BRANCH,
                    },
                    'kcb': {
                        'api_url': settings.KCB_BANK_API_URL,
                        'client_id': settings.KCB_BANK_CLIENT_ID,
                        'client_secret': settings.KCB_BANK_CLIENT_SECRET,
                        'account_number': settings.KCB_BANK_ACCOUNT,
                    },
                    'coop': {
                        'api_url': settings.COOP_BANK_API_URL,
                        'username': settings.COOP_BANK_USERNAME,
                        'password': settings.COOP_BANK_PASSWORD,
                        'account_number': settings.COOP_BANK_ACCOUNT,
                    },
                    # Add more banks as needed
                }
            }
    
    def verify_bank_account(self, bank_name, account_number, account_name=None):
        """
        Verify bank account details
        
        Args:
            bank_name: Name of the bank
            account_number: Account number to verify
            account_name: Account holder name (optional)
        
        Returns:
            dict: Verification result
        """
        try:
            bank_config = self.config['banks'].get(bank_name.lower())
            if not bank_config:
                return {
                    'success': False,
                    'error': f'Bank {bank_name} not supported'
                }
            
            # Different banks have different verification APIs
            if bank_name.lower() == 'equity':
                return self._verify_equity_account(account_number, account_name, bank_config)
            elif bank_name.lower() == 'kcb':
                return self._verify_kcb_account(account_number, account_name, bank_config)
            elif bank_name.lower() == 'coop':
                return self._verify_coop_account(account_number, account_name, bank_config)
            else:
                return {
                    'success': False,
                    'error': f'Verification not implemented for {bank_name}'
                }
                
        except Exception as e:
            logger.error(f"Error verifying bank account: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def _verify_equity_account(self, account_number, account_name, config):
        """Verify Equity Bank account"""
        try:
            # Equity Bank API implementation
            api_url = f"{config['api_url']}/accounts/verify"
            
            headers = {
                'Authorization': f"Bearer {config['api_key']}",
                'Content-Type': 'application/json',
                'X-Signature': self._generate_equity_signature(config)
            }
            
            payload = {
                'accountNumber': account_number,
                'branchCode': config.get('branch_code', ''),
                'accountName': account_name
            }
            
            response = requests.post(api_url, json=payload, headers=headers)
            
            if response.status_code == 200:
                data = response.json()
                return {
                    'success': True,
                    'is_valid': data.get('isValid', False),
                    'account_name': data.get('accountName'),
                    'branch': data.get('branch'),
                    'currency': data.get('currency', 'KES')
                }
            else:
                return {
                    'success': False,
                    'error': f"API Error: {response.status_code}",
                    'response': response.text
                }
                
        except Exception as e:
            logger.error(f"Error verifying Equity account: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def _verify_kcb_account(self, account_number, account_name, config):
        """Verify KCB Bank account"""
        try:
            # KCB Bank API implementation
            api_url = f"{config['api_url']}/v1/accounts/validate"
            
            # Get access token first
            token_response = requests.post(
                f"{config['api_url']}/oauth/token",
                data={
                    'grant_type': 'client_credentials',
                    'client_id': config['client_id'],
                    'client_secret': config['client_secret']
                }
            )
            
            if token_response.status_code != 200:
                return {
                    'success': False,
                    'error': 'Failed to get access token'
                }
            
            access_token = token_response.json()['access_token']
            
            headers = {
                'Authorization': f"Bearer {access_token}",
                'Content-Type': 'application/json'
            }
            
            payload = {
                'accountNumber': account_number,
                'accountName': account_name
            }
            
            response = requests.post(api_url, json=payload, headers=headers)
            
            if response.status_code == 200:
                data = response.json()
                return {
                    'success': True,
                    'is_valid': data.get('valid', False),
                    'account_name': data.get('accountHolderName'),
                    'branch': data.get('branchName'),
                    'account_type': data.get('accountType')
                }
            else:
                return {
                    'success': False,
                    'error': f"API Error: {response.status_code}",
                    'response': response.text
                }
                
        except Exception as e:
            logger.error(f"Error verifying KCB account: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def _verify_coop_account(self, account_number, account_name, config):
        """Verify Co-operative Bank account"""
        try:
            # Co-op Bank API implementation
            api_url = f"{config['api_url']}/api/validate-account"
            
            # Basic auth
            auth = (config['username'], config['password'])
            
            payload = {
                'accountNo': account_number,
                'accountName': account_name
            }
            
            response = requests.post(api_url, json=payload, auth=auth)
            
            if response.status_code == 200:
                data = response.json()
                return {
                    'success': True,
                    'is_valid': data.get('isValid', False),
                    'account_name': data.get('accountName'),
                    'branch_code': data.get('branchCode'),
                    'branch_name': data.get('branchName')
                }
            else:
                return {
                    'success': False,
                    'error': f"API Error: {response.status_code}",
                    'response': response.text
                }
                
        except Exception as e:
            logger.error(f"Error verifying Co-op account: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def initiate_transfer(self, bank_name, account_number, amount, narration, reference):
        """
        Initiate bank transfer
        
        Args:
            bank_name: Name of the bank
            account_number: Recipient account number
            amount: Amount to transfer
            narration: Transfer description
            reference: Payment reference
        
        Returns:
            dict: Transfer result
        """
        try:
            bank_config = self.config['banks'].get(bank_name.lower())
            if not bank_config:
                return {
                    'success': False,
                    'error': f'Bank {bank_name} not supported'
                }
            
            # Different banks have different transfer APIs
            if bank_name.lower() == 'equity':
                return self._initiate_equity_transfer(account_number, amount, narration, reference, bank_config)
            elif bank_name.lower() == 'kcb':
                return self._initiate_kcb_transfer(account_number, amount, narration, reference, bank_config)
            elif bank_name.lower() == 'coop':
                return self._initiate_coop_transfer(account_number, amount, narration, reference, bank_config)
            else:
                return {
                    'success': False,
                    'error': f'Transfer not implemented for {bank_name}'
                }
                
        except Exception as e:
            logger.error(f"Error initiating bank transfer: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def _initiate_equity_transfer(self, account_number, amount, narration, reference, config):
        """Initiate Equity Bank transfer"""
        try:
            api_url = f"{config['api_url']}/transfers/internal"
            
            headers = {
                'Authorization': f"Bearer {config['api_key']}",
                'Content-Type': 'application/json',
                'X-Signature': self._generate_equity_signature(config)
            }
            
            payload = {
                'sourceAccount': config['account_number'],
                'destinationAccount': account_number,
                'amount': str(amount),
                'currency': 'KES',
                'narration': narration[:50],  # Limit narration length
                'reference': reference,
                'transactionDate': datetime.now().strftime('%Y%m%d')
            }
            
            response = requests.post(api_url, json=payload, headers=headers)
            
            if response.status_code == 200:
                data = response.json()
                return {
                    'success': True,
                    'transaction_id': data.get('transactionId'),
                    'reference': data.get('reference'),
                    'status': data.get('status', 'PROCESSING'),
                    'amount': data.get('amount'),
                    'timestamp': data.get('timestamp')
                }
            else:
                return {
                    'success': False,
                    'error': f"API Error: {response.status_code}",
                    'response': response.text
                }
                
        except Exception as e:
            logger.error(f"Error initiating Equity transfer: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def _initiate_kcb_transfer(self, account_number, amount, narration, reference, config):
        """Initiate KCB Bank transfer"""
        try:
            # Get access token first
            token_response = requests.post(
                f"{config['api_url']}/oauth/token",
                data={
                    'grant_type': 'client_credentials',
                    'client_id': config['client_id'],
                    'client_secret': config['client_secret']
                }
            )
            
            if token_response.status_code != 200:
                return {
                    'success': False,
                    'error': 'Failed to get access token'
                }
            
            access_token = token_response.json()['access_token']
            
            api_url = f"{config['api_url']}/v1/transfers"
            
            headers = {
                'Authorization': f"Bearer {access_token}",
                'Content-Type': 'application/json'
            }
            
            payload = {
                'sourceAccount': config['account_number'],
                'beneficiaryAccount': account_number,
                'amount': str(amount),
                'currency': 'KES',
                'narration': narration,
                'paymentReference': reference,
                'paymentDate': datetime.now().isoformat()
            }
            
            response = requests.post(api_url, json=payload, headers=headers)
            
            if response.status_code == 200:
                data = response.json()
                return {
                    'success': True,
                    'transaction_id': data.get('transactionId'),
                    'payment_reference': data.get('paymentReference'),
                    'status': data.get('status', 'PENDING'),
                    'amount': data.get('amount')
                }
            else:
                return {
                    'success': False,
                    'error': f"API Error: {response.status_code}",
                    'response': response.text
                }
                
        except Exception as e:
            logger.error(f"Error initiating KCB transfer: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def _initiate_coop_transfer(self, account_number, amount, narration, reference, config):
        """Initiate Co-operative Bank transfer"""
        try:
            api_url = f"{config['api_url']}/api/transfer"
            
            auth = (config['username'], config['password'])
            
            payload = {
                'fromAccount': config['account_number'],
                'toAccount': account_number,
                'amount': str(amount),
                'currency': 'KES',
                'description': narration,
                'reference': reference,
                'transactionDate': datetime.now().strftime('%Y-%m-%d')
            }
            
            response = requests.post(api_url, json=payload, auth=auth)
            
            if response.status_code == 200:
                data = response.json()
                return {
                    'success': True,
                    'transaction_id': data.get('transactionId'),
                    'reference': data.get('reference'),
                    'status': data.get('status', 'SUBMITTED'),
                    'amount': data.get('amount')
                }
            else:
                return {
                    'success': False,
                    'error': f"API Error: {response.status_code}",
                    'response': response.text
                }
                
        except Exception as e:
            logger.error(f"Error initiating Co-op transfer: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def check_transfer_status(self, bank_name, transaction_id):
        """
        Check status of a bank transfer
        
        Args:
            bank_name: Name of the bank
            transaction_id: Transaction ID to check
        
        Returns:
            dict: Transfer status
        """
        try:
            bank_config = self.config['banks'].get(bank_name.lower())
            if not bank_config:
                return {
                    'success': False,
                    'error': f'Bank {bank_name} not supported'
                }
            
            # Different banks have different status check APIs
            if bank_name.lower() == 'equity':
                return self._check_equity_status(transaction_id, bank_config)
            elif bank_name.lower() == 'kcb':
                return self._check_kcb_status(transaction_id, bank_config)
            elif bank_name.lower() == 'coop':
                return self._check_coop_status(transaction_id, bank_config)
            else:
                return {
                    'success': False,
                    'error': f'Status check not implemented for {bank_name}'
                }
                
        except Exception as e:
            logger.error(f"Error checking transfer status: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def _check_equity_status(self, transaction_id, config):
        """Check Equity Bank transfer status"""
        try:
            api_url = f"{config['api_url']}/transfers/{transaction_id}/status"
            
            headers = {
                'Authorization': f"Bearer {config['api_key']}",
                'X-Signature': self._generate_equity_signature(config)
            }
            
            response = requests.get(api_url, headers=headers)
            
            if response.status_code == 200:
                data = response.json()
                return {
                    'success': True,
                    'transaction_id': data.get('transactionId'),
                    'status': data.get('status'),
                    'amount': data.get('amount'),
                    'timestamp': data.get('timestamp'),
                    'completed_at': data.get('completedAt')
                }
            else:
                return {
                    'success': False,
                    'error': f"API Error: {response.status_code}",
                    'response': response.text
                }
                
        except Exception as e:
            logger.error(f"Error checking Equity status: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def _check_kcb_status(self, transaction_id, config):
        """Check KCB Bank transfer status"""
        try:
            # Get access token first
            token_response = requests.post(
                f"{config['api_url']}/oauth/token",
                data={
                    'grant_type': 'client_credentials',
                    'client_id': config['client_id'],
                    'client_secret': config['client_secret']
                }
            )
            
            if token_response.status_code != 200:
                return {
                    'success': False,
                    'error': 'Failed to get access token'
                }
            
            access_token = token_response.json()['access_token']
            
            api_url = f"{config['api_url']}/v1/transfers/{transaction_id}/status"
            
            headers = {
                'Authorization': f"Bearer {access_token}"
            }
            
            response = requests.get(api_url, headers=headers)
            
            if response.status_code == 200:
                data = response.json()
                return {
                    'success': True,
                    'transaction_id': data.get('transactionId'),
                    'status': data.get('status'),
                    'amount': data.get('amount'),
                    'payment_date': data.get('paymentDate'),
                    'value_date': data.get('valueDate')
                }
            else:
                return {
                    'success': False,
                    'error': f"API Error: {response.status_code}",
                    'response': response.text
                }
                
        except Exception as e:
            logger.error(f"Error checking KCB status: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def _check_coop_status(self, transaction_id, config):
        """Check Co-operative Bank transfer status"""
        try:
            api_url = f"{config['api_url']}/api/transfers/{transaction_id}/status"
            
            auth = (config['username'], config['password'])
            
            response = requests.get(api_url, auth=auth)
            
            if response.status_code == 200:
                data = response.json()
                return {
                    'success': True,
                    'transaction_id': data.get('transactionId'),
                    'status': data.get('status'),
                    'amount': data.get('amount'),
                    'processed_at': data.get('processedAt'),
                    'confirmation_number': data.get('confirmationNumber')
                }
            else:
                return {
                    'success': False,
                    'error': f"API Error: {response.status_code}",
                    'response': response.text
                }
                
        except Exception as e:
            logger.error(f"Error checking Co-op status: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def _generate_equity_signature(self, config):
        """Generate signature for Equity Bank API"""
        timestamp = datetime.now().strftime('%Y%m%d%H%M%S')
        message = f"{config['api_key']}{timestamp}"
        
        signature = hmac.new(
            config['secret_key'].encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()
        
        return signature
    
    def get_bank_statement(self, bank_name, start_date, end_date):
        """
        Get bank statement for a period
        
        Args:
            bank_name: Name of the bank
            start_date: Start date for statement
            end_date: End date for statement
        
        Returns:
            dict: Bank statement
        """
        try:
            bank_config = self.config['banks'].get(bank_name.lower())
            if not bank_config:
                return {
                    'success': False,
                    'error': f'Bank {bank_name} not supported'
                }
            
            # This would typically call the bank's statement API
            # For now, return a placeholder response
            
            return {
                'success': True,
                'bank': bank_name,
                'account_number': bank_config.get('account_number'),
                'period': f"{start_date} to {end_date}",
                'transactions': [],
                'opening_balance': 0,
                'closing_balance': 0,
                'total_deposits': 0,
                'total_withdrawals': 0
            }
            
        except Exception as e:
            logger.error(f"Error getting bank statement: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def reconcile_payments(self, bank_name, date=None):
        """
        Reconcile bank payments with system records
        
        Args:
            bank_name: Name of the bank
            date: Date to reconcile (defaults to yesterday)
        
        Returns:
            dict: Reconciliation result
        """
        try:
            if not date:
                date = timezone.now().date()
            
            # Get bank statement for the date
            statement = self.get_bank_statement(bank_name, date, date)
            
            if not statement['success']:
                return statement
            
            # Get payments from system for the same date
            from ..models.payment_models import Payment
            system_payments = Payment.objects.filter(
                payment_method__method_type='BANK_TRANSFER',
                payment_date__date=date,
                status='COMPLETED'
            )
            
            # Perform reconciliation
            matched = []
            unmatched = []
            errors = []
            
            # Simplified reconciliation logic
            # In production, this would match transaction references, amounts, etc.
            
            return {
                'success': True,
                'date': date,
                'bank': bank_name,
                'total_transactions': len(statement.get('transactions', [])),
                'system_payments': system_payments.count(),
                'matched': len(matched),
                'unmatched': len(unmatched),
                'errors': len(errors),
                'details': {
                    'matched': matched,
                    'unmatched': unmatched,
                    'errors': errors
                }
            }
            
        except Exception as e:
            logger.error(f"Error reconciling payments: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }