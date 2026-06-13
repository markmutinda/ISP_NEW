import logging
import re
import shutil
from pathlib import Path

from celery import shared_task
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.conf import settings
from django.db import connection
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django_tenants.utils import get_public_schema_name, schema_context

from apps.core.models import Changelog, Tenant
from apps.notifications.models import Notification
from apps.notifications.services import NotificationManager
from apps.superadmin.models import TenantDeletionJob


logger = logging.getLogger(__name__)
User = get_user_model()
PROTECTED_SCHEMAS = {"public", "information_schema", "pg_catalog", "pg_toast"}
SCHEMA_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

STEP_PROGRESS = {
    TenantDeletionJob.STEP_QUEUED: 0,
    TenantDeletionJob.STEP_REVOKING_ACCESS: 15,
    TenantDeletionJob.STEP_CLEANING_STORAGE: 35,
    TenantDeletionJob.STEP_CLEANING_INTEGRATIONS: 60,
    TenantDeletionJob.STEP_DROPPING_SCHEMA: 82,
    TenantDeletionJob.STEP_DELETING_RECORDS: 94,
    TenantDeletionJob.STEP_COMPLETED: 100,
    TenantDeletionJob.STEP_FAILED: 100,
}


def queue_changelog_notifications(changelog_id: int, channels: list[str]) -> None:
    if not channels:
        return
    try:
        deliver_changelog_notifications.delay(changelog_id, channels)
    except Exception:
        logger.exception("Failed to queue changelog notifications, running inline.")
        deliver_changelog_notifications(changelog_id, channels)


def queue_tenant_deletion(job_id: str) -> None:
    try:
        process_tenant_deletion.delay(job_id)
    except Exception:
        logger.exception("Failed to queue tenant deletion job %s, running inline.", job_id)
        process_tenant_deletion(job_id)


def _record_job_step(
    job: TenantDeletionJob,
    step: str,
    message: str,
    *,
    status_value: str | None = None,
    error_message: str = "",
    extra_summary: dict | None = None,
) -> None:
    job.status = status_value or job.status
    job.current_step = step
    job.progress_percent = STEP_PROGRESS.get(step, job.progress_percent)
    job.status_message = message
    job.error_message = error_message
    summary = dict(job.cleanup_summary or {})
    if extra_summary:
        summary.update(extra_summary)
    job.cleanup_summary = summary
    history = list(job.step_history or [])
    history.append(
        {
            "step": step,
            "status": job.status,
            "message": message,
            "timestamp": timezone.now().isoformat(),
        }
    )
    job.step_history = history
    if job.status == TenantDeletionJob.STATUS_RUNNING and not job.started_at:
        job.started_at = timezone.now()
    if job.status in (TenantDeletionJob.STATUS_COMPLETED, TenantDeletionJob.STATUS_FAILED):
        job.finished_at = timezone.now()
    job.save(
        update_fields=[
            "status",
            "current_step",
            "progress_percent",
            "status_message",
            "error_message",
            "cleanup_summary",
            "step_history",
            "started_at",
            "finished_at",
            "updated_at",
        ]
    )


def _revoke_sessions_for_users(user_ids: list[int]) -> int:
    if not user_ids:
        return 0
    revoked = 0
    for session in Session.objects.all().iterator():
        try:
            data = session.get_decoded()
        except Exception:
            continue
        auth_user_id = data.get("_auth_user_id")
        if auth_user_id and int(auth_user_id) in user_ids:
            session.delete()
            revoked += 1
    return revoked


def _safe_remove_tree(path: Path, media_root: Path) -> bool:
    try:
        resolved_path = path.resolve(strict=False)
        resolved_root = media_root.resolve(strict=False)
    except Exception:
        return False
    if resolved_path == resolved_root or resolved_root not in resolved_path.parents:
        return False
    if not resolved_path.exists():
        return False
    shutil.rmtree(resolved_path, ignore_errors=True)
    return True


def _cleanup_media_assets(job: TenantDeletionJob) -> dict:
    summary = {
        "media_paths_deleted": [],
        "company_logo_deleted": False,
    }
    media_root = Path(settings.MEDIA_ROOT)
    candidate_paths = [
        media_root / job.schema_name,
        media_root / job.subdomain,
        media_root / "tenants" / job.schema_name,
        media_root / "tenants" / job.subdomain,
    ]
    for path in candidate_paths:
        if _safe_remove_tree(path, media_root):
            summary["media_paths_deleted"].append(str(path.relative_to(media_root)))

    if job.tenant_id:
        tenant = Tenant.objects.select_related("company").filter(pk=job.tenant_id).first()
        company = getattr(tenant, "company", None)
        logo_field = getattr(company, "logo", None)
        logo_name = getattr(logo_field, "name", "")
        if logo_name:
            try:
                logo_field.storage.delete(logo_name)
                summary["company_logo_deleted"] = True
            except Exception:
                logger.warning("Failed to delete company logo for tenant deletion job %s", job.id, exc_info=True)
    return summary


