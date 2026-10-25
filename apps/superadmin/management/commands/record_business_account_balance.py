from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.dateparse import parse_datetime
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context

from apps.superadmin.models import BusinessAccountBalanceSnapshot


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
            snapshot, created = BusinessAccountBalanceSnapshot.objects.update_or_create(
                ledger=options["account"],
                balance_at=balance_at,
                defaults={"balance": balance, "reference": reference, "notes": options["notes"].strip()},
            )
        self.stdout.write(
            f"{'Recorded' if created else 'Updated'}: {snapshot.ledger} balance KES {snapshot.balance} "
            f"at {snapshot.balance_at.isoformat()} ({snapshot.reference})."
        )
