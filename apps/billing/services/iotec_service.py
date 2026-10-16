# apps/billing/services/iotec_service.py
"""
iotec Pay (Uganda mobile money) client + settlement.

Flow: collect -> Pending -> (poll by portal / Celery reconcile) -> settle_payment()
settle_payment is idempotent and row-locked, so poll / reconcile / future webhook can race safely.
"""
import logging
import time
from datetime import timedelta
from urllib.parse import quote

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from django.conf import settings
from django.core.cache import cache
from django.db import connection, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

MIN_AMOUNT = 500
IOTEC_COUNTRY = 'UG'
_TIMEOUT = (5, 15)

_SUCCESS = {'success', 'successful', 'completed'}
_FAILED = {'failed', 'cancelled', 'canceled', 'rejected', 'declined', 'expired',
           'timeout', 'timedout', 'error'}


class IotecError(Exception):
    pass


# POST (collect) is never retried on read errors -> no duplicate charges.
# urllib3 still retries pure connect failures (request never sent), which is safe.
_session = requests.Session()
_session.mount("https://", HTTPAdapter(
    pool_connections=10, pool_maxsize=20,
    max_retries=Retry(total=2, connect=2, read=1, backoff_factor=0.4,
                      status_forcelist=[502, 503, 504],
                      allowed_methods=frozenset(["GET"]), raise_on_status=False),
))


def _api_base():
    return getattr(settings, 'IOTEC_API_BASE_URL', 'https://pay.iotec.io').rstrip('/')


def _error_message(resp):
    try:
        body = resp.json()
        if isinstance(body, dict):
            return str(body.get('message') or body.get('detail') or body.get('title')
                       or body.get('statusMessage') or body)[:300]
        return str(body)[:300]
    except ValueError:
        return (resp.text or f"HTTP {resp.status_code}")[:300]


# ── Auth ───────────────────────────────────────────────────────────────────

def get_access_token(config, force_refresh=False):
    key = config.token_cache_key
    if not force_refresh:
        cached = cache.get(key)
        if cached:
            return cached

    lock_key = f"{key}:lock"
    got_lock = cache.add(lock_key, "1", timeout=15)
    if not got_lock:
        for _ in range(20):                      # another worker is fetching; wait up to 5s
            time.sleep(0.25)
            cached = cache.get(key)
            if cached and not force_refresh:
                return cached
    try:
        form = {
            'grant_type': 'client_credentials',
            'client_id': config.client_id,
            'client_secret': config.get_client_secret(),
        }
        scope = getattr(settings, 'IOTEC_TOKEN_SCOPE', '')
        if scope:
            form['scope'] = scope
        try:
            resp = _session.post(getattr(settings, 'IOTEC_TOKEN_URL', 'https://id.iotec.io/connect/token'),
                                 data=form, timeout=_TIMEOUT)
        except requests.exceptions.RequestException as exc:
            logger.error("iotec token request failed: %s", exc)
            raise IotecError("Payment gateway is temporarily unavailable. Please retry.") from exc
        if resp.status_code != 200:
            logger.warning("iotec token rejected (%s): %s", resp.status_code, _error_message(resp))
            raise IotecError("iotec rejected the credentials. Check Client ID and Client Secret.")
        try:
            data = resp.json()
            token = data['access_token']
        except (ValueError, KeyError) as exc:
            raise IotecError("Unexpected response from iotec authentication.") from exc
        ttl = max(60, int(data.get('expires_in', 3600)) - 120)
        cache.set(key, token, timeout=ttl)
        return token
    finally:
        if got_lock:
            cache.delete(lock_key)


def _api(config, method, path, *, json=None, _retry=True):
    token = get_access_token(config)
    resp = _session.request(
        method, f"{_api_base()}{path}", json=json,
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/json'},
        timeout=_TIMEOUT,
    )
    if resp.status_code == 401 and _retry:
        get_access_token(config, force_refresh=True)
        return _api(config, method, path, json=json, _retry=False)
    return resp


# ── Collect / status ───────────────────────────────────────────────────────

