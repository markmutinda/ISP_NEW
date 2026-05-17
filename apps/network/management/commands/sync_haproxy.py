from django.core.management.base import BaseCommand
from apps.network.services.haproxy_manager import sync_haproxy_config

class Command(BaseCommand):
    help = 'Regenerate HAProxy config and reload'

    def handle(self, *args, **options):
        self.stdout.write("Syncing HAProxy config...")
        success = sync_haproxy_config()
        if success:
            self.stdout.write(self.style.SUCCESS("HAProxy synced successfully"))
        else:
            self.stdout.write(self.style.ERROR("HAProxy sync failed — check logs"))