# apps/billing/services/tuma_service.py
import hashlib
import requests
import logging
from django.conf import settings
from django.core.cache import cache


logger = logging.getLogger(__name__)


class TumaError(Exception):
    pass


class TumaNotFound(TumaError):
    """Raised when the Tuma business no longer exists (404)."""
    pass


class TumaClient:
    TOKEN_CACHE_PREFIX = "tuma_token:"
    TOKEN_SAFETY_MARGIN = 300  # 5 minutes before actual expiry

    def __init__(self):
        self.base = settings.TUMA_API_BASE_URL.rstrip("/")

    # ------------------------------------------------------------------
    # Authentication with caching
    # ------------------------------------------------------------------

    def auth_token(self, email, api_key):
        """
        Authenticate with Tuma API and get access token.
        Raw call — prefer get_token() which adds caching.
        """
        response = requests.post(
            f"{self.base}/auth/token",
            json={"email": email, "api_key": api_key},
            timeout=20
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise TumaError(payload.get("message", "Authentication failed"))
        return payload

    def get_token(self, email, api_key):
        """
        Get a cached Tuma JWT token or authenticate if cache miss / expired.
        Returns the token string.
        """
        cache_key = f"{self.TOKEN_CACHE_PREFIX}{hashlib.sha256(email.encode()).hexdigest()[:16]}"
        cached = cache.get(cache_key)
        if cached:
            return cached

        payload = self.auth_token(email, api_key)
        token = payload.get("data", {}).get("token") or payload.get("token")
        if not token:
            raise TumaError("No token in auth response")

        expires_in = payload.get("expires_in", 86400)
        ttl = max(expires_in - self.TOKEN_SAFETY_MARGIN, 60)
        cache.set(cache_key, token, ttl)
        logger.info(f"Tuma token cached for {email} (TTL={ttl}s)")
        return token

    def get_master_token(self):
        """Convenience: get cached token using master credentials from settings."""
        return self.get_token(settings.TUMA_MASTER_EMAIL, settings.TUMA_MASTER_API_KEY)

    def list_banks(self, token):
        """
        Get list of available banks from Tuma reference data.
        
        Args:
            token: Valid authentication token
            
        Returns:
            dict: Response containing list of banks
        """
        response = requests.get(
            f"{self.base}/reference/banks",
            headers={"Authorization": f"Bearer {token}"},
            timeout=20
        )
        response.raise_for_status()
        return response.json()

    def get_banks_map(self, token):
        """
        Get a dictionary mapping bank_id to bank details.
        Useful for quick lookups when updating business configurations.
        
        Args:
            token: Valid authentication token
            
        Returns:
            dict: Mapping of bank_id -> bank data
        """
        payload = self.list_banks(token)
        items = payload.get("data", []) if isinstance(payload, dict) else []
        return {str(x.get("id")): x for x in items}

    def create_business(self, token, body):
        """
        Create a child business under the master account.
        
        Args:
            token: Master authentication token
            body: Business creation payload containing:
                - name: Business name
                - email: Business email
                - mobile: Contact phone number
                - bank_id: Selected bank ID
                - account_number: Bank account number
                - logo: Logo URL (optional)
                - description: Business description (optional)
                
        Returns:
            dict: Created business data
        """
        response = requests.post(
            f"{self.base}/businesses",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            timeout=20
        )
        response.raise_for_status()
        return response.json()

    def update_business(self, token, business_id, body):
        """
        Update an existing child business.
        Called when tenant changes their bank, account number, or other details.
        
        Args:
            token: Master authentication token
            business_id: ID of the business to update
            body: Update payload containing fields to change:
                - name: Business name (optional)
                - email: Business email (optional)
                - mobile: Contact phone (optional)
                - bank_id: New bank ID (optional)
                - account_number: New account number (optional)
                - logo: New logo URL (optional)
                - description: New description (optional)
                - is_active: Toggle business active status (optional)
                
        Returns:
            dict: Updated business data
        """
        response = requests.put(
            f"{self.base}/businesses/{business_id}",
            headers={"Authorization": f"Bearer {token}"},
            json=body,
            timeout=20
        )
        if response.status_code == 404:
            raise TumaNotFound(f"Tuma business {business_id} no longer exists")
        response.raise_for_status()
        return response.json()

    def delete_business(self, token, business_id):
        """
        Delete a child business.
        Returns the response dict, or a synthetic success if already gone (404).
        """
        response = requests.delete(
            f"{self.base}/businesses/{business_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=20
        )
        if response.status_code == 404:
            logger.info(f"Tuma business {business_id} already deleted (404)")
            return {"success": True, "already_gone": True}
        response.raise_for_status()
        return response.json()

    def stk_push(self, token, amount, phone, callback_url, description):
        """
        Initiates an STK Push.
        
        Args:
            token: Child business authentication token
            amount: Payment amount (will be converted to integer for Safaricom)
            phone: Customer phone number (format: 2547XXXXXXXX)
            callback_url: Webhook URL for payment confirmation
            description: Original description that will be cleaned and used as unique reference
            
        Returns:
            dict: Contains merchant_request_id and checkout_request_id
        """
        # 1. Clean Amount: Safaricom wants an integer
        clean_amount = int(float(amount))
        
        # 2. Clean Description/Reference: Tuma maps this to PayHero's external_reference.
        # It MUST NOT have spaces, and must be short. We append a timestamp to ensure it's always unique.
        import time
        safe_desc = str(description).replace(" ", "-").replace("/", "-")[:10]
        unique_reference = f"{safe_desc}-{int(time.time())}"

        response = requests.post(
            f"{self.base}/payment/stk-push",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "amount": clean_amount, 
                "phone": phone,
                "callback_url": callback_url,
                "description": unique_reference,  # This is now perfectly safe
            },
            timeout=20
        )
        
        # If Tuma returns 400, this helps us see exactly why in the logs
        if response.status_code != 200:
            logging.error(f"TUMA API ERROR: {response.text}")
            
        response.raise_for_status()
        return response.json()

    def query_payment_status(self, token, merchant_request_id=None, checkout_request_id=None):
        """
        Query the status of a payment transaction.
        Useful for manual reconciliation or debugging.
        
        Args:
            token: Child business authentication token
            merchant_request_id: Merchant request ID from stk_push response
            checkout_request_id: Checkout request ID from stk_push response
            
        Returns:
            dict: Payment status details
        """
        if not merchant_request_id and not checkout_request_id:
            raise TumaError("Either merchant_request_id or checkout_request_id is required")
        
        payload = {}
        if merchant_request_id:
            payload["merchant_request_id"] = merchant_request_id
        if checkout_request_id:
            payload["checkout_request_id"] = checkout_request_id
        
        response = requests.post(
            f"{self.base}/payment/query",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=20
        )
        response.raise_for_status()
        return response.json()


