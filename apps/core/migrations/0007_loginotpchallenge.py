from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0006_emailotp"),
    ]

    operations = [
        migrations.CreateModel(
            name="LoginOTPChallenge",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("tenant_scope", models.CharField(blank=True, default="", max_length=120)),
                ("session_scope", models.CharField(blank=True, default="", max_length=255)),
                ("failed_attempts", models.PositiveSmallIntegerField(default=0)),
                ("resend_count", models.PositiveSmallIntegerField(default=0)),
                ("is_completed", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("last_sent_at", models.DateTimeField(auto_now_add=True)),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="login_otp_challenges", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddField(
            model_name="emailotp",
            name="login_challenge",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="otps", to="core.loginotpchallenge"),
        ),
        migrations.AddIndex(
            model_name="loginotpchallenge",
            index=models.Index(fields=["user", "created_at"], name="core_loginot_user_id_e201ca_idx"),
        ),
        migrations.AddIndex(
            model_name="loginotpchallenge",
            index=models.Index(fields=["tenant_scope", "session_scope"], name="core_loginot_tenant__a61194_idx"),
        ),
        migrations.AddIndex(
            model_name="loginotpchallenge",
            index=models.Index(fields=["expires_at", "is_completed"], name="core_loginot_expires_8ec9a1_idx"),
        ),
    ]
