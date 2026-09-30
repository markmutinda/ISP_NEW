from django.core.management.base import BaseCommand
from django_tenants.utils import get_public_schema_name, schema_context

from apps.subscriptions.billing_lifecycle import complete_subscription_stk_payment
from apps.subscriptions.models import SubscriptionPayment


class Command(BaseCommand):
    help = (
        "Apply the subscription lifecycle for completed platform payments that "
        "were received but did not yet unlock or extend the tenant subscription."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show matching payments without applying the lifecycle repair.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Maximum number of payments to inspect. Default: 100.",
        )
        parser.add_argument(
            "--reference",
            action="append",
            default=[],
            help=(
                "Optional payment reference, receipt, checkout id, or UUID to repair. "
                "Can be supplied multiple times."
            ),
        )

    def handle(self, *args, **options):
        dry_run = bool(options["dry_run"])
        limit = max(1, int(options["limit"] or 100))
        references = [str(ref).strip() for ref in options.get("reference") or [] if str(ref).strip()]

        with schema_context(get_public_schema_name()):
            qs = (
                SubscriptionPayment.objects.select_related("subscription__company", "subscription__plan")
                .filter(status="completed", activation_applied_at__isnull=True)
                .order_by("completed_at", "created_at")
            )
            if references:
                qs = qs.filter(
                    models_q_for_references(references)
                )
            payments = list(qs[:limit])

        if not payments:
            self.stdout.write(self.style.SUCCESS("No completed unapplied subscription payments found."))
            return

        repaired = skipped = errors = 0
        for payment in payments:
            company_name = payment.subscription.company.name if payment.subscription and payment.subscription.company else "-"
            label = (
                f"{company_name} | payment={payment.id} | receipt={payment.mpesa_receipt or '-'} "
                f"| checkout={payment.payhero_checkout_id or '-'} | completed_at={payment.completed_at}"
            )
            if dry_run:
                self.stdout.write(f"Would repair: {label}")
                skipped += 1
                continue

            try:
                repaired_payment, invoice = complete_subscription_stk_payment(
                    payment,
                    mpesa_receipt=payment.mpesa_receipt or "",
                )
                if repaired_payment.activation_applied_at:
                    repaired += 1
                    invoice_label = getattr(invoice, "invoice_number", None) or getattr(invoice, "pk", None) or "-"
                    self.stdout.write(self.style.SUCCESS(f"Repaired: {label} | invoice={invoice_label}"))
                else:
                    skipped += 1
                    self.stdout.write(self.style.WARNING(f"Still pending invoice settlement: {label}"))
            except Exception as exc:
                errors += 1
                self.stderr.write(self.style.ERROR(f"Failed: {label} | {exc}"))

        summary = f"completed payment repair finished: repaired={repaired}, skipped={skipped}, errors={errors}"
        if errors:
            self.stderr.write(self.style.ERROR(summary))
        else:
            self.stdout.write(self.style.SUCCESS(summary))


def models_q_for_references(references):
    import uuid

    from django.db.models import Q

    query = Q()
    for ref in references:
        try:
            query |= Q(id=uuid.UUID(ref))
        except (TypeError, ValueError):
            pass
        query |= Q(mpesa_receipt__iexact=ref)
        query |= Q(payhero_checkout_id__iexact=ref)
        query |= Q(payhero_reference__iexact=ref)
    return query
