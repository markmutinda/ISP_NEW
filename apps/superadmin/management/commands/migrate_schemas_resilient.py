"""
Run django-tenants migrations with a defensive patch for historical graph drift.

This command exists because some production databases contain old migration
history from earlier branches. During Django's project-state rebuild, certain
historical RemoveField operations can raise KeyError if the field is already
absent from state even though the live schema is fine.

We patch ProjectState.remove_field to behave defensively for these legacy cases,
then delegate to the normal migrate_schemas command.
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db.migrations.state import ProjectState


class Command(BaseCommand):
    help = "Run migrate_schemas with resilient state handling for legacy migration history"

    def add_arguments(self, parser):
        parser.add_argument("--shared", action="store_true", help="Migrate shared/public schema only")
        parser.add_argument("--tenant", action="store_true", help="Migrate tenant schemas only")
        parser.add_argument("--schema", type=str, help="Migrate a single schema")
        parser.add_argument("--fake", action="store_true", help="Mark migrations as run without running SQL")

    def handle(self, *args, **options):
        original_remove_field = ProjectState.remove_field

        def resilient_remove_field(state, app_label, model_name, name):
            model_state = state.models.get((app_label, model_name))
            if model_state is None:
                return None
            if name not in model_state.fields:
                return None
            return original_remove_field(state, app_label, model_name, name)

        ProjectState.remove_field = resilient_remove_field
        try:
            call_command(
                "migrate_schemas",
                shared=options.get("shared", False),
                tenant=options.get("tenant", False),
                schema_name=options.get("schema"),
                fake=options.get("fake", False),
            )
        finally:
            ProjectState.remove_field = original_remove_field