def _cleanup_integrations(job: TenantDeletionJob) -> dict:
    summary = {
        "router_index_entries_deleted": 0,
        "router_map_entries_deleted": 0,
        "radius_registry_deleted": 0,
        "radius_cleanup_ok": True,
    }
    from apps.core.models import GlobalRouterMap, RouterTenantIndex

    summary["router_index_entries_deleted"] = RouterTenantIndex.objects.filter(
        tenant_id=job.tenant_id
    ).delete()[0]
    summary["router_map_entries_deleted"] = GlobalRouterMap.objects.filter(
        tenant_id=job.tenant_id
    ).delete()[0]

    try:
        from apps.radius.models import RadiusTenantConfig

        summary["radius_registry_deleted"] = RadiusTenantConfig.objects.filter(
            schema_name=job.schema_name
        ).delete()[0]
    except Exception:
        logger.warning("RadiusTenantConfig cleanup skipped for %s", job.schema_name, exc_info=True)

    try:
        from apps.radius.services.tenant_radius_service import tenant_radius_service

        tenant_radius_service.remove_tenant_radius(job.schema_name)
    except Exception:
        summary["radius_cleanup_ok"] = False
        logger.warning("RADIUS cleanup failed for %s", job.schema_name, exc_info=True)

    return summary


def _drop_tenant_schema(job: TenantDeletionJob) -> None:
    if job.schema_name in PROTECTED_SCHEMAS:
        raise ValueError(f"Refusing to drop protected schema '{job.schema_name}'.")
    if not SCHEMA_NAME_RE.match(job.schema_name):
        raise ValueError(f"Invalid schema name '{job.schema_name}'.")
    with connection.cursor() as cursor:
        cursor.execute('SET search_path TO "public"')
        cursor.execute(f'DROP SCHEMA IF EXISTS "{job.schema_name}" CASCADE')


@shared_task(bind=True)
def process_tenant_deletion(self, job_id: str) -> dict:
    public_schema = get_public_schema_name()

    with schema_context(public_schema):
        job = TenantDeletionJob.objects.select_related("tenant", "requested_by").get(pk=job_id)

        if job.status == TenantDeletionJob.STATUS_COMPLETED:
            return {"status": job.status, "job_id": str(job.id)}

        _record_job_step(
            job,
            TenantDeletionJob.STEP_REVOKING_ACCESS,
            f"Revoking access for {job.company_name}.",
            status_value=TenantDeletionJob.STATUS_RUNNING,
        )

        try:
            with transaction.atomic():
                tenant = Tenant.objects.select_related("company").filter(pk=job.tenant_id).first()
                company = getattr(tenant, "company", None)
                if tenant and tenant.status != "suspended":
                    tenant.status = "suspended"
                    tenant.save(update_fields=["status", "updated_at"])

                public_users = list(
                    User.objects.filter(
                        Q(tenant_id=job.tenant_id) | Q(company__tenant__schema_name=job.schema_name)
                    )
                    .distinct()
                    .values_list("id", flat=True)
                )
                disabled_public_users = User.objects.filter(id__in=public_users).update(is_active=False)
                revoked_sessions = _revoke_sessions_for_users(public_users)

                tenant_user_count = 0
                try:
                    with schema_context(job.schema_name):
                        tenant_user_count = User.objects.all().update(is_active=False)
                except Exception:
                    logger.warning("Tenant-schema user deactivation skipped for %s", job.schema_name, exc_info=True)

            _record_job_step(
                job,
                TenantDeletionJob.STEP_CLEANING_STORAGE,
                "Cleaning tenant storage and uploaded assets.",
                extra_summary={
                    "public_users_disabled": disabled_public_users,
                    "tenant_users_disabled": tenant_user_count,
                    "sessions_revoked": revoked_sessions,
                },
            )

            media_summary = _cleanup_media_assets(job)
            _record_job_step(
                job,
                TenantDeletionJob.STEP_CLEANING_INTEGRATIONS,
                "Cleaning router indexes, RADIUS hooks, and shared integrations.",
                extra_summary=media_summary,
            )

            integration_summary = _cleanup_integrations(job)
            _record_job_step(
                job,
                TenantDeletionJob.STEP_DROPPING_SCHEMA,
                "Dropping tenant database schema.",
                extra_summary=integration_summary,
            )

            _record_job_step(
                job,
                TenantDeletionJob.STEP_DROPPING_SCHEMA,
                "Dropping tenant database schema and purging all public records.",
            )

            # ── Hard purge: uses purge_tenant_completely which handles
            #    SubscriptionPayment → CompanySubscription → User → Domain
            #    → Tenant → Company in the correct FK order, then DROP SCHEMA
            from apps.core.tenant_purge import purge_tenant_completely

            purge_result = purge_tenant_completely(job.tenant_id)

            _record_job_step(
                job,
                TenantDeletionJob.STEP_DELETING_RECORDS,
                "Auditing deletion — recording final cleanup summary.",
                extra_summary=purge_result.as_dict(),
            )

            from apps.core.models import AuditLog

            AuditLog.log_action(
                user=job.requested_by,
                action="delete",
                model_name="Tenant",
                object_id=str(job.tenant_id) if job.tenant_id else "",
                object_repr=job.subdomain,
                changes={
                    "company_name": job.company_name,
                    "schema_name": job.schema_name,
                    "deletion_job_id": str(job.id),
                    "status": "completed",
                    "purge_summary": purge_result.as_dict(),
                },
            )

            _record_job_step(
                job,
                TenantDeletionJob.STEP_COMPLETED,
                f"{job.company_name} was permanently deleted.",
                status_value=TenantDeletionJob.STATUS_COMPLETED,
            )
            return {"status": job.status, "job_id": str(job.id)}
        except Exception as exc:
            logger.exception("Tenant deletion job %s failed", job.id)
            _record_job_step(
                job,
                TenantDeletionJob.STEP_FAILED,
                "Tenant deletion failed. Review the job details before retrying.",
                status_value=TenantDeletionJob.STATUS_FAILED,
                error_message=str(exc),
            )
            return {"status": job.status, "job_id": str(job.id), "error": str(exc)}
        finally:
            connection.set_schema_to_public()


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
