"""Superadmin financial CSV export and reviewed import."""

import csv
import io
from datetime import datetime
from decimal import Decimal, InvalidOperation
import logging

from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django_tenants.utils import get_public_schema_name, schema_context
from rest_framework import status
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.models import Tenant
from apps.messaging.models import SMSCreditLedger, SMSUnitTopup, TenantSMSWallet
from apps.subscriptions.models import CompanySubscription, SubscriptionPayment
from .models import PlatformExpenditure
from .permissions import IsSuperAdmin
from .serializers import PlatformExpenditureSerializer
from .views import (
    PROTECTED_SCHEMAS, SubscriptionPaymentListView, PlatformExpenditureView,
    _ensure_public, _log_action,
)
from rest_framework.permissions import IsAuthenticated


HEADERS = {
    "expenditure": ["date", "category", "title", "amount", "notes"],
    "expenditure-2": ["date", "category", "title", "amount", "notes"],
    "sms": ["tenant_subdomain", "reference", "completed_at", "units", "amount", "payment_method", "status"],
    "subscription-payments": [
        "tenant_subdomain", "reference", "completed_at", "amount", "payment_method",
        "billing_period", "phone_number", "business_account", "status",
    ],
}
logger = logging.getLogger(__name__)


def _safe_cell(value):
    text = str(value or "")
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else text


