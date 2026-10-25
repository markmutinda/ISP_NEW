import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("superadmin", "0008_financial_reporting"),
    ]

    operations = [
        migrations.AddField(
            model_name="businessaccountbalancesnapshot",
            name="system_balance_before",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
        ),
        migrations.AddField(
            model_name="businessaccountbalancesnapshot",
            name="reconciliation_difference",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True),
        ),
        migrations.CreateModel(
            name="PlatformReceiptAllocation",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("source_type", models.CharField(choices=[("subscription", "Subscription payment"), ("sms", "Tenant SMS top-up")], max_length=24)),
                ("source_id", models.CharField(max_length=160)),
                ("reference", models.CharField(blank=True, default="", max_length=120)),
                ("ledger", models.CharField(choices=[("primary", "Original Business Account"), ("new_business", "New Business Account")], db_index=True, max_length=32)),
                ("occurred_at", models.DateTimeField(blank=True, null=True)),
                ("notes", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="platform_receipt_allocations_created", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-occurred_at", "-updated_at"]},
        ),
        migrations.AddConstraint(
            model_name="platformreceiptallocation",
            constraint=models.UniqueConstraint(fields=("source_type", "source_id"), name="sadm_unique_receipt_allocation"),
        ),
    ]
