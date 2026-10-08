from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django_tenants.utils import get_public_schema_name, schema_context

from apps.superadmin.models import BusinessAccountTransfer


class Command(BaseCommand):
    help = "Record a transfer from the original business account to the new business account. Dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument("--amount", required=True)
        parser.add_argument("--effective-at", required=True, help="ISO timestamp with timezone offset")
        parser.add_argument("--reference", required=True, help="Unique bank transfer reference or internal audit reference")
        parser.add_argument("--apply", action="store_true", help="Save the transfer")

    def handle(self, *args, **options):
        try:
            amount = Decimal(options["amount"]).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError):
            raise CommandError("Enter a valid amount.")
        if not amount.is_finite() or amount <= 0:
            raise CommandError("Amount must be greater than zero.")

        effective_at = parse_datetime(options["effective_at"])
        if not effective_at or timezone.is_naive(effective_at):
            raise CommandError("Use an ISO timestamp with a timezone offset, such as 2026-09-06T23:29:00+03:00.")
        reference = options["reference"].strip().upper()
        if not reference or len(reference) > 100:
            raise CommandError("Reference must be between 1 and 100 characters.")

        with schema_context(get_public_schema_name()):
            if not options["apply"]:
                self.stdout.write(
                    f"DRY RUN: KES {amount} from Account 1 to Account 2 at "
                    f"{effective_at.isoformat()} (reference {reference}). No data changed."
                )
                return
            with transaction.atomic():
                transfer, created = BusinessAccountTransfer.objects.get_or_create(
                    reference=reference,
                    defaults={"amount": amount, "effective_at": effective_at},
                )
                if transfer.amount != amount or transfer.effective_at != effective_at:
                    raise CommandError("That reference already exists with different amount or time. No data changed.")

        self.stdout.write(
            f"{'Recorded' if created else 'Already recorded'}: KES {amount} from Account 1 to Account 2 "
            f"at {effective_at.isoformat()} (reference {reference})."
        )
