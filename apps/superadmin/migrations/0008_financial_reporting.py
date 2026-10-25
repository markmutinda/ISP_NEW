import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("superadmin", "0007_business_account_transfer"),
    ]

    operations = [
        migrations.AddField(
            model_name="platformexpenditure",
            name="entry_type",
            field=models.CharField(
                choices=[
                    ("operating_expense", "Operating expense"),
                    ("bank_fee", "Bank or transaction fee"),
                    ("owner_draw", "Owner or team distribution"),
                    ("cash_adjustment", "Cash reconciliation adjustment"),
                ],
                db_index=True,
                default="operating_expense",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="platformexpenditure",
            name="reference",
            field=models.CharField(blank=True, db_index=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="businessaccounttransfer",
            name="from_ledger",
            field=models.CharField(
                choices=[("primary", "Original Business Account"), ("new_business", "New Business Account")],
                default="primary",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="businessaccounttransfer",
            name="to_ledger",
            field=models.CharField(
                choices=[("primary", "Original Business Account"), ("new_business", "New Business Account")],
                default="new_business",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="businessaccounttransfer",
            name="notes",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.CreateModel(
            name="BusinessAccountBalanceSnapshot",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("ledger", models.CharField(choices=[("primary", "Original Business Account"), ("new_business", "New Business Account")], db_index=True, max_length=32)),
                ("balance", models.DecimalField(decimal_places=2, max_digits=14)),
                ("balance_at", models.DateTimeField(db_index=True)),
                ("reference", models.CharField(blank=True, default="", max_length=120)),
                ("notes", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="business_account_balance_snapshots_created", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-balance_at", "-created_at"]},
        ),
        migrations.AddIndex(
            model_name="businessaccountbalancesnapshot",
            index=models.Index(fields=["ledger", "balance_at"], name="sadm_bal_ledger_at_idx"),
        ),
        migrations.AddConstraint(
            model_name="businessaccountbalancesnapshot",
            constraint=models.UniqueConstraint(fields=("ledger", "balance_at"), name="sadm_unique_ledger_balance_at"),
        ),
    ]
