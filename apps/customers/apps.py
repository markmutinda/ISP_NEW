from django.apps import AppConfig

class CustomersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.customers'
    verbose_name = 'Customer Management'

    def ready(self):
        """Import signals for RADIUS cleanup, IP release, user cleanup, and billing account generation."""
        try:
            # Import signals to register handlers
            from . import signals  # noqa: F401
        except ImportError as e:
            import logging
            logging.getLogger(__name__).warning(f"Could not import customer signals: {e}")