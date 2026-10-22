"""Minimal Daraja STK client for the shop (uses SHOP_MPESA_* or falls back to NETILY_PAYBILL_*)."""
import base64
import logging

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

TIMEOUT = (3.05, 12)
TOKEN_KEY = "shop:daraja:token"


class MpesaError(Exception):
    """Message is safe to show to the customer."""


def _setting(name, fallback):
    return getattr(settings, f"SHOP_MPESA_{name}", "") or getattr(settings, f"NETILY_PAYBILL_{fallback}", "")


def _base_url():
    env = (getattr(settings, "SHOP_MPESA_ENVIRONMENT", "")
           or getattr(settings, "NETILY_PAYBILL_ENVIRONMENT", "production")).lower()
    return "https://api.safaricom.co.ke" if env == "production" else "https://sandbox.safaricom.co.ke"


def callback_url():
    explicit = getattr(settings, "SHOP_MPESA_CALLBACK_URL", "")
    if explicit:
        return explicit
    base = settings.SHOP_PUBLIC_API_BASE.rstrip("/")
    # NOTE: Daraja rejects callback URLs that contain words like "mpesa"/"safaricom".
    return f"{base}/api/v1/shop/webhooks/pay/{settings.SHOP_MPESA_CALLBACK_SECRET}/"


def _token(force=False):
    if not force:
        cached = cache.get(TOKEN_KEY)
        if cached:
            return cached
    key, secret = _setting("CONSUMER_KEY", "CONSUMER_KEY"), _setting("CONSUMER_SECRET", "CONSUMER_SECRET")
    if not key or not secret:
        raise MpesaError("Payments are not configured. Please contact support.")
    try:
        r = requests.get(
            f"{_base_url()}/oauth/v1/generate",
            params={"grant_type": "client_credentials"},
            auth=(key, secret),
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        token = data["access_token"]
    except (requests.RequestException, ValueError, KeyError) as exc:
        logger.error("Shop Daraja token failure: %s", exc)
        raise MpesaError("Could not reach the payment provider. Please try again.")
    cache.set(TOKEN_KEY, token, timeout=max(int(data.get("expires_in", 3599)) - 120, 60))
    return token


def _credentials():
    shortcode = _setting("SHORTCODE", "SHORTCODE")
    passkey = _setting("PASSKEY", "PASSKEY")
    if not shortcode or not passkey:
        raise MpesaError("Payments are not configured. Please contact support.")
    ts = timezone.localtime().strftime("%Y%m%d%H%M%S")
    password = base64.b64encode(f"{shortcode}{passkey}{ts}".encode()).decode()
    return shortcode, ts, password


def _post(path, payload):
    resp = None
    for attempt in (0, 1):
        try:
            resp = requests.post(
                f"{_base_url()}{path}",
                json=payload,
                headers={"Authorization": f"Bearer {_token(force=attempt == 1)}"},
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            logger.error("Shop Daraja request failed (%s): %s", path, exc)
            raise MpesaError("Could not reach the payment provider. Please try again.")
        if resp.status_code == 401 and attempt == 0:
            cache.delete(TOKEN_KEY)
            continue
        break
    try:
        data = resp.json()
    except ValueError:
        data = {}
    return resp.status_code, data


def stk_push(*, phone, amount, reference, description, callback_url):
    shortcode, ts, password = _credentials()
    payload = {
        "BusinessShortCode": shortcode,
        "Password": password,
        "Timestamp": ts,
        "TransactionType": getattr(settings, "SHOP_MPESA_TRANSACTION_TYPE", "CustomerPayBillOnline"),
        "Amount": int(amount),
        "PartyA": phone,
        "PartyB": shortcode,
        "PhoneNumber": phone,
        "CallBackURL": callback_url,
        "AccountReference": reference[:12],
        "TransactionDesc": description[:13],
    }
    status, data = _post("/mpesa/stkpush/v1/processrequest", payload)
    if status != 200 or str(data.get("ResponseCode")) != "0":
        logger.warning("Shop STK push rejected status=%s body=%s", status, data)
        raise MpesaError(data.get("errorMessage") or data.get("ResponseDescription")
                         or "The payment request was rejected. Please try again.")
    return {
        "checkout_request_id": data["CheckoutRequestID"],
        "merchant_request_id": data.get("MerchantRequestID", ""),
        "customer_message": data.get("CustomerMessage", "Check your phone to approve the payment."),
    }


def stk_query(checkout_request_id):
    """Return (result_code:int, desc:str) once Safaricom has decided, else None."""
    shortcode, ts, password = _credentials()
    status, data = _post("/mpesa/stkpushquery/v1/query", {
        "BusinessShortCode": shortcode,
        "Password": password,
        "Timestamp": ts,
        "CheckoutRequestID": checkout_request_id,
    })
    if status != 200 or "ResultCode" not in data:
        return None  # still processing / transient error
    try:
        return int(data["ResultCode"]), str(data.get("ResultDesc", ""))[:255]
    except (TypeError, ValueError):
        return None