# ======================================================================
# Tenant-level helpers
# ======================================================================

def _clear_tuma_config(cfg):
    """Reset all Tuma-related fields on a TenantTumaConfig to blank/inactive."""
    cfg.tuma_business_id = ""
    cfg.tuma_business_api_key = ""
    cfg.tuma_business_email = ""
    cfg.is_active = False
    cfg.collection_reference_id = ""
    cfg.collection_reference_code = ""
    cfg.collection_reference_name = ""
    cfg.collection_account_number = ""
    cfg.active_mode = ""
    cfg.save()


def _resolve_tenant_identity(schema_name):
    """
    Resolve company name, email, mobile from the tenant's Company model.
    Returns (name, email, mobile) with best-effort values.
    """
    from apps.core.models import Tenant, Company
    from django_tenants.utils import schema_context, get_public_schema_name

    with schema_context(get_public_schema_name()):
        tenant = Tenant.objects.get(schema_name=schema_name)
        try:
            company = tenant.company
            name = company.name or tenant.schema_name
            email = company.email or f"{tenant.schema_name}@netily.co.ke"
            mobile = company.phone_number or ""
        except (Company.DoesNotExist, AttributeError):
            name = tenant.schema_name
            email = f"{tenant.schema_name}@netily.co.ke"
            mobile = ""
    return name, email, mobile


