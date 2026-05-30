"""
Run django-tenants migrations with defensive patches for historical graph drift.

This command exists because some production databases contain old migration
history from earlier branches. During Django's project-state rebuild, certain
historical operations can fail even though the live schema is already usable:

- RemoveField can raise KeyError if the field is already absent from state.
- RenameIndex can raise ValueError if the old historical index name is missing.

We patch those legacy cases, then delegate to the normal migrate_schemas
command.
"""

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import ProgrammingError
from django.db.migrations.operations.fields import AddField
from django.db.migrations.operations.models import RenameIndex
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
        original_add_field_forwards = AddField.database_forwards
        original_rename_index_forwards = RenameIndex.database_forwards
        original_rename_index_backwards = RenameIndex.database_backwards

        def resilient_remove_field(state, app_label, model_name, name):
            model_state = state.models.get((app_label, model_name))
            if model_state is None:
                return None
            if name not in model_state.fields:
                return None
            return original_remove_field(state, app_label, model_name, name)

        def _skip_missing_legacy_index(app_label, operation, model_state, schema_editor):
            table_name = model_state.options.get("db_table") or f"{app_label}_{model_state.name_lower}"
            constraints = schema_editor.connection.introspection.get_constraints(
                schema_editor.connection.cursor(),
                table_name,
            )
            if operation.new_name in constraints:
                return True
            if operation.old_name not in constraints:
                return True
            return False

        def resilient_rename_index_forwards(operation, app_label, schema_editor, from_state, to_state):
            try:
                return original_rename_index_forwards(
                    operation,
                    app_label,
                    schema_editor,
                    from_state,
                    to_state,
                )
            except ValueError as exc:
                if "No index named" not in str(exc):
                    raise

                model_state = from_state.models.get((app_label, operation.model_name_lower))
                if model_state is None or not _skip_missing_legacy_index(app_label, operation, model_state, schema_editor):
                    raise

                self.stdout.write(
                    self.style.WARNING(
                        f"Skipping legacy RenameIndex for {app_label}.{model_state.name}: "
                        f"{operation.old_name} -> {operation.new_name}"
                    )
                )
                return None

        def _column_exists(table_name, column_name, schema_editor):
            with schema_editor.connection.cursor() as cursor:
                description = schema_editor.connection.introspection.get_table_description(cursor, table_name)
            return any(col.name == column_name for col in description)

        def _resolve_field_column(field, field_name):
            clone = field.clone()
            clone.set_attributes_from_name(field_name)
            return clone.db_column or clone.get_attname_column()[1]

        def resilient_add_field_forwards(operation, app_label, schema_editor, from_state, to_state):
            try:
                return original_add_field_forwards(
                    operation,
                    app_label,
                    schema_editor,
                    from_state,
                    to_state,
                )
            except ProgrammingError as exc:
                if 'already exists' not in str(exc):
                    raise

                model_state = to_state.models.get((app_label, operation.model_name_lower))
                if model_state is None:
                    raise

                table_name = model_state.options.get("db_table") or f"{app_label}_{model_state.name_lower}"
                field = model_state.get_field(operation.name)
                column_name = _resolve_field_column(field, operation.name)
                if not _column_exists(table_name, column_name, schema_editor):
                    raise

                self.stdout.write(
                    self.style.WARNING(
                        f"Skipping legacy AddField for {app_label}.{model_state.name}.{operation.name}: "
                        f"column {column_name} already exists on {table_name}"
                    )
                )
                return None

        def resilient_rename_index_backwards(operation, app_label, schema_editor, from_state, to_state):
            try:
                return original_rename_index_backwards(
                    operation,
                    app_label,
                    schema_editor,
                    from_state,
                    to_state,
                )
            except ValueError as exc:
                if "No index named" not in str(exc):
                    raise

                model_state = to_state.models.get((app_label, operation.model_name_lower))
                if model_state is None or not _skip_missing_legacy_index(app_label, operation, model_state, schema_editor):
                    raise

                self.stdout.write(
                    self.style.WARNING(
                        f"Skipping legacy reverse RenameIndex for {app_label}.{model_state.name}: "
                        f"{operation.new_name} -> {operation.old_name}"
                    )
                )
                return None

        ProjectState.remove_field = resilient_remove_field
        AddField.database_forwards = resilient_add_field_forwards
        RenameIndex.database_forwards = resilient_rename_index_forwards
        RenameIndex.database_backwards = resilient_rename_index_backwards
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
            AddField.database_forwards = original_add_field_forwards
            RenameIndex.database_forwards = original_rename_index_forwards
            RenameIndex.database_backwards = original_rename_index_backwards