def initiate_collection(config, *, payment, phone_number, amount, external_id,
                        payer_note='', payee_note='', payer_name=''):
    """Starts a mobile-money prompt. Stores iotec's transaction id on the payment."""
    from utils.phone import format_phone_local

    try:
        amount_int = int(round(float(amount)))
    except (TypeError, ValueError):
        raise IotecError("Invalid amount.")
    if amount_int < MIN_AMOUNT:
        raise IotecError(f"Minimum payment is UGX {MIN_AMOUNT}.")

    payer = format_phone_local(phone_number, IOTEC_COUNTRY)
    if not payer:
        raise IotecError("Invalid phone number.")

    payload = {
        'category': 'MobileMoney',
        'currency': 'ITX' if config.is_sandbox else 'UGX',
        'walletId': str(config.wallet_id),
        'externalId': str(external_id)[:100],
        'payer': payer,
        'amount': amount_int,
        'transactionChargesCategory': config.transaction_charges_category,
    }
    if payer_note:
        payload['payerNote'] = payer_note[:100]
    if payee_note:
        payload['payeeNote'] = payee_note[:100]
    if payer_name:
        payload['payerName'] = payer_name[:150]

    try:
        resp = _api(config, 'POST', '/api/collections/collect', json=payload)
    except requests.exceptions.ReadTimeout:
        # Request may have reached iotec. Keep payment PROCESSING; polling by externalId resolves it.
        logger.warning("iotec collect read-timeout for %s; will reconcile by externalId", external_id)
        return {'id': None, 'status': 'Pending', 'externalId': payload['externalId']}
    except requests.exceptions.RequestException as exc:
        logger.error("iotec collect failed: %s", exc)
        raise IotecError("Payment gateway is temporarily unavailable. Please retry.") from exc

    if resp.status_code == 401:
        raise IotecError("iotec rejected the credentials. Contact support.")
    if resp.status_code != 200:
        msg = _error_message(resp)
        logger.warning("iotec collect rejected (%s): %s", resp.status_code, msg)
        raise IotecError(msg or "Payment request was rejected.")
    try:
        data = resp.json()
    except ValueError:
        raise IotecError("Unreadable response from iotec.")

    outcome, message = interpret_status(data)
    if outcome == 'failed':
        raise IotecError(message or "Payment request failed.")

    tx_id = str(data.get('id') or '')
    if tx_id:
        payment.iotec_transaction_id = tx_id
        payment.save(update_fields=['iotec_transaction_id', 'updated_at'])
    return data


def get_status(config, payment):
    """Returns iotec transaction dict, or None if iotec has no such transaction (404)."""
    if payment.iotec_transaction_id:
        path = f"/api/collections/status/{quote(payment.iotec_transaction_id, safe='')}"
    else:
        path = f"/api/collections/external-id/{quote(payment.payment_number, safe='')}"
    try:
        resp = _api(config, 'GET', path)
    except requests.exceptions.RequestException as exc:
        raise IotecError("Could not reach iotec.") from exc
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise IotecError(_error_message(resp))
    try:
        return resp.json()
    except ValueError:
        raise IotecError("Unreadable response from iotec.")


def interpret_status(data):
    data = data or {}
    raw = str(data.get('status') or '').strip().lower()
    code = str(data.get('statusCode') or '').strip().lower()
    msg = data.get('statusMessage') or ''
    if raw in _SUCCESS or code in _SUCCESS:
        return 'success', msg
    if raw in _FAILED or code in _FAILED:
        return 'failed', msg
    return 'pending', msg          # Pending, SentToVendor, unknown


def test_connection(config):
    """Token + wallet balance: validates credentials AND wallet id in one call."""
    try:
        get_access_token(config, force_refresh=True)
        resp = _api(config, 'GET', f"/api/wallet-balance/{config.wallet_id}")
    except IotecError as exc:
        return {'success': False, 'message': str(exc)}
    except requests.exceptions.RequestException as exc:
        return {'success': False, 'message': f'Could not reach iotec: {exc}'}
    if resp.status_code == 200:
        try:
            balance = resp.json()
        except ValueError:
            balance = None
        return {'success': True, 'message': 'Connected to iotec. Wallet verified.', 'balance': balance}
    if resp.status_code == 404:
        return {'success': False, 'message': 'Authenticated, but wallet ID was not found.'}
    return {'success': False, 'message': _error_message(resp)}


# ── Settlement ─────────────────────────────────────────────────────────────

