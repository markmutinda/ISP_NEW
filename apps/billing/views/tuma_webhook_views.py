# apps/billing/views/tuma_webhook_views.py
from django.db import transaction
from django.db.utils import ProgrammingError
from django.http import JsonResponse
from django.utils import timezone
import logging
logger = logging.getLogger(__name__)

from rest_framework.views import APIView
from django_tenants.utils import schema_context, get_public_schema_name
from apps.core.models import Tenant, TumaCallbackMap

from apps.billing.models.payment_models import Payment, StkCancellationTracker


# ── Safaricom/Daraja result code mapping ──
DARAJA_RESULT_CODES = {
    "0": "Success",
    "1": "Insufficient balance in M-Pesa account",
    "1032": "Transaction cancelled by user",
    "1037": "Timeout - no response from user",
    "2001": "Invalid M-Pesa PIN entered",
    "1025": "Duplicated MSISDN. MSISDN has an existing USSD Session",
    "1019": "Error occurred while sending push request",
    "1001": "Internal system error",
    "1002": "Invalid request parameters",
    "1003": "Service unavailable",
    "1004": "Duplicate transaction",
    "1005": "Transaction not found",
    "1006": "Invalid shortcode",
    "1007": "Invalid phone number",
    "1008": "Invalid amount",
    "1009": "Invalid account reference",
    "1010": "Invalid transaction description",
    "1011": "Invalid callback URL",
    "1012": "Invalid security credential",
    "1013": "Invalid access token",
    "1014": "Invalid consumer key/secret",
    "1015": "Invalid IP address",
    "1016": "Invalid timestamp",
    "1017": "Invalid signature",
    "1018": "Invalid encryption",
    "1020": "Invalid transaction type",
    "1021": "Invalid party B",
    "1022": "Invalid party A",
    "1023": "Invalid phone number format",
    "1024": "Invalid amount format",
    "1026": "MSISDN is not registered on M-Pesa",
    "1027": "MSISDN has insufficient balance",
    "1028": "MSISDN is not active",
    "1029": "MSISDN is blocked",
    "1030": "MSISDN is not allowed",
    "1031": "MSISDN not found",
    "1033": "Transaction already completed",
    "1034": "Transaction already failed",
    "1035": "Transaction already cancelled",
    "1036": "Transaction expired",
    "1038": "Network error",
    "1039": "System error",
    "1040": "Invalid security answer",
    "1041": "Invalid security question",
    "1042": "Invalid security challenge",
    "1043": "Invalid security response",
    "1044": "Invalid security token",
    "1045": "Invalid security signature",
}


