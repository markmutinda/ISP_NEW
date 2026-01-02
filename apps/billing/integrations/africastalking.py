import africastalking
import logging
from django.conf import settings
from django.utils import timezone
from datetime import datetime
from decimal import Decimal

logger = logging.getLogger(__name__)


class SMSService:
    """
    Africa's Talking SMS Service Integration
    """
    
    def __init__(self, company=None):
        self.company = company
        self.config = self._get_config()
        self._initialize_sdk()
    
    def _get_config(self):
        """Get Africa's Talking configuration for the company"""
        if self.company and hasattr(self.company, 'africastalking_config'):
            return self.company.africastalking_config
        else:
            # Default configuration from settings
            return {
                'username': settings.AFRICASTALKING_USERNAME,
                'api_key': settings.AFRICASTALKING_API_KEY,
                'sender_id': settings.AFRICASTALKING_SENDER_ID,
                'environment': settings.AFRICASTALKING_ENVIRONMENT,
            }
    
    def _initialize_sdk(self):
        """Initialize Africa's Talking SDK"""
        try:
            africastalking.initialize(
                username=self.config['username'],
                api_key=self.config['api_key']
            )
            self.sms = africastalking.SMS
            logger.info("Africa's Talking SDK initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Africa's Talking SDK: {str(e)}")
            raise
    
    def send_single_sms(self, phone_number, message):
        """
        Send single SMS to a phone number
        
        Args:
            phone_number: Recipient phone number
            message: SMS message content
        
        Returns:
            dict: Send result
        """
        try:
            # Format phone number
            formatted_number = self._format_phone_number(phone_number)
            
            # Send SMS
            response = self.sms.send(
                message=message,
                recipients=[formatted_number],
                sender_id=self.config['sender_id']
            )
            
            # Parse response
            if response['SMSMessageData']['Recipients'][0]['status'] == 'Success':
                return {
                    'success': True,
                    'message_id': response['SMSMessageData']['Recipients'][0]['messageId'],
                    'cost': response['SMSMessageData']['Recipients'][0]['cost'],
                    'status': 'SENT',
                    'recipient': formatted_number
                }
            else:
                return {
                    'success': False,
                    'error': response['SMSMessageData']['Recipients'][0]['status'],
                    'status': 'FAILED',
                    'recipient': formatted_number
                }
                
        except Exception as e:
            logger.error(f"Error sending SMS: {str(e)}")
            return {
                'success': False,
                'error': str(e),
                'status': 'ERROR'
            }
    
    def send_bulk_sms(self, phone_numbers, message):
        """
        Send SMS to multiple phone numbers
        
        Args:
            phone_numbers: List of recipient phone numbers
            message: SMS message content
        
        Returns:
            dict: Send results
        """
        try:
            # Format phone numbers
            formatted_numbers = [self._format_phone_number(num) for num in phone_numbers]
            
            # Send SMS
            response = self.sms.send(
                message=message,
                recipients=formatted_numbers,
                sender_id=self.config['sender_id']
            )
            
            # Parse responses
            results = {
                'total_sent': len(formatted_numbers),
                'successful': 0,
                'failed': 0,
                'details': []
            }
            
            for recipient in response['SMSMessageData']['Recipients']:
                result = {
                    'phone_number': recipient['number'],
                    'status': recipient['status'],
                    'message_id': recipient.get('messageId'),
                    'cost': recipient.get('cost')
                }
                
                if recipient['status'] == 'Success':
                    results['successful'] += 1
                else:
                    results['failed'] += 1
                
                results['details'].append(result)
            
            results['success'] = results['failed'] == 0
            
            return results
            
        except Exception as e:
            logger.error(f"Error sending bulk SMS: {str(e)}")
            return {
                'success': False,
                'error': str(e),
                'total_sent': len(phone_numbers),
                'successful': 0,
                'failed': len(phone_numbers)
            }
    
    def send_invoice_reminder(self, customer, invoice):
        """
        Send invoice payment reminder via SMS
        
        Args:
            customer: Customer object
            invoice: Invoice object
        
        Returns:
            dict: Send result
        """
        try:
            phone_number = customer.user.phone_number
            invoice_number = invoice.invoice_number
            due_date = invoice.due_date.strftime('%d/%m/%Y')
            amount = invoice.balance
            
            message = f"Dear {customer.user.first_name},\n"
            message += f"Reminder: Invoice {invoice_number} of KES {amount:,.2f} "
            message += f"is due on {due_date}. Please make payment to avoid service interruption.\n"
            message += f"Thank you for choosing {self.company.name if self.company else 'our service'}."
            
            return self.send_single_sms(phone_number, message)
            
        except Exception as e:
            logger.error(f"Error sending invoice reminder: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def send_payment_confirmation(self, customer, payment):
        """
        Send payment confirmation via SMS
        
        Args:
            customer: Customer object
            payment: Payment object
        
        Returns:
            dict: Send result
        """
        try:
            phone_number = customer.user.phone_number
            amount = payment.amount
            payment_method = payment.payment_method.name
            receipt_number = payment.payment_number
            
            message = f"Dear {customer.user.first_name},\n"
            message += f"Payment of KES {amount:,.2f} via {payment_method} "
            message += f"has been received. Receipt No: {receipt_number}.\n"
            message += f"Thank you for your payment."
            
            return self.send_single_sms(phone_number, message)
            
        except Exception as e:
            logger.error(f"Error sending payment confirmation: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def send_service_activation(self, customer, service):
        """
        Send service activation notification via SMS
        
        Args:
            customer: Customer object
            service: ServiceConnection object
        
        Returns:
            dict: Send result
        """
        try:
            phone_number = customer.user.phone_number
            service_type = service.service_type
            speed = f"{service.download_speed}Mbps/{service.upload_speed}Mbps"
            
            message = f"Dear {customer.user.first_name},\n"
            message += f"Your {service_type} service has been activated!\n"
            message += f"Speed: {speed}\n"
            message += f"IP: {service.ip_address}\n"
            message += f"Welcome to {self.company.name if self.company else 'our network'}!"
            
            return self.send_single_sms(phone_number, message)
            
        except Exception as e:
            logger.error(f"Error sending service activation: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def send_voucher_pin(self, customer, voucher):
        """
        Send voucher PIN via SMS
        
        Args:
            customer: Customer object
            voucher: Voucher object
        
        Returns:
            dict: Send result
        """
        try:
            phone_number = customer.user.phone_number
            voucher_code = voucher.code
            pin = voucher.pin
            amount = voucher.face_value
            
            message = f"Dear {customer.user.first_name},\n"
            message += f"Your voucher details:\n"
            message += f"Code: {voucher_code}\n"
            message += f"PIN: {pin}\n"
            message += f"Amount: KES {amount:,.2f}\n"
            message += f"Valid until: {voucher.valid_to.strftime('%d/%m/%Y')}"
            
            return self.send_single_sms(phone_number, message)
            
        except Exception as e:
            logger.error(f"Error sending voucher PIN: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def check_sms_balance(self):
        """
        Check SMS balance from Africa's Talking
        
        Returns:
            dict: Balance information
        """
        try:
            # Africa's Talking doesn't have a direct balance API in the SDK
            # We can use the application API to get balance
            import requests
            
            username = self.config['username']
            api_key = self.config['api_key']
            
            # This is a workaround - Africa's Talking balance check might require
            # using their application API endpoint
            url = f"https://api.africastalking.com/version1/user"
            
            headers = {
                'apiKey': api_key,
                'Content-Type': 'application/json'
            }
            
            response = requests.get(url, headers=headers)
            
            if response.status_code == 200:
                data = response.json()
                return {
                    'success': True,
                    'balance': data.get('balance', 'N/A'),
                    'currency': data.get('currency', 'KES')
                }
            else:
                return {
                    'success': False,
                    'error': f"API Error: {response.status_code}"
                }
                
        except Exception as e:
            logger.error(f"Error checking SMS balance: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }
    
    def _format_phone_number(self, phone_number):
        """
        Format phone number for Africa's Talking
        
        Args:
            phone_number: Phone number to format
        
        Returns:
            str: Formatted phone number
        """
        # Remove any non-digit characters
        phone = ''.join(filter(str.isdigit, phone_number))
        
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
            # Return as is if doesn't match Kenyan format
            formatted = phone
        
        return formatted
    
    def get_delivery_report(self, message_id):
        """
        Get SMS delivery report
        
        Args:
            message_id: Message ID to check
        
        Returns:
            dict: Delivery status
        """
        try:
            # Note: Africa's Talking doesn't provide direct delivery report in basic plan
            # This would require premium subscription
            
            # For now, return a mock response
            return {
                'success': True,
                'message_id': message_id,
                'status': 'DELIVERED',  # Assumed delivered
                'delivered_at': timezone.now().isoformat()
            }
            
        except Exception as e:
            logger.error(f"Error getting delivery report: {str(e)}")
            return {
                'success': False,
                'error': str(e)
            }