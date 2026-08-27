import base64
import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation

import requests
from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

logger = logging.getLogger(__name__)


SIM_CACHE_PREFIX = "netily_system_payment_sim"
SIM_TTL_SECONDS = 60 * 60


def _setting(name, default=""):
    return str(getattr(settings, name, default) or "").strip()


def _simulation_enabled():
    return _setting("NETILY_SYSTEM_PAYMENT_SIMULATOR_ENABLED", "False").lower() in {"1", "true", "yes", "on"}


def _require_simulator_token(request):
    expected = _setting("NETILY_SYSTEM_PAYMENT_SIMULATOR_TOKEN")
    if not expected:
        return False
    supplied = (
        request.headers.get("X-Netily-System-Payment-Token")
        or getattr(request, "query_params", {}).get("test_key")
        or request.data.get("test_key")
        or ""
    )
    return str(supplied).strip() == expected


def _normalize_ke_phone(value):
    phone = "".join(ch for ch in str(value or "") if ch.isdigit())
    if phone.startswith("0") and len(phone) == 10:
        phone = f"254{phone[1:]}"
    elif phone.startswith("7") and len(phone) == 9:
        phone = f"254{phone}"
    if not (phone.startswith("254") and len(phone) == 12 and phone[3] in {"1", "7"}):
        raise ValueError("Enter a valid Safaricom phone number, for example 2547XXXXXXXX.")
    return phone


def _money(value):
    try:
        amount = Decimal(str(value)).quantize(Decimal("1"))
    except (InvalidOperation, TypeError):
        raise ValueError("Enter a valid amount.")
    if amount < Decimal("1"):
        raise ValueError("Amount must be at least KES 1.")
    if amount > Decimal("150000"):
        raise ValueError("Amount is too high for this simulator.")
    return amount


def _cache_key(checkout_request_id):
    return f"{SIM_CACHE_PREFIX}:{checkout_request_id}"


def _callback_url(request):
    configured = _setting("NETILY_SYSTEM_PAYMENT_CALLBACK_URL")
    if configured:
        return configured
    base = _setting("BASE_URL")
    if not base:
        base = request.build_absolute_uri("/").rstrip("/")
    return f"{base.rstrip('/')}/api/v1/billing/netily-system-payment/callback/"


