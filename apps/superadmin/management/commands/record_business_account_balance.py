from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context

from apps.superadmin.models import BusinessAccountBalanceSnapshot
from apps.superadmin.financial_reporting import FinancialReportBuilder


PROTECTED_SCHEMAS = {"public", "information_schema", "pg_catalog", "pg_toast"}


class Command(BaseCommand):
    help = "Record a confirmed business-account statement balance used as a cash anchor."

    def add_arguments(self, parser):
        parser.add_argument("--account", choices=("primary", "new_business"), required=True)
        parser.add_argument("--balance", required=True)
        parser.add_argument("--balance-at", required=True, help="ISO timestamp with timezone offset")
        parser.add_argument("--reference", required=True)
        parser.add_argument("--notes", default="")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        try:
            balance = Decimal(options["balance"]).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError):
            raise CommandError("Balance must be a valid number.")
        if not balance.is_finite():
            raise CommandError("Balance must be finite.")
        balance_at = parse_datetime(options["balance_at"])
        if not balance_at or balance_at.tzinfo is None:
            raise CommandError("Balance time must be ISO format with a timezone offset.")
        if balance_at > timezone.now():
            raise CommandError("Balance time cannot be in the future.")
        reference = options["reference"].strip().upper()
        if not reference or len(reference) > 120:
            raise CommandError("Reference must be between 1 and 120 characters.")
        if not options["apply"]:
            self.stdout.write(
                f"DRY RUN: {options['account']} balance KES {balance} at {balance_at.isoformat()} "
                f"({reference}). No data changed."
            )
            return

        with schema_context(get_public_schema_name()), transaction.atomic():
            previous = BusinessAccountBalanceSnapshot.objects.filter(
                ledger=options["account"], balance_at__lt=balance_at
            ).order_by("-balance_at", "-created_at").first()
            expected = None
            difference = None
            if previous:
                builder = FinancialReportBuilder(
                    timezone.localdate(previous.balance_at), timezone.localdate(balance_at), PROTECTED_SCHEMAS
                )
                movement = builder.cash_movements(options["account"], previous.balance_at, balance_at)["net"]
                expected = previous.balance + movement
                difference = balance - expected
            snapshot, created = BusinessAccountBalanceSnapshot.objects.update_or_create(
                ledger=options["account"],
                balance_at=balance_at,
                defaults={
                    "balance": balance, "reference": reference, "notes": options["notes"].strip(),
                    "system_balance_before": expected, "reconciliation_difference": difference,
                },
            )
        self.stdout.write(
            f"{'Recorded' if created else 'Updated'}: {snapshot.ledger} balance KES {snapshot.balance} "
            f"at {snapshot.balance_at.isoformat()} ({snapshot.reference})."
        )
