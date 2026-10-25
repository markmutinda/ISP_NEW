"""Read-only integrity report for the platform's two business-account ledgers."""

import json
from datetime import datetime
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django_tenants.utils import get_public_schema_name, schema_context

from apps.core.models import Tenant
from apps.messaging.models import SMSUnitTopup
from apps.subscriptions.models import SubscriptionPayment
from apps.superadmin.models import BusinessAccountBalanceSnapshot, BusinessAccountTransfer, PlatformExpenditure
from apps.superadmin.views import PROTECTED_SCHEMAS


CUTOVER_REFERENCE = "ACCD33A971EF0"
ACCOUNTS = ("primary", "new_business")


def money(value):
    return str((value or Decimal("0.00")).quantize(Decimal("0.01")))


class Command(BaseCommand):
    help = "Report platform ledger allocation, duplicate candidates, and reconciliation gaps without changing data."

    def add_arguments(self, parser):
        parser.add_argument("--start", help="Optional inclusive date, YYYY-MM-DD.")
        parser.add_argument("--end", help="Optional inclusive date, YYYY-MM-DD.")
        parser.add_argument("--json", action="store_true", help="Print structured JSON for audit or spreadsheet use.")
        parser.add_argument("--details", action="store_true", help="Include payment, SMS, duplicate, and transfer detail rows.")
        parser.add_argument(
            "--receipt-account", action="append", default=[], metavar="REFERENCE=ACCOUNT",
            help="Read-only allocation override for a subscription or SMS receipt. ACCOUNT is primary or new_business.",
        )
        parser.add_argument(
            "--transfer-direction", action="append", default=[], metavar="REFERENCE=FROM:TO",
            help="Read-only transfer direction override, for example ACCT2-ACCT1-001=new_business:primary.",
        )
        parser.add_argument(
            "--opening-balance", action="append", default=[], metavar="ACCOUNT=AMOUNT",
            help="Statement opening balance used only to calculate an estimated closing balance.",
        )
        parser.add_argument(
            "--known-transfer", action="append", default=[], metavar="REFERENCE=FROM:TO:AMOUNT:TIMESTAMP",
            help="Read-only transfer not yet represented in the old one-directional transfer table.",
        )

    def handle(self, *args, **options):
        start = self._date(options.get("start"), "start")
        end = self._date(options.get("end"), "end")
        if start and end and start > end:
            raise CommandError("--start cannot be later than --end.")
        receipt_overrides = self._account_overrides(options["receipt_account"], "--receipt-account")
        transfer_directions = self._transfer_overrides(options["transfer_direction"])
        opening_balances = self._money_overrides(options["opening_balance"], "--opening-balance")
        known_transfers = self._known_transfers(options["known_transfer"])

        with schema_context(get_public_schema_name()):
            cutover = self._cutover()
            cutover_at = cutover.completed_at if cutover else timezone.make_aware(
                datetime(2026, 9, 29, 16, 39, 59)
            )
            report = {
                "window": {"start": start.isoformat() if start else None, "end": end.isoformat() if end else None},
                "cutover": {
                    "reference": CUTOVER_REFERENCE,
                    "verified": bool(cutover),
                    "completed_at": cutover_at.isoformat(),
                    "company": self._company_name(cutover),
                },
                "allocation_overrides": {
                    "receipts": receipt_overrides,
                    "transfer_directions": transfer_directions,
                    "opening_balances": {account: money(amount) for account, amount in opening_balances.items()},
                    "known_transfers": known_transfers,
                },
                "subscriptions": self._subscriptions(start, end, cutover_at, receipt_overrides, options["details"]),
                "sms_topups": self._sms_topups(start, end, cutover_at, receipt_overrides, options["details"]),
                "manual_costs": self._manual_costs(start, end, options["details"]),
                "transfers": self._transfers(start, end, transfer_directions, known_transfers, options["details"]),
                "latest_balance_snapshots": self._snapshots(),
            }
            report["ledger_view"] = self._ledger_view(report, opening_balances)
            report["integrity_flags"] = self._flags(report)

        if options["json"]:
            self.stdout.write(json.dumps(report, indent=2, sort_keys=True, default=str))
            return
        self._text(report, options["details"])

    def _date(self, raw, label):
        if not raw:
            return None
        value = parse_date(raw)
        if not value:
            raise CommandError(f"--{label} must use YYYY-MM-DD.")
        return value

    def _account_overrides(self, values, option):
        result = {}
        for raw in values:
            reference, separator, account = raw.partition("=")
            reference, account = reference.strip().upper(), account.strip()
            if not separator or not reference or account not in ACCOUNTS:
                raise CommandError(f"{option} must use REFERENCE=primary or REFERENCE=new_business.")
            result[reference] = account
        return result

    def _transfer_overrides(self, values):
        result = {}
        for raw in values:
            reference, separator, direction = raw.partition("=")
            source, direction_separator, destination = direction.partition(":")
            reference, source, destination = reference.strip().upper(), source.strip(), destination.strip()
            if not separator or not direction_separator or not reference or source not in ACCOUNTS or destination not in ACCOUNTS or source == destination:
                raise CommandError("--transfer-direction must use REFERENCE=primary:new_business or REFERENCE=new_business:primary.")
            result[reference] = {"from": source, "to": destination}
        return result

    def _money_overrides(self, values, option):
        result = {}
        for raw in values:
            account, separator, amount = raw.partition("=")
            account = account.strip()
            try:
                parsed = Decimal(amount.strip()).quantize(Decimal("0.01"))
            except Exception as exc:
                raise CommandError(f"{option} needs a valid amount, for example primary=25000.00.") from exc
            if not separator or account not in ACCOUNTS:
                raise CommandError(f"{option} must use primary=AMOUNT or new_business=AMOUNT.")
            result[account] = parsed
        return result

    def _known_transfers(self, values):
        result = []
        for raw in values:
            reference, separator, payload = raw.partition("=")
            parts = payload.split(":", 3)
            if not separator or len(parts) != 4:
                raise CommandError("--known-transfer must use REFERENCE=FROM:TO:AMOUNT:TIMESTAMP.")
            source, destination, raw_amount, raw_timestamp = (part.strip() for part in parts)
            try:
                amount = Decimal(raw_amount).quantize(Decimal("0.01"))
                effective_at = parse_datetime(raw_timestamp)
            except Exception as exc:
                raise CommandError("--known-transfer has an invalid amount or ISO timestamp.") from exc
            if source not in ACCOUNTS or destination not in ACCOUNTS or source == destination or amount <= 0 or not effective_at or timezone.is_naive(effective_at):
                raise CommandError("--known-transfer needs different valid accounts, a positive amount, and a timestamp with timezone offset.")
            result.append({"reference": reference.strip().upper(), "from": source, "to": destination, "amount": amount, "effective_at": effective_at})
        return result

    def _cutover(self):
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

    def _company_name(self, payment):
        return (
            payment.subscription.company.name
            if payment and payment.subscription and payment.subscription.company
            else None
        )

    def _within(self, qs, field, start, end):
        if start:
            qs = qs.filter(**{f"{field}__date__gte": start})
        if end:
            qs = qs.filter(**{f"{field}__date__lte": end})
        return qs

    def _expected_account(self, completed_at, cutover_at):
        return "primary" if completed_at <= cutover_at else "new_business"

    def _empty_accounts(self):
        return {account: {"count": 0, "total": Decimal("0.00")} for account in ACCOUNTS}

    def _subscriptions(self, start, end, cutover_at, overrides, details):
        base_qs = SubscriptionPayment.objects.select_related("subscription__company").filter(status="completed")
        missing_date = [str(payment.id) for payment in base_qs.filter(completed_at__isnull=True)]
        qs = self._within(
            base_qs.filter(completed_at__isnull=False),
            "completed_at", start, end,
        )
        accounts = self._empty_accounts()
        explicit_conflicts = []
        rows = []
        for payment in qs.order_by("completed_at", "created_at"):
            expected = self._expected_account(payment.completed_at, cutover_at)
            reference = (payment.mpesa_receipt or payment.bank_reference or payment.payhero_reference or "").upper()
            override = overrides.get(reference)
            effective = override or payment.business_account or expected
            accounts[effective]["count"] += 1
            accounts[effective]["total"] += payment.amount
            if payment.business_account and payment.business_account != expected and not override:
                explicit_conflicts.append({
                    "id": str(payment.id), "reference": reference, "stored": payment.business_account,
                    "expected": expected, "completed_at": payment.completed_at.isoformat(), "amount": money(payment.amount),
                })
            if details:
                rows.append({
                    "id": str(payment.id), "company": self._company_name(payment), "reference": reference,
                    "amount": money(payment.amount), "completed_at": payment.completed_at.isoformat(),
                    "stored_account": payment.business_account, "effective_account": effective, "expected_account": expected,
                    "allocation": "report override" if override else ("stored account" if payment.business_account else "cutover-date inference"),
                })
        return {
            "accounts": self._serialise_accounts(accounts), "missing_completed_at": missing_date,
            "explicit_account_conflicts": explicit_conflicts, "rows": rows if details else [],
        }

    def _sms_topups(self, start, end, cutover_at, overrides, details):
        accounts = self._empty_accounts()
        missing_date = []
        rows = []
        tenants = Tenant.objects.exclude(schema_name__in=PROTECTED_SCHEMAS)
        for tenant in tenants.only("subdomain", "schema_name"):
            try:
                with schema_context(tenant.schema_name):
                    base_qs = SMSUnitTopup.objects.filter(status="completed")
                    missing_date.extend(
                        {"tenant": tenant.subdomain, "id": topup.id}
                        for topup in base_qs.filter(completed_at__isnull=True)
                    )
                    qs = self._within(base_qs.filter(completed_at__isnull=False), "completed_at", start, end)
                    for topup in qs.order_by("completed_at", "created_at"):
                        reference = (topup.payment_reference or topup.checkout_request_id or "").upper()
                        override = overrides.get(reference)
                        effective = override or self._expected_account(topup.completed_at, cutover_at)
                        accounts[effective]["count"] += 1
                        accounts[effective]["total"] += topup.amount_paid
                        if details:
                            rows.append({
                                "tenant": tenant.subdomain, "id": topup.id,
                                "reference": reference,
                                "amount": money(topup.amount_paid), "completed_at": topup.completed_at.isoformat(),
                                "effective_account": effective, "allocation": "report override" if override else "cutover-date inference",
                            })
            except Exception as exc:
                missing_date.append({"tenant": tenant.subdomain, "error": str(exc)})
        return {
            "accounts": self._serialise_accounts(accounts), "missing_or_unreadable": missing_date,
            "allocation_note": "SMS top-ups do not store a business-account allocation; every allocation is inferred from cutover date.",
            "rows": rows if details else [],
        }

    def _manual_costs(self, start, end, details):
        accounts = {}
        duplicate_candidates = []
        rows = []
        for account in ACCOUNTS:
            qs = PlatformExpenditure.objects.filter(ledger=account)
            if start:
                qs = qs.filter(incurred_on__gte=start)
            if end:
                qs = qs.filter(incurred_on__lte=end)
            operating_total = qs.filter(entry_type__in=("operating_expense", "bank_fee")).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
            owner_draws = qs.filter(entry_type="owner_draw").aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
            cash_adjustments = qs.filter(entry_type="cash_adjustment").aggregate(total=Sum("amount"))["total"] or Decimal("0.00")
            accounts[account] = {
                "entries": qs.count(), "operating_costs": operating_total,
                "owner_draws": owner_draws, "cash_adjustments": cash_adjustments,
                "net": operating_total + owner_draws + cash_adjustments,
            }
            duplicate_groups = (
                qs.values("incurred_on", "category", "title", "amount")
                .annotate(count=Count("id"))
                .filter(count__gt=1)
                .order_by("incurred_on", "title")
            )
            duplicate_candidates.extend([{"ledger": account, **group} for group in duplicate_groups])
            if details:
                rows.extend({
                    "id": str(item.id), "ledger": account, "date": item.incurred_on.isoformat(),
                    "entry_type": item.entry_type, "category": item.category, "title": item.title,
                    "reference": item.reference, "amount": money(item.amount),
                    "reverses": str(item.reverses_id) if item.reverses_id else None,
                } for item in qs.order_by("incurred_on", "created_at"))
        return {"accounts": self._serialise_manual_accounts(accounts), "duplicate_candidates": duplicate_candidates, "rows": rows if details else []}

    def _transfers(self, start, end, directions, known_transfers, details):
        qs = self._within(BusinessAccountTransfer.objects.all(), "effective_at", start, end)
        net_effect = {account: Decimal("0.00") for account in ACCOUNTS}
        rows = []
        assumed_direction_count = 0
        for item in qs.order_by("effective_at", "created_at"):
            direction = directions.get(item.reference.upper())
            if not direction:
                direction = {"from": item.from_ledger, "to": item.to_ledger}
            net_effect[direction["from"]] -= item.amount
            net_effect[direction["to"]] += item.amount
            if details:
                rows.append({
                    "reference": item.reference, "amount": money(item.amount), "effective_at": item.effective_at.isoformat(),
                    "from_account": direction["from"], "to_account": direction["to"],
                    "direction": "report override" if item.reference.upper() in directions else "stored direction",
                })
        included_known = []
        for item in known_transfers:
            effective_date = item["effective_at"].date()
            if (start and effective_date < start) or (end and effective_date > end):
                continue
            net_effect[item["from"]] -= item["amount"]
            net_effect[item["to"]] += item["amount"]
            included_known.append(item)
            if details:
                rows.append({
                    "reference": item["reference"], "amount": money(item["amount"]), "effective_at": item["effective_at"].isoformat(),
                    "from_account": item["from"], "to_account": item["to"], "direction": "read-only known transfer",
                })
        return {
            "count": qs.count() + len(included_known),
            "total": money((qs.aggregate(total=Sum("amount"))["total"] or Decimal("0.00")) + sum((item["amount"] for item in included_known), Decimal("0.00"))),
            "net_effect": {account: money(amount) for account, amount in net_effect.items()},
            "legacy_direction_assumptions": assumed_direction_count,
            "direction_assumption": "Stored transfer directions are used unless a read-only report override is supplied.",
            "rows": rows if details else [],
        }

    def _ledger_view(self, report, opening_balances):
        result = {}
        for account in ACCOUNTS:
            receipts = (
                Decimal(report["subscriptions"]["accounts"][account]["total"])
                + Decimal(report["sms_topups"]["accounts"][account]["total"])
            )
            operating_costs = Decimal(report["manual_costs"]["accounts"][account]["operating_costs"])
            owner_draws = Decimal(report["manual_costs"]["accounts"][account]["owner_draws"])
            cash_adjustments = Decimal(report["manual_costs"]["accounts"][account]["cash_adjustments"])
            operating_profit = receipts - operating_costs
            transfer_effect = Decimal(report["transfers"]["net_effect"][account])
            cash_movement = operating_profit - owner_draws - cash_adjustments + transfer_effect
            opening = opening_balances.get(account)
            result[account] = {
                "receipts": money(receipts), "operating_costs": money(operating_costs),
                "owner_draws": money(owner_draws), "cash_adjustments": money(cash_adjustments),
                "operating_profit": money(operating_profit), "transfer_effect": money(transfer_effect),
                "net_recorded_cash_movement": money(cash_movement),
                "opening_balance": money(opening) if opening is not None else None,
                "estimated_closing_balance": money(opening + cash_movement) if opening is not None else None,
            }
        return result

    def _snapshots(self):
        result = {}
        for account in ACCOUNTS:
            item = BusinessAccountBalanceSnapshot.objects.filter(ledger=account).order_by("-balance_at", "-created_at").first()
            result[account] = None if not item else {
                "balance": money(item.balance), "balance_at": item.balance_at.isoformat(),
                "reference": item.reference, "notes": item.notes,
            }
        return result

    def _flags(self, report):
        flags = []
        if not report["cutover"]["verified"]:
            flags.append("Cutover receipt is not verified; all date-based allocations are provisional.")
        if report["subscriptions"]["missing_completed_at"]:
            flags.append("Completed subscription receipts missing completion timestamps cannot be allocated reliably.")
        if report["subscriptions"]["explicit_account_conflicts"]:
            flags.append("Some explicit subscription account assignments conflict with the cutover-date rule.")
        if report["sms_topups"]["missing_or_unreadable"]:
            flags.append("Some SMS top-ups are missing a completion date or could not be read.")
        if report["manual_costs"]["duplicate_candidates"]:
            flags.append("Potential duplicate manual-cost records need human review; do not delete blindly.")
        if report["transfers"]["legacy_direction_assumptions"]:
            flags.append("One or more transfers still use the legacy Account 1 to Account 2 assumption. Supply --transfer-direction for any reverse transfer.")
        flags.append("Net recorded cash movement is not a bank balance until every receipt, transfer, opening balance, and bank-only movement is confirmed.")
        return flags

    def _serialise_accounts(self, accounts):
        return {key: {"count": value["count"], "total": money(value["total"])} for key, value in accounts.items()}

    def _serialise_manual_accounts(self, accounts):
        return {key: {field: money(value) if isinstance(value, Decimal) else value for field, value in fields.items()} for key, fields in accounts.items()}

    def _text(self, report, details):
        self.stdout.write("PLATFORM FINANCIAL INTEGRITY REPORT")
        self.stdout.write(f"Window: {report['window']['start'] or 'all time'} to {report['window']['end'] or 'today'}")
        self.stdout.write(f"Cutover: {report['cutover']['completed_at']} | verified={report['cutover']['verified']} | {CUTOVER_REFERENCE}")
        for account in ACCOUNTS:
            view = report["ledger_view"][account]
            self.stdout.write(
                f"{account}: receipts={view['receipts']} operating_costs={view['operating_costs']} "
                f"operating_profit={view['operating_profit']} transfer_effect={view['transfer_effect']} "
                f"net_recorded_cash_movement={view['net_recorded_cash_movement']} "
                f"estimated_closing_balance={view['estimated_closing_balance'] or 'opening balance not supplied'} "
                f"latest_snapshot={report['latest_balance_snapshots'][account] or 'not recorded'}"
            )
        self.stdout.write(f"Subscription account conflicts: {len(report['subscriptions']['explicit_account_conflicts'])}")
        self.stdout.write(f"Manual-cost duplicate candidates: {len(report['manual_costs']['duplicate_candidates'])}")
        self.stdout.write(f"Transfers: {report['transfers']['count']} | total={report['transfers']['total']}")
        for flag in report["integrity_flags"]:
            self.stdout.write(self.style.WARNING(f"FLAG: {flag}"))
        if details:
            self.stdout.write(json.dumps({
                "subscription_conflicts": report["subscriptions"]["explicit_account_conflicts"],
                "duplicate_candidates": report["manual_costs"]["duplicate_candidates"],
                "transfers": report["transfers"]["rows"],
            }, indent=2, default=str))
