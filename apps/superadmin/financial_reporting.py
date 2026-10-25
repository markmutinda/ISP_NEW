"""Financial reporting helpers for platform receipts, costs, cash and reconciliation."""

from collections import defaultdict
from datetime import date, datetime, time
from decimal import Decimal

from django.db.models import Q, Sum
from django.utils import timezone
from django_tenants.utils import schema_context

from apps.core.models import Tenant
from apps.messaging.models import SMSUnitTopup
from apps.subscriptions.models import SubscriptionPayment
from apps.superadmin.models import (
    BusinessAccountBalanceSnapshot,
    BusinessAccountTransfer,
    PlatformExpenditure,
)


ZERO = Decimal("0.00")
ACCOUNTS = (PlatformExpenditure.LEDGER_PRIMARY, PlatformExpenditure.LEDGER_NEW_BUSINESS)
CUTOVER_REFERENCE = "ACCD33A971EF0"


def money(value):
    return str((value or ZERO).quantize(Decimal("0.01")))


def cutover_payment():
    return (
        SubscriptionPayment.objects.select_related("subscription__company")
        .filter(status="completed", completed_at__isnull=False)
        .filter(
            Q(mpesa_receipt__iexact=CUTOVER_REFERENCE)
            | Q(bank_reference__iexact=CUTOVER_REFERENCE)
            | Q(payhero_reference__iexact=CUTOVER_REFERENCE)
        )
        .first()
    )


def cutover_time():
    payment = cutover_payment()
    return payment.completed_at if payment else timezone.make_aware(datetime(2026, 9, 29, 16, 39, 59))


def subscription_account(payment, boundary):
    if payment.business_account in ACCOUNTS:
        return payment.business_account, "explicit"
    return ("primary" if payment.completed_at <= boundary else "new_business"), "cutover_inference"


def sms_account(topup, boundary):
    return ("primary" if topup.completed_at <= boundary else "new_business"), "cutover_inference"


def _month_key(value):
    return value.strftime("%Y-%m")


def _month_label(key):
    return datetime.strptime(key, "%Y-%m").strftime("%b %Y")


def _month_keys(start, end):
    keys = []
    current = date(start.year, start.month, 1)
    final = date(end.year, end.month, 1)
    while current <= final:
        keys.append(_month_key(current))
        current = date(current.year + (1 if current.month == 12 else 0), 1 if current.month == 12 else current.month + 1, 1)
    return keys


def _new_bucket(key):
    return {
        "key": key,
        "label": _month_label(key),
        "subscriptions": ZERO,
        "sms": ZERO,
        "operating_expenses": ZERO,
        "owner_draws": ZERO,
        "cash_adjustments": ZERO,
        "transfer_in": ZERO,
        "transfer_out": ZERO,
    }


