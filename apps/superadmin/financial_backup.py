"""Portable, versioned backup for the platform financial reporting layer."""

from django.utils import timezone

from apps.superadmin.models import (
    BusinessAccountBalanceSnapshot,
    BusinessAccountTransfer,
    PlatformExpenditure,
    PlatformReceiptAllocation,
)


SCHEMA_VERSION = 1


def _text(value):
    return str(value) if value is not None else None


def build_financial_backup():
    expenditures = []
    for item in PlatformExpenditure.objects.all().order_by("incurred_on", "created_at"):
        expenditures.append({
            "id": str(item.id), "ledger": item.ledger, "entry_type": item.entry_type,
            "category": item.category, "title": item.title, "reference": item.reference,
            "amount": _text(item.amount), "currency": item.currency,
            "incurred_on": item.incurred_on.isoformat(), "notes": item.notes,
            "reverses_id": _text(item.reverses_id),
        })
    return {
        "schema": "internetily.platform_financial_backup",
        "schema_version": SCHEMA_VERSION,
        "exported_at": timezone.now().isoformat(),
        "expenditures": expenditures,
        "transfers": [{
            "id": str(item.id), "reference": item.reference,
            "from_ledger": item.from_ledger, "to_ledger": item.to_ledger,
            "amount": _text(item.amount), "effective_at": item.effective_at.isoformat(),
            "notes": item.notes,
        } for item in BusinessAccountTransfer.objects.all().order_by("effective_at", "created_at")],
        "balance_snapshots": [{
            "id": str(item.id), "ledger": item.ledger, "balance": _text(item.balance),
            "balance_at": item.balance_at.isoformat(), "reference": item.reference,
            "notes": item.notes, "system_balance_before": _text(item.system_balance_before),
            "reconciliation_difference": _text(item.reconciliation_difference),
        } for item in BusinessAccountBalanceSnapshot.objects.all().order_by("balance_at", "created_at")],
        "receipt_allocations": [{
            "id": str(item.id), "source_type": item.source_type, "source_id": item.source_id,
            "reference": item.reference, "ledger": item.ledger,
            "occurred_at": item.occurred_at.isoformat() if item.occurred_at else None,
            "notes": item.notes,
        } for item in PlatformReceiptAllocation.objects.all().order_by("created_at")],
    }
