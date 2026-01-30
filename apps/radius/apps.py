from django.apps import AppConfig


class RadiusConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.radius'
    verbose_name = 'RADIUS Management'

    def ready(self):
        """
        Import signals when app is ready for auto-sync with RADIUS.
        
        This enables automatic synchronization:
        - Customer → RADIUS credentials
        - ServiceConnection → RADIUS user enable/disable
        - Plan changes → Bandwidth updates
        - Invoice/Payment → Auto-suspend/restore
        - Router → NAS entries
        - Tenant creation → RADIUS configuration
        """
        try:
            from . import signals  # noqa: F401 - Original signals
            from . import signals_auto_sync  # noqa: F401 - New auto-sync signals
        except ImportError as e:
            import logging
            logging.getLogger(__name__).warning(f"Could not import RADIUS signals: {e}")
