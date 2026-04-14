# apps/billing/views/tuma_webhook_views.py
from django.db import transaction
from django.db.utils import ProgrammingError
from django.http import JsonResponse
from django.utils import timezone
import logging
logger = logging.getLogger(__name__)

from rest_framework.views import APIView
from django_tenants.utils import schema_context, get_public_schema_name
from apps.core.models import Tenant, TumaCallbackMap  # ADDED: TumaCallbackMap for O(1) lookup

from apps.billing.models.payment_models import Payment, StkCancellationTracker


class TumaWebhookView(APIView):
    """
    Webhook endpoint for Tuma payment gateway callbacks.
    This endpoint is PUBLIC (no authentication) because Tuma's servers call it.
    
    NOTE: This webhook ONLY updates payment status. It does NOT activate
    hotspot sessions or create RADIUS credentials. That responsibility
    belongs to the polling endpoint (HotspotPurchaseStatusView) to ensure
    idempotency and proper error handling.
    """
    authentication_classes = []
    permission_classes = []

    def _find_payment_and_schema(self, merchant_id, checkout_id):
        """
        Locate payment and the tenant schema where it lives.
        Handles callbacks arriving on public schema where tenant tables are absent.
        
        Priority order:
            1. Fast path: Public lookup map (TumaCallbackMap) - O(1)
            2. Fallback: Search all tenant schemas (legacy)
        
        Returns:
            tuple: (payment, schema_name) or (None, None) if not found
        """
        # ============================================================
        # 1) Fast path: public lookup map (O(1))
        # ============================================================
        with schema_context(get_public_schema_name()):
            mapping = None
            if merchant_id:
                mapping = TumaCallbackMap.objects.filter(
                    merchant_request_id=merchant_id
                ).first()
            if not mapping and checkout_id:
                mapping = TumaCallbackMap.objects.filter(
                    checkout_request_id=checkout_id
                ).first()

        # FIX 1: Wrap mapping block in try/except to handle ProgrammingError
        if mapping:
            logger.info(f"Found TumaCallbackMap mapping for merchant_id={merchant_id}, schema={mapping.schema_name}")
            try:
                with schema_context(mapping.schema_name):
                    payment = None
                    if merchant_id:
                        payment = Payment.objects.filter(
                            tuma_merchant_request_id=merchant_id
                        ).first()
                    if not payment and checkout_id:
                        payment = Payment.objects.filter(
                            tuma_checkout_request_id=checkout_id
                        ).first()
                    if payment:
                        logger.info(f"Found payment via lookup map in schema: {mapping.schema_name}")
                        return payment, mapping.schema_name
                    else:
                        logger.warning(f"Mapping exists but payment not found in schema {mapping.schema_name}")
            except ProgrammingError as e:
                logger.debug(f"ProgrammingError in mapping schema {mapping.schema_name}: {e}")

        # ============================================================
        # 2) Fallback: Search all tenant schemas (legacy callbacks without map)
        # ============================================================
        logger.info("Falling back to searching all tenant schemas for payment...")
        with schema_context(get_public_schema_name()):
            tenant_schemas = list(Tenant.objects.filter(is_active=True).values_list("schema_name", flat=True))

        for schema_name in tenant_schemas:
            with schema_context(schema_name):
                try:
                    payment = None
                    if merchant_id:
                        payment = Payment.objects.filter(
                            tuma_merchant_request_id=merchant_id
                        ).first()
                    if not payment and checkout_id:
                        payment = Payment.objects.filter(
                            tuma_checkout_request_id=checkout_id
                        ).first()
                    if payment:
                        logger.info(f"Found payment via fallback search in tenant schema: {schema_name}")
                        return payment, schema_name
                except ProgrammingError as e:
                    # Skip schemas where billing_payment doesn't exist yet
                    logger.debug(f"Payment table not found in schema {schema_name}: {e}")
                    continue

        return None, None

    def _handle_sms_topup_callback(self, merchant_id, checkout_id, data, result_code):
        """
        Handle SMS topup callbacks that arrive at the main Tuma webhook.
        
        FIX 2: This method processes SMS unit top-up payments that come through
        the same Tuma webhook endpoint. SMS topups use TUMA_CALLBACK_URL and
        need to be credited to the tenant's SMS wallet.
        
        Returns:
            JsonResponse if processed, None otherwise
        """
        from django_tenants.utils import schema_context, get_public_schema_name
        from django.db.models import Q
        from apps.core.models import TumaCallbackMap
        from django.db.utils import ProgrammingError

        # Find mapping that belongs to an SMS topup
        with schema_context(get_public_schema_name()):
            mapping = TumaCallbackMap.objects.filter(
                Q(merchant_request_id=merchant_id) | Q(checkout_request_id=checkout_id)
            ).first()

        if not mapping:
            return None
        if not (mapping.payment_reference or '').startswith('SMS-TOPUP-'):
            return None

        # It's an SMS topup — process it in the correct tenant schema
        from apps.messaging.models import SMSUnitTopup, TenantSMSWallet, SMSCreditLedger
        from decimal import Decimal
        from django.db import transaction as _tx

        target_schema = mapping.schema_name
        co_id = mapping.checkout_request_id

        try:
            with schema_context(target_schema):
                topup = SMSUnitTopup.objects.filter(checkout_request_id=co_id).first()
                if not topup or topup.status == 'completed':
                    return JsonResponse({'success': True, 'message': 'Already processed'}, status=200)

                if str(result_code) == '0':
                    topup.status = 'completed'
                    topup.save()

                    with _tx.atomic():
                        wallet, _ = TenantSMSWallet.objects.get_or_create(
                            is_active=True,
                            defaults={
                                'sms_units': Decimal('0'),
                                'sell_price_per_unit': Decimal('0.40'),
                            }
                        )
                        w = TenantSMSWallet.objects.select_for_update().get(pk=wallet.pk)
                        w.sms_units += Decimal(str(topup.units_purchased))
                        w.save(update_fields=['sms_units', 'updated_at'])

                    SMSCreditLedger.objects.create(
                        wallet=wallet,
                        entry_type='topup',
                        units=Decimal(str(topup.units_purchased)),
                        unit_price=wallet.sell_price_per_unit,
                        amount=topup.amount_paid,
                        reference=topup.payment_reference or '',
                        notes=f'Top-up #{topup.id} ({topup.units_purchased} units)',
                    )
                    logger.info(f"Credited {topup.units_purchased} SMS units to {target_schema}")
                else:
                    topup.status = 'failed'
                    topup.notes = data.get('result_desc', 'Payment failed')
                    topup.save()

            return JsonResponse({'success': True, 'sms_topup_processed': True}, status=200)

        except ProgrammingError as e:
            logger.error(f"DB error processing SMS topup for {target_schema}: {e}")
            return None

    def post(self, request):
        """
        Handle Tuma webhook callback for payment status updates.
        
        Expected data structure:
        {
            "merchant_request_id": "string",
            "checkout_request_id": "string", 
            "result_code": 0,  # 0 = success, non-zero = failure
            "status": "completed",  # or "failed", "pending", etc.
            "result_desc": "string",
            "mpesa_receipt_number": "string",  # on success
            "failure_reason": "string"  # on failure
        }
        
        NOTE: @transaction.atomic is NOT applied at the method level.
        We use a narrower transaction scope only for the final update
        to avoid long-running transactions and deadlocks.
        """
        data = request.data
        merchant_id = data.get("merchant_request_id")
        checkout_id = data.get("checkout_request_id")
        result_code = data.get("result_code")
        
        # Validate required identifiers
        if not merchant_id and not checkout_id:
            return JsonResponse(
                {"success": False, "message": "Missing merchant_request_id or checkout_request_id"}, 
                status=400
            )

        # Find the payment and its tenant schema
        payment, payment_schema = self._find_payment_and_schema(merchant_id, checkout_id)
        
        # FIX 2: Check if this is an SMS topup callback before returning 404
        if not payment or not payment_schema:
            # Handle SMS topup callbacks
            sms_result = self._handle_sms_topup_callback(merchant_id, checkout_id, data, result_code)
            if sms_result:
                return sms_result

            logger.warning(f"Payment not found for merchant_id={merchant_id}, checkout_id={checkout_id}")
            return JsonResponse(
                {"success": False, "message": "Payment not found"}, 
                status=404
            )

        # ============================================================
        # NARROW TRANSACTION SCOPE: Only around the final update
        # This prevents long-running transactions and reduces deadlock risk
        # ============================================================
        with schema_context(payment_schema):
            with transaction.atomic():
                # Re-fetch the payment within the transaction to ensure we have the latest
                payment = Payment.objects.select_for_update().get(pk=payment.pk)

                # ====================== UPDATE STK CANCELLATION TRACKER ======================
                # Track consecutive 1032 cancellations (user cancelling STK prompt)
                self._update_stk_cancellation_tracker(payment, data, result_code)
                # ============================================================================

                # Idempotency check: Don't process twice
                if payment.status == "COMPLETED":
                    logger.info(f"Payment {payment.payment_number} already completed, skipping")
                    return JsonResponse(
                        {"success": True, "message": "Already processed"}, 
                        status=200
                    )

                # Update Tuma specifics with callback data
                payment.tuma_callback_payload = data
                payment.tuma_result_code = result_code
                payment.tuma_result_desc = data.get("result_desc", "")
                payment.tuma_status = data.get("status", "").lower()

                # ============================================================
                # Trust result_code 0 primarily
                # ============================================================
                is_success = str(result_code) == "0"

                if is_success:
                    payment.status = "COMPLETED"
                    payment.processed_at = data.get("processed_at") or timezone.now()
                    payment.mpesa_receipt = data.get("mpesa_receipt_number", "")
                    payment.transaction_id = data.get("transaction_id", "") or data.get("mpesa_receipt_number", "")
                    payment.is_reconciled = True
                    payment.reconciled_at = data.get("completed_at") or data.get("processed_at") or timezone.now()
                    payment.failure_reason = ""  # Clear any previous failure reasons
                    payment.save()
                    
                    logger.info(f"Payment {payment.payment_number} marked as COMPLETED")
                    
                    # ═══════════════════════════════════════════════════════════════════
                    # WEBHOOK ONLY MARKS PAYMENT AS COMPLETED
                    # Activation is handled by HotspotPurchaseStatusView polling
                    # ═══════════════════════════════════════════════════════════════════
                    # If linked hotspot session exists, just mark it as paid (not active)
                    # The polling endpoint will handle activation and RADIUS creation
                    hotspot_session = getattr(payment, 'hotspot_session', None)
                    if hotspot_session:
                        # Only mark as paid if not already paid or active
                        if hotspot_session.status not in ['paid', 'active']:
                            logger.info(
                                f"Webhook: Marking hotspot session {hotspot_session.session_id} "
                                f"as paid for payment {payment.payment_number} (activation deferred to polling)"
                            )
                            # Just mark as paid - don't activate yet
                            hotspot_session.mark_paid(payment.mpesa_receipt or payment.transaction_id or "")
                        else:
                            logger.debug(
                                f"Webhook: Hotspot session {hotspot_session.session_id} already "
                                f"in status {hotspot_session.status}, skipping mark_paid"
                            )
                    
                    # ================================================================
                    # TODO: Add your logic here for other session types
                    # (e.g., PPPoE sessions, prepaid plans, etc.)
                    # For non-hotspot payments, you may want immediate activation here
                    # ================================================================

                    # ================================================================
                    # HOTSPOT REVENUE ACCUMULATION
                    # NOTE: Revenue is accumulated in HotspotSession.activate()
                    # (hotspot_models.py) — the single source of truth.
                    # Do NOT accumulate here to avoid double-counting.
                    # ================================================================
                    
                else:
                    payment.status = "FAILED"
                    payment.failure_reason = data.get("failure_reason") or data.get("result_desc") or "Transaction failed"
                    payment.save()
                    
                    logger.warning(f"Payment {payment.payment_number} marked as FAILED: {payment.failure_reason}")
                    
                    # If linked hotspot session exists, mark as failed
                    hotspot_session = getattr(payment, 'hotspot_session', None)
                    if hotspot_session:
                        if hotspot_session.status not in ['failed', 'cancelled']:
                            logger.info(
                                f"Webhook: Marking hotspot session {hotspot_session.session_id} "
                                f"as failed for payment {payment.payment_number}"
                            )
                            hotspot_session.mark_failed(payment.failure_reason)

                return JsonResponse({"success": True, "payment_id": payment.id}, status=200)

    def _update_stk_cancellation_tracker(self, payment, data, result_code):
        """
        Update the STK cancellation tracker for this phone number.
        Especially handles result_code 1032 (User cancelled the STK Push prompt).
        """
        schema = payment.schema_name
        phone = payment.payer_phone or payment.mpesa_phone or ""

        if not phone:
            logger.debug("No phone number found for STK cancellation tracking")
            return  # No phone number to track

        # Get or create tracker
        tracker = StkCancellationTracker.get_or_create_tracker(schema, phone)

        # Idempotency guard: don't count the same checkout request twice
        incoming_checkout = data.get("checkout_request_id", "") or ""
        if incoming_checkout and tracker.last_checkout_request_id == incoming_checkout:
            logger.debug(f"Already processed checkout request {incoming_checkout}, skipping")
            return  # Already processed this callback

        # Update tracker based on result_code
        if str(result_code) == "1032":  # User cancelled STK prompt
            tracker.consecutive_1032_count += 1
            
            if tracker.consecutive_1032_count >= 3 and not tracker.is_blocked:
                tracker.is_blocked = True
                tracker.blocked_at = timezone.now()
                logger.warning(f"Phone {phone} blocked due to {tracker.consecutive_1032_count} consecutive cancellations")
        else:
            # Any successful payment or other failure resets the cancellation streak
            if tracker.consecutive_1032_count > 0:
                logger.info(f"Resetting cancellation streak for phone {phone} (was {tracker.consecutive_1032_count})")
            tracker.consecutive_1032_count = 0
            tracker.is_blocked = False
            tracker.blocked_at = None

        # Always update last known values
        tracker.last_result_code = int(result_code) if str(result_code).isdigit() else None
        tracker.last_checkout_request_id = incoming_checkout
        tracker.save()
        
        logger.debug(f"STK tracker updated for phone {phone}: count={tracker.consecutive_1032_count}, blocked={tracker.is_blocked}")