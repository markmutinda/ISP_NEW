from django.apps import AppConfig

class CustomersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.customers'

    def ready(self):
        """Import signals for RADIUS cleanup, IP release, and user cleanup."""
        try:
            # Import signals to register handlers
            from . import signals  # noqa: F401
        except ImportError as e:
            import logging
            logging.getLogger(__name__).warning(f"Could not import customer signals: {e}")