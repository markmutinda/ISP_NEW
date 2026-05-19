from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0008_globalsystemsettings_admin_email_otp_enabled"),
    ]

    operations = [
        migrations.AlterField(
            model_name="routertenantindex",
            name="router_id",
            field=models.BigIntegerField(db_index=True),
        ),
        migrations.AddIndex(
            model_name="routertenantindex",
            index=models.Index(
                fields=["tenant_schema", "router_id"],
                name="core_router_tenant__b3e645_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="routertenantindex",
            constraint=models.UniqueConstraint(
                fields=("tenant_schema", "router_id"),
                name="uniq_routertenantindex_schema_router_id",
            ),
        ),
    ]
