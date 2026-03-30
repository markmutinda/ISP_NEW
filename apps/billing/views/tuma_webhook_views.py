# apps/billing/views/tuma_webhook_views.py
from django.db import transaction
from django.http import JsonResponse
from rest_framework.views import APIView
from apps.billing.models.payment_models import Payment

class TumaWebhookView(APIView):
    authentication_classes = []
    permission_classes = []

    @transaction.atomic
    def post(self, request):
        data = request.data
        merchant_id = data.get("merchant_request_id")
        checkout_id = data.get("checkout_request_id")
        result_code = data.get("result_code")
        status_text = (data.get("status") or "").lower()

        if not merchant_id and not checkout_id:
            return JsonResponse({"success": False, "message": "Missing IDs"}, status=400)

        # Find the payment using select_for_update to prevent race conditions
        payment = (
            Payment.objects.select_for_update()
            .filter(tuma_merchant_request_id=merchant_id)
            .first()
            or Payment.objects.select_for_update().filter(tuma_checkout_request_id=checkout_id).first()
        )

        if not payment:
            return JsonResponse({"success": False, "message": "Payment not found"}, status=404)

        # Idempotency check: Don't process twice
        if payment.status == "COMPLETED":
            return JsonResponse({"success": True, "message": "Already processed"}, status=200)

        # Update Tuma specifics
        payment.tuma_callback_payload = data
        payment.tuma_result_code = result_code
        payment.tuma_result_desc = data.get("result_desc", "")
        payment.tuma_status = status_text

        # Official Tuma Docs: success is result_code 0
        if str(result_code) == "0" and status_text == "completed":
            payment.status = "COMPLETED"
            payment.mpesa_receipt = data.get("mpesa_receipt_number", "")
            payment.save()
            
            # TODO: Add your logic here to activate the Hotspot or PPPoE session
            # (e.g., radius_sync_service.activate_hotspot(payment.customer))
            
        else:
            payment.status = "FAILED"
            payment.failure_reason = data.get("failure_reason") or data.get("result_desc", "Failed")
            payment.save()

        return JsonResponse({"success": True}, status=200)