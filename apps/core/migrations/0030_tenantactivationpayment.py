import uuid
from django.db import migrations, models
from django.conf import settings
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0029_tr069deviceindex"),
    ]

    operations = [
        migrations.CreateModel(
            name="TenantActivationPayment",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("processing", "Processing"), ("completed", "Completed"), ("provisioning", "Provisioning"), ("consumed", "Account created"), ("failed", "Failed"), ("expired", "Expired")], db_index=True, default="pending", max_length=20)),
                ("amount", models.DecimalField(decimal_places=2, default="500.00", max_digits=10)),
                ("currency", models.CharField(default="KES", max_length=3)),
                ("phone_number", models.CharField(max_length=20)),
                ("registration_data", models.JSONField(default=dict)),
                ("checkout_request_id", models.CharField(blank=True, max_length=120, null=True, unique=True)),
                ("merchant_request_id", models.CharField(blank=True, default="", max_length=120)),
                ("account_reference", models.CharField(db_index=True, max_length=40, unique=True)),
                ("mpesa_receipt", models.CharField(blank=True, default="", max_length=50)),
                ("failure_reason", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("company", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="activation_payments", to="core.company")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="tenant_activation_payments", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="tenantactivationpayment",
            index=models.Index(fields=["status", "created_at"], name="core_tenant_status_03d25c_idx"),
        ),
    ]
