from django.apps import AppConfig


class RadiusConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'apps.radius'
    verbose_name = 'RADIUS Management'

    def ready(self):
        # Import signals when app is ready
        pass
