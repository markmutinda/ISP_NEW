# apps/billing/views/tuma_webhook_views.py
from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone
import logging
logger = logging.getLogger(__name__)

from rest_framework.views import APIView

from apps.billing.models.payment_models import Payment, StkCancellationTracker


class TumaWebhookView(APIView):
    """
    Webhook endpoint for Tuma payment gateway callbacks.
    This endpoint is PUBLIC (no authentication) because Tuma's servers call it.
    """
    authentication_classes = []
    permission_classes = []

    @transaction.atomic
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

        # Find the payment using select_for_update to prevent race conditions
        payment = None
        if merchant_id:
            payment = Payment.objects.select_for_update().filter(
                tuma_merchant_request_id=merchant_id
            ).first()
        
        if not payment and checkout_id:
            payment = Payment.objects.select_for_update().filter(
                tuma_checkout_request_id=checkout_id
            ).first()

        if not payment:
            return JsonResponse(
                {"success": False, "message": "Payment not found"}, 
                status=404
            )

        # ====================== NEW: UPDATE STK CANCELLATION TRACKER ======================
        # Track consecutive 1032 cancellations (user cancelling STK prompt)
        self._update_stk_cancellation_tracker(payment, data, result_code)
        # =================================================================================

        # Idempotency check: Don't process twice
        if payment.status == "COMPLETED":
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
        # Senior Dev Fix: Trust result_code 0 primarily
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
            
            # ═══════════════════════════════════════════════════════════════════
            # ACTIVATE HOTSPOT SESSION (Idempotent)
            # ═══════════════════════════════════════════════════════════════════
            # If linked hotspot session exists, mark as paid (idempotent)
            hotspot_session = getattr(payment, 'hotspot_session', None)
            if hotspot_session:
                # Only mark as paid if not already paid or active
                if hotspot_session.status not in ['paid', 'active']:
                    logger.info(
                        f"Webhook: Marking hotspot session {hotspot_session.session_id} "
                        f"as paid for payment {payment.payment_number}"
                    )
                    hotspot_session.mark_paid(payment.mpesa_receipt or payment.transaction_id or "")
                    
                    # Option 1: Activate immediately (recommended)
                    # This ensures the session is activated as soon as payment is confirmed
                    # The activate() method already has idempotency guards
                    hotspot_session.activate(hotspot_session.access_code)
                    
                    # Create RADIUS credentials for immediate connectivity
                    try:
                        from apps.billing.services.hotspot_radius_service import HotspotRadiusService
                        
                        radius_service = HotspotRadiusService()
                        radius_service.create_hotspot_credentials(
                            username=hotspot_session.access_code,
                            password=hotspot_session.access_code,
                            router=hotspot_session.router,
                            plan=hotspot_session.plan,
                            expires_at=hotspot_session.expires_at,
                            mac_address=hotspot_session.mac_address or '',
                        )
                        logger.info(
                            f"Webhook: RADIUS credentials created for session "
                            f"{hotspot_session.session_id} (user: {hotspot_session.access_code})"
                        )
                    except Exception as e:
                        logger.error(
                            f"Webhook: Failed to create RADIUS credentials for session "
                            f"{hotspot_session.session_id}: {e}",
                            exc_info=True
                        )
                    
            # ================================================================
            # TODO: Add your logic here to activate other session types
            # (e.g., PPPoE sessions, prepaid plans, etc.)
            # ================================================================
            
        else:
            payment.status = "FAILED"
            payment.failure_reason = data.get("failure_reason") or data.get("result_desc") or "Transaction failed"
            payment.save()
            
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
            return  # No phone number to track

        # Get or create tracker
        tracker = StkCancellationTracker.get_or_create_tracker(schema, phone)

        # Idempotency guard: don't count the same checkout request twice
        incoming_checkout = data.get("checkout_request_id", "") or ""
        if incoming_checkout and tracker.last_checkout_request_id == incoming_checkout:
            return  # Already processed this callback

        # Update tracker based on result_code
        if str(result_code) == "1032":  # User cancelled STK prompt
            tracker.consecutive_1032_count += 1
            
            if tracker.consecutive_1032_count >= 3 and not tracker.is_blocked:
                tracker.is_blocked = True
                tracker.blocked_at = timezone.now()
        else:
            # Any successful payment or other failure resets the cancellation streak
            tracker.consecutive_1032_count = 0
            tracker.is_blocked = False
            tracker.blocked_at = None

        # Always update last known values
        tracker.last_result_code = int(result_code) if str(result_code).isdigit() else None
        tracker.last_checkout_request_id = incoming_checkout
        tracker.save()