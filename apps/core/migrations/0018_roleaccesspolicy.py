from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0017_globalsystemsettings_hide_lower_plans_in_customer_portal"),
    ]

    operations = [
        migrations.CreateModel(
            name="RoleAccessPolicy",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "role",
                    models.CharField(
                        choices=[
                            ("admin", "Administrator"),
                            ("staff", "Staff Member"),
                            ("technician", "Technician"),
                            ("customer", "Customer"),
                            ("accountant", "Accountant"),
                            ("support", "Support Agent"),
                        ],
                        max_length=20,
                    ),
                ),
                ("allowed_paths", models.JSONField(blank=True, default=list)),
            ],
            options={
                "verbose_name": "Role Access Policy",
                "verbose_name_plural": "Role Access Policies",
                "ordering": ["role"],
            },
        ),
        migrations.AddConstraint(
            model_name="roleaccesspolicy",
            constraint=models.UniqueConstraint(fields=("role",), name="unique_role_access_policy_role"),
        ),
    ]
