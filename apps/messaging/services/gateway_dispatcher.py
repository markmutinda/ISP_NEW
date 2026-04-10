"""
Multi-provider SMS gateway dispatcher.

Reads the active SMSGatewayConfig for the current tenant and routes
send/balance calls to the correct provider SDK.

Supported providers:
  1. Africa's Talking
  2. Twilio
  3. Vonage (Nexmo)
  4. Infobip
  5. Beem Africa
  6. Advanta SMS
  7. Hubtel
"""
import logging
import requests
import time
from decimal import Decimal
from typing import Dict, Any, Optional, Tuple

from django.utils import timezone

logger = logging.getLogger(__name__)


def _fmt_phone(phone: str) -> str:
    """Normalise phone to +2547xxxx format."""
    phone = ''.join(filter(str.isdigit, str(phone)))
    if phone.startswith('0'):
        phone = '254' + phone[1:]
    elif phone.startswith('7') or phone.startswith('1'):
        phone = '254' + phone
    elif not phone.startswith('254'):
        phone = '254' + phone.lstrip('+')
    return f"+{phone}"


# ─── PROVIDER BACKENDS ───────────────────────────────────────────

class AfricasTalkingBackend:
    def __init__(self, api_key: str, username: str, sender_id: str = '', **kw):
        import africastalking
        self.api_key = api_key
        self.username = username
        self.sender_id = sender_id or None
        africastalking.initialize(username=username, api_key=api_key)
        self.sms = africastalking.SMS

    def send(self, to: str, message: str) -> Tuple[bool, str, Decimal]:
        resp = self.sms.send(message=message, recipients=[to],
                             sender_id=self.sender_id, enqueue=True)
        r = resp['SMSMessageData']['Recipients'][0]
        ok = r['status'] == 'Success'
        return ok, r.get('messageId', ''), Decimal(str(r.get('cost', '0')).replace('KES ', ''))

    def get_balance(self) -> Dict[str, Any]:
        resp = requests.get(
            "https://api.africastalking.com/version1/user",
            headers={"apiKey": self.api_key, "Accept": "application/json"},
            params={"username": self.username},
            timeout=10,
        )
        resp.raise_for_status()
        bal_str = resp.json().get('balance', '0')
        balance = float(bal_str.replace('KES ', '').strip())
        return {'balance': balance, 'currency': 'KES'}


class TwilioBackend:
    def __init__(self, api_key: str, api_secret: str, sender_id: str = '', **kw):
        from twilio.rest import Client
        self.client = Client(api_key, api_secret)
        self.from_number = sender_id

    def send(self, to: str, message: str) -> Tuple[bool, str, Decimal]:
        msg = self.client.messages.create(body=message, from_=self.from_number, to=to)
        return True, msg.sid, Decimal('0.00')

    def get_balance(self) -> Dict[str, Any]:
        bal = self.client.api.v2010.account.balance.fetch()
        return {'balance': float(bal.balance), 'currency': bal.currency}


class VonageBackend:
    def __init__(self, api_key: str, api_secret: str, sender_id: str = '', **kw):
        self.api_key = api_key
        self.api_secret = api_secret
        self.sender_id = sender_id or 'NETILY'

    def send(self, to: str, message: str) -> Tuple[bool, str, Decimal]:
        resp = requests.post("https://rest.nexmo.com/sms/json", json={
            "from": self.sender_id, "to": to.lstrip('+'),
            "text": message, "api_key": self.api_key, "api_secret": self.api_secret,
        }, timeout=15)
        data = resp.json()['messages'][0]
        ok = data['status'] == '0'
        return ok, data.get('message-id', ''), Decimal(data.get('message-price', '0'))

    def get_balance(self) -> Dict[str, Any]:
        resp = requests.get("https://rest.nexmo.com/account/get-balance",
                            params={"api_key": self.api_key, "api_secret": self.api_secret}, timeout=10)
        data = resp.json()
        return {'balance': float(data.get('value', 0)), 'currency': 'EUR'}


class InfobipBackend:
    def __init__(self, api_key: str, sender_id: str = '', extra_config: dict = None, **kw):
        self.api_key = api_key
        self.sender_id = sender_id or 'NETILY'
        self.base_url = (extra_config or {}).get('base_url', 'https://api.infobip.com')

    def send(self, to: str, message: str) -> Tuple[bool, str, Decimal]:
        resp = requests.post(f"{self.base_url}/sms/2/text/advanced", json={
            "messages": [{"from": self.sender_id, "destinations": [{"to": to.lstrip('+')}], "text": message}]
        }, headers={"Authorization": f"App {self.api_key}"}, timeout=15)
        data = resp.json()
        msg = data.get('messages', [{}])[0]
        ok = msg.get('status', {}).get('groupName') == 'PENDING'
        return ok, msg.get('messageId', ''), Decimal('0.00')

    def get_balance(self) -> Dict[str, Any]:
        resp = requests.get(f"{self.base_url}/account/1/balance",
                            headers={"Authorization": f"App {self.api_key}"}, timeout=10)
        data = resp.json()
        return {'balance': float(data.get('balance', 0)), 'currency': data.get('currency', 'EUR')}


class BeemBackend:
    def __init__(self, api_key: str, api_secret: str, sender_id: str = '', **kw):
        self.api_key = api_key
        self.api_secret = api_secret
        self.sender_id = sender_id or 'NETILY'

    def send(self, to: str, message: str) -> Tuple[bool, str, Decimal]:
        resp = requests.post("https://apisms.beem.africa/v1/send", json={
            "source_addr": self.sender_id,
            "message": message,
            "recipients": [{"recipient_id": 1, "dest_addr": to.lstrip('+')}],
            "encoding": 0,
        }, auth=(self.api_key, self.api_secret), timeout=15)
        data = resp.json()
        ok = data.get('code') == 100
        return ok, str(data.get('request_id', '')), Decimal('0.00')

    def get_balance(self) -> Dict[str, Any]:
        resp = requests.get("https://apisms.beem.africa/public/v1/vendors/balance",
                            auth=(self.api_key, self.api_secret), timeout=10)
        data = resp.json()
        return {'balance': float(data.get('data', {}).get('credit_balance', 0)), 'currency': 'TZS'}


