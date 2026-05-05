from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_lead_message"),
    ]

    operations = [
        migrations.CreateModel(
            name="EmailOTP",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("purpose", models.CharField(choices=[("login", "Tenant Login"), ("payment_method_change", "Payment Method Verification")], db_index=True, default="login", max_length=40)),
                ("code", models.CharField(max_length=6)),
                ("failed_attempts", models.PositiveSmallIntegerField(default=0)),
                ("is_used", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("verified_at", models.DateTimeField(blank=True, null=True)),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="email_otps", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="emailotp",
            index=models.Index(fields=["user", "purpose", "created_at"], name="core_emailot_user_id_fec6cf_idx"),
        ),
        migrations.AddIndex(
            model_name="emailotp",
            index=models.Index(fields=["expires_at", "is_used"], name="core_emailot_expires_216fdb_idx"),
        ),
    ]
