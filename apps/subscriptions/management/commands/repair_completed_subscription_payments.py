from django.core.management.base import BaseCommand
from django.db.models import Q
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
        parser.add_argument(
            "--company",
            action="append",
            default=[],
            help="Optional company/tenant name search. Can be supplied multiple times.",
        )
        parser.add_argument(
            "--newest",
            action="store_true",
            help="Inspect newest completed payments first instead of oldest first.",
        )
        parser.add_argument(
            "--include-applied-stuck",
            action="store_true",
            help=(
                "Also repair completed payments that already have activation_applied_at "
                "but whose subscription did not move beyond the payment period end."
            ),
        )

    def handle(self, *args, **options):
        dry_run = bool(options["dry_run"])
        limit = max(1, int(options["limit"] or 100))
        references = [str(ref).strip() for ref in options.get("reference") or [] if str(ref).strip()]
        companies = [str(name).strip() for name in options.get("company") or [] if str(name).strip()]
        include_applied_stuck = bool(options["include_applied_stuck"])
        order_by = ("-completed_at", "-created_at") if options["newest"] else ("completed_at", "created_at")

        with schema_context(get_public_schema_name()):
            qs = (
                SubscriptionPayment.objects.select_related("subscription__company", "subscription__plan")
                .filter(status="completed")
                .order_by(*order_by)
            )
            if not include_applied_stuck:
                qs = qs.filter(activation_applied_at__isnull=True)
            if references:
                qs = qs.filter(models_q_for_references(references))
            if companies:
                qs = qs.filter(models_q_for_companies(companies))
            payments = [
                payment
                for payment in qs[:limit]
                if payment.activation_applied_at is None or _is_applied_but_not_extended(payment)
            ]

        if not payments:
            self.stdout.write(self.style.SUCCESS("No completed unapplied subscription payments found."))
            return

        repaired = skipped = errors = previewed = 0
        for payment in payments:
            company_name = payment.subscription.company.name if payment.subscription and payment.subscription.company else "-"
            label = (
                f"{company_name} | payment={payment.id} | receipt={payment.mpesa_receipt or '-'} "
                f"| bank_ref={payment.bank_reference or '-'} | payhero_ref={payment.payhero_reference or '-'} "
                f"| checkout={payment.payhero_checkout_id or '-'} | completed_at={payment.completed_at} "
                f"| period_end={payment.period_end} | current_end={payment.subscription.current_period_end} "
                f"| activation_applied_at={payment.activation_applied_at or '-'}"
            )
            if dry_run:
                self.stdout.write(f"Would repair: {label}")
                previewed += 1
                continue

            try:
                if payment.activation_applied_at and _is_applied_but_not_extended(payment):
                    SubscriptionPayment.objects.filter(pk=payment.pk).update(activation_applied_at=None)
                    payment.activation_applied_at = None

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

        summary = (
            f"completed payment repair finished: previewed={previewed}, "
            f"repaired={repaired}, skipped={skipped}, errors={errors}"
        )
        if errors:
            self.stderr.write(self.style.ERROR(summary))
        else:
            self.stdout.write(self.style.SUCCESS(summary))


def models_q_for_references(references):
    import uuid

    query = Q()
    for ref in references:
        try:
            query |= Q(id=uuid.UUID(ref))
        except (TypeError, ValueError):
            pass
        query |= Q(mpesa_receipt__iexact=ref)
        query |= Q(bank_reference__iexact=ref)
        query |= Q(payhero_checkout_id__iexact=ref)
        query |= Q(payhero_reference__iexact=ref)
    return query


def models_q_for_companies(companies):
    query = Q()
    for company in companies:
        query |= Q(subscription__company__name__icontains=company)
    return query


def _is_applied_but_not_extended(payment):
    subscription = getattr(payment, "subscription", None)
    return bool(
        payment.activation_applied_at
        and payment.period_end
        and subscription
        and subscription.current_period_end
        and subscription.current_period_end <= payment.period_end
    )