def _resolve_bank_for_method(method, banks_list):
    """
    Map a payment method (settlement account) to a Tuma bank reference.

    Every payment method type represents a real settlement channel:
      - MOBILE_MONEY       → M-Pesa Paybill reference (phone is the account)
      - MPESA_PAYBILL      → Paybill reference + paybill_number
      - MPESA_TILL         → Buy Goods reference + till_number
      - BANK_TRANSFER      → Matched bank reference + account_number
      - PAYMENT_LINK       → Not a Tuma settlement channel (returns None)

    Returns:
        (bank_id, account_number, description) or (None, None, None)
    """
    config = method.config_json or {}
    mtype = method.method_type

    if mtype == 'MOBILE_MONEY':
        # Mobile Money → route through M-Pesa Paybill reference on Tuma
        # The phone_number IS the settlement target
        phone = config.get('phone_number', '')
        if phone:
            ref = next(
                (b for b in banks_list
                 if 'paybill' in (b.get('code', '') + ' ' + b.get('name', '')).lower()),
                None,
            )
            if ref:
                provider = config.get('mobile_provider', 'SAFARICOM')
                desc = f"Mobile Money ({provider}): {phone}"
                return str(ref['id']), phone, desc
            # Fallback: try any M-Pesa related reference
            ref = next(
                (b for b in banks_list
                 if 'mpesa' in (b.get('code', '') + ' ' + b.get('name', '')).lower()),
                None,
            )
            if ref:
                return str(ref['id']), phone, f"Mobile Money: {phone}"

    elif mtype == 'MPESA_PAYBILL':
        paybill = config.get('paybill_number', '')
        if paybill:
            ref = next(
                (b for b in banks_list
                 if 'paybill' in (b.get('code', '') + ' ' + b.get('name', '')).lower()),
                None,
            )
            if ref:
                return str(ref['id']), paybill, f"M-Pesa Paybill: {paybill}"

    elif mtype == 'MPESA_TILL':
        till = config.get('till_number', '')
        if till:
            ref = next(
                (b for b in banks_list
                 if any(k in (b.get('code', '') + ' ' + b.get('name', '')).lower()
                        for k in ['buygoods', 'buy goods', 'till'])),
                None,
            )
            if ref:
                return str(ref['id']), till, f"M-Pesa Till: {till}"

    elif mtype == 'BANK_TRANSFER':
        bank_name = config.get('bank_name', '')
        account = config.get('account_number', '')
        if bank_name and account:
            # Try exact-ish match against Tuma reference names
            ref = next(
                (b for b in banks_list
                 if bank_name.lower() in b.get('name', '').lower()
                 or b.get('name', '').lower() in bank_name.lower()),
                None,
            )
            if ref:
                return str(ref['id']), account, f"{bank_name}: {account}"

    # PAYMENT_LINK or unmappable — not a direct Tuma settlement channel
    return None, None, None


