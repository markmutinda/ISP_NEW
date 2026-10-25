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
        parser.add_argument("--from-account", choices=("primary", "new_business"), default="primary")
        parser.add_argument("--to-account", choices=("primary", "new_business"), default="new_business")
        parser.add_argument("--notes", default="")
        parser.add_argument(
            "--correct-direction",
            action="store_true",
            help="Correct only the direction of an existing matching reference, amount and timestamp.",
        )
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
        from_account = options.get("from_account", "primary")
        to_account = options.get("to_account", "new_business")
        if from_account == to_account:
            raise CommandError("Source and destination accounts must be different.")

        with schema_context(get_public_schema_name()):
            if not options["apply"]:
                self.stdout.write(
                    f"DRY RUN: KES {amount} from {from_account} to {to_account} at "
                    f"{effective_at.isoformat()} (reference {reference}). No data changed."
                )
                return
            with transaction.atomic():
                transfer, created = BusinessAccountTransfer.objects.get_or_create(
                    reference=reference,
                    defaults={
                        "amount": amount,
                        "effective_at": effective_at,
                        "from_ledger": from_account,
                        "to_ledger": to_account,
                        "notes": options.get("notes", "").strip(),
                    },
                )
                if transfer.amount != amount or transfer.effective_at != effective_at:
                    raise CommandError("That reference already exists with a different amount or time. No data changed.")
                direction_changed = transfer.from_ledger != from_account or transfer.to_ledger != to_account
                if direction_changed and not options.get("correct_direction"):
                    raise CommandError("That reference has a different direction. Review it, then use --correct-direction if appropriate.")
                if direction_changed:
                    transfer.from_ledger = from_account
                    transfer.to_ledger = to_account
                    transfer.notes = options.get("notes", "").strip() or transfer.notes
                    transfer.save(update_fields=["from_ledger", "to_ledger", "notes"])

        self.stdout.write(
            f"{'Recorded' if created else ('Direction corrected' if direction_changed else 'Already recorded')}: "
            f"KES {amount} from {from_account} to {to_account} "
            f"at {effective_at.isoformat()} (reference {reference})."
        )
