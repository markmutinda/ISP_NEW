import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def delayed_service_activation(self, service_id):
    """
    Auto-activate a PENDING service after a scheduled delay (e.g. 1 hour).
    Called via apply_async(countdown=...) from the service creation serializer.
    """
    from apps.customers.models import ServiceConnection

    try:
        service = ServiceConnection.objects.get(id=service_id)
    except ServiceConnection.DoesNotExist:
        logger.warning("delayed_service_activation: Service %s not found", service_id)
        return

    if service.status != 'PENDING':
        logger.info(
            "delayed_service_activation: Service %s is already %s, skipping",
            service_id, service.status,
        )
        return

    try:
        service.activate_service()
        logger.info("delayed_service_activation: Service %s activated successfully", service_id)
    except Exception as exc:
        logger.error("delayed_service_activation: Failed to activate service %s: %s", service_id, exc)
        raise self.retry(exc=exc)
