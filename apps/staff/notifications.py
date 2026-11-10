import logging

from apps.core.event_sms import queue_event_sms

logger = logging.getLogger(__name__)


def notify_technician_assignment(job, company_name=""):
    try:
        tech = job.assigned_to
        if not tech or not (tech.phone or "").strip():
            return
        when = f"{job.scheduled_date}"
        if job.scheduled_time:
            when += f" {str(job.scheduled_time)[:5]}"
        parts = [
            f"Hi {tech.name}, new job {job.job_number}: {job.get_job_type_display()} ({job.get_priority_display()}).",
            f"Customer: {job.customer_name}",
        ]
        if job.customer_phone:
            parts.append(f"Phone: {job.customer_phone}")
        if job.customer_address:
            parts.append(f"Location: {job.customer_address[:80]}")
        parts.append(f"When: {when}.")
        if company_name:
            parts.append(f"- {company_name}")
        queue_event_sms(tech.phone, " ".join(parts), recipient_name=tech.name, msg_type="dispatch")
    except Exception:
        logger.exception("Dispatch SMS failed for job %s", getattr(job, "pk", None))