def settle_payment(payment_id, data):
    """
    Apply an iotec result to a Payment (and its hotspot session / PPPoE subscription).
    Idempotent + row-locked. Returns True only if state changed.
    """
    from apps.billing.models.payment_models import Payment
    from apps.billing.utils.payment_errors import humanize_payment_failure

    outcome, message = interpret_status(data)
    if outcome == 'pending':
        return False

    schema = connection.schema_name
    hotspot_session = None

    with transaction.atomic():
        # no select_related here: FOR UPDATE can't lock the nullable side of an outer join
        payment = Payment.objects.select_for_update().get(pk=payment_id)

        if payment.status == 'COMPLETED':
            return False
        if outcome == 'failed' and payment.status in ('FAILED', 'CANCELLED', 'REFUNDED'):
            return False

        now = timezone.now()
        hotspot_session = payment.hotspot_session

        if outcome == 'failed':
            payment.status = 'FAILED'
            payment.failure_reason = humanize_payment_failure(message or 'Payment failed')
            payment.save(update_fields=['status', 'failure_reason', 'updated_at'])
            if hotspot_session and hotspot_session.status == 'pending':
                hotspot_session.mark_failed(payment.failure_reason)
            logger.warning("iotec payment %s FAILED: %s", payment.payment_number, message)
            return True

        # ── success ──
        try:
            from decimal import Decimal
            got = Decimal(str(data.get('amount'))) if data.get('amount') is not None else None
        except Exception:
            got = None
        if got is not None and got < payment.amount:
            logger.error("iotec payment %s underpaid: expected %s got %s — manual review",
                         payment.payment_number, payment.amount, got)
            return False

        vendor_ref = str(data.get('vendorTransactionId') or data.get('id') or '')
        payment.status = 'COMPLETED'
        payment.processed_at = now
        payment.transaction_id = vendor_ref[:255]
        if data.get('id') and not payment.iotec_transaction_id:
            payment.iotec_transaction_id = str(data['id'])
        payment.is_reconciled = True
        payment.reconciled_at = now
        payment.failure_reason = ''
        payment.save(update_fields=[
            'status', 'processed_at', 'transaction_id', 'iotec_transaction_id',
            'is_reconciled', 'reconciled_at', 'failure_reason', 'updated_at',
        ])
        logger.info("iotec payment %s COMPLETED (ref=%s)", payment.payment_number, vendor_ref)

        if hotspot_session:
            # 'failed' is included on purpose: the 10-min stale cleanup may have failed the
            # session before a late approval arrived. Money was collected -> activate.
            if hotspot_session.status in ('pending', 'failed'):
                hotspot_session.mark_paid(vendor_ref[:50])
            if hotspot_session.status in ('paid', 'pending'):
                try:
                    hotspot_session.activate(hotspot_session.access_code)
                    from apps.billing.services.hotspot_radius_service import HotspotRadiusService
                    from apps.billing.services.hotspot_credentials import schedule_tv_login_push
                    HotspotRadiusService().create_hotspot_credentials(
                        username=hotspot_session.access_code,
                        password=hotspot_session.access_code,
                        router=hotspot_session.router,
                        plan=hotspot_session.plan,
                        expires_at=hotspot_session.expires_at,
                        mac_address=hotspot_session.mac_address or '',
                    )
                    schedule_tv_login_push(hotspot_session, schema)

                    from apps.messaging.tasks import send_hotspot_welcome_sms
                    sid = hotspot_session.session_id
                    transaction.on_commit(lambda: send_hotspot_welcome_sms.delay(sid, schema))
                except Exception:
                    # Payment stays COMPLETED; auto-login reseeds RADIUS when the user returns.
                    logger.exception("iotec hotspot activation failed for session %s",
                                     hotspot_session.session_id)
        elif payment.customer_id:
            from apps.billing.services.pppoe_activation import activate_pppoe_from_payment
            try:
                activate_pppoe_from_payment(payment)
            except Exception:
                logger.exception("iotec PPPoE activation failed for %s", payment.payment_number)

    return True


def poll_and_settle_payment(payment_id, *, min_interval=2):
    """
    Ask iotec for the payment's state and settle it if final.
    min_interval throttles per-payment polling (portal polls every ~1.2s). Returns True if state changed.
    """
    from apps.billing.models.payment_models import Payment

    if min_interval and not cache.add(
        f"iotec:poll:{connection.schema_name}:{payment_id}", 1, timeout=min_interval
    ):
        return False

    payment = (
        Payment.objects.select_related('payment_method__iotec_configuration')
        .filter(pk=payment_id, status__in=('PROCESSING', 'PENDING'))
        .first()
    )
    if not payment:
        return False
    method = payment.payment_method
    if not method or method.method_type != 'IOTEC_MOMO' or not method.iotec_configuration:
        return False

    try:
        data = get_status(method.iotec_configuration, payment)
    except IotecError as exc:
        logger.warning("iotec status check failed for %s: %s", payment.payment_number, exc)
        return False

    if data is None:
        # never reached iotec (e.g. collect timed out in flight)
        if not payment.iotec_transaction_id and timezone.now() - payment.created_at > timedelta(minutes=5):
            data = {'status': 'Failed',
                    'statusMessage': 'The payment request was not received by the provider.'}
        else:
            return False

    return settle_payment(payment.pk, data)