class NetilySystemPaymentInitiateView(APIView):
    """
    Live Daraja STK simulator for the Netily master paybill.

    This intentionally stores status in cache only. It does not create tenant
    payments, subscription payments, invoices, settlements, or payouts.
    """

    authentication_classes = []
    permission_classes = []

    def get(self, request):
        if not _simulation_enabled():
            return Response(
                {"success": False, "message": "Netily system payment simulator is disabled."},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(
            {
                "success": True,
                "status": "ready",
                "message": "Netily system payment simulator is online. Use POST to initiate a live STK push.",
            }
        )

    def post(self, request):
        if not _simulation_enabled():
            return Response(
                {"success": False, "message": "Netily system payment simulator is disabled."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not _require_simulator_token(request):
            return Response(
                {"success": False, "message": "Enter the simulator test key to continue."},
                status=status.HTTP_403_FORBIDDEN,
            )

        consumer_key = _setting("NETILY_SYSTEM_MPESA_CONSUMER_KEY")
        consumer_secret = _setting("NETILY_SYSTEM_MPESA_CONSUMER_SECRET")
        shortcode = _setting("NETILY_SYSTEM_MPESA_SHORTCODE")
        passkey = _setting("NETILY_SYSTEM_MPESA_PASSKEY")
        environment = _setting("NETILY_SYSTEM_MPESA_ENVIRONMENT", "production").lower()
        if not all([consumer_key, consumer_secret, shortcode, passkey]):
            return Response(
                {"success": False, "message": "Simulator Daraja credentials are not configured on the server."},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        try:
            phone = _normalize_ke_phone(request.data.get("phone_number"))
            amount = _money(request.data.get("amount"))
        except ValueError as exc:
            return Response({"success": False, "message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        model = str(request.data.get("model") or "netily_passthrough").lower()
        if model not in {"direct_tenant", "netily_passthrough"}:
            return Response({"success": False, "message": "Choose a valid simulation model."}, status=status.HTTP_400_BAD_REQUEST)

        tenant_code = str(request.data.get("tenant_code") or "DEMO").upper().replace(" ", "")[:8] or "DEMO"
        try:
            fee_rate = Decimal(str(request.data.get("fee_rate") or "2")).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError):
            return Response({"success": False, "message": "Enter a valid processing fee rate."}, status=status.HTTP_400_BAD_REQUEST)
        if fee_rate < Decimal("0") or fee_rate > Decimal("15"):
            return Response({"success": False, "message": "Processing fee must be between 0% and 15%."}, status=status.HTTP_400_BAD_REQUEST)
        account_reference = f"NET-{tenant_code}"[:12]
        description = "NetilySub"[:13]
        api_base = "https://sandbox.safaricom.co.ke" if environment == "sandbox" else "https://api.safaricom.co.ke"

        try:
            token_response = requests.get(
                f"{api_base}/oauth/v1/generate?grant_type=client_credentials",
                auth=(consumer_key, consumer_secret),
                timeout=20,
            )
            token_data = token_response.json()
            access_token = token_data.get("access_token")
            if token_response.status_code != 200 or not access_token:
                logger.warning("Netily simulator Daraja auth failed: %s", token_data)
                return Response(
                    {"success": False, "message": "Could not authenticate with Daraja. Check server credentials."},
                    status=status.HTTP_502_BAD_GATEWAY,
                )

            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            password = base64.b64encode(f"{shortcode}{passkey}{timestamp}".encode()).decode()
            callback_url = _callback_url(request)
            payload = {
                "BusinessShortCode": shortcode,
                "Password": password,
                "Timestamp": timestamp,
                "TransactionType": "CustomerPayBillOnline",
                "Amount": str(int(amount)),
                "PartyA": phone,
                "PartyB": shortcode,
                "PhoneNumber": phone,
                "CallBackURL": callback_url,
                "AccountReference": account_reference,
                "TransactionDesc": description,
            }
            stk_response = requests.post(
                f"{api_base}/mpesa/stkpush/v1/processrequest",
                json=payload,
                headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
                timeout=25,
            )
            stk_data = stk_response.json()
        except requests.RequestException as exc:
            logger.exception("Netily simulator STK network error")
            return Response(
                {"success": False, "message": "Could not reach Daraja. Please try again.", "detail": str(exc)},
                status=status.HTTP_502_BAD_GATEWAY,
            )
        except ValueError:
            logger.exception("Netily simulator received non-JSON Daraja response")
            return Response({"success": False, "message": "Daraja returned an unreadable response."}, status=status.HTTP_502_BAD_GATEWAY)

        if stk_response.status_code == 200 and stk_data.get("ResponseCode") == "0":
            checkout_id = stk_data.get("CheckoutRequestID")
            merchant_id = stk_data.get("MerchantRequestID")
            fee_amount = (amount * fee_rate / Decimal("100")).quantize(Decimal("1"))
            state = {
                "success": True,
                "status": "pending",
                "model": model,
                "phone_number": phone,
                "amount": str(amount),
                "fee_rate": str(fee_rate),
                "fee_amount": str(fee_amount if model == "netily_passthrough" else Decimal("0")),
                "tenant_payout_amount": str(amount - fee_amount if model == "netily_passthrough" else amount),
                "destination_shortcode": shortcode,
                "destination_label": "Netily system Equity paybill",
                "account_reference": account_reference,
                "checkout_request_id": checkout_id,
                "merchant_request_id": merchant_id,
                "customer_message": stk_data.get("CustomerMessage", "Check your phone and enter your M-Pesa PIN."),
                "created_at": timezone.now().isoformat(),
                "callback_url": callback_url,
                "last_result_desc": "",
                "mpesa_receipt": "",
                "safeguard": "Simulation only. No SubscriptionPayment, invoice, tenant wallet, or payout record was created.",
            }
            cache.set(_cache_key(checkout_id), state, SIM_TTL_SECONDS)
            return Response(state)

        message = stk_data.get("ResponseDescription") or stk_data.get("errorMessage") or "STK push was not accepted by Daraja."
        return Response(
            {"success": False, "message": message, "daraja_response": stk_data},
            status=status.HTTP_502_BAD_GATEWAY,
        )


class NetilySystemPaymentStatusView(APIView):
    authentication_classes = []
    permission_classes = []

    def get(self, request, checkout_request_id):
        if not _simulation_enabled():
            return Response({"success": False, "message": "Simulator is disabled."}, status=status.HTTP_404_NOT_FOUND)
        if not _require_simulator_token(request):
            return Response({"success": False, "message": "Enter the simulator test key to continue."}, status=status.HTTP_403_FORBIDDEN)
        state = cache.get(_cache_key(checkout_request_id))
        if not state:
            return Response({"success": False, "status": "expired", "message": "Simulation record expired or was not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(state)


class NetilySystemPaymentCallbackView(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        body = request.data.get("Body", {}).get("stkCallback", {})
        checkout_id = body.get("CheckoutRequestID")
        result_code = body.get("ResultCode")
        result_desc = body.get("ResultDesc", "")
        if not checkout_id:
            return Response({"ResultCode": 0, "ResultDesc": "Accepted"})

        state = cache.get(_cache_key(checkout_id)) or {
            "success": True,
            "checkout_request_id": checkout_id,
            "status": "orphaned_callback",
            "safeguard": "Callback received but no cache state was found. No system records were changed.",
        }

        if str(result_code) == "0":
            items = body.get("CallbackMetadata", {}).get("Item", [])
            meta = {item.get("Name"): item.get("Value") for item in items}
            state.update(
                {
                    "status": "completed",
                    "mpesa_receipt": meta.get("MpesaReceiptNumber", ""),
                    "amount": str(meta.get("Amount", state.get("amount", ""))),
                    "phone_number": str(meta.get("PhoneNumber", state.get("phone_number", ""))),
                    "completed_at": timezone.now().isoformat(),
                    "last_result_desc": result_desc or "Payment completed.",
                }
            )
        elif str(result_code) == "1032":
            state.update({"status": "cancelled", "last_result_desc": "The STK prompt was cancelled. You can retry immediately."})
        else:
            state.update({"status": "failed", "last_result_desc": result_desc or "Payment failed."})

        cache.set(_cache_key(checkout_id), state, SIM_TTL_SECONDS)
        logger.info("Netily system payment simulator callback: checkout=%s status=%s", checkout_id, state.get("status"))
        return Response({"ResultCode": 0, "ResultDesc": "Accepted"})


def netily_system_payment_lab(request):
    html = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Netily System Payment Lab</title>
  <style>
    :root { color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    * { box-sizing: border-box; }
    body { margin: 0; background: #f6f7f2; color: #0f172a; }
    main { width: min(1180px, calc(100% - 32px)); margin: 0 auto; padding: 32px 0; }
    header { display: grid; grid-template-columns: 1fr; gap: 18px; margin-bottom: 24px; }
    h1 { margin: 0; font-size: clamp(2rem, 5vw, 4rem); line-height: .95; letter-spacing: -.03em; }
    p { color: #475569; line-height: 1.6; }
    .pill { display: inline-flex; width: fit-content; border: 1px solid #bbf7d0; background: #ecfdf5; color: #166534; border-radius: 999px; padding: 7px 12px; font-weight: 800; font-size: 13px; }
    .grid { display: grid; grid-template-columns: minmax(0, 1.05fr) minmax(320px, .95fr); gap: 22px; align-items: start; }
    .card, form { background: #fff; border: 1px solid #e2e8f0; border-radius: 22px; box-shadow: 0 20px 50px rgba(15, 23, 42, .08); }
    form { padding: 22px; }
    .models { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin-bottom: 16px; }
    .model { border: 1px solid #e2e8f0; border-radius: 18px; padding: 16px; cursor: pointer; background: #fff; text-align: left; }
    .model.active { border-color: #0f172a; box-shadow: 0 12px 30px rgba(15, 23, 42, .12); }
    label { display: grid; gap: 7px; font-weight: 800; font-size: 14px; color: #334155; }
    input, select { width: 100%; height: 48px; border: 1px solid #cbd5e1; border-radius: 14px; padding: 0 14px; font: inherit; background: #f8fafc; color: #0f172a; }
    input:focus, select:focus { outline: 3px solid rgba(37, 99, 235, .18); border-color: #2563eb; }
    .fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
    .wide { grid-column: 1 / -1; }
    button.primary { width: 100%; height: 50px; margin-top: 18px; border: 0; border-radius: 14px; background: #0f172a; color: #fff; font-weight: 900; cursor: pointer; }
    button.primary:disabled { background: #94a3b8; cursor: not-allowed; }
    .summary { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; margin-top: 16px; padding: 14px; border-radius: 16px; background: #f8fafc; border: 1px solid #e2e8f0; }
    .summary strong { display: block; margin-top: 4px; font-size: 18px; color: #0f172a; }
    aside { display: grid; gap: 16px; }
    .card { padding: 20px; }
    .status { border-radius: 18px; padding: 18px; border: 1px solid #bfdbfe; background: #eff6ff; color: #1e3a8a; }
    .status.success { border-color: #bbf7d0; background: #ecfdf5; color: #166534; }
    .status.error { border-color: #fecaca; background: #fef2f2; color: #991b1b; }
    .status.warn { border-color: #fde68a; background: #fffbeb; color: #92400e; }
    .bar { height: 8px; border-radius: 999px; background: rgba(255,255,255,.75); overflow: hidden; margin-top: 14px; }
    .bar span { display: block; height: 100%; width: 0; background: currentColor; transition: width .25s ease; }
    dl { display: grid; gap: 10px; margin: 0; }
    .row { background: #f8fafc; border-radius: 14px; padding: 12px; }
    dt { font-size: 12px; font-weight: 800; color: #64748b; }
    dd { margin: 4px 0 0; font-weight: 900; overflow-wrap: anywhere; }
    .note { margin-top: 14px; border: 1px solid #fed7aa; background: #fff7ed; color: #9a3412; border-radius: 16px; padding: 13px; font-size: 14px; font-weight: 700; }
    @media (max-width: 820px) { main { width: min(100% - 22px, 1180px); padding: 18px 0; } .grid, .models, .fields, .summary { grid-template-columns: 1fr; } .wide { grid-column: auto; } }
  </style>
</head>
<body>
  <main>
    <header>
      <span class="pill">Backend-hosted live STK simulator</span>
      <div>
        <h1>Netily system payment lab</h1>
        <p>Use this page when the frontend deployment is stale or blocked. It is served by Django and posts directly to the live simulator endpoint on the API service.</p>
      </div>
    </header>

    <section class="grid">
      <div>
        <div class="models">
          <button class="model" id="directBtn" type="button">Direct-to-tenant model<br><small>BYOP settlement simulation.</small></button>
          <button class="model active" id="passBtn" type="button">Netily passthrough model<br><small>Central collection with payout math.</small></button>
        </div>
        <form id="paymentForm">
          <div class="fields">
            <label class="wide">Simulator test key <input id="testKey" type="password" autocomplete="off" required placeholder="Private test key"></label>
            <label>Phone number <input id="phone" inputmode="tel" required placeholder="2547XXXXXXXX"></label>
            <label>Amount <input id="amount" inputmode="numeric" required value="10"></label>
            <label>Tenant reference <input id="tenantCode" value="DEMO"></label>
            <label>Passthrough fee % <input id="feeRate" inputmode="decimal" value="2"></label>
          </div>
          <div class="summary">
            <div>Gross<strong id="gross">KES 10</strong></div>
            <div>Netily fee<strong id="fee">KES 0</strong></div>
            <div>Tenant payout<strong id="payout">KES 10</strong></div>
          </div>
          <button class="primary" id="submitBtn" type="submit">Send live STK push</button>
          <div class="note">Simulation only. No tenant subscription payment, invoice, wallet, payout, or tenant unlock is created.</div>
        </form>
      </div>

      <aside>
        <div class="status" id="statusBox">
          <strong id="statusTitle">Ready</strong>
          <p id="statusText">Fill in the tester phone number and send a live STK push.</p>
          <div class="bar"><span id="bar"></span></div>
        </div>
        <div class="card">
          <h2>Simulation receipt</h2>
          <dl>
            <div class="row"><dt>Checkout ID</dt><dd id="checkout">Not started</dd></div>
            <div class="row"><dt>Receipt</dt><dd id="receipt">Pending</dd></div>
            <div class="row"><dt>Reference</dt><dd id="reference">NET-DEMO</dd></div>
            <div class="row"><dt>Destination</dt><dd id="destination">Netily system Equity paybill</dd></div>
          </dl>
        </div>
      </aside>
    </section>
  </main>

  <script>
    const initiateUrl = "/api/v1/billing/netily-system-payment/initiate/";
    const statusBase = "/api/v1/billing/netily-system-payment/status/";
    let model = "netily_passthrough";
    let pollTimer = null;
    let pollStart = 0;

    const els = {
      directBtn: document.getElementById("directBtn"),
      passBtn: document.getElementById("passBtn"),
      form: document.getElementById("paymentForm"),
      testKey: document.getElementById("testKey"),
      phone: document.getElementById("phone"),
      amount: document.getElementById("amount"),
      tenantCode: document.getElementById("tenantCode"),
      feeRate: document.getElementById("feeRate"),
      submitBtn: document.getElementById("submitBtn"),
      gross: document.getElementById("gross"),
      fee: document.getElementById("fee"),
      payout: document.getElementById("payout"),
      statusBox: document.getElementById("statusBox"),
      statusTitle: document.getElementById("statusTitle"),
      statusText: document.getElementById("statusText"),
      bar: document.getElementById("bar"),
      checkout: document.getElementById("checkout"),
      receipt: document.getElementById("receipt"),
      reference: document.getElementById("reference"),
      destination: document.getElementById("destination"),
    };

    function money(value) {
      const amount = Number(value || 0);
      return new Intl.NumberFormat("en-KE", { style: "currency", currency: "KES", maximumFractionDigits: 0 }).format(Number.isFinite(amount) ? amount : 0);
    }

    function updatePreview() {
      const gross = Number(els.amount.value || 0);
      const rate = Number(els.feeRate.value || 0);
      const fee = model === "netily_passthrough" ? Math.round((gross * rate) / 100) : 0;
      els.gross.textContent = money(gross);
      els.fee.textContent = money(fee);
      els.payout.textContent = money(Math.max(gross - fee, 0));
      els.feeRate.disabled = model === "direct_tenant";
    }

    function setModel(nextModel) {
      model = nextModel;
      els.directBtn.classList.toggle("active", model === "direct_tenant");
      els.passBtn.classList.toggle("active", model === "netily_passthrough");
      updatePreview();
    }

    function showStatus(type, title, text) {
      els.statusBox.className = "status" + (type ? " " + type : "");
      els.statusTitle.textContent = title;
      els.statusText.textContent = text;
    }

    async function readJson(response) {
      const text = await response.text();
      try { return JSON.parse(text); }
      catch { return { success: false, message: text.replace(/<[^>]+>/g, " ").replace(/\\s+/g, " ").trim().slice(0, 180) || "Unexpected non-JSON response." }; }
    }

    async function poll(checkoutId) {
      pollStart = Date.now();
      clearInterval(pollTimer);
      pollTimer = setInterval(async () => {
        const elapsed = Date.now() - pollStart;
        els.bar.style.width = Math.min(100, Math.round((elapsed / 30000) * 100)) + "%";
        if (elapsed >= 30000) {
          clearInterval(pollTimer);
          els.submitBtn.disabled = false;
          showStatus("warn", "Still pending", "The 30 second window ended. The callback may still arrive, but no system records are changed.");
          return;
        }
        try {
          const response = await fetch(statusBase + encodeURIComponent(checkoutId) + "/", {
            headers: { "X-Netily-System-Payment-Token": els.testKey.value.trim() },
            cache: "no-store",
          });
          const data = await readJson(response);
          if (!response.ok) throw new Error(data.message || "Could not read status.");
          if (data.status === "completed") {
            clearInterval(pollTimer);
            els.submitBtn.disabled = false;
            els.receipt.textContent = data.mpesa_receipt || "Received";
            showStatus("success", "Payment received", data.last_result_desc || "Daraja confirmed the STK payment.");
          } else if (data.status === "failed" || data.status === "cancelled") {
            clearInterval(pollTimer);
            els.submitBtn.disabled = false;
            showStatus("error", data.status === "cancelled" ? "Prompt cancelled" : "Payment failed", data.last_result_desc || "You can retry safely.");
          }
        } catch (error) {
          showStatus("warn", "Polling issue", error.message || "Could not poll status.");
        }
      }, 3000);
    }

    els.directBtn.addEventListener("click", () => setModel("direct_tenant"));
    els.passBtn.addEventListener("click", () => setModel("netily_passthrough"));
    els.amount.addEventListener("input", updatePreview);
    els.feeRate.addEventListener("input", updatePreview);
    els.tenantCode.addEventListener("input", () => { els.reference.textContent = "NET-" + (els.tenantCode.value || "DEMO").toUpperCase().replace(/\\s+/g, "").slice(0, 8); });

    els.form.addEventListener("submit", async (event) => {
      event.preventDefault();
      clearInterval(pollTimer);
      els.submitBtn.disabled = true;
      els.bar.style.width = "8%";
      showStatus("", "Sending STK push", "Contacting Daraja through the Netily API service.");
      try {
        const response = await fetch(initiateUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-Netily-System-Payment-Token": els.testKey.value.trim(),
          },
          body: JSON.stringify({
            model,
            phone_number: els.phone.value,
            amount: els.amount.value,
            tenant_code: els.tenantCode.value,
            fee_rate: els.feeRate.value,
            test_key: els.testKey.value.trim(),
          }),
        });
        const data = await readJson(response);
        if (!response.ok || data.success === false) throw new Error(data.message || "The STK request was not accepted.");
        els.checkout.textContent = data.checkout_request_id || "Pending";
        els.reference.textContent = data.account_reference || els.reference.textContent;
        els.destination.textContent = data.destination_label || "Netily system Equity paybill";
        showStatus("", "Awaiting M-Pesa PIN", data.customer_message || "Approve the prompt on the tester phone.");
        poll(data.checkout_request_id);
      } catch (error) {
        els.submitBtn.disabled = false;
        showStatus("error", "Could not send STK", error.message || "Payment simulation could not start.");
      }
    });

    updatePreview();
  </script>
</body>
</html>
"""
    return HttpResponse(html, content_type="text/html")