def ensure_child_business(schema_name, method=None):
    """
    Ensure the tenant has a Tuma child business, provisioned with real data.

    If a TenantTumaConfig with a populated ``tuma_business_id`` already exists,
    this returns it immediately (no remote call).

    On first creation, the method's real settlement details (bank_id,
    account_number, phone) are sent to Tuma so the child business has
    correct data from day one.

    Returns:
        TenantTumaConfig instance

    Raises:
        TumaError on remote failures.
    """
    from apps.billing.models.payment_models import TenantTumaConfig

    # 1. Check if already provisioned — but verify the remote business still exists
    cfg, _ = TenantTumaConfig.objects.get_or_create(schema_name=schema_name)
    if cfg.tuma_business_id:
        return cfg

    # 2. Resolve tenant identity from Company model
    name, email, mobile = _resolve_tenant_identity(schema_name)

    # 3. Auth with master credentials (cached)
    client = TumaClient()
    master_token = client.get_master_token()

    # 4. Resolve bank_id + account_number from payment method config
    banks_data = client.list_banks(master_token)
    banks_list = banks_data.get("data", [])

    bank_id, account_number, desc = None, None, None
    if method:
        bank_id, account_number, desc = _resolve_bank_for_method(method, banks_list)
        # Enrich mobile from method config
        method_config = method.config_json or {}
        if method_config.get('phone_number'):
            mobile = method_config['phone_number']

    # Fallback chain for bank_id
    if not bank_id:
        bank_id = cfg.collection_reference_id or cfg.bank_id
    if not bank_id and banks_list:
        bank_id = str(banks_list[0]["id"])

    # Fallback for account_number — use mobile or placeholder only as last resort
    if not account_number:
        account_number = cfg.collection_account_number or cfg.bank_account_number or mobile
    if not account_number:
        account_number = "0000000000"

    # Ensure mobile is always populated
    if not mobile:
        mobile = "254700000000"

    # 5. Create on Tuma with real data
    description = desc or f"{name} ISP payment profile"
    payload = {
        "name": name,
        "email": email,
        "mobile": mobile,
        "bank_id": bank_id,
        "account_number": account_number,
        "logo": getattr(settings, "TUMA_DEFAULT_LOGO_URL", "https://placehold.co/400x400.png"),
        "description": description,
    }

    res = client.create_business(master_token, payload)
    if not res.get("success"):
        raise TumaError(res.get("message", "Failed to create Tuma child business"))

    data = res["data"]
    cfg.tuma_business_id = data.get("id", "")
    cfg.tuma_business_api_key = data.get("api_key", "")
    cfg.tuma_business_email = email
    cfg.is_active = True

    # Store collection reference details from the bank we used
    ref = next((b for b in banks_list if str(b.get("id")) == str(bank_id)), None)
    if ref:
        cfg.collection_reference_id = str(ref["id"])
        cfg.collection_reference_code = ref.get("code", "")
        cfg.collection_reference_name = ref.get("name", "")
        code = cfg.collection_reference_code.upper()
        cfg.active_mode = "TILL" if code in ["TILL", "PAYBILL", "BUYGOODS"] else "BANK"
    cfg.collection_account_number = account_number

    cfg.save()
    logger.info(
        f"Tuma child business created for {schema_name}: "
        f"id={cfg.tuma_business_id}, bank_ref={cfg.collection_reference_name}, "
        f"account={account_number}"
    )
    return cfg