class TumaWebhookView(APIView):
    """
    Webhook endpoint for Tuma payment gateway callbacks.
    This endpoint is PUBLIC (no authentication) because Tuma's servers call it.
    
    This webhook now handles both payment status updates AND server-side
    activation of hotspot sessions. This decouples activation from the
    client's browser, ensuring sessions activate even if the user's
    phone locks, loses WiFi, or closes the tab mid-payment.
    """
    authentication_classes = []
    permission_classes = []

    def _humanize_result_code(self, result_code):
        """Convert Safaricom result code to human-readable message."""
        code = str(result_code)
        return DARAJA_RESULT_CODES.get(code, f"Failed due to an unresolved reason type. (Error code: {code})")

    def _tenant_label(self, schema_name):
        """Get tenant company name for Telegram alerts."""
        try:
            with schema_context(get_public_schema_name()):
                t = Tenant.objects.filter(schema_name=schema_name).select_related('company').first()
                if t and t.company:
                    return t.company.name
                return schema_name
        except Exception:
            return schema_name

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

        if mapping:
            logger.info(f"Found TumaCallbackMap mapping for merchant_id={merchant_id}, schema={mapping.schema_name}")
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
        
        This webhook now performs server-side activation for hotspot sessions
        immediately upon successful payment confirmation. This eliminates the
        dependency on the client's browser polling to complete activation.
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
        
        if not payment or not payment_schema:
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
                # Re-fetch the payment within the transaction with row lock
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
                    
                    # ── TELEGRAM SUCCESS ALERT ──
                    try:
                        from apps.notifications.tasks import send_telegram_payment_alert_task
                        from apps.core.telegram_notify import build_payment_success_message
                        send_telegram_payment_alert_task.apply_async(args=[
                            build_payment_success_message(
                                receipt=payment.mpesa_receipt or payment.transaction_id or "N/A",
                                amount=payment.amount,
                                phone=payment.payer_phone or payment.mpesa_phone or "",
                                tenant_label=self._tenant_label(payment_schema),
                            )
                        ], retry=False)
                    except Exception as e:
                        logger.warning(f"Telegram success alert enqueue failed: {e}")
                    
                    # ============================================================
                    # HOTSPOT SESSION ACTIVATION - SERVER-SIDE
                    # ============================================================
                    # The webhook now takes ownership of activation. This decouples
                    # the activation process from the client's browser, ensuring
                    # sessions activate even if the user's phone locks, loses WiFi,
                    # or closes the tab mid-payment.
                    hotspot_session = getattr(payment, 'hotspot_session', None)
                    if hotspot_session:
                        # Sync payment receipt first
                        if hotspot_session.status not in ['paid', 'active']:
                            logger.info(
                                f"Webhook: Syncing payment parameters for hotspot session {hotspot_session.session_id} "
                                f"linked to payment {payment.payment_number}"
                            )
                            # 1. Sync the receipt parameters first
                            hotspot_session.mark_paid(payment.mpesa_receipt or payment.transaction_id or "")
                        
                        # 2. Immediately trigger server-side activation and RADIUS credential synchronization
                        # This uses select_for_update() internally to prevent race conditions
                        if hotspot_session.status in ['paid', 'pending']:
                            try:
                                # Trigger timelines, expirations, and metered metrics row updates
                                hotspot_session.activate(hotspot_session.access_code)
                                
                                # Sync directly into FreeRADIUS table matrices
                                from apps.billing.services.hotspot_radius_service import HotspotRadiusService
                                HotspotRadiusService().create_hotspot_credentials(
                                    username=hotspot_session.access_code,
                                    password=hotspot_session.access_code,
                                    router=hotspot_session.router,
                                    plan=hotspot_session.plan,
                                    expires_at=hotspot_session.expires_at,
                                    mac_address=hotspot_session.mac_address or '',
                                )
                                logger.info(
                                    f"🚀 Webhook Core: Successfully auto-activated session {hotspot_session.session_id} "
                                    f"and provisioned RADIUS profiles directly to disk."
                                )

                                # ── SEND WELCOME SMS ──────────────────────────────────
                                # Re-fetch session so expires_at and access_code are up to date
                                # after activate() updated them in DB
                                try:
                                    hotspot_session.refresh_from_db()
                                    from apps.messaging.services.notification_sender import SMSNotifier
                                    with schema_context(payment_schema):
                                        SMSNotifier.hotspot_welcome(
                                            hotspot_session,
                                            schema_name=payment_schema,
                                        )
                                    logger.info(
                                        f"Welcome SMS sent for session {hotspot_session.session_id}"
                                    )
                                except Exception as sms_err:
                                    # SMS failure must never break payment confirmation
                                    logger.warning(
                                        f"Hotspot welcome SMS failed for session "
                                        f"{hotspot_session.session_id}: {sms_err}"
                                    )
                                # ─────────────────────────────────────────────────────

                            except Exception as radius_err:
                                logger.error(
                                    f"❌ Webhook Core: RADIUS profile synchronization failed for session "
                                    f"{hotspot_session.session_id}: {radius_err}"
                                )
                                # Don't re-raise - we want to keep the payment as completed
                                # even if RADIUS sync fails. The polling endpoint can retry.
                        else:
                            logger.debug(
                                f"Webhook: Hotspot session {hotspot_session.session_id} is already "
                                f"at status '{hotspot_session.status}'. Direct server activation skipped."
                            )
                    else:
                        logger.debug(f"No hotspot session linked to payment {payment.payment_number}")
                    
                    # ================================================================
                    # PPPoE / STANDARD PLAN ACTIVATION LOGIC
                    # ================================================================
                    if payment.customer and not hotspot_session:
                        try:
                            # 1. Get the customer's primary service connection
                            service = payment.customer.services.filter(
                                status__in=['ACTIVE', 'SUSPENDED']
                            ).first()
                            
                            if service and hasattr(payment.customer, 'radius_credentials'):
                                creds = payment.customer.radius_credentials
                                plan = service.plan
                                
                                # 2. Calculate new expiration date using the Plan's exact settings
                                now = timezone.now()
                                
                                # Fetch exact timedelta (minutes, hours, days, months)
                                validity_delta = None
                                if hasattr(plan, 'get_validity_timedelta'):
                                    validity_delta = plan.get_validity_timedelta()
                                else:
                                    from datetime import timedelta
                                    validity_delta = timedelta(days=getattr(plan, 'validity_days', 30))
                                
                                current_expiry = creds.expiration_date
                                
                                if validity_delta is None:
                                    # Unlimited Plan
                                    new_expiry = None
                                else:
                                    # If they still have active time, add to it. If expired, start from right now.
                                    if current_expiry and current_expiry > now:
                                        new_expiry = current_expiry + validity_delta
                                    else:
                                        new_expiry = now + validity_delta
                                
                                # 3. Update Radius Credentials in the database
                                creds.expiration_date = new_expiry
                                creds.is_enabled = True
                                creds.subscription_activated_at = now
                                creds.save(update_fields=[
                                    'expiration_date', 
                                    'is_enabled', 
                                    'subscription_activated_at'
                                ])
                                
                                # 4. Sync updates to the FreeRADIUS SQL tables
                                creds.sync_to_radius()
                                
                                # 5. Ensure the service object is marked active
                                if service.status == 'SUSPENDED':
                                    service.status = 'ACTIVE'
                                    service.save(update_fields=['status'])
                                    
                                logger.info(f"Successfully activated PPPoE service for {payment.customer}. New expiry: {new_expiry}")
                                
                                # 6. Send renewal SMS notification
                                try:
                                    from apps.messaging.services.notification_sender import SMSNotifier
                                    plan_name = plan.name if plan else ''
                                    SMSNotifier.pppoe_renewal(
                                        customer=payment.customer,
                                        plan_name=plan_name,
                                        expires_at=new_expiry,
                                    )
                                    logger.info(f"Renewal SMS sent to {payment.customer.phone} for PPPoE renewal")
                                except Exception as e:
                                    logger.warning(f"Renewal SMS failed for payment {payment.payment_number}: {e}")
                                
                                # 7. (Optional but recommended) Kick the suspended session off the router
                                # so it immediately reconnects and picks up the new active profile.
                                try:
                                    from apps.radius.services.coa_service import CoAService
                                    coa = CoAService()
                                    coa.disconnect_user(creds.username)
                                except ImportError:
                                    pass # CoAService might not be implemented yet
                                except Exception as e:
                                    logger.warning(f"Failed to send CoA disconnect for {creds.username}: {e}")
                                    
                        except Exception as e:
                            logger.error(f"Error activating PPPoE service for payment {payment.payment_number}: {e}")

                    # ================================================================
                    # HOTSPOT REVENUE ACCUMULATION
                    # NOTE: Revenue is accumulated in HotspotSession.activate()
                    # (hotspot_models.py) — the single source of truth.
                    # Do NOT accumulate here to avoid double-counting.
                    # ================================================================
                    
                else:
                    # ── Get human-readable failure reason ──
                    failure_reason = (
                        data.get("failure_reason") 
                        or data.get("result_desc") 
                        or self._humanize_result_code(result_code)
                    )
                    
                    payment.status = "FAILED"
                    payment.failure_reason = failure_reason
                    payment.save()
                    
                    logger.warning(f"Payment {payment.payment_number} marked as FAILED: {payment.failure_reason}")
                    
                    # ── TELEGRAM FAILURE ALERT ──
                    try:
                        from apps.notifications.tasks import send_telegram_payment_alert_task
                        from apps.core.telegram_notify import build_payment_failure_message
                        send_telegram_payment_alert_task.apply_async(args=[
                            build_payment_failure_message(
                                phone=payment.payer_phone or payment.mpesa_phone or "",
                                amount=payment.amount,
                                tenant_label=self._tenant_label(payment_schema),
                                reason=payment.failure_reason or "Transaction failed",
                            )
                        ], retry=False)
                    except Exception as e:
                        logger.warning(f"Telegram failure alert enqueue failed: {e}")
                    
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