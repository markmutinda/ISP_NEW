# apps/network/apps.py
from django.apps import AppConfig


class NetworkConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.network'
    verbose_name = 'Network Management'
    
    def ready(self):
        """
        Import signals when the app is ready.
        This ensures the router → NAS sync signal is registered.
        """
        import apps.network.signals  # Register signal handlers

