import json
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils.dateparse import parse_datetime
from django_tenants.utils import get_public_schema_name, schema_context

from apps.superadmin.financial_backup import SCHEMA_VERSION
from apps.superadmin.models import (
    BusinessAccountBalanceSnapshot,
    BusinessAccountTransfer,
    PlatformExpenditure,
    PlatformReceiptAllocation,
)


class Command(BaseCommand):
    help = "Validate or restore a platform financial JSON backup from stdin. Dry-run by default."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Create records missing from the database.")

    def _uuid(self, value, label):
        try:
            return UUID(str(value))
        except (TypeError, ValueError):
            raise CommandError(f"Invalid UUID for {label}.")

    def _money(self, value, label):
        if value is None:
            return None
        try:
            parsed = Decimal(str(value)).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError):
            raise CommandError(f"Invalid amount for {label}.")
        if not parsed.is_finite():
            raise CommandError(f"Invalid amount for {label}.")
        return parsed

    def _datetime(self, value, label, nullable=False):
        if nullable and not value:
            return None
        parsed = parse_datetime(str(value or ""))
        if not parsed or parsed.tzinfo is None:
            raise CommandError(f"Invalid timezone-aware timestamp for {label}.")
        return parsed

    def handle(self, *args, **options):
        try:
            payload = json.load(sys.stdin)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CommandError(f"Backup is not valid JSON: {exc}")
        if payload.get("schema") != "internetily.platform_financial_backup":
            raise CommandError("This is not an Internetily platform financial backup.")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise CommandError(f"Unsupported backup schema version. Expected {SCHEMA_VERSION}.")

        prepared = self._validate(payload)
        counts = {name: len(rows) for name, rows in prepared.items()}
        self.stdout.write(
            "Validated backup: " + ", ".join(f"{name}={count}" for name, count in counts.items())
        )
        if not options["apply"]:
            self.stdout.write("DRY RUN: no records changed. Re-run with --apply after reviewing these counts.")
            return

        created = {name: 0 for name in prepared}
        skipped = {name: 0 for name in prepared}
        with schema_context(get_public_schema_name()), transaction.atomic():
            for _, pk, values in prepared["expenditures"]:
                _, was_created = PlatformExpenditure.objects.get_or_create(pk=pk, defaults=values)
                created["expenditures"] += int(was_created)
                skipped["expenditures"] += int(not was_created)
            for _, pk, values in prepared["transfers"]:
                exists = BusinessAccountTransfer.objects.filter(Q(pk=pk) | Q(reference=values["reference"])).exists()
                if not exists:
                    BusinessAccountTransfer.objects.create(pk=pk, **values)
                created["transfers"] += int(not exists)
                skipped["transfers"] += int(exists)
            for _, pk, values in prepared["balance_snapshots"]:
                exists = BusinessAccountBalanceSnapshot.objects.filter(
                    Q(pk=pk) | Q(ledger=values["ledger"], balance_at=values["balance_at"])
                ).exists()
                if not exists:
                    BusinessAccountBalanceSnapshot.objects.create(pk=pk, **values)
                created["balance_snapshots"] += int(not exists)
                skipped["balance_snapshots"] += int(exists)
            for _, pk, values in prepared["receipt_allocations"]:
                exists = PlatformReceiptAllocation.objects.filter(
                    Q(pk=pk) | Q(source_type=values["source_type"], source_id=values["source_id"])
                ).exists()
                if not exists:
                    PlatformReceiptAllocation.objects.create(pk=pk, **values)
                created["receipt_allocations"] += int(not exists)
                skipped["receipt_allocations"] += int(exists)
            for row in payload.get("expenditures", []):
                if row.get("reverses_id"):
                    PlatformExpenditure.objects.filter(pk=row["id"], reverses__isnull=True).update(
                        reverses_id=row["reverses_id"]
                    )
        self.stdout.write(
            "Restore complete: " + ", ".join(
                f"{name} created={created[name]} existing={skipped[name]}" for name in prepared
            )
        )

    def _validate(self, payload):
        result = {"expenditures": [], "transfers": [], "balance_snapshots": [], "receipt_allocations": []}
        ledgers = {"primary", "new_business"}
        for row in payload.get("expenditures", []):
            pk = self._uuid(row.get("id"), "expenditure.id")
            try:
                incurred_on = date.fromisoformat(row.get("incurred_on", ""))
            except (TypeError, ValueError):
                raise CommandError("Invalid expenditure date.")
            if row.get("ledger") not in ledgers:
                raise CommandError("Invalid expenditure ledger.")
            if row.get("entry_type") not in dict(PlatformExpenditure.ENTRY_TYPE_CHOICES):
                raise CommandError("Invalid expenditure entry type.")
            if row.get("category") not in dict(PlatformExpenditure.CATEGORY_CHOICES):
                raise CommandError("Invalid expenditure category.")
            if not str(row.get("title") or "").strip():
                raise CommandError("Expenditure title cannot be empty.")
            values = {
                "ledger": row["ledger"], "entry_type": row["entry_type"], "category": row["category"],
                "title": row["title"], "reference": row.get("reference", ""),
                "amount": self._money(row.get("amount"), "expenditure.amount"),
                "currency": row.get("currency") or "KES", "incurred_on": incurred_on,
                "notes": row.get("notes", ""),
            }
            result["expenditures"].append((PlatformExpenditure, pk, values))
        for row in payload.get("transfers", []):
            if row.get("from_ledger") not in ledgers or row.get("to_ledger") not in ledgers:
                raise CommandError("Invalid transfer ledger.")
            if row.get("from_ledger") == row.get("to_ledger"):
                raise CommandError("Transfer source and destination must be different.")
            values = {
                "reference": row["reference"], "from_ledger": row["from_ledger"],
                "to_ledger": row["to_ledger"], "amount": self._money(row.get("amount"), "transfer.amount"),
                "effective_at": self._datetime(row.get("effective_at"), "transfer.effective_at"),
                "notes": row.get("notes", ""),
            }
            result["transfers"].append((BusinessAccountTransfer, self._uuid(row.get("id"), "transfer.id"), values))
        for row in payload.get("balance_snapshots", []):
            if row.get("ledger") not in ledgers:
                raise CommandError("Invalid balance snapshot ledger.")
            values = {
                "ledger": row["ledger"], "balance": self._money(row.get("balance"), "snapshot.balance"),
                "balance_at": self._datetime(row.get("balance_at"), "snapshot.balance_at"),
                "reference": row.get("reference", ""), "notes": row.get("notes", ""),
                "system_balance_before": self._money(row.get("system_balance_before"), "snapshot.system_balance_before"),
                "reconciliation_difference": self._money(row.get("reconciliation_difference"), "snapshot.reconciliation_difference"),
            }
            result["balance_snapshots"].append((BusinessAccountBalanceSnapshot, self._uuid(row.get("id"), "snapshot.id"), values))
        for row in payload.get("receipt_allocations", []):
            if row.get("ledger") not in ledgers or row.get("source_type") not in {"subscription", "sms"}:
                raise CommandError("Invalid receipt allocation.")
            values = {
                "source_type": row["source_type"], "source_id": row["source_id"],
                "reference": row.get("reference", ""), "ledger": row["ledger"],
                "occurred_at": self._datetime(row.get("occurred_at"), "allocation.occurred_at", nullable=True),
                "notes": row.get("notes", ""),
            }
            result["receipt_allocations"].append((PlatformReceiptAllocation, self._uuid(row.get("id"), "allocation.id"), values))
        return result
