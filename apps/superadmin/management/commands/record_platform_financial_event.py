from decimal import Decimal, InvalidOperation

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.dateparse import parse_date
from django_tenants.utils import get_public_schema_name, schema_context

from apps.superadmin.models import PlatformExpenditure


class Command(BaseCommand):
    help = "Record an idempotent platform expense, fee, owner draw or cash adjustment."

    def add_arguments(self, parser):
        parser.add_argument("--account", choices=("primary", "new_business"), required=True)
        parser.add_argument("--entry-type", choices=[item[0] for item in PlatformExpenditure.ENTRY_TYPE_CHOICES], required=True)
        parser.add_argument("--category", choices=[item[0] for item in PlatformExpenditure.CATEGORY_CHOICES], required=True)
        parser.add_argument("--title", required=True)
        parser.add_argument("--amount", required=True)
        parser.add_argument("--date", required=True)
        parser.add_argument("--reference", required=True)
        parser.add_argument("--notes", default="")
        parser.add_argument("--apply", action="store_true")

    def handle(self, *args, **options):
        try:
            amount = Decimal(options["amount"]).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError):
            raise CommandError("Amount must be a valid number.")
        if not amount.is_finite() or amount == 0:
            raise CommandError("Amount must be finite and non-zero.")
        incurred_on = parse_date(options["date"])
        if not incurred_on:
            raise CommandError("Date must use YYYY-MM-DD format.")
        reference = options["reference"].strip().upper()
        if not reference or len(reference) > 120:
            raise CommandError("Reference must be between 1 and 120 characters.")

        values = {
            "ledger": options["account"],
            "entry_type": options["entry_type"],
            "category": options["category"],
            "title": options["title"].strip()[:160],
            "amount": amount,
            "currency": "KES",
            "incurred_on": incurred_on,
            "reference": reference,
            "notes": options["notes"].strip(),
        }
        if not options["apply"]:
            self.stdout.write(
                f"DRY RUN: {values['ledger']} "
                f"{values['entry_type']} KES {amount} on {incurred_on} ({reference}). No data changed."
            )
            return

        with schema_context(get_public_schema_name()), transaction.atomic():
            existing = PlatformExpenditure.objects.filter(ledger=values["ledger"], reference__iexact=reference).first()
            if existing:
                comparable = (existing.entry_type, existing.category, existing.amount, existing.incurred_on)
                requested = (values["entry_type"], values["category"], amount, incurred_on)
                if comparable != requested:
                    raise CommandError("That reference already exists with different details. No data changed.")
                created = False
                event = existing
            else:
                event = PlatformExpenditure.objects.create(**values)
                created = True
        self.stdout.write(
            f"{'Recorded' if created else 'Already recorded'}: {event.ledger} {event.entry_type} "
            f"KES {event.amount} on {event.incurred_on} ({event.reference})."
        )