class FinancialReportBuilder:
    def __init__(self, start, end, protected_schemas):
        self.start = start
        self.end = end
        self.protected_schemas = protected_schemas
        self.boundary = cutover_time()
        self.months = {
            account: {key: _new_bucket(key) for key in _month_keys(start, end)}
            for account in (*ACCOUNTS, "combined")
        }
        self.totals = {
            account: defaultdict(lambda: ZERO)
            for account in (*ACCOUNTS, "combined")
        }
        self.events = []
        self.quality = defaultdict(int)

    def build(self):
        self._collect_subscriptions()
        self._collect_sms()
        self._collect_expenditure()
        self._collect_transfers()
        accounts = {account: self._account_summary(account) for account in ACCOUNTS}
        combined = self._account_summary("combined")
        cash = {account: self._cash_position(account) for account in ACCOUNTS}
        known_cash = [Decimal(item["estimated_balance"]) for item in cash.values() if item["estimated_balance"] is not None]
        return {
            "currency": "KES",
            "range": {"start": self.start.isoformat(), "end": self.end.isoformat()},
            "cutover": {"at": self.boundary.isoformat(), "reference": CUTOVER_REFERENCE, "verified": bool(cutover_payment())},
            "combined": combined,
            "accounts": accounts,
            "cash_positions": cash,
            "known_combined_cash": money(sum(known_cash, ZERO)) if len(known_cash) == len(ACCOUNTS) else None,
            "monthly": [self._serialise_bucket(self.months["combined"][key]) for key in self.months["combined"]],
            "monthly_by_account": {
                account: [self._serialise_bucket(self.months[account][key]) for key in self.months[account]]
                for account in ACCOUNTS
            },
            "forecast": self._forecast(),
            "expense_categories": self._expense_categories(),
            "recent_events": sorted(self.events, key=lambda item: item["occurred_at"], reverse=True)[:40],
            "data_quality": {
                "missing_subscription_dates": self.quality["missing_subscription_dates"],
                "inferred_subscription_allocations": self.quality["inferred_subscription_allocations"],
                "inferred_sms_allocations": self.quality["inferred_sms_allocations"],
                "unreconciled_accounts": sum(1 for item in cash.values() if item["snapshot"] is None),
                "message": "Cash is reliable from each account's latest confirmed snapshot forward. Inferred receipt allocations still need statement review.",
            },
        }

    def _in_range(self, value):
        local_date = timezone.localtime(value).date() if hasattr(value, "tzinfo") else value
        return self.start <= local_date <= self.end

    def _add(self, account, month, field, amount):
        for target in (account, "combined"):
            if month in self.months[target]:
                self.months[target][month][field] += amount
            self.totals[target][field] += amount

    def _collect_subscriptions(self):
        qs = SubscriptionPayment.objects.select_related("subscription__company").filter(status="completed")
        self.quality["missing_subscription_dates"] = qs.filter(completed_at__isnull=True).count()
        for payment in qs.filter(completed_at__isnull=False).iterator():
            if not self._in_range(payment.completed_at):
                continue
            account, allocation = subscription_account(payment, self.boundary)
            if allocation == "cutover_inference":
                self.quality["inferred_subscription_allocations"] += 1
            month = _month_key(timezone.localtime(payment.completed_at))
            self._add(account, month, "subscriptions", payment.amount)
            company = payment.subscription.company.name if payment.subscription and payment.subscription.company else "Tenant"
            self.events.append({
                "type": "subscription", "account": account, "direction": "in", "amount": money(payment.amount),
                "title": f"{company} subscription", "reference": payment.mpesa_receipt or payment.bank_reference or payment.payhero_reference or "",
                "occurred_at": payment.completed_at.isoformat(), "allocation": allocation,
            })

    def _collect_sms(self):
        tenants = Tenant.objects.exclude(schema_name__in=self.protected_schemas).only("subdomain", "schema_name")
        for tenant in tenants:
            try:
                with schema_context(tenant.schema_name):
                    for topup in SMSUnitTopup.objects.filter(status="completed", completed_at__isnull=False).iterator():
                        if not self._in_range(topup.completed_at):
                            continue
                        account, allocation = sms_account(topup, self.boundary)
                        self.quality["inferred_sms_allocations"] += 1
                        month = _month_key(timezone.localtime(topup.completed_at))
                        self._add(account, month, "sms", topup.amount_paid)
                        self.events.append({
                            "type": "sms", "account": account, "direction": "in", "amount": money(topup.amount_paid),
                            "title": f"{tenant.subdomain} SMS top-up", "reference": topup.payment_reference or topup.checkout_request_id or "",
                            "occurred_at": topup.completed_at.isoformat(), "allocation": allocation,
                        })
            except Exception:
                self.quality["unreadable_sms_schemas"] += 1

    def _collect_expenditure(self):
        qs = PlatformExpenditure.objects.filter(incurred_on__gte=self.start, incurred_on__lte=self.end)
        for item in qs.iterator():
            month = _month_key(item.incurred_on)
            if item.entry_type in (PlatformExpenditure.ENTRY_OPERATING_EXPENSE, PlatformExpenditure.ENTRY_BANK_FEE):
                field = "operating_expenses"
            elif item.entry_type == PlatformExpenditure.ENTRY_OWNER_DRAW:
                field = "owner_draws"
            else:
                field = "cash_adjustments"
            self._add(item.ledger, month, field, item.amount)
            occurred_at = timezone.make_aware(datetime.combine(item.incurred_on, time.max)).isoformat()
            self.events.append({
                "type": item.entry_type, "account": item.ledger, "direction": "out" if item.amount > 0 else "in",
                "amount": money(abs(item.amount)), "title": item.title, "reference": item.reference,
                "occurred_at": occurred_at, "allocation": "explicit",
            })

    def _collect_transfers(self):
        start_at = timezone.make_aware(datetime.combine(self.start, time.min))
        end_at = timezone.make_aware(datetime.combine(self.end, time.max))
        for item in BusinessAccountTransfer.objects.filter(effective_at__range=(start_at, end_at)).iterator():
            month = _month_key(timezone.localtime(item.effective_at))
            self._add(item.from_ledger, month, "transfer_out", item.amount)
            self._add(item.to_ledger, month, "transfer_in", item.amount)
            self.events.append({
                "type": "transfer", "account": item.from_ledger, "to_account": item.to_ledger,
                "direction": "transfer", "amount": money(item.amount), "title": "Internal account transfer",
                "reference": item.reference, "occurred_at": item.effective_at.isoformat(), "allocation": "explicit",
            })

    def _account_summary(self, account):
        totals = self.totals[account]
        receipts = totals["subscriptions"] + totals["sms"]
        operating_profit = receipts - totals["operating_expenses"]
        cash_movement = operating_profit - totals["owner_draws"] - totals["cash_adjustments"] + totals["transfer_in"] - totals["transfer_out"]
        margin = (operating_profit / receipts * Decimal("100")) if receipts else ZERO
        return {
            "subscriptions": money(totals["subscriptions"]), "sms": money(totals["sms"]),
            "receipts": money(receipts), "operating_expenses": money(totals["operating_expenses"]),
            "owner_draws": money(totals["owner_draws"]), "cash_adjustments": money(totals["cash_adjustments"]),
            "transfer_in": money(totals["transfer_in"]), "transfer_out": money(totals["transfer_out"]),
            "operating_profit": money(operating_profit), "profit_margin": money(margin),
            "net_cash_movement": money(cash_movement),
        }

    def _cash_position(self, account):
        snapshot = BusinessAccountBalanceSnapshot.objects.filter(ledger=account).order_by("-balance_at", "-created_at").first()
        if not snapshot:
            return {"snapshot": None, "movements_after_snapshot": None, "estimated_balance": None, "confidence": "needs_snapshot"}
        receipts = ZERO
        for payment in SubscriptionPayment.objects.filter(status="completed", completed_at__gt=snapshot.balance_at).iterator():
            allocated, _ = subscription_account(payment, self.boundary)
            if allocated == account:
                receipts += payment.amount
        tenants = Tenant.objects.exclude(schema_name__in=self.protected_schemas).only("schema_name")
        for tenant in tenants:
            try:
                with schema_context(tenant.schema_name):
                    for topup in SMSUnitTopup.objects.filter(status="completed", completed_at__gt=snapshot.balance_at).iterator():
                        allocated, _ = sms_account(topup, self.boundary)
                        if allocated == account:
                            receipts += topup.amount_paid
            except Exception:
                continue
        # Expenditure is date-only, so a statement snapshot is treated as the
        # account's confirmed closing balance for that calendar date.
        outflows = PlatformExpenditure.objects.filter(
            ledger=account, incurred_on__gt=timezone.localtime(snapshot.balance_at).date()
        ).aggregate(total=Sum("amount"))["total"] or ZERO
        transfer_in = BusinessAccountTransfer.objects.filter(to_ledger=account, effective_at__gt=snapshot.balance_at).aggregate(total=Sum("amount"))["total"] or ZERO
        transfer_out = BusinessAccountTransfer.objects.filter(from_ledger=account, effective_at__gt=snapshot.balance_at).aggregate(total=Sum("amount"))["total"] or ZERO
        movement = receipts - outflows + transfer_in - transfer_out
        return {
            "snapshot": {
                "id": str(snapshot.id), "balance": money(snapshot.balance), "balance_at": snapshot.balance_at.isoformat(),
                "reference": snapshot.reference, "notes": snapshot.notes,
            },
            "movements_after_snapshot": {
                "receipts": money(receipts), "outflows": money(outflows),
                "transfer_in": money(transfer_in), "transfer_out": money(transfer_out), "net": money(movement),
            },
            "estimated_balance": money(snapshot.balance + movement),
            "confidence": "reconciled_anchor_plus_recorded_movements",
        }

    def _serialise_bucket(self, bucket):
        receipts = bucket["subscriptions"] + bucket["sms"]
        profit = receipts - bucket["operating_expenses"]
        cash = profit - bucket["owner_draws"] - bucket["cash_adjustments"] + bucket["transfer_in"] - bucket["transfer_out"]
        return {
            **{key: value for key, value in bucket.items() if key in ("key", "label")},
            "subscriptions": money(bucket["subscriptions"]), "sms": money(bucket["sms"]),
            "receipts": money(receipts), "expenses": money(bucket["operating_expenses"]),
            "owner_draws": money(bucket["owner_draws"]), "profit": money(profit), "cash_movement": money(cash),
        }

    def _forecast(self):
        rows = [self._serialise_bucket(bucket) for bucket in self.months["combined"].values()]
        completed = rows[:-1] if len(rows) > 1 else rows
        sample = completed[-3:]
        if not sample:
            return {"basis_months": 0, "monthly_receipts": "0.00", "monthly_expenses": "0.00", "monthly_profit": "0.00", "next_three_months_profit": "0.00"}
        divisor = Decimal(len(sample))
        receipts = sum((Decimal(row["receipts"]) for row in sample), ZERO) / divisor
        expenses = sum((Decimal(row["expenses"]) for row in sample), ZERO) / divisor
        profit = receipts - expenses
        return {
            "basis_months": len(sample), "monthly_receipts": money(receipts),
            "monthly_expenses": money(expenses), "monthly_profit": money(profit),
            "next_three_months_profit": money(profit * Decimal("3")),
        }

    def _expense_categories(self):
        rows = (
            PlatformExpenditure.objects.filter(
                incurred_on__gte=self.start,
                incurred_on__lte=self.end,
                entry_type__in=(PlatformExpenditure.ENTRY_OPERATING_EXPENSE, PlatformExpenditure.ENTRY_BANK_FEE),
            )
            .values("category")
            .annotate(total=Sum("amount"))
            .order_by("-total")
        )
        return [{"category": row["category"], "total": money(row["total"])} for row in rows]
