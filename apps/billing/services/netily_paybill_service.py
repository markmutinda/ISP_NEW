# apps/billing/services/netily_paybill_service.py
import base64
import time
import logging
import threading
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from django.conf import settings
from django.core.cache import cache

logger = logging.getLogger(__name__)

TOKEN_CACHE_KEY = "netily_paybill_access_token"
TOKEN_LOCK_KEY = "netily_paybill_token_fetch_lock"
CIRCUIT_BREAKER_KEY = "netily_paybill_circuit_open"
CIRCUIT_BREAKER_TTL = 20  # seconds — stop hammering Safaricom for a short cooldown


class NetilyPaybillError(Exception):
    pass


# ── Shared session: reuses TCP/TLS connections instead of opening one per call ──
_session = requests.Session()
_adapter = HTTPAdapter(
    pool_connections=20,
    pool_maxsize=20,
    max_retries=Retry(
        total=2,
        connect=2,
        read=1,
        backoff_factor=0.5,          # 0.5s, 1s
        status_forcelist=[502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    ),
)
_session.mount("https://", _adapter)
_session.mount("http://", _adapter)

# Short connect timeout — fail fast instead of blocking a worker for 25s.
# (connect_timeout, read_timeout)
_REQUEST_TIMEOUT = (5, 15)


def _api_base():
    return (
        "https://api.safaricom.co.ke"
        if settings.NETILY_PAYBILL_ENVIRONMENT == "production"
        else "https://sandbox.safaricom.co.ke"
    )


def _circuit_open() -> bool:
    return bool(cache.get(CIRCUIT_BREAKER_KEY))


def _trip_circuit():
    cache.set(CIRCUIT_BREAKER_KEY, True, timeout=CIRCUIT_BREAKER_TTL)


def _get_access_token():
    cached = cache.get(TOKEN_CACHE_KEY)
    if cached:
        return cached

    if _circuit_open():
        raise NetilyPaybillError("Payment gateway is temporarily unavailable. Please retry shortly.")

    # Prevent thundering herd: only one process fetches a fresh token at a time.
    got_lock = cache.add(TOKEN_LOCK_KEY, "1", timeout=15)
    if not got_lock:
        # Someone else is fetching — wait briefly for them to populate the cache.
        for _ in range(20):
            time.sleep(0.25)
            cached = cache.get(TOKEN_CACHE_KEY)
            if cached:
                return cached
        raise NetilyPaybillError("Payment gateway is busy. Please retry.")

    try:
        cached = cache.get(TOKEN_CACHE_KEY)
        if cached:
            return cached

        auth = base64.b64encode(
            f"{settings.NETILY_PAYBILL_CONSUMER_KEY}:{settings.NETILY_PAYBILL_CONSUMER_SECRET}".encode()
        ).decode()
        try:
            resp = _session.get(
                f"{_api_base()}/oauth/v1/generate?grant_type=client_credentials",
                headers={"Authorization": f"Basic {auth}"},
                timeout=_REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
        except requests.exceptions.RequestException as exc:
            _trip_circuit()
            logger.error("Netily paybill token fetch failed: %s", exc)
            raise NetilyPaybillError("Could not authenticate with payment gateway.") from exc

        token = resp.json()["access_token"]
        cache.set(TOKEN_CACHE_KEY, token, timeout=3300)
        return token
    finally:
        cache.delete(TOKEN_LOCK_KEY)


def _timestamp():
    return time.strftime("%Y%m%d%H%M%S")


def _password(timestamp):
    raw = f"{settings.NETILY_PAYBILL_SHORTCODE}{settings.NETILY_PAYBILL_PASSKEY}{timestamp}"
    return base64.b64encode(raw.encode()).decode()


def stk_push(*, amount, phone_number, party_b, account_reference, transaction_desc, transaction_type, callback_url=None):
    """
    party_b: destination shortcode — a bank's paybill, tenant's till, or tenant's paybill.
    transaction_type: 'CustomerPayBillOnline' or 'CustomerBuyGoodsOnline'
    callback_url: override default Netily callback URL (optional)
    """
    if _circuit_open():
        raise NetilyPaybillError("Payment gateway is temporarily unavailable. Please retry shortly.")

    token = _get_access_token()
    timestamp = _timestamp()
    account_ref = account_reference or ""

    payload = {
        "BusinessShortCode": settings.NETILY_PAYBILL_SHORTCODE,
        "Password": _password(timestamp),
        "Timestamp": timestamp,
        "TransactionType": transaction_type,
        "Amount": str(int(round(float(amount)))),
        "PartyA": phone_number,
        "PartyB": party_b,
        "PhoneNumber": phone_number,
        "CallBackURL": callback_url or settings.NETILY_PAYBILL_CALLBACK_URL,
        "AccountReference": account_ref,
        "TransactionDesc": (transaction_desc or "Payment")[:13],
    }

    try:
        resp = _session.post(
            f"{_api_base()}/mpesa/stkpush/v1/processrequest",
            json=payload,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=_REQUEST_TIMEOUT,
        )
    except requests.exceptions.ConnectTimeout as exc:
        _trip_circuit()
        logger.warning("Netily paybill STK connect timeout: %s", exc)
        raise NetilyPaybillError(
            "Payment gateway is temporarily unavailable. Please retry in a few seconds."
        ) from exc
    except requests.exceptions.RequestException as exc:
        _trip_circuit()
        logger.error("Netily paybill STK request failed: %s", exc)
        raise NetilyPaybillError("Payment gateway error. Please retry.") from exc

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


def stk_push_own_paybill(*, amount, phone_number, account_reference, transaction_desc, callback_url=None):
    """
    STK push collected directly into Netily's OWN paybill — no tenant/bank
    party_b redirection. Use for platform-internal charges (SMS top-ups,
    subscription fees, etc).
    """
    return stk_push(
        amount=amount,
        phone_number=phone_number,
        party_b=settings.NETILY_PAYBILL_SHORTCODE,
        account_reference=account_reference,
        transaction_desc=transaction_desc,
        transaction_type="CustomerPayBillOnline",
        callback_url=callback_url,
    )


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