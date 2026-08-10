from django.apps import AppConfig

class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.core'
    label = 'core'

    def ready(self):
        """
        Register signal handlers when the app is ready.
        This ensures tenant cache is invalidated when tenants are created,
        updated, or deleted.
        """
        from django.db.models.signals import post_save, post_delete
        from django.dispatch import receiver
        from .models import Tenant
        from .tenant_cache import invalidate_tenant_cache

        @receiver(post_save, sender=Tenant)
        @receiver(post_delete, sender=Tenant)
        def _clear_tenant_cache(sender, instance, **kwargs):
            """
            Clear tenant cache when a tenant is saved or deleted.
            This ensures resolve_tenant_cached() always returns fresh data.
            """
            try:
                invalidate_tenant_cache(
                    subdomain=instance.subdomain, 
                    schema_name=instance.schema_name
                )
            except Exception as e:
                # Log but don't crash the request - cache will eventually expire
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(f"Failed to invalidate tenant cache for {instance.subdomain}: {e}")