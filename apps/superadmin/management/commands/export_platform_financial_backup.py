import json

from django.core.management.base import BaseCommand
from django_tenants.utils import get_public_schema_name, schema_context

from apps.superadmin.financial_backup import build_financial_backup


class Command(BaseCommand):
    help = "Export financial checkpoints, mappings, transfers and cash events as JSON."

    def handle(self, *args, **options):
        with schema_context(get_public_schema_name()):
            self.stdout.write(json.dumps(build_financial_backup(), indent=2))

