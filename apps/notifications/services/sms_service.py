import logging
from django.conf import settings
from typing import Dict, List, Optional, Tuple
import uuid

logger = logging.getLogger(__name__)

# Simple simulated services for development
class SimpleAfricasTalkingService:
    def send_sms(self, to: str, message: str, sender_id: Optional[str] = None) -> Tuple[bool, str, Dict]:
        logger.info(f"[SMS SIMULATION] To: {to}")
        logger.info(f"   Message: {message[:50]}...")
        return True, f"SMS_{uuid.uuid4().hex[:8]}", {'simulated': True}
    
    def check_delivery_status(self, message_id: str) -> Dict:
        return {'status': 'delivered', 'simulated': True}
    
    def get_balance(self) -> Dict:
        return {'balance': 999, 'currency': 'KES', 'simulated': True}

class SimpleTwilioService:
    def send_sms(self, to: str, message: str, sender_id: Optional[str] = None) -> Tuple[bool, str, Dict]:
        logger.info(f"[TWILIO SIMULATION] To: {to}")
        return True, f"TWL_{uuid.uuid4().hex[:8]}", {'simulated': True}
    
    def check_delivery_status(self, message_id: str) -> Dict:
        return {'status': 'delivered', 'simulated': True}
    
    def get_balance(self) -> Dict:
        return {'balance': 100, 'currency': 'USD', 'simulated': True}

class SimpleNexmoService:
    def send_sms(self, to: str, message: str, sender_id: Optional[str] = None) -> Tuple[bool, str, Dict]:
        logger.info(f"[NEXMO SIMULATION] To: {to}")
        return True, f"NXM_{uuid.uuid4().hex[:8]}", {'simulated': True}
    
    def check_delivery_status(self, message_id: str) -> Dict:
        return {'status': 'delivered', 'simulated': True}
    
    def get_balance(self) -> Dict:
        return {'balance': 50, 'currency': 'EUR', 'simulated': True}

class SMSService:
    """Service for sending SMS notifications - SIMULATED VERSION"""
    
    def __init__(self):
        self.provider = getattr(settings, 'SMS_PROVIDER', 'simulated')
        self.config = getattr(settings, 'SMS_CONFIG', {})
        
        # Initialize simulated services
        if self.provider == 'africastalking':
            self.backend = SimpleAfricasTalkingService()
        elif self.provider == 'twilio':
            self.backend = SimpleTwilioService()
        elif self.provider == 'nexmo':
            self.backend = SimpleNexmoService()
        else:
            self.backend = SimpleAfricasTalkingService()  # Default
    
    def send_sms(self, recipient: str, message: str, sender_id: Optional[str] = None, **kwargs):
        """Send SMS using simulated service"""
        return self.backend.send_sms(recipient, message, sender_id)
    
    def send_bulk_sms(self, recipients: List[str], message: str, **kwargs):
        """Send bulk SMS - simulated"""
        results = []
        for recipient in recipients:
            success, msg_id, _ = self.send_sms(recipient, message)
            results.append({
                'recipient': recipient,
                'success': success,
                'message_id': msg_id
            })
        
        return {
            'total': len(recipients),
            'success': sum(1 for r in results if r['success']),
            'failed': sum(1 for r in results if not r['success']),
            'results': results
        }
    
    def check_delivery_status(self, message_id: str):
        """Check delivery status - simulated"""
        return self.backend.check_delivery_status(message_id)
    
    def get_balance(self):
        """Get balance - simulated"""
        return self.backend.get_balance()

# Simple email service for development
class EmailService:
    def send_email(self, recipient, subject, message, **kwargs):
        logger.info(f"[EMAIL SIMULATION] To: {recipient}")
        logger.info(f"   Subject: {subject}")
        return True, f"EMAIL_{uuid.uuid4().hex[:8]}", {'simulated': True}

# Simple push service for development
class PushNotificationService:
    def send_push(self, device_tokens, title, body, **kwargs):
        logger.info(f"[PUSH SIMULATION] Title: {title}")
        return True, f"PUSH_{uuid.uuid4().hex[:8]}", {'simulated': True}

