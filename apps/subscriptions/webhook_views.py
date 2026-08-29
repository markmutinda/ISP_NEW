import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny

from .models import SubscriptionPayment
from .billing_lifecycle import complete_subscription_stk_payment

logger = logging.getLogger(__name__)


class SubscriptionPaybillCallbackView(APIView):
    """
    PUBLIC STK callback for Netily's own paybill — used exclusively for
    platform subscription payments. Replaces the old Tuma subscription
    callback entirely.
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        body = request.data.get("Body", {}).get("stkCallback", {})
        result_code = body.get("ResultCode")
        checkout_request_id = body.get("CheckoutRequestID")

        if not checkout_request_id:
            return Response({"ResultCode": 0, "ResultDesc": "Accepted"})

        try:
            payment = SubscriptionPayment.objects.get(payhero_checkout_id=checkout_request_id)
        except SubscriptionPayment.DoesNotExist:
            logger.warning("Subscription STK callback: no payment for checkout %s", checkout_request_id)
            return Response({"ResultCode": 0, "ResultDesc": "Accepted"})

        if str(result_code) != "0":
            if payment.status != "completed":
                payment.mark_failed(body.get("ResultDesc", "Payment failed"))
            return Response({"ResultCode": 0, "ResultDesc": "Accepted"})

        items = {
            item.get("Name"): item.get("Value")
            for item in body.get("CallbackMetadata", {}).get("Item", [])
        }
        receipt = items.get("MpesaReceiptNumber", "")

        try:
            complete_subscription_stk_payment(payment, mpesa_receipt=receipt)
        except Exception:
            logger.exception("Failed completing subscription payment %s", payment.id)

        return Response({"ResultCode": 0, "ResultDesc": "Accepted"})