class AdvantaBackend:
    def __init__(self, api_key: str, extra_config: dict = None, sender_id: str = '', **kw):
        self.api_key = api_key
        self.sender_id = sender_id or 'NETILY'
        self.partner_id = (extra_config or {}).get('partner_id', '')

    def send(self, to: str, message: str) -> Tuple[bool, str, Decimal]:
        resp = requests.post("https://quicksms.advantasms.com/api/services/sendsms/", json={
            "apikey": self.api_key, "partnerID": self.partner_id,
            "message": message, "shortcode": self.sender_id,
            "mobile": to.lstrip('+'),
        }, timeout=15)
        data = resp.json()
        ok = 'success' in str(data.get('responses', [{}])[0].get('response-description', '')).lower()
        return ok, str(data.get('responses', [{}])[0].get('messageid', '')), Decimal('0.00')

    def get_balance(self) -> Dict[str, Any]:
        resp = requests.post("https://quicksms.advantasms.com/api/services/getbalance/",
                             json={"apikey": self.api_key, "partnerID": self.partner_id}, timeout=10)
        data = resp.json()
        return {'balance': float(data.get('credit', 0)), 'currency': 'KES'}


class HubtelBackend:
    def __init__(self, api_key: str, api_secret: str, sender_id: str = '', **kw):
        self.client_id = api_key
        self.client_secret = api_secret
        self.sender_id = sender_id or 'NETILY'

    def send(self, to: str, message: str) -> Tuple[bool, str, Decimal]:
        resp = requests.post("https://smsc.hubtel.com/v1/messages/send", params={
            "clientid": self.client_id, "clientsecret": self.client_secret,
            "from": self.sender_id, "to": to.lstrip('+'), "content": message,
        }, timeout=15)
        data = resp.json()
        ok = data.get('status') == 0
        return ok, data.get('messageId', ''), Decimal(str(data.get('rate', '0')))

    def get_balance(self) -> Dict[str, Any]:
        return {'balance': 0, 'currency': 'GHS', 'note': 'Check Hubtel dashboard'}


# ─── PROVIDER REGISTRY ─────────────────────────────────────────

BACKENDS = {
    'africastalking': AfricasTalkingBackend,
    'twilio': TwilioBackend,
    'vonage': VonageBackend,
    'infobip': InfobipBackend,
    'beem': BeemBackend,
    'advanta': AdvantaBackend,
    'hubtel': HubtelBackend,
}

# Human-readable field labels per provider
PROVIDER_FIELDS = {
    'africastalking': {'api_key': 'API Key', 'username': 'Username', 'sender_id': 'Sender ID'},
    'twilio':         {'api_key': 'Account SID', 'api_secret': 'Auth Token', 'sender_id': 'From Number'},
    'vonage':         {'api_key': 'API Key', 'api_secret': 'API Secret', 'sender_id': 'Sender ID'},
    'infobip':        {'api_key': 'API Key', 'sender_id': 'Sender ID'},
    'beem':           {'api_key': 'API Key', 'api_secret': 'Secret Key', 'sender_id': 'Sender Name'},
    'advanta':        {'api_key': 'API Key', 'sender_id': 'Short Code'},
    'hubtel':         {'api_key': 'Client ID', 'api_secret': 'Client Secret', 'sender_id': 'Sender ID'},
}


# ─── DISPATCHER ─────────────────────────────────────────────────

class GatewayDispatcher:
    """
    Reads the active SMSGatewayConfig for the current tenant and dispatches
    send/balance calls to the matching provider backend.
    """

    def __init__(self):
        from apps.messaging.models import SMSGatewayConfig
        self.config = SMSGatewayConfig.objects.filter(is_active=True).first()
        if not self.config:
            raise ValueError("No active SMS gateway configured. Go to Settings → SMS to set one up.")
        cls = BACKENDS.get(self.config.provider)
        if not cls:
            raise ValueError(f"Unknown SMS provider: {self.config.provider}")
        self.backend = cls(
            api_key=self.config.api_key,
            api_secret=self.config.api_secret,
            username=self.config.username,
            sender_id=self.config.sender_id,
            extra_config=self.config.extra_config,
        )

    def send_sms(self, to: str, message: str) -> Dict[str, Any]:
        phone = _fmt_phone(to)
        try:
            ok, msg_id, cost = self.backend.send(phone, message)
            return {'success': ok, 'provider_id': msg_id, 'cost': cost, 'status': 'sent' if ok else 'failed'}
        except Exception as e:
            logger.error(f"[{self.config.provider}] SMS send failed: {e}")
            return {'success': False, 'error': str(e), 'status': 'failed'}

    def get_balance(self) -> Dict[str, Any]:
        try:
            data = self.backend.get_balance()
            data['success'] = True
            data['provider'] = self.config.provider
            return data
        except Exception as e:
            logger.error(f"[{self.config.provider}] Balance check failed: {e}")
            return {'success': False, 'error': str(e), 'balance': 0, 'currency': 'KES'}

    def get_config_summary(self) -> Dict[str, Any]:
        return {
            'provider': self.config.provider,
            'provider_display': self.config.get_provider_display(),
            'sender_id': self.config.sender_id,
            'is_active': self.config.is_active,
        }
