import uuid

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("superadmin", "0006_platform_expenditure_reverses")]

    operations = [
        migrations.CreateModel(
            name="BusinessAccountTransfer",
            fields=[
                ("id", models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False, serialize=False)),
                ("reference", models.CharField(max_length=100, unique=True)),
                ("amount", models.DecimalField(max_digits=14, decimal_places=2)),
                ("effective_at", models.DateTimeField(db_index=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"ordering": ["-effective_at", "-created_at"]},
        ),
    ]
