import logging
import requests
from django.conf import settings
from typing import Dict, Tuple, Optional

logger = logging.getLogger(__name__)

class AfricasTalkingService:
    """Africa's Talking SMS Service for notifications"""
    
    def __init__(self):
        self.username = getattr(settings, 'AFRICASTALKING_USERNAME', 'sandbox')
        self.api_key = getattr(settings, 'AFRICASTALKING_API_KEY', '')
        self.sender_id = getattr(settings, 'AFRICASTALKING_SENDER_ID', 'ISPMS')
        
        # Base URLs
        self.sms_url = 'https://api.africastalking.com/version1/messaging'
        self.voice_url = 'https://voice.africastalking.com'
        self.payments_url = 'https://payments.africastalking.com'
        
        # Headers
        self.headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/x-www-form-urlencoded',
            'apiKey': self.api_key
        }
    
    def send_sms(self, to: str, message: str, sender_id: Optional[str] = None) -> Tuple[bool, Optional[str], Dict]:
        """
        Send SMS via Africa's Talking
        
        Returns: (success, message_id, response_data)
        """
        try:
            # If in sandbox mode, recipient must be whitelisted
            if self.username == 'sandbox':
                logger.warning("Using Africa's Talking sandbox mode. Recipient must be whitelisted.")
            
            # Prepare payload
            payload = {
                'username': self.username,
                'to': to,
                'message': message,
                'from': sender_id or self.sender_id
            }
            
            # Send request
            response = requests.post(
                self.sms_url,
                headers=self.headers,
                data=payload,
                timeout=30
            )
            
            response_data = response.json()
            
            if response.status_code == 201:
                # Success
                recipients = response_data.get('SMSMessageData', {}).get('Recipients', [])
                if recipients:
                    message_id = recipients[0].get('messageId')
                    return True, message_id, response_data
                else:
                    return True, None, response_data
            else:
                # Error
                error_message = response_data.get('SMSMessageData', {}).get('Message', 'Unknown error')
                logger.error(f"Africa's Talking SMS failed: {error_message}")
                return False, None, response_data
                
        except requests.exceptions.Timeout:
            logger.error("Africa's Talking API timeout")
            return False, None, {'error': 'API timeout'}
        except requests.exceptions.ConnectionError:
            logger.error("Africa's Talking connection error")
            return False, None, {'error': 'Connection error'}
        except Exception as e:
            logger.error(f"Africa's Talking SMS error: {str(e)}")
            return False, None, {'error': str(e)}
    
    def send_bulk_sms(self, recipients: list, message: str, sender_id: Optional[str] = None) -> Dict:
        """Send SMS to multiple recipients"""
        results = {
            'total': len(recipients),
            'success': 0,
            'failed': 0,
            'details': []
        }
        
        for recipient in recipients:
            success, message_id, response = self.send_sms(
                to=recipient,
                message=message,
                sender_id=sender_id
            )
            
            if success:
                results['success'] += 1
            else:
                results['failed'] += 1
            
            results['details'].append({
                'recipient': recipient,
                'success': success,
                'message_id': message_id,
                'error': response.get('error') if not success else None
            })
        
        return results
    
    def check_delivery_status(self, message_id: str) -> Dict:
        """Check SMS delivery status"""
        try:
            # Africa's Talking doesn't have a direct delivery status API
            # You would need to implement callback handling
            return {'status': 'unknown', 'message': 'Delivery status requires callback setup'}
        except Exception as e:
            return {'status': 'error', 'error': str(e)}
    
    def get_balance(self) -> Dict:
        """Get SMS balance from Africa's Talking"""
        try:
            url = f"https://api.africastalking.com/version1/user"
            
            response = requests.get(
                url,
                headers=self.headers,
                params={'username': self.username},
                timeout=30
            )
            
            if response.status_code == 200:
                data = response.json()
                balance = data.get('UserData', {}).get('balance', '0')
                return {
                    'balance': float(balance) if balance.replace('.', '').isdigit() else 0,
                    'currency': 'KES',
                    'data': data
                }
            else:
                return {'balance': 0, 'error': 'Failed to fetch balance'}
                
        except Exception as e:
            logger.error(f"Failed to get Africa's Talking balance: {str(e)}")
            return {'balance': 0, 'error': str(e)}
    
    def validate_phone_number(self, phone_number: str) -> Tuple[bool, Optional[str]]:
        """Validate and format phone number for Africa's Talking"""
        import re
        
        # Remove all non-digit characters
        digits = re.sub(r'\D', '', phone_number)
        
        # Handle Kenyan numbers
        if digits.startswith('0'):
            # Convert 07... to 2547...
            formatted = '254' + digits[1:]
        elif digits.startswith('7'):
            # Convert 7... to 2547...
            formatted = '254' + digits
        elif digits.startswith('254'):
            # Already formatted
            formatted = digits
        else:
            # Add 254 if missing
            formatted = '254' + digits
        
        # Check length
        if len(formatted) != 12:
            return False, None
        
        return True, formatted
    
    def send_voice_call(self, phone_number: str, message: str) -> Tuple[bool, Optional[str], Dict]:
        """Send voice call with message"""
        try:
            url = f"{self.voice_url}/call"
            
            payload = {
                'username': self.username,
                'to': phone_number,
                'from': self.sender_id,
                'text': message
            }
            
            response = requests.post(
                url,
                headers=self.headers,
                data=payload,
                timeout=30
            )
            
            if response.status_code == 200:
                data = response.json()
                call_id = data.get('entries', [{}])[0].get('callId')
                return True, call_id, data
            else:
                return False, None, response.json()
                
        except Exception as e:
            logger.error(f"Africa's Talking voice call error: {str(e)}")
            return False, None, {'error': str(e)}