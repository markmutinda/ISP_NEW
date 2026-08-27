# apps/billing/views/netily_paybill_webhook_views.py
from types import SimpleNamespace
from rest_framework.views import APIView
from .tuma_webhook_views import TumaWebhookView


class NetilyPaybillWebhookView(APIView):
    """
    Public STK callback for Netily's own master paybill.
    Normalizes raw Safaricom stkCallback into the flat shape
    TumaWebhookView already knows how to process, then delegates.
    """
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        body = request.data.get("Body", {}).get("stkCallback", {})
        result_code = body.get("ResultCode")
        items = {
            item.get("Name"): item.get("Value")
            for item in body.get("CallbackMetadata", {}).get("Item", [])
        }
        receipt = items.get("MpesaReceiptNumber", "")

        normalized = {
            "merchant_request_id": body.get("MerchantRequestID"),
            "checkout_request_id": body.get("CheckoutRequestID"),
            "result_code": result_code,
            "status": "completed" if str(result_code) == "0" else "failed",
            "result_desc": body.get("ResultDesc", ""),
            "mpesa_receipt_number": receipt,
            "transaction_id": receipt,
            "failure_reason": body.get("ResultDesc", ""),
            "processed_at": None,
            "completed_at": None,
        }

        return TumaWebhookView().post(SimpleNamespace(data=normalized))