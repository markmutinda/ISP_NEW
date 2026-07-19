# Generated manually for per-user dashboard access overrides.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0019_roleaccesspolicy_created_by_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="custom_allowed_paths",
            field=models.JSONField(
                blank=True,
                default=None,
                help_text=(
                    "Per-user dashboard permission tokens. Null inherits the role "
                    "policy; an explicit list overrides it."
                ),
                null=True,
            ),
        ),
    ]
