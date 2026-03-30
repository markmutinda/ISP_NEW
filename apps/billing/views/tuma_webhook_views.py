# apps/billing/views/tuma_webhook_views.py
from django.db import transaction
from django.http import JsonResponse
from rest_framework.views import APIView
from apps.billing.models.payment_models import Payment


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
        # Don't be too strict with the string "completed"
        # According to Tuma docs: result_code == 0 means success
        # ============================================================
        is_success = str(result_code) == "0"

        if is_success:
            payment.status = "COMPLETED"
            payment.processed_at = data.get("processed_at")  # Use Tuma's timestamp if provided
            # Store M-Pesa receipt number if provided (for M-Pesa payments)
            payment.mpesa_receipt = data.get("mpesa_receipt_number", "")
            payment.transaction_id = data.get("transaction_id", "") or data.get("mpesa_receipt_number", "")
            payment.is_reconciled = True
            payment.reconciled_at = data.get("completed_at") or data.get("processed_at")
            payment.failure_reason = ""  # Clear any previous failure reasons
            payment.save()
            
            # ========================================================
            # TODO: Add your logic here to activate the Hotspot or PPPoE session
            # This is where you'd integrate with your service activation system
            # ========================================================
            # Example:
            # from apps.network.services.radius_sync_service import activate_service
            # activate_service(payment.customer, payment.amount, payment.invoice)
            #
            # Or for hotspot:
            # from apps.hotspot.services.hotspot_activation import activate_hotspot_voucher
            # activate_hotspot_voucher(payment.customer, payment.amount)
            #
            # Or for PPPoE:
            # from apps.radius.services.pppoe_activation import activate_pppoe_session
            # activate_pppoe_session(payment.customer, payment.invoice)
            # ========================================================
            
        else:
            payment.status = "FAILED"
            payment.failure_reason = data.get("failure_reason") or data.get("result_desc") or "Transaction failed"
            payment.save()

        return JsonResponse({"success": True, "payment_id": payment.id}, status=200)