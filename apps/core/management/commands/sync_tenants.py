# apps/core/management/commands/sync_tenants.py
from django.core.management.base import BaseCommand
from django.db import connection
from apps.core.models import Tenant
from django.core.management import call_command

class Command(BaseCommand):
    help = 'Sync all tenant schemas with migrations'

    def handle(self, *args, **options):
        tenants = Tenant.objects.all()
        
        for tenant in tenants:
            self.stdout.write(f"Syncing tenant: {tenant.subdomain}...")
            
            # Switch to tenant
            connection.set_tenant(tenant)
            
            # Run migrations
            call_command('migrate', verbosity=0)
            
            # Switch back to public
            connection.set_schema_to_public()
            
            self.stdout.write(f"✓ Synced {tenant.subdomain}")
        
        self.stdout.write(self.style.SUCCESS(f"Synced {len(tenants)} tenants"))