import logging

from celery import shared_task
from django.contrib.auth import get_user_model
from django.db import connection
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context

from apps.core.models import Changelog, Tenant
from apps.notifications.models import Notification
from apps.notifications.services import NotificationManager


logger = logging.getLogger(__name__)
User = get_user_model()


def queue_changelog_notifications(changelog_id: int, channels: list[str]) -> None:
    if not channels:
        return
    try:
        deliver_changelog_notifications.delay(changelog_id, channels)
    except Exception:
        logger.exception("Failed to queue changelog notifications, running inline.")
        deliver_changelog_notifications(changelog_id, channels)


@shared_task(bind=True, autoretry_for=(Exception,), retry_backoff=True, max_retries=3)
def deliver_changelog_notifications(self, changelog_id: int, channels: list[str]) -> dict:
    public_schema = get_public_schema_name()
    manager = NotificationManager()

    with schema_context(public_schema):
        changelog = Changelog.objects.get(pk=changelog_id)
        tenants = list(Tenant.objects.select_related("company").filter(is_active=True))

    summary = {
        "channels": channels,
        "tenants_total": len(tenants),
        "tenants_processed": 0,
        "users_targeted": 0,
        "notifications_created": 0,
        "notifications_sent": 0,
        "notifications_failed": 0,
        "errors": [],
    }

    for tenant in tenants:
        summary["tenants_processed"] += 1
        try:
            with schema_context(tenant.schema_name):
                recipients = list(
                    User.objects.filter(is_active=True)
                    .exclude(role="customer")
                    .only("id", "email", "phone_number", "first_name", "role")
                )
                summary["users_targeted"] += len(recipients)

                for user in recipients:
                    subject = f"Netily update: {changelog.title}"
                    message = (
                        f"{changelog.title}\n\n"
                        f"Version: {changelog.version or 'Latest release'}\n"
                        f"Type: {changelog.get_update_type_display()}\n\n"
                        f"{changelog.content}"
                    )
                    metadata = {
                        "changelog_id": changelog.id,
                        "changelog_title": changelog.title,
                        "tenant_subdomain": tenant.subdomain,
                        "release_date": changelog.release_date.isoformat() if changelog.release_date else None,
                    }

                    if "in_app" in channels:
                        notification = Notification.objects.create(
                            user=user,
                            notification_type="in_app",
                            subject=subject,
                            message=message,
                            priority=3,
                            metadata=metadata,
                        )
                        summary["notifications_created"] += 1
                        if manager.send_notification(notification):
                            summary["notifications_sent"] += 1
                        else:
                            summary["notifications_failed"] += 1

                    if "email" in channels and user.email:
                        notification = Notification.objects.create(
                            user=user,
                            notification_type="email",
                            subject=subject,
                            message=message,
                            recipient_email=user.email,
                            priority=3,
                            metadata=metadata,
                        )
                        summary["notifications_created"] += 1
                        if manager.send_notification(notification):
                            summary["notifications_sent"] += 1
                        else:
                            summary["notifications_failed"] += 1

                    if "sms" in channels and user.phone_number:
                        notification = Notification.objects.create(
                            user=user,
                            notification_type="sms",
                            subject=subject,
                            message=f"Netily update: {changelog.title}. {changelog.version or ''}".strip(),
                            recipient_phone=user.phone_number,
                            priority=2,
                            metadata=metadata,
                        )
                        summary["notifications_created"] += 1
                        if manager.send_notification(notification):
                            summary["notifications_sent"] += 1
                        else:
                            summary["notifications_failed"] += 1
        except Exception as exc:
            logger.exception("Failed changelog notification delivery for tenant %s", tenant.schema_name)
            summary["errors"].append(
                {
                    "tenant": tenant.schema_name,
                    "error": str(exc),
                }
            )

    with schema_context(public_schema):
        Changelog.objects.filter(pk=changelog_id).update(
            notification_channels=channels,
            notification_sent_at=timezone.now(),
            notification_summary=summary,
        )

    connection.set_schema_to_public()
    return summary
