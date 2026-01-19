import africastalking
import logging
import requests
from decimal import Decimal
from django.conf import settings
from django.utils import timezone
from typing import List, Dict, Any, Optional, Union

from apps.messaging.models import SMSMessage, SMSTemplate, SMSCampaign
from apps.customers.models import Customer  # if needed for customer lookup

logger = logging.getLogger(__name__)


class SMSService:
    """
    Africa's Talking SMS Service Wrapper
    Handles sending, balance checking, and status updates
    """

    def __init__(self, username: str = None, api_key: str = None):
        """
        Initialize with credentials (falls back to settings if not provided)
        """
        self.username = username or settings.AFRICASTALKING_USERNAME
        self.api_key = api_key or settings.AFRICASTALKING_API_KEY
        self.sender_id = getattr(settings, 'AFRICASTALKING_SENDER_ID', None)

        if not self.username or not self.api_key:
            raise ValueError("Africa's Talking credentials not configured in settings")

        self._initialize_sdk()

    def _initialize_sdk(self):
        """Initialize the Africa's Talking SDK"""
        try:
            africastalking.initialize(username=self.username, api_key=self.api_key)
            self.sms = africastalking.SMS
            logger.info("Africa's Talking SDK initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize Africa's Talking SDK: {str(e)}")
            raise RuntimeError(f"Africa's Talking initialization failed: {str(e)}")

    def _format_phone_number(self, phone: str) -> str:
        """
        Ensure phone number is in international format (e.g. 2547xxxxxxxx)
        """
        phone = ''.join(filter(str.isdigit, str(phone)))
        if phone.startswith('0'):
            phone = '254' + phone[1:]
        elif phone.startswith('7') or phone.startswith('1'):
            phone = '254' + phone
        elif not phone.startswith('254'):
            phone = '254' + phone.lstrip('+')
        return f"+{phone}"

    def send_single(
        self,
        recipient: str,
        message: str,
        template: Optional[SMSTemplate] = None,
        customer: Optional[Customer] = None,
        campaign: Optional[SMSCampaign] = None,
    ) -> Dict[str, Any]:
        """
        Send a single SMS and create a SMSMessage record
        """
        formatted_recipient = self._format_phone_number(recipient)

        # Create message record first (pending)
        sms_message = SMSMessage.objects.create(
            recipient=formatted_recipient,
            recipient_name=customer.user.get_full_name() if customer else None,
            customer=customer,
            message=message,
            status='pending',
            type='single',
            template=template,
            campaign=campaign,
            provider='africastalking',
        )

        try:
            response = self.sms.send(
                message=message,
                recipients=[formatted_recipient],
                sender_id=self.sender_id,
                enqueue=True  # recommended for reliability
            )

            recipient_data = response['SMSMessageData']['Recipients'][0]

            if recipient_data['status'] == 'Success':
                sms_message.mark_sent(
                    message_id=recipient_data['messageId'],
                    cost=Decimal(recipient_data['cost'])
                )
                return {
                    'success': True,
                    'message_id': sms_message.id,
                    'provider_id': recipient_data['messageId'],
                    'cost': recipient_data['cost'],
                    'status': 'sent'
                }
            else:
                sms_message.mark_failed(recipient_data['status'])
                return {
                    'success': False,
                    'error': recipient_data['status'],
                    'status': 'failed'
                }

        except Exception as e:
            logger.error(f"SMS send failed: {str(e)}", exc_info=True)
            sms_message.mark_failed(str(e))
            return {'success': False, 'error': str(e), 'status': 'error'}

    def send_bulk(
        self,
        recipients: List[str],
        message: str,
        template: Optional[SMSTemplate] = None,
        campaign: Optional[SMSCampaign] = None,
    ) -> Dict[str, Any]:
        """
        Send bulk SMS (creates multiple SMSMessage records)
        """
        formatted_recipients = [self._format_phone_number(r) for r in recipients]

        # Create pending records
        messages = []
        for phone in formatted_recipients:
            msg = SMSMessage.objects.create(
                recipient=phone,
                message=message,
                status='pending',
                type='bulk',
                template=template,
                campaign=campaign,
                provider='africastalking',
            )
            messages.append(msg)

        try:
            response = self.sms.send(
                message=message,
                recipients=formatted_recipients,
                sender_id=self.sender_id,
                enqueue=True
            )

            results = {'queued': 0, 'total_cost': Decimal('0.00'), 'messages': []}

            for idx, recipient_data in enumerate(response['SMSMessageData']['Recipients']):
                msg = messages[idx]
                if recipient_data['status'] == 'Success':
                    msg.mark_sent(
                        message_id=recipient_data['messageId'],
                        cost=Decimal(recipient_data['cost'])
                    )
                    results['queued'] += 1
                    results['total_cost'] += Decimal(recipient_data['cost'])
                    results['messages'].append({
                        'id': msg.id,
                        'recipient': msg.recipient,
                        'status': 'sent',
                        'provider_id': recipient_data['messageId']
                    })
                else:
                    msg.mark_failed(recipient_data['status'])
                    results['messages'].append({
                        'id': msg.id,
                        'recipient': msg.recipient,
                        'status': 'failed',
                        'error': recipient_data['status']
                    })

            return results

        except Exception as e:
            logger.error(f"Bulk SMS failed: {str(e)}", exc_info=True)
            for msg in messages:
                msg.mark_failed(str(e))
            return {'success': False, 'error': str(e), 'queued': 0}

    def get_balance(self) -> Dict[str, Any]:
        """
        Fetch current SMS balance from Africa's Talking
        Uses the correct /user endpoint
        """
        try:
            url = "https://api.africastalking.com/version1/user"
            headers = {
                "apiKey": self.api_key,
                "Accept": "application/json"
            }

            response = requests.get(url, headers=headers, timeout=10)
            response.raise_for_status()

            data = response.json()
            balance_str = data.get('balance', '0')

            # Parse "KES 1234.56" or similar
            balance = float(balance_str.replace('KES ', '').strip())

            return {
                'success': True,
                'balance': balance,
                'currency': 'KES',
                'unit_cost': Decimal('0.50'),  # adjust based on your plan
                'units_remaining': int(balance / 0.50),  # rough estimate
                'provider': 'africastalking',
                'last_updated': timezone.now().isoformat()
            }

        except requests.RequestException as e:
            logger.error(f"Failed to fetch SMS balance: {str(e)}")
            return {
                'success': False,
                'error': str(e),
                'balance': 0,
                'currency': 'KES'
            }
        except Exception as e:
            logger.error(f"Unexpected error fetching balance: {str(e)}")
            return {'success': False, 'error': str(e), 'balance': 0}
