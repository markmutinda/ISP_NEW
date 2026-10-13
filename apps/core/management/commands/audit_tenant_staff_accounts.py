from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from django.db import DatabaseError
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django_tenants.utils import get_public_schema_name, schema_context

from apps.core.models import Tenant, User
from apps.subscriptions.system_accounts import SYSTEM_BILLING_EMAIL, get_tenant_billing_customer


DELEGATED_ROLES = {"staff", "technician", "accountant", "support"}
DASHBOARD_ROLES = DELEGATED_ROLES | {"admin"}


class Command(BaseCommand):
    help = (
        "Audit tenant-local dashboard accounts, report privilege/visibility drift, "
        "and optionally repair flags or deactivate a known account."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant",
            action="append",
            default=[],
            help="Tenant subdomain or schema name. Repeat to audit several tenants.",
        )
        parser.add_argument(
            "--since",
            help="Only show accounts created at or after this ISO-8601 timestamp.",
        )
        parser.add_argument(
            "--email",
            action="append",
            default=[],
            help="Only show this email address. Repeat for several addresses.",
        )
        parser.add_argument(
            "--suspicious-only",
            action="store_true",
            help="Hide accounts without a detected consistency or privilege warning.",
        )
        parser.add_argument(
            "--repair-flags",
            action="store_true",
            help="Remove unsafe customer flags and repair missing tenant identity. Never grants access.",
        )
        parser.add_argument(
            "--approve-staff-email",
            action="append",
            default=[],
            help="Grant the staff flag to an exact delegated-role email after review. Requires --tenant.",
        )
        parser.add_argument(
            "--normalize-system-accounts",
            action="store_true",
            help="Convert the NET-BILL identity into an inactive, non-login customer account.",
        )
        parser.add_argument(
            "--deactivate-email",
            action="append",
            default=[],
            help="Deactivate and revoke the password for an exact email match.",
        )

    def handle(self, *args, **options):
        since = self._parse_since(options.get("since"))
        tenant_selectors = {value.strip().lower() for value in options["tenant"] if value.strip()}
        email_filters = {value.strip().lower() for value in options["email"] if value.strip()}
        deactivate_emails = {
            value.strip().lower() for value in options["deactivate_email"] if value.strip()
        }
        approved_staff_emails = {
            value.strip().lower() for value in options["approve_staff_email"] if value.strip()
        }
        if approved_staff_emails and not tenant_selectors:
            raise CommandError("--approve-staff-email requires at least one explicit --tenant.")

        with schema_context(get_public_schema_name()):
            tenants = list(Tenant.objects.select_related("company").order_by("schema_name"))

        if tenant_selectors:
            tenants = [
                tenant for tenant in tenants
                if tenant.schema_name.lower() in tenant_selectors
                or str(getattr(tenant, "subdomain", "")).lower() in tenant_selectors
            ]
        if not tenants:
            raise CommandError("No matching tenants were found.")

        totals = {"tenants": 0, "accounts": 0, "warnings": 0, "repaired": 0, "deactivated": 0}
        for tenant in tenants:
            try:
                result = self._audit_tenant(
                    tenant,
                    since=since,
                    email_filters=email_filters,
                    suspicious_only=options["suspicious_only"],
                    repair_flags=options["repair_flags"],
                    approved_staff_emails=approved_staff_emails,
                    normalize_system_accounts=options["normalize_system_accounts"],
                    deactivate_emails=deactivate_emails,
                )
            except DatabaseError as exc:
                self.stderr.write(f"{tenant.schema_name}: skipped ({exc})")
                continue
            totals["tenants"] += 1
            for key in ("accounts", "warnings", "repaired", "deactivated"):
                totals[key] += result[key]

        self.stdout.write(
            self.style.SUCCESS(
                "Staff audit complete: "
                f"tenants={totals['tenants']} accounts={totals['accounts']} "
                f"warnings={totals['warnings']} repaired={totals['repaired']} "
                f"deactivated={totals['deactivated']}"
            )
        )

    def _audit_tenant(
        self,
        tenant,
        *,
        since,
        email_filters,
        suspicious_only,
        repair_flags,
        approved_staff_emails,
        normalize_system_accounts,
        deactivate_emails,
    ):
        counters = {"accounts": 0, "warnings": 0, "repaired": 0, "deactivated": 0}
        company_name = getattr(getattr(tenant, "company", None), "name", None)

        with schema_context(tenant.schema_name):
            if normalize_system_accounts:
                billing_user = User.objects.filter(email__iexact=SYSTEM_BILLING_EMAIL).first()
                needed_normalization = bool(
                    billing_user
                    and (
                        billing_user.role != "customer"
                        or billing_user.is_staff
                        or billing_user.is_superuser
                        or billing_user.is_active
                        or billing_user.has_usable_password()
                    )
                )
                get_tenant_billing_customer()
                if needed_normalization:
                    counters["repaired"] += 1
                    self.stdout.write(
                        f"\n[{tenant.schema_name}] normalized {SYSTEM_BILLING_EMAIL} as a non-login system customer"
                    )

            queryset = User.objects.filter(
                Q(role__in=DASHBOARD_ROLES) | Q(is_staff=True) | Q(is_superuser=True)
            ).order_by("created_at", "id")
            if since:
                queryset = queryset.filter(created_at__gte=since)
            if email_filters:
                queryset = queryset.filter(email__in=email_filters)

            users = list(queryset)
            platform_emails = {
                str(value).strip().lower()
                for value in (getattr(settings, "PLATFORM_SUPERADMIN_EMAILS", []) or [])
                if str(value).strip()
            }
            principal_candidates = [
                user for user in users
                if user.role == "admin"
                and user.is_superuser
                and str(user.email or "").lower() not in platform_emails
                and str(user.email or "").lower() != SYSTEM_BILLING_EMAIL
            ]
            principal_admin_id = principal_candidates[0].id if principal_candidates else None
            if users:
                self.stdout.write(
                    f"\n[{tenant.schema_name}] {getattr(tenant, 'subdomain', '-')}: {len(users)} account(s)"
                )

            for user in users:
                email = str(user.email or "").lower()
                flags = self._flags(user, tenant, principal_admin_id=principal_admin_id)
                changed_fields = []

                if repair_flags and not user.is_superuser:
                    # Repair is deliberately revoke-only. Never convert a role
                    # string from an old public registration into trusted access.
                    if user.role == "customer" and user.is_staff:
                        user.is_staff = False
                        changed_fields.append("is_staff")
                    if not user.tenant_subdomain and getattr(tenant, "subdomain", None):
                        user.tenant_subdomain = tenant.subdomain
                        changed_fields.append("tenant_subdomain")
                    if not user.company_name and company_name:
                        user.company_name = company_name
                        changed_fields.append("company_name")

                if email in approved_staff_emails and user.role in DELEGATED_ROLES:
                    if not user.is_staff:
                        user.is_staff = True
                        changed_fields.append("is_staff")

                if email and email in deactivate_emails and user.is_active:
                    user.is_active = False
                    user.set_unusable_password()
                    changed_fields.extend(["is_active", "password"])
                    counters["deactivated"] += 1

                if changed_fields:
                    changed_fields.append("updated_at")
                    user.save(update_fields=list(dict.fromkeys(changed_fields)))
                    counters["repaired"] += 1

                flags = self._flags(user, tenant, principal_admin_id=principal_admin_id)

                if suspicious_only and not flags and email not in deactivate_emails:
                    continue

                counters["accounts"] += 1
                counters["warnings"] += int(bool(flags))
                created = timezone.localtime(user.created_at).isoformat() if user.created_at else "unknown"
                status = "active" if user.is_active else "inactive"
                warning_text = ",".join(flags) if flags else "none"
                self.stdout.write(
                    f"- id={user.pk} email={user.email or '-'} role={user.role} "
                    f"status={status} staff={user.is_staff} superuser={user.is_superuser} "
                    f"trusted={user.dashboard_role_is_trusted} created={created} warnings={warning_text}"
                )

        return counters

    @staticmethod
    def _flags(user, tenant, *, principal_admin_id=None):
        flags = []
        email = str(user.email or "").lower()
        platform_emails = {
            str(value).strip().lower()
            for value in (getattr(settings, "PLATFORM_SUPERADMIN_EMAILS", []) or [])
            if str(value).strip()
        }
        if email == SYSTEM_BILLING_EMAIL:
            if (
                user.role != "customer"
                or user.is_staff
                or user.is_superuser
                or user.is_active
                or user.has_usable_password()
            ):
                flags.append("privileged_system_billing_account")
        elif user.role == "admin" and not user.is_superuser:
            flags.append("additional_non_principal_admin")
        elif user.role == "admin" and user.is_superuser and email in platform_emails:
            # Expected tenant-local mirror for a configured platform admin.
            pass
        elif (
            user.role == "admin"
            and user.is_superuser
            and principal_admin_id is not None
            and user.id != principal_admin_id
        ):
            flags.append("additional_superuser_admin")
        if user.role in DELEGATED_ROLES and not user.is_staff:
            flags.append("delegated_role_missing_staff_approval")
        if user.role in DELEGATED_ROLES and user.is_superuser:
            flags.append("delegated_role_is_superuser")
        if user.role == "customer" and user.is_staff:
            flags.append("customer_has_staff_flag")
        if user.role in DELEGATED_ROLES and not user.email:
            flags.append("staff_missing_email")
        tenant_subdomain = str(getattr(tenant, "subdomain", "") or "").lower()
        user_subdomain = str(user.tenant_subdomain or "").lower()
        if user_subdomain and tenant_subdomain and user_subdomain != tenant_subdomain:
            flags.append("tenant_identity_mismatch")
        return flags

    @staticmethod
    def _parse_since(value):
        if not value:
            return None
        parsed = parse_datetime(value)
        if parsed is None:
            raise CommandError("--since must be an ISO-8601 timestamp.")
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
        return parsed
