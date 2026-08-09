"""Default dashboard RBAC policies shared by tenant and superadmin tooling."""

from __future__ import annotations

from typing import Any


EDITABLE_RBAC_ROLES = ("staff", "technician", "accountant", "support")


def tokens(path: str, actions: tuple[str, ...] = ("view",)) -> list[str]:
    return [f"{path}::{action}" for action in actions]


DEFAULT_ROLE_ACCESS_POLICIES: dict[str, list[str]] = {
    "staff": (
        tokens("/admin")
        + tokens("/admin/users", ("view", "view_details", "add", "edit"))
        + tokens("/admin/dispatch", ("view", "view_details", "add", "edit"))
        + tokens("/admin/inventory", ("view", "view_details"))
        + tokens("/admin/tickets", ("view", "view_details", "add", "edit"))
        + tokens("/admin/leads", ("view", "view_details", "add", "edit"))
        + tokens("/admin/loyalty", ("view", "add", "edit"))
        + tokens("/admin/sms", ("view", "add"))
        + tokens("/admin/ads", ("view", "add", "edit"))
    ),
    "technician": (
        tokens("/admin")
        + tokens("/admin/olt", ("view", "add", "edit"))
        + tokens("/admin/onu", ("view", "add", "edit"))
        + tokens("/admin/routers", ("view", "view_details", "add", "edit"))
        + tokens("/admin/networks", ("view", "add", "edit"))
        + tokens("/admin/radius", ("view", "add", "edit"))
        + tokens("/admin/fup", ("view", "add", "edit"))
        + tokens("/admin/usage")
        + tokens("/admin/dispatch", ("view", "view_details", "edit"))
        + tokens("/admin/inventory", ("view", "view_details"))
        + tokens("/admin/tickets", ("view", "view_details", "edit"))
    ),
    "accountant": (
        tokens("/admin")
        + tokens("/admin/users", ("view", "view_details"))
        + tokens("/admin/invoices", ("view", "view_details", "add", "edit"))
        + tokens("/admin/payments", ("view", "view_details", "add"))
        + tokens("/admin/receipts", ("view", "view_details"))
        + tokens("/admin/vouchers", ("view", "add", "edit"))
        + tokens("/admin/payment-methods", ("view", "edit"))
        + tokens("/admin/analytics")
        + tokens("/admin/settings/billing", ("view", "edit"))
        + tokens("/admin/sms", ("view", "add"))
    ),
    "support": (
        tokens("/admin")
        + tokens("/admin/users", ("view", "view_details", "edit"))
        + tokens("/admin/dispatch", ("view", "view_details", "add", "edit"))
        + tokens("/admin/tickets", ("view", "view_details", "add", "edit"))
        + tokens("/admin/leads", ("view", "view_details", "add", "edit"))
        + tokens("/admin/loyalty", ("view", "add", "edit"))
        + tokens("/admin/sms", ("view", "add"))
        + tokens("/admin/ads", ("view", "add", "edit"))
        + tokens("/admin/inventory", ("view", "view_details"))
    ),
}


LEGACY_DEFAULT_ROLE_ACCESS_POLICIES: dict[str, list[str]] = {
    "staff": [
        "/admin", "/admin/users", "/admin/dispatch", "/admin/inventory", "/admin/tickets",
        "/admin/leads", "/admin/loyalty", "/admin/sms", "/admin/ads",
    ],
    "technician": [
        "/admin", "/admin/olt", "/admin/onu", "/admin/routers", "/admin/networks", "/admin/radius",
        "/admin/fup", "/admin/usage", "/admin/dispatch", "/admin/inventory", "/admin/tickets",
    ],
    "accountant": [
        "/admin", "/admin/users", "/admin/invoices", "/admin/payments", "/admin/receipts",
        "/admin/vouchers", "/admin/payment-methods", "/admin/analytics",
        "/admin/settings/billing", "/admin/sms",
    ],
    "support": [
        "/admin", "/admin/users", "/admin/dispatch", "/admin/tickets", "/admin/leads",
        "/admin/loyalty", "/admin/sms", "/admin/ads", "/admin/inventory",
    ],
}


def normalize_role_access_policies(*, reset_defaults: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """Create missing role policies and upgrade untouched legacy defaults.

    reset_defaults=True intentionally overwrites custom role policies. The default
    mode preserves custom tenant modifications and only updates policies that are
    missing or exactly match the old plain-path defaults.
    """
    from apps.core.models import RoleAccessPolicy

    summary: dict[str, Any] = {
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "custom_preserved": 0,
        "deduplicated": 0,
        "roles": {},
    }

    for role in EDITABLE_RBAC_ROLES:
        default_paths = list(DEFAULT_ROLE_ACCESS_POLICIES[role])
        legacy_paths = LEGACY_DEFAULT_ROLE_ACCESS_POLICIES.get(role, [])
        policies = list(RoleAccessPolicy.objects.filter(role=role).order_by("id"))
        policy = policies[0] if policies else None

        if len(policies) > 1:
            summary["deduplicated"] += len(policies) - 1
            if not dry_run:
                RoleAccessPolicy.objects.filter(role=role).exclude(id=policy.id).delete()

        if not policy:
            summary["created"] += 1
            summary["roles"][role] = "created"
            if not dry_run:
                RoleAccessPolicy.objects.create(role=role, allowed_paths=default_paths)
            continue

        current_paths = policy.allowed_paths or []
        default_without_dashboard = [
            path for path in default_paths
            if path not in {"/admin::view", "/admin"}
        ]
        legacy_without_dashboard = [
            path for path in legacy_paths
            if path != "/admin"
        ]
        current_set = set(current_paths)
        should_update = (
            reset_defaults
            or current_set == set(legacy_paths)
            or current_set == set(legacy_without_dashboard)
            or current_set == set(default_without_dashboard)
        )
        if should_update and set(current_paths) != set(default_paths):
            summary["updated"] += 1
            summary["roles"][role] = "reset" if reset_defaults else "normalized"
            if not dry_run:
                policy.allowed_paths = default_paths
                policy.save(update_fields=["allowed_paths", "updated_at"])
        elif should_update:
            summary["unchanged"] += 1
            summary["roles"][role] = "already_default"
        else:
            summary["custom_preserved"] += 1
            summary["roles"][role] = "custom_preserved"

    return summary
