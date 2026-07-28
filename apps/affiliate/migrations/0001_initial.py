import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import apps.affiliate.models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("core", "0020_user_custom_allowed_paths"),
    ]

    operations = [
        migrations.CreateModel(
            name="AffiliateAccount",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("referral_code", models.CharField(db_index=True, default=apps.affiliate.models.generate_referral_code, max_length=16, unique=True)),
                ("status", models.CharField(choices=[("active", "Active"), ("inactive", "Inactive"), ("suspended", "Suspended")], db_index=True, default="active", max_length=12)),
                ("country", models.CharField(max_length=80)),
                ("currency", models.CharField(default="KES", max_length=3)),
                ("tier", models.CharField(choices=[("bronze", "Bronze"), ("silver", "Silver"), ("gold", "Gold")], default="bronze", max_length=12)),
                ("is_verified", models.BooleanField(default=False)),
                ("verified_at", models.DateTimeField(blank=True, null=True)),
                ("payment_method", models.CharField(choices=[("mpesa", "M-Pesa"), ("bank", "Bank transfer")], default="mpesa", max_length=12)),
                ("mpesa_phone", models.CharField(blank=True, max_length=20)),
                ("mpesa_name", models.CharField(blank=True, max_length=120)),
                ("bank_name", models.CharField(blank=True, max_length=120)),
                ("bank_account", models.CharField(blank=True, max_length=120)),
                ("bank_branch", models.CharField(blank=True, max_length=120)),
                ("payment_verified", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="affiliate_account", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="AffiliateClick",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("attribution_token", models.UUIDField(db_index=True, unique=True)),
                ("source", models.CharField(default="Direct", max_length=100)),
                ("landing_url", models.URLField(blank=True, max_length=1000)),
                ("referrer", models.URLField(blank=True, max_length=1000)),
                ("ip_hash", models.CharField(blank=True, max_length=64)),
                ("user_agent", models.CharField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("affiliate", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="clicks", to="affiliate.affiliateaccount")),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="AffiliateReferral",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("signup_email", models.EmailField(max_length=254)),
                ("company_name", models.CharField(blank=True, max_length=255)),
                ("status", models.CharField(choices=[("pending", "Pending review"), ("paid", "Commission paid"), ("churned", "Rejected or churned")], db_index=True, default="pending", max_length=12)),
                ("reward_amount", models.DecimalField(decimal_places=2, default=0, help_text="Set manually by a superadmin.", max_digits=12, validators=[django.core.validators.MinValueValidator(0)])),
                ("currency", models.CharField(default="KES", max_length=3)),
                ("admin_notes", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("affiliate", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="referrals", to="affiliate.affiliateaccount")),
                ("click", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="signups", to="affiliate.affiliateclick")),
                ("company", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="core.company")),
                ("lead", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="core.lead")),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.CreateModel(
            name="AffiliatePayout",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12, validators=[django.core.validators.MinValueValidator(0.01)])),
                ("currency", models.CharField(default="KES", max_length=3)),
                ("method", models.CharField(choices=[("mpesa", "M-Pesa"), ("bank", "Bank transfer")], max_length=12)),
                ("status", models.CharField(choices=[("pending", "Pending"), ("completed", "Completed"), ("failed", "Failed")], db_index=True, default="pending", max_length=12)),
                ("reference", models.CharField(blank=True, max_length=100)),
                ("notes", models.TextField(blank=True)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("affiliate", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="payouts", to="affiliate.affiliateaccount")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="affiliate_payouts_created", to=settings.AUTH_USER_MODEL)),
            ],
            options={"ordering": ("-created_at",)},
        ),
        migrations.AddConstraint(
            model_name="affiliatereferral",
            constraint=models.UniqueConstraint(fields=("signup_email",), name="unique_affiliate_signup_email"),
        ),
        migrations.AddIndex(
            model_name="affiliateclick",
            index=models.Index(fields=["affiliate", "created_at"], name="affiliate_a_affilia_58b8bf_idx"),
        ),
    ]