def _csv_response(kind, rows):
    stream = io.StringIO()
    writer = csv.writer(stream)
    writer.writerow(HEADERS[kind])
    for row in rows:
        writer.writerow([_safe_cell(value) if not isinstance(value, (int, Decimal)) else value for value in row])
    response = HttpResponse("\ufeff" + stream.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{kind}.csv"'
    response["Cache-Control"] = "no-store"
    return response


def _paid_at(raw):
    try:
        value = parse_datetime(raw) or (parse_date(raw) if len(raw) == 10 else None)
    except ValueError:
        return None
    if value and not hasattr(value, "hour"):
        from datetime import datetime, time
        value = datetime.combine(value, time.min)
    if value and timezone.is_naive(value):
        value = timezone.make_aware(value)
    return value


def _amount(raw):
    try:
        value = Decimal(raw)
        if not value.is_finite() or value.as_tuple().exponent < -2:
            return None
        return value.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _sms_reference(topup):
    if topup.payment_reference:
        return topup.payment_reference
    if topup.notes.startswith("Receipt: "):
        return topup.notes.removeprefix("Receipt: ").strip()
    return topup.checkout_request_id or ""


def _sms_reference_q(reference):
    return Q(payment_reference__iexact=reference) | Q(checkout_request_id__iexact=reference) | Q(notes__iexact=f"Receipt: {reference}")


class FinancialCSVView(APIView):
    permission_classes = [IsAuthenticated, IsSuperAdmin]
    parser_classes = [MultiPartParser, FormParser]

    def get(self, request, kind):
        _ensure_public()
        if kind not in HEADERS:
            return Response({"detail": "Unknown CSV type."}, status=404)
        if request.query_params.get("template") == "1":
            return _csv_response(kind, [])
        rows = []
        if kind.startswith("expenditure"):
            ledger = "primary" if kind == "expenditure" else "new_business"
            qs = PlatformExpenditure.objects.filter(ledger=ledger).order_by("incurred_on", "created_at")
            start, end = PlatformExpenditureView()._parse_range(request)
            if start:
                qs = qs.filter(incurred_on__gte=start)
            if end:
                qs = qs.filter(incurred_on__lte=end)
            rows = ((p.incurred_on.isoformat(), p.category, p.title, p.amount, p.notes) for p in qs.iterator())
        elif kind == "subscription-payments":
            cutover = PlatformExpenditureView()._cutover_payment()
            cutover_at = cutover.completed_at if cutover else datetime.fromisoformat("2026-09-29T16:39:59+03:00")
            qs = SubscriptionPayment.objects.select_related("subscription__company__tenant").order_by("created_at")
            search = request.query_params.get("search", "").strip()
            state = request.query_params.get("status", "").strip()
            if search:
                qs = qs.filter(Q(subscription__company__name__icontains=search) | Q(mpesa_receipt__icontains=search) | Q(bank_reference__icontains=search) | Q(payhero_reference__icontains=search) | Q(phone_number__icontains=search))
            if state:
                qs = qs.filter(status=state)
            rows = ((
                getattr(getattr(p.subscription, "company", None), "tenant", None).subdomain if p.subscription and getattr(getattr(p.subscription, "company", None), "tenant", None) else "",
                p.mpesa_receipt or p.bank_reference or p.payhero_reference or "",
                p.completed_at.isoformat() if p.completed_at else "",
                p.amount, p.payment_method, p.intended_billing_period or p.subscription.billing_period or "monthly",
                p.phone_number or "", p.business_account or ("primary" if p.completed_at <= cutover_at else "new_business") if p.completed_at else "", p.status,
            ) for p in qs.iterator())
        else:
            rows = self._sms_export_rows()
        return _csv_response(kind, rows)

    def _sms_export_rows(self):
        rows = []
        tenants = Tenant.objects.exclude(schema_name__in=PROTECTED_SCHEMAS)
        for tenant in tenants:
            try:
                with schema_context(tenant.schema_name):
                    for topup in SMSUnitTopup.objects.order_by("created_at").iterator():
                        rows.append((tenant.subdomain, _sms_reference(topup), topup.completed_at.isoformat() if topup.completed_at else "", topup.units_purchased, topup.amount_paid, topup.payment_method, topup.status))
            except Exception:
                logger.exception("SMS CSV export failed for schema %s", tenant.schema_name)
                raise
        return rows

    def post(self, request, kind):
        _ensure_public()
        if kind not in HEADERS:
            return Response({"detail": "Unknown CSV type."}, status=404)
        file = request.FILES.get("file")
        if not file or not file.name.lower().endswith(".csv") or file.size > 2 * 1024 * 1024:
            return Response({"detail": "Choose a CSV file smaller than 2 MB."}, status=400)
        try:
            content = file.read().decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(content, newline=""), strict=True)
            if not reader.fieldnames or not set(HEADERS[kind]).issubset(reader.fieldnames):
                return Response({"detail": f"CSV needs these columns: {', '.join(HEADERS[kind])}."}, status=400)
            rows = list(reader)
        except (UnicodeError, csv.Error):
            return Response({"detail": "Could not read CSV. Save it as UTF-8 and try again."}, status=400)
        if not rows or len(rows) > 500:
            return Response({"detail": "CSV must contain 1 to 500 rows."}, status=400)
        commit = request.data.get("commit") == "true"
        if commit and request.data.get("confirm") != "IMPORT":
            return Response({"detail": "Confirm the reviewed import before saving."}, status=400)
        results = []
        seen = set()
        for number, row in enumerate(rows, start=2):
            errors = []
            amount = _amount((row.get("amount") or "").strip())
            if amount is None or (amount == 0 if kind.startswith("expenditure") else amount <= 0):
                errors.append("Enter a valid non-zero amount." if kind.startswith("expenditure") else "Enter a positive amount.")
            if amount is not None and abs(amount) >= (Decimal("1000000000000") if kind.startswith("expenditure") else Decimal("100000000")):
                errors.append("Amount exceeds the supported limit.")
            if kind.startswith("expenditure"):
                payload, key = self._check_expenditure(kind, row, amount, errors)
            elif kind == "sms":
                payload, key = self._check_sms(row, amount, errors)
            else:
                payload, key = self._check_subscription(row, amount, errors)
            if key in seen:
                errors.append("Duplicate row in this file.")
            seen.add(key)
            label = (
                f"{payload.get('incurred_on', '')} | {payload.get('title', '')} | KES {payload.get('amount', '')}"
                if kind.startswith("expenditure") else
                f"{payload.get('tenant_subdomain', row.get('tenant_subdomain', ''))} | {payload.get('reference', '')} | KES {payload.get('amount', '')}"
            )
            results.append({"line": number, "label": label, "errors": errors, "payload": payload})
        if commit and any(item["errors"] for item in results):
            return Response({"detail": "Fix every flagged row before importing.", "rows": results}, status=400)
        if not commit:
            return Response({"rows": results, "valid": sum(not item["errors"] for item in results), "invalid": sum(bool(item["errors"]) for item in results)})
        if kind.startswith("expenditure"):
            ledger = "primary" if kind == "expenditure" else "new_business"
            with transaction.atomic():
                for item in results:
                    serializer = PlatformExpenditureSerializer(data=item["payload"])
                    serializer.is_valid(raise_exception=True)
                    serializer.save(created_by=request.user, ledger=ledger)
            _log_action(request.user, "import", "FinancialCSV", object_repr=f"{kind}: {len(results)} rows", request=request)
            return Response({"imported": len(results), "failed": 0, "rows": results})
        saved = 0
        for item in results:
            payload = item["payload"]
            if kind == "sms":
                try:
                    self._import_sms(payload)
                except Exception as exc:
                    logger.exception("SMS CSV import failed on line %s", item["line"])
                    item["errors"] = [str(exc)]
                    continue
            else:
                if SubscriptionPayment.objects.filter(Q(mpesa_receipt__iexact=payload["reference"]) | Q(bank_reference__iexact=payload["reference"]) | Q(payhero_reference__iexact=payload["reference"])).exists():
                    item["errors"] = ["This payment reference was imported already."]
                    continue
                payment_request = type("PaymentRequest", (), {"data": payload, "user": request.user, "META": request.META})()
                try:
                    with transaction.atomic():
                        response = SubscriptionPaymentListView().post(payment_request)
                except Exception:
                    logger.exception("Subscription CSV import failed on line %s", item["line"])
                    item["errors"] = ["Payment could not be imported. Check the server log before retrying."]
                    continue
                if response.status_code >= 400:
                    item["errors"] = [response.data.get("detail", "Payment could not be imported.")]
                    continue
            saved += 1
        _ensure_public()
        _log_action(request.user, "import", "FinancialCSV", object_repr=f"{kind}: {saved} rows", request=request)
        return Response({"imported": saved, "failed": len(results) - saved, "rows": results})

    def _check_expenditure(self, kind, row, amount, errors):
        payload = {"incurred_on": (row.get("date") or "").strip(), "category": (row.get("category") or "").strip(), "title": (row.get("title") or "").strip(), "amount": str(amount) if amount is not None else "", "notes": (row.get("notes") or "").strip(), "currency": "KES"}
        serializer = PlatformExpenditureSerializer(data=payload)
        if not serializer.is_valid():
            errors.extend(str(value) for values in serializer.errors.values() for value in values)
        ledger = "primary" if kind == "expenditure" else "new_business"
        key = (ledger, payload["incurred_on"], payload["category"], payload["title"].casefold(), payload["amount"])
        try:
            valid_date = parse_date(payload["incurred_on"])
        except ValueError:
            valid_date = None
        if valid_date and amount is not None and PlatformExpenditure.objects.filter(ledger=ledger, incurred_on=valid_date, category=payload["category"], title__iexact=payload["title"], amount=amount).exists():
            errors.append("This expenditure already exists in this account.")
        return payload, key

    def _tenant(self, row, errors):
        subdomain = (row.get("tenant_subdomain") or "").strip()
        tenant = Tenant.objects.exclude(schema_name__in=PROTECTED_SCHEMAS).filter(subdomain__iexact=subdomain).first()
        if not tenant:
            errors.append("Tenant subdomain was not found.")
        return tenant, subdomain

    def _check_sms(self, row, amount, errors):
        tenant, subdomain = self._tenant(row, errors)
        reference = (row.get("reference") or "").strip()
        paid = _paid_at((row.get("completed_at") or "").strip())
        try:
            units = int((row.get("units") or "").strip())
            if units <= 0 or units > 1000000000:
                raise ValueError
        except ValueError:
            units = 0
            errors.append("Units must be a positive whole number.")
        if not reference or len(reference) > 100:
            errors.append("Payment reference is required (up to 100 characters).")
        if not paid:
            errors.append("Completed date must be ISO format, for example 2026-10-09T12:30:00+03:00.")
        if (row.get("status") or "completed").strip() != "completed":
            errors.append("Only completed SMS receipts can be imported.")
        if tenant and reference:
            with schema_context(tenant.schema_name):
                if SMSUnitTopup.objects.filter(_sms_reference_q(reference)).exists():
                    errors.append("This SMS receipt already exists for the tenant.")
        payload = {"schema_name": tenant.schema_name if tenant else "", "tenant_subdomain": subdomain, "reference": reference, "completed_at": paid.isoformat() if paid else "", "units": units, "amount": str(amount) if amount else "", "payment_method": (row.get("payment_method") or "mpesa").strip()[:50]}
        return payload, (subdomain.casefold(), reference.casefold())

    def _check_subscription(self, row, amount, errors):
        tenant, subdomain = self._tenant(row, errors)
        reference = (row.get("reference") or "").strip()
        paid = _paid_at((row.get("completed_at") or "").strip())
        method = (row.get("payment_method") or "").strip()
        period = (row.get("billing_period") or "monthly").strip()
        account = (row.get("business_account") or "").strip()
        if not reference or len(reference) > 100:
            errors.append("Payment reference is required (up to 100 characters).")
        if not paid:
            errors.append("Completed date must be ISO format.")
        if (row.get("status") or "completed").strip() != "completed":
            errors.append("Only completed subscription payments can be imported.")
        if method not in SubscriptionPaymentListView.payment_methods or period not in SubscriptionPaymentListView.billing_periods:
            errors.append("Payment method or billing period is invalid.")
        if tenant and not CompanySubscription.objects.filter(company=tenant.company).exists():
            errors.append("This tenant has no subscription.")
        if reference and SubscriptionPayment.objects.filter(Q(mpesa_receipt__iexact=reference) | Q(bank_reference__iexact=reference) | Q(payhero_reference__iexact=reference)).exists():
            errors.append("This payment reference already exists. Imports never replace payments.")
        if paid and account and account != SubscriptionPaymentListView()._account_for_paid_at(paid):
            errors.append("Business account does not match the cutover date.")
        payload = {"tenant_id": str(tenant.id) if tenant else "", "reference": reference, "completed_at": paid.isoformat() if paid else "", "amount": str(amount) if amount else "", "payment_method": method, "billing_period": period, "phone_number": (row.get("phone_number") or "").strip(), "business_account": account, "apply_to_subscription": False, "notify_tenant": False}
        return payload, reference.casefold()

    def _import_sms(self, payload):
        with schema_context(payload["schema_name"]), transaction.atomic():
            if SMSUnitTopup.objects.select_for_update().filter(_sms_reference_q(payload["reference"])).exists():
                raise ValueError("Duplicate SMS receipt.")
            topup = SMSUnitTopup.objects.create(units_purchased=payload["units"], amount_paid=payload["amount"], payment_reference=payload["reference"], payment_method=payload["payment_method"], status="completed", completed_at=_paid_at(payload["completed_at"]), schema_name=payload["schema_name"], notes="Imported by superadmin")
            wallet, _ = TenantSMSWallet.objects.get_or_create(is_active=True, defaults={"sms_units": Decimal("0"), "sell_price_per_unit": Decimal("0.40")})
            wallet = TenantSMSWallet.objects.select_for_update().get(pk=wallet.pk)
            wallet.sms_units += Decimal(payload["units"])
            wallet.save(update_fields=["sms_units", "updated_at"])
            SMSCreditLedger.objects.create(wallet=wallet, entry_type="topup", units=Decimal(payload["units"]), unit_price=wallet.sell_price_per_unit, amount=Decimal(payload["amount"]), reference=payload["reference"], notes=f"Imported top-up #{topup.id}")