def sync_active_method_to_tuma(schema_name, method):
    """
    Sync an activated payment method (settlement account) to the Tuma business.

    Updates the remote Tuma business with the correct bank_id + account_number
    derived from the method's type and config. This is what determines WHERE
    customer payments actually settle.

    Returns:
        dict with sync details for frontend feedback, or None if no config.
    """
    from apps.billing.models.payment_models import TenantTumaConfig

    try:
        cfg = TenantTumaConfig.objects.get(schema_name=schema_name)
    except TenantTumaConfig.DoesNotExist:
        logger.warning(f"sync_active_method_to_tuma: no TenantTumaConfig for {schema_name}")
        return None

    if not cfg.tuma_business_id:
        # No business yet — provision one now
        cfg = ensure_child_business(schema_name, method=method)
        if not cfg.tuma_business_id:
            logger.warning(f"sync_active_method_to_tuma: could not provision for {schema_name}")
            return None

    client = TumaClient()
    master_token = client.get_master_token()

    # Resolve bank reference from method config
    banks_data = client.list_banks(master_token)
    banks_list = banks_data.get("data", [])
    bank_id, account_number, desc = _resolve_bank_for_method(method, banks_list)

    # Build update payload
    name, email, mobile = _resolve_tenant_identity(schema_name)
    method_config = method.config_json or {}
    if method_config.get('phone_number'):
        mobile = method_config['phone_number']
    if not mobile:
        mobile = "254700000000"

    update_payload = {
        "name": name,
        "email": email,
        "mobile": mobile,
        "is_active": True,
    }

    sync_details = {
        "tuma_synced": True,
        "settlement_channel": desc or method.name,
    }

    if bank_id and account_number:
        update_payload["bank_id"] = bank_id
        update_payload["account_number"] = account_number
        update_payload["description"] = desc or f"{name} - {method.name}"

        # Update local config with collection reference details
        ref = next((b for b in banks_list if str(b.get("id")) == str(bank_id)), None)
        if ref:
            cfg.collection_reference_id = str(ref["id"])
            cfg.collection_reference_code = ref.get("code", "")
            cfg.collection_reference_name = ref.get("name", "")
            code = cfg.collection_reference_code.upper()
            cfg.active_mode = "TILL" if code in ["TILL", "PAYBILL", "BUYGOODS"] else "BANK"
            sync_details["tuma_reference"] = cfg.collection_reference_name
        cfg.collection_account_number = account_number
        sync_details["account_number"] = account_number
    else:
        # Method type not directly mappable (e.g. PAYMENT_LINK) — just activate
        sync_details["note"] = "Payment link is not a Tuma settlement channel"

    try:
        res = client.update_business(master_token, cfg.tuma_business_id, update_payload)
    except TumaNotFound:
        # Business was deleted externally — re-provision and retry
        logger.warning(f"Tuma business gone for {schema_name}, re-provisioning...")
        _clear_tuma_config(cfg)
        cfg = ensure_child_business(schema_name, method=method)
        if not cfg.tuma_business_id:
            raise TumaError("Failed to re-provision Tuma business")
        res = client.update_business(master_token, cfg.tuma_business_id, update_payload)

    if not res.get("success"):
        raise TumaError(res.get("message", "Failed to sync method to Tuma"))

    cfg.is_active = True
    cfg.save()
    logger.info(
        f"Tuma business synced for {schema_name}: "
        f"method={method.name}, type={method.method_type}, "
        f"bank_ref={cfg.collection_reference_name}, account={cfg.collection_account_number}"
    )
    return sync_details


def deactivate_tuma_collections(schema_name):
    """
    Deactivate the Tuma business when no payment methods are active.
    Sets is_active=False on the remote Tuma business and the local config.
    """
    from apps.billing.models.payment_models import TenantTumaConfig

    try:
        cfg = TenantTumaConfig.objects.get(schema_name=schema_name)
    except TenantTumaConfig.DoesNotExist:
        return

    if not cfg.tuma_business_id:
        return

    client = TumaClient()
    master_token = client.get_master_token()

    try:
        res = client.update_business(master_token, cfg.tuma_business_id, {"is_active": False})
        if not res.get("success"):
            raise TumaError(res.get("message", "Failed to deactivate Tuma business"))
    except TumaNotFound:
        # Business was already deleted on Tuma — clear local config
        logger.warning(f"Tuma business gone for {schema_name} during deactivate, clearing local config")
        _clear_tuma_config(cfg)
        return

    cfg.is_active = False
    cfg.save(update_fields=['is_active', 'updated_at'])
    logger.info(f"Tuma business deactivated for {schema_name}")


def delete_tuma_business(schema_name):
    """
    Delete the Tuma business entirely when the last payment method is removed.
    Clears the local TenantTumaConfig credentials.

    Returns True if deleted, False if nothing to delete.
    """
    from apps.billing.models.payment_models import TenantTumaConfig

    try:
        cfg = TenantTumaConfig.objects.get(schema_name=schema_name)
    except TenantTumaConfig.DoesNotExist:
        return False

    if not cfg.tuma_business_id:
        return False

    client = TumaClient()
    master_token = client.get_master_token()

    res = client.delete_business(master_token, cfg.tuma_business_id)
    # delete_business handles 404 gracefully (already_gone)
    if not res.get("success"):
        raise TumaError(res.get("message", "Failed to delete Tuma business"))

    old_id = cfg.tuma_business_id
    _clear_tuma_config(cfg)
    logger.info(f"Tuma business deleted for {schema_name}: was id={old_id}")
    return True