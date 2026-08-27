# apps/billing/services/netily_paybill_service.py
import base64
import time
import logging
import requests
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

TOKEN_CACHE_KEY = "netily_paybill_access_token"


class NetilyPaybillError(Exception):
    pass


def _api_base():
    return (
        "https://api.safaricom.co.ke"
        if settings.NETILY_PAYBILL_ENVIRONMENT == "production"
        else "https://sandbox.safaricom.co.ke"
    )


def _get_access_token():
    cached = cache.get(TOKEN_CACHE_KEY)
    if cached:
        return cached
    auth = base64.b64encode(
        f"{settings.NETILY_PAYBILL_CONSUMER_KEY}:{settings.NETILY_PAYBILL_CONSUMER_SECRET}".encode()
    ).decode()
    resp = requests.get(
        f"{_api_base()}/oauth/v1/generate?grant_type=client_credentials",
        headers={"Authorization": f"Basic {auth}"},
        timeout=20,
    )
    resp.raise_for_status()
    token = resp.json()["access_token"]
    cache.set(TOKEN_CACHE_KEY, token, timeout=3300)  # Safaricom tokens last 3600s
    return token


def _timestamp():
    return time.strftime("%Y%m%d%H%M%S")


def _password(timestamp):
    raw = f"{settings.NETILY_PAYBILL_SHORTCODE}{settings.NETILY_PAYBILL_PASSKEY}{timestamp}"
    return base64.b64encode(raw.encode()).decode()


def stk_push(*, amount, phone_number, party_b, account_reference, transaction_desc, transaction_type):
    """
    party_b: destination shortcode — a bank's paybill, tenant's till, or tenant's paybill.
    transaction_type: 'CustomerPayBillOnline' or 'CustomerBuyGoodsOnline'
    """
    token = _get_access_token()
    timestamp = _timestamp()
    payload = {
        "BusinessShortCode": settings.NETILY_PAYBILL_SHORTCODE,
        "Password": _password(timestamp),
        "Timestamp": timestamp,
        "TransactionType": transaction_type,
        "Amount": str(int(round(float(amount)))),
        "PartyA": phone_number,
        "PartyB": party_b,
        "PhoneNumber": phone_number,
        "CallBackURL": settings.NETILY_PAYBILL_CALLBACK_URL,
        "AccountReference": (account_reference or "")[:12],
        "TransactionDesc": (transaction_desc or "Payment")[:13],
    }

    resp = requests.post(
        f"{_api_base()}/mpesa/stkpush/v1/processrequest",
        json=payload,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=25,
    )
    try:
        data = resp.json()
    except ValueError:
        raise NetilyPaybillError(f"Invalid Daraja response: {resp.text[:300]}")

    if resp.status_code != 200 or data.get("ResponseCode") != "0":
        raise NetilyPaybillError(
            data.get("errorMessage") or data.get("ResponseDescription") or "STK push rejected"
        )

    return {
        "merchant_request_id": data.get("MerchantRequestID", ""),
        "checkout_request_id": data.get("CheckoutRequestID", ""),
        "customer_message": data.get("CustomerMessage", ""),
    }


def _normalize_bank_name(text: str) -> str:
    """Normalize bank name for fuzzy matching against BANK_PAYBILL_MAP keys."""
    t = text.lower().replace("bank", "").replace("-", "").replace(" ", "").replace("&", "")
    if "im" in t and len(t) <= 4:
        return "im"
    if "kcb" in t or "kenyacommercial" in t:
        return "kenyacommercial"
    return "".join(c for c in t if c.isalnum())


_NORMALIZED_BANK_MAP = None


def _get_normalized_bank_map():
    """Cache normalized bank map for performance."""
    global _NORMALIZED_BANK_MAP
    if _NORMALIZED_BANK_MAP is None:
        from apps.billing.constants.bank_paybills import BANK_PAYBILL_MAP
        _NORMALIZED_BANK_MAP = {
            _normalize_bank_name(name): (name, paybill)
            for name, paybill in BANK_PAYBILL_MAP.items()
        }
    return _NORMALIZED_BANK_MAP


def resolve_destination(method):
    """
    Returns (party_b, account_reference, transaction_type, description)
    from an InvoiceItemPayment's saved settlement config, or None.
    """
    from apps.billing.constants.bank_paybills import BANK_PAYBILL_MAP

    config = method.config_json or {}
    mtype = method.method_type

    def _get(key, attr=None):
        val = config.get(key) or (getattr(method, attr, "") if attr else "")
        return str(val or "").strip()

    if mtype == "BANK_TRANSFER":
        bank_name = _get("bank_name", "bank_name")
        account_number = _get("account_number", "account_number")
        if not (bank_name and account_number):
            return None
        match = _get_normalized_bank_map().get(_normalize_bank_name(bank_name))
        if not match:
            return None
        canonical_name, paybill = match
        return paybill, account_number, "CustomerPayBillOnline", f"{canonical_name} settlement"

    if mtype == "MPESA_TILL":
        till = _get("till_number", "till_number")
        if not till:
            return None
        return till, "", "CustomerBuyGoodsOnline", "Till settlement"

    if mtype == "MPESA_PAYBILL":
        paybill = _get("paybill_number", "paybill_number")
        if not paybill:
            return None
        return paybill, _get("account_reference") or "", "CustomerPayBillOnline", "Paybill settlement"

    return None