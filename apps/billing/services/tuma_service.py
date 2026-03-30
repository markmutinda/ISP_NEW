# apps/billing/services/tuma_service.py
import requests
from django.conf import settings

class TumaError(Exception):
    pass

class TumaClient:
    def __init__(self):
        self.base = settings.TUMA_API_BASE_URL.rstrip("/")

    def auth_token(self, email, api_key):
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
        response = requests.get(
            f"{self.base}/reference/banks",
            headers={"Authorization": f"Bearer {token}"},
            timeout=20
        )
        response.raise_for_status()
        return response.json()

    def create_business(self, token, body):
        response = requests.post(
            f"{self.base}/businesses",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            timeout=20
        )
        response.raise_for_status()
        return response.json()

    def stk_push(self, token, amount, phone, callback_url, description):
        response = requests.post(
            f"{self.base}/payment/stk-push",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "amount": float(amount),
                "phone": phone,
                "callback_url": callback_url,
                "description": description,
            },
            timeout=20
        )
        response.raise_for_status()
        return response.json()