import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("superadmin", "0003_superadmin_activity_log"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PlatformExpenditure",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "category",
                    models.CharField(
                        choices=[
                            ("infrastructure", "Infrastructure"),
                            ("sms", "SMS Costs"),
                            ("payroll", "Payroll"),
                            ("marketing", "Marketing"),
                            ("software", "Software"),
                            ("operations", "Operations"),
                            ("tax", "Tax"),
                            ("other", "Other"),
                        ],
                        default="operations",
                        max_length=40,
                    ),
                ),
                ("title", models.CharField(max_length=160)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14)),
                ("currency", models.CharField(default="KES", max_length=8)),
                ("incurred_on", models.DateField(db_index=True)),
                ("notes", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="platform_expenditures_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-incurred_on", "-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="platformexpenditure",
            index=models.Index(fields=["category", "incurred_on"], name="sadm_exp_cat_date_idx"),
        ),
        migrations.AddIndex(
            model_name="platformexpenditure",
            index=models.Index(fields=["incurred_on"], name="sadm_exp_date_idx"),
        ),
